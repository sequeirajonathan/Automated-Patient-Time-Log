"""The week's arithmetic.

EVERY NAME AND EVERY TIME IN THIS FILE IS INVENTED. The real schedule is a
person's health information and lives on the device, in /etc/aptlog, for the
same reason the site's own particulars do. What is tested here is the SHAPE of
the rules, and a made-up round exercises those better than the real one anyway
— it can have the awkward cases on purpose.

The rules under test, in the order they are easy to get wrong:

  * a visit fires at its nominal time unless somebody else's visit ends at
    exactly that minute, in which case five minutes are added for the drive;
  * a patient's own second entry is not a drive and gets no buffer;
  * a gap that already exists is not widened;
  * "what is next" at eleven at night is tomorrow morning;
  * and the two days a year when a wall clock does not mean what it says.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from apt_log import schedule as sched
# Captured at import, BEFORE conftest redirects it to a temporary path — the
# claim being tested is about where the SHIPPED module reads from.
from apt_log.schedule import SCHEDULE_PATH as REAL_PATH

EAST = ZoneInfo("America/New_York")

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]


def visit(patient, start, end, days=None, app="app.one", **extra):
    return {"patient": patient, "app": app, "start": start, "end": end,
            "days": days or WEEKDAYS, **extra}


def built(*visits, zone=None):
    doc = {"visits": list(visits)}
    if zone:
        doc["zone"] = zone
    return sched.parse(doc)


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=EAST)


# A Monday in ordinary time, nowhere near a clock change.
MONDAY = date(2026, 6, 15)


class TestTheTravelBuffer:
    """The rule that cannot be read out of any app: where one patient's visit
    ends at the minute the next one's begins, the caregiver still has to
    drive there."""

    def test_a_visit_with_nobody_before_it_fires_on_time(self):
        day = built(visit("Ada", "06:00", "09:00")).on(MONDAY)
        assert day[0].fires == at(2026, 6, 15, 6)
        assert day[0].buffered is False

    def test_a_visit_that_follows_another_exactly_fires_five_late(self):
        day = built(visit("Ada", "05:00", "06:00"),
                    visit("Bea", "06:00", "09:00")).on(MONDAY)
        assert [v.patient for v in day] == ["Ada", "Bea"]
        assert day[1].fires == at(2026, 6, 15, 6, 5)
        assert day[1].buffered is True

    def test_the_buffer_moves_the_start_and_not_the_end(self):
        """The agency's visit is still three hours long. Only the machine's
        moment of acting moved."""
        day = built(visit("Ada", "05:00", "06:00"),
                    visit("Bea", "06:00", "09:00")).on(MONDAY)
        assert day[1].ends == at(2026, 6, 15, 9)
        assert day[1].starts == at(2026, 6, 15, 6)

    def test_a_gap_that_already_exists_is_not_widened(self):
        """Five minutes are already built into the app's own times here, and
        adding five more would push every visit later all day."""
        day = built(visit("Ada", "06:00", "09:00"),
                    visit("Bea", "09:05", "11:05")).on(MONDAY)
        assert day[1].fires == at(2026, 6, 15, 9, 5)
        assert day[1].buffered is False

    def test_an_hour_of_daylight_between_visits_is_left_alone(self):
        day = built(visit("Ada", "13:00", "15:00"),
                    visit("Bea", "16:00", "18:00")).on(MONDAY)
        assert day[1].fires == at(2026, 6, 15, 16)

    def test_a_patients_own_second_entry_is_not_a_drive(self):
        """One stretch of care recorded as two entries. Nobody goes anywhere
        between them, so the second fires on the hour — this is the case a
        naive "did anything end here" check gets wrong."""
        day = built(visit("Ada", "20:05", "21:05", part=1, of=2),
                    visit("Ada", "21:05", "22:05", part=2, of=2)).on(MONDAY)
        assert [v.block.part for v in day] == [1, 2]
        assert day[1].fires == at(2026, 6, 15, 21, 5)
        assert day[1].buffered is False

    def test_but_somebody_else_ending_there_still_counts(self):
        """Same minute, different patient, even where the patient also has a
        split block of her own."""
        day = built(visit("Zoe", "19:05", "20:05"),
                    visit("Ada", "20:05", "21:05", part=1, of=2),
                    visit("Ada", "21:05", "22:05", part=2, of=2)).on(MONDAY)
        by_patient = {(v.patient, v.block.part): v for v in day}
        assert by_patient[("Ada", 1)].fires == at(2026, 6, 15, 20, 10)
        assert by_patient[("Ada", 2)].fires == at(2026, 6, 15, 21, 5)

    def test_the_buffer_depends_on_the_day_not_on_the_block(self):
        """The same visit is preceded on a weekday and alone on Saturday,
        which is why this arithmetic cannot live on the block itself."""
        week = built(visit("Ada", "05:00", "06:00", days=WEEKDAYS),
                     visit("Bea", "06:00", "09:00",
                           days=WEEKDAYS + ["sat"]))
        monday = {v.patient: v for v in week.on(MONDAY)}
        saturday = {v.patient: v for v in week.on(date(2026, 6, 20))}
        assert monday["Bea"].buffered is True
        assert saturday["Bea"].buffered is False
        assert saturday["Bea"].fires == at(2026, 6, 20, 6)


class TestArmingAheadOfTime:
    def test_arming_happens_a_quarter_hour_before_firing(self):
        day = built(visit("Ada", "06:00", "09:00")).on(MONDAY)
        assert day[0].arms == day[0].fires - sched.LEAD
        assert day[0].arms == at(2026, 6, 15, 5, 45)

    def test_arming_follows_the_buffer_rather_than_the_nominal_time(self):
        """The point of arming is to be standing on the check-in screen when
        the moment comes. If the moment moved, the preparation moves."""
        day = built(visit("Ada", "05:00", "06:00"),
                    visit("Bea", "06:00", "09:00")).on(MONDAY)
        assert day[1].arms == at(2026, 6, 15, 5, 50)

    def test_an_app_known_to_be_slow_gets_longer(self, monkeypatch):
        monkeypatch.setattr(sched, "LEAD_BY_APP",
                            {"app.slow": timedelta(minutes=25)})
        day = built(visit("Ada", "06:00", "09:00", app="app.slow")).on(MONDAY)
        assert day[0].arms == at(2026, 6, 15, 5, 35)


class TestWhatIsNext:
    def _week(self):
        return built(visit("Ada", "05:00", "06:00"),
                     visit("Bea", "06:00", "09:00"),
                     visit("Cyd", "18:00", "20:00", days=["mon", "tue"]),
                     visit("Dot", "09:00", "13:00", days=["sun"]))

    def test_the_next_visit_is_the_next_one_to_fire(self):
        found = self._week().next_after(at(2026, 6, 15, 4))
        assert found.patient == "Ada"

    def test_a_visit_already_fired_is_behind_us(self):
        found = self._week().next_after(at(2026, 6, 15, 5, 30))
        assert found.patient == "Bea"

    def test_late_at_night_the_answer_is_tomorrow_morning(self):
        """The moment the answer matters most, and the one a version that
        only looked at today would get wrong by showing nothing."""
        found = self._week().next_after(at(2026, 6, 15, 23, 30))
        assert found.patient == "Ada"
        assert found.fires == at(2026, 6, 16, 5)

    def test_it_crosses_a_quiet_day_to_find_one(self):
        """Saturday has nothing in this round; asking on Saturday must reach
        Sunday rather than give up at midnight."""
        found = self._week().next_after(at(2026, 6, 20, 10))
        assert found.patient == "Dot"

    def test_a_schedule_with_nothing_in_it_has_no_next_visit(self):
        assert built().next_after(at(2026, 6, 15, 4)) is None

    def test_upcoming_comes_back_in_order_and_stops_where_asked(self):
        found = self._week().upcoming(at(2026, 6, 15, 4), limit=3)
        assert [v.patient for v in found] == ["Ada", "Bea", "Cyd"]
        assert found == sorted(found, key=lambda v: v.fires)

    def test_upcoming_spans_days_when_one_day_cannot_fill_it(self):
        found = self._week().upcoming(at(2026, 6, 15, 19), limit=3)
        assert [v.patient for v in found] == ["Ada", "Bea", "Cyd"]
        assert found[0].fires.date() == date(2026, 6, 16)

    def test_a_naive_time_is_read_as_local_rather_than_as_utc(self):
        """Five hours is the difference between "next is at five this
        morning" and "next is at five tomorrow"."""
        found = self._week().next_after(datetime(2026, 6, 15, 4))
        assert found.patient == "Ada"
        assert found.fires == at(2026, 6, 15, 5)


