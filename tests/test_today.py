"""Today's visit list.

Names below are invented. The real ones are PHI and never reach this repository,
a log line, or the audit trail — REQUIREMENTS §5.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apt_log.screens.today import (
    TodayScheduleError,
    TodayScheduleScreen,
    VisitRow,
    redact,
)

P = "com.hhaexchange.caregiver:id/"
NAME, SSTART, SEND = P + "lbl_patient_name", P + "lbl_schedule_start_time", P + "lbl_schedule_end_time"
VSTART, VEND = P + "lbl_visit_start_time", P + "lbl_visit_end_time"
LIST = P + "list_today_schedule"

ALICE = "Alice Example"
BOB = "Bob Sample"


def _el(text=""):
    e = MagicMock()
    e.text = text
    return e


class FakeDriver:
    def __init__(self, names, sstart, send, vstart, vend,
                 activity=".TodayScheduleActivity"):
        self.data = {
            NAME: names, SSTART: sstart, SEND: send, VSTART: vstart, VEND: vend,
        }
        self.current_activity = activity
        self.clicked_xpath = None
        self._ancestor = MagicMock()

    def find_elements(self, by, value):
        if by == "xpath":
            self.clicked_xpath = value
            return [self._ancestor]
        if value == LIST:
            return [MagicMock()]
        return [_el(x) for x in self.data.get(value, [])]


def _two_visits(vstart=("", ""), vend=("", "")):
    return FakeDriver([ALICE, BOB], ["09:00", "14:00"], ["10:00", "15:00"],
                      list(vstart), list(vend))


class TestReadingRows:
    def test_reads_both_visits(self):
        rows = TodayScheduleScreen(_two_visits()).visits()
        assert len(rows) == 2
        assert rows[0].patient_name == ALICE
        assert rows[0].scheduled_start == "09:00"

    def test_empty_visit_time_means_not_clocked_in(self):
        """The agency's own idempotency signal, better than the local database."""
        rows = TodayScheduleScreen(_two_visits()).visits()
        assert rows[0].clocked_in is False
        assert rows[0].clocked_out is False

    def test_populated_visit_time_means_already_clocked_in(self):
        rows = TodayScheduleScreen(_two_visits(vstart=("09:04", ""))).visits()
        assert rows[0].clocked_in is True
        assert rows[1].clocked_in is False

    def test_scheduled_and_actual_are_never_the_same_field(self):
        rows = TodayScheduleScreen(_two_visits(vstart=("09:07", ""))).visits()
        assert rows[0].scheduled_start == "09:00"
        assert rows[0].visit_start == "09:07"

    def test_mismatched_field_counts_raise_rather_than_zipping(self):
        """Pairing to the shortest list would attribute one patient's time to another."""
        d = FakeDriver([ALICE, BOB], ["09:00"], ["10:00", "15:00"], ["", ""], ["", ""])
        with pytest.raises(TodayScheduleError, match="disagree in count"):
            TodayScheduleScreen(d).visits()


class TestFindByName:
    def test_finds_the_right_patient(self):
        row = TodayScheduleScreen(_two_visits()).find(BOB)
        assert row.patient_name == BOB
        assert row.index == 1

    def test_is_not_positional(self):
        """Schedule order changes between runs; an index points at someone else."""
        d = FakeDriver([BOB, ALICE], ["14:00", "09:00"], ["15:00", "10:00"],
                       ["", ""], ["", ""])
        assert TodayScheduleScreen(d).find(ALICE).index == 1

    def test_case_and_whitespace_insensitive(self):
        d = FakeDriver(["  ALICE   EXAMPLE "], ["09:00"], ["10:00"], [""], [""])
        assert TodayScheduleScreen(d).find(ALICE) is not None

    def test_unique_partial_match_is_accepted(self):
        d = FakeDriver(["Alice Example Jr"], ["09:00"], ["10:00"], [""], [""])
        assert TodayScheduleScreen(d).find(ALICE).index == 0

    def test_missing_patient_raises_without_naming_them(self):
        with pytest.raises(TodayScheduleError) as exc:
            TodayScheduleScreen(_two_visits()).find("Carol Nobody")
        assert "Carol" not in str(exc.value)
        assert "CN" in str(exc.value)

    def test_duplicate_names_refuse_rather_than_guess(self):
        d = FakeDriver([ALICE, ALICE], ["09:00", "14:00"], ["10:00", "15:00"],
                       ["", ""], ["", ""])
        with pytest.raises(TodayScheduleError, match="refusing to"):
            TodayScheduleScreen(d).find(ALICE)


class TestOpen:
    def test_taps_the_clickable_ancestor(self):
        d = _two_visits()
        row = TodayScheduleScreen(d).open(ALICE)
        assert row.patient_name == ALICE
        d._ancestor.click.assert_called_once()
        assert "lbl_patient_name" in d.clicked_xpath

    def test_apostrophe_in_a_name_is_escaped(self):
        """O'Brien would otherwise terminate the XPath literal."""
        d = FakeDriver(["Mary O'Brien"], ["09:00"], ["10:00"], [""], [""])
        TodayScheduleScreen(d).open("Mary O'Brien")
        assert "concat(" in d.clicked_xpath

    def test_missing_patient_taps_nothing(self):
        d = _two_visits()
        with pytest.raises(TodayScheduleError):
            TodayScheduleScreen(d).open("Carol Nobody")
        d._ancestor.click.assert_not_called()


class TestRedaction:
    @pytest.mark.parametrize("name,expected", [
        ("Alice Example", "AE"), ("Mary O'Brien", "MO"),
        ("  bob   sample ", "BS"), ("", "?"),
    ])
    def test_initials_only(self, name, expected):
        assert redact(name) == expected

    def test_repr_does_not_leak_the_name(self):
        row = VisitRow(0, ALICE, "09:00", "10:00", "", "")
        assert ALICE not in repr(row)
        assert "AE" in repr(row)
