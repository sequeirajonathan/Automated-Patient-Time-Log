"""Visit detail and the EVV verification chooser.

Nothing here completes a clock-in. The chooser is where a real record starts,
and selecting a method is a deployment decision rather than a default.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apt_log.screens.visit import VisitDetailScreen, VisitError

P = "com.hhaexchange.caregiver:id/"
CLOCK_IN, CLOCK_OUT = P + "btn_clock_in", P + "btn_clock_out"
START, END = P + "lbl_start_time", P + "lbl_end_time"
CSTART, CEND = P + "lbl_confirmed_start_time", P + "lbl_confirmed_end_time"
TASKS, SIG = P + "lbl_task_id", P + "layout_tab_content_signature"
METHOD, CANCEL, MLIST = P + "label_title", P + "button_cancel", P + "list_verification_method"


def _el(text=""):
    e = MagicMock()
    e.text = text
    return e


class FakeDriver:
    def __init__(self, data, activity=".VisitDetailActivity"):
        self.data = data
        self.current_activity = activity
        self.clicked_xpath = None
        self._ancestor = MagicMock()

    def find_elements(self, by, value):
        if by == "xpath":
            self.clicked_xpath = value
            return [self._ancestor]
        return [_el(x) for x in self.data.get(value, [])]


def _visit(**over):
    data = {
        CLOCK_IN: ["Registrar Entrada"], CLOCK_OUT: ["Registrar Salida"],
        START: ["08/14 at 08:00PM"], END: ["08/14 at 10:00PM"],
        CSTART: [""], CEND: [""],
        TASKS: ["106 - Hair Care-Shampoo", "127 - Toilet Use", "134 - Bathing"],
        SIG: ["x"],
    }
    data.update(over)
    return FakeDriver(data)


class TestReading:
    def test_scheduled_and_confirmed_are_separate(self):
        s = VisitDetailScreen(_visit())
        assert s.scheduled_window() == ("08/14 at 08:00PM", "08/14 at 10:00PM")
        assert s.confirmed_window() == ("", "")

    def test_not_clocked_in_when_confirmed_is_empty(self):
        assert VisitDetailScreen(_visit()).clocked_in() is False

    def test_clocked_in_once_confirmed_is_populated(self):
        assert VisitDetailScreen(_visit(**{CSTART: ["08:04PM"]})).clocked_in() is True

    def test_task_codes_only_no_descriptions(self):
        """Duty descriptions are care information about a specific patient."""
        codes = VisitDetailScreen(_visit()).task_ids()
        assert codes == ["106", "127", "134"]
        assert not any("Bathing" in c for c in codes)

    def test_signature_tab_detected(self):
        assert VisitDetailScreen(_visit()).has_signature_tab() is True


class TestVerificationChooser:
    def test_clock_in_opens_the_chooser_and_lists_methods(self):
        d = _visit(**{METHOD: ["GPS", "token de seguridad"]})
        assert VisitDetailScreen(d).open_verification_chooser() == [
            "GPS", "token de seguridad"]

    def test_no_clock_in_control_raises(self):
        d = _visit(**{CLOCK_IN: []})
        with pytest.raises(VisitError, match="no clock-in control"):
            VisitDetailScreen(d).open_verification_chooser()

    def test_cancel_backs_out(self):
        d = _visit(**{CANCEL: ["Anular"]})
        VisitDetailScreen(d).cancel_verification()

    def test_cancel_without_the_control_raises(self):
        with pytest.raises(VisitError, match="no cancel control"):
            VisitDetailScreen(_visit()).cancel_verification()

    def test_chooser_open_detection(self):
        assert VisitDetailScreen(_visit(**{MLIST: ["x"]})).chooser_is_open() is True
        assert VisitDetailScreen(_visit()).chooser_is_open() is False


class TestMethodSelectionIsExplicit:
    def test_selects_gps_when_asked(self):
        d = _visit(**{METHOD: ["GPS", "token de seguridad"]})
        assert VisitDetailScreen(d).choose_verification("gps") == "GPS"
        assert "GPS" in d.clicked_xpath

    def test_selects_token_when_asked(self):
        d = _visit(**{METHOD: ["GPS", "token de seguridad"]})
        assert VisitDetailScreen(d).choose_verification("token") == "token de seguridad"

    def test_there_is_no_default_method(self):
        """GPS and token evidence different things; neither is assumed."""
        with pytest.raises(TypeError):
            VisitDetailScreen(_visit()).choose_verification()

    def test_unknown_method_raises(self):
        with pytest.raises(VisitError, match="unknown verification method"):
            VisitDetailScreen(_visit()).choose_verification("carrier pigeon")

    def test_absent_method_lists_what_was_offered(self):
        d = _visit(**{METHOD: ["GPS"]})
        with pytest.raises(VisitError, match="offered"):
            VisitDetailScreen(d).choose_verification("token")

    def test_ambiguous_match_refuses(self):
        d = _visit(**{METHOD: ["token de seguridad", "security token"]})
        with pytest.raises(VisitError, match="refusing to guess"):
            VisitDetailScreen(d).choose_verification("token")
