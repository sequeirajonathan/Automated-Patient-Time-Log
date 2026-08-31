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

from apt_log import arming, autoentry, macros, schedule as sched

ZONE = ZoneInfo("America/New_York")


def a_schedule(**over):
    visit = {"patient": "Ada", "app": "com.tellus.evv.v2",
             "start": "09:00", "end": "12:00", "days": ["mon"]}
    visit.update(over)
    return sched.parse({"zone": "America/New_York", "visits": [visit]})


def monday(hour, minute=0):
    # 2026-08-17 is a Monday.
    return datetime(2026, 8, 17, hour, minute, tzinfo=ZONE)


class _FixedClock:
    """`datetime`, with `now()` pinned to the fixture week."""

    @staticmethod
    def now(tz=None):
        at = monday(12, 0)
        return at if tz is None else at.astimezone(tz)

    fromisoformat = staticmethod(datetime.fromisoformat)


@pytest.fixture(autouse=True)
def _the_ledger_clock_stands_still(monkeypatch):
    """A SUITE THAT EXPIRES IS A DEPLOY GATE THAT EXPIRES.

    `autoentry._forget_old` prunes the ledger against the REAL clock —
    `datetime.now() - LEDGER_KEEPS`, eight days — and every fixture in this
    file lives on a fixed Monday. So `spend()` wrote an occurrence and the
    prune dropped it on the way back out, and seven tests here asserting
    "nothing fires twice" quietly stopped testing anything.

    They did not fail on the day the fixture date was chosen. They passed for
    eight days and then failed for ever, and they failed on the machine that
    gates deploys — the manager runs this suite before every release, so an
    expired fixture is a Pi that cannot ship. Caught eleven days after the
    fixture Monday, with the deploy gate already red.

    Pinning the clock is the fix rather than moving the date forward, which
    would only reset the fuse.
    """
    monkeypatch.setattr(autoentry, "datetime", _FixedClock)


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


# THE STAND-IN FOR "NOBODY HAS WALKED THIS APP'S CHECK-IN".
#
# It used to be HHAeXchange+, which was walked on 2026-08-21 and is now in
# SUPPORTED — so these tests would have passed for the wrong reason, or been
# deleted, which is worse. The retired legacy app is genuinely unwalked and
# still a care app, so the rule keeps a real example to be true about.
LEGACY = "com.hhaexchange.caregiver"

class TestAnAppWhoseControlHasNeverBeenWalked:
    """HHAeXchange+'s check-in control has only ever been seen on a visit
    already under way. Pressing an unknown control on a live agency record to
    find out what it does is not a thing to do."""

    def test_the_two_walked_apps_are_supported(self):
        assert autoentry.supported("com.tellus.evv.v2") is True
        assert autoentry.supported("com.inmyteam.inmyteam") is True

    def test_and_the_unwalked_one_is_not(self):
        assert autoentry.supported(LEGACY) is False

    def test_the_refusal_has_a_reason_that_can_be_said_out_loud(self):
        assert autoentry.refusal(LEGACY, "entry") == "app_not_walked"

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
        # Both dates from the SAME clock the ledger prunes against. Taking
        # "old" from the real clock and letting the module prune against the
        # pinned one is how this test would go green on a lie.
        today = monday(12, 0)
        old = (today - autoentry.LEDGER_KEEPS - timedelta(days=2)).date()
        autoentry.spend(f"abc:{old.isoformat()}:entry", "done")
        autoentry.spend(f"def:{today.date().isoformat()}:entry", "done")
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
        # "app_not_walked" rather than "control_not_walked": nobody has
        # mapped this app at all. The narrower reason existed for an app
        # whose screens WERE walked but whose check-in button was never
        # seen, which was HHAeXchange+ until it was.
        assert autoentry.refusal(LEGACY, "entry") == "app_not_walked"

    def test_no_exit_is_fired_on_any_app(self):
        """No app's check-out control has been walked. inMyTeam's is "Note &
        Check out", which opens the note and the signature flow — a screen
        where the caregiver signs, and not one a timer should press for her."""
        for app in ("com.tellus.evv.v2", "com.inmyteam.inmyteam",
                    LEGACY):
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
        s = a_schedule(app=LEGACY)
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
        # AND WHICH VISIT, not just which patient. One patient can have two
        # cards on the same evening in HHAeXchange+, each with its own
        # check-in button, and the walk cannot choose between them without
        # being told the hour the block starts.
        assert sent == {"app": "com.tellus.evv.v2", "patient": "Ada",
                        "at": "09:00"}

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
                             app=LEGACY)
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

        self._armed(monkeypatch, monday(8, 50), app=LEGACY)
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


class TestGettingToTheVisitBeforePressingAnything:
    """THE FIRST ARMED FIRE FAILED ON THIS, and the reason is worth keeping.

    Android keeps app state, and `_bring_up` returns the moment the right
    package is in front — it does not care which of its screens that is. So
    the fire looked for the patient's row on the plan-of-care sheet the app
    had been sitting on since the night before, found nothing, and failed
    with "that visit is not on this screen". The lead-window walk failed the
    same way fifteen minutes earlier, which should have been the warning.
    """

    def test_the_two_walked_apps_know_where_their_visits_live(self):
        """Both apps need a word now, and Mobile Caregiver+'s was missing
        rather than unnecessary.

        This asserted an EMPTY tuple, with a comment explaining that the app
        lands on its week list so no bucket needs opening. That was written
        when nobody had found the control: walking it live turned up
        `spinnerPeriod`, a period selector whose options include "Hoy", and
        selecting it is what narrows the list to one day. The empty tuple was
        a gap wearing the clothes of a decision.
        """
        from apt_log import macros

        assert "Hoy" in macros.EVV_TODAY_WORDS["com.tellus.evv.v2"]
        # inMyTeam lands on buckets; today's rows are one tap further in.
        assert "Today" in macros.EVV_TODAY_WORDS["com.inmyteam.inmyteam"]

    def test_the_row_is_taken_where_it_stands_when_it_is_there(self):
        """No navigation when she is already on the list — the fire has a
        five-minute window and every Back press spends some of it."""
        from unittest.mock import patch
        from apt_log import macros

        with patch.object(macros, "_row_for", return_value="THE ROW") as look, \
             patch.object(macros, "_app_home") as home:
            got = macros._find_visit_row(object(), lambda _s: None,
                                         "com.inmyteam.inmyteam", "Ada")
        assert got == "THE ROW"
        assert home.call_count == 0
        assert look.call_count == 1

    def test_otherwise_it_walks_home_and_looks_again(self):
        from unittest.mock import patch
        from apt_log import macros

        found = [None, "THE ROW"]
        with patch.object(macros, "_row_for", side_effect=found), \
             patch.object(macros, "_app_home") as home, \
             patch.object(macros.time, "sleep"):
            got = macros._find_visit_row(object(), lambda _s: None,
                                         "com.tellus.evv.v2", "Ada")
        assert got == "THE ROW"
        assert home.call_count == 1

    def test_and_opens_todays_bucket_when_home_is_not_enough(self):
        """inMyTeam's home is a page of counts, not of visits."""
        from unittest.mock import MagicMock, patch
        from apt_log import macros

        bucket = MagicMock()
        with patch.object(macros, "_row_for",
                          side_effect=[None, None, "THE ROW"]), \
             patch.object(macros, "_app_home"), \
             patch.object(macros, "_words", return_value=bucket), \
             patch.object(macros.time, "sleep"):
            got = macros._find_visit_row(object(), lambda _s: None,
                                         "com.inmyteam.inmyteam", "Ada")
        assert got == "THE ROW"
        assert bucket.click.called

    def test_a_visit_that_is_genuinely_absent_still_refuses(self):
        """Walking further must not turn "not there" into a guess."""
        from unittest.mock import patch
        from apt_log import macros

        with patch.object(macros, "_row_for", return_value=None), \
             patch.object(macros, "_app_home"), \
             patch.object(macros, "_words", return_value=None), \
             patch.object(macros.time, "sleep"):
            assert macros._find_visit_row(object(), lambda _s: None,
                                          "com.inmyteam.inmyteam", "Ada") is None


