"""The week's visits: when each one starts, and when the machine must move.

Three apps, several patients, and a caregiver whose first visit begins while
she is driving to her other job. The portal has always been able to show what the
phone is doing right now; what it could not say is what is coming, or when
anything has to happen. This is that answer.

WHAT IS AND IS NOT IN THIS FILE. Not one patient name, not one visit time.
Those are a person's health information — who is being cared for, at whose home,
at what hour — and a repository remembers forever. The schedule is read from
/etc/aptlog/schedule.json on the device, exactly as `config.py` reads the site's
own particulars and for exactly the reason stated at the top of that file. What
lives here is the SHAPE of a schedule and the arithmetic on it, which is the
part worth reviewing, testing, and arguing about.

THE ARITHMETIC, which is the whole reason this is a module and not a table.

  * A visit's NOMINAL time is what the app displays. It is the only time the
    agency knows about, and nothing here changes it.

  * A visit's FIRE time is when the machine acts. It equals the nominal start,
    except where the caregiver is physically coming from somebody else's home:
    where one patient's visit ends at the very minute the next one's begins,
    five minutes are added so she can get there. That gap does not exist in any
    app and cannot be read from one — it is a fact about driving, applied here.

  * The buffer is for TRAVEL, so it applies between different patients and not
    within one. Where a patient's two-hour block is recorded as two separate
    hourly entries, the second follows the first at the same address with
    nobody to drive anywhere, and it fires on the hour.

  * Where a gap already exists — an hour between visits, or the five minutes
    already built into the app's own times — nothing is added. The rule is
    "no gap at all", not "always five minutes".

  * ARM time is earlier still: opening an app, signing in, choosing an agency
    and finding a patient takes a while, and the whole point is to be standing
    on the check-in screen before the minute arrives rather than starting to
    look for it then.

TIME ZONE. Eastern, with daylight saving handled rather than hoped about. A
schedule stored as wall-clock times is the right way round — 5:00am is 5:00am
in March and in July, which is what the agency means — but it puts the burden
of resolving those to real instants here. Two dates a year do not have the
usual twenty-four hours, and both are handled explicitly below: this project
has a note in OPERATIONS.md about a Pi that believes it is 1970 firing a whole
day's schedule at once, and the appetite for clock surprises is low.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import (date as Date, datetime, time as Time, timedelta,
                      timezone)
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

SCHEDULE_PATH = Path("/etc/aptlog/schedule.json")

# Eastern. Named as a zone and not as an offset, which is the difference
# between a schedule that survives March and one that is an hour wrong for
# eight months of the year.
DEFAULT_ZONE = "America/New_York"

# The travel gap. Five minutes to leave one home and reach the next — read off
# the caregiver's own account of her round, not from any app.
BUFFER = timedelta(minutes=5)

# How long before firing the machine starts opening things. A starting figure,
# to be tuned per app once each flow has been walked and timed; `LEAD_BY_APP`
# is where a measured number goes when there is one.
LEAD = timedelta(minutes=15)
LEAD_BY_APP: dict[str, timedelta] = {}

# Monday-first, matching `date.weekday()`. Spanish accepted because the phone
# and the portal both speak it and the person maintaining the file may write it
# either way.
DAYS = {
    "mon": 0, "monday": 0, "lunes": 0,
    "tue": 1, "tuesday": 1, "martes": 1,
    "wed": 2, "wednesday": 2, "miercoles": 2, "miércoles": 2,
    "thu": 3, "thursday": 3, "jueves": 3,
    "fri": 4, "friday": 4, "viernes": 4,
    "sat": 5, "saturday": 5, "sabado": 5, "sábado": 5,
    "sun": 6, "sunday": 6, "domingo": 6,
}


class BadSchedule(ValueError):
    """The schedule file says something that cannot be acted on.

    Raised rather than skipped. A visit this code cannot read is a visit
    nobody is told about, and a schedule quietly one entry short is worse
    than one that refuses to load and says which line is wrong.
    """


@dataclass(frozen=True)
class Block:
    """One patient, one app, one recurring time — the file's own unit."""

    patient: str
    app: str
    days: frozenset[int]
    start: Time
    end: Time
    agency: str = ""
    # Which of a patient's entries this is, where a single stretch of care is
    # recorded as several. `of` is how many there are, so a caller can say
    # "entry 1 of 2" without counting.
    part: int = 1
    of: int = 1

    @property
    def split(self) -> bool:
        return self.of > 1


