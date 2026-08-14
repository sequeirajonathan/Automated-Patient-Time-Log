"""REQ-3 login check — the smoke test that opens the app and signs in.

The properties worth holding are about *honesty*, not mechanics: it must not
claim a sign-in it did not observe, it must not touch the phone when the
credentials are missing, it must stop before agency selection, and its output
must be safe to paste into a chat. Nothing here talks to a device.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apt_log.login_check import LoginCheckReport, Status, run_login_check
from apt_log.screens.agency import PACKAGE
from apt_log.secrets import MemorySecretProvider
from apt_log.transport import TransportMode

PW = "//*[@password='true']"
ED = "//android.widget.EditText"
BT = "//android.widget.Button"

AGENCY_LIST = f"{PACKAGE}:id/list_agencies"
HOME_SCROLL = f"{PACKAGE}:id/scroll_list_home"
MESSAGE = f"{PACKAGE}:id/lbl_message"
DISMISS = f"{PACKAGE}:id/btn_negative"

CREDS = MemorySecretProvider(APP_USERNAME="nurse1", APP_PASSWORD="hunter2")


def _el(eid: str, text: str = ""):
    e = MagicMock()
    e.id = eid
    e.text = text
    return e


class FakeDriver:
    """Returns elements per selector, and can change screen between reads.

    `screens` is a list of selector->elements maps. Each call to find_elements
    reads the current one; `advance()` moves to the next, which is how a login
    that succeeds is distinguished from one that leaves the form on screen.
    """

    def __init__(self, screens: list[dict], activity: str = ".LoginActivity"):
        self.screens = screens
        self.index = 0
        self.activity = activity

    @property
    def current_activity(self) -> str:
        return self.activity

    def advance(self) -> None:
        self.index = min(self.index + 1, len(self.screens) - 1)

    def find_elements(self, _by, selector):
        return self.screens[self.index].get(selector, [])


class FakeSession:
    """Stands in for DeviceSession. Records what the check asked it to do."""

    def __init__(self, driver, *, serial="R58M12345XY", mode=TransportMode.USB):
        self.package = "com.hhaexchange.caregiver"
        self.server_url = "http://127.0.0.1:4723"
        self.driver = driver
        self.serial = serial
        self.transport_mode = mode
        self.calls: list[str] = []

    def ensure_device(self) -> str:
        self.calls.append("ensure_device")
        return self.serial

    def wake_and_unlock(self) -> None:
        self.calls.append("wake_and_unlock")

    def cold_start(self) -> None:
        self.calls.append("cold_start")

    def attach(self):
        self.calls.append("attach")
        return self.driver

    def close(self) -> None:
        self.calls.append("close")


def _login_then(landing: dict) -> FakeDriver:
    """A login form that becomes `landing` once the submit control is tapped."""
    pw = _el("pw")
    user = _el("user")
    submit = _el("b", "Sign In")
    driver = FakeDriver([
        {PW: [pw], ED: [user, pw], BT: [submit]},
        landing,
    ])
    submit.click.side_effect = driver.advance
    return driver


def _run(driver, session=None, **kwargs) -> LoginCheckReport:
    """Run the check with the device layer stubbed out."""
    session = session or FakeSession(driver)
    with patch("apt_log.login_check._app_running", return_value=True), \
         patch("apt_log.login_check.time.sleep"):
        return run_login_check(
            "com.hhaexchange.caregiver", kwargs.pop("secrets", CREDS),
            session=session, gate_timeout=1, settle_timeout=1, poll=0, **kwargs,
        )


def _step(report: LoginCheckReport, number: int):
    return next(s for s in report.steps if s.number == number)


class TestHappyPath:
    def test_signs_in_and_reaches_agency_selection(self):
        driver = _login_then({AGENCY_LIST: [_el("list")]})
        report = _run(driver)

        assert report.signed_in is True
        assert _step(report, 7).status is Status.PASS
        assert _step(report, 8).status is Status.PASS
        assert "agency selection" in _step(report, 8).detail

    def test_home_menu_also_counts_as_signed_in(self):
        report = _run(_login_then({HOME_SCROLL: [_el("scroll")]}))
        assert report.signed_in is True

    def test_cold_starts_before_attaching(self):
        session = FakeSession(_login_then({HOME_SCROLL: [_el("s")]}))
        _run(session.driver, session=session)
        assert session.calls.index("cold_start") < session.calls.index("attach")

    def test_no_cold_start_leaves_the_app_alone(self):
        session = FakeSession(_login_then({HOME_SCROLL: [_el("s")]}))
        report = _run(session.driver, session=session, cold_start=False)
        assert "cold_start" not in session.calls
        assert _step(report, 3).status is Status.SKIP

    def test_the_session_is_always_closed(self):
        session = FakeSession(_login_then({AGENCY_LIST: [_el("l")]}))
        _run(session.driver, session=session)
        assert session.calls[-1] == "close"


class TestStopsShortOfActing:
    """Signing in is the whole job. Agency selection is somebody else's."""

    def test_does_not_tap_anything_on_the_agency_screen(self):
        row = _el("row", "Some Agency (Some Agency)")
        driver = _login_then({AGENCY_LIST: [_el("list")],
                              f"{PACKAGE}:id/label_agency_name": [row]})
        assert _run(driver).signed_in is True
        row.click.assert_not_called()


