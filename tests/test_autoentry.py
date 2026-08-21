"""What the scheduler is allowed to do, and when it must refuse.

Every name here is invented, as everywhere else that touches the round.

The thing being decided is whether to write an EVV record asserting a caregiver
was at a patient's home. So the properties this file holds are the refusals:
nothing fires unarmed, nothing fires twice, nothing fires late, and nothing
fires into an app whose control has never been walked.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from apt_log import arming, autoentry, schedule as sched

ZONE = ZoneInfo("America/New_York")


def a_schedule(**over):
    visit = {"patient": "Ada", "app": "com.tellus.evv.v2",
             "start": "09:00", "end": "12:00", "days": ["mon"]}
    visit.update(over)
    return sched.parse({"zone": "America/New_York", "visits": [visit]})


def monday(hour, minute=0):
    # 2026-08-17 is a Monday.
    return datetime(2026, 8, 17, hour, minute, tzinfo=ZONE)


def arm(block, who="Jonathan", since=None):
    """Arm a block, attested BEFORE the fixture week.

    `set_armed` stamps the real clock, and these fixtures live in a fixed
    week in the past — so every attestation would land in the future and
    `missed()` would suppress the whole file's worth of slipped visits. The
    stamp is rewritten to a week before the fixture Monday, which is what
    "this has been armed for a while" means here. A test about the switch
    being thrown JUST NOW passes its own `since`.
    """
    key = arming.key_for(block)
    arming.set_armed(key, True, who=who)
    when = since if since is not None else monday(0, 0) - timedelta(days=7)
    doc = arming._read()
    doc.setdefault("by", {})[key] = {"who": who, "at": when.isoformat()}
    arming._path().write_text(json.dumps(doc), encoding="utf-8")
    return key


class TestNothingFiresUnlessSomebodyArmedIt:
    def test_a_due_visit_nobody_armed_is_not_due(self):
        s = a_schedule()
        assert autoentry.due(s, monday(9, 0)) == []

    def test_and_the_same_visit_armed_is(self):
        s = a_schedule()
        arm(s.blocks[0])
        got = autoentry.due(s, monday(9, 0))
        assert [d.kind for d in got] == ["entry"]

    def test_disarming_takes_it_away_again(self):
        s = a_schedule()
        key = arm(s.blocks[0])
        arming.set_armed(key, False)
        assert autoentry.due(s, monday(9, 0)) == []


class TestTheAttestationTravelsWithTheFire:
    """REQ-5.9. A record whose presence was attested by a person must never be
    indistinguishable from one whose presence was observed by a machine —
    that is the whole reason the trade is acceptable."""

    def test_who_armed_it_reaches_the_decision(self):
        s = a_schedule()
        arm(s.blocks[0], who="Jonathan")
        got = autoentry.due(s, monday(9, 0))
        assert got[0].who == "Jonathan"
        assert got[0].attested_at        # a real timestamp, not a blank

    def test_an_arming_with_no_name_still_arms_but_says_so(self):
        """A missing name must never silently mean "not armed" — the switch
        is the decision. It is recorded as unknown instead."""
        s = a_schedule()
        arming.set_armed(arming.key_for(s.blocks[0]), True)
        got = autoentry.due(s, monday(9, 0))
        assert len(got) == 1
        assert got[0].who == "unknown"

    def test_disarming_drops_the_claim_so_a_rearm_cannot_inherit_it(self):
        s = a_schedule()
        key = arm(s.blocks[0], who="Jonathan")
        arming.set_armed(key, False)
        assert arming.attestation(key) == {}


class TestNothingFiresTwice:
    """A double check-in is a corrupted record on somebody's timesheet, which
    is worse than a missed one."""

    def test_a_spent_occurrence_is_not_offered_again(self):
        s = a_schedule()
        arm(s.blocks[0])
        first = autoentry.due(s, monday(9, 0))[0]
        autoentry.spend(first.occurrence, "done")
        assert autoentry.due(s, monday(9, 1)) == []

    def test_the_slot_is_claimed_before_the_phone_is_touched(self):
        """A crash between pressing the button and recording it would leave
        the occurrence unspent and the next tick would press it again."""
        s = a_schedule()
        arm(s.blocks[0])
        item = autoentry.due(s, monday(9, 0))[0]
        autoentry.spend(item.occurrence, "running")
        assert autoentry.is_spent(item.occurrence) is True
        autoentry.spend(item.occurrence, "done")
        assert autoentry.spent()[item.occurrence]["outcome"] == "done"

    def test_a_failure_still_spends_the_slot(self):
        """Fail closed (REQ-5.5): a fire that could not complete is not
        retried into a record with a later arrival minute than the truth."""
        s = a_schedule()
        arm(s.blocks[0])
        item = autoentry.due(s, monday(9, 0))[0]
        autoentry.spend(item.occurrence, "failed")
        assert autoentry.due(s, monday(9, 2)) == []

    def test_mondays_entry_does_not_spend_the_next_mondays(self):
        s = a_schedule()
        arm(s.blocks[0])
        item = autoentry.due(s, monday(9, 0))[0]
        autoentry.spend(item.occurrence, "done")
        next_monday = monday(9, 0) + timedelta(days=7)
        assert len(autoentry.due(s, next_monday)) == 1

    def test_an_entry_does_not_spend_its_own_exit(self):
        s = a_schedule()
        arm(s.blocks[0])
        entry = autoentry.due(s, monday(9, 0))[0]
        autoentry.spend(entry.occurrence, "done")
        assert [d.kind for d in autoentry.due(s, monday(12, 0))] == ["exit"]


class TestLateIsWorseThanNever:
    """The record would claim an arrival minute that has passed, which is the
    one thing an EVV record is for."""

    def test_it_does_not_fire_before_its_moment(self):
        s = a_schedule()
        arm(s.blocks[0])
        assert autoentry.due(s, monday(8, 59)) == []

    def test_it_fires_within_the_grace_window(self):
        s = a_schedule()
        arm(s.blocks[0])
        late = monday(9, 0) + autoentry.GRACE - timedelta(seconds=1)
        assert len(autoentry.due(s, late)) == 1

    def test_and_never_once_the_window_has_closed(self):
        s = a_schedule()
        arm(s.blocks[0])
        late = monday(9, 0) + autoentry.GRACE + timedelta(minutes=1)
        assert autoentry.due(s, late) == []

    def test_what_slipped_is_reported_rather_than_swallowed(self):
        """A person now has to enter it by hand, so a person has to be told."""
        s = a_schedule()
        arm(s.blocks[0])
        late = monday(9, 0) + autoentry.GRACE + timedelta(minutes=1)
        assert [d.kind for d in autoentry.missed(s, late)] == ["entry"]

    def test_something_already_done_is_not_reported_as_missed(self):
        s = a_schedule()
        arm(s.blocks[0])
        item = autoentry.due(s, monday(9, 0))[0]
        autoentry.spend(item.occurrence, "done")
        late = monday(9, 0) + autoentry.GRACE + timedelta(minutes=1)
        assert autoentry.missed(s, late) == []

    def test_a_visit_late_at_night_is_still_reachable_after_midnight(self):
        """Its grace window crosses the date line, and looking only at today
        would drop it."""
        s = a_schedule(start="23:58", end="23:59")
        arm(s.blocks[0])
        just_after = datetime(2026, 8, 18, 0, 1, tzinfo=ZONE)
        got = autoentry.due(s, just_after)
        # Both halves of this one are inside the window at 00:01, and both
        # belong to YESTERDAY's occurrence rather than today's.
        assert [d.kind for d in got] == ["entry", "exit"]
        assert all(d.occurrence.startswith(f"{d.key}:2026-08-17:")
                   for d in got)


class TestAnAppWhoseControlHasNeverBeenWalked:
    """HHAeXchange+'s check-in control has only ever been seen on a visit
    already under way. Pressing an unknown control on a live agency record to
    find out what it does is not a thing to do."""

    def test_the_two_walked_apps_are_supported(self):
        assert autoentry.supported("com.tellus.evv.v2") is True
        assert autoentry.supported("com.inmyteam.inmyteam") is True

    def test_and_the_unwalked_one_is_not(self):
        assert autoentry.supported("com.hhaexchange.uma") is False

    def test_the_refusal_has_a_reason_that_can_be_said_out_loud(self):
        assert autoentry.UNSUPPORTED_REASON["com.hhaexchange.uma"]

    def test_nothing_unknown_is_supported_by_accident(self):
        assert autoentry.supported("com.some.other.app") is False
        assert autoentry.supported("") is False


class TestGettingReadyOnTheDay:
    """inMyTeam draws "Check in" only on the scheduled day — the evening
    before shows "This visit is not scheduled for today" and no control. So
    the WALK belongs in the lead window on the day, and arming stays the
    standing decision it is."""

    def test_an_armed_visit_is_prepared_inside_its_lead_window(self):
        s = a_schedule(app="com.inmyteam.inmyteam")
        arm(s.blocks[0])
        ready = autoentry.preparing(s, monday(8, 50))
        assert [d.visit.patient for d in ready] == ["Ada"]

    def test_not_before_the_lead_window_opens(self):
        s = a_schedule(app="com.inmyteam.inmyteam")
        arm(s.blocks[0])
        assert autoentry.preparing(s, monday(8, 30)) == []

    def test_and_not_once_it_has_fired(self):
        s = a_schedule(app="com.inmyteam.inmyteam")
        arm(s.blocks[0])
        item = autoentry.due(s, monday(9, 0))[0]
        autoentry.spend(item.occurrence, "done")
        assert autoentry.preparing(s, monday(9, 0)) == []

    def test_nothing_unarmed_is_prepared(self):
        s = a_schedule(app="com.inmyteam.inmyteam")
        assert autoentry.preparing(s, monday(8, 50)) == []


class TestTheSplitVisitRule:
    """"When there is two you only enter on the earliest one of the halves and
    then you only check out on the later one." Arming has to respect that or
    the machine records two short visits where the agency expects one."""

    def _split(self):
        return sched.parse({"zone": "America/New_York", "visits": [
            {"patient": "Ada", "app": "com.tellus.evv.v2", "days": ["mon"],
             "start": "09:00", "end": "10:00", "part": 1, "of": 2},
            {"patient": "Ada", "app": "com.tellus.evv.v2", "days": ["mon"],
             "start": "10:00", "end": "11:00", "part": 2, "of": 2},
        ]})

    def test_the_first_half_enters_and_does_not_leave(self):
        s = self._split()
        for b in s.blocks:
            arm(b)
        assert [d.kind for d in autoentry.due(s, monday(9, 0))] == ["entry"]
        assert autoentry.due(s, monday(10, 0)) == []

    def test_the_last_half_leaves_and_does_not_enter(self):
        s = self._split()
        for b in s.blocks:
            arm(b)
        assert [d.kind for d in autoentry.due(s, monday(11, 0))] == ["exit"]

    def test_arming_one_half_does_not_arm_the_other(self):
        s = self._split()
        arm(s.blocks[0])
        assert [d.kind for d in autoentry.due(s, monday(9, 0))] == ["entry"]
        assert autoentry.due(s, monday(11, 0)) == []


class TestTheLedgerDoesNotGrowForever:
    def test_an_old_occurrence_is_forgotten(self):
        """It exists to stop a repeat, and a repeat can only happen within a
        day. Keeping a year would hold a record of a caregiver's movements
        long after the thing it prevents is impossible."""
        old = (datetime.now().astimezone()
               - autoentry.LEDGER_KEEPS - timedelta(days=2)).date()
        autoentry.spend(f"abc:{old.isoformat()}:entry", "done")
        autoentry.spend("def:%s:entry"
                        % datetime.now().astimezone().date().isoformat(),
                        "done")
        keys = autoentry.spent()
        assert not any(k.startswith("abc:") for k in keys)
        assert any(k.startswith("def:") for k in keys)

    def test_an_unreadable_key_is_kept_rather_than_guessed_at(self):
        autoentry.spend("nonsense", "done")
        assert "nonsense" in autoentry.spent()


class TestTheLedgerFailsSafe:
    def test_an_unreadable_ledger_reports_nothing_spent(self, tmp_path):
        """Which errs toward firing, so it is the reading that needs saying
        out loud: the arming switch and the grace window are what stop a
        runaway, and both are read from elsewhere."""
        from apt_log.ui import state as state_mod

        (state_mod.STATE_DIR / autoentry.LEDGER_NAME).write_text(
            "{ not json", encoding="utf-8")
        assert autoentry.spent() == {}

    def test_a_ledger_of_the_wrong_shape_too(self):
        from apt_log.ui import state as state_mod

        (state_mod.STATE_DIR / autoentry.LEDGER_NAME).write_text(
            '{"done": "everything"}', encoding="utf-8")
        assert autoentry.spent() == {}


class TestOrdering:
    def test_a_backlog_is_worked_oldest_first(self):
        """If two are somehow waiting, the earlier one's window closes first."""
        s = sched.parse({"zone": "America/New_York", "visits": [
            {"patient": "Ada", "app": "com.tellus.evv.v2", "days": ["mon"],
             "start": "09:00", "end": "09:30"},
            {"patient": "Bo", "app": "com.tellus.evv.v2", "days": ["mon"],
             "start": "09:02", "end": "09:40"},
        ]})
        for b in s.blocks:
            arm(b)
        got = autoentry.due(s, monday(9, 3))
        assert [d.when for d in got] == sorted(d.when for d in got)


