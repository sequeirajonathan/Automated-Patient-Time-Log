"""The home menu.

Rows share resource-ids and are only distinguishable by label text, which is
language-dependent — so these tests run the same assertions in both languages.
Matching only Spanish would pass on the development handheld and fail anywhere
else, which is the split this project keeps having to avoid.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apt_log.screens.home import HomeError, HomeScreen, _xpath_literal

NAME = "com.hhaexchange.caregiver:id/lbl_name"
DESC = "com.hhaexchange.caregiver:id/lbl_description"
COUNT = "com.hhaexchange.caregiver:id/lbl_count"
SCROLL = "com.hhaexchange.caregiver:id/scroll_list_home"
HEADER = "com.hhaexchange.caregiver:id/lbl_header"

# As read off the device.
ES_ROWS = ["Horario para hoy", "Visita no programada", "Visitas", "Pacientes",
           "Mensajes", "Mi disponibilidad"]
EN_ROWS = ["Today's schedule", "Unscheduled visit", "Visits", "Patients",
           "Messages", "My availability"]


def _el(text=""):
    e = MagicMock()
    e.text = text
    return e


class FakeDriver:
    def __init__(self, labels, counts=(), activity=".HomeActivity", header=""):
        self.labels = [_el(x) for x in labels]
        self.counts = [_el(x) for x in counts]
        self.current_activity = activity
        self.header = header
        self.clicked_xpath = None
        self._ancestor = MagicMock()

    def find_elements(self, by, value):
        if by == "xpath":
            self.clicked_xpath = value
            return [self._ancestor]
        if value == NAME:
            return self.labels
        if value == COUNT:
            return self.counts
        if value == HEADER:
            return [_el(self.header)] if self.header else []
        if value == SCROLL:
            return [MagicMock()]
        return []


class TestDetection:
    def test_detects_by_activity(self):
        assert HomeScreen(FakeDriver([])).is_displayed() is True

    def test_detects_by_scroll_container(self):
        d = FakeDriver(ES_ROWS, activity=".Other")
        assert HomeScreen(d).is_displayed() is True


class TestBilingualRows:
    @pytest.mark.parametrize("labels", [ES_ROWS, EN_ROWS])
    def test_every_row_is_recognised_in_both_languages(self, labels):
        rows = HomeScreen(FakeDriver(labels)).rows()
        assert [r.key for r in rows] == [
            "today_schedule", "unscheduled_visit", "visits",
            "patients", "messages", "availability",
        ]

    @pytest.mark.parametrize("labels", [ES_ROWS, EN_ROWS])
    def test_opens_the_right_row_in_both_languages(self, labels):
        d = FakeDriver(labels)
        returned = HomeScreen(d).open("today_schedule")
        assert returned == labels[0]
        d._ancestor.click.assert_called_once()
        # The label may appear escaped as concat(...) when it contains an
        # apostrophe, so assert on its parts rather than the raw string.
        for fragment in labels[0].replace("'", " ").split():
            assert fragment in d.clicked_xpath

    def test_visits_is_not_confused_with_unscheduled_visit(self):
        """"Visitas" and "Visita no programada" share a prefix."""
        d = FakeDriver(ES_ROWS)
        HomeScreen(d).open("visits")
        assert "'Visitas'" in d.clicked_xpath

    def test_an_unknown_label_has_no_key_rather_than_a_wrong_one(self):
        rows = HomeScreen(FakeDriver(["Algo nuevo"])).rows()
        assert rows[0].key is None


class TestOpenRefusesToGuess:
    def test_missing_row_raises_and_lists_what_is_present(self):
        d = FakeDriver(["Mensajes"])
        with pytest.raises(HomeError, match="no menu row matching"):
            HomeScreen(d).open("today_schedule")

    def test_missing_row_taps_nothing(self):
        d = FakeDriver(["Mensajes"])
        with pytest.raises(HomeError):
            HomeScreen(d).open("today_schedule")
        d._ancestor.click.assert_not_called()

    def test_duplicate_rows_raise(self):
        d = FakeDriver(["Visitas", "Visitas"])
        with pytest.raises(HomeError, match="refusing to guess"):
            HomeScreen(d).open("visits")

    def test_unknown_key_raises(self):
        with pytest.raises(HomeError, match="unknown menu key"):
            HomeScreen(FakeDriver(ES_ROWS)).open("nonsense")

    def test_no_clickable_ancestor_raises(self):
        d = FakeDriver(ES_ROWS)
        d.find_elements = lambda by, value: [] if by == "xpath" else (
            d.labels if value == NAME else []
        )
        with pytest.raises(HomeError, match="nothing clickable"):
            HomeScreen(d).open("today_schedule")


class TestTodayCount:
    def test_reads_the_badge(self):
        d = FakeDriver(ES_ROWS, counts=["2"])
        assert HomeScreen(d).today_visit_count() == 2

    def test_zero_is_returned_not_treated_as_missing(self):
        """Zero means nothing to do — worth knowing without opening the list."""
        d = FakeDriver(ES_ROWS, counts=["0"])
        assert HomeScreen(d).today_visit_count() == 0

    def test_absent_badge_is_none(self):
        assert HomeScreen(FakeDriver(ES_ROWS)).today_visit_count() is None

    def test_non_numeric_badge_is_ignored(self):
        d = FakeDriver(ES_ROWS, counts=["•", "3"])
        assert HomeScreen(d).today_visit_count() == 3


class TestAgencyEcho:
    def test_header_reports_the_selected_agency(self):
        """A cheap check that the right employer is in context."""
        d = FakeDriver(ES_ROWS, header="Example Home Care, Inc. (Example Home Care, Inc.)")
        assert "Example Home Care" in HomeScreen(d).agency_label()


class TestXpathLiteral:
    def test_plain_string(self):
        assert _xpath_literal("Visitas") == "'Visitas'"

    def test_apostrophe_is_escaped_not_broken(self):
        """"Today's schedule" would otherwise terminate the XPath string."""
        out = _xpath_literal("Today's schedule")
        assert out.startswith("concat(")
        assert "Today" in out and "s schedule" in out
