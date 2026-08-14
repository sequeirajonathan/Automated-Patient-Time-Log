"""REQ-2 and the OPERATIONS §1.1 recovery ladder.

The properties that matter here are behavioural, not cosmetic: the ladder must
escalate in cost order, must skip rungs that cannot help, and must *stop*. A
ladder that loops forever looks exactly like a working system to anyone reading
a dashboard.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import call, patch

import pytest

from apt_log.device import (
    DeviceRecovery,
    DeviceSession,
    DeviceUnavailable,
    Rung,
)
from apt_log.secrets import MemorySecretProvider
from apt_log.transport import TransportMode


def _present(serial="R58M12345XY", mode=TransportMode.USB):
    return (True, serial, mode)


def _absent(mode=TransportMode.USB):
    return (False, None, mode)


class TestLadderEscalation:
    def test_no_rungs_tried_when_device_is_already_there(self):
        with patch("apt_log.device._device_present", return_value=_present()):
            outcome = DeviceRecovery().recover()
        assert outcome.recovered
        assert outcome.rungs_tried == []

    def test_stops_at_the_first_rung_that_works(self):
        # absent, then present after adb reconnect
        with patch("apt_log.device._device_present",
                   side_effect=[_absent(), _present()]), \
             patch.object(DeviceRecovery, "_adb_reconnect") as reconnect, \
             patch.object(DeviceRecovery, "_adb_server_restart") as restart:
            outcome = DeviceRecovery(settle_seconds=0).recover()
        assert outcome.recovered
        assert outcome.rungs_tried == [Rung.ADB_RECONNECT]
        reconnect.assert_called_once()
        restart.assert_not_called()

    def test_escalates_in_cost_order(self):
        with patch("apt_log.device._device_present",
                   side_effect=[_absent(), _absent(), _absent(), _present()]), \
             patch.object(DeviceRecovery, "_adb_reconnect"), \
             patch.object(DeviceRecovery, "_adb_server_restart"), \
             patch.object(DeviceRecovery, "_usb_power_cycle"):
            outcome = DeviceRecovery(
                settle_seconds=0, uhubctl_location="1-1"
            ).recover()
        assert outcome.rungs_tried == [
            Rung.ADB_RECONNECT,
            Rung.ADB_SERVER_RESTART,
            Rung.USB_POWER_CYCLE,
        ]

    def test_a_failing_rung_does_not_abort_the_ladder(self):
        # A rung that throws is skipped without a presence re-check, so only two
        # readings are consumed: the initial one, and the one after the restart.
        with patch("apt_log.device._device_present",
                   side_effect=[_absent(), _present()]), \
             patch.object(DeviceRecovery, "_adb_reconnect",
                          side_effect=RuntimeError("boom")), \
             patch.object(DeviceRecovery, "_adb_server_restart") as restart:
            outcome = DeviceRecovery(settle_seconds=0).recover()
        assert outcome.recovered
        assert outcome.rungs_tried == [Rung.ADB_RECONNECT, Rung.ADB_SERVER_RESTART]
        restart.assert_called_once()


class TestLadderTerminates:
    """Rung 7 exists so the system cannot retry forever in silence."""

    def test_gives_up_and_alerts(self):
        alerts = []
        with patch("apt_log.device._device_present", return_value=_absent()), \
             patch.object(DeviceRecovery, "_adb_reconnect"), \
             patch.object(DeviceRecovery, "_adb_server_restart"), \
             patch.object(DeviceRecovery, "_usb_power_cycle"), \
             patch.object(DeviceRecovery, "_reboot_host"):
            outcome = DeviceRecovery(
                settle_seconds=0,
                uhubctl_location="1-1",
                allow_host_reboot=False,
                alert=alerts.append,
            ).recover()
        assert not outcome.recovered
        assert outcome.rungs_tried[-1] is Rung.GIVE_UP
        assert len(alerts) == 1

    def test_reboot_ends_the_ladder_without_claiming_recovery(self):
        with patch("apt_log.device._device_present", return_value=_absent()), \
             patch.object(DeviceRecovery, "_adb_reconnect"), \
             patch.object(DeviceRecovery, "_adb_server_restart"), \
             patch.object(DeviceRecovery, "_usb_power_cycle"), \
             patch.object(DeviceRecovery, "_reboot_host") as reboot:
            outcome = DeviceRecovery(settle_seconds=0, uhubctl_location="1-1").recover()
        reboot.assert_called_once()
        assert not outcome.recovered
        assert Rung.GIVE_UP not in outcome.rungs_tried


class TestDevTransportSkipsPowerCycle:
    """A TCP-attached phone is not on a hub port; cycling one would do nothing."""

    def test_power_cycle_skipped_under_dev_transport(self):
        with patch("apt_log.device._device_present",
                   side_effect=[_absent(TransportMode.DEV)] * 4), \
             patch.object(DeviceRecovery, "_adb_reconnect"), \
             patch.object(DeviceRecovery, "_adb_server_restart"), \
             patch.object(DeviceRecovery, "_usb_power_cycle") as cycle, \
             patch.object(DeviceRecovery, "_reboot_host"):
            outcome = DeviceRecovery(
                settle_seconds=0, uhubctl_location="1-1"
            ).recover()
        cycle.assert_not_called()
        assert Rung.USB_POWER_CYCLE not in outcome.rungs_tried


class TestSessionLifecycle:
    def test_ensure_device_raises_when_the_ladder_fails(self):
        session = DeviceSession("com.example.app", MemorySecretProvider())
        with patch("apt_log.device._device_present", return_value=_absent()), \
             patch.object(session.recovery, "recover") as recover:
            recover.return_value.recovered = False
            recover.return_value.detail = "gone"
            recover.return_value.rungs_tried = [Rung.GIVE_UP]
            with pytest.raises(DeviceUnavailable, match="gone"):
                session.ensure_device()

    def test_is_alive_detects_a_swapped_serial(self):
        session = DeviceSession("com.example.app", MemorySecretProvider())
        session.serial = "R58M12345XY"
        with patch("apt_log.device._device_present",
                   return_value=_present("DIFFERENT")):
            assert session.is_alive() is False

    def _traced(self, session, order):
        """Patch every lifecycle step to record that it ran, in order."""
        return [
            patch.object(session, name, side_effect=lambda n=name: order.append(n))
            for name in ("ensure_device", "wake_and_unlock", "cold_start",
                         "attach", "ensure_authenticated")
        ]

    def test_open_cold_starts_before_attaching(self):
        """REQ-2: a scheduled run starts from a known state, not from whatever
        the phone was left doing hours earlier."""
        session = DeviceSession("com.example.app", MemorySecretProvider())
        order: list[str] = []
        with ExitStack() as stack:
            for p in self._traced(session, order):
                stack.enter_context(p)
            session.open()
        assert order == ["ensure_device", "wake_and_unlock", "cold_start",
                         "attach", "ensure_authenticated"]

    def test_open_can_skip_the_cold_start_and_the_sign_in(self):
        """The login check drives these itself so it can report each one."""
        session = DeviceSession("com.example.app", MemorySecretProvider())
        order: list[str] = []
        with ExitStack() as stack:
            for p in self._traced(session, order):
                stack.enter_context(p)
            session.open(cold_start=False, authenticate=False)
        assert order == ["ensure_device", "wake_and_unlock", "attach"]

    def test_cold_start_force_stops_before_launching(self):
        session = DeviceSession("com.example.app", MemorySecretProvider())
        with patch("apt_log.device._adb") as adb, patch("apt_log.device.time.sleep"):
            session.cold_start()
        args = [c.args for c in adb.call_args_list]
        assert args[0] == ("shell", "am", "force-stop", "com.example.app")
        assert "monkey" in args[1]


class TestUnlock:
    def test_enters_the_pin_when_one_is_configured(self):
        session = DeviceSession(
            "com.example.app", MemorySecretProvider(PHONE_PIN="1234")
        )
        with patch("apt_log.device._adb") as adb:
            session.wake_and_unlock()
        flat = [c.args for c in adb.call_args_list]
        assert ("shell", "input", "text", "1234") in flat
        assert ("shell", "input", "keyevent", "66") in flat

    def test_swipe_only_lock_is_not_an_error(self):
        session = DeviceSession("com.example.app", MemorySecretProvider())
        with patch("apt_log.device._adb") as adb:
            session.wake_and_unlock()
        flat = [c.args for c in adb.call_args_list]
        assert not any("text" in a for a in flat)
        assert call("shell", "input", "keyevent", "82") in adb.call_args_list
