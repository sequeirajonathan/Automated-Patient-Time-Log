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

SENDING, the second half of this module, is the other direction: the phone has
a SIM and cell service, so the code it received can be passed on as a text to
the people who might otherwise be locked out. That is a deliberate widening of
who can sign in and it is the owner's call, made twice; what this module owes
it is that it happens ONCE per code, to a list that lives outside this
repository, and never to anybody who did not ask to be on it.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import time
from pathlib import Path

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
    return _newest(sender, within, serial, now)[1]


def _newest(sender: str = INMYTEAM_SENDER,
            within: float = FRESH_WITHIN,
            serial: str | None = None,
            now: float | None = None) -> tuple[float, str]:
    """The newest code AND when it arrived, as (timestamp, code).

    The timestamp is what forwarding is keyed on — "have we already passed
    this one along" is a question about a message, not about a clock — so the
    read has to hand back both. `latest_code` is the same work with the half
    its callers do not need thrown away.
    """
    digits = "".join(c for c in sender if c.isdigit())
    if not digits:
        return 0.0, ""
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
        return 0.0, ""

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
    return best_at, best


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


# ============================================================== SENDING ONE
# The phone has a SIM and cell service. Everything below is about using it.
#
# WHY A BINDER TRANSACTION AND NOT THE MESSAGING APP. The obvious way to send
# a text from a phone is to open its messaging app, type, and press send — and
# this controller can drive an app, so that road exists. It is the wrong one
# here for a specific reason: the moment a code needs forwarding is the moment
# inMyTeam is holding its code screen open, and walking away to Messages
# abandons that screen mid-sign-in. The whole point of forwarding is to help
# somebody sign in; a forwarder that breaks the sign-in to do it has taken
# something away and given it back.
#
# `service call isms` goes to the telephony service directly. Nothing appears
# on the display, nothing takes focus, and the resident Appium session is not
# touched — so this can run while the app is mid-walk, which is exactly when
# it has to.
#
# THE TRANSACTION NUMBER IS A FACT ABOUT THE DEVICE, NOT ABOUT ANDROID.
# `ISms`'s methods are numbered by their order in the AIDL, so a version that
# adds or drops a method renumbers everything after it. 5 is
# `sendTextForSubscriber` on Android 12 through 14, which is what this phone
# runs (13) and what its replacement will run. It was not taken on faith:
# transaction 5 was called on the live device with the arguments below and a
# probe message came back into its own inbox from its own number.
#
# On a new phone, do that again rather than assuming — one text to itself,
# then read it back:
#
#   adb shell service call isms 5 i32 <subId> s16 com.android.mms.service \
#       s16 null s16 <the phone's own number> s16 null s16 aptlogprobe \
#       s16 null s16 null i32 0 i64 0
#   adb shell content query --uri content://sms \
#       --projection date:address:body --where "body LIKE '%aptlogprobe%'"
#
# If the row does not appear, `SEND_TXN` is wrong for that device and MUST be
# corrected rather than worked around: a wrong number on this service is not a
# no-op, it is a different telephony method being called with nonsense.
SEND_TXN = "5"

# The AIDL, for reading the argument list below against:
#
#   sendTextForSubscriber(int subId, String callingPkg,
#                         String callingAttributionTag, String destAddr,
#                         String scAddr, String text, PendingIntent sentIntent,
#                         PendingIntent deliveryIntent,
#                         boolean persistMessageForNonDefaultSmsApp,
#                         long messageId)
#
# The two PendingIntents are passed as the string "null" because `service
# call` has no way to marshal one, and neither is wanted: they exist to tell a
# caller whether the message went, and this process is not waiting.
SEND_PKG = "com.android.mms.service"

# Which SIM. 1 on this phone (`dumpsys isub`, defaultSmsSubId=1) — read rather
# than guessed, because 0 is the SLOT index and a plausible wrong answer.
DEFAULT_SUB_ID = "1"

