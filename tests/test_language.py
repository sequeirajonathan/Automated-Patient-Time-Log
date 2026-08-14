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


class TestAdvancePastGates:
    def test_no_gates_is_a_no_op(self):
        assert advance_past_startup_gates(FakeDriver({})) == []

    def test_clears_a_single_gate(self):
        apply_el = MagicMock()
        d = FakeDriver({APPLY: [apply_el]}, activity=".LanguageSelectionActivity")

        def clear(*_a, **_k):
            d.current_activity = ".LoginActivity"
            d.ids = {}

        apply_el.click.side_effect = clear
        assert advance_past_startup_gates(d) == ["language"]

    def test_a_stuck_gate_raises_rather_than_spinning(self):
        d = FakeDriver({APPLY: [MagicMock()]}, activity=".LanguageSelectionActivity")
        with pytest.raises(RuntimeError, match="not advancing blind"):
            advance_past_startup_gates(d, max_steps=3)
