"""Unit tests for the probe's decision logic.

These deliberately need no device and no Appium server: they cover the reasoning
that decides whether the project is viable, which is the part that must not be
wrong. The device-dependent paths are exercised by running the probe for real.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from apt_log.probe import CheckResult, ProbeReport, Status, usb_devices


def _adb_output(text: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=text, stderr="")


class TestUsbDevices:
    """REQ-5.4: adb over TCP is rejected in code, not by convention."""

    def test_accepts_usb_serial(self):
        out = "List of devices attached\nR58M12345XY\tdevice\n"
        with patch("apt_log.probe._adb", return_value=_adb_output(out)):
            assert usb_devices() == ["R58M12345XY"]

    def test_rejects_tcp_transport(self):
        out = "List of devices attached\n192.168.1.50:5555\tdevice\n"
        with patch("apt_log.probe._adb", return_value=_adb_output(out)):
            assert usb_devices() == []

    def test_rejects_tcp_but_keeps_usb_alongside_it(self):
        out = (
            "List of devices attached\n"
            "192.168.1.50:5555\tdevice\n"
            "R58M12345XY\tdevice\n"
        )
        with patch("apt_log.probe._adb", return_value=_adb_output(out)):
            assert usb_devices() == ["R58M12345XY"]

    def test_ignores_unauthorised_device(self):
        out = "List of devices attached\nR58M12345XY\tunauthorized\n"
        with patch("apt_log.probe._adb", return_value=_adb_output(out)):
            assert usb_devices() == []

    def test_empty_when_nothing_attached(self):
        with patch("apt_log.probe._adb", return_value=_adb_output("List of devices attached\n")):
            assert usb_devices() == []


class TestViability:
    """UNKNOWN is not a verdict — only a confirmed blocking failure stops the project."""

    def test_all_pass_is_viable(self):
        report = ProbeReport(package="com.example.app", results=[
            CheckResult(1, "launches", Status.PASS, "", blocking=True),
            CheckResult(2, "hierarchy", Status.PASS, "", blocking=True),
        ])
        assert report.viable
        assert report.blocking_failures == []

    def test_blocking_failure_is_not_viable(self):
        report = ProbeReport(package="com.example.app", results=[
            CheckResult(1, "launches", Status.PASS, "", blocking=True),
            CheckResult(4, "not blocked", Status.FAIL, "detected", blocking=True),
        ])
        assert not report.viable
        assert [r.number for r in report.blocking_failures] == [4]

    def test_unknown_blocking_check_does_not_condemn_the_project(self):
        report = ProbeReport(package="com.example.app", results=[
            CheckResult(4, "not blocked", Status.UNKNOWN, "ambiguous", blocking=True),
        ])
        assert report.viable

    def test_nonblocking_failure_does_not_condemn_the_project(self):
        # FLAG_SECURE breaks the screenshot panel; it does not make the project
        # impossible, so it must not read as a stop.
        report = ProbeReport(package="com.example.app", results=[
            CheckResult(3, "FLAG_SECURE", Status.FAIL, "set", blocking=False),
        ])
        assert report.viable


class TestRender:
    def test_stop_line_names_the_failing_checks(self):
        report = ProbeReport(package="com.example.app", results=[
            CheckResult(2, "hierarchy", Status.FAIL, "empty", blocking=True),
        ])
        out = report.render()
        assert "STOP" in out
        assert "#2" in out
        assert "com.example.app" in out

    def test_unknowns_are_surfaced_rather_than_read_as_a_go(self):
        report = ProbeReport(package="com.example.app", results=[
            CheckResult(1, "launches", Status.PASS, "", blocking=True),
            CheckResult(5, "integrity", Status.UNKNOWN, "ambiguous"),
        ])
        out = report.render()
        assert "UNKNOWN" in out
        assert "STOP" not in out
