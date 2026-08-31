"""The five-minute nudge before a patient's visit.

"What we do need is a 5 minute reminder before another patient starts on the
schedule ... patient name, reminder message with time left, all in eastern
time."

Every name here is invented, as everywhere else that touches the round.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from apt_log import remind, schedule as sched

ZONE = ZoneInfo("America/New_York")


def a_schedule(**over):
    visit = {"patient": "UN PACIENTE", "app": "com.inmyteam.inmyteam",
             "start": "05:00", "end": "06:00", "days": ["tue"]}
    visit.update(over)
    return sched.parse({"zone": "America/New_York", "visits": [visit]})


def tuesday(hour, minute=0):
    # 2026-09-01 is a Tuesday.
    return datetime(2026, 9, 1, hour, minute, tzinfo=ZONE)


@pytest.fixture(autouse=True)
def _its_own_ledger(tmp_path, monkeypatch):
    """A real file, moved somewhere disposable — the once-only rule is the
    whole behaviour and a fake store would not exercise it."""
    from apt_log.ui import state

    monkeypatch.setattr(state, "STATE_DIR", str(tmp_path))


class TestWhenItSpeaks:
    def test_five_minutes_before_the_visit(self):
        s = a_schedule()
        assert [v.starts.hour for v in remind.due(s, tuesday(4, 55))] == [5]

    def test_not_a_moment_earlier(self):
        s = a_schedule()
        assert remind.due(s, tuesday(4, 54)) == []

    def test_still_speaks_if_the_tick_was_late(self):
        """A controller busy for a minute must still send one. Late by a
        minute is a reminder; a tick-exact match would simply miss."""
        s = a_schedule()
        assert len(remind.due(s, tuesday(4, 58))) == 1

    def test_but_not_once_the_visit_has_started(self):
        """Past the start it is not a reminder, it is a note about something
        she is already doing."""
        s = a_schedule()
        assert remind.due(s, tuesday(5, 0)) == []
        assert remind.due(s, tuesday(5, 30)) == []

    def test_every_visit_gets_one_armed_or_not(self):
        """Arming says whether the CONTROLLER may act. This is for the
        person, and she works the visits nobody armed too."""
        from apt_log import arming

        s = a_schedule()
        assert arming.armed() == set()
        assert len(remind.due(s, tuesday(4, 55))) == 1


class TestWhatItSays:
    def test_the_patient_is_the_title(self):
        """On a lock screen the first line is what gets read."""
        s = a_schedule()
        visit = s.on(tuesday(5, 0).date())[0]
        title, _ = remind.words(visit, tuesday(4, 55))
        assert title == "UN PACIENTE"

    def test_the_body_counts_the_minutes_it_actually_has(self):
        """Not "5" regardless — a late tick would then say five with two to
        go."""
        s = a_schedule()
        visit = s.on(tuesday(5, 0).date())[0]
        assert "5 minutos" in remind.words(visit, tuesday(4, 55))[1]
        assert "2 minutos" in remind.words(visit, tuesday(4, 58))[1]
        assert "1 minuto" in remind.words(visit, tuesday(4, 59))[1]

    def test_it_is_in_spanish(self):
        s = a_schedule()
        visit = s.on(tuesday(5, 0).date())[0]
        body = remind.words(visit, tuesday(4, 55))[1]
        assert body.startswith("Comienza en")
        assert "min" in body

    def test_and_carries_the_hour_in_eastern_time(self):
        """"All in eastern time" is not a formatting preference: a controller
        running UTC would otherwise name the wrong hour."""
        s = a_schedule()
        visit = s.on(tuesday(5, 0).date())[0]
        assert "5:00 a. m." in remind.words(visit, tuesday(4, 55))[1]

    def test_the_clock_is_written_the_way_spanish_writes_it(self):
        assert remind._clock(tuesday(17, 5)) == "5:05 p. m."
        assert remind._clock(tuesday(12, 0)) == "12:00 p. m."
        assert remind._clock(tuesday(0, 30)) == "12:30 a. m."

    def test_an_afternoon_visit_is_not_called_morning(self):
        s = a_schedule(start="15:20", end="17:20")
        visit = s.on(tuesday(5, 0).date())[0]
        assert "3:20 p. m." in remind.words(visit, tuesday(15, 15))[1]


class TestItSaysItOnce:
    def _push(self):
        return patch("apt_log.push.send", return_value=1)

    def test_the_tick_does_not_repeat_it_thirty_times(self):
        """The window is five minutes wide and the tick runs every few
        seconds."""
        s = a_schedule()
        with self._push() as sender:
            assert remind.send(s, tuesday(4, 55)) == 1
            assert remind.send(s, tuesday(4, 56)) == 0
            assert remind.send(s, tuesday(4, 58)) == 0
        assert sender.call_count == 1

    def test_tomorrow_is_a_different_visit(self):
        """Keyed by the block AND the date, so the same block reminds again
        the next time it comes round."""
        s = a_schedule(days=["tue", "wed"])
        with self._push() as sender:
            remind.send(s, tuesday(4, 55))
            remind.send(s, tuesday(4, 55) + timedelta(days=1))
        assert sender.call_count == 2

    def test_it_is_recorded_even_when_nobody_is_subscribed(self):
        """The question the ledger answers is "has this visit been
        announced", not "did a phone light up"."""
        s = a_schedule()
        with patch("apt_log.push.send", return_value=0):
            assert remind.send(s, tuesday(4, 55)) == 1
            assert remind.send(s, tuesday(4, 56)) == 0

    def test_a_push_that_throws_is_not_recorded(self):
        """It failed, so it has not been said. Trying again on the next tick
        is the right answer."""
        s = a_schedule()
        with patch("apt_log.push.send", side_effect=RuntimeError("no keys")):
            assert remind.send(s, tuesday(4, 55)) == 0
        with self._push():
            assert remind.send(s, tuesday(4, 56)) == 1

    def test_no_patient_name_reaches_the_ledger(self):
        """It is a state file on a machine that gets imaged. The key is a
        hash for exactly this reason."""
        s = a_schedule()
        with self._push():
            remind.send(s, tuesday(4, 55))
        written = (remind._path()).read_text(encoding="utf-8")
        assert "PACIENTE" not in written.upper()

    def test_two_visits_do_not_overwrite_each_others_banner(self):
        """A tag per visit, so the six o'clock notice does not replace the
        five o'clock one on a lock screen she has not looked at."""
        s = sched.parse({"zone": "America/New_York", "visits": [
            {"patient": "UN PACIENTE", "app": "com.inmyteam.inmyteam",
             "start": "05:00", "end": "06:00", "days": ["tue"]},
            {"patient": "OTRA PACIENTE", "app": "com.tellus.evv.v2",
             "start": "05:02", "end": "07:00", "days": ["tue"]}]})
        with self._push() as sender:
            remind.send(s, tuesday(4, 57))
        tags = {c.kwargs.get("tag") for c in sender.call_args_list}
        assert len(tags) == 2


class TestTheChannel:
    def test_it_never_goes_to_the_public_relay(self):
        """The relay is a public topic and this notice NAMES A PATIENT. That
        is the whole reason it is push-only."""
        from pathlib import Path

        src = (Path(remind.__file__)).read_text(encoding="utf-8")
        body = src.split('"""', 2)[2]
        assert "notify" not in body

    def test_a_missing_push_channel_is_not_an_error(self):
        """This runs on the tick beside the things that write EVV records."""
        s = a_schedule()
        with patch("apt_log.push.send", side_effect=OSError("no vapid")):
            assert remind.send(s, tuesday(4, 55)) == 0


class TestTheTickCallsIt:
    def test_the_runner_asks_on_every_pass(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1]
               / "src/apt_log/macros.py").read_text(encoding="utf-8")
        loop = src[src.index("self.maybe_fire()"):]
        assert "self.maybe_remind()" in loop[:400]

    def test_and_it_is_not_gated_on_arming(self, tmp_path):
        """Everything else on the tick is. This one must not be."""
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1]
               / "src/apt_log/macros.py").read_text(encoding="utf-8")
        body = src.split("def maybe_remind(", 1)[1].split("\n    def ", 1)[0]
        assert "arming" not in body