class TestTheAppFetchesTodayBeforeAnythingIsPressed:
    """A SECOND WAY THE WALK COULD ARRIVE AT THE WRONG DAY, and this one no
    amount of navigation fixes.

    `_bring_up` returns the moment the right package is in front, and an app
    that has been in front since yesterday is still in front today, holding
    the list it fetched then. So the walk can reach a correctly-named screen
    and every check after it — the day check included — reads stale data.

    A force-stop is the only lever that an app cannot ignore. Pressing the
    app's own Refresh only re-renders what it already holds.
    """

    def _stub(self, macros, front, monkeypatch):
        """Everything a cold start touches, replaced by something watchable."""
        calls = {"stopped": [], "launched": []}
        monkeypatch.setattr(macros, "_force_stop",
                            lambda p: calls["stopped"].append(p))
        monkeypatch.setattr(macros, "_forget_stitched", lambda p: None)
        monkeypatch.setattr(macros, "wake_display", lambda: None)
        monkeypatch.setattr(macros, "_front_package", lambda: front)
        monkeypatch.setattr(macros.time, "sleep", lambda _s: None)
        monkeypatch.setattr(macros, "wait_for",
                            lambda check, timeout=0.0: check())
        from apt_log import feed as feed_mod
        monkeypatch.setattr(feed_mod, "_adb",
                            lambda argv: calls["launched"].append(argv))
        macros._freshened.clear()
        return calls

    def test_it_kills_the_app_and_brings_it_back(self, monkeypatch):
        from apt_log import macros

        pkg = "com.inmyteam.inmyteam"
        calls = self._stub(macros, pkg, monkeypatch)
        assert macros._freshen(object(), lambda _s: None, pkg) is True
        assert calls["stopped"] == [pkg]
        # Through the launcher intent, never the driver: after a force-stop
        # the driver's handle on the app is stale.
        assert any(pkg in argv for argv in calls["launched"])

    def test_an_app_that_does_not_come_back_is_a_failure(self, monkeypatch):
        import pytest
        from apt_log import macros

        pkg = "com.inmyteam.inmyteam"
        self._stub(macros, "com.android.launcher", monkeypatch)
        with pytest.raises(RuntimeError):
            macros._freshen(object(), lambda _s: None, pkg)

    def test_a_recent_cold_start_is_not_repeated(self, monkeypatch):
        """What lets the fire skip the cost the lead window already paid."""
        from apt_log import macros

        pkg = "com.inmyteam.inmyteam"
        calls = self._stub(macros, pkg, monkeypatch)
        macros._freshen(object(), lambda _s: None, pkg)
        again = macros._freshen(object(), lambda _s: None, pkg,
                                max_age=macros.FRESH_FOR)
        assert again is False
        assert calls["stopped"] == [pkg]

    def test_but_a_stale_one_is(self, monkeypatch):
        """An app last fetched yesterday is exactly the case this exists for."""
        import time as real_time
        from apt_log import macros

        pkg = "com.inmyteam.inmyteam"
        calls = self._stub(macros, pkg, monkeypatch)
        macros._freshened[pkg] = real_time.time() - macros.FRESH_FOR - 1
        assert macros._freshen(object(), lambda _s: None, pkg,
                               max_age=macros.FRESH_FOR) is True
        assert calls["stopped"] == [pkg]

    def test_the_ceiling_covers_the_lead_window_with_room_to_spare(self):
        """FRESH_FOR has to outlast the gap between getting ready and firing,
        or the fire pays for a restart it does not need — at the one moment
        in the day when twenty seconds are worth something."""
        from apt_log import macros

        assert macros.FRESH_FOR > 15 * 60

    def test_the_lead_window_always_starts_from_cold(self, monkeypatch):
        """Fifteen minutes of slack is what the lead window is FOR."""
        from unittest.mock import patch
        from apt_log import macros

        seen = {}

        def spy(driver, report, package, max_age=0.0):
            seen["max_age"] = max_age
            return True

        with patch.object(macros, "_freshen", spy), \
             patch.object(macros, "_open_todays_visit"):
            macros._evv_prepare(object(), lambda _s: None,
                                macros._evv_arg("com.inmyteam.inmyteam", "Ada"))
        assert seen["max_age"] == 0.0

    def test_the_fire_starts_from_cold_only_when_nothing_else_has(
            self, monkeypatch):
        """The lead window failing is the case that made this matter: a fire
        whose app has not been touched since yesterday must not press
        against yesterday's list."""
        from unittest.mock import patch
        from apt_log import macros

        for freshened, expect_bring_up in ((True, 0), (False, 1)):
            with patch.object(macros, "_freshen", return_value=freshened), \
                 patch.object(macros, "_bring_up") as up, \
                 patch.object(macros, "grant_location"), \
                 patch.object(macros, "_already_entered", return_value=""), \
                 patch.object(macros, "_open_todays_visit"), \
                 patch.object(macros, "_words", return_value=None):
                try:
                    macros._evv_entry(
                        object(), lambda _s: None,
                        macros._evv_arg("com.inmyteam.inmyteam", "Ada"))
                except RuntimeError:
                    # No control on the stubbed screen; the launch already
                    # happened, which is what this is watching.
                    pass
            assert up.call_count == expect_bring_up