@dataclass(frozen=True)
class Visit:
    """A block resolved onto a real date: actual instants, not wall clock."""

    block: Block
    starts: datetime          # nominal, as the app displays it
    ends: datetime
    fires: datetime           # when the machine acts (nominal + buffer, maybe)
    arms: datetime            # when it starts getting ready

    @property
    def patient(self) -> str:
        return self.block.patient

    @property
    def app(self) -> str:
        return self.block.app

    @property
    def agency(self) -> str:
        return self.block.agency

    @property
    def buffered(self) -> bool:
        """Whether the travel gap moved this one off its nominal start."""
        return self.fires != self.starts

    @property
    def duration(self) -> timedelta:
        """How long the visit actually lasts.

        THROUGH UTC, AND NOT BY SUBTRACTING THE TWO. Python documents that
        when two aware datetimes share a tzinfo, subtraction ignores it and
        works on the naive wall clocks — so on the March morning the clocks
        move, one in the morning to five reads as four hours when three
        elapse. That is a silent hour, on a number that describes how long
        somebody was paid to be somewhere, and it is exactly the sort of
        thing nobody finds until March.
        """
        return (self.ends.astimezone(timezone.utc)
                - self.starts.astimezone(timezone.utc))

    # ------------------------------------------------- entering and leaving
    #
    # THE AGENCY'S RULE FOR A SPLIT VISIT, in the owner's words: "when there
    # is two you only enter on the earliest one of the halves and then you
    # only check out on the later one."
    #
    # So a pair of hourly entries is ONE stretch of care as far as EVV is
    # concerned — she arrives at the start of the first and leaves at the end
    # of the last, and nothing is recorded at the seam between them. Which is
    # the same shape as a single block: enter at the beginning, leave at the
    # end. The split exists for the agency's billing, not for the door.
    #
    # Getting this wrong is not cosmetic. Checking out at the seam and back
    # in again would put two short visits on the record where the agency
    # expects one, and a check-in on the second half is a check-in for a
    # visit she never left.

    @property
    def does_entry(self) -> bool:
        """Whether the check-in happens on this block."""
        return self.block.part == 1

    @property
    def does_exit(self) -> bool:
        """Whether the check-out happens on this block."""
        return self.block.part == self.block.of

    @property
    def entry_at(self) -> datetime | None:
        """When to check in, or None if this block does not.

        The FIRE time, not the nominal start: arriving is the half the travel
        gap is about.
        """
        return self.fires if self.does_entry else None

    @property
    def exit_at(self) -> datetime | None:
        """When to check out, or None if this block does not.

        The nominal end, with no buffer on it. A gap before a visit is time
        spent driving to it; there is no equivalent at the other end, and
        adding one would record her as staying five minutes longer than the
        agency scheduled.
        """
        return self.ends if self.does_exit else None

    def running(self, now: datetime) -> bool:
        """Whether this visit is happening at `now`.

        Measured from the NOMINAL start rather than the fire time: the visit
        the agency knows about began when it began, whatever minute the
        machine got to it.
        """
        return self.starts <= now < self.ends


# ---------------------------------------------------------------- reading it
def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise BadSchedule(f"unknown time zone {name!r}") from exc


