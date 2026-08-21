"""Which visits the machine is allowed to act on, and which it is not.

The schedule says when things happen. This says whether anything is supposed
to happen about them, and the answer for every visit is NO until a person says
otherwise. That default is the whole design: a scheduler whose switches
default to on is a scheduler that starts doing things the first time somebody
edits a file, and the things it would do here are EVV records asserting that a
caregiver was at a patient's home.

WHAT ARMING MEANS. It is an ATTESTATION OF PRESENCE, and that is a stronger
thing than a preference. The owner settled it (REQ-5.9):

    "If armed you can assume my sister is already taking care of the patient.
    ... She's already there at that time, she just doesn't have the time to do
    the entry because she has to start right away. Arming is a commitment."

The presence gate REQ-5 describes cannot do this job in this deployment: the
Pi does not travel, so its network anchors attest to the caregiver's own house
identically for every patient on the round, and would return the same strong
PASS whichever address she was at. So the claim comes from a person instead,
in advance, and the act that makes it is throwing this switch.

Which is why WHO AND WHEN ARE RECORDED ALONGSIDE THE KEY. A record whose
presence was attested by a human must never be indistinguishable from one
whose presence was observed by a machine — that is the whole reason the trade
is acceptable. The switch is not just on; it is on because somebody said so,
at a time, and that travels with the fire into the audit entry.

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


def _read() -> dict:
    """The file, or an empty one. Never raises — see `armed()`."""
    try:
        doc = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def armed() -> set[str]:
    """The keys somebody has switched on. Empty is the shipped state."""
    keys = _read().get("armed")
    if not isinstance(keys, list):
        return set()
    return {str(k) for k in keys}


def attestations() -> dict[str, dict]:
    """Who armed each key, and when.

    Read separately from the key set so a file written before this existed
    still disarms and arms correctly — an armed key with no attestation is a
    switch somebody threw, and the missing half is recorded as unknown rather
    than treated as unarmed.
    """
    by_key = _read().get("by")
    if not isinstance(by_key, dict):
        return {}
    return {str(k): v for k, v in by_key.items() if isinstance(v, dict)}


def attestation(key: str) -> dict:
    """The one for this key: `{"who": ..., "at": ...}`, or empty."""
    return attestations().get(key, {})


def is_armed(block) -> bool:
    return key_for(block) in armed()


def set_armed(key: str, on: bool, who: str = "") -> bool:
    """Switch one block on or off. Returns what it is now.

    `who` names the person making the presence claim (REQ-5.9). It is stored
    beside the key and travels into the audit entry when the visit fires, so a
    reader can tell an attested record from an observed one. Arming without a
    name is still arming — the switch is what matters and a missing name must
    never silently mean "not armed" — but it records the gap as such.

    Writes the whole document every time rather than appending: the file is a
    dozen short strings, and a partial write on a power cut leaving half a
    decision behind is not a trade worth making for a schedule this size.
    """
    from datetime import datetime

    doc = _read()
    keys = armed()
    by_key = attestations()
    if on:
        keys.add(key)
        by_key[key] = {"who": who or "unknown",
                       "at": datetime.now().astimezone().isoformat()}
    else:
        keys.discard(key)
        # The claim goes with the switch. A stale attestation left behind
        # would let a re-arm inherit somebody else's name and minute.
        by_key.pop(key, None)
    doc["armed"] = sorted(keys)
    doc["by"] = by_key
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
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
        path.write_text(json.dumps({"armed": [], "by": {}}), encoding="utf-8")
    except OSError as exc:
        log.warning("cannot disarm (%s)", exc)