class TestWhatTheMachineWillActuallyPress:
    """Deciding a visit is due is not the same as being able to do it. The
    apps whose control has never been walked, and the check-OUT nobody has
    seen, are refused HERE — visibly and by name — rather than being absent
    and looking like an oversight."""

    def test_an_entry_on_a_walked_app_is_fireable(self):
        assert autoentry.refusal("com.tellus.evv.v2", "entry") == ""
        assert autoentry.refusal("com.inmyteam.inmyteam", "entry") == ""

    def test_an_entry_on_the_unwalked_app_is_not(self):
        assert autoentry.refusal("com.hhaexchange.uma", "entry") \
            == "control_not_walked"

    def test_no_exit_is_fired_on_any_app(self):
        """No app's check-out control has been walked. inMyTeam's is "Note &
        Check out", which opens the note and the signature flow — a screen
        where the caregiver signs, and not one a timer should press for her."""
        for app in ("com.tellus.evv.v2", "com.inmyteam.inmyteam",
                    "com.hhaexchange.uma"):
            assert autoentry.refusal(app, "exit") == "exit_is_hers"

    def test_fireable_keeps_the_entry_and_drops_the_exit(self):
        s = a_schedule()
        arm(s.blocks[0])
        both = autoentry.due(s, monday(9, 0)) + autoentry.due(s, monday(12, 0))
        assert {d.kind for d in both} == {"entry", "exit"}
        assert [d.kind for d in autoentry.fireable(both)] == ["entry"]

    def test_the_unwalked_app_is_dropped_even_when_armed_and_due(self):
        """The whole point: an armed HHAeXchange+ visit is DUE, and still not
        pressed, because nobody has seen the button that starts it."""
        s = a_schedule(app="com.hhaexchange.uma")
        arm(s.blocks[0])
        due_now = autoentry.due(s, monday(9, 0))
        assert len(due_now) == 1
        assert autoentry.fireable(due_now) == []

    def test_an_unknown_app_is_refused_with_a_reason_not_a_crash(self):
        assert autoentry.refusal("com.some.new.app", "entry") == "app_not_walked"


