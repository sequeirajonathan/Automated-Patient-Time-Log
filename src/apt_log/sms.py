"""The codes a care app texts, read off the phone that received them.

inMyTeam is the one app in this system that cannot be signed in without a
human: it texts a six-digit code and waits. Every other credential is stored;
this one arrives, once, on a phone. So the walk has always stopped at the code
screen, notified whoever was around, and waited for somebody to read a text
and type it into the portal.

That is only necessary while the code lands somewhere the controller cannot
see. When it lands on the phone the controller is already driving, the whole
relay is a person retyping six digits that are sitting in a database two feet
away — and `adb shell content query --uri content://sms/inbox` reads them.

WHAT THIS DELIBERATELY DOES NOT DO. It does not read the inbox at large. A
caregiver's phone carries messages from patients, from an agency, from her
family, and none of that is this project's business — REQ-3's whole argument
is about what the portal is allowed to know. So every read is filtered to ONE
sender, at the provider, and only the digits are ever returned. The body of a
matching message is never logged, never published, and never written down.

Freshness is the other half. A code that arrived an hour ago is not the code
the app is waiting for — it is the one that already expired — and typing it
produces "Incorrect verification code" and burns an attempt. Nothing older
than the window counts, and the window is short on purpose.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import time

log = logging.getLogger(__name__)

# Who sends inMyTeam's codes. Read off a real message; kept here rather than
# in a macro because it is a fact about the world, not a step in a walk.
INMYTEAM_SENDER = "7864204664"

# How recent a code has to be to be worth typing. inMyTeam's own codes outlive
# this, but a code the app is waiting for has just been sent — and the failure
# this bounds is the expensive one: an old code is REJECTED, which clears the
# field, raises a dialog nobody can see (it is absent from the tree) and
# spends one of a limited number of attempts.
FRESH_WITHIN = 180.0

# Six digits standing alone. Anchored on word boundaries so a phone number or
# an order reference inside the same sentence cannot be mistaken for the code:
# "Here is your passcode from INMYTEAM: 604820" is the shape this reads.
_CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# One row of `content query` output. The provider prints `key=value` pairs
# separated by ", " on a line beginning "Row: N".
_ROW = re.compile(r"^Row:\s*\d+\s*(.*)$", re.M)


def _adb(args: list[str], serial: str | None = None,
         timeout: float = 20.0) -> str:
    cmd = ["adb"] + (["-s", serial] if serial else []) + args
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("adb failed reading messages: %s", exc)
        return ""
    if done.returncode != 0:
        return ""
    return done.stdout.decode("utf-8", "replace")


def _rows(out: str) -> list[dict]:
    """The provider's rows as dicts, in the order it printed them."""
    rows = []
    for line in _ROW.findall(out or ""):
        row: dict = {}
        for pair in line.split(", "):
            key, sep, value = pair.partition("=")
            if sep:
                row[key.strip()] = value.strip()
        if row:
            rows.append(row)
    return rows


def code_from(body: str) -> str:
    """The six-digit code in a message body, or "".

    The LAST match, not the first. "Here is your passcode from INMYTEAM:
    604820" has one, but a message that also carries a reference number puts
    the code where a person's eye ends up — at the end, after the colon.
    """
    found = _CODE.findall(body or "")
    return found[-1] if found else ""


def latest_code(sender: str = INMYTEAM_SENDER,
                within: float = FRESH_WITHIN,
                serial: str | None = None,
                now: float | None = None) -> str:
    """The newest code this sender texted inside the window, or "".

    Filtered at the PROVIDER by sender, so no other conversation is read into
    this process at all — the query cannot return a neighbour's message even
    momentarily. The digits are the only thing that comes back out.
    """
    digits = "".join(c for c in sender if c.isdigit())
    if not digits:
        return ""
    # LIKE on the trailing digits: the provider stores numbers in whatever
    # shape they arrived — "+17864204664", "7864204664", sometimes spaced —
    # and the last ten are the part that is the same in all of them.
    #
    # QUOTED FOR THE DEVICE'S OWN SHELL. `adb shell` does not take an argv —
    # it joins what it is given into a command line and hands it to sh on the
    # phone, so an unquoted clause arrives as five separate words and
    # `content` answers with its usage text. Caught live: the query came back
    # 2,765 bytes of help and the parser read nought rows out of it, which
    # looks exactly like an empty inbox. `feed.type_into` learned this same
    # lesson about a patient's name with a space in it.
    where = shlex.quote(f"address LIKE '%{digits[-10:]}%'")
    out = _adb(["shell", "content", "query", "--uri", "content://sms/inbox",
                "--projection", "date:body", "--where", where], serial)
    if not out:
        return ""

    at = time.time() if now is None else now
    best_at, best = 0.0, ""
    for row in _rows(out):
        try:
            # The provider reports milliseconds since the epoch.
            when = float(row.get("date", "0")) / 1000.0
        except (TypeError, ValueError):
            continue
        if at - when > within or when > at + 60:
            # Too old to be the code being waited for — or dated in the
            # future, which a phone with a wrong clock will produce and which
            # must not be trusted just because it sorts first.
            continue
        code = code_from(row.get("body", ""))
        if code and when > best_at:
            best_at, best = when, code
    return best


def wait_for_code(sender: str = INMYTEAM_SENDER,
                  timeout: float = 90.0,
                  poll: float = 3.0,
                  after: float | None = None,
                  serial: str | None = None) -> str:
    """Wait for a code that arrives AFTER this call, or "" if none does.

    `after` is what makes this safe to call twice. Without it a second run
    would find the first run's code still sitting in the inbox, type a code
    the app has already rejected, and report success — so the window starts
    when the caller says it does, which is the moment before Sign in was
    pressed.
    """
    started = time.time() if after is None else after
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = latest_code(sender, within=max(time.time() - started, 1.0),
                           serial=serial)
        if code:
            return code
        time.sleep(poll)
    return ""
