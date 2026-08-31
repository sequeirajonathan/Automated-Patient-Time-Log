"""A nudge five minutes before a patient's visit starts.

WHAT IT IS FOR, in the owner's words: "what we do need is a 5 minute
reminder before another patient starts on the schedule ... patient name,
reminder message with time left, all in eastern time."

So it is not about the machine. Arming decides whether the CONTROLLER acts;
this is for the person, and every visit on the schedule gets one whether or
not anything is armed against it.

**Web Push only, and that is a privacy decision rather than a convenience
one.** This notice names a patient. The relay (`notify`/`alert.sh`) is a
public topic — anyone who knows it can read what is posted there — which is
why nothing carrying a patient, a visit or a code has ever gone to it. Web
Push is encrypted per subscription and reaches only the phones that asked,
so the name is safe there and nowhere else. `notify` is deliberately not
imported by this module.

**In the schedule's own zone, always.** "All in eastern time" is not a
formatting preference: the controller may be running UTC (the default on a
fresh Raspberry Pi OS), and a reminder that says the wrong hour is worse
than no reminder. Every clock here is rendered through `schedule.zone`.

**Once per visit, recorded on disk.** The tick runs every few seconds and
the window is minutes wide, so without a ledger she would get the same
notice thirty times. Keyed by the block's HASH and the date — the same key
arming uses — so no patient's name reaches this file either.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# How long before the visit she is told. The owner asked for five minutes:
# long enough to put a cup down and pick the phone up, short enough that it
# is still about this visit and not a fact about the day.
LEAD = timedelta(minutes=5)

# Beside the other things the machine remembers about itself.
LEDGER_NAME = "reminded.json"

# A reminder is only worth sending while it is still ahead of her. Past the
# start it is not a reminder, it is a note about something she is already
# doing — and the schedule view says that better.
KEEPS = timedelta(days=3)


def _path() -> Path:
    """Resolved on the call so a test can move it — the same lesson every
    other state file in this project learned the hard way."""
    from apt_log.ui.state import STATE_DIR

    return Path(STATE_DIR) / LEDGER_NAME


def _read() -> dict:
    try:
        doc = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return doc.get("done", {}) if isinstance(doc, dict) else {}


def _write(done: dict) -> None:
    target = _path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({"done": done}), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("cannot record a reminder (%s)", exc)


def _forget_old(done: dict, now: float) -> dict:
    """Yesterday's reminders are not worth remembering. Kept for a few days
    only so a clock that steps backwards cannot resend a week of them."""
    keep = KEEPS.total_seconds()
    return {k: v for k, v in done.items()
            if isinstance(v, (int, float)) and 0 <= now - v <= keep}


def occurrence(visit, zone) -> str:
    """What makes one visit's reminder distinct from every other.

    The block's own hash and the date the visit FALLS ON — off the visit's
    own start, never off `now`, for the reason the fire ledger gives: a
    reminder sent at 00:01 for a visit at 00:03 must not be filed under the
    wrong day. Hashed, so no patient's name reaches a state file or a log.
    """
    from apt_log import arming

    day = visit.starts.astimezone(zone).date()
    return f"{arming.key_for(visit.block)}:{day.isoformat()}"


def already_sent(key: str) -> bool:
    return key in _read()


def mark(key: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    done = _forget_old(_read(), now)
    done[key] = now
    _write(done)


def _clock(when: datetime) -> str:
    """The hour as Spanish writes it: "5:00 a. m.", not "05:00AM".

    Built by hand rather than with %p, whose output is the C library's and is
    "AM" on this machine whatever the locale is set to.
    """
    hour = when.hour % 12 or 12
    half = "a. m." if when.hour < 12 else "p. m."
    return f"{hour}:{when.minute:02d} {half}"


def words(visit, now: datetime) -> tuple[str, str]:
    """The notice: (title, body).

    THE PATIENT'S NAME IS THE TITLE. Asked for that way — "first in the
    notification patient name" — and it is right: on a lock screen the first
    line is what gets read, and which patient is the thing she needs to know
    before she needs anything else.

    The minutes are COUNTED, not assumed to be five. A tick that lands late,
    or a controller that was asleep at the five-minute mark, would otherwise
    say "5 min" with two to go.
    """
    left = max(0, round((visit.starts - now).total_seconds() / 60.0))
    when = _clock(visit.starts)
    if left <= 0:
        return visit.patient, f"Comienza ahora · {when}"
    unit = "minuto" if left == 1 else "minutos"
    return visit.patient, f"Comienza en {left} {unit} · {when}"


def due(schedule, now: datetime) -> list:
    """Visits whose reminder moment has arrived and has not been sent.

    The window opens LEAD before the visit and shuts when it starts. Wide
    rather than an instant, so a controller that was busy for a minute still
    sends one — late by a minute is a reminder, and a tick-exact match would
    simply miss.
    """
    local = now.astimezone(schedule.zone)
    out = []
    # Yesterday as well, for a visit just after midnight whose window opened
    # on the day before.
    for offset in (-1, 0):
        day = local.date() + timedelta(days=offset)
        for visit in schedule.on(day):
            if not (visit.starts - LEAD <= now < visit.starts):
                continue
            if already_sent(occurrence(visit, schedule.zone)):
                continue
            out.append(visit)
    out.sort(key=lambda v: v.starts)
    return out


def send(schedule, now: datetime) -> int:
    """Push the reminders that are due. Returns how many were sent.

    Never raises: this runs on the controller's tick, beside the things that
    actually write EVV records, and a notification that cannot be delivered
    must not be able to stop them.
    """
    sent = 0
    try:
        from apt_log import push
    except Exception as exc:  # noqa: BLE001
        log.debug("no push channel for reminders (%s)", exc)
        return 0
    for visit in due(schedule, now):
        title, body = words(visit, now)
        key = occurrence(visit, schedule.zone)
        try:
            # A tag PER VISIT, so the six o'clock reminder does not replace
            # the five o'clock one on a lock screen she has not looked at.
            push.send(title, body, url="/app", tag=f"visit-{key[:12]}")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not push a visit reminder (%s)", exc)
            continue
        # Marked even if nobody is subscribed: the question this answers is
        # "has this visit been announced", not "did a phone light up".
        mark(key)
        sent += 1
        log.info("reminded about a visit starting at %s",
                 visit.starts.strftime("%H:%M"))
    return sent
