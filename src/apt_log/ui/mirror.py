"""What the controller is looking at right now, mirrored to her page.

The relay asks her a question; the mirror is how she can tell what the question
is *about*. A prompt reading "choose a verification method" with no picture of
the screen behind it asks her to answer for a machine she cannot see.

Two rules shape this file.

**The mirror carries keys, not text.** Screen and step are members of a fixed
vocabulary, translated on the page. Nothing free-form crosses from the device
into this file, so the app's own labels — which on the visit screen include a
patient's name in the header — cannot arrive here by accident. That failure has
already happened once in this project through a label that was safe on every
other screen, and an allow-list is the version of the rule that does not depend
on remembering.

**A stale mirror says so.** The screen it shows is a still from some moment in
the past, and if the agent stopped publishing, the last frame stays on disk
looking current. `Mirror.stale` is the difference between "the phone is on the
verification chooser" and "the phone was on the verification chooser, two
minutes ago, and nothing has been heard since".
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

STATE_DIR = Path("/var/lib/aptlog")
MIRROR_PATH = STATE_DIR / "mirror.json"

STALE_AFTER = timedelta(minutes=2)

# Where the controller is. One key per page object; ".unknown" is a real answer
# and better than a guess.
SCREENS = (
    "startup", "language", "permissions", "login", "agency", "home",
    "today", "visit", "verification", "signature", "unknown",
)

# What it is doing there. "waiting" is the one that concerns her — it means the
# controller has stopped and the relay is asking.
STEPS = ("idle", "working", "waiting", "blocked", "done", "unknown")

# Identifiers only. REQUIREMENTS §5: a name never reaches the record, and this
# file is part of the record's surface.
_ID_SHAPE = re.compile(r"\A[A-Za-z0-9_.:-]{1,32}\Z")
UNKNOWN_ID = "?"


@dataclass
class Mirror:
    at: datetime | None = None
    screen: str = "unknown"
    step: str = "unknown"
    patient_id: str = UNKNOWN_ID
    scheduled: datetime | None = None

    @property
    def stale(self) -> bool:
        """True when this frame is too old to describe the present."""
        if self.at is None:
            return True
        return datetime.now() - self.at > STALE_AFTER

    @property
    def waiting(self) -> bool:
        return self.step == "waiting"


def _safe_id(patient_id: str) -> str:
    """Refuse anything that is not identifier-shaped.

    A name has a space in it and fails this, which is the point: the guard has
    to hold against a caller that passes the wrong variable, not only against
    one that means well.
    """
    candidate = (patient_id or "").strip()
    if candidate == UNKNOWN_ID:
        # The sentinel is a valid answer, not a rejected value. It fails the id
        # shape by design, and treating that as a refusal logged an error on
        # every frame the feed published -- five seconds apart, forever, drowning
        # the refusals that actually mean something.
        return UNKNOWN_ID
    if not _ID_SHAPE.match(candidate):
        if candidate:
            log.error("mirror: refusing a patient identifier that is not id-shaped")
        return UNKNOWN_ID
    return candidate


def _member(value: str, allowed: tuple[str, ...], what: str) -> str:
    if value in allowed:
        return value
    log.error("mirror: unknown %s %r — publishing 'unknown'", what, value)
    return "unknown"


def publish(
    screen: str,
    step: str,
    patient_id: str = UNKNOWN_ID,
    scheduled: datetime | None = None,
    path: Path | None = None,
) -> Mirror:
    """Record what the controller sees. Called by the agent, read by the page.

    Written through a temporary file and renamed, so the page never reads half a
    frame — it polls this on a timer and would otherwise hit a truncated file
    eventually.
    """
    target = path or MIRROR_PATH
    frame = Mirror(
        at=datetime.now(),
        screen=_member(screen, SCREENS, "screen"),
        step=_member(step, STEPS, "step"),
        patient_id=_safe_id(patient_id),
        scheduled=scheduled,
    )

    payload = {
        "at": frame.at.isoformat(),
        "screen": frame.screen,
        "step": frame.step,
        "patient_id": frame.patient_id,
        "scheduled": scheduled.isoformat() if scheduled else None,
    }

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        # The mirror going dark must not stop a visit being recorded. She loses
        # the picture, not the record.
        log.warning("mirror: cannot publish (%s)", exc)
    return frame


def read(path: Path | None = None) -> Mirror:
    """The last published frame, or an empty one. Never raises."""
    target = path or MIRROR_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Mirror()

    def _dt(key):
        try:
            raw = payload.get(key)
            return datetime.fromisoformat(raw) if raw else None
        except (TypeError, ValueError):
            return None

    return Mirror(
        at=_dt("at"),
        # Re-checked on the way in, not only on the way out: this file lives on
        # disk between two processes, and the reader should not trust it more
        # than the writer did.
        screen=_member(str(payload.get("screen", "")), SCREENS, "screen"),
        step=_member(str(payload.get("step", "")), STEPS, "step"),
        patient_id=_safe_id(str(payload.get("patient_id", ""))),
        scheduled=_dt("scheduled"),
    )
