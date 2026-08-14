"""REQ-3 — login detection, credential handling, and refusal to guess."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apt_log import capture
from apt_log.screens.login import (
    LoginError,
    LoginScreen,
    _redact,
    authenticate_if_needed,
)
from apt_log.secrets import MemorySecretProvider


def _el(eid: str, text: str = ""):
    e = MagicMock()
    e.id = eid
    e.text = text
    return e


class FakeDriver:
    """Returns elements per XPath. Nothing here talks to a device."""

    def __init__(self, mapping: dict[str, list]):
        self.mapping = mapping
        self.screenshots = 0

    def find_elements(self, _by, xpath):
        return self.mapping.get(xpath, [])

    def get_screenshot_as_base64(self):
        self.screenshots += 1
        return "PNGDATA"


PW = "//*[@password='true']"
ED = "//android.widget.EditText"
BT = "//android.widget.Button"
FOCUSED_PW = "//*[@password='true' and @focused='true']"


def _login_driver():
    pw = _el("pw")
    user = _el("user")
    return FakeDriver({PW: [pw], ED: [user, pw], BT: [_el("b", "Sign In")]})


class TestDetection:
    def test_password_field_means_login_screen(self):
        assert LoginScreen(_login_driver()).is_displayed() is True

    def test_no_password_field_means_not_login(self):
        assert LoginScreen(FakeDriver({})).is_displayed() is False

    def test_detection_does_not_depend_on_language(self):
        """The operator is bilingual; matching "Sign In" would fail in Spanish."""
        d = FakeDriver({PW: [_el("pw")], ED: [_el("u"), _el("pw")],
                        BT: [_el("b", "Iniciar sesión")]})
        assert LoginScreen(d).is_displayed() is True


class TestRefusesToGuess:
    def test_two_password_fields_is_an_error(self):
        # A change-password form, not a login form. Filling it blind could
        # change the account's password.
        d = FakeDriver({PW: [_el("p1"), _el("p2")]})
        with pytest.raises(LoginError, match="2 password fields"):
            LoginScreen(d).login(MemorySecretProvider(
                APP_USERNAME="u", APP_PASSWORD="p"))

    def test_two_candidate_username_fields_is_an_error(self):
        pw = _el("pw")
        d = FakeDriver({PW: [pw], ED: [_el("a"), _el("b"), pw]})
        with pytest.raises(LoginError, match="candidate username"):
            LoginScreen(d)._username_field()

    def test_ambiguous_submit_control_is_an_error(self):
        pw = _el("pw")
        d = FakeDriver({
            PW: [pw], ED: [_el("u"), pw],
            BT: [_el("b1", "Help"), _el("b2", "About")],
        })
        with pytest.raises(LoginError, match="submit control"):
            LoginScreen(d)._submit_control()

    def test_single_unnamed_button_is_accepted(self):
        pw = _el("pw")
        d = FakeDriver({PW: [pw], ED: [_el("u"), pw], BT: [_el("only", "")]})
        assert LoginScreen(d)._submit_control().id == "only"


class TestLoginFlow:
    def test_fills_both_fields_and_submits(self):
        d = _login_driver()
        secrets = MemorySecretProvider(APP_USERNAME="nurse1", APP_PASSWORD="hunter2")
        LoginScreen(d).login(secrets)

        user = d.mapping[ED][0]
        pw = d.mapping[PW][0]
        user.send_keys.assert_called_once_with("nurse1")
        pw.send_keys.assert_called_once_with("hunter2")
        d.mapping[BT][0].click.assert_called_once()

    def test_capture_is_suppressed_for_the_whole_flow(self):
        """Not just while focus is in the field — the tap before it counts too."""
        seen = []
        d = _login_driver()
        pw_field = d.mapping[PW][0]
        pw_field.send_keys.side_effect = lambda _v: seen.append(capture._suppressed())

        LoginScreen(d).login(MemorySecretProvider(APP_USERNAME="u", APP_PASSWORD="p"))
        assert seen == [True]
        assert capture._suppressed() is False  # restored afterwards


class TestAuthenticateIfNeeded:
    def test_returns_false_and_does_nothing_when_already_signed_in(self):
        assert authenticate_if_needed(FakeDriver({}), MemorySecretProvider()) is False

    def test_logs_in_when_the_screen_is_present(self):
        d = _login_driver()
        assert authenticate_if_needed(
            d, MemorySecretProvider(APP_USERNAME="u", APP_PASSWORD="p")
        ) is True
        d.mapping[BT][0].click.assert_called_once()


class TestRedaction:
    @pytest.mark.parametrize("value,expected", [
        ("nurse1", "n****1"), ("ab", "**"), ("a", "*"),
    ])
    def test_redacts_all_but_the_ends(self, value, expected):
        assert _redact(value) == expected


class TestScreenshotInterlock:
    def test_refuses_while_a_password_field_has_focus(self):
        d = FakeDriver({FOCUSED_PW: [_el("pw")]})
        assert capture.safe_screenshot(d) is None
        assert d.screenshots == 0

    def test_allows_when_no_password_field_has_focus(self):
        d = FakeDriver({})
        assert capture.safe_screenshot(d) == "PNGDATA"

    def test_strict_refuses_a_password_field_merely_present(self):
        d = FakeDriver({PW: [_el("pw")]})
        assert capture.safe_screenshot(d, strict=True) is None
        assert capture.safe_screenshot(d) == "PNGDATA"

    def test_refuses_when_focus_cannot_be_determined(self):
        """Unsafe case assumed: a missed diagnostic beats a captured credential."""
        d = FakeDriver({})
        d.find_elements = MagicMock(side_effect=RuntimeError("hierarchy gone"))
        assert capture.safe_screenshot(d) is None

    def test_explicit_guard_blocks_capture(self):
        d = FakeDriver({})
        with capture.suppressed():
            assert capture.safe_screenshot(d) is None
        assert capture.safe_screenshot(d) == "PNGDATA"
