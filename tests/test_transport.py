"""REQ-5.4.1 containment tests.

The concession `dev` makes is real, so the clauses containing it are the part that
must not quietly rot. Each test below maps to one of them.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from apt_log.probe import usb_devices
from apt_log.transport import (
    ProductionRefused,
    TransportMode,
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


@pytest.fixture
def config(tmp_path):
    """Point CONFIG_PATH at a temp file; return a writer for it."""
    path = tmp_path / "transport.conf"
    with patch("apt_log.transport.CONFIG_PATH", path):
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