class TestNotEnteringAVisitSomebodyAlreadyEntered:
    """THIS HAPPENED. On 2026-08-21 a 05:00-06:00 visit the caregiver had
    checked in and out from her own handset collected two more check-ins from
    the controller's phone, at 09:54 and 10:00, four hours after it ended.

    Nothing stopped it because the screen the walk reads is the wrong one.
    The Visit Detail's "Your activity on this patient" is a PER-DEVICE log —
    it survived a force-stop and cold relaunch still omitting her two events,
    at the same moment `My Work` -> Checks listed all four — so a phone that
    has never checked a patient in shows an empty record and a live `Check
    in`, and this app accepts the press and answers "Success".
    """

    def _log(self, macros, monkeypatch, lines, patient="Carmen Villalon"):
        """The Checks tab, answered without a phone."""
        rows = [{"txt": "HOME CARE ON CALL, LLC", "b": [0, 300, 700, 318]},
                {"txt": patient, "b": [0, 330, 700, 346]}]
        rows += [{"txt": w, "b": [0, 392 + i * 18, 700, 408 + i * 18]}
                 for i, w in enumerate(lines)]
        from apt_log import feed as feed_mod
        monkeypatch.setattr(feed_mod, "statics", lambda _x: list(rows))
        monkeypatch.setattr(macros, "_open_my_work", lambda d, r: True)
        monkeypatch.setattr(macros.time, "sleep", lambda _s: None)
        clicked = []
        monkeypatch.setattr(macros, "_words",
                            lambda d, *w: type("E", (), {
                                "click": lambda self: clicked.append(w[0])})())
        today = macros.datetime.now().astimezone().strftime("%Y-%m-%d")
        return type("D", (), {"page_source": f"<x>{today}</x>",
                              "current_package": "com.inmyteam.inmyteam"})()

    def test_it_reads_the_days_events_for_the_patient(self, monkeypatch):
        from apt_log import macros

        driver = self._log(macros, monkeypatch,
                           ["Check in 05:00 AM", "Check out 06:00 AM"])
        got = macros._todays_check_events(driver, lambda _s: None,
                                          "Carmen Villalon")
        assert got == ["Check in 05:00 AM", "Check out 06:00 AM"]

    def test_a_clear_day_reads_as_clear(self, monkeypatch):
        from apt_log import macros

        driver = self._log(macros, monkeypatch, [], patient="Someone Else")
        assert macros._todays_check_events(driver, lambda _s: None,
                                           "Carmen Villalon") == []

    def test_a_log_it_cannot_reach_is_an_error_not_an_empty_day(
            self, monkeypatch):
        """"I could not look" and "there is nothing there" are the same shape
        and opposite meanings, and a live agency record turns on which."""
        import pytest
        from apt_log import macros

        driver = self._log(macros, monkeypatch, ["Check in 05:00 AM"])
        monkeypatch.setattr(macros, "_open_my_work", lambda d, r: False)
        with pytest.raises(RuntimeError):
            macros._todays_check_events(driver, lambda _s: None, "Carmen Villalon")

    def test_a_log_showing_another_day_is_an_error_too(self, monkeypatch):
        """A range left over from another search answers a different question
        in exactly the same words."""
        import pytest
        from apt_log import macros

        driver = self._log(macros, monkeypatch, ["Check in 05:00 AM"])
        driver.page_source = "<x>1999-01-01</x>"
        # The cold-start recovery is exercised in TestTheRangeHasToSayToday;
        # here it is stubbed out so this asserts the refusal that follows it.
        monkeypatch.setattr(macros, "_freshen", lambda d, r, p: True)
        with pytest.raises(RuntimeError, match="not showing today"):
            macros._todays_check_events(driver, lambda _s: None, "Carmen Villalon")

    def test_the_checks_tab_is_opened_before_the_search_is_run(
            self, monkeypatch):
        """THE ORDER MATTERS AND IT IS NOT THE OBVIOUS ONE. The Visits tab
        returns nothing for a day whose Checks tab has four events, so a
        search run on the tab that opens comes back empty and looks exactly
        like a day with no work on it."""
        from apt_log import macros

        order = []
        driver = self._log(macros, monkeypatch, ["Check in 05:00 AM"])
        monkeypatch.setattr(macros, "_words",
                            lambda d, *w: type("E", (), {
                                "click": lambda self: order.append(w[0])})())
        macros._todays_check_events(driver, lambda _s: None, "Carmen Villalon")
        assert order.index(macros.CHECKS_TAB_WORDS[0]) \
            < order.index(macros.SEARCH_WORDS[0])

    def test_an_existing_check_in_is_named(self, monkeypatch):
        from apt_log import macros

        driver = self._log(macros, monkeypatch,
                           ["Check in 05:00 AM", "Check out 06:00 AM"])
        assert macros._already_entered(driver, lambda _s: None,
                                       "com.inmyteam.inmyteam",
                                       "Carmen Villalon") == "Check in 05:00 AM"

    def test_an_app_whose_log_is_not_walked_is_not_thereby_cleared(
            self, monkeypatch):
        """It answers "" because it has no way to know — which is why the
        caller gets a string and not a bool. Only the apps named in
        CHECK_LOG_APPS can give evidence either way."""
        from apt_log import macros

        assert "com.inmyteam.inmyteam" in macros.CHECK_LOG_APPS
        assert "com.hhaexchange.uma" not in macros.CHECK_LOG_APPS
        looked = []
        monkeypatch.setattr(macros, "_todays_check_events",
                            lambda *a: looked.append(1) or [])
        assert macros._already_entered(object(), lambda _s: None,
                                       "com.hhaexchange.uma", "Ada") == ""
        assert looked == []

    def test_the_fire_refuses_a_visit_that_is_already_checked_in(self):
        """And refuses BEFORE the visit is opened, because the visit's own
        page is the one that lies."""
        from unittest.mock import patch
        import pytest
        from apt_log import macros

        with patch.object(macros, "_freshen", return_value=True), \
             patch.object(macros, "grant_location"), \
             patch.object(macros, "_already_entered",
                          return_value="Check in 05:00 AM"), \
             patch.object(macros, "_open_todays_visit") as opened, \
             patch.object(macros, "_words") as pressed:
            with pytest.raises(RuntimeError, match="already checked in"):
                macros._evv_entry(object(), lambda _s: None,
                                  macros._evv_arg("com.inmyteam.inmyteam",
                                                  "Ada"))
        assert opened.call_count == 0
        assert pressed.call_count == 0

    def test_and_goes_ahead_on_a_clear_day(self):
        from unittest.mock import patch
        from apt_log import macros

        with patch.object(macros, "_freshen", return_value=True), \
             patch.object(macros, "grant_location"), \
             patch.object(macros, "_already_entered", return_value=""), \
             patch.object(macros, "_open_todays_visit") as opened, \
             patch.object(macros, "_words", return_value=None):
            try:
                macros._evv_entry(object(), lambda _s: None,
                                  macros._evv_arg("com.inmyteam.inmyteam",
                                                  "Ada"))
            except RuntimeError:
                pass        # no control on the stubbed screen; it got there
        assert opened.call_count == 1

    def test_the_screen_is_reachable_as_a_macro_of_its_own(self):
        """Four presses deep behind a drawer, a tab that is not the one that
        opens, and a Search that must come after the tab. Reaching it by hand
        while an entry is being watched is not a reasonable ask."""
        from apt_log import macros

        assert "evv_checks" in macros.MACROS
        assert macros.MACROS["evv_checks"].takes_arg