class TestRefusesToClaimSuccess:
    def test_still_on_the_login_form_is_a_failure(self):
        """A rejected password leaves the form up. That is not a pass."""
        pw = _el("pw")
        driver = FakeDriver([{PW: [pw], ED: [_el("u"), pw], BT: [_el("b", "Sign In")]}])
        report = _run(driver)

        assert report.signed_in is False
        assert _step(report, 8).status is Status.FAIL
        assert "rejected the credentials" in _step(report, 8).detail

    def test_an_unrecognised_screen_is_unknown_not_a_pass(self):
        report = _run(_login_then({}))
        assert _step(report, 8).status is Status.UNKNOWN
        assert report.signed_in is False

    def test_a_blocking_dialog_fails_even_over_the_home_menu(self):
        report = _run(_login_then({
            HOME_SCROLL: [_el("scroll")],
            MESSAGE: [_el("msg", "Debes de iniciar sesion.")],
            DISMISS: [_el("ok", "DE ACUERDO")],
        }))
        assert _step(report, 8).status is Status.FAIL
        assert report.signed_in is False

    def test_an_unknown_dialog_is_not_dismissed(self):
        ok = _el("ok", "DE ACUERDO")
        report = _run(_login_then({
            MESSAGE: [_el("msg", "Something nobody has read")], DISMISS: [ok],
        }))
        ok.click.assert_not_called()
        assert _step(report, 8).status is Status.FAIL

    def test_an_ambiguous_form_fails_without_typing_anything(self):
        """Two password fields is a change-password form (screens/login.py)."""
        driver = FakeDriver([{PW: [_el("p1"), _el("p2")]}])
        report = _run(driver)
        assert _step(report, 7).status is Status.FAIL
        assert "wrong password" in _step(report, 7).detail


class TestCredentialsFirst:
    def test_missing_credentials_stop_before_the_phone_is_touched(self):
        session = FakeSession(_login_then({HOME_SCROLL: [_el("s")]}))
        report = _run(session.driver, session=session,
                      secrets=MemorySecretProvider(APP_USERNAME="nurse1"))

        assert _step(report, 1).status is Status.FAIL
        assert session.calls == []          # nothing was cold-started or cleared
        assert len(report.steps) == 1

    def test_the_failure_names_the_key_but_never_a_value(self):
        report = _run(_login_then({}), secrets=MemorySecretProvider(
            APP_USERNAME="nurse1", APP_PASSWORD=""))
        assert "APP_PASSWORD" in _step(report, 1).detail
        assert "nurse1" not in report.render()


class TestAlreadySignedIn:
    def test_no_login_form_is_skipped_not_failed(self):
        driver = FakeDriver([{HOME_SCROLL: [_el("scroll")]}], activity=".HomeActivity")
        report = _run(driver)

        assert _step(report, 6).status is Status.SKIP
        assert _step(report, 7).status is Status.SKIP
        assert _step(report, 8).status is Status.PASS
        assert report.signed_in is True

    def test_the_skip_explains_how_to_force_a_login(self):
        driver = FakeDriver([{HOME_SCROLL: [_el("scroll")]}], activity=".HomeActivity")
        assert "--clear-data" in _step(_run(driver), 6).detail