# What a transaction that raised nothing looks like. `service call` prints the
# reply parcel, and a method that returns void and threw no exception replies
# with a zero status word. Anything else — an exception, a wrong arity, a
# transaction that is not this method on this device — prints differently, and
# that difference is the only evidence available here that the text went.
_SENT_OK = "Parcel(00000000"


def _dialable(number: str) -> str:
    """A number as digits, with a North American country code taken off.

    "+1 (954) 540-5276", "1-954-540-5276" and "9545405276" are one person,
    and a list where they are three is a list that texts them twice and calls
    it two recipients. Eleven digits beginning with 1 is the only shape
    collapsed — anything longer is a country code that is NOT this one's, and
    guessing at those from a Miami phone would break a number to tidy it.
    """
    digits = "".join(c for c in number if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def send(number: str, body: str, serial: str | None = None,
         sub_id: str = DEFAULT_SUB_ID) -> bool:
    """Send one text from this phone's own SIM. True if telephony took it.

    True means the telephony service accepted the message without raising,
    not that it reached a handset — nothing here waits for a carrier, and a
    forwarder that blocked on delivery receipts would hold up the sign-in it
    exists to help.
    """
    digits = _dialable(number)
    if not digits or not body:
        return False
    # QUOTED FOR THE DEVICE'S OWN SHELL, the same lesson `latest_code` learned
    # the expensive way: `adb shell` joins its arguments into a command line
    # for sh ON THE PHONE, so a body with a space in it arrives as several
    # arguments and `service call` reads the tail of the sentence as
    # transaction arguments.
    out = _adb(["shell", "service", "call", "isms", SEND_TXN,
                "i32", sub_id,
                "s16", SEND_PKG,
                "s16", "null",
                "s16", digits,
                "s16", "null",
                "s16", shlex.quote(body),
                "s16", "null",
                "s16", "null",
                "i32", "0",
                "i64", "0"], serial)
    if _SENT_OK in out:
        return True
    # The number is not logged. The failure is worth a line; who it was for is
    # not this log's business, and a log on this machine is read over
    # somebody's shoulder more often than not.
    log.warning("telephony refused a text (%s)", (out or "no reply").strip()[:120])
    return False


# ========================================================= FORWARDING A CODE
# What the people on the list receive. Deliberately short, deliberately dull,
# and deliberately carrying no patient, no visit and no agency: it lands on
# three lock screens, and the same argument that keeps all of that out of a
# push notification keeps it out of a text.
#
# It says which app, so somebody holding two codes can tell them apart, and it
# says the code is short-lived, because the failure this whole feature exists
# to prevent is a person typing a code that expired while they looked for it.
#
# PLAIN ASCII, and that is not a style preference. A message that is entirely
# GSM-7 characters fits 160 to a part; one non-GSM character — an em dash, a
# curly quote, an accent — switches the whole message to UCS-2 and the limit
# drops to 70. The prose in this file is written with em dashes throughout, so
# this line is the one place that rule has to be broken deliberately.
FORWARD_TEXT = "inMyTeam sign-in code {code} - expires in a few minutes."

# One part. `sendTextForSubscriber` sends a single message and does not split
# one, so a body over the limit is a body that arrives truncated or not at
# all — and truncation takes the tail, which is where the code is. There is a
# test on this and on the encoding that decides which limit applies.
SMS_ONE_PART = 160


def _forwarded_path() -> Path:
    """Resolved on the call, not bound at import, so a test can move it."""
    from apt_log.ui.state import STATE_DIR

    return Path(STATE_DIR) / "code-forwarded.json"


def already_forwarded(when: float) -> bool:
    """Whether the message that arrived at `when` has already gone out.

    Keyed on the MESSAGE's timestamp rather than on a cooldown, and written to
    disk rather than held in memory. Both of those are the same lesson,
    learned from the notification storm: a guard that lives in a process is a
    guard that a deploy forgets, and every deploy restarts this one. A
    timestamp on disk cannot be forgotten by a restart, and it cannot be
    confused about which code it is talking about.
    """
    try:
        seen = float(json.loads(
            _forwarded_path().read_text(encoding="utf-8"))["at"])
    except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError):
        return False
    # `>=` and not `>`: the same message read twice is the case this exists
    # for. A message that is OLDER than the mark is also not forwarded — that
    # is a code from before the last one, which nobody is waiting for.
    return when <= seen


