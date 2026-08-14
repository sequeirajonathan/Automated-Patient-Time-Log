"""The outstanding signature request (REQ-10.5, 10.6, 10.10).

Holds at most one request at a time. The agent pauses a check-off when the app
asks for a signature, so a second concurrent request would mean two paused
check-offs, and the caregiver would have no way to tell which one she was
signing for.

Strokes are handed to a waiting consumer and dropped. Nothing re-stampable is
kept: REQ-10.6 draws the line between transmitting her signing and signing on
her behalf, and a stored stroke set is on the wrong side of it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = timedelta(minutes=10)


@dataclass
class _Request:
    nonce: str
    patient_id: str
    scheduled: datetime | None
    expires_at: datetime
    strokes: list | None = None
    digest: str | None = None


class SignatureExpired(KeyError):
    pass


class SignatureQueue:
    def __init__(self, timeout: timedelta = DEFAULT_TIMEOUT):
        self._timeout = timeout
        self._lock = threading.Lock()
        self._request: _Request | None = None
        self._delivered = threading.Event()

    # ---------------------------------------------------------------- agent
    def request(self, patient_id: str, scheduled: datetime | None) -> str:
        """Open a request and return its single-use nonce."""
        with self._lock:
            nonce = secrets.token_urlsafe(16)
            self._request = _Request(
                nonce=nonce,
                patient_id=patient_id,
                scheduled=scheduled,
                expires_at=datetime.now() + self._timeout,
            )
            self._delivered.clear()
        log.info("signature requested for %s", patient_id)
        return nonce

    def wait(self, timeout_seconds: float) -> tuple[list, str] | None:
        """Block until strokes arrive, or return None on timeout.

        REQ-10.10: the caller abandons the check-off and alerts. It must never
        submit unsigned, and never hold the session open indefinitely.
        """
        if not self._delivered.wait(timeout_seconds):
            with self._lock:
                self._request = None
            return None
        with self._lock:
            req = self._request
            if req is None or req.strokes is None:
                return None
            strokes, digest = req.strokes, req.digest
            # Consumed. Nothing reusable survives this call.
            self._request = None
            self._delivered.clear()
        return strokes, digest or ""

    def cancel(self) -> None:
        with self._lock:
            self._request = None
            self._delivered.clear()

    # ------------------------------------------------------------------- ui
    def current(self) -> dict | None:
        with self._lock:
            req = self._request
            if req is None or req.strokes is not None:
                return None
            if datetime.now() > req.expires_at:
                log.info("signature request for %s expired", req.patient_id)
                self._request = None
                return None
            return {
                "nonce": req.nonce,
                "patient_id": req.patient_id,
                "scheduled": req.scheduled,
            }

    def submit(self, nonce: str, strokes: list) -> str:
        """Attach strokes to the outstanding request. Raises if the nonce is stale.

        The nonce is what makes a captured request body unusable a second time
        (REQ-10.5) — it is cleared the moment the agent consumes the strokes.
        """
        with self._lock:
            req = self._request
            if req is None or req.nonce != nonce:
                raise SignatureExpired(nonce)
            if datetime.now() > req.expires_at:
                self._request = None
                raise SignatureExpired(nonce)
            req.strokes = strokes
            req.digest = hashlib.sha256(
                json.dumps(strokes, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest()
            self._delivered.set()
            return req.digest