class TestGettingToTheWorkLogFromWhereverTheAppIsStanding:
    """THE FIRST LIVE RUN FAILED HERE, from a visit detail, reporting the work
    log unreachable when it was three Backs away.

    `_app_home` asks the atlas whether it is home yet, and inMyTeam's atlas
    names ONE activity — MainActivity is the visits hub, every bucket list and
    the visit detail alike. So "home" was true wherever it stood, the walk
    pressed nothing, and the drawer it then looked for was not there: an inner
    page draws Back in that corner, not the drawer.
    """

    def _phone(self, macros, monkeypatch, drawer_after: int):
        """A phone whose drawer appears after `drawer_after` Back presses."""
        state = {"backs": 0, "package": "com.inmyteam.inmyteam"}
        from apt_log import device as device_mod

        def back(_action):
            state["backs"] += 1
        monkeypatch.setattr(device_mod, "send_ui_action", back)
        monkeypatch.setattr(macros.time, "sleep", lambda _s: None)
        monkeypatch.setattr(macros, "_front_package", lambda: state["package"])
        monkeypatch.setattr(
            macros, "_the_drawer",
            lambda d: (type("Drawer", (), {"click": lambda self: None})()
                       if state["backs"] >= drawer_after else None))
        driver = type("D", (), {"current_package": state["package"]})()
        return state, driver

    def test_it_presses_back_until_the_drawer_is_there(self, monkeypatch):
        from apt_log import macros

        state, driver = self._phone(macros, monkeypatch, drawer_after=3)
        monkeypatch.setattr(macros, "_where_in_app", lambda p: "MyWorksFragment")
        clicked = []
        monkeypatch.setattr(macros, "_words",
                            lambda d, *w: type("E", (), {
                                "click": lambda self: clicked.append(w[0])})())
        assert macros._open_my_work(driver, lambda _s: None) is True
        assert state["backs"] == 3

    def test_a_front_page_needs_no_presses_at_all(self, monkeypatch):
        from apt_log import macros

        state, driver = self._phone(macros, monkeypatch, drawer_after=0)
        monkeypatch.setattr(macros, "_where_in_app", lambda p: "MyWorksFragment")
        monkeypatch.setattr(macros, "_words",
                            lambda d, *w: type("E", (), {
                                "click": lambda self: None})())
        assert macros._open_my_work(driver, lambda _s: None) is True
        assert state["backs"] == 0

    def test_it_gives_up_rather_than_pressing_back_for_ever(self, monkeypatch):
        """A drawer that never appears is a screen this walk does not know,
        and the caller must hear that rather than a clear day."""
        from apt_log import macros

        state, driver = self._phone(macros, monkeypatch, drawer_after=99)
        assert macros._back_to_the_drawer(
            driver, lambda _s: None, "com.inmyteam.inmyteam") is None
        assert state["backs"] == macros.BACKS_TO_DRAWER

    def test_and_stops_the_moment_back_leaves_the_app(self, monkeypatch):
        """Back from the app's root pops the task stack into whatever was
        under it. A second press from there would leave a second time."""
        from apt_log import macros

        state, driver = self._phone(macros, monkeypatch, drawer_after=99)
        from apt_log import device as device_mod

        def back(_action):
            state["backs"] += 1
            state["package"] = "com.android.launcher3"
        monkeypatch.setattr(device_mod, "send_ui_action", back)
        brought = []
        monkeypatch.setattr(macros, "_bring_up",
                            lambda d, p: brought.append(p))
        assert macros._back_to_the_drawer(
            driver, lambda _s: None, "com.inmyteam.inmyteam") is None
        assert state["backs"] == 1
        assert brought == ["com.inmyteam.inmyteam"]


class TestTheWorkLogIsOnePress:
    """Asked for outright: "I just need the steps to get to that screen so
    that my sister can keep an eye on the arm. I can't seem to reliably get to
    this screen."

    Neither could the walk on its first try, and neither could I by hand on
    three of five attempts. Four presses deep behind a drawer, a tab that is
    not the one that opens, and a Search that must follow the tab — and every
    wrong turn returns an empty list that looks exactly like a day with no
    work on it.
    """

    def test_it_is_offered_on_the_page_she_already_has(self):
        from apt_log import macros

        assert "evv_checks" in macros.OPERATIONS

    def test_it_never_needs_her_to_name_the_patient(self, monkeypatch):
        """The scheduler picked the visit; asking her which one it picked is
        asking the wrong person."""
        from apt_log import macros

        asked = {}
        monkeypatch.setattr(macros, "_the_visit_in_hand",
                            lambda: ("com.inmyteam.inmyteam", "Carmen"))
        monkeypatch.setattr(macros, "_bring_up", lambda d, p: None)
        monkeypatch.setattr(
            macros, "_todays_check_events",
            lambda d, r, patient: asked.setdefault("patient", patient) and [])
        macros._evv_checks(object(), lambda _s: None, "")
        assert asked["patient"] == "Carmen"

    def test_the_visit_in_hand_is_the_running_one(self, monkeypatch):
        from apt_log import macros

        running = self._v("com.inmyteam.inmyteam", "Carmen")
        plan = type("P", (), {"zone": None,
                              "current": lambda self, now: running,
                              "upcoming": lambda self, now, limit: []})()
        self._with_plan(macros, monkeypatch, plan)
        assert macros._the_visit_in_hand() == ("com.inmyteam.inmyteam",
                                               "Carmen")

    def test_and_the_next_one_when_none_is_running(self, monkeypatch):
        from apt_log import macros

        nxt = self._v("com.inmyteam.inmyteam", "Bea")
        plan = type("P", (), {"zone": None,
                              "current": lambda self, now: None,
                              "upcoming": lambda self, now, limit: [nxt]})()
        self._with_plan(macros, monkeypatch, plan)
        assert macros._the_visit_in_hand() == ("com.inmyteam.inmyteam", "Bea")

    def test_it_looks_past_visits_whose_record_it_cannot_read(
            self, monkeypatch):
        """LIVE, AT ELEVEN IN THE MORNING, the very next visit was on
        HHAeXchange+ — whose work log is not walked — so taking "next"
        literally made the button refuse for most of the day, in words that
        sound like a fault. The screen it lands on names the patient and the
        date, so which record is on show is never in doubt."""
        from apt_log import macros

        soon = [self._v("com.hhaexchange.uma", "Nieves"),
                self._v("com.tellus.evv.v2", "Bea"),
                self._v("com.inmyteam.inmyteam", "Carmen")]
        plan = type("P", (), {"zone": None,
                              "current": lambda self, now: None,
                              "upcoming": lambda self, now, limit: soon})()
        self._with_plan(macros, monkeypatch, plan)
        assert macros._the_visit_in_hand() == ("com.inmyteam.inmyteam",
                                               "Carmen")

    def test_but_still_names_the_real_situation_when_none_can_be_read(
            self, monkeypatch):
        """A day with nothing on a walked app must not come back pretending
        otherwise — the refusal names the app, which is the useful answer."""
        from apt_log import macros

        soon = [self._v("com.hhaexchange.uma", "Nieves")]
        plan = type("P", (), {"zone": None,
                              "current": lambda self, now: None,
                              "upcoming": lambda self, now, limit: soon})()
        self._with_plan(macros, monkeypatch, plan)
        assert macros._the_visit_in_hand() == ("com.hhaexchange.uma", "Nieves")

    def _v(self, app, patient):
        return type("V", (), {"app": app, "patient": patient})()

    def test_an_empty_schedule_says_so_rather_than_guessing(self, monkeypatch):
        import pytest
        from apt_log import macros

        plan = type("P", (), {"zone": None,
                              "current": lambda self, now: None,
                              "upcoming": lambda self, now, limit: []})()
        self._with_plan(macros, monkeypatch, plan)
        with pytest.raises(RuntimeError):
            macros._the_visit_in_hand()

    def _with_plan(self, macros, monkeypatch, plan):
        from apt_log import schedule as schedule_mod
        monkeypatch.setattr(schedule_mod, "load", lambda: plan)


