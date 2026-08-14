"""REQ-5.4.1 containment tests.

The concession `dev` makes is real, so the clauses containing it are the part that
must not quietly rot. Each test below maps to one of them.
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import patch

import pytest

from apt_log.probe import usb_devices
from apt_log.transport import (
    ProductionRefused,
    TransportMode,
    _config_is_trustworthy,
    assert_production_allowed,
    attached_devices,
    resolve_transport_mode,
    transport_precondition_satisfied,
)


def _adb_output(text: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=text, stderr="")


BOTH_ATTACHED = (
    "List of devices attached\n"
    "192.168.1.50:5555\tdevice\n"
    "R58M12345XY\tdevice\n"
)
ONLY_TCP = "List of devices attached\n192.168.1.50:5555\tdevice\n"


def _fake_stat(uid: int = 0, mode: int = 0o644) -> os.stat_result:
    """A stat result with the ownership bits we care about.

    Ownership is faked rather than produced by a real chown so the suite behaves
    identically whether it runs as root in a container or as the service user on
    the Pi. The real-filesystem case is covered separately below.
    """
    return os.stat_result((0o100000 | mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))


@pytest.fixture
def config(tmp_path):
    """Point CONFIG_PATH at a temp file; return a writer for it.

    Defaults to a root-owned 0644 config so the transport-mode tests exercise
    parsing rather than ownership. Ownership has its own tests.
    """
    path = tmp_path / "transport.conf"
    with patch("apt_log.transport.CONFIG_PATH", path), \
         patch("apt_log.transport._stat_config", return_value=_fake_stat()):
        yield lambda text: path.write_text(text, encoding="utf-8")


class TestDefaultIsUsb:
    def test_missing_config_is_usb(self, tmp_path):
        with patch("apt_log.transport.CONFIG_PATH", tmp_path / "absent.conf"):
            assert resolve_transport_mode() is TransportMode.USB

    def test_empty_config_is_usb(self, config):
        config("")
        assert resolve_transport_mode() is TransportMode.USB

    def test_unrecognised_value_is_usb(self, config):
        # A corrupt config must not silently unlock the permissive mode.
        config("TRANSPORT_MODE=banana\n")
        assert resolve_transport_mode() is TransportMode.USB

    def test_comments_and_blanks_ignored(self, config):
        config("# TRANSPORT_MODE=dev\n\n")
        assert resolve_transport_mode() is TransportMode.USB

    def test_dev_is_honoured_when_explicitly_set(self, config):
        config("TRANSPORT_MODE=dev\n")
        assert resolve_transport_mode() is TransportMode.DEV

    def test_quoted_and_cased_values_accepted(self, config):
        config('TRANSPORT_MODE = "DEV"\n')
        assert resolve_transport_mode() is TransportMode.DEV


class TestConfigMustBeRootOwned:
    """The module defends itself rather than trusting firstboot.sh to have run.

    `dev` is justified as an act by someone with root; a config the service user
    could write would let the agent switch off its own containment.
    """

    def test_root_owned_and_not_group_writable_is_trusted(self):
        with patch("apt_log.transport._stat_config", return_value=_fake_stat(0, 0o644)):
            assert _config_is_trustworthy() is True

    def test_non_root_owner_is_refused(self):
        with patch("apt_log.transport._stat_config", return_value=_fake_stat(1000, 0o644)):
            assert _config_is_trustworthy() is False

    def test_group_writable_is_refused(self):
        with patch("apt_log.transport._stat_config", return_value=_fake_stat(0, 0o664)):
            assert _config_is_trustworthy() is False

    def test_world_writable_is_refused(self):
        with patch("apt_log.transport._stat_config", return_value=_fake_stat(0, 0o666)):
            assert _config_is_trustworthy() is False

    def test_absent_config_is_not_trusted(self):
        with patch("apt_log.transport._stat_config", return_value=None):
            assert _config_is_trustworthy() is False

    def test_dev_is_refused_when_the_config_is_not_root_owned(self, tmp_path):
        """End to end: a service-user-writable config cannot enable dev."""
        path = tmp_path / "transport.conf"
        path.write_text("TRANSPORT_MODE=dev\n", encoding="utf-8")
        with patch("apt_log.transport.CONFIG_PATH", path), \
             patch("apt_log.transport._stat_config", return_value=_fake_stat(1000, 0o644)):
            assert resolve_transport_mode() is TransportMode.USB

    def test_real_filesystem_config_owned_by_current_user(self, tmp_path):
        """Sanity check against a real file, whoever the suite runs as.

        Root in a container writes a root-owned file and should be trusted; the
        service user on the Pi writes one owned by itself and should not.
        """
        path = tmp_path / "transport.conf"
        path.write_text("TRANSPORT_MODE=dev\n", encoding="utf-8")
        with patch("apt_log.transport.CONFIG_PATH", path):
            expected = TransportMode.DEV if os.geteuid() == 0 else TransportMode.USB
            assert resolve_transport_mode() is expected


class TestNotEnableableByEnvironment:
    """REQ-5.4.1: never an environment variable a stray shell could set."""

    def test_env_var_does_not_enable_dev(self, config, monkeypatch):
        config("")
        monkeypatch.setenv("TRANSPORT_MODE", "dev")
        monkeypatch.setenv("APTLOG_TRANSPORT_MODE", "dev")
        assert resolve_transport_mode() is TransportMode.USB


class TestReq54RejectionUnweakened:
    """The 5.4 default path is untouched by anything in this module."""

    def test_usb_devices_still_drops_tcp_entries(self):
        with patch("apt_log.probe._adb", return_value=_adb_output(BOTH_ATTACHED)):
            assert usb_devices() == ["R58M12345XY"]

    def test_usb_mode_sees_only_the_usb_device(self, config):
        config("TRANSPORT_MODE=usb\n")
        with patch("apt_log.probe._adb", return_value=_adb_output(BOTH_ATTACHED)):
            serials, mode = attached_devices()
        assert serials == ["R58M12345XY"]
        assert mode is TransportMode.USB

    def test_usb_mode_sees_nothing_when_only_tcp_is_attached(self, config):
        config("TRANSPORT_MODE=usb\n")
        with patch("apt_log.probe._adb", return_value=_adb_output(ONLY_TCP)):
            serials, mode = attached_devices()
        assert serials == []
        assert mode is TransportMode.USB

    def test_dev_mode_admits_the_tcp_device(self, config):
        config("TRANSPORT_MODE=dev\n")
        with patch("apt_log.transport._adb", return_value=_adb_output(ONLY_TCP)):
            serials, mode = attached_devices()
        assert serials == ["192.168.1.50:5555"]
        assert mode is TransportMode.DEV


class TestPrecondition:
    def test_dev_satisfies_the_transport_precondition(self, config):
        config("TRANSPORT_MODE=dev\n")
        assert transport_precondition_satisfied() is True

    def test_usb_precondition_requires_a_usb_device(self, config):
        config("TRANSPORT_MODE=usb\n")
        with patch("apt_log.probe._adb", return_value=_adb_output(ONLY_TCP)):
            assert transport_precondition_satisfied() is False

    def test_usb_precondition_met_with_a_usb_device(self, config):
        config("TRANSPORT_MODE=usb\n")
        with patch("apt_log.probe._adb", return_value=_adb_output(BOTH_ATTACHED)):
            assert transport_precondition_satisfied() is True


class TestProductionGuard:
    """REQ-0: both development affordances are refusals, for the same reason."""

    def test_refuses_while_dev_transport_active(self):
        with pytest.raises(ProductionRefused, match="transport_mode"):
            assert_production_allowed(TransportMode.DEV, "PhoneLocationSource")

    def test_refuses_on_stub_location_source(self):
        with pytest.raises(ProductionRefused, match="location source"):
            assert_production_allowed(TransportMode.USB, "StubLocationSource")

    def test_allows_when_both_affordances_are_off(self):
        assert_production_allowed(TransportMode.USB, "PhoneLocationSource") is None
