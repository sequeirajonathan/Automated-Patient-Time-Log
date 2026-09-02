"""Which visit is open, so the signature pad it leads to knows whose it is.

WHY THIS EXISTS, and why the obvious answers do not work.

A signature belongs to the visit whose check-out raised it. Naming the wrong
person on that pad is the worst failure this feature has — one person's
signature under another's heading, on a record an auditor reads — and it has
happened once already, from a version that asked the CLOCK ("the visit
running now, else the next one up"). At 18:37, with a patient's check-out
sheet open, her visit had ended, so "next" was somebody else's. Reported
immediately: "Wrong patient. This is not Marina."

The rule that replaced it — read the name off the screen — is right and does
not go away here. The trouble is that on Mobile Caregiver+ the screen never
says it at the moment it matters. Read off the live phone:

  * The pad is an AlertDialog, and the published tree's whole extent is that
    dialog: eleven nodes, three of them buttons, and no name anywhere.
  * The visit detail behind it carries the service code, the address and the
    phone — no name either (see docs/EVV_FLOWS.md, walked twice).
  * The only page that names anybody is the WEEK LIST, and it names every
    patient of the day at once, one sentence per row:
        "La visita está programada para <PATIENT> en <día> … y su estado es …"

So the last page that could identify the signer names three people, and the
question "which of the three" has exactly one honest answer available: THE
ROW SHE OPENED. Choosing a visit is an act, it happens through the portal,
and this module is the memory of it.

WHAT IT WILL NOT DO. It never guesses from the clock, it never falls back to
"the only patient left", and it goes empty the moment anything makes the
answer doubtful:

  * a page listing several patients is published — she is back at the list,
    choosing again, so whatever was open is not open any more;
  * the app in front changes;
  * the reading gets old (`LATCH_SECONDS`);
  * the schedule says that patient's visit cannot plausibly be in hand — a
    veto only, and one that can remove a name but never invent one.

Empty is a real answer and the safe one: the pad then says which ROLE it is
waiting for, offers that side's parties and names nobody, which is what it
did before any of this existed.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

NAME = "visit-open.json"

# How long a reading goes on meaning this signature. A pad is reached seconds
# after the visit it belongs to is opened, not half an hour later; the window
# is this wide only so a check-out interrupted by a door, a phone call or a
# patient who needs helping to the table still knows whose it is.
LATCH_SECONDS = 20 * 60

# How far outside its own scheduled window a visit may still be the one in
# hand. Generous after the end, because running late is the ordinary case and
# a check-out is the last thing that happens; tight before the start, because
# a signature collected an hour early is a different kind of mistake.
LATE_GRACE = timedelta(hours=3)
EARLY_GRACE = timedelta(hours=1)

# How long a fresh record is protected from being cleared by a page arriving
# late. See `note_screen`.
SETTLE_SECONDS = 5.0

# Owner-only, like the signature store beside it: this names a patient and
# says which visit she is in the middle of.
FILE_MODE = 0o600


def _fold(text: str) -> str:
    return " ".join((text or "").casefold().split())


def named_in(text: str, patients) -> str:
    """The one scheduled patient this text names, or "".

    Two names is the same as none — that is the week list, and the week list
    is precisely the page that cannot answer the question.
    """
    folded = _fold(text)
    if not folded:
        return ""
    seen = {p for p in patients if p and _fold(p) in folded}
    return seen.pop() if len(seen) == 1 else ""


def row_words(doc: dict, element: dict) -> str:
    """Everything the tapped row says, including what it does not carry.

    Mobile Caregiver+'s visit rows publish NO text of their own: `txt` is
    empty on every `visits_event<D>_<N>`, and the sentence naming the patient
    arrives as a separate static with the SAME BOUNDS as the row. Read off
    the live phone:

        E visits_event0_1 [0, 309, 1080, 380]
        S [0, 309, 1080, 380] "La visita está programada para …"

    So a rule that looked only at the element found nothing, and one that
    looked only at the page found all three patients. The row's own box is
    what separates them.
    """
    box = element.get("b") or []
    words = [str(element.get("txt") or "")]
    words.extend(str(line) for line in (element.get("lines") or []))
    if len(box) == 4:
        x1, y1, x2, y2 = box
        for static in doc.get("statics") or []:
            b = static.get("b") or []
            if len(b) != 4:
                continue
            if b[0] >= x1 and b[1] >= y1 and b[2] <= x2 and b[3] <= y2:
                words.append(str(static.get("txt") or ""))
    return " ".join(w for w in words if w)


# THE APP'S OWN STATE MACHINE, in its own words.
#
# Every row of Mobile Caregiver+'s week list ends with `y su estado es <STATE>`,
# and the states are machine-readable. Read off the live phone at 21:35 on
# 1 Sep, with one check-out half finished:
#
#   ATANASIO … de 9:05 AM a 11:05 AM y su estado es Completada
#   MARINA   … de 3:20 PM a 5:20 PM  y su estado es Completada
#   ONORINA  … de 6:00 PM a 8:00 PM  y su estado es En Progreso, Tarde
#
# So the app says outright which visit is open, and that beats every other
# signal here: it needs no tap, it survives a portal restart, it corrects
# itself every time the list is on screen, and it cannot be confused by a
# person navigating on the phone instead of through the portal.
#
# "En Progreso" is absent from the vocabulary in docs/EVV_FLOWS.md for an
# honest reason — the discovery walk never had a running visit to look at.
# The other apps' words are here too; a word this does not know costs the
# fallback, never a wrong name.
IN_PROGRESS_WORDS = ("en progreso", "en curso", "in progress",
                     "iniciada", "comenzada", "started", "visita iniciada")


def running_row(doc: dict, patients) -> str:
    """The patient whose visit this page marks as IN PROGRESS, or "".

    Per ROW, not per page: the page carries three sentences and only one of
    them says in progress, so a rule that folded the page into one string
    would find every patient and no state.

    Two rows claiming it is the same as none — that is a screen this code
    does not understand, and understanding it wrongly is how a signature
    lands on the wrong record.
    """
    found = set()
    for words in _row_sentences(doc):
        low = _fold(words)
        if not any(mark in low for mark in IN_PROGRESS_WORDS):
            continue
        name = named_in(words, patients)
        if name:
            found.add(name)
    return found.pop() if len(found) == 1 else ""


def _row_sentences(doc: dict) -> list[str]:
    """Each line of the page on its own, statics and controls alike."""
    out = [str(s.get("txt") or "") for s in (doc.get("statics") or [])]
    for element in doc.get("elements") or []:
        out.append(str(element.get("txt") or ""))
        out.extend(str(line) for line in (element.get("lines") or []))
    return [line for line in out if line]


def page_names(doc: dict, patients) -> list[str]:
    """Every scheduled patient this whole page names.

    Both halves of the document: the apps put a name in a static on one page
    and in a control's own caption on the next, and a rule that reads one of
    them is a rule that works on some screens.
    """
    words = " ".join(
        [str(s.get("txt") or "") for s in (doc.get("statics") or [])]
        + [str(e.get("txt") or "") for e in (doc.get("elements") or [])]
        + [str(line) for e in (doc.get("elements") or [])
           for line in (e.get("lines") or [])])
    folded = _fold(words)
    return [p for p in patients if p and _fold(p) in folded]


# ------------------------------------------------------------------ the store
def _path(path: Path | None) -> Path:
    if path is not None:
        return path
    from apt_log.ui import state as state_mod

    return state_mod.STATE_DIR / NAME


def open_visit(package: str, path: Path | None = None) -> dict:
    """What is on record, as written. No freshness rules applied."""
    try:
        doc = json.loads(_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(doc, dict) or doc.get("app") != package:
        return {}
    return doc


def remember(name: str, package: str, path: Path | None = None,
             why: str = "opened") -> None:
    """Record that this patient's visit is the one open on this app.

    `why` says which signal wrote it — "in_progress" where the app said so
    itself, "opened" where it is the row she pressed. Kept because the two
    are not equally strong and because a record nobody can account for is a
    record nobody can debug.
    """
    target = _path(path)
    doc = {"name": name, "app": package, "at": time.time(), "why": why}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".otmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("cannot record the open visit (%s)", exc)


def forget(path: Path | None = None) -> None:
    """No visit is open. Called the moment the answer becomes doubtful."""
    try:
        _path(path).unlink()
    except OSError:
        pass


# ------------------------------------------------------------ writing it down
def note_tap(doc: dict, element: dict, path: Path | None = None) -> str:
    """Remember the visit this tap opens, and return the name or "".

    THE TAP IS THE CHOOSING, and it is the only unambiguous signal on this
    app: the week list names three people and the page after it names none,
    so the difference between them is which row she pressed.
    """
    package = str(doc.get("app") or "")
    if not package:
        return ""
    name = named_in(row_words(doc, element), _scheduled_for(package))
    if name:
        remember(name, package, path)
    return name


def note_screen(doc: dict, path: Path | None = None) -> None:
    """Keep the record honest as the phone moves.

    A page naming several patients is the list, and being back at the list
    means the visit that was open is not open any more — so the record goes,
    rather than surviving into somebody else's pad. A page naming exactly one
    is that patient's own, and it is allowed to say so: on the other two apps
    the visit detail does name her, and there the record needs no tap at all.
    """
    package = str(doc.get("app") or "")
    if not package:
        return
    patients = _scheduled_for(package)
    # THE APP'S OWN ANSWER FIRST, wherever it gives one. A row that says it
    # is in progress is the visit in hand, said by the only party that
    # actually knows — and it re-establishes itself every time the list is
    # on screen, so a person navigating on the phone rather than through the
    # portal cannot leave this pointing at somebody else.
    running = running_row(doc, patients)
    if running:
        remember(running, package, path, why="in_progress")
        return
    found = page_names(doc, patients)
    if len(found) > 1:
        # NOT A RECORD THAT WAS JUST WRITTEN. Every viewer runs its own copy
        # of the screen loop, and one that connects a moment after the tap
        # meets the list page for the first time and would call this — the
        # tap's answer erased by a page it had already left. A few seconds is
        # far longer than that overlap and far shorter than a visit.
        recent = open_visit(package, path)
        if time.time() - float(recent.get("at") or 0) > SETTLE_SECONDS:
            forget(path)
    elif len(found) == 1:
        remember(found[0], package, path)


# -------------------------------------------------------------- reading it back
def current(package: str, now: datetime | None = None,
            path: Path | None = None) -> str:
    """The patient whose visit is open on this app, or "".

    Every guard here can only take a name away.
    """
    doc = open_visit(package, path)
    name = str(doc.get("name") or "")
    if not name:
        return ""
    if time.time() - float(doc.get("at") or 0) > LATCH_SECONDS:
        return ""
    return name if _plausible(name, package, now) else ""


def _scheduled_for(package: str) -> list[str]:
    from apt_log import schedule as schedule_mod

    try:
        plan = schedule_mod.load()
    except Exception:  # noqa: BLE001
        return []
    return [b.patient for b in getattr(plan, "blocks", []) or []
            if b.patient and (not package or b.app == package)]


def _plausible(name: str, package: str, now: datetime | None) -> bool:
    """Whether the schedule can account for this patient being in hand.

    THE CLOCK GETS A VETO AND NOTHING MORE. It does not choose the patient —
    that is the mistake this whole module is written around — but a name from
    a reading taken while a different visit was on screen is one the schedule
    can often rule out, and ruling it out costs only a heading.

    A schedule that cannot be read, or that has nothing for this patient
    today, is not evidence against her: the answer then stays yes, because
    the name came from the screen and this is only a veto.
    """
    from apt_log import schedule as schedule_mod

    try:
        plan = schedule_mod.load()
        moment = now or datetime.now(plan.zone)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=plan.zone)
        day = moment.astimezone(plan.zone).date()
        windows = [v for offset in (0, -1)
                   for v in plan.on(day + timedelta(days=offset))
                   if _fold(v.patient) == _fold(name)
                   and (not package or v.app == package)]
    except Exception:  # noqa: BLE001
        return True
    if not windows:
        return True
    return any(v.starts - EARLY_GRACE <= moment <= v.ends + LATE_GRACE
               for v in windows)