class TestAskingTheAppWhereItIs:
    """"Does adb expose views on the app?" — asked while weighing a cold start
    against mapping every page into a graph of neighbours.

    It does, and it is better than a graph. `dumpsys activity` publishes the
    fragment back stack by class name, and unlike a map we maintain it cannot
    go stale: it is the app answering rather than us remembering. The visits
    hub says `VisitsFragment`, the work log says `MyWorksFragment` — checked
    live on the phone.

    It does NOT give a way to jump. inMyTeam declares no schemes at all, only
    MAIN/VIEW with LAUNCHER on its single activity, and everything inside is
    fragments, which are not addressable from outside. So the walk stays a
    walk — it just stops being a blind one.
    """

    DUMP = """
      Added Fragments:
        #0: ReportFragment{37fd427 #0 androidx.lifecycle.report_fragment_tag}
        #0: NavHostFragment{f702d04 (cd9c85d5) id=0x7f0a01ba}
        #0: MyWorksFragment{52db513 (70e31d4c) id=0x7f0a01ba}
    """

    def _adb(self, macros, monkeypatch, text):
        from apt_log import feed as feed_mod
        monkeypatch.setattr(
            feed_mod, "_adb",
            lambda *a, **k: type("R", (), {
                "stdout": text.encode()})())

    def test_it_names_the_screen_the_app_is_on(self, monkeypatch):
        from apt_log import macros

        self._adb(macros, monkeypatch, self.DUMP)
        assert macros._where_in_app("com.inmyteam.inmyteam") == \
            "MyWorksFragment"

    def test_the_plumbing_every_app_carries_is_not_a_screen(self,
                                                            monkeypatch):
        """ReportFragment and NavHostFragment are on every Jetpack screen and
        say nothing about where anybody is."""
        from apt_log import macros

        self._adb(macros, monkeypatch,
                  "#0: ReportFragment{a}\n#0: NavHostFragment{b}\n")
        assert macros._where_in_app("com.inmyteam.inmyteam") == ""

    def test_a_phone_that_will_not_say_answers_nothing(self, monkeypatch):
        """And "" must read as "no idea", never as "not there"."""
        from apt_log import macros
        from apt_log import feed as feed_mod

        def boom(*a, **k):
            raise OSError("adb is not there")
        monkeypatch.setattr(feed_mod, "_adb", boom)
        assert macros._where_in_app("com.inmyteam.inmyteam") == ""

    def test_the_walk_confirms_where_it_landed(self, monkeypatch):
        """A Search button is on more than one of this app's screens;
        MyWorksFragment is on exactly one."""
        from apt_log import macros

        monkeypatch.setattr(macros, "_back_to_the_drawer",
                            lambda d, r, p: self._clicker())
        monkeypatch.setattr(macros, "_words", lambda d, *w: self._clicker())
        monkeypatch.setattr(macros.time, "sleep", lambda _s: None)
        driver = type("D", (), {"current_package": "com.inmyteam.inmyteam"})()

        monkeypatch.setattr(macros, "_where_in_app", lambda p: "MyWorksFragment")
        assert macros._open_my_work(driver, lambda _s: None) is True

        monkeypatch.setattr(macros, "_where_in_app", lambda p: "VisitsFragment")
        assert macros._open_my_work(driver, lambda _s: None) is False

    def test_a_lost_walk_starts_the_app_from_cold_and_tries_once_more(
            self, monkeypatch):
        """Asked for outright: "or simply just do a quick restart to default
        towards the front of the page." A cold start is the one position this
        code can be certain of."""
        from apt_log import macros

        tries = {"n": 0}
        froze = []

        def drawer(d, r, p):
            tries["n"] += 1
            return self._clicker() if tries["n"] > 1 else None

        monkeypatch.setattr(macros, "_back_to_the_drawer", drawer)
        monkeypatch.setattr(macros, "_freshen",
                            lambda d, r, p: froze.append(p) or True)
        monkeypatch.setattr(macros, "_words", lambda d, *w: self._clicker())
        monkeypatch.setattr(macros, "_where_in_app", lambda p: "MyWorksFragment")
        monkeypatch.setattr(macros.time, "sleep", lambda _s: None)
        driver = type("D", (), {"current_package": "com.inmyteam.inmyteam"})()
        assert macros._open_my_work(driver, lambda _s: None) is True
        assert froze == ["com.inmyteam.inmyteam"]
        assert tries["n"] == 2

    def test_and_gives_up_if_the_cold_start_does_not_help(self, monkeypatch):
        from apt_log import macros

        monkeypatch.setattr(macros, "_back_to_the_drawer", lambda d, r, p: None)
        monkeypatch.setattr(macros, "_freshen", lambda d, r, p: True)
        monkeypatch.setattr(macros.time, "sleep", lambda _s: None)
        driver = type("D", (), {"current_package": "com.inmyteam.inmyteam"})()
        assert macros._open_my_work(driver, lambda _s: None) is False

    def _clicker(self):
        return type("E", (), {"click": lambda self: None})()


