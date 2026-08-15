"""The relay channel.

Most of these hold one line: the relay carries her answer and nothing else. It
cannot be talked into accepting an answer the app never offered, and it does not
keep anything after handing it over.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from apt_log.ui.relay import (
    DEFAULT_TIMEOUT,
    OTP_TIMEOUT,
    KIND_CHOICE,
    KIND_OTP,
    KIND_SIGNATURE,
    KIND_TOKEN,
    RelayError,
    RelayExpired,
    RelayQueue,
    normalise_token,
)

STROKE = [[[0.1, 0.2, 0]]]


class TestOneAtATime:
    def test_a_second_request_replaces_the_first(self):
        q = RelayQueue()
        q.request_signature("PT-1", None)
        second = q.request_signature("PT-2", None)
        assert q.current()["nonce"] == second
        assert q.current()["patient_id"] == "PT-2"

    def test_an_expired_request_stops_being_offered(self):
        q = RelayQueue(timeout=timedelta(seconds=-1))
        q.request_token("PT-1", None)
        assert q.current() is None

    def test_timeout_returns_none_so_the_caller_can_abandon(self):
        """REQ-10.10 — never submit unanswered, never wait forever."""
        q = RelayQueue()
        q.request_signature("PT-1", None)
        assert q.wait(0.05) is None


class TestSignature:
    def test_strokes_are_dropped_once_consumed(self):
        """REQ-10.6 — nothing re-stampable survives."""
        q = RelayQueue()
        nonce = q.request_signature("PT-1", None)
        q.submit(nonce, KIND_SIGNATURE, STROKE)
        assert q.wait(0.5) is not None
        assert q.current() is None
        with pytest.raises(RelayExpired):
            q.submit(nonce, KIND_SIGNATURE, STROKE)

    def test_hash_is_stable_and_distinguishes_signatures(self):
        q = RelayQueue()
        n1 = q.request_signature("PT-1", None)
        d1 = q.submit(n1, KIND_SIGNATURE, [[[0.1, 0.1, 0]]])
        q.wait(0.5)
        n2 = q.request_signature("PT-2", None)
        d2 = q.submit(n2, KIND_SIGNATURE, [[[0.9, 0.9, 5]]])
        q.wait(0.5)
        assert d1 != d2 and len(d1) == 64

    def test_empty_strokes_are_refused(self):
        q = RelayQueue()
        nonce = q.request_signature("PT-1", None)
        with pytest.raises(RelayError):
            q.submit(nonce, KIND_SIGNATURE, [])


class TestToken:
    def test_a_token_is_carried_and_then_dropped(self):
        """It is her EVV credential, and a stored copy is a reusable one — the
        same objection REQ-10.6 makes about strokes. Nothing here needs it once
        it has been typed in."""
        q = RelayQueue()
        nonce = q.request_token("PT-1", datetime.now())
        q.submit(nonce, KIND_TOKEN, "4821 77")
        response = q.wait(0.5)
        assert response.value == "482177"
        assert q.current() is None

    @pytest.mark.parametrize("raw,expected", [
        ("482177", "482177"), (" 4821-77 ", "482177"), ("A1B2C3", "A1B2C3"),
    ])
    def test_separators_are_stripped_the_way_people_type_them(self, raw, expected):
        assert normalise_token(raw) == expected

    @pytest.mark.parametrize("raw", ["", "12", "x" * 20,
                                     "https://example.test/token"])
    def test_anything_not_code_shaped_is_refused(self, raw):
        with pytest.raises(RelayError):
            normalise_token(raw)

    @pytest.mark.parametrize("raw", ["Alice Example", "PACIENTE FICTICIA", "hello"])
    def test_a_name_does_not_pass_for_a_code(self, raw):
        """Stripping the space out of a two-word name leaves a run of letters
        the right length to look like a token. Requiring a digit is what tells
        a code from a word."""
        with pytest.raises(RelayError):
            normalise_token(raw)

    def test_the_token_is_not_repeated_back_in_the_error(self):
        q = RelayQueue()
        nonce = q.request_token("PT-1", None)
        with pytest.raises(RelayError) as exc:
            q.submit(nonce, KIND_TOKEN, "not a token at all")
        assert "not a token at all" not in str(exc.value)


class TestChoice:
    def test_only_an_offered_option_is_accepted(self):
        """The narrow point of the whole relay.

        Whatever the agent did not offer cannot be selected from a browser, so
        the relay cannot be turned into a way around the presence gate.
        """
        q = RelayQueue()
        nonce = q.request_choice("PT-1", None, ("token de seguridad",))
        with pytest.raises(RelayError):
            q.submit(nonce, KIND_CHOICE, "GPS")
        assert q.submit(nonce, KIND_CHOICE, "token de seguridad")

    def test_a_choice_with_no_options_is_refused_at_the_source(self):
        q = RelayQueue()
        with pytest.raises(RelayError, match="free-text"):
            q.request(KIND_CHOICE, "PT-1", None, ())

    def test_the_offered_options_reach_the_page(self):
        q = RelayQueue()
        q.request_choice("PT-1", None, ("GPS", "token de seguridad"))
        assert q.current()["options"] == ["GPS", "token de seguridad"]


class TestKindIsPartOfTheAnswer:
    def test_a_signature_cannot_satisfy_a_token_request(self):
        """The two mean different things in the audit trail.

        A visit recorded as token-verified when a signature arrived would be a
        record claiming evidence that was never produced.
        """
        q = RelayQueue()
        nonce = q.request_token("PT-1", None)
        with pytest.raises(RelayError, match="token"):
            q.submit(nonce, KIND_SIGNATURE, STROKE)

    def test_an_unknown_kind_cannot_be_requested(self):
        q = RelayQueue()
        with pytest.raises(RelayError):
            q.request("anything", "PT-1", None)


class TestOtp:
    def test_a_login_code_is_carried_and_dropped(self):
        q = RelayQueue()
        nonce = q.request_otp("inmyteam")
        q.submit(nonce, KIND_OTP, "483 921")
        assert q.wait(0.5).value == "483921"
        assert q.current() is None

    def test_the_window_is_shorter_than_the_default(self):
        """An SMS code outlives its request or the reverse; if the request wins,
        the controller types an expired code and the app rejects it — which is
        indistinguishable from a wrong password, the worst diagnosis available."""
        assert OTP_TIMEOUT < DEFAULT_TIMEOUT

    def test_it_is_not_tied_to_a_visit(self):
        """A login code opens a session; it does not attest to a patient."""
        q = RelayQueue()
        q.request_otp("inmyteam")
        assert q.current()["scheduled"] is None
        assert q.current()["patient_id"] == "inmyteam"

    def test_a_token_answer_cannot_satisfy_a_login_code(self):
        q = RelayQueue()
        nonce = q.request_otp("inmyteam")
        with pytest.raises(RelayError, match="otp"):
            q.submit(nonce, KIND_TOKEN, "483921")