class TestTheRunnerActuallyFires:
    """The scheduler's hands. Every property here is about the ORDER things
    happen in, because the order is the safety."""

    def _runner(self, tmp_path):
        from apt_log import macros

        return macros.Runner(tmp_path / "req.json", tmp_path / "status.json",
                             screen_path=tmp_path / "screen.json")

    def _armed_schedule(self, monkeypatch, when, app="com.tellus.evv.v2"):
        from apt_log import macros, schedule as schedule_mod

        s = a_schedule(app=app)
        arm(s.blocks[0], who="Jonathan")
        monkeypatch.setattr(schedule_mod, "load", lambda *a, **k: s)
        monkeypatch.setattr(macros, "datetime", _FrozenClock(when))
        return s

    def test_an_armed_due_visit_is_pressed(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        self._armed_schedule(monkeypatch, monday(9, 0))
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            execute.return_value = _Done()
            assert runner.maybe_fire() is True
        assert execute.call_args.args[0] == "evv_entry"

    def test_the_patient_reaches_the_macro_with_its_app(self, tmp_path,
                                                        monkeypatch):
        import json as _json
        from unittest.mock import patch

        self._armed_schedule(monkeypatch, monday(9, 0))
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            execute.return_value = _Done()
            runner.maybe_fire()
        sent = _json.loads(execute.call_args.args[2])
        assert sent == {"app": "com.tellus.evv.v2", "patient": "Ada"}

    def test_it_does_not_fire_twice(self, tmp_path, monkeypatch):
        """THE LEDGER IS WHAT STOPS THIS, not the tick throttle. So the
        throttle is cleared between the two calls — otherwise this test
        passes whether or not the ledger works at all, which is the least
        useful shape a safety test can have."""
        from unittest.mock import patch

        self._armed_schedule(monkeypatch, monday(9, 0))
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            execute.return_value = _Done()
            assert runner.maybe_fire() is True
            runner._fire_checked = None
            assert runner.maybe_fire() is False
            assert execute.call_count == 1

    def test_the_tick_throttle_does_not_hide_a_second_fire(self, tmp_path,
                                                           monkeypatch):
        """And prove the throttle alone would NOT have stopped it: with the
        ledger cleared, the same runner fires again."""
        from unittest.mock import patch

        self._armed_schedule(monkeypatch, monday(9, 0))
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            execute.return_value = _Done()
            assert runner.maybe_fire() is True
            from apt_log.ui import state as state_mod
            (state_mod.STATE_DIR / autoentry.LEDGER_NAME).write_text(
                '{"done": {}}', encoding="utf-8")
            runner._fire_checked = None
            assert runner.maybe_fire() is True
            assert execute.call_count == 2

    def test_the_slot_is_spent_even_when_the_press_throws(self, tmp_path,
                                                          monkeypatch):
        """A crash mid-press must not leave the slot open for the next tick —
        a double check-in is worse than a missed one."""
        from unittest.mock import patch

        s = self._armed_schedule(monkeypatch, monday(9, 0))
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute", side_effect=RuntimeError("boom")):
            assert runner.maybe_fire() is False
        item = autoentry.due(s, monday(9, 1))
        assert item == []          # spent, not offered again

    def test_the_attestation_is_written_into_the_ledger(self, tmp_path,
                                                        monkeypatch):
        """REQ-5.9 — an attested record must never read as an observed one."""
        from unittest.mock import patch

        s = self._armed_schedule(monkeypatch, monday(9, 0))
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            execute.return_value = _Done()
            runner.maybe_fire()
        entry = list(autoentry.spent().values())[0]
        assert entry["presence"] == "attested"
        assert entry["attested_by"] == "Jonathan"
        assert entry["outcome"] == "done"

    def test_the_unwalked_app_is_never_pressed(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        self._armed_schedule(monkeypatch, monday(9, 0),
                             app="com.hhaexchange.uma")
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_fire() is False
        assert execute.call_count == 0

    def test_it_yields_to_a_running_macro_rather_than_queueing(
            self, tmp_path, monkeypatch):
        """A fire that waits is a fire that lands late, and late is refused."""
        from unittest.mock import patch
        from apt_log import macros

        self._armed_schedule(monkeypatch, monday(9, 0))
        runner = self._runner(tmp_path)
        macros.write_status(macros.Status(id="x", name="rescan",
                                          state="running"),
                            tmp_path / "status.json")
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_fire() is False
        assert execute.call_count == 0

    def test_nothing_armed_presses_nothing(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        from apt_log import macros, schedule as schedule_mod

        monkeypatch.setattr(schedule_mod, "load",
                            lambda *a, **k: a_schedule())
        monkeypatch.setattr(macros, "datetime", _FrozenClock(monday(9, 0)))
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_fire() is False
        assert execute.call_count == 0

    def test_an_unreadable_schedule_is_not_an_error(self, tmp_path,
                                                    monkeypatch):
        from unittest.mock import patch
        from apt_log import macros, schedule as schedule_mod

        def boom(*a, **k):
            raise schedule_mod.BadSchedule("nope")

        monkeypatch.setattr(schedule_mod, "load", boom)
        monkeypatch.setattr(macros, "datetime", _FrozenClock(monday(9, 0)))
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_fire() is False
        assert execute.call_count == 0


class _Done:
    state = "done"
    error = ""


class _FrozenClock:
    """`datetime.now().astimezone()` pinned, so a test can stand at a minute."""

    def __init__(self, when):
        self._when = when

    def now(self, tz=None):
        return self._when


class TestTheLedgerNamesNobody:
    """The occurrence key is a hash and the detail is structural. A file
    recording where a caregiver was, minute by minute, is exactly the spread
    this project keeps refusing — and this one is written on every fire."""

    def test_no_patient_name_reaches_the_ledger(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        from apt_log import macros, schedule as schedule_mod
        from apt_log.ui import state as state_mod

        s = a_schedule(patient="Wilhelmina Farthingale")
        arm(s.blocks[0], who="Jonathan")
        monkeypatch.setattr(schedule_mod, "load", lambda *a, **k: s)
        monkeypatch.setattr(macros, "datetime", _FrozenClock(monday(9, 0)))
        runner = macros.Runner(tmp_path / "req.json", tmp_path / "status.json",
                               screen_path=tmp_path / "screen.json")
        with patch.object(runner, "execute") as execute:
            execute.return_value = _Done()
            runner.maybe_fire()
        raw = (state_mod.STATE_DIR / autoentry.LEDGER_NAME).read_text(
            encoding="utf-8")
        assert "Wilhelmina" not in raw and "Farthingale" not in raw

    def test_nor_the_status_file_a_failure_writes(self, tmp_path, monkeypatch):
        """`execute` sends `type(exc).__name__` rather than the message for
        exactly this reason, and the EVV macros keep that true by never
        putting a name in one."""
        from apt_log import macros

        for text in (macros.FIRE_FAILED, macros.FIRE_MISSED):
            assert "{" not in text        # nothing interpolated into a notice

    def test_the_notice_that_leaves_the_building_says_almost_nothing(self):
        """These land on a lock screen and travel through a public relay, so
        the sentence's whole job is to get somebody to open the portal —
        where the details sit behind the tailnet and a login."""
        from apt_log import macros

        for text in (macros.FIRE_FAILED, macros.FIRE_MISSED):
            assert "check-in" in text.lower()
            assert "portal" in text.lower() or "hand" in text.lower()
            # No name, no clock, no app: a fixed sentence with nothing
            # interpolated can only ever say what is written here.
            assert "{" not in text and "%" not in text


class TestWhatMakesAnArmFireOnTime:
    """The owner's question, and it deserves a precise answer: "What
    determines an arm to be on schedule every time without fail? I noticed
    when you run stuff the time is totally off — I'm assuming the sandbox is
    in UTC so we need to make sure these macros actually run on eastern."

    Three separate things, and only one of them was ever at risk.
    """

    def test_the_schedule_carries_its_own_zone(self):
        """Not the machine's. The file says America/New_York and that is what
        every wall-clock time in it means."""
        s = a_schedule()
        assert str(s.zone) == "America/New_York"

    def test_firing_compares_instants_so_the_machines_zone_cannot_shift_it(self):
        """An aware 9am Eastern is the same instant however it is spelled.
        This is why a UTC controller would still fire at the right moment —
        the part that was never at risk."""
        s = a_schedule()
        arm(s.blocks[0])
        from datetime import timezone
        as_utc = monday(9, 0).astimezone(timezone.utc)
        assert as_utc.hour != 9              # genuinely a different spelling
        assert len(autoentry.due(s, as_utc)) == 1

    def test_the_calendar_day_is_read_in_the_schedules_zone(self):
        """THE PART THAT WAS AT RISK. `now.date()` is not an instant, it is a
        calendar day, and which day it names depends on the zone it is read
        in. A controller running UTC calls 9pm Eastern "tomorrow" — so a
        visit late in the evening would be looked for on the wrong page of
        the calendar for four hours out of every day."""
        from datetime import timezone

        s = a_schedule(start="21:00", end="22:00")
        arm(s.blocks[0])
        nine_pm_eastern = monday(21, 0)
        # The same instant as read by a UTC machine: already the 18th there.
        as_utc = nine_pm_eastern.astimezone(timezone.utc)
        assert as_utc.date().day == 18 and nine_pm_eastern.date().day == 17
        got = autoentry.due(s, as_utc)
        assert [d.kind for d in got] == ["entry"]

    def test_and_the_lead_window_is_too(self):
        from datetime import timezone

        s = a_schedule(start="21:00", end="22:00",
                       app="com.inmyteam.inmyteam")
        arm(s.blocks[0])
        as_utc = monday(20, 50).astimezone(timezone.utc)
        assert [d.visit.patient for d in autoentry.preparing(s, as_utc)] \
            == ["Ada"]


class TestGettingTheAppUpBeforeTheEntry:
    """Without this the fire is a cold start against the clock: launch, wait
    for a splash, maybe sign in, find the patient, open the visit, press —
    all inside a five-minute window, on an app asleep since yesterday."""

    def _runner(self, tmp_path):
        from apt_log import macros

        return macros.Runner(tmp_path / "req.json", tmp_path / "status.json",
                             screen_path=tmp_path / "screen.json")

    def _armed(self, monkeypatch, when, app="com.inmyteam.inmyteam"):
        from apt_log import macros, schedule as schedule_mod

        s = a_schedule(app=app)
        arm(s.blocks[0], who="Jonathan")
        monkeypatch.setattr(schedule_mod, "load", lambda *a, **k: s)
        monkeypatch.setattr(macros, "datetime", _FrozenClock(when))
        return s

    def test_the_lead_window_walks_the_app_to_the_visit(self, tmp_path,
                                                        monkeypatch):
        from unittest.mock import patch

        self._armed(monkeypatch, monday(8, 50))
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_prepare() is True
        assert execute.call_args.args[0] == "evv_prepare"

    def test_it_walks_once_and_not_on_every_tick(self, tmp_path, monkeypatch):
        """It navigates the phone; doing that on a loop would fight her for
        it."""
        from unittest.mock import patch

        self._armed(monkeypatch, monday(8, 50))
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_prepare() is True
            runner._prep_checked = None
            assert runner.maybe_prepare() is False
        assert execute.call_count == 1

    def test_nothing_is_walked_before_the_lead_window(self, tmp_path,
                                                      monkeypatch):
        from unittest.mock import patch

        self._armed(monkeypatch, monday(8, 30))
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_prepare() is False
        assert execute.call_count == 0

    def test_the_unwalked_app_is_not_prepared_either(self, tmp_path,
                                                     monkeypatch):
        from unittest.mock import patch

        self._armed(monkeypatch, monday(8, 50), app="com.hhaexchange.uma")
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_prepare() is False
        assert execute.call_count == 0

    def test_a_failed_walk_does_not_stop_the_fire(self, tmp_path,
                                                  monkeypatch):
        """The entry still has its own attempt from wherever the phone is,
        and that attempt is the one that matters."""
        from unittest.mock import patch

        s = self._armed(monkeypatch, monday(8, 50))
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute", side_effect=RuntimeError("no")):
            runner.maybe_prepare()
        assert len(autoentry.due(s, monday(9, 0))) == 1


class TestArmingDoesNotClaimThePast:
    """WATCHED LIVE. Arming Carmen's weekday block at 00:46 immediately
    announced YESTERDAY's 05:00 as missed and alerted a phone about it. It
    was not missed — nobody had armed it, so the machine was never going to
    do it, and saying otherwise turns the one notice that means "go and do
    this by hand" into noise."""

    def test_a_switch_thrown_now_does_not_report_yesterday(self):
        s = a_schedule(days=["mon", "tue"])
        key = arm(s.blocks[0], who="Jonathan")
        # Armed a minute ago; yesterday's visit is long past its window.
        claims = {key: {"who": "Jonathan",
                        "at": monday(13, 0).isoformat()}}
        tuesday = monday(9, 0) + timedelta(days=1)
        assert autoentry.missed(s, tuesday, attestations=claims) == []

    def test_but_it_does_report_one_missed_since_it_was_armed(self):
        s = a_schedule(days=["mon", "tue"])
        key = arm(s.blocks[0], who="Jonathan")
        claims = {key: {"who": "Jonathan",
                        "at": (monday(9, 0) - timedelta(days=2)).isoformat()}}
        late = monday(9, 0) + autoentry.GRACE + timedelta(minutes=1)
        assert [d.kind for d in
                autoentry.missed(s, late, attestations=claims)] == ["entry"]

    def test_a_switch_with_no_recorded_time_reports_as_before(self):
        """It predates attestations, which makes it an OLD switch — armed
        long enough ago that yesterday really was its responsibility."""
        s = a_schedule()
        key = arm(s.blocks[0])
        late = monday(9, 0) + autoentry.GRACE + timedelta(minutes=1)
        assert [d.kind for d in
                autoentry.missed(s, late, attestations={key: {}})] == ["entry"]

    def test_an_unparseable_time_is_treated_as_unknown_not_as_now(self):
        s = a_schedule()
        key = arm(s.blocks[0])
        late = monday(9, 0) + autoentry.GRACE + timedelta(minutes=1)
        claims = {key: {"who": "x", "at": "not a date"}}
        assert [d.kind for d in
                autoentry.missed(s, late, attestations=claims)] == ["entry"]