class TestTheRangeHasToSayToday:
    """"...and inputs today's date range by default."

    It already does — the two fields are picker-backed and the app fills them
    with today when the screen is fresh. What was missing was what happens
    when they are NOT today: a range left over from another search answers a
    different question in exactly the same words, and the answer decides
    whether a live agency record gets touched.

    Nothing is typed into a picker. A cold start is what puts the app's own
    defaults back, so that is the recovery — once, and then a refusal.
    """

    def _driver(self, macros, monkeypatch, sources):
        seen = iter(sources)
        monkeypatch.setattr(macros.time, "sleep", lambda _s: None)
        monkeypatch.setattr(macros, "_words", lambda d, *w: type(
            "E", (), {"click": lambda self: None})())
        from apt_log import feed as feed_mod
        monkeypatch.setattr(feed_mod, "statics", lambda _x: [])
        last = [sources[-1]]

        def read(self):
            try:
                last[0] = next(seen)
            except StopIteration:
                pass
            return last[0]

        return type("D", (), {
            "current_package": "com.inmyteam.inmyteam",
            "page_source": property(read)})()

    def test_a_stale_range_is_recovered_by_a_cold_start(self, monkeypatch):
        from apt_log import macros

        today = macros.datetime.now().astimezone().strftime("%Y-%m-%d")
        driver = self._driver(macros, monkeypatch,
                              ["<x>1999-01-01</x>", f"<x>{today}</x>",
                               f"<x>{today}</x>"])
        froze, opened = [], []
        monkeypatch.setattr(macros, "_freshen",
                            lambda d, r, p: froze.append(p) or True)
        monkeypatch.setattr(macros, "_open_my_work",
                            lambda d, r: opened.append(1) or True)
        assert macros._todays_check_events(driver, lambda _s: None, "Ada") == []
        assert froze == ["com.inmyteam.inmyteam"]
        assert len(opened) == 2

    def test_a_fresh_range_costs_no_restart(self, monkeypatch):
        from apt_log import macros

        today = macros.datetime.now().astimezone().strftime("%Y-%m-%d")
        driver = self._driver(macros, monkeypatch,
                              [f"<x>{today}</x>", f"<x>{today}</x>"])
        froze = []
        monkeypatch.setattr(macros, "_freshen",
                            lambda d, r, p: froze.append(p) or True)
        monkeypatch.setattr(macros, "_open_my_work", lambda d, r: True)
        assert macros._todays_check_events(driver, lambda _s: None, "Ada") == []
        assert froze == []

    def test_and_still_wrong_after_the_restart_is_a_refusal(self, monkeypatch):
        """Not a third attempt. A log that will not show today cannot answer
        the question, and saying so is the only honest move left."""
        import pytest
        from apt_log import macros

        driver = self._driver(macros, monkeypatch,
                              ["<x>1999-01-01</x>", "<x>1999-01-01</x>"])
        monkeypatch.setattr(macros, "_freshen", lambda d, r, p: True)
        monkeypatch.setattr(macros, "_open_my_work", lambda d, r: True)
        with pytest.raises(RuntimeError, match="not showing today"):
            macros._todays_check_events(driver, lambda _s: None, "Ada")


class TestTheCheckInBelongsToTheFirstHalf:
    """"We're only supposed to check in for caridad on her first block not
    the second one."

    Where a patient's evening is written as two consecutive entries, the
    agency's rule is: enter on the first, leave on the last, nothing at the
    seam. The engine has always known it — `Visit.entry_at` is None on a
    later half, so `due()` never raises an entry there and nothing could ever
    have fired.

    WHAT IT DID NOT DO WAS SAY SO. The arming page offered a switch on the
    second half whose only honest setting was off, and refused it — if at all
    — for an unrelated reason about the app. A switch that cannot do what it
    says is worse than a missing one, and this one sat next to a real switch
    for the same patient on the same evening.
    """

    def _block(self, part=1, of=2, app="com.inmyteam.inmyteam"):
        return type("B", (), {"part": part, "of": of, "app": app})()

    def test_the_second_half_is_refused_by_name(self):
        from apt_log import autoentry

        assert autoentry.refusal("com.inmyteam.inmyteam", "entry",
                                 self._block(part=2)) == \
            "entry_is_the_first_half"

    def test_the_first_half_is_not(self):
        from apt_log import autoentry

        assert autoentry.refusal("com.inmyteam.inmyteam", "entry",
                                 self._block(part=1)) == ""

    def test_a_walked_app_does_not_excuse_it(self):
        """It is a fact about the visit, not about the software, so it is
        checked BEFORE the app is."""
        from apt_log import autoentry

        assert "com.inmyteam.inmyteam" in autoentry.SUPPORTED
        assert autoentry.refusal("com.inmyteam.inmyteam", "entry",
                                 self._block(part=2)) != ""

    def test_and_an_unsplit_visit_is_untouched(self):
        from apt_log import autoentry

        assert autoentry.refusal("com.inmyteam.inmyteam", "entry",
                                 self._block(part=1, of=1)) == ""

    def test_the_engine_never_offered_an_entry_there_anyway(self):
        """Belt and braces, and worth stating: the refusal is about what the
        page SAYS. Nothing could have fired regardless."""
        from datetime import date
        from apt_log import schedule as sched

        plan = sched.parse({"zone": "America/New_York", "visits": [
            {"patient": "Ada", "app": "com.inmyteam.inmyteam",
             "days": ["mon"], "start": "20:05", "end": "21:05",
             "part": 1, "of": 2},
            {"patient": "Ada", "app": "com.inmyteam.inmyteam",
             "days": ["mon"], "start": "21:05", "end": "22:05",
             "part": 2, "of": 2}]})
        first, second = plan.on(date(2026, 6, 15))
        assert first.entry_at is not None and first.exit_at is None
        assert second.entry_at is None and second.exit_at is not None

    def test_fireable_drops_a_later_half_it_is_handed(self):
        from apt_log import autoentry

        def due(part):
            visit = type("V", (), {"app": "com.inmyteam.inmyteam",
                                   "block": self._block(part=part)})()
            return type("D", (), {"visit": visit, "kind": "entry"})()

        kept = autoentry.fireable([due(1), due(2)])
        assert [d.visit.block.part for d in kept] == [1]