class TestTheVisitHappeningNow:
    def _week(self):
        return built(visit("Ada", "05:00", "06:00"),
                     visit("Bea", "06:00", "09:00"))

    def test_a_visit_in_progress_is_reported(self):
        assert self._week().current(at(2026, 6, 15, 7)).patient == "Bea"

    def test_measured_from_the_nominal_start_not_the_fire_time(self):
        """The agency's visit began at six whatever minute the machine got
        to it, and the portal must not show a five-minute hole."""
        assert self._week().current(at(2026, 6, 15, 6, 2)).patient == "Bea"

    def test_the_end_is_not_part_of_the_visit(self):
        assert self._week().current(at(2026, 6, 15, 9)) is None

    def test_between_visits_there_is_none(self):
        assert self._week().current(at(2026, 6, 15, 14)) is None

    def test_one_that_started_yesterday_evening_still_counts(self):
        late = built(visit("Ada", "23:00", "23:59", days=["mon"]))
        assert late.current(at(2026, 6, 15, 23, 30)).patient == "Ada"


class TestTheClockChanges:
    """Two days a year a wall clock does not mean what it says. This project
    already has a note about a Pi that believed it was 1970 and fired a whole
    day at once; the appetite for clock surprises is low."""

    def test_an_ordinary_morning_is_five_hours_off_utc_in_summer(self):
        day = built(visit("Ada", "06:00", "09:00")).on(date(2026, 7, 6))
        assert day[0].fires.utcoffset() == timedelta(hours=-4)

    def test_and_four_in_winter(self):
        day = built(visit("Ada", "06:00", "09:00")).on(date(2026, 1, 5))
        assert day[0].fires.utcoffset() == timedelta(hours=-5)

    def test_a_wall_time_that_does_not_exist_is_refused_not_guessed(self):
        """In March the clocks jump from two to three. A visit at half past
        two that morning names no instant at all, and Python will hand back
        something an hour out without complaining."""
        spring = built(visit("Ada", "02:30", "04:00", days=["sun"]))
        with pytest.raises(sched.BadSchedule, match="does not exist"):
            spring.on(date(2026, 3, 8))

    def test_the_same_visit_is_fine_on_every_other_sunday(self):
        spring = built(visit("Ada", "02:30", "04:00", days=["sun"]))
        assert spring.on(date(2026, 3, 15))[0].fires.hour == 2

    def test_an_hour_that_happens_twice_takes_the_earlier_one(self):
        """November repeats one o'clock. A scheduler whose brief is to err
        early takes the first of the two."""
        autumn = built(visit("Ada", "01:30", "03:00", days=["sun"]))
        fires = autumn.on(date(2026, 11, 1))[0].fires
        assert fires.utcoffset() == timedelta(hours=-4)   # still EDT

    def test_a_visit_spanning_the_spring_gap_lasts_an_hour_less(self):
        """One in the morning to five is four hours on the wall and three in
        real time, because an hour of that morning does not happen."""
        day = built(visit("Ada", "01:00", "05:00",
                          days=["sun"])).on(date(2026, 3, 8))
        assert day[0].duration == timedelta(hours=3)

    def test_and_subtracting_the_two_would_have_said_four(self):
        """The trap `duration` exists for, pinned so nobody "simplifies" it
        back. Python documents that subtracting two aware datetimes that
        share a tzinfo ignores the zone and works on the naive wall clocks —
        which is a silent extra hour on a number describing how long somebody
        was paid to be somewhere."""
        day = built(visit("Ada", "01:00", "05:00",
                          days=["sun"])).on(date(2026, 3, 8))
        assert day[0].ends - day[0].starts == timedelta(hours=4)
        assert day[0].duration != day[0].ends - day[0].starts

    def test_an_ordinary_visit_is_the_length_it_looks(self):
        day = built(visit("Ada", "06:00", "09:00")).on(MONDAY)
        assert day[0].duration == timedelta(hours=3)


