"""Which visits the machine is allowed to act on, and which it is not.

The schedule says when things happen. This says whether anything is supposed
to happen about them, and the answer for every visit is NO until a person says
otherwise. That default is the whole design: a scheduler whose switches
default to on is a scheduler that starts doing things the first time somebody
edits a file, and the things it would do here are EVV records asserting that a
caregiver was at a patient's home.

WHAT ARMING MEANS TODAY. Nothing fires yet — the arm-and-fire macros are not
built, and there is a question in front of them that is not this module's to
answer (see docs/EVV_FLOWS.md). Arming records intent, and the control page
says so plainly rather than implying a capability that does not exist. When
firing does land, this is the gate it reads, and a switch somebody has already
set will already mean what they meant by it.

KEYED BY THE BLOCK, NOT THE OCCURRENCE. "Arm this patient's Monday morning
visit" is a standing decision about a recurring thing, not a decision about
the fourteenth of March. So the key is derived from what makes a block itself
— who, which app, which days, which hours, which of a split pair — and it
survives a week rolling over.

The key is a HASH of those, not the words. Two reasons and both matter: a
patient's name would otherwise end up in form values, page markup and server
logs, which is exactly the spread this project keeps refusing; and a stable
short token is what a form field wants anyway.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Beside the other things the machine remembers about itself rather than in
# /etc: this is state a person changes from the portal, not configuration
# somebody edits by hand.
ARMED_NAME = "armed.json"


def _path() -> Path:
    """Resolved on the call so a test can move it — the lesson from every
    other state file in this project, learned by deleting /var/lib/aptlog and
    watching what grew back."""
    from apt_log.ui.state import STATE_DIR

    return Path(STATE_DIR) / ARMED_NAME


def key_for(block) -> str:
    """A stable short token for one recurring block.

    Derived from the block's own identity, so editing an unrelated visit does
    not silently re-key this one and disarm it. Hashed so no patient's name
    reaches a form value or a log line.
    """
    identity = "|".join((
        block.patient,
        block.app,
        block.agency,
        ",".join(str(d) for d in sorted(block.days)),
        block.start.isoformat(),
        block.end.isoformat(),
        f"{block.part}/{block.of}",
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


def armed() -> set[str]:
    """The keys somebody has switched on. Empty is the shipped state."""
    try:
        doc = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    keys = doc.get("armed")
    if not isinstance(keys, list):
        return set()
    return {str(k) for k in keys}


def is_armed(block) -> bool:
    return key_for(block) in armed()


def set_armed(key: str, on: bool) -> bool:
    """Switch one block on or off. Returns what it is now.

    Writes the whole set every time rather than appending: the file is a
    dozen short strings, and a partial write on a power cut leaving half a
    decision behind is not a trade worth making for a schedule this size.
    """
    keys = armed()
    if on:
        keys.add(key)
    else:
        keys.discard(key)
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"armed": sorted(keys)}), encoding="utf-8")
    except OSError as exc:
        log.warning("cannot record what is armed (%s)", exc)
        # Reporting the state we FAILED to reach would be a switch that looks
        # thrown and is not.
        return key in armed()
    return on


def disarm_all() -> None:
    """Everything off. The state this ships in, and the way back to it."""
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"armed": []}), encoding="utf-8")
    except OSError as exc:
        log.warning("cannot disarm (%s)", exc)