class TestPickingTheRightCardInExchangePlus:
    """WALKED LIVE on 2026-08-21 at 20:05, four minutes before a real visit,
    with the owner watching, and confirmed by him to hold for every patient
    in this app "regardless of agency selected".

    HHAeXchange+ puts its check-in on the landing screen: Programación lists
    today's visits already expanded, each with its own `Registro de entrada
    de EVV`. No agency is chosen and no visit detail is opened.

    WHICH MAKES PICKING THE CARD THE WHOLE PROBLEM. A patient whose evening
    is written as two entries has TWO cards, each with a button, and pressing
    the wrong one records the wrong half of the visit on a live agency
    record. They are told apart by the hours printed on the card — which are
    the AGENCY's window, not ours: the cards read 8:00-9:00 and 9:00-10:00
    where the schedule says 8:05 and 9:05.
    """

    def _cards(self, *pairs):
        from datetime import time as dtime
        return [{"button": {"b": [44, y, 688, y + 52]},
                 "at": dtime(int(t.split(":")[0]), int(t.split(":")[1])),
                 "says": t}
                for y, t in pairs]

    def test_it_reads_the_hours_a_card_prints(self):
        from apt_log import macros

        assert macros._uma_start("8:00 p. m. - 9:00 p. m.").hour == 20
        assert macros._uma_start("9:00 p. m. - 10:00 p. m.").hour == 21
        assert macros._uma_start("5:00 a. m. - 8:00 a. m.").hour == 5
        assert macros._uma_start("12:15 p. m. - 5:15 p. m.").hour == 12
        assert macros._uma_start("12:30 a. m. - 1:30 a. m.").hour == 0
        assert macros._uma_start("Detalles del paciente") is None

    def test_the_nearest_card_wins_not_the_equal_one(self):
        """The schedule says 8:05 and the card says 8:00, by design — the
        five minutes are the travel buffer. Equality would never match."""
        from apt_log import macros

        cards = self._cards((388, "20:00"), (637, "21:00"))
        assert macros._uma_pick(cards, "20:05")["says"] == "20:00"
        assert macros._uma_pick(cards, "21:05")["says"] == "21:00"

    def test_a_lone_card_needs_no_time_at_all(self):
        """Two of the three apps show one card per patient per day, and the
        schedule has never had to say which visit it meant."""
        from apt_log import macros

        cards = self._cards((388, "06:00"))
        assert macros._uma_pick(cards, "")["says"] == "06:00"

    def test_but_two_cards_and_no_time_is_a_refusal(self):
        import pytest
        from apt_log import macros

        cards = self._cards((388, "20:00"), (637, "21:00"))
        with pytest.raises(RuntimeError, match="tell them apart"):
            macros._uma_pick(cards, "")

    def test_and_nothing_close_enough_is_a_refusal(self):
        """A check-in on the wrong half is worse than one not made."""
        import pytest
        from apt_log import macros

        cards = self._cards((388, "20:00"), (637, "21:00"))
        with pytest.raises(RuntimeError, match="matches that visit"):
            macros._uma_pick(cards, "06:00")

    def test_a_tie_is_a_refusal_too(self):
        import pytest
        from apt_log import macros

        cards = self._cards((388, "20:00"), (637, "20:00"))
        with pytest.raises(RuntimeError, match="equally well"):
            macros._uma_pick(cards, "20:05")

    def test_an_empty_screen_is_a_refusal(self):
        import pytest
        from apt_log import macros

        with pytest.raises(RuntimeError, match="not on this screen"):
            macros._uma_pick([], "20:05")

    def test_the_app_is_now_walked_end_to_end(self):
        from apt_log import macros

        assert "com.hhaexchange.uma" in macros.EVV_ENTRY_WORDS
        assert "Registro de entrada de EVV" in \
            macros.EVV_ENTRY_WORDS["com.hhaexchange.uma"]
        assert "Registro de salida de EVV" in \
            macros.EVV_STARTED_WORDS["com.hhaexchange.uma"]

    def test_the_fire_tells_the_walk_which_visit_it_means(self):
        """Without the start time the walk cannot choose between two cards,
        so the scheduler has to say — and it does, off the block itself."""
        from apt_log import macros

        arg = macros._evv_arg("com.hhaexchange.uma", "Ada", "20:05")
        assert macros._evv_when(arg) == "20:05"
        assert macros._evv_parts(arg) == ("com.hhaexchange.uma", "Ada")


class TestTheDrawerIsFoundInEitherLanguage:
    """The guard that failed open.

    `_the_drawer` matched one English content-desc. The moment the app
    rendered Spanish it returned None, and `_open_my_work` — the route to the
    Checks table, which is what stops an entry being fired for a visit the
    caregiver already entered by hand — became a no-op. Silently, and in the
    direction of doing the unsafe thing.

    Confirmed on the live handset: `Abrir panel lateral de navegación` is what
    the button says, and the English string is nowhere in that hierarchy.
    """

    class _Driver:
        """Answers an xpath the way UiAutomator2 does: matches on the
        content-desc literals named in the query."""

        def __init__(self, desc):
            self.desc = desc
            self.asked = []

        def find_elements(self, how, query):
            self.asked.append(query)
            if f'@content-desc="{self.desc}"' in query:
                return [_Shown()]
            return []

    def test_english(self):
        d = self._Driver("Open navigation drawer")
        assert macros._the_drawer(d) is not None

    def test_spanish(self):
        d = self._Driver("Abrir panel lateral de navegación")
        assert macros._the_drawer(d) is not None

    def test_spanish_without_the_accent(self):
        d = self._Driver("Abrir panel lateral de navegacion")
        assert macros._the_drawer(d) is not None

    def test_a_screen_with_no_drawer_is_still_none(self):
        d = self._Driver("Navigate up")
        assert macros._the_drawer(d) is None

    def test_it_asks_once_rather_than_once_per_language(self):
        """This runs inside a walk against the clock, and reading the tree is
        the expensive part."""
        d = self._Driver("Abrir panel lateral de navegación")
        macros._the_drawer(d)
        assert len(d.asked) == 1

    def test_every_spelling_is_in_the_one_query(self):
        d = self._Driver("nothing")
        macros._the_drawer(d)
        for spelling in macros.DRAWER_DESCS:
            assert spelling in d.asked[0]


class _Shown:
    def is_displayed(self):
        return True