def _time(raw, what: str) -> Time:
    """"HH:MM" as a time. Rejects anything else rather than guessing."""
    try:
        hour, _, minute = str(raw).partition(":")
        return Time(int(hour), int(minute))
    except (TypeError, ValueError) as exc:
        raise BadSchedule(f"{what} is not a HH:MM time: {raw!r}") from exc


def _days(raw, patient: str) -> frozenset[int]:
    if not raw:
        raise BadSchedule(f"{patient} has no days")
    out = set()
    for name in raw:
        key = str(name).strip().lower()
        if key not in DAYS:
            raise BadSchedule(f"{patient}: {name!r} is not a day of the week")
        out.add(DAYS[key])
    return frozenset(out)


def _block(entry: dict) -> Block:
    patient = str(entry.get("patient") or "").strip()
    if not patient:
        raise BadSchedule("a visit has no patient")
    app = str(entry.get("app") or "").strip()
    if not app:
        raise BadSchedule(f"{patient} has no app")
    start = _time(entry.get("start"), f"{patient}'s start")
    end = _time(entry.get("end"), f"{patient}'s end")
    if end <= start:
        # Not supported rather than silently mishandled. Every arithmetic
        # below assumes a visit begins and ends on one local date, and a
        # block that wrapped midnight would quietly compare against the wrong
        # day's neighbours.
        raise BadSchedule(
            f"{patient} at {start:%H:%M} ends at {end:%H:%M}; a visit must "
            "start and end on the same day")
    part = int(entry.get("part", 1))
    of = int(entry.get("of", 1))
    if part < 1 or of < 1 or part > of:
        raise BadSchedule(f"{patient}: entry {part} of {of} is not a pair")
    return Block(patient=patient, app=app, days=_days(entry.get("days"),
                                                      patient),
                 start=start, end=end,
                 agency=str(entry.get("agency") or "").strip(),
                 part=part, of=of)