class TestOutputIsSafeToPaste:
    def test_the_username_is_redacted_in_the_report(self):
        rendered = _run(_login_then({AGENCY_LIST: [_el("l")]})).render()
        assert "n****1" in rendered
        assert "nurse1" not in rendered
        assert "hunter2" not in rendered

    def test_dialog_text_is_never_printed(self):
        rendered = _run(_login_then({
            MESSAGE: [_el("msg", "CARIDAD ROJAS has an unsigned visit")],
            DISMISS: [_el("ok", "DE ACUERDO")],
        })).render()
        assert "CARIDAD" not in rendered
        assert "apt-log inspect" in rendered

    def test_dev_transport_is_declared(self):
        session = FakeSession(_login_then({AGENCY_LIST: [_el("l")]}),
                              mode=TransportMode.DEV)
        rendered = _run(session.driver, session=session).render()
        assert "REQ-5.4.1" in rendered


class TestClearData:
    def test_clear_data_wipes_before_launching(self):
        session = FakeSession(_login_then({AGENCY_LIST: [_el("l")]}))
        adb = MagicMock()
        adb.return_value.stdout = "Success\n"
        with patch("apt_log.login_check._adb", adb):
            report = _run(session.driver, session=session, clear_data=True)

        assert adb.call_args.args == ("shell", "pm", "clear", session.package)
        assert report.signed_in is True

    def test_clear_data_still_relaunches_under_no_cold_start(self):
        """pm clear kills the process; not relaunching would attach to nothing."""
        session = FakeSession(_login_then({AGENCY_LIST: [_el("l")]}))
        adb = MagicMock()
        adb.return_value.stdout = "Success\n"
        with patch("apt_log.login_check._adb", adb):
            report = _run(session.driver, session=session,
                          clear_data=True, cold_start=False)

        assert "cold_start" in session.calls
        assert report.signed_in is True

    def test_a_failed_clear_stops_the_run(self):
        session = FakeSession(_login_then({AGENCY_LIST: [_el("l")]}))
        adb = MagicMock()
        adb.return_value.stdout = "Failed"
        adb.return_value.stderr = ""
        with patch("apt_log.login_check._adb", adb):
            report = _run(session.driver, session=session, clear_data=True)

        assert _step(report, 3).status is Status.FAIL
        assert "attach" not in session.calls


class TestDeviceFailures:
    def test_an_unavailable_device_reports_the_rungs_and_stops(self):
        from apt_log.device import DeviceUnavailable

        session = FakeSession(_login_then({}))
        session.ensure_device = MagicMock(
            side_effect=DeviceUnavailable("gone; rungs tried: adb reconnect"))
        report = _run(session.driver, session=session)

        assert _step(report, 2).status is Status.FAIL
        assert "adb reconnect" in _step(report, 2).detail
        assert "attach" not in session.calls

    def test_a_failed_appium_session_points_at_the_probe(self):
        session = FakeSession(_login_then({}))
        session.attach = MagicMock(side_effect=RuntimeError("connection refused"))
        report = _run(session.driver, session=session)

        assert _step(report, 4).status is Status.FAIL
        assert "apt-log probe" in _step(report, 4).detail


class TestStartupGates:
    def test_a_gate_that_will_not_clear_fails_the_run(self):
        driver = _login_then({AGENCY_LIST: [_el("l")]})
        with patch("apt_log.screens.language.advance_past_startup_gates",
                   side_effect=TimeoutError("app did not reach a login screen")):
            report = _run(driver)

        assert _step(report, 5).status is Status.FAIL
        assert "TimeoutError" in _step(report, 5).detail

    def test_cleared_gates_are_named(self):
        driver = _login_then({AGENCY_LIST: [_el("l")]})
        with patch("apt_log.screens.language.advance_past_startup_gates",
                   return_value=["language", "permission[btn_allow]"]):
            report = _run(driver)

        assert _step(report, 5).detail == "language, permission[btn_allow]"


@pytest.mark.parametrize("statuses,signed_in", [
    ([Status.PASS, Status.SKIP], True),
    ([Status.PASS, Status.UNKNOWN], False),
    ([Status.PASS, Status.FAIL], False),
])
def test_unknown_is_not_success(statuses, signed_in):
    """The probe treats UNKNOWN as "no verdict"; here it means "did not confirm"."""
    report = LoginCheckReport(package="x")
    for i, status in enumerate(statuses):
        report.add(i, f"step{i}", status)
    assert report.signed_in is signed_in