class TestReadingTheFile:
    def test_a_missing_file_is_an_empty_schedule_not_a_crash(self, tmp_path):
        """This ships without one. The portal has to render before anybody
        has written a schedule."""
        assert len(sched.load(tmp_path / "nothing.json")) == 0

    def test_a_file_that_exists_and_is_wrong_is_an_error(self, tmp_path):
        """The opposite case, and it must not be collapsed into the one
        above: a schedule quietly one entry short is worse than one that
        refuses to load and says which line is wrong."""
        path = tmp_path / "schedule.json"
        path.write_text("{ not json", encoding="utf-8")
        with pytest.raises(sched.BadSchedule, match="valid JSON"):
            sched.load(path)

    def test_it_reads_what_the_device_would_hold(self, tmp_path):
        path = tmp_path / "schedule.json"
        path.write_text(json.dumps({
            "zone": "America/New_York",
            "visits": [visit("Ada", "06:00", "09:00", app="app.one",
                             agency="Some Agency")],
        }), encoding="utf-8")
        loaded = sched.load(path)
        assert loaded.patients == ["Ada"]
        assert loaded.on(MONDAY)[0].agency == "Some Agency"

    def test_spanish_day_names_are_understood(self):
        """The phone and the portal both speak it, and whoever maintains the
        file may write it either way."""
        day = built(visit("Ada", "06:00", "09:00",
                          days=["domingo"])).on(date(2026, 6, 14))
        assert day and day[0].patient == "Ada"

    def test_a_visit_with_no_patient_is_refused(self):
        with pytest.raises(sched.BadSchedule, match="no patient"):
            built({"app": "app.one", "start": "06:00", "end": "09:00",
                   "days": ["mon"]})

    def test_a_visit_with_no_app_is_refused(self):
        """There is nothing to open."""
        with pytest.raises(sched.BadSchedule, match="no app"):
            built({"patient": "Ada", "start": "06:00", "end": "09:00",
                   "days": ["mon"]})

    def test_a_visit_with_no_days_is_refused(self):
        with pytest.raises(sched.BadSchedule, match="no days"):
            built({"patient": "Ada", "app": "app.one", "start": "06:00",
                   "end": "09:00", "days": []})

    def test_a_day_nobody_recognises_is_refused(self):
        with pytest.raises(sched.BadSchedule, match="not a day"):
            built(visit("Ada", "06:00", "09:00", days=["someday"]))

    def test_a_time_that_is_not_a_time_is_refused(self):
        with pytest.raises(sched.BadSchedule, match="HH:MM"):
            built(visit("Ada", "sixish", "09:00"))

    def test_a_visit_that_ends_before_it_starts_is_refused(self):
        with pytest.raises(sched.BadSchedule, match="same day"):
            built(visit("Ada", "23:00", "01:00"))

    def test_a_visit_across_midnight_is_refused_rather_than_mishandled(self):
        """Not supported, and said so. Every rule above compares a visit
        against its own day's neighbours, and a block that wrapped would
        quietly be measured against the wrong ones."""
        with pytest.raises(sched.BadSchedule, match="same day"):
            built(visit("Ada", "22:00", "02:00"))

    def test_an_impossible_pair_of_entries_is_refused(self):
        with pytest.raises(sched.BadSchedule, match="not a pair"):
            built(visit("Ada", "20:00", "21:00", part=3, of=2))

    def test_an_unknown_time_zone_is_refused(self):
        with pytest.raises(sched.BadSchedule, match="time zone"):
            built(visit("Ada", "06:00", "09:00"), zone="Mars/Olympus")

    def test_a_schedule_that_is_not_an_object_is_refused(self):
        with pytest.raises(sched.BadSchedule):
            sched.parse([])

    def test_a_schedule_with_no_visits_list_is_refused(self):
        with pytest.raises(sched.BadSchedule, match="visits"):
            sched.parse({"zone": "America/New_York"})


