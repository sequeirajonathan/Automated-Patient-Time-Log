"""A signature adopted once, applied by its owner with one press.

WHY THIS EXISTS, AND WHAT CHANGED TO ALLOW IT.

REQ-10.6 forbade keeping a signature. The reasoning was sound and is worth
restating, because this module is the exception to it and an exception nobody
can explain is just a hole: a stored signature replayed by a timer attests to
a visit nobody witnessed at that moment, and on a record that gets billed that
is a falsified attestation, with the caregiver's name on it.

What changed is not the storage. It is the moment.

Several of these patients cannot hold a stylus. They were asking the caregiver
to sign for them, which is the very thing REQ-10.6 was written to prevent and
was already happening by hand, off the record, with no trail at all. The
agency's answer — approved, and written up by the owner as an amendment to
REQ-10.6 — is the one every e-signature system reaches: a party adopts a
signature once, in person, and afterwards applies it with a single deliberate
press. Presence and intent are what make an attestation, not the freshness of
the strokes.

So the rules this module keeps are the ones that carry that argument, and they
are not negotiable inside it:

**A press, always, by the person it belongs to.** Nothing here is reachable
from the scheduler, from a macro, or from any timer. `autoentry` cannot see
this module and a test says so. Check-out does not fire on any app and this
does not change that — it makes the signature screen a one-press screen for
two people standing in front of it, and nothing else.

**The strokes never leave this machine.** No route answers them, in any shape.
The portal asks "apply Carmen's signature" by name; the lookup and the replay
both happen on the Pi. A stolen portal session can ask for a signature to be
drawn on the phone in the room; it cannot walk away with the signature.

**Enrolment is dated and recorded.** Every adoption keeps the date it was
made, and every application afterwards keeps its own.

This condition used to demand more: a typed sentence naming who was present,
refused if it was missing. The owner of the requirement removed it — the field
asked, in front of a patient, a question whose answer was always the same two
people, and a box somebody fills in the same way every time is not a record of
anything. What is kept is what the machine actually knows: when it happened,
whose it is, and every use of it afterwards. An honest empty field beats an
attestation nobody meant.

**Every application is audited.** Who, when, on which app, with the digest of
what was drawn. This is the trail that answers an auditor, and it is the whole
reason this is better than the caregiver signing by hand — which leaves none.

WHERE IT LIVES. `/var/lib/aptlog/signatures.json`, 0600, owned by the service
user.

It was `/etc/aptlog/` first, beside the schedule, and it could not stay there.
That directory is root-owned and NOT writable by the service on purpose — REQ
5.4.1 turns on the service being unable to create `/etc/aptlog/transport.conf`
and switch off its own containment — so the very first live registration died
with `PermissionError: /etc/aptlog/signatures.tmp`, because writing a file
atomically means creating a temporary one next to it. Loosening that directory
to fix this would have traded a containment guarantee for a save button.

`/var/lib/aptlog` is the directory the service already owns and writes every
few seconds. The FILE stays 0600 and owned by the service user, which is what
actually decides who can read a stroke set; the directory being listable gives
up nothing, since the filename says only that adoptions exist. It is more
sensitive than the schedule, not less — a stroke set is reproducible ink — so
`sanitize-for-image.sh` removes it before any image is taken, and nothing in
this repository has ever seen one.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from apt_log import sign as sign_mod

log = logging.getLogger(__name__)

STORE_PATH = Path("/var/lib/aptlog/signatures.json")

# Owner-only. The schedule next to it is 0644 and can afford to be; a stroke
# set is the thing itself, and any account on this machine reading it has a
# signature it can reproduce.
STORE_MODE = 0o600

# Kept as a FIELD and no longer as a demand. Adoptions already written carry
# the sentence that was typed at the time, and the roster still shows it, so
# the column cannot be deleted without losing what those records say. New
# adoptions simply leave it empty rather than being refused for it — see the
# note in the module docstring for why the demand went.

_SPACE = re.compile(r"\s+")


def key(name: str) -> str:
    """The stable form of a party's name.

    Case and spacing vary between the card, the app and whatever gets typed
    into the portal at eight in the morning; none of that should produce a
    second enrolment for the same person. Accents are folded for matching only
    — the display name is kept exactly as it was given, because it is a
    person's name and this file is the last place to be casual about that.
    """
    folded = unicodedata.normalize("NFKD", name or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return _SPACE.sub(" ", folded).strip().casefold()


# --------------------------------------------------------------------- store
def _read(path: Path | None = None) -> dict:
    try:
        doc = json.loads((path or STORE_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _write(doc: dict, path: Path | None = None) -> None:
    """Replace the store, never widening its mode.

    Written to a temporary file with the mode set BEFORE the rename, so there
    is no instant at which a world-readable file holds a signature.
    """
    target = path or STORE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    os.chmod(tmp, STORE_MODE)
    os.replace(tmp, target)


def enroll(name: str, strokes, aspect: float = 1.0, witness: str = "",
           path: Path | None = None) -> str:
    """Adopt a signature for `name`. Returns its digest.

    Refuses a payload that is not signature-shaped — the same check the live
    replay uses, so nothing can be enrolled that could not be drawn — and
    refuses one that belongs to nobody.

    `witness` is optional and still stored. It stopped being required when the
    field asking for it was taken off the sheet; adoptions made before that
    carry what was typed then, and this keeps writing whatever it is given.
    """
    display = _SPACE.sub(" ", (name or "").strip())
    if not display:
        raise ValueError("a signature belongs to somebody")
    if not sign_mod.validate(strokes):
        raise ValueError("that is not a signature")

    doc = _read(path)
    digest = sign_mod.digest(strokes)
    doc[key(display)] = {
        "name": display,
        "strokes": strokes,
        "aspect": float(aspect or 1.0),
        "digest": digest,
        "witness": (witness or "").strip(),
        "at": datetime.now().astimezone().isoformat(),
    }
    _write(doc, path)
    # The digest and nothing else. The log is read over a shoulder and shipped
    # in a bug report; the strokes are not going into it.
    log.info("signature adopted (%s…)", digest[:8])
    return digest


def forget(name: str, path: Path | None = None) -> bool:
    """Drop an adoption. True if there was one."""
    doc = _read(path)
    if doc.pop(key(name), None) is None:
        return False
    _write(doc, path)
    log.info("signature adoption withdrawn")
    return True


def enrolled(name: str, path: Path | None = None) -> bool:
    return key(name) in _read(path)


def roster(path: Path | None = None) -> list[dict]:
    """Who has adopted a signature — WITHOUT the signatures.

    This is what the portal is allowed to see. Every field here is safe on a
    screen somebody else can look at; `strokes` is deliberately absent and the
    route that serves this must never reach past it.
    """
    out = []
    for entry in _read(path).values():
        if not isinstance(entry, dict):
            continue
        out.append({"name": entry.get("name", ""),
                    "digest": (entry.get("digest") or "")[:12],
                    "witness": entry.get("witness", ""),
                    "at": entry.get("at", "")})
    return sorted(out, key=lambda e: e["name"].casefold())


def strokes_for(name: str, path: Path | None = None) -> tuple | None:
    """The adopted strokes and their aspect, or None.

    THE ONE FUNCTION THAT HANDS BACK A SIGNATURE, and the reason the module
    docstring is as long as it is. Its only legitimate caller is the route
    that answers a press by the person the signature belongs to. It must never
    be reachable from the scheduler, and `test_enrolled.py` holds a test that
    fails if `autoentry` or `macros` so much as imports this module.
    """
    entry = _read(path).get(key(name))
    if not isinstance(entry, dict):
        return None
    strokes = entry.get("strokes")
    if not sign_mod.validate(strokes):
        # A store that has been edited by hand into something unsignable is a
        # refusal, not a crash and not a half-drawn signature.
        log.warning("an adopted signature is not signature-shaped; refusing")
        return None
    return strokes, float(entry.get("aspect") or 1.0)


def digest_for(name: str, path: Path | None = None) -> str:
    entry = _read(path).get(key(name))
    return (entry or {}).get("digest", "") if isinstance(entry, dict) else ""


# --------------------------------------------------------------------- trail
# Deliberately NOT an `audit.AuditRecord`. That type is a check-off attempt and
# its mandatory fields say so — a scheduled time, a gate result, a transport
# mode. Applying a signature has none of those, and inventing them to satisfy
# the constructor would put fiction in the one file that exists to be trusted.
# So this is its own append-only line, with only what actually happened in it.
USE_PATH = Path("/var/lib/aptlog/signings.jsonl")


def record_use(name: str, digest: str, package: str = "",
               path: Path | None = None) -> None:
    """One line per application. Never the strokes.

    This trail is the answer to the question the agency will eventually ask —
    who signed, when, on which app — and it is the reason an adopted signature
    applied in the room is a better record than the caregiver signing by hand,
    which leaves nothing behind at all.
    """
    target = path or USE_PATH
    line = json.dumps({"at": datetime.now().astimezone().isoformat(),
                       "name": name, "digest": (digest or "")[:16],
                       "package": package, "how": "pressed"},
                      separators=(",", ":"), sort_keys=True)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        # A trail that cannot be written is worth saying loudly, but it must
        # not take the signature down with it — the two people are standing
        # there and the screen is in front of them.
        log.warning("could not record the signing (%s)", exc)


# ------------------------------------------------------- matching the screen
# THE APP'S NAME AND THE ADOPTED NAME ARE NOT THE SAME STRING, and requiring
# them to be would make this feature useless on the one case it exists for.
#
# The app renders a legal name off an agency record — "LUCRESIA L PUPO" — and
# the adoption is typed into a phone by somebody standing in a living room,
# who writes what they call her. Case, accents and a middle initial should not
# decide whether her own signature is offered to her.
#
# WHAT IS NOT TOLERATED IS AMBIGUITY. Every loosening here is a step toward
# putting one person's signature under another person's name, on a record an
# auditor reads, so the rule is deliberately conservative in the one direction
# that matters: more than one candidate returns NOTHING. A caregiver being
# shown no suggestion loses a tap; a caregiver being shown the wrong one loses
# the trail.
_INITIAL = 1


def _tokens(name: str) -> set:
    """Significant parts of a name, folded. Initials are not significant."""
    return {p for p in key(name).split(" ") if len(p) > _INITIAL}


def matches(screen_name: str, adopted_name: str) -> bool:
    """Whether these two strings plausibly name one person.

    Neither side is treated as authoritative: the app may carry more of the
    name than the adoption does ("LUCRESIA L PUPO" against "Lucresia Pupo") or
    less. So the SHORTER significant set has to sit entirely inside the longer
    one — every word the shorter name uses is a word the longer name agrees
    with — which rejects two people who merely share a surname while accepting
    the same person written two ways.
    """
    a, b = _tokens(screen_name), _tokens(adopted_name)
    if not a or not b:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return short <= long


def who_signs(screen_name: str, path: Path | None = None) -> str:
    """The adopted party this screen is asking for, or "".

    Returns the roster's OWN spelling, so the caller can point at a button by
    the name printed on it rather than by the app's version of it.

    "" for no match and "" for more than one, deliberately conflated: both
    mean "do not put a signature in front of anybody", and a caller that told
    them apart would be a caller tempted to pick one.
    """
    if not (screen_name or "").strip():
        return ""
    hits = [e["name"] for e in roster(path)
            if e.get("name") and matches(screen_name, e["name"])]
    return hits[0] if len(hits) == 1 else ""
