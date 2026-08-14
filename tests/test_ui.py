"""The dashboard and the signature queue.

The routing tests exist mainly to hold two lines that are easy to erode: the page
renders in the caregiver's language, and there is no way to record a visit from
it. The gate is what makes a recorded visit mean anything, and a helpful
"record anyway" button would quietly undo the whole system.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from apt_log.ui.app import LANGUAGE_COOKIE, app, queue
from apt_log.ui.signature_queue import SignatureExpired, SignatureQueue


@pytest.fixture
def client():
    queue.cancel()
    return TestClient(app)


class TestHealthz:
    def test_reports_ok(self, client):
        """manager.sh gates every deploy on this and heartbeat.sh needs it."""
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestLanguage:
    def test_defaults_to_spanish(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Registro de Horas" in r.text

    def test_honours_the_accept_language_header(self, client):
        r = client.get("/", headers={"Accept-Language": "en-US,en;q=0.9"})
        assert "Patient Time Log" in r.text

    def test_cookie_beats_the_header(self, client):
        r = client.get(
            "/",
            headers={"Accept-Language": "en-US"},
            cookies={LANGUAGE_COOKIE: "es"},
        )
        assert "Registro de Horas" in r.text

    def test_toggle_sets_the_cookie(self, client):
        r = client.post("/language", data={"language": "en"}, follow_redirects=False)
        assert r.status_code == 303
        assert LANGUAGE_COOKIE in r.cookies

    def test_unknown_language_falls_back_rather_than_erroring(self, client):
        r = client.get("/", headers={"Accept-Language": "de-DE"})
        assert r.status_code == 200


class TestNoOverrideControl:
    """ARCHITECTURE §3.2 — three write paths, and none of them records a visit."""

    def test_page_offers_no_record_anyway_action(self, client):
        body = client.get("/").text.lower()
        for phrase in ("record anyway", "registrar de todos modos",
                       "force", "forzar", "override"):
            assert phrase not in body

    def test_only_the_declared_write_routes_exist(self):
        posts = {
            r.path for r in app.routes
            if getattr(r, "methods", None) and "POST" in r.methods
        }
        assert posts == {"/language", "/signature", "/acknowledge", "/control"}


class TestApiState:
    def test_returns_machine_readable_state(self, client):
        body = client.get("/api/state").json()
        assert set(body) >= {"overall", "transport_mode", "paused",
                             "health", "attention", "signature_pending"}


class TestSignatureRoute:
    def test_rejects_an_empty_payload(self, client):
        r = client.post("/signature", json={"nonce": "x", "strokes": []})
        assert r.status_code == 400

    def test_rejects_an_unknown_nonce(self, client):
        r = client.post("/signature", json={"nonce": "nope", "strokes": [[[0, 0, 0]]]})
        assert r.status_code == 409

    def test_accepts_the_outstanding_nonce(self, client):
        nonce = queue.request("PT-0042", datetime.now())
        r = client.post("/signature", json={"nonce": nonce, "strokes": [[[0.1, 0.2, 0]]]})
        assert r.status_code == 200

    def test_a_captured_payload_cannot_be_replayed(self, client):
        """REQ-10.5 — the nonce is what makes a copied request body useless."""
        nonce = queue.request("PT-0042", datetime.now())
        payload = {"nonce": nonce, "strokes": [[[0.1, 0.2, 0]]]}
        assert client.post("/signature", json=payload).status_code == 200
        queue.wait(0.1)                       # agent consumes it
        assert client.post("/signature", json=payload).status_code == 409

    def test_prompt_names_the_patient_and_renders_in_spanish(self, client):
        queue.request("PT-0042", datetime(2026, 8, 14, 14, 0))
        body = client.get("/").text
        assert "PT-0042" in body      # REQ-10.3: she must see what she is signing for
        assert "Firme" in body
        queue.cancel()


class TestSignatureQueue:
    def test_only_one_request_outstanding_at_a_time(self):
        q = SignatureQueue()
        q.request("PT-1", None)
        second = q.request("PT-2", None)
        assert q.current()["nonce"] == second
        assert q.current()["patient_id"] == "PT-2"

    def test_strokes_are_dropped_once_consumed(self):
        """REQ-10.6 — nothing re-stampable survives."""
        q = SignatureQueue()
        nonce = q.request("PT-1", None)
        q.submit(nonce, [[[0.1, 0.1, 0]]])
        assert q.wait(0.5) is not None
        assert q.current() is None
        with pytest.raises(SignatureExpired):
            q.submit(nonce, [[[0.1, 0.1, 0]]])

    def test_hash_is_stable_and_distinguishes_signatures(self):
        q = SignatureQueue()
        n1 = q.request("PT-1", None)
        d1 = q.submit(n1, [[[0.1, 0.1, 0]]])
        q.wait(0.5)
        n2 = q.request("PT-2", None)
        d2 = q.submit(n2, [[[0.9, 0.9, 5]]])
        q.wait(0.5)
        assert d1 != d2 and len(d1) == 64

    def test_timeout_returns_none_so_the_caller_can_abandon(self):
        """REQ-10.10 — never submit unsigned, never wait forever."""
        q = SignatureQueue()
        q.request("PT-1", None)
        assert q.wait(0.05) is None

    def test_an_expired_request_stops_being_offered(self):
        q = SignatureQueue(timeout=timedelta(seconds=-1))
        q.request("PT-1", None)
        assert q.current() is None
