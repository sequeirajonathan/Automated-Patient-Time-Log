"""The relay channel — one outstanding request for something only she can supply.

This generalises what was the signature queue, because the signature turned out
not to be the only wall the controller hits.

Clocking in opens a chooser: **GPS or a security token**. GPS asks the phone
where it is, and the phone is parked in a room four floors from the patient. A
wrong answer there is not a failed automation — it is a phone call from the
agency to the caregiver, which is the specific outcome this whole project exists
to avoid. So the controller does not answer that question. It stops, shows her
the screen it is looking at, and asks.

Four kinds of request, and each one asks for something the machine genuinely
cannot produce:

- **signature** — her attestation (REQ-10). Hers, drawn fresh, replayed as touch.
- **token** — a code off her own token device, which the agency accepts in place
  of a location check. The device travels with her, so the code evidences
  nothing about where she is; what it does is stop the app asking the phone.
- **choice** — one of the options *the app itself is offering*, relayed back.
- **otp** — a login code texted to her phone. One of the four apps authenticates
  by phone number, so this is the only way in.

The token one is worth stating plainly because it is easy to get backwards. A
token is not proof of presence and does not replace the gate. It is the reason
the app never runs its radius check, which is an operational win and nothing
more. Whether a visit may be recorded at all is still REQ-5's question, answered
by the controller's own signals, and the token does not touch it.

**Opening an OTP request is not free.** By the time one exists the app has already
sent an SMS, so a caller that retries on timeout is sending a second text to a
real person and walking toward a rate limit. One request, one code; on timeout,
abandon and alert rather than asking again.

The choice one is the one to be careful about, so it is built to be narrow. The
agent enumerates the options when it opens the request, and `submit` refuses
anything not on that list. There is no free-text answer and no "proceed anyway".
That matters because a relay that accepts arbitrary input is a remote control
with a presence gate bolted to the side, and the gate would be the part that
falls off. If the agent never offers GPS-without-presence as an option, no
request body from any device can select it.

Everything is dropped once consumed. REQ-10.6 draws that line for signatures,
and the same reasoning covers a token or a login code: they are credentials, a
stored copy is a reusable one, and nothing here needs them after they have been
typed in.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = timedelta(minutes=10)

KIND_SIGNATURE = "signature"
KIND_TOKEN = "token"
KIND_CHOICE = "choice"
KIND_OTP = "otp"
KINDS = (KIND_SIGNATURE, KIND_TOKEN, KIND_CHOICE, KIND_OTP)

# An SMS code is short-lived, and a request that outlives the code it is asking
# for produces the worst diagnosis available: the controller types an expired
# code, the app rejects it, and the failure is indistinguishable from a wrong
# password. Better to give up while the reason is still legible.
OTP_TIMEOUT = timedelta(minutes=4)

# Separators people type into a code field but that the app does not want.
_TOKEN_SEPARATORS = re.compile(r"[\s\-]+")
_TOKEN_SHAPE = re.compile(r"\A[A-Za-z0-9]{4,16}\Z")
_HAS_DIGIT = re.compile(r"\d")


class RelayError(RuntimeError):
    """The relay was asked for something it will not do."""


class RelayExpired(KeyError):
    """The nonce is not the outstanding one, or it has timed out."""


@dataclass
class RelayResponse:
    kind: str
    value: object          # strokes | code string | chosen option
    digest: str            # of the value, for the audit trail


@dataclass
class _Request:
    nonce: str
    kind: str
    patient_id: str
    scheduled: datetime | None
    expires_at: datetime
    options: tuple[str, ...] = ()
    response: RelayResponse | None = None


def normalise_token(raw: str) -> str:
    """Strip what a person types around a code, and check it looks like one.

    She is reading a code off a small screen with one hand, so spaces and dashes
    arrive whether the field wants them or not, and they are removed.

    Removing them is also what makes the digit rule necessary. Strip the space
    out of a two-word name and the result is a run of letters the right length
    to pass for a code — "Alice Example" becomes "AliceExample", twelve
    alphanumeric characters. Requiring a digit is what separates a code from a
    word, and it is why this rejects rather than accepts when in doubt.

    This is a typo guard, not a privacy control: the value is never logged and
    never stored, which is what actually keeps a mistyped name out of the
    record. If a real token turns out to be letters only, this is the line to
    change — and finding that out by being refused is the better way round.

    Shared with the SMS login codes, because the two genuinely have the same
    shape and inventing a difference would be pretending to a precision this
    does not have. What separates them is the kind — which the audit trail
    records, since one attests a visit and the other opens a session — and how
    long the request stays open.
    """
    cleaned = _TOKEN_SEPARATORS.sub("", (raw or "").strip())
    if not _TOKEN_SHAPE.match(cleaned) or not _HAS_DIGIT.search(cleaned):
        raise RelayError("that does not look like a security token")
    return cleaned


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True, default=str).encode()
    ).hexdigest()


class RelayQueue:
    """At most one outstanding request, of one kind, for one visit.

    One at a time is not a simplification. The agent pauses a check-off while it
    waits, so two outstanding requests would mean two paused check-offs and no
    way for her to tell which visit she was answering for — and answering the
    wrong one attaches her signature, or a patient's token, to somebody else's
    visit.
    """

    def __init__(self, timeout: timedelta = DEFAULT_TIMEOUT):
        self._timeout = timeout
        self._lock = threading.Lock()
        self._request: _Request | None = None
        self._delivered = threading.Event()

    # ----------------------------------------------------------------- agent
    def request(
        self,
        kind: str,
        patient_id: str,
        scheduled: datetime | None,
        options: tuple[str, ...] = (),
        timeout: timedelta | None = None,
    ) -> str:
        """Open a request and return its single-use nonce."""
        if kind not in KINDS:
            raise RelayError(f"unknown relay kind {kind!r}; known: {KINDS}")
        if kind == KIND_CHOICE and not options:
            raise RelayError(
                "a choice with no options would be a free-text answer; "
                "the agent must enumerate what the app is offering"
            )

        with self._lock:
            nonce = secrets.token_urlsafe(16)
            self._request = _Request(
                nonce=nonce,
                kind=kind,
                patient_id=patient_id,
                scheduled=scheduled,
                expires_at=datetime.now() + (timeout or self._timeout),
                options=tuple(options),
            )
            self._delivered.clear()
        log.info("relay: asked for a %s for %s", kind, patient_id)
        return nonce

    def request_signature(self, patient_id: str, scheduled: datetime | None) -> str:
        return self.request(KIND_SIGNATURE, patient_id, scheduled)

    def request_token(self, patient_id: str, scheduled: datetime | None) -> str:
        return self.request(KIND_TOKEN, patient_id, scheduled)

    def request_otp(self, app_name: str) -> str:
        """Ask for a login code. Not tied to a visit — this is about a session.

        The short window is the point: see OTP_TIMEOUT.
        """
        return self.request(KIND_OTP, app_name, None, timeout=OTP_TIMEOUT)

    def request_choice(self, patient_id: str, scheduled: datetime | None,
                       options: tuple[str, ...]) -> str:
        return self.request(KIND_CHOICE, patient_id, scheduled, options)

    def wait(self, timeout_seconds: float) -> RelayResponse | None:
        """Block until she answers, or return None so the caller can give up.

        REQ-10.10: on None the caller abandons the check-off and alerts. It must
        never submit unanswered, and never hold the session open indefinitely —
        a session held open is a phone stuck mid-clock-in.
        """
        if not self._delivered.wait(timeout_seconds):
            with self._lock:
                self._request = None
            return None
        with self._lock:
            req = self._request
            if req is None or req.response is None:
                return None
            response = req.response
            # Consumed. Nothing reusable survives this call — not a stroke set,
            # not a token.
            self._request = None
            self._delivered.clear()
        return response

    def cancel(self) -> None:
        with self._lock:
            self._request = None
            self._delivered.clear()

    # -------------------------------------------------------------------- ui
    def current(self) -> dict | None:
        """What the page should be asking for, or None."""
        with self._lock:
            req = self._request
            if req is None or req.response is not None:
                return None
            if datetime.now() > req.expires_at:
                log.info("relay: the %s request for %s expired",
                         req.kind, req.patient_id)
                self._request = None
                return None
            return {
                "nonce": req.nonce,
                "kind": req.kind,
                "patient_id": req.patient_id,
                "scheduled": req.scheduled,
                "options": list(req.options),
            }

    def submit(self, nonce: str, kind: str, value) -> str:
        """Answer the outstanding request. Returns the digest for the audit.

        The kind is checked as well as the nonce. A signature POST must not be
        able to satisfy a token request just because it guessed the nonce right:
        the two mean different things in the record, and the audit trail says
        which one happened.
        """
        with self._lock:
            req = self._request
            if req is None or req.nonce != nonce:
                raise RelayExpired(nonce)
            if datetime.now() > req.expires_at:
                self._request = None
                raise RelayExpired(nonce)
            if req.kind != kind:
                raise RelayError(
                    f"outstanding request is a {req.kind}, not a {kind}"
                )

            value = self._validate(req, kind, value)
            digest = _digest(value)
            req.response = RelayResponse(kind=kind, value=value, digest=digest)
            self._delivered.set()
            return digest

    @staticmethod
    def _validate(req: _Request, kind: str, value):
        if kind == KIND_SIGNATURE:
            if not isinstance(value, list) or not value:
                raise RelayError("no strokes")
            return value

        if kind in (KIND_TOKEN, KIND_OTP):
            if not isinstance(value, str):
                raise RelayError("a code is text")
            # normalise_token raises on anything that is not code-shaped, and
            # the raw value never reaches a log line on the way past.
            return normalise_token(value)

        if kind == KIND_CHOICE:
            if value not in req.options:
                # The offered options are the app's own words and safe to echo;
                # what was submitted is not, so it is not repeated back.
                raise RelayError(
                    f"that is not one of the options the app offered "
                    f"({', '.join(req.options)})"
                )
            return value

        raise RelayError(f"unknown relay kind {kind!r}")