class Schedule:
    """The week, and the arithmetic for asking questions of it."""

    def __init__(self, blocks: list[Block], zone: ZoneInfo | None = None):
        self.blocks = list(blocks)
        self.zone = zone or ZoneInfo(DEFAULT_ZONE)

    def __len__(self) -> int:
        return len(self.blocks)

    @property
    def patients(self) -> list[str]:
        """Every patient named, in the order the file names them."""
        seen: list[str] = []
        for block in self.blocks:
            if block.patient not in seen:
                seen.append(block.patient)
        return seen

    # ------------------------------------------------------------- one day
    def on(self, day: Date) -> list[Visit]:
        """Every visit on one local date, in the order they fire.

        This is where the travel buffer is applied, and it has to be here
        rather than on a Block: whether a visit is preceded by somebody else
        is a question about a DAY, and the same block is buffered on Monday
        and not on Saturday for exactly that reason.
        """
        today = [b for b in self.blocks if day.weekday() in b.days]
        # Nominal ends belonging to OTHER patients. A patient's own second
        # entry is not somebody to drive away from — see the note at the top.
        ends: dict[str, set[str]] = {}
        for block in today:
            ends.setdefault(block.end.isoformat(), set()).add(block.patient)

        visits = []
        for block in today:
            starts = self._at(day, block.start)
            others = ends.get(block.start.isoformat(), set()) - {block.patient}
            fires = starts + BUFFER if others else starts
            lead = LEAD_BY_APP.get(block.app, LEAD)
            visits.append(Visit(block=block, starts=starts,
                                ends=self._at(day, block.end),
                                fires=fires, arms=fires - lead))
        visits.sort(key=lambda v: (v.fires, v.block.patient, v.block.part))
        return visits

    def week(self, start: Date) -> list[Visit]:
        """Seven days from `start`, flat and in order."""
        out: list[Visit] = []
        for offset in range(7):
            out.extend(self.on(start + timedelta(days=offset)))
        return out

    # --------------------------------------------------------- what is next
    def upcoming(self, now: datetime, limit: int = 5,
                 days: int = 8) -> list[Visit]:
        """The next `limit` visits still to fire, soonest first.

        Walks forward across dates rather than filtering one day, because
        "what is next" at eleven at night is tomorrow's five o'clock and a
        version that only looked at today would show nothing at the moment
        the answer matters most.
        """
        now = self._aware(now)
        out: list[Visit] = []
        day = now.astimezone(self.zone).date()
        for offset in range(days):
            for visit in self.on(day + timedelta(days=offset)):
                if visit.fires > now:
                    out.append(visit)
                    if len(out) >= limit:
                        return out
        return out

    def next_after(self, now: datetime) -> Visit | None:
        found = self.upcoming(now, limit=1)
        return found[0] if found else None

    def current(self, now: datetime) -> Visit | None:
        """The visit happening right now, if one is.

        Yesterday is included in the search for the sake of nothing in
        particular today, but a block that runs to ten at night on a machine
        asked at 10:01 should not report a visit from a week ago either, so
        the window is deliberately narrow.
        """
        now = self._aware(now)
        day = now.astimezone(self.zone).date()
        for offset in (0, -1):
            for visit in self.on(day + timedelta(days=offset)):
                if visit.running(now):
                    return visit
        return None

    # ------------------------------------------------------------ the clock
    def _aware(self, when: datetime) -> datetime:
        """A naive datetime is read as local rather than refused.

        Callers inside this process pass aware datetimes. Callers from a
        template or a test often do not, and reading a bare datetime as UTC —
        which is what `astimezone` would do — would put every answer five
        hours out without saying so.
        """
        return when if when.tzinfo else when.replace(tzinfo=self.zone)

    def _at(self, day: Date, clock: Time) -> datetime:
        """A wall-clock time on a date, as a real instant.

        THE TWO DAYS A YEAR THIS EXISTS FOR.

        In March an hour does not happen. A wall time inside it names no
        instant, and Python will hand back something an hour off without
        complaint. None of the current visits fall in that window, which is
        exactly the kind of luck that stops being true when somebody edits
        the file, so it is detected and reported rather than left to be
        discovered by a visit firing at the wrong time.

        In November an hour happens twice, and the same wall time names two
        instants. `fold=0` takes the FIRST — the earlier one — which is the
        right way round for a scheduler whose whole brief is to err early.
        """
        when = datetime.combine(day, clock, tzinfo=self.zone)
        # A time that exists survives the round trip through UTC unchanged.
        # One that does not comes back as a different wall clock, which is
        # the only portable way to ask this question.
        round_trip = when.astimezone(timezone.utc).astimezone(self.zone)
        if round_trip.timetz() != when.timetz():
            raise BadSchedule(
                f"{clock:%H:%M} does not exist on {day.isoformat()} in "
                f"{self.zone}: the clocks move that morning")
        return when


def parse(doc: dict) -> Schedule:
    """A schedule from the file's own structure."""
    if not isinstance(doc, dict):
        raise BadSchedule("the schedule must be an object")
    entries = doc.get("visits")
    if not isinstance(entries, list):
        raise BadSchedule('the schedule needs a "visits" list')
    zone = _zone(str(doc.get("zone") or DEFAULT_ZONE))
    return Schedule([_block(e) for e in entries], zone)


def load(path: Path | None = None) -> Schedule:
    """The schedule on this device, or an empty one if there is none.

    An ABSENT file is not an error: this ships without a schedule and the
    portal has to render before anybody has written one. A file that exists
    and is wrong IS an error, and says which entry is wrong — the two cases
    look nothing alike and must not be collapsed.
    """
    target = path or SCHEDULE_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.info("no schedule at %s", target)
        return Schedule([])
    except OSError as exc:
        log.warning("cannot read the schedule (%s)", exc)
        return Schedule([])
    try:
        return parse(json.loads(text))
    except json.JSONDecodeError as exc:
        raise BadSchedule(f"{target} is not valid JSON: {exc}") from exc