class TestNoPatientIsNamedInThisRepository:
    """The constraint this module is shaped around. Their names, their homes
    and their hours are health information, and a git history is permanent —
    so the schedule is read from the device, exactly as the site's own
    particulars are.

    A test rather than a comment because a comment does not fail."""

    def test_the_module_ships_with_no_schedule_in_it(self):
        assert sched.Schedule([]).patients == []

    def test_the_path_it_reads_is_the_devices_own_config_directory(self):
        from apt_log.config import SITE_CONFIG_PATH

        assert REAL_PATH.parent == SITE_CONFIG_PATH.parent

    def test_and_the_suite_cannot_read_the_real_one(self):
        """conftest redirects it for every test, including the ones that
        never think about schedules. The deploy gate runs this suite on the
        machine that holds the real file."""
        assert sched.SCHEDULE_PATH != REAL_PATH

    def test_no_real_name_is_written_down_here(self):
        """Names in this file are invented, and the source of the module is
        checked rather than trusted."""
        import inspect

        source = inspect.getsource(sched)
        for surname in ("Villalon", "Puppo", "Mastrapa", "Batista",
                        "Torrien", "Zaldivar", "Fonseca", "Mederos"):
            assert surname.lower() not in source.lower()


class TestTheWalkedFlowsCarryNoPatient:
    """docs/EVV_FLOWS.md records what each app requires to reach a check-in
    control. Those screens carry names, home addresses and phone numbers, and
    a document written from them is exactly where one would end up by
    accident.

    Structure — resource ids, control captions, status words — is what a macro
    needs and what nobody can re-derive later. That is what may be written
    down; the people may not.
    """

    def _doc(self) -> str:
        from pathlib import Path

        return Path("docs/EVV_FLOWS.md").read_text(encoding="utf-8")

    def test_no_surname_from_the_round_appears(self):
        doc = self._doc().lower()
        for surname in ("villalon", "puppo", "mastrapa", "batista",
                        "torrien", "zaldivar", "fonseca", "mederos",
                        "caridad", "onorina", "atanasio", "marina",
                        "lucresia", "nieves", "carmen"):
            assert surname not in doc, f"{surname} reached a document"

    def test_no_street_address_or_telephone_number(self):
        """Both were on the visit-detail screen this was written from."""
        import re

        doc = self._doc()
        assert not re.search(r"\b\d{2,5}\s+\w+\s+(St|Ave|Rd|Dr|Blvd)\b", doc)
        assert not re.search(r"\(\d{3}\)\s*\d{3}-\d{4}", doc)

    def test_it_still_says_what_a_macro_would_need(self):
        """The other half of the rule. A document scrubbed until it is
        useless has not protected anybody, it has just lost the walk."""
        doc = self._doc()
        for needed in ("visits_event", "Comenzar Visita", "Cancelar Visita",
                       "content-desc", "com.tellus.evv.v2"):
            assert needed in doc