def _mark_forwarded(when: float) -> None:
    try:
        path = _forwarded_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": when}), encoding="utf-8")
    except OSError as exc:
        log.debug("cannot record the forwarded code (%s)", exc)


def recipients(provider=None) -> list[str]:
    """Who the code goes to, read from the secrets file.

    NOT FROM THIS REPOSITORY. They are three people's personal mobile
    numbers, and a git history is the one place a phone number can never be
    taken back out of. They live beside the app credentials in
    /etc/aptlog/secrets.env, which is 0600 and off the machine's disk image:

        CODE_RECIPIENTS=Jonathan:555...,Sadia:555...,Bertha:555...

    The names are for whoever edits that file — nothing here uses them — and
    a bare comma-separated list of numbers works just as well.
    """
    from apt_log.secrets import (CODE_RECIPIENTS, FileSecretProvider,
                                 SecretNotFound)

    try:
        raw = (provider or FileSecretProvider()).get(CODE_RECIPIENTS)
    except (SecretNotFound, OSError, PermissionError):
        return []
    out = []
    for entry in raw.split(","):
        # "Name:number" or just "number".
        digits = _dialable(entry.rpartition(":")[2])
        if len(digits) >= 10 and digits not in out:
            out.append(digits)
    return out


def forward_code(code: str, when: float, provider=None,
                 serial: str | None = None) -> int:
    """Text one code onward, once. Returns how many went.

    Returns 0 and sends nothing if this message has already been forwarded,
    which is what makes it safe to call from both the sign-in walk and the
    tick behind it — whichever gets there first does the work and the other
    finds it done.
    """
    if not code or already_forwarded(when):
        return 0
    people = recipients(provider)
    if not people:
        return 0
    # Marked BEFORE the first send, not after. If this process dies halfway
    # through three texts, the cost of the mark being early is that somebody
    # misses one code; the cost of it being late is the next tick starting the
    # whole list again, which is how one code becomes six texts. The storm is
    # the worse failure and it is the one that actually happened.
    _mark_forwarded(when)
    body = FORWARD_TEXT.format(code=code)
    sent = sum(1 for number in people if send(number, body, serial))
    log.info("code forwarded to %d of %d", sent, len(people))
    return sent


# How often the tick behind the walk is allowed to ask the phone for messages.
# Every read is an adb subprocess with a twenty-second ceiling against a phone
# that is usually busy driving an app, and the thing being waited for takes a
# carrier the better part of a minute anyway.
FORWARD_POLL = 20.0
_last_poll = [0.0]


def forward_any_new(serial: str | None = None, now: float | None = None,
                    force: bool = False) -> int:
    """Look for a code that has arrived and pass it on. Returns how many went.

    This is the standing half of the feature, and it is what makes forwarding
    true of codes this system did not ask for: somebody signing in from their
    own phone causes inMyTeam to text THIS phone, and nothing in the portal
    knows that happened. A poll behind the tick notices, and everyone on the
    list gets the code whether the portal was involved or not.

    Throttled, and quietly: called every tick, it does device work a few times
    a minute rather than a few times a second. `force` is for the one caller
    that already knows a code just landed — the sign-in walk, which has spent
    a minute waiting for it and should not then wait out a poll interval to
    pass it on. Forcing skips the THROTTLE, never the dedup.
    """
    at = time.time() if now is None else now
    if not force and at - _last_poll[0] < FORWARD_POLL:
        return 0
    _last_poll[0] = at
    when, code = _newest(serial=serial, now=at)
    if not code:
        return 0
    return forward_code(code, when, serial=serial)
