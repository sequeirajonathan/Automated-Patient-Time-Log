"""What the scheduler is allowed to do, right now, and to which visit.

THIS MODULE DECIDES; IT DOES NOT ACT. Everything here is a pure function of a
schedule, the armed set, a ledger of what has already happened and a clock.
That separation is deliberate: the thing being decided is whether to write an
EVV record asserting a caregiver was at a patient's home, and a decision of
that kind should be readable, and testable, without a phone in the room.

WHY IT MAY ACT AT ALL. The controller and the phone live IN the building with
the patients — the owner's correction, and it is the fact the whole design
rests on: "the Pi lives at the building and so does the phone, there will never
be an out of range GPS check". So REQ-5's anchors describe the right place, and
the gate is corroboration rather than a thing to work around.

What arming adds on top of that is the decision itself (REQ-5.9). The owner's
words: "If armed you can assume my sister is already taking care of the patient
... she just doesn't have the time to do the entry because she has to start
right away. Arming is a commitment."

So the problem this solves is not "she is not there yet". It is "she is there,
with her hands full, and the entry has to land on the minute the agency expects
rather than whenever she next gets a free moment."

FOUR THINGS GUARD EVERY FIRE, and none of them is presence:

  ARMED      somebody attested to this block, and the attestation is carried
             into the record so an attested fire never reads as an observed one
  DUE        the moment has arrived, and has not been missed by so much that
             firing now would be a lie about when she arrived
  UNSPENT    this occurrence has not already been done — by the machine, or by
             her, or by anybody
  SUPPORTED  the app's entry is actually implemented and walked

The last one is not a formality, and it held HHAeXchange+ out of this list
until its check-in was actually walked on a live visit with the owner watching
(docs/EVV_FLOWS.md). An app whose entry nobody has seen is refused by name
rather than guessed at, and the refusal is the feature.

LATE IS WORSE THAN NEVER. A fire that has slipped past its window is not
quietly caught up: the record would claim an arrival minute that has passed,
which is the one thing an EVV record is for. Past the window it is dropped and
said out loud, so a person can enter it by hand with the truth in front of
them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# Beside the other things the machine remembers about itself.
LEDGER_NAME = "fired.json"

# HOW LATE IS TOO LATE. The tick runs every few seconds, so under normal
# conditions a fire lands within one of its due minute. This window is for the
# abnormal: the resident session rebuilding, a macro holding the phone, the Pi
# rebooting. Five minutes is the same span as the travel buffer, and past it
# the arrival minute the record would claim is far enough from the truth that
# a person should be the one deciding what to write.
GRACE = timedelta(minutes=5)

# APPS WHOSE ENTRY HAS BEEN WALKED AND CAN BE PRESSED.
#
# Mobile Caregiver+ publishes `Comenzar Visita` with real bounds and a
# machine-readable status, and inMyTeam publishes `Check in` on the day.
#
# HHAeXchange+ WAS ABSENT FROM THIS LIST FOR MONTHS, and the reason it is
# here now is that the thing it was waiting on finally happened: its control
# had only ever been seen on a visit already under way, so the button that
# starts an unstarted one was unobserved, and adding it would have meant
# pressing an unknown control on a live agency record.
#
# It was walked on 2026-08-21 at 20:05, four minutes before a real visit,
# with the owner watching and confirming each step. Programación lists the
# day's visits already expanded, each with its own `Registro de entrada de
# EVV`; pressing it opens a GPS map with one `Continuar`; pressing that
# writes the record. No agency has to be chosen — confirmed by the owner for
# every patient in the app — and no visit detail has to be opened.
SUPPORTED = ("com.tellus.evv.v2", "com.inmyteam.inmyteam",
             "com.hhaexchange.uma")

# Why each app is not supported, in a word a sentence can be built from.
# Empty, now that all three have been walked — kept because the next app
# added will arrive unwalked, and because "" is a worse answer than a name.
UNSUPPORTED_REASON: dict[str, str] = {}

# ENTRY ONLY, AND THE NAME OF THE FEATURE IS THE HONEST SCOPE: auto-ENTRY.
#
# No app's check-OUT control has been walked. Mobile Caregiver+ was only ever
# seen in its not-started and completed states, so the control that ends a
# running visit is unobserved; inMyTeam's is "Note & Check out", which opens
# the note and the signature flow — a screen where the caregiver signs, and
# not one a timer should be pressing on her behalf.
#
# So an armed visit's exit is DECIDED here and refused here, visibly, rather
# than being absent and looking like an oversight. She checks out herself,
# which is also the moment she is standing there with the patient anyway.
FIREABLE_KINDS = ("entry",)


def refusal(app: str, kind: str, block=None) -> str:
    """Why this cannot be fired, or "" if it can.

    One place, so the front end, the notifier and the runner all say the same
    thing about the same visit rather than three near-misses.

    `block` is optional and answers a question the app alone cannot: where a
    patient's evening is written as two consecutive entries, the check-in
    belongs to the FIRST and only the first. Said by the owner about exactly
    that patient — "we're only supposed to check in for caridad on her first
    block not the second one".

    The engine has always known this: `Visit.entry_at` is None on a later
    half, so `due()` never raises an entry there and nothing could have
    fired. What it did NOT do was say so — the arming page offered a switch
    on the second half whose only honest setting was off, and refused it, if
    at all, for an unrelated reason about the app. A switch that cannot do
    what it says is worse than a missing one.

    Checked BEFORE the app, because it is true of a fully walked app too.
    """
    if kind not in FIREABLE_KINDS:
        return "exit_is_hers"
    if block is not None and getattr(block, "part", 1) > 1:
        return "entry_is_the_first_half"
    if not supported(app):
        return UNSUPPORTED_REASON.get(app, "app_not_walked")
    return ""


def fireable(items) -> list:
    """The subset of `due()` this machine will actually press."""
    return [d for d in items
            if not refusal(d.visit.app, d.kind, d.visit.block)]


@dataclass(frozen=True)
class Due:
    """One thing to do, now, with the reason it is allowed."""

    visit: object                 # schedule.Visit
    kind: str                     # "entry" | "exit"
    when: datetime                # the moment it was due
    key: str                      # the block's arming key
    who: str                      # who attested to it
    attested_at: str              # when they did

    @property
    def occurrence(self) -> str:
        """What makes this one fire distinct from every other.

        The block, the DATE it falls on, and which half of the visit — so a
        Monday entry cannot spend Tuesday's, and an entry cannot spend an
        exit. The date comes off the due moment rather than off `now`: a fire
        at 00:01 for a visit due at 23:58 must still spend yesterday's slot.
        """
        return f"{self.key}:{self.when.date().isoformat()}:{self.kind}"


def _path() -> Path:
    """Resolved on the call so a test can move it."""
    from apt_log.ui.state import STATE_DIR

    return Path(STATE_DIR) / LEDGER_NAME


def spent() -> dict[str, dict]:
    """Occurrences already done, and what happened to each."""
    try:
        doc = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    done = doc.get("done")
    return {str(k): v for k, v in done.items()} if isinstance(done, dict) else {}


def is_spent(occurrence: str) -> bool:
    return occurrence in spent()


def spend(occurrence: str, outcome: str, detail: dict | None = None) -> None:
    """Write down that this occurrence happened, whatever the outcome.

    CALLED BEFORE THE PHONE IS TOUCHED, not after. A crash between pressing
    the button and recording it would otherwise leave the occurrence unspent
    and the next tick would press it again — and a double check-in is a
    corrupted record on somebody's timesheet, which is worse than a missed
    one. The outcome is amended afterwards; the claim on the slot is taken
    first.
    """
    doc: dict = {}
    try:
        doc = json.loads(_path().read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            doc = {}
    except (OSError, json.JSONDecodeError):
        doc = {}
    done = doc.get("done")
    if not isinstance(done, dict):
        done = {}
    entry = dict(done.get(occurrence) or {})
    entry.update(detail or {})
    entry["outcome"] = outcome
    entry["at"] = datetime.now().astimezone().isoformat()
    done[occurrence] = entry
    doc["done"] = _forget_old(done)
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
    except OSError as exc:
        log.warning("cannot record what fired (%s)", exc)


# The ledger's only job is to stop a repeat, and a repeat can only happen
# within a day of the original. Keeping a year of them would turn a file read
# on every tick into a growing one, and would hold a record of a caregiver's
# movements far longer than the thing it exists to prevent needs.
LEDGER_KEEPS = timedelta(days=8)


def _forget_old(done: dict) -> dict:
    cutoff = (datetime.now().astimezone() - LEDGER_KEEPS).date()
    kept = {}
    for occurrence, entry in done.items():
        try:
            when = Date.fromisoformat(occurrence.split(":")[1])
        except (IndexError, ValueError):
            kept[occurrence] = entry       # unreadable: keep, never guess
            continue
        if when >= cutoff:
            kept[occurrence] = entry
    return kept


def supported(app: str) -> bool:
    return app in SUPPORTED


def _armed_since(claim: dict) -> datetime | None:
    """When the switch was thrown, or None if the record does not say.

    None means the switch predates attestations being recorded, which makes
    it an OLD one — armed long enough ago that yesterday's visit really was
    its responsibility. So an unknown time reports as normal rather than
    suppressing; the filter exists for the switch thrown minutes ago, not for
    the one thrown last month.
    """
    raw = str((claim or {}).get("at") or "")
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo else when.astimezone()


def _days_to_look_at(schedule, now: datetime) -> tuple:
    """Which calendar days could hold a visit that matters at `now`.

    IN THE SCHEDULE'S OWN ZONE, NEVER THE MACHINE'S. Comparing two aware
    instants is correct whatever clock the controller keeps — that part was
    never at risk — but `now.date()` is not an instant, it is a calendar day,
    and which day it names depends on the zone it is read in. A controller
    running UTC would call 9pm Eastern "tomorrow" and look for a visit on the
    wrong page of the calendar for four hours out of every day.

    The Pi is on America/New_York today and NTP-synced, so this changes
    nothing right now. It means a reimaged Pi that comes up UTC — the default
    on a fresh Raspberry Pi OS — fires correctly anyway instead of failing
    silently between 8pm and midnight.

    Yesterday as well as today, because a visit due at 23:58 is still inside
    its grace window at 00:01.
    """
    local = now.astimezone(schedule.zone)
    return (local.date() - timedelta(days=1), local.date())


def due(schedule, now: datetime, armed_keys=None,
        attestations=None) -> list[Due]:
    """Everything armed whose moment has arrived and has not been spent.

    Sorted by when it was due, so a backlog is worked oldest first — if two
    are somehow waiting, the earlier one is the one whose window closes first.
    """
    from apt_log import arming

    keys = arming.armed() if armed_keys is None else set(armed_keys)
    claims = arming.attestations() if attestations is None else attestations
    if not keys:
        return []

    out: list[Due] = []
    for day in _days_to_look_at(schedule, now):
        for visit in schedule.on(day):
            key = arming.key_for(visit.block)
            if key not in keys:
                continue
            claim = claims.get(key) or {}
            for kind, moment in (("entry", visit.entry_at),
                                 ("exit", visit.exit_at)):
                if moment is None or moment > now or now - moment > GRACE:
                    continue
                item = Due(visit=visit, kind=kind, when=moment, key=key,
                           who=str(claim.get("who") or "unknown"),
                           attested_at=str(claim.get("at") or ""))
                if is_spent(item.occurrence):
                    continue
                out.append(item)
    out.sort(key=lambda d: d.when)
    return out


def missed(schedule, now: datetime, armed_keys=None,
           attestations=None) -> list[Due]:
    """Armed occurrences whose window closed with nothing recorded.

    Not a fire — the opposite. This is what a person needs told, because the
    entry now has to be made by hand and the honest arrival minute is already
    in the past.

    NOTHING BEFORE THE SWITCH WAS THROWN. Watched live: arming Carmen's
    weekday block at 00:46 immediately announced YESTERDAY's 05:00 as missed
    and alerted a phone about it. It was not missed — nobody had armed it, so
    the machine was never going to do it, and saying otherwise turns the one
    notice that means "go and do this by hand" into noise. A visit is only
    missable from the moment somebody took responsibility for it.
    """
    from apt_log import arming

    keys = arming.armed() if armed_keys is None else set(armed_keys)
    claims = arming.attestations() if attestations is None else attestations
    if not keys:
        return []
    out: list[Due] = []
    for day in _days_to_look_at(schedule, now):
        for visit in schedule.on(day):
            key = arming.key_for(visit.block)
            if key not in keys:
                continue
            since = _armed_since(claims.get(key) or {})
            for kind, moment in (("entry", visit.entry_at),
                                 ("exit", visit.exit_at)):
                if moment is None or now - moment <= GRACE:
                    continue
                if since is not None and moment < since:
                    continue        # nobody had armed it when it came round
                item = Due(visit=visit, kind=kind, when=moment, key=key,
                           who="", attested_at="")
                if is_spent(item.occurrence):
                    continue
                out.append(item)
    out.sort(key=lambda d: d.when)
    return out


def on_duty(schedule, now: datetime, app: str = "", armed_keys=None) -> bool:
    """Whether an armed visit is relying on an app right now.

    THE SECOND REASON A DEAD SESSION MAY BE REVIVED.

    Automatic sign-in is otherwise gated on somebody watching the portal, and
    that gate is right for what it was built for: an unwatched phone must not
    loop sign-in against its own inactivity timer all night. But it answers
    "is anybody looking", and the question that actually matters at half past
    four in the morning is "is anything about to need this app". Those come
    apart exactly when it hurts — the session expires unattended overnight,
    the phone stands on the sign-in page, and at the visit the entry fires
    into a signed-out app.

    Arming is what closes the gap, and it closes it honestly. It is already
    an attestation by a person that the caregiver is with the patient
    (REQ-5.9) — so reviving the app that visit runs in is inside what
    somebody has already asked for, not a new liberty the machine is taking.

    The window runs from ARMS to the end of the visit plus GRACE: the lead
    walk needs the app, the fire needs it, and an expiry MID-visit still has
    a check-out to make. Outside that window nothing here says yes, so the
    overnight loop the gate was built to prevent stays prevented.

    `app` narrows it to one package's visits; empty asks about any.
    """
    from apt_log import arming

    keys = arming.armed() if armed_keys is None else set(armed_keys)
    if not keys:
        return False
    for day in _days_to_look_at(schedule, now):
        for visit in schedule.on(day):
            if app and visit.app != app:
                continue
            if arming.key_for(visit.block) not in keys:
                continue
            if visit.arms <= now <= visit.ends + GRACE:
                return True
    return False


def preparing(schedule, now: datetime, armed_keys=None) -> list[Due]:
    """Armed visits inside their lead window that have not fired yet.

    THE ANSWER TO inMyTeam BEING UNARMABLE THE NIGHT BEFORE. That app draws
    "Check in" only on the scheduled day — a walk the evening before finds
    "This visit is not scheduled for today" and no control at all, so there is
    nothing to get ready against.

    Arming is not that walk, though. Arming is a standing decision about a
    recurring block, made whenever she likes; the WALK belongs on the day, in
    the lead window, which is what this returns. By the time the fire is due
    the app is open, signed in, and on the patient's own detail — so the fire
    is one press rather than a cold start, a login and a search against the
    clock.

    That helps every app. It is the only thing that makes inMyTeam work at all.
    """
    from apt_log import arming

    keys = arming.armed() if armed_keys is None else set(armed_keys)
    if not keys:
        return []
    out: list[Due] = []
    for visit in schedule.on(now.astimezone(schedule.zone).date()):
        key = arming.key_for(visit.block)
        if key not in keys or visit.entry_at is None:
            continue
        if not (visit.arms <= now < visit.entry_at):
            continue
        item = Due(visit=visit, kind="entry", when=visit.entry_at, key=key,
                   who="", attested_at="")
        if is_spent(item.occurrence):
            continue
        out.append(item)
    out.sort(key=lambda d: d.when)
    return out