class TestASessionMayBeRevivedForAVisitThatNeedsIt:
    """Automatic sign-in was gated on somebody watching the portal, and that
    gate answers the wrong question overnight.

    The app expires its own session on inactivity. At four in the morning
    nobody is watching, so the one thing that exists to fix that refuses to
    run; the phone stands on the sign-in page until morning and the visit
    fires into a signed-out app.

    Arming is what closes it honestly: it is already a person's attestation
    that the caregiver is with the patient (REQ-5.9), so reviving the app
    that visit runs in is inside what somebody has asked for.
    """

    def test_nothing_armed_means_nothing_needs_reviving(self):
        s = a_schedule()
        assert autoentry.on_duty(s, monday(9, 0)) is False

    def test_an_armed_visit_in_its_window_needs_the_app(self):
        s = a_schedule()
        arm(s.blocks[0])
        assert autoentry.on_duty(s, monday(9, 0)) is True

    def test_the_window_opens_when_the_visit_arms_not_when_it_fires(self):
        """The lead walk needs the app signed in too — by the time the fire
        is due it should be one press, not a cold start and a login against
        the clock."""
        s = a_schedule()
        arm(s.blocks[0])
        visit = s.on(monday(9, 0).date())[0]
        assert visit.arms < visit.fires
        assert autoentry.on_duty(s, visit.arms) is True
        assert autoentry.on_duty(
            s, visit.arms - timedelta(minutes=1)) is False

    def test_it_still_holds_mid_visit_because_a_check_out_is_coming(self):
        """The expiry that started all this happened DURING a visit. A window
        that closed at the entry would leave the check-out stranded."""
        s = a_schedule()
        arm(s.blocks[0])
        assert autoentry.on_duty(s, monday(10, 30)) is True

    def test_and_closes_once_the_visit_and_its_grace_are_past(self):
        s = a_schedule()
        arm(s.blocks[0])
        visit = s.on(monday(9, 0).date())[0]
        assert autoentry.on_duty(s, visit.ends + autoentry.GRACE) is True
        assert autoentry.on_duty(
            s, visit.ends + autoentry.GRACE + timedelta(minutes=1)) is False

    def test_the_overnight_loop_the_gate_was_built_for_stays_prevented(self):
        """Three in the morning, armed visit or not: nothing is due, so
        nothing may sign itself in."""
        s = a_schedule()
        arm(s.blocks[0])
        assert autoentry.on_duty(s, monday(3, 0)) is False

    def test_it_is_asked_about_one_app_not_any_app(self):
        """A visit in Mobile Caregiver+ is no reason to revive inMyTeam."""
        s = a_schedule(app="com.tellus.evv.v2")
        arm(s.blocks[0])
        assert autoentry.on_duty(s, monday(9, 0), "com.tellus.evv.v2") is True
        assert autoentry.on_duty(
            s, monday(9, 0), "com.inmyteam.inmyteam") is False

    def test_no_app_named_asks_about_any_of_them(self):
        s = a_schedule()
        arm(s.blocks[0])
        assert autoentry.on_duty(s, monday(9, 0), "") is True

    def test_a_visit_nobody_armed_is_not_a_reason(self):
        """The switch defaults to off and that default is the design. An
        unarmed visit must not open the unattended door."""
        s = a_schedule()
        assert autoentry.on_duty(s, monday(9, 0), "com.tellus.evv.v2") is False


class TestAFailedFireSaysWhyItFailed:
    """"So did it actually arm the patient in the morning, or did it fail?"

    It failed, and the only durable record said `RuntimeError` and nothing
    else. The journal that held the reason is Storage=volatile and 64MB and
    had already rotated by the time the question was asked, so the answer was
    gone. A check-in that did not happen for a real patient is the one event
    that has to stay explicable hours later.

    The message goes in the LEDGER, which never leaves the machine — never on
    `Status`, every field of which is written to macro-status.json and drawn
    in the portal.
    """

    def _runner(self, tmp_path):
        from apt_log import macros

        return macros.Runner(tmp_path / "req.json", tmp_path / "status.json",
                             screen_path=tmp_path / "screen.json")

    def _armed(self, monkeypatch, when=None):
        from apt_log import macros, schedule as schedule_mod

        s = a_schedule()
        arm(s.blocks[0], who="Jonathan")
        monkeypatch.setattr(schedule_mod, "load", lambda *a, **k: s)
        monkeypatch.setattr(macros, "datetime",
                            _FrozenClock(when or monday(9, 0)))
        return s

    def _record(self, s):
        key = arming.key_for(s.blocks[0])
        return autoentry.spent()[f"{key}:{monday(9, 0).date()}:entry"]

    def test_an_exception_escaping_the_run_is_quoted(self, tmp_path,
                                                     monkeypatch):
        from unittest.mock import patch

        s = self._armed(monkeypatch)
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute",
                          side_effect=RuntimeError("no check-in control")):
            assert runner.maybe_fire() is False
        rec = self._record(s)
        assert rec["outcome"] == "failed"
        assert rec["error"] == "RuntimeError"
        assert "no check-in control" in rec["detail"]

    def _macro_raises(self, monkeypatch, words):
        """Make the evv_entry macro itself blow up, so `execute` catches it —
        the path this morning's fire actually took, where the message never
        reaches the caller at all."""
        from unittest.mock import MagicMock

        from apt_log import macros, resident

        def run(driver, report, *rest):
            raise RuntimeError(words)

        monkeypatch.setattr(macros.MACROS["evv_entry"], "run", run)
        monkeypatch.setattr(resident, "run",
                            lambda fn: fn(MagicMock()))
        from apt_log.ui import mirror as mirror_mod

        monkeypatch.setattr(mirror_mod, "publish", lambda **kw: None)

    def test_and_so_is_one_the_macro_swallowed(self, tmp_path, monkeypatch):
        s = self._armed(monkeypatch)
        self._macro_raises(monkeypatch, "the visit card was not there")
        runner = self._runner(tmp_path)
        assert runner.maybe_fire() is False
        rec = self._record(s)
        assert rec["error"] == "RuntimeError"
        assert "the visit card was not there" in rec["detail"]

    def test_the_message_never_reaches_the_portal(self, tmp_path,
                                                  monkeypatch):
        """Everything on Status is written to macro-status.json and shown to
        her, so a message that may name a screen, a control or a patient must
        not be on it. That rule predates this and still holds."""
        import json as _json

        self._armed(monkeypatch)
        self._macro_raises(monkeypatch, "A PATIENT has no card")
        runner = self._runner(tmp_path)
        runner.maybe_fire()
        shown = _json.loads((tmp_path / "status.json").read_text())
        assert shown.get("error") == "RuntimeError"
        assert "A PATIENT" not in _json.dumps(shown)

    def test_a_success_carries_no_stale_message(self, tmp_path, monkeypatch):
        """The field is cleared at the start of every run, so a failure
        cannot lend its words to the next visit's record."""
        from unittest.mock import patch

        s = self._armed(monkeypatch)
        runner = self._runner(tmp_path)
        runner._last_failure = "something from an earlier run"
        with patch.object(runner, "execute") as execute:
            execute.return_value = _Done()
            assert runner.maybe_fire() is True
        assert self._record(s)["detail"] == ""

    def test_the_ledger_is_swept_before_an_image_is_taken(self):
        """It holds dates and WHO ATTESTED to presence, and now a macro's own
        words as well. The hashed keys are why this looked harmless."""
        import pathlib

        script = (pathlib.Path(__file__).resolve().parents[1]
                  / "scripts/sanitize-for-image.sh").read_text(encoding="utf-8")
        assert "fired.json" in script
