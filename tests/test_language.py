"""The pre-login language gate.

Found by inspecting the real app: a cold start lands on
`.LanguageSelectionActivity`, not on a login form. Without clearing it,
authenticate_if_needed finds no password field and wrongly concludes the session
is still valid.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apt_log.screens.language import (
    LanguageMismatch,
    LanguageScreen,
    advance_past_startup_gates,
)

PICKER = "com.hhaexchange.caregiver:id/pickerView"
APPLY = "com.hhaexchange.caregiver:id/txtApply"


class FakeDriver:
    def __init__(self, ids: dict[str, list], activity: str = ".MainActivity"):
        self.ids = ids
        self.current_activity = activity

    def find_elements(self, _by, value):
        return self.ids.get(value, [])


class TestDetection:
    def test_detects_by_activity_name(self):
        d = FakeDriver({}, activity=".LanguageSelectionActivity")
        assert LanguageScreen(d).is_displayed() is True

    def test_falls_back_to_the_picker_if_the_activity_was_renamed(self):
        d = FakeDriver({PICKER: [MagicMock()]}, activity=".SomethingElse")
        assert LanguageScreen(d).is_displayed() is True

    def test_absent_when_neither_signal_is_there(self):
        assert LanguageScreen(FakeDriver({})).is_displayed() is False

    def test_survives_a_driver_without_current_activity(self):
        d = FakeDriver({PICKER: [MagicMock()]})
        del d.current_activity
        # Attribute access raises; detection must fall through to the picker.
        d.__class__ = type("NoActivity", (FakeDriver,), {
            "current_activity": property(
                lambda _s: (_ for _ in ()).throw(RuntimeError("unsupported"))
            )
        })
        assert LanguageScreen(d).is_displayed() is True


HEADER = "com.hhaexchange.caregiver:id/lbl_header"


def _text_el(text):
    e = MagicMock()
    e.text = text
    return e


class TestHeaderLanguage:
    """The only readable language signal — the picker itself is opaque."""

    def test_recognises_english(self):
        d = FakeDriver({HEADER: [_text_el("Select Language")]})
        assert LanguageScreen(d).header_language() == "en"

    def test_recognises_spanish(self):
        d = FakeDriver({HEADER: [_text_el("Seleccionar idioma")]})
        assert LanguageScreen(d).header_language() == "es"

    def test_unrecognised_language_is_none_not_a_guess(self):
        d = FakeDriver({HEADER: [_text_el("Sprache auswählen")]})
        assert LanguageScreen(d).header_language() is None

    def test_missing_header_is_none(self):
        assert LanguageScreen(FakeDriver({})).header_language() is None


class TestLanguageExpectation:
    def test_accepts_when_the_app_matches_the_expected_language(self):
        apply_el = MagicMock()
        d = FakeDriver({HEADER: [_text_el("Seleccionar idioma")], APPLY: [apply_el]},
                       activity=".LanguageSelectionActivity")
        LanguageScreen(d).apply(expect_language="es")
        apply_el.click.assert_called_once()

    def test_refuses_and_does_not_tap_on_a_mismatch(self):
        """A wrong-language run must fail at provisioning, not surprise Florida."""
        apply_el = MagicMock()
        d = FakeDriver({HEADER: [_text_el("Select Language")], APPLY: [apply_el]},
                       activity=".LanguageSelectionActivity")
        with pytest.raises(LanguageMismatch, match="expected the app in 'es'"):
            LanguageScreen(d).apply(expect_language="es")
        apply_el.click.assert_not_called()

    def test_mismatch_message_points_at_the_manual_fix(self):
        d = FakeDriver({HEADER: [_text_el("Select Language")], APPLY: [MagicMock()]},
                       activity=".LanguageSelectionActivity")
        with pytest.raises(LanguageMismatch, match="Android Settings"):
            LanguageScreen(d).apply(expect_language="es")

    def test_no_expectation_means_no_check(self):
        apply_el = MagicMock()
        d = FakeDriver({APPLY: [apply_el]}, activity=".LanguageSelectionActivity")
        LanguageScreen(d).apply()
        apply_el.click.assert_called_once()


class TestApply:
    def test_taps_the_apply_control(self):
        apply_el = MagicMock()
        d = FakeDriver({APPLY: [apply_el]}, activity=".LanguageSelectionActivity")
        LanguageScreen(d).apply()
        apply_el.click.assert_called_once()

    def test_raises_when_the_layout_changed(self):
        d = FakeDriver({}, activity=".LanguageSelectionActivity")
        with pytest.raises(RuntimeError, match="layout has changed"):
            LanguageScreen(d).apply()

    def test_does_not_choose_a_language(self):
        """The app's language is the caregiver's screen, not ours to set."""
        apply_el = MagicMock()
        picker = MagicMock()
        d = FakeDriver({APPLY: [apply_el], PICKER: [picker]},
                       activity=".LanguageSelectionActivity")
        LanguageScreen(d).apply()
        picker.click.assert_not_called()
        picker.send_keys.assert_not_called()


PW = "//*[@password='true']"


class ScriptedDriver:
    """Splash for two polls, then the gate, then login once Apply is tapped.

    Mirrors what the real app does: .AppLaunchActivity first, the language gate
    a few seconds later.
    """

    ACTIVITIES = {
        "splash": ".AppLaunchActivity",
        "gate": ".LanguageSelectionActivity",
        "login": ".LoginActivity",
    }

    def __init__(self, splash_polls: int = 2):
        self.phase = "splash"
        self.splash_polls = splash_polls
        self.polls = 0
        self.clicks = 0

    @property
    def current_activity(self):
        return self.ACTIVITIES[self.phase]

    def find_elements(self, _by, value):
        if value == PW:
            # One PW lookup per loop iteration; use it as the poll marker.
            self.polls += 1
            if self.phase == "splash" and self.polls >= self.splash_polls:
                self.phase = "gate"
            return [MagicMock()] if self.phase == "login" else []
        if self.phase == "gate" and value == APPLY:
            el = MagicMock()
            el.click.side_effect = self._click
            return [el]
        if self.phase == "gate" and value == PICKER:
            return [MagicMock()]
        return []

    def _click(self, *_a, **_k):
        self.clicks += 1
        self.phase = "login"


class TestAdvancePastGates:
    def test_already_signed_in_is_a_no_op(self):
        d = FakeDriver({}, activity=".DashboardActivity")
        assert advance_past_startup_gates(d, timeout=2, poll=0) == []

    def test_waits_out_a_splash_before_the_gate_appears(self):
        """Observed on hardware: .AppLaunchActivity first, gate ~3s later.

        A single immediate check sees no gate and returns while the app is still
        on its way to the screen it was meant to clear.
        """
        d = ScriptedDriver(splash_polls=2)
        assert advance_past_startup_gates(d, timeout=5, poll=0) == ["language"]
        assert d.clicks == 1
        assert d.phase == "login"

    def test_stops_as_soon_as_login_is_reachable(self):
        d = FakeDriver({PW: [MagicMock()]}, activity=".LoginActivity")
        assert advance_past_startup_gates(d, timeout=2, poll=0) == []

    def test_a_stuck_gate_raises_rather_than_looping(self):
        d = FakeDriver({APPLY: [MagicMock()]}, activity=".LanguageSelectionActivity")
        with pytest.raises(RuntimeError, match="not advancing blind"):
            advance_past_startup_gates(d, timeout=5, poll=0, max_clears=3)

    def test_a_permanent_splash_times_out_with_the_activity_named(self):
        d = FakeDriver({}, activity=".AppLaunchActivity")
        with pytest.raises(TimeoutError, match="AppLaunchActivity"):
            advance_past_startup_gates(d, timeout=0.05, poll=0)

    def test_a_transition_between_checks_is_not_mistaken_for_settled(self):
        """The bug this replaced: each check is a separate round trip.

        Observed on hardware — the gate appeared after the gate check read splash
        and before the transient check read the activity. The mismatch looked like
        "settled on something else" and returned, leaving the driver on the gate.
        """

        class RacingDriver:
            """Reports splash to the gate check, then the gate to the next read."""

            def __init__(self):
                self.reads = 0
                self.clicks = 0
                self.phase = "racing"

            @property
            def current_activity(self):
                self.reads += 1
                if self.phase == "cleared":
                    return ".LoginActivity"
                # 1st read (inside the gate check) sees splash; the 2nd, in the
                # transient check, sees the gate — the exact interleaving observed.
                return ".AppLaunchActivity" if self.reads == 1 else \
                       ".LanguageSelectionActivity"

            def find_elements(self, _by, value):
                if value == PW:
                    return [MagicMock()] if self.phase == "cleared" else []
                if value == APPLY and self.phase != "cleared":
                    el = MagicMock()
                    el.click.side_effect = self._click
                    return [el]
                return []

            def _click(self, *_a, **_k):
                self.clicks += 1
                self.phase = "cleared"

        d = RacingDriver()
        assert advance_past_startup_gates(d, timeout=5, poll=0) == ["language"]
        assert d.clicks == 1
