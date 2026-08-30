"""The relay-and-mirror page.

The routing tests exist mainly to hold two lines that are easy to erode: the page
renders in the caregiver's language, and there is no way to record a visit from
it. The gate is what makes a recorded visit mean anything, and a helpful
"record anyway" button would quietly undo the whole system.

The relay makes that second line harder to hold and therefore worth testing more
carefully, because it does accept input now. What keeps it honest is that every
answer it accepts was enumerated by the agent first — the queue's own tests cover
that, and the tests here cover the route not widening it on the way through.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from conftest import strip_js_comments
from apt_log.ui.app import LANGUAGE_COOKIE, _mirror_payload, app, queue
from apt_log.ui.i18n import Translator
from apt_log.ui import mirror as mirror_mod
from apt_log.ui import state as state_mod
from apt_log.ui.mirror import Mirror
from apt_log.ui.relay import KIND_CHOICE, KIND_SIGNATURE, KIND_TOKEN
from apt_log.ui.state import DashboardState


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
        r = client.get("/console")
        assert r.status_code == 200
        assert 'lang="es"' in r.text
        assert "Centro de control" in r.text

    def test_honours_the_accept_language_header(self, client):
        r = client.get("/console", headers={"Accept-Language": "en-US,en;q=0.9"})
        assert "Control centre" in r.text

    def test_cookie_beats_the_header(self, client):
        r = client.get(
            "/console",
            headers={"Accept-Language": "en-US"},
            cookies={LANGUAGE_COOKIE: "es"},
        )
        assert "Centro de control" in r.text

    def test_toggle_sets_the_cookie(self, client):
        r = client.post("/language", data={"language": "en"}, follow_redirects=False)
        assert r.status_code == 303
        assert LANGUAGE_COOKIE in r.cookies

    def test_unknown_language_falls_back_rather_than_erroring(self, client):
        r = client.get("/console", headers={"Accept-Language": "de-DE"})
        assert r.status_code == 200

    def test_the_stored_choice_outranks_the_browsers_own(self, client):
        """The two people using this portal want opposite languages from it.

        His browser asks for English; hers asks for Spanish. Once a device has
        actually chosen, the choice has to beat the header, or his phone reads
        Spanish again every time the cookie jar is cleared."""
        from apt_log import prefs

        prefs.set_language("device-1", "en")
        r = client.get("/console", headers={"Accept-Language": "es-MX"},
                       cookies={"aptlog_device": "device-1"})
        assert "Control centre" in r.text

    def test_a_device_that_never_chose_still_follows_its_browser(self, client):
        """Merely visiting must not be recorded as a preference — a stored
        default would outrank the header for every device forever."""
        from apt_log import prefs

        prefs.seen("device-2", where="/app")
        assert prefs.language_of("device-2") == ""
        r = client.get("/console", headers={"Accept-Language": "en-US"},
                       cookies={"aptlog_device": "device-2"})
        assert "Control centre" in r.text

    def test_the_switch_returns_to_the_page_it_was_pressed_on(self, client):
        """It used to always redirect to the dashboard, so changing language
        from the phone view threw her out of the phone view."""
        r = client.post("/language", data={"language": "es", "next": "/app"},
                        follow_redirects=False)
        assert r.headers["location"] == "/app"

    def test_the_switch_refuses_to_send_you_off_this_portal(self, client):
        """`next` is attacker-shaped: an open redirect from a page that can
        move the phone is a way to get somebody to press a control on a page
        they think is somewhere else."""
        for hostile in ("https://example.com/x", "//example.com/x"):
            r = client.post("/language",
                            data={"language": "es", "next": hostile},
                            follow_redirects=False)
            assert r.headers["location"] == "/app"


class TestWritePaths:
    """What the page may do to the phone.

    The old invariant here was "no write path records a visit". That stopped
    being true on purpose: the portal exists so she can drive the app herself,
    and driving the app is how a visit gets recorded. Pretending otherwise would
    leave a docstring guarding a property the product no longer has.

    Two narrower invariants replace it, and they are the ones worth keeping:

    - the write routes are an enumerated set, not whatever a handler grew
    - nothing accepts a raw coordinate or a raw keycode. She acts on things she
      can see, named from a frame the server can re-check. A route taking a
      point or a keycode could drive the app blind, and blind is the failure
      that produces a call from the agency.
    """

    def test_page_offers_no_record_anyway_action(self, client):
        """Still true, and still the point: nothing claims a visit happened
        except the app itself, driven by her, on a screen she is looking at."""
        body = client.get("/console").text.lower()
        for phrase in ("record anyway", "registrar de todos modos",
                       "force", "forzar", "record the visit",
                       "registrar la visita"):
            assert phrase not in body
        # "Override" is deliberately NOT in that list any more: the control
        # centre has density overrides, and they are the good kind — a layer
        # ABOVE the tuned defaults that can be cleared to reveal them again.
        # The banned word is the one that would mean overriding the presence
        # gate, which is what the phrases above catch.

    def test_only_the_declared_write_routes_exist(self):
        posts = {
            r.path for r in app.routes
            if getattr(r, "methods", None) and "POST" in r.methods
        }
        assert posts == {"/language", "/signature", "/relay", "/device", "/tap",
                         "/macro", "/acknowledge", "/control", "/sign",
                         # The app's own clear/save on the signature screen,
                         # pressed on her explicit ask. Takes a kind from an
                         # allow-list, never a coordinate — the spot is
                         # derived server-side from the canvas the finder
                         # sees, and off the signature moment it refuses.
                         "/sign-action",
                         # Preferences. They change what a page LOOKS like and
                         # how big the phone lays its text out; none of them
                         # can reach the app, name a patient, or touch the
                         # record. /settings/density is the one with teeth,
                         # and it clamps rather than trusting — see prefs.
                         "/settings/name", "/settings/density",
                         "/settings/density/clear",
                         # A browser asking to be told when the login code
                         # arrives. It stores what the browser handed over
                         # and nothing else: the subscription can push to
                         # that phone and cannot reach the app, the phone,
                         # or the record.
                         "/api/push/subscribe",
                         # Which visits the scheduler will be allowed to act
                         # on. It writes one short file on the CONTROLLER and
                         # cannot reach the app, the phone or the record —
                         # and today nothing reads it, because nothing fires
                         # yet. It is here so that the switch a person throws
                         # already means what they meant by it when firing
                         # does land.
                         "/schedule/arm",
                         # Adopted signatures, REQ-10.6a. Three writes, and
                         # each one is a person pressing a button in a room
                         # with the phone:
                         #
                         #   /signature/enroll  adopt, with a witness named
                         #   /signature/apply   draw an adopted one, by NAME
                         #   /signature/forget  withdraw an adoption
                         #
                         # `apply` is the one worth staring at, and it belongs
                         # on this list for the same reason `/sign` does: it
                         # takes no coordinate and no strokes. It takes a name,
                         # the lookup happens on the Pi, and the replay lands
                         # inside exactly one canvas element or refuses. So it
                         # can put a signature on the screen in front of the
                         # person it belongs to, and it cannot be used to get
                         # one out of the machine.
                         #
                         # None of the three is reachable from the scheduler —
                         # test_enrolled.py holds the import-graph test that
                         # keeps it that way, which is the invariant that
                         # actually matters here.
                         "/signature/enroll", "/signature/apply",
                         "/signature/forget",
                         # The sign-in code, texted onward because somebody
                         # pressed a button. It writes nothing to the phone's
                         # apps and records no visit — it reads the inbox and
                         # sends SMS — but it IS a write path in the sense
                         # this list cares about: pressing it makes the phone
                         # do something in the world. The digits are never in
                         # the response; see the route.
                         "/code/broadcast",
                         # Letting automatic sign-in try again after it stood
                         # itself down. It writes one short file on the
                         # CONTROLLER and cannot reach the app, the phone or
                         # the record — the most it can do is allow a future
                         # attempt, which is why it is a press by a person
                         # rather than a timer: the stop exists because only
                         # a person can fix what caused it, so only a person
                         # can say it is fixed.
                         "/auth/resume"}

    def test_no_route_accepts_a_raw_coordinate_or_keycode(self, client):
        """/tap takes an element from a named frame; /device takes an action
        name from an allow-list. Neither takes a number that means "here"."""
        assert client.post("/tap", json={"frame": "f", "x": 100, "y": 200}
                           ).status_code in (400, 409)
        assert client.post("/device", data={"action": "66"},
                           follow_redirects=False
                           ).headers["location"] == "/app?device=failed"


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
        nonce = queue.request_signature("PT-0042", datetime.now())
        r = client.post("/signature", json={"nonce": nonce, "strokes": [[[0.1, 0.2, 0]]]})
        assert r.status_code == 200

    def test_a_captured_payload_cannot_be_replayed(self, client):
        """REQ-10.5 — the nonce is what makes a copied request body useless."""
        nonce = queue.request_signature("PT-0042", datetime.now())
        payload = {"nonce": nonce, "strokes": [[[0.1, 0.2, 0]]]}
        assert client.post("/signature", json=payload).status_code == 200
        queue.wait(0.1)                       # agent consumes it
        assert client.post("/signature", json=payload).status_code == 409

    def test_prompt_names_the_patient_and_renders_in_spanish(self, client):
        queue.request_signature("PT-0042", datetime(2026, 8, 14, 14, 0))
        body = client.get("/app").text
        assert "PT-0042" in body      # REQ-10.3: she must see what she is signing for
        assert "Firme" in body
        queue.cancel()


class TestRelayRoute:
    def test_a_token_request_renders_a_code_field_in_spanish(self, client):
        queue.request_token("PT-0042", datetime(2026, 8, 14, 20, 0))
        body = client.get("/app").text
        assert "PT-0042" in body          # she must see which visit this is for
        assert "Token de seguridad" in body
        assert 'name="value"' in body
        queue.cancel()

    def test_the_token_panel_does_not_claim_to_prove_where_she_is(self, client):
        """Her token device travels with her. It is the reason the app never
        runs a location check, not evidence of one — and the natural misreading
        goes the other way, so the page says so where she can see it."""
        queue.request_token("PT-0042", None)
        body = client.get("/app").text
        assert "no indica dónde está usted" in body
        assert "el teléfono está en el edificio" in body
        queue.cancel()

    def test_a_choice_renders_the_apps_own_words(self, client):
        """Relayed, not reworded — she is answering on a screen the controller
        is looking at, and a paraphrase here would be a different screen."""
        queue.request_choice("PT-0042", None, ("GPS", "token de seguridad"))
        body = client.get("/app").text
        assert "GPS" in body and "token de seguridad" in body
        queue.cancel()

    def test_choosing_location_carries_a_warning_about_where_the_phone_is(self, client):
        queue.request_choice("PT-0042", None, ("GPS", "token de seguridad"))
        body = client.get("/app").text
        assert "el teléfono no está con usted" in body
        queue.cancel()

    def test_an_option_the_app_never_offered_is_refused(self, client):
        """The route must not widen what the queue allows."""
        nonce = queue.request_choice("PT-0042", None, ("token de seguridad",))
        r = client.post("/relay", follow_redirects=False,
                        data={"nonce": nonce, "kind": KIND_CHOICE, "value": "GPS"})
        assert r.headers["location"] == "/app?relay=refused"
        assert queue.current() is not None      # still outstanding, not consumed
        queue.cancel()

    def test_an_offered_option_is_carried(self, client):
        nonce = queue.request_choice("PT-0042", None, ("token de seguridad",))
        r = client.post("/relay", follow_redirects=False,
                        data={"nonce": nonce, "kind": KIND_CHOICE,
                              "value": "token de seguridad"})
        assert r.headers["location"] == "/app?relay=sent"
        assert queue.wait(0.5).value == "token de seguridad"

    def test_a_token_is_carried(self, client):
        nonce = queue.request_token("PT-0042", None)
        r = client.post("/relay", follow_redirects=False,
                        data={"nonce": nonce, "kind": KIND_TOKEN, "value": "4821-77"})
        assert r.headers["location"] == "/app?relay=sent"
        assert queue.wait(0.5).value == "482177"

    def test_a_stale_nonce_is_told_so_rather_than_silently_dropped(self, client):
        r = client.post("/relay", follow_redirects=False,
                        data={"nonce": "nope", "kind": KIND_TOKEN, "value": "482177"})
        assert r.headers["location"] == "/app?relay=expired"

    def test_a_signature_cannot_be_posted_through_the_form_route(self, client):
        """One kind, one route. /signature carries strokes and nothing else does."""
        nonce = queue.request_signature("PT-0042", None)
        r = client.post("/relay", follow_redirects=False,
                        data={"nonce": nonce, "kind": KIND_SIGNATURE, "value": "x"})
        assert r.status_code == 400
        queue.cancel()

    def test_the_refusal_reason_does_not_echo_the_token(self, client):
        nonce = queue.request_token("PT-0042", None)
        r = client.post("/relay", follow_redirects=False,
                        data={"nonce": nonce, "kind": KIND_TOKEN,
                              "value": "Alice Example"})
        assert "Alice" not in r.headers["location"]
        queue.cancel()


class TestMirrorPanel:
    """What the controller reports about itself.

    The panel that rendered this is gone: it described the agent's own idea of
    which screen it was on, which drifted from what the phone was actually
    showing often enough to be worse than nothing. The phone view reads the
    screen document instead — the thing the device said.

    The payload survives it, because the socket still carries it and the
    translation rule it is built on is the one worth holding.
    """

    def test_the_stream_payload_carries_translated_text_not_keys(self):
        """The script has no catalog and must never acquire one: a page that
        renders in Spanish until it updates itself into English is worse than
        one that does not update.

        Built rather than streamed — /events is an endless generator by design,
        and a test that reads one frame from it and walks away leaves the stream
        open.
        """
        s = DashboardState(mirror=Mirror(at=datetime.now(), screen="verification",
                                         step="waiting"))
        payload = _mirror_payload(s, Translator("es"))
        assert payload["text_where"] == Translator("es")("mirror.screen.verification")
        assert "mirror.screen." not in payload["text_where"]
        assert "mirror.step." not in payload["text_step"]

    def test_the_stream_payload_omits_text_when_no_language_is_bound(self):
        """/api/state is machine-readable and has no reader to translate for."""
        payload = _mirror_payload(DashboardState())
        assert "text_where" not in payload

    def test_api_state_exposes_the_mirror(self, client):
        body = client.get("/api/state").json()
        assert set(body["mirror"]) >= {"screen", "step", "stale", "screen_at"}
        assert set(body) >= {"relay", "mirror", "signature_pending"}


class TestDeviceAction:
    """/device exists so a sleeping screen doesn't look like a broken feed.

    The safety argument is that the action is a name from an allow-list, not a
    keycode. Waking a screen cannot record a visit; a route forwarding arbitrary
    keycodes could drive the app and do exactly that.
    """

    def test_an_unlisted_action_is_refused(self, client):
        r = client.post("/device", follow_redirects=False,
                        data={"action": "tap"})
        assert r.headers["location"] == "/app?device=failed"

    def test_a_raw_keycode_is_not_an_action(self, client):
        r = client.post("/device", follow_redirects=False,
                        data={"action": "66"})
        assert r.headers["location"] == "/app?device=failed"

    def test_navigation_is_offered_so_no_screen_is_a_dead_end(self, client):
        """A keypad came up with its nav bar outside the tappable set, leaving
        no way back from a thousand miles away."""
        body = client.get("/console").text
        for act in ("wake", "back", "home", "recents"):
            assert f'name="action" value="{act}"' in body

    def test_the_page_still_never_offers_a_raw_keycode(self, client):
        body = client.get("/console").text
        for forbidden in ("keyevent", "keycode", 'value="tap"'):
            assert forbidden not in body


class TestPortal:
    def test_frame_map_is_served_even_before_the_feed_runs(self, client):
        body = client.get("/frame.json").json()
        assert set(body) >= {"id", "size", "elements"}

    def test_a_tap_without_a_frame_is_malformed(self, client):
        r = client.post("/tap", json={"element": {"rid": "x"}})
        assert r.status_code == 400

    def test_a_stale_aim_is_409_and_not_an_error(self, client):
        """409 means "look again", which the page turns into a fresh frame and
        another try — not a failure she has to interpret."""
        r = client.post("/tap", json={"frame": "gone", "element": {"b": [0, 0, 1, 1]}})
        assert r.status_code == 409

    def test_the_page_never_offers_a_coordinate_field(self, client):
        """Coordinates are not accepted anywhere. The element identity is the
        only thing that can be posted, and that is the safety property."""
        body = client.get("/console").text
        for forbidden in ('name="x"', 'name="y"', "input tap"):
            assert forbidden not in body


class TestLiveSocket:
    """One connection, and no page reloads behind it."""

    def test_the_socket_pushes_rendered_html_not_a_catalog(self, client):
        """The client never assembles a sentence. Owning a catalog in the
        browser is how a page ends up half in one language."""
        with client.websocket_connect("/ws") as ws:
            msg = ws.receive_json()
        assert msg["type"] == "state"
        assert "relay_html" in msg
        assert "<" in msg["relay_html"]

    def test_the_pushed_panel_is_in_the_requested_language(self, client):
        client.cookies.set(LANGUAGE_COOKIE, "en")
        with client.websocket_connect("/ws") as ws:
            msg = ws.receive_json()
        assert "Nothing needs your answer" in msg["relay_html"]
        client.cookies.clear()

    def test_a_tap_over_the_socket_is_refused_when_stale(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "tap", "frame": "gone",
                          "element": {"rid": "x", "cls": "y", "b": [0, 0, 1, 1]}})
            while True:
                msg = ws.receive_json()
                if msg.get("type") == "tap_result":
                    break
        assert msg["ok"] is False

    def test_a_tap_without_a_frame_is_malformed_not_attempted(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "tap", "element": {}})
            while True:
                msg = ws.receive_json()
                if msg.get("type") == "tap_result":
                    break
        assert msg["reason"] == "malformed"

    def test_the_http_routes_still_work_without_a_socket(self, client):
        """A socket is an enhancement. She may be on whatever browser her phone
        has, standing in someone's kitchen."""
        assert client.get("/console").status_code == 200
        assert client.get("/frame.json").status_code == 200
        assert client.post("/tap", json={"frame": "x", "element": {}}
                           ).status_code in (400, 409)


class TestMacroRoute:
    """A name from a list, never steps.

    A route that accepted a sequence from a browser would be arbitrary remote
    scripting with a friendlier label, and "the portal cannot do anything she did
    not ask for" would quietly become "the client is well-behaved".
    """

    def test_a_known_macro_is_accepted(self, client, tmp_path):
        with patch("apt_log.macros.REQUEST_PATH", tmp_path / "req.json"):
            r = client.post("/macro", follow_redirects=False,
                            data={"name": "hhax_legacy_login"})
        assert r.headers["location"] == "/app?macro=started"

    def test_an_unknown_macro_is_refused(self, client, tmp_path):
        with patch("apt_log.macros.REQUEST_PATH", tmp_path / "req.json"):
            r = client.post("/macro", follow_redirects=False,
                            data={"name": "sudo-rm-rf"})
        assert r.headers["location"] == "/app?macro=unknown"
        assert not (tmp_path / "req.json").exists()

    def test_the_page_offers_only_registered_macros(self, client):
        """Every name the page offers is one the runner knows. Not the
        converse: the control centre deliberately offers a SUBSET — the
        operational ones. The sign-in walks run themselves when a session
        expires, and a button duplicating that is only useful for pressing at
        a bad moment."""
        import re

        from apt_log import macros

        body = client.get("/console").text
        offered = set(re.findall(r'name="name" value="([a-z_]+)"', body))
        assert offered
        assert offered <= set(macros.MACROS)
        assert offered == set(macros.OPERATIONS)

    def test_the_sign_in_walks_are_not_on_the_page(self, client):
        body = client.get("/console").text
        for name in ("hhax_legacy_login", "hhax_uma_login",
                     "mobile_caregiver_pin"):
            assert f'value="{name}"' not in body

    def test_the_page_says_shortcuts_never_clock_in(self, client):
        """The line, stated where she reads it rather than only in a docstring."""
        body = client.get("/console").text
        assert "registran la entrada" in body


class TestStaleIsVisible:
    """A page that has stopped listening must not look like a quiet one.

    She sat in front of a page frozen at load time with no way to tell: the
    picture was two minutes old and the "Taken" line beside it, rendered once by
    the server, said so without meaning to.
    """

    def test_the_page_carries_an_offline_notice(self, client):
        body = client.get("/app").text
        # The sentence rides in a |tojson block, so its accents are escaped in
        # the source. The colour word is the part that survives verbatim, and
        # it is the part she reads first: red means the page is not live.
        assert "Rojo:" in body
        assert "body.offline" in body

    def test_tapping_is_disabled_while_offline(self, client):
        """Aiming at a frozen screen is how a tap lands somewhere she did not
        choose. The stage stops accepting presses rather than trusting it."""
        body = client.get("/app").text
        assert "body.offline #stage { opacity:.5; pointer-events:none; }" in body

    def test_the_taken_timestamp_is_pushed_not_only_rendered(self):
        """Left server-rendered it ages in place, and a stale timestamp reads as
        a fresh one to anyone not doing the arithmetic.

        Asserted through the translator's own formatter rather than a literal:
        the first version hard-coded "07:38" and failed because the Spanish
        catalog renders a 24-hour clock. A test that assumes one locale's time
        format is testing the catalog, not the behaviour.
        """
        from datetime import datetime
        taken = datetime(2026, 8, 14, 19, 38)
        t = Translator("es")
        s = DashboardState(screenshot_at=taken, mirror=Mirror(at=datetime.now()))
        payload = _mirror_payload(s, t)
        assert "taken_text" in payload
        assert t.time(taken) in payload["taken_text"]

    def test_the_timestamp_is_absent_when_there_is_no_picture(self):
        """"No recent picture" and "taken at some time" are different facts, and
        collapsing them would date a photograph that does not exist."""
        s = DashboardState(screenshot_at=None, mirror=Mirror(at=datetime.now()))
        payload = _mirror_payload(s, Translator("es"))
        assert payload["taken_text"] == Translator("es")("phone.none")


class TestBlindScreenIsUsable:
    """A screen the mirror refuses to photograph still has to be operable.

    The refusal was right and the consequence was not: she was left with one
    unlabelled rectangle where the app had put a dialog, no words, and the
    *previous* screen's capture still being served underneath it — so the boxes
    of this screen sat over the picture of another one.

    Asserted against the phone view, which is where she meets a refused screen.
    The control centre shows the same refusals without the photograph mattering
    to it: it renders the tree, which is present whether or not a picture is.
    """

    def test_the_page_can_name_every_refusal(self, client):
        """The reason crosses as a code, so the sentence has to be on the page
        already. A code with no sentence falls back rather than saying nothing.
        """
        body = client.get("/app").text
        for code in ("no_focus", "login_activity", "password_field",
                     "secure_screen", "capture_failed"):
            assert code + ":" in body
        assert "blockedOther" in body

    def test_a_refused_screen_is_still_operable(self, client):
        """The photograph is what is refused, never the controls. The wireframe
        is built from the tree, which is what the buttons are drawn from."""
        body = client.get("/app").text
        assert "body.blocked" in body
        assert 'id="stage"' in body

    def test_the_app_gets_to_say_what_it_said(self, client, tmp_path):
        """A screen with no picture still has its words, and the control
        centre prints them rather than summarising them — which is the whole
        reason it exists beside the phone view."""
        doc = {"at": "2026-08-18T10:00:00", "app": "com.hhaexchange.caregiver",
               "activity": ".SignatureActivity", "blocked": "secure_screen",
               "elements": [], "size": [1080, 2400],
               "statics": [{"cls": "TextView", "b": [0, 100, 500, 140],
                            "txt": "Firma del paciente"}]}
        (tmp_path / "screen.json").write_text(json.dumps(doc), encoding="utf-8")
        with patch.object(state_mod, "STATE_DIR", tmp_path):
            body = client.get("/console").text
        assert "Firma del paciente" in body

    def test_the_control_centre_reads_the_screen_without_a_picture(self, client):
        """No capture has ever been written — the case of a controller sitting
        on a sign-in screen since boot. The control centre still renders, still
        says there is no picture, and still has somewhere to put the tree."""
        from apt_log.ui import state as state_mod

        with patch.object(state_mod, "SCREENSHOT_PATH",
                          Path("/nonexistent/never-captured.jpg")):
            body = client.get("/console").text
        assert "No hay ninguna imagen reciente" in body
        assert "La pantalla, sin recortar" in body


class TestDeviceOverTheSocket:
    """Back, Home, Recents and Wake, without reloading the page.

    They were the last four controls still posting a form and taking a redirect
    — which is a page navigation by construction. Everything else on this page
    had been live for a while, so pressing Home threw away her scroll position
    and the screen she was watching, mid-visit, four floors from the phone.
    """

    def test_an_allow_listed_action_reaches_the_device_layer(self, client):
        from apt_log import device as device_mod

        with patch.object(device_mod, "send_ui_action") as send:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
                ws.send_json({"type": "device", "action": "home"})
                while True:
                    msg = ws.receive_json()
                    if msg.get("type") == "device_result":
                        break
        assert msg["ok"] is True
        assert send.call_args.args[0] == "home"

    def test_the_socket_is_a_second_door_not_a_second_policy(self, client):
        """The allow-list is checked where it always was. A keycode arriving
        over a socket must be refused by the same code that refuses one arriving
        over a form, or there are two rules and one of them will rot."""
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "device", "action": "KEYCODE_POWER"})
            while True:
                msg = ws.receive_json()
                if msg.get("type") == "device_result":
                    break
        assert msg["ok"] is False

    def test_a_missing_action_is_refused_rather_than_guessed(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "device"})
            while True:
                msg = ws.receive_json()
                if msg.get("type") == "device_result":
                    break
        assert msg["ok"] is False

    def test_the_form_post_still_works_without_a_socket(self, client):
        """She may be on whatever browser her phone has. The form is the
        fallback, not dead code."""
        from apt_log import device as device_mod

        with patch.object(device_mod, "send_ui_action"):
            r = client.post("/device", data={"action": "wake"},
                            follow_redirects=False)
        assert r.status_code == 303


class TestConsoleFormsAreOrdinaryForms:
    """The bug that made this a bug report, and how it stopped being possible.

    Every /device form carries a hidden <input name="action">, and a form
    control named "action" shadows the form's own action property. So
    `form.action` returned that input element, `form.action.endsWith` threw, the
    submit listener died before preventDefault, and the browser posted the form
    for real — reloading the page.

    The script holding that listener is gone with the page it served. The
    control centre intercepts no form at all: every control on it is a POST and
    a redirect, which is why the trap cannot come back. These tests hold that
    property rather than the fix that used to work around it.
    """

    SCRIPT_DIR = Path(__file__).resolve().parents[1] / "src/apt_log/ui/static"

    def test_the_control_centre_never_touches_the_trap(self, client):
        """One form on the page is intercepted — the reboot, to ask first —
        and it reaches for a data attribute rather than for `form.action`,
        which on a form carrying <input name="action"> is the input element."""
        body = client.get("/console").text
        assert "form.action" not in body
        assert body.count("addEventListener('submit'") == 1
        assert "data-confirm" in body

    def test_the_reboot_is_the_only_thing_here_that_asks(self, client):
        """Everything else is one press. A confirmation on a control that can
        be undone by pressing it again is a step people learn to click
        through, which is how the one that matters gets clicked through too.

        Scoped to what this page OFFERS. `update_app` also asks, and is
        deliberately not among the standing operations — it appears on the
        app page only where an update is actually being demanded."""
        import re

        from apt_log import macros

        body = client.get("/console").text
        asked = re.findall(r'data-confirm="[^"]*"[^>]*>\s*<input[^>]*value="([a-z_]+)"',
                           body)
        assert set(asked) == set(macros.CONFIRM) & set(macros.OPERATIONS)
        assert asked == ["restart_phone"]

    def test_the_shadowed_property_is_never_dereferenced_anywhere(self):
        """The phone view does intercept — its controls ride the socket — so
        the trap still applies to it. Comments are stripped first: the fix's
        own comment names the trap in order to explain it, and a guard that
        cannot tell code from prose would forbid describing the bug it exists
        to prevent."""
        for script in self.SCRIPT_DIR.glob("*.js"):
            code = "\n".join(
                line for line in script.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith(("//", "*", "/*")))
            for trap in ("form.action.", "form.action)", "form.action,"):
                assert trap not in code, f"{script.name} dereferences {trap}"

    def test_every_form_that_names_a_field_action_still_posts_plainly(self, client):
        """Names the forms this applies to, so a new one cannot quietly join
        them: four device buttons and pause/resume, which renders one form
        whose value flips rather than two."""
        body = client.get("/console").text
        assert body.count('name="action"') == 5


class TestPauseSurvivesItsOwnPress:
    """One button whose meaning inverts, and nothing was inverting it.

    The server had been pushing `paused` over the socket the whole time and no
    client code listened. So after a Pause the button still read "Pause" and
    still posted `pause`: pressing it again paused a second time, and there was
    no way to resume without reloading the page. Verified against the live
    controller, which is how it was found.

    The control centre answers it differently and more simply: pressing it is a
    POST and a redirect, so the page that comes back is rendered from the state
    the press just produced. There is no window in which the label and the
    action can disagree, because there is no client keeping them in sync.
    """

    def test_the_socket_carries_the_paused_state(self, client):
        with client.websocket_connect("/ws") as ws:
            seen = None
            for _ in range(4):
                msg = ws.receive_json()
                if msg.get("type") == "state" and "paused" in msg:
                    seen = msg["paused"]
                    break
        assert seen is not None

    def test_the_label_and_the_action_agree_in_both_states(self, client):
        for paused, word, action in ((False, "Pausar", "pause"),
                                     (True, "Reanudar", "resume")):
            with patch.object(state_mod, "is_paused", return_value=paused):
                body = client.get("/console").text
            assert f'value="{action}"' in body
            assert word in body

    def test_a_paused_schedule_says_so_on_the_page(self, client):
        with patch.object(state_mod, "is_paused", return_value=True):
            body = client.get("/console").text
        assert "El programa está en pausa" in body

    def test_pressing_it_comes_back_to_the_control_centre(self, client):
        r = client.post("/control", data={"action": "resume"},
                        follow_redirects=False)
        assert r.headers["location"] == "/console"


class TestShellNeverGoesStale:
    """Seen live on an iPhone: a cached page shell from one deploy rendering
    the next deploy's fragments — new markup, none of the new styles, and it
    reads as a broken design rather than a cache.
    """

    def test_the_pages_are_never_cached(self, client):
        for path in ("/", "/app"):
            assert client.get(path).headers.get("cache-control") == "no-store"

    def test_the_shell_and_the_socket_share_a_boot_id(self, client, tmp_path):
        from apt_log.ui.app import BOOT_ID

        body = client.get("/app").text
        assert f'data-boot="{BOOT_ID}"' in body
        with patch.object(state_mod, "STATE_DIR", tmp_path):
            with client.websocket_connect("/ws") as ws:
                msg = ws.receive_json()
        assert msg["boot"] == BOOT_ID

    def test_assets_are_stamped_with_it(self, client):
        from apt_log.ui.app import BOOT_ID

        for path in ("/", "/app"):
            assert f"?v={BOOT_ID}" in client.get(path).text

    def test_the_clients_reload_on_a_mismatch(self):
        for name in ("phone.js",):
            js = (Path(__file__).resolve().parents[1]
                  / "src/apt_log/ui/static" / name).read_text(encoding="utf-8")
            assert "location.reload()" in js
            assert "aptlog-reloaded" in js     # and the loop guard with it

    def test_every_page_says_which_build_drew_it(self, client):
        """THE PAGE HAD NO WAY TO SAY WHICH BUILD IT WAS. A fix was deployed
        and verified, and a screenshot still showed the bug — with nothing on
        screen, on either side, able to settle whether that page was running
        the fix. "Boot id idea is great let's add that globally so we can see
        anywhere on the app.\""""
        from apt_log.ui.app import BOOT_ID

        for path in ("/app", "/console"):
            body = client.get(path).text
            assert 'class="buildstamp"' in body, path
            assert BOOT_ID in body, path

    def test_the_stamp_cannot_take_a_tap_meant_for_the_page(self):
        """It is fixed over the header band on every view, so a diagnostic
        that could swallow a press on Back would be worse than no diagnostic.
        """
        css = (Path(__file__).resolve().parents[1]
               / "src/apt_log/ui/static/design.css").read_text(encoding="utf-8")
        rule = css[css.index(".buildstamp {"):]
        assert "pointer-events:none" in rule[:rule.index("}")]

    def test_the_stamp_is_anchored_per_page_because_the_bands_differ(self):
        """One position cannot be clean on both. Centred at the top is the
        empty middle of every header row in /app — and on /console it lands
        straight through "Centro de control", which is what the browser
        showed. The console bar is full and that page has no fixed chrome at
        the bottom, so the stamp goes there instead."""
        css = (Path(__file__).resolve().parents[1]
               / "src/apt_log/ui/static/design.css").read_text(encoding="utf-8")
        assert "body[data-view] .buildstamp" in css
        assert "body:not([data-view]) .buildstamp" in css
        # /app is the only one of the two whose body carries data-view, which
        # is what the two selectors turn on.
        phone = (Path(__file__).resolve().parents[1]
                 / "src/apt_log/ui/templates/phone.html").read_text(encoding="utf-8")
        console = (Path(__file__).resolve().parents[1]
                   / "src/apt_log/ui/templates/console.html").read_text(encoding="utf-8")
        assert "data-view=" in phone
        assert "data-view=" not in console.split("<body")[1].split(">")[0]

    def test_the_stamp_is_shared_rather_than_per_page(self):
        """"Globally" means one rule in the stylesheet both pages already
        load — not a copy per template that the next page forgets."""
        css = (Path(__file__).resolve().parents[1]
               / "src/apt_log/ui/static/design.css").read_text(encoding="utf-8")
        assert ".buildstamp {" in css
        for name in ("phone.html", "console.html"):
            tpl = (Path(__file__).resolve().parents[1]
                   / "src/apt_log/ui/templates" / name).read_text(encoding="utf-8")
            assert ".buildstamp {" not in tpl, name

    def test_a_page_that_did_not_reload_says_so_instead_of_looking_current(self):
        """The reload is guarded against loops and can decline to fire. When
        it does, old code keeps running on a page that looks entirely current
        — the exact failure this stamp exists to make visible — so the mark
        goes on BEFORE the reload is attempted, not after."""
        js = (Path(__file__).resolve().parents[1]
              / "src/apt_log/ui/static/phone.js").read_text(encoding="utf-8")
        mark = js.index("stamp.classList.add('behind')")
        guard = js.index("aptlog-reloaded")
        assert mark < guard
        # And it names both builds, so the two ids can be compared by eye.
        assert "body.dataset.boot + ' → ' + msg.boot" in js


class TestTheCodeBarClearsThePhonesControls:
    """Reported from the field: the OTP input sat so close to the controls
    that Cancel pressed Home, which leaves the app view — and with it the
    screen the code was for.

    The cause was two constants standing in for one measurement. The pill
    alone is one height; the pill under the app's own tab row is another, and
    the tab row comes and goes with the screen. Both numbers were written
    from the no-tabs case, so on the schedule — which has tabs every day —
    the type bar landed on the pill and the content ran under both.
    """

    SCRIPT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/phone.js")

    def test_the_bar_is_placed_from_a_measurement(self, client):
        """Comments are stripped first: the rule's own comment quotes the
        constant it replaced in order to explain it, and a guard that cannot
        tell code from prose forbids describing the bug it prevents — the
        same trap the form.action guard already documents."""
        import re

        body = client.get("/app").text
        assert "var(--chrome-h" in body
        css = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        assert "bottom:88px" not in css

    def test_the_content_tail_clears_the_same_measurement(self, client):
        body = client.get("/app").text
        assert "padding:2px 0 calc(var(--chrome-h" in body

    def test_the_measurement_follows_the_tab_row_appearing(self):
        """The tab row is what changes the height, so the height has to be
        re-read when it does — not once at load."""
        source = self.SCRIPT.read_text(encoding="utf-8")
        assert "ResizeObserver" in source
        assert "--chrome-h" in source

    def test_the_phones_controls_go_inert_while_she_types(self, client):
        """The real guarantee. Spacing makes a mis-tap unlikely; this makes
        it harmless — Cancel is the only live control down there while the
        bar is open."""
        body = client.get("/app").text
        assert "body.typing .navbar" in body
        assert "pointer-events:none" in body

    def test_the_client_re_registers_what_it_holds_on_every_load(self):
        """The server can lose a subscription the browser still has — a
        pruned store, a lost file, a mistake in the sender — and nothing in
        the browser would notice: it holds a valid subscription, asks for
        nothing, and the phone goes quiet. Exactly that happened here, and
        the recovery was asking a person to tap a toggle a third time."""
        source = self.SCRIPT.read_text(encoding="utf-8")
        assert "existing.toJSON()" in source

    def test_the_client_sets_and_clears_that_state(self):
        source = self.SCRIPT.read_text(encoding="utf-8")
        assert "classList.add('typing')" in source
        assert "classList.remove('typing')" in source

    def test_the_peek_arrows_clear_the_chrome_by_measurement(self, client):
        """Reported from the field with the overlap circled: on a screen with
        an app tab row the scroll arrows sat on top of Back and Home.

        Same fault as this class's own bug and the same fix — 102px was
        written from the no-tab-row case, and the chrome is 134px with tabs.
        Comments stripped first, because the rule's comment quotes the
        constant it replaced."""
        import re

        body = client.get("/app").text
        css = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        arrows = re.search(r"#phone-scroll\s*\{[^}]*\}", css)
        assert arrows, "the peek's scroll control should still be styled"
        assert "var(--chrome-h" in arrows.group(0)
        assert "102px + env" not in css

    def test_the_peek_arrows_appear_only_on_a_screen_that_scrolls(self, client):
        """The density is tuned so most screens fit whole, which makes a
        permanent pair of arrows furniture that does nothing — and furniture
        sitting over the phone's own controls at that. The phone is the only
        thing that knows, so it says."""
        body = client.get("/app").text
        # And not while a system panel covers the app: nothing scrolls behind
        # the shade, and the arrows would be aiming at it rather than at her
        # page.
        assert ("body.peeking:not(.asleep):not(.covered).scrolls #phone-scroll"
                in body)
        # The flag has to survive the whole way: the phone's own attribute →
        # the fragment → a class the CSS above can see.
        fragment = (Path(__file__).resolve().parents[1]
                    / "src/apt_log/ui/templates/_screen.html"
                    ).read_text(encoding="utf-8")
        assert "data-scrolls" in fragment
        source = self.SCRIPT.read_text(encoding="utf-8")
        assert "dataset.scrolls" in source
        assert "'scrolls'" in source

    def test_a_screen_that_scrolls_says_so_in_the_fragment(self):
        """End of the same chain, from the model rather than the markup."""
        from apt_log.ui.screenview import build

        doc = {"id": "f", "elements": [], "statics": [], "size": [720, 1600],
               "scrollable": True}
        assert build(doc)["scrollable"] is True
        doc["scrollable"] = False
        assert build(doc)["scrollable"] is False

    def test_the_code_box_is_not_inside_the_controls_that_go_inert(self, client):
        """The fault behind two field reports, and the only one that mattered.

        The bar was a CHILD of <nav class="navbar">, which is
        `pointer-events:none` so the phone's screen stays touchable around the
        floating pill — only `.pill` turns them back on. So the input, Send
        and Cancel took no touches at all, and `body.typing .navbar *` then
        forced them inert and dimmed them to 30% on top of that: the rule
        written to protect the code box was landing on the code box.

        Checked in a real browser at iPhone size with elementFromPoint before
        this was written — on the old markup a tap on the box landed on
        `stage`, the phone screen behind it. This holds the structure that
        made it true, because the CSS above cannot reach what is not a child.
        """
        import re

        body = client.get("/app").text
        nav = re.search(r'<nav class="navbar".*?</nav>', body, flags=re.S)
        assert nav, "the phone's control bar should still be there"
        assert 'id="typebar"' not in nav.group(0)
        assert 'id="typebar"' in body          # and it is still on the page

    def test_the_code_box_says_it_takes_touches(self, client):
        """Stated rather than inherited, so moving it again cannot silently
        take it away — which is exactly how it was lost the first time."""
        import re

        body = client.get("/app").text
        assert re.search(r"\.typebar\s*\{[^}]*pointer-events:\s*auto", body)
        assert ".typebar * { pointer-events:auto; }" in body

    def test_the_bar_is_not_measured_by_the_thing_it_is_placed_against(
            self, client):
        """`measureChrome()` measures `.navbar` to place the bar above it. With
        the bar inside the nav, opening it grew the element its own position is
        derived from — a feedback loop through the ResizeObserver."""
        import re

        body = client.get("/app").text
        nav = re.search(r'<nav class="navbar".*?</nav>', body, flags=re.S)
        assert "typebox" not in nav.group(0)

    def test_the_field_is_focused_inside_the_tap_that_opened_it(self):
        """Reported from the field: the bar rendered and could not be typed
        into — read, reasonably, as "greyed out".

        iOS raises the keyboard only for a `focus()` that runs while a user
        gesture is still on the stack. The call sat in a `setTimeout`, so the
        field took focus and drew its ring and no keyboard ever came up: a bar
        that looks live, is live, and cannot accept a character. The fix is
        one synchronous statement, and this is the guard that it stays one —
        a focus reachable only from a callback would pass a substring check
        while failing on the phone.
        """
        import re

        source = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        body = source.split("function openTypeBar", 1)[1].split("\n  }", 1)[0]
        statements = [line.strip() for line in body.splitlines()]
        assert "box.focus();" in statements, (
            "openTypeBar must focus the box as a plain statement, not only "
            "from inside a callback")

    def test_the_old_deferred_focus_is_gone(self):
        source = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        assert "setTimeout(() => box.focus(), 50)" not in source


class TestPhoneBoundaries:
    """Every page respects the device's own chrome.

    Opened from the installed app the dashboard renders edge-to-edge, and its
    header sat under the iOS status bar — the page title colliding with the
    clock and battery. Safe-area insets are not an /app nicety; they are what
    keeps any page out from under hardware it does not own.
    """

    def test_every_page_pads_for_the_status_bar(self, client):
        for path in ("/", "/app"):
            body = client.get(path).text
            assert "env(safe-area-inset-top)" in body, path
            assert "env(safe-area-inset-bottom)" in body, path
            assert "viewport-fit=cover" in body, path


class TestSketchMismatchIsStale:
    """`app` is the focus of this moment; `h_app` is whose screen the
    rendered elements were read under. When they disagree an app switch is
    in progress and the sketch on the page is the old app's — amber and
    dimmed, never a green Live with the launcher's rows dressed in the new
    app's name."""

    def _doc(self, **over):
        import time as _t
        from datetime import datetime as _dt
        doc = {"at": _dt.now().isoformat(), "h_at": _t.time(),
               "app": "com.hhaexchange.caregiver",
               "h_app": "com.hhaexchange.caregiver"}
        doc.update(over)
        return doc

    def test_a_sketch_of_another_app_is_never_live(self):
        from apt_log.ui.app import _screen_is_stale
        assert _screen_is_stale(
            self._doc(h_app="com.android.launcher3")) is True

    def test_agreement_is_live(self):
        from apt_log.ui.app import _screen_is_stale
        assert _screen_is_stale(self._doc()) is False

    def test_a_doc_from_before_the_field_is_not_condemned(self):
        """Old documents carry no h_app; unknown is not a mismatch, or the
        first minute after every deploy would open on amber."""
        from apt_log.ui.app import _screen_is_stale
        assert _screen_is_stale(self._doc(h_app="")) is False


class TestScreenChurnDoesNotRepaint:
    """h_at moves on every hierarchy read and img on any repainted pixel —
    the phone's own clock is enough. Comparing the raw document re-sent the
    wireframe every second over an unchanged screen: a steady shimmer, and
    a scrolled page snapping back to the top."""

    def _doc(self, **over):
        doc = {"id": "abc", "at": "2026-08-16T00:00:00", "h_at": 1.0,
               "img": "aaa", "app": "com.hhaexchange.uma",
               "h_app": "com.hhaexchange.uma", "screen": "home",
               "blocked": "", "notice": "", "elements": [], "statics": []}
        doc.update(over)
        return doc

    def test_churn_alone_does_not_change_the_render_key(self):
        from apt_log.ui.app import _render_key
        a = self._doc()
        b = self._doc(at="2026-08-16T00:00:02", h_at=3.4, img="bbb")
        assert _render_key(a) == _render_key(b)

    def test_a_real_change_does(self):
        from apt_log.ui.app import _render_key
        a = self._doc()
        for changed in (self._doc(blocked="password_field"),
                        self._doc(h_app="com.android.chrome"),
                        self._doc(elements=[{"rid": "x"}])):
            assert _render_key(a) != _render_key(changed)


class TestControlsTheAppNeverNamed:
    """Reported from the field twice: the agency filter draws as a blank area
    and the refresh beside it as `···`.

    Neither has a name the reflow is allowed to print. The filter is an
    EditText whose placeholder Android counts as CONTENT (`showing-hint` is
    literally false on the live phone), so the rule that keeps a typed code
    out of the screen document strips it. The refresh is a bare View that
    Android itself flags `NAF="true"`: no id, no text, no description.

    The identification lives in `apt_log.controls` beside the raw text,
    because the same table decides what a box IS and whether its words may be
    shown, and those two answers must not drift apart.
    """

    PKG = "com.inmyteam.inmyteam"
    SIZE = (720, 1600)
    FILTER = ('<node class="android.widget.EditText" resource-id="" text="" '
              'clickable="true" bounds="[13,106][668,143]"/>')
    REFRESH = ('<node class="android.view.View" resource-id="" text="" '
               'clickable="true" bounds="[678,109][710,141]"/>')
    LIST = ('<node class="android.widget.TextView" text="Today" '
            'bounds="[70,191][106,206]"/>')

    def _keys(self, xml, package=None):
        from apt_log import feed

        return {e.get("name_key") for e in
                feed.elements(xml, label=True, package=package or self.PKG,
                              size=self.SIZE)} - {None}

    def test_the_filter_and_the_refresh_are_named(self):
        keys = self._keys(self.FILTER + self.REFRESH + self.LIST)
        assert keys == {"papp.imt.agency_filter", "papp.imt.refresh"}

    def test_the_code_box_is_never_called_a_filter(self):
        """The one that matters. A code screen also has a single unnamed
        EditText, and §12 already paid for mistaking one inMyTeam screen for
        another — there the walk would have typed a phone number over a
        part-entered code."""
        code = ('<node class="android.widget.TextView" '
                'text="Verify Your Account" bounds="[0,296][720,340]"/>'
                + self.FILTER)
        assert self._keys(code) == set()

    def test_the_words_alone_are_enough_to_refuse(self):
        xml = ('<node class="android.widget.TextView" text="Enter your code" '
               'bounds="[48,1057][125,1073]"/>' + self.FILTER)
        assert self._keys(xml) == set()

    def test_the_band_alone_is_enough_to_refuse(self):
        """A field down the page is not the filter — the filter sits under
        the title bar, the code box in the middle."""
        low = self.FILTER.replace("[13,106][668,143]", "[13,1041][668,1083]")
        assert self._keys(low + self.LIST) == set()

    def test_another_app_is_left_alone(self):
        assert self._keys(self.FILTER + self.LIST,
                          package="com.hhaexchange.mobile") == set()

    def test_a_control_that_names_itself_is_not_renamed(self):
        named = self.FILTER.replace('resource-id=""',
                                    'resource-id="app:id/search"')
        assert self._keys(named + self.LIST) == set()

    def test_the_key_is_resolved_into_the_text_the_page_prints(self):
        """`label_keys` runs at render time, not build time — the model is
        language-free until somebody asks for it in a language, which is what
        lets the same document render Spanish for her and English for whoever
        is helping her."""
        from apt_log import feed
        from apt_log.ui.i18n import Translator
        from apt_log.ui.screenview import build, label_keys

        xml = self.FILTER + self.LIST
        doc = {"id": "f", "size": list(self.SIZE), "h_app": self.PKG,
               "elements": feed.elements(xml, label=True, package=self.PKG,
                                         size=self.SIZE),
               "statics": feed.statics(xml)}
        for lang, wanted in (("es", "Filtrar por agencia"),
                             ("en", "Filter by agency")):
            m = label_keys(build(doc), Translator(lang))
            printed = [it.get("txt") for row in m["rows"]
                       for it in row["items"]]
            assert wanted in printed


class TestTheAppsOwnUpArrowDoesNotRideAlong:
    """Reported from the field: "we have Navigate up and we have Back".

    Two controls, one meaning, three inches apart, in different words — a
    question to stop and answer mid-visit. The pill's Back sends the phone's
    own Back, which is what the arrow does, so there is nothing to chain it
    to that is not already there; it is simply not published.

    Only a control that SAYS it goes up is dropped. The top-left button is
    otherwise whatever the app put there, and this project has already been
    bitten by assuming it is Back — on HHAeXchange+ it was the help button,
    drawn with a chevron, and pressing it opened a documentation site.
    """

    W, H = 720, 1600

    def _doc(self, buttons):
        return {"id": "f", "size": [self.W, self.H],
                "h_app": "com.inmyteam.inmyteam",
                "elements": [dict(b, rid="", has_text=True) for b in buttons],
                "statics": [{"txt": "Today Visits", "b": [52, 76, 127, 94]}]}

    def _nav(self, buttons):
        from apt_log.ui.screenview import build

        return build(self._doc(buttons))["nav"]

    UP = {"cls": "ImageButton", "b": [5, 64, 42, 106], "txt": "Navigate up"}

    def test_navigate_up_is_not_published(self):
        nav = self._nav([self.UP])
        assert nav["back"] is None and nav["trailing"] == []

    @pytest.mark.parametrize("caption", ["Navigate up", "Back", "Atrás",
                                         "Volver", "navigate back"])
    def test_every_way_an_app_says_back(self, caption):
        nav = self._nav([dict(self.UP, txt=caption)])
        assert nav["back"] is None

    @pytest.mark.parametrize("caption", ["Anular", "Add Notes",
                                         "Volver a la lista"])
    def test_controls_that_are_not_back_stay(self, caption):
        """Anular is the signature screen's cancel and must never be quietly
        dropped; "Volver a la lista" is a link and matched on the WHOLE
        caption, not a substring."""
        nav = self._nav([dict(self.UP, txt=caption)])
        assert nav["back"] is not None
        assert nav["back"]["txt"] == caption

    @pytest.mark.parametrize("caption", ["Help", "Ayuda", "Información"])
    def test_help_does_not_ride_along_either(self, caption):
        """On the owner's instruction: "the question marks also sometimes
        confuse me, not a useful thing on our end honestly — I don't see me
        or my sister ever pressing that". It opens a documentation website,
        which is the one place the phone may not go, and unlabelled in the
        corner it has been taken for Back."""
        assert self._nav([dict(self.UP, txt=caption)])["back"] is None

    # The bar HHAeXchange+ actually ships, at the bounds recorded on the
    # phone: two textless buttons whose only names are descriptions folded
    # in as lines. Reading `txt` alone meant the word lists never saw a
    # single one of them, and both came through as "···" bubbles in the
    # corner — "these bubbles on the top right with … are confusing".
    def _uma_bar(self, statics):
        from apt_log.ui.screenview import build

        return build({
            "id": "f", "size": [720, 1600], "blocked": "", "notice": "",
            "elements": [
                {"rid": "menu_top_bar_back_button", "cls": "View",
                 "b": [6, 68, 31, 93], "txt": "", "focused": False,
                 "checked": False},
                {"rid": "help_button", "cls": "View", "b": [691, 68, 718, 94],
                 "txt": "", "focused": False, "checked": False},
            ],
            "statics": [{"cls": "TextView", "txt": "Visitas de búsqueda",
                         "b": [310, 73, 410, 88]}] + statics,
        })["nav"]

    def test_neither_survives_when_only_a_description_names_them(self):
        nav = self._uma_bar([
            {"cls": "View", "txt": "Atrás", "b": [6, 68, 31, 93]},
            {"cls": "View", "txt": "Ayuda", "b": [691, 69, 718, 93]},
        ])
        assert nav is not None
        assert nav["back"] is None and nav["trailing"] == []

    def test_and_the_page_keeps_its_title(self):
        """Dropping the buttons must not drop the bar."""
        nav = self._uma_bar([
            {"cls": "View", "txt": "Atrás", "b": [6, 68, 31, 93]},
            {"cls": "View", "txt": "Ayuda", "b": [691, 69, 718, 93]},
        ])
        assert nav["title"] == "Visitas de búsqueda"

    def test_the_help_id_alone_is_enough(self):
        """Some pages ship it with no description at all."""
        nav = self._uma_bar([{"cls": "View", "txt": "Atrás",
                              "b": [6, 68, 31, 93]}])
        assert nav["back"] is None and nav["trailing"] == []

    def test_the_help_ROW_inside_a_menu_page_is_untouched(self):
        """A destination she chose to walk to, not a bubble in the chrome.
        HHAeXchange+'s Menú lists Ayuda between Agencias and Idioma."""
        from apt_log.ui.screenview import build

        model = build({"id": "f", "size": [720, 1600], "blocked": "",
                       "notice": "",
                       "elements": [{"rid": "menu_screen_help", "cls": "View",
                                     "b": [14, 342, 706, 369], "txt": "",
                                     "focused": False, "checked": False}],
                       "statics": [{"cls": "TextView", "txt": "Ayuda",
                                    "b": [104, 349, 684, 362]}]})
        said = [line for row in model["rows"] for it in row["items"]
                for line in (it.get("lines") or [])]
        assert "Ayuda" in said

    def test_the_rest_of_the_bar_survives_losing_it(self):
        other = {"cls": "Button", "b": [600, 64, 700, 106], "txt": "Add Notes"}
        nav = self._nav([self.UP, other])
        kept = ([nav["back"]] if nav["back"] else []) + nav["trailing"]
        assert [k["txt"] for k in kept] == ["Add Notes"]

    def test_the_page_still_renders_with_no_nav_buttons_left(self, client):
        """The template used to build `[m.nav.back] + trailing`, which is a
        crash the moment back is None."""
        from apt_log.ui.screenview import build, label_keys
        from apt_log.ui.i18n import Translator
        from apt_log.ui.app import templates

        model = label_keys(build(self._doc([self.UP])), Translator("en"))
        html = templates.get_template("_screen.html").render(
            m=model, t=Translator("en"))
        assert "Today Visits" in html
        assert "Navigate up" not in html


class TestTheLanguageSwitchOnTheHomePage:
    """Reported from the field: the EN/ES buttons are "not very responsive or
    don't work at all". Both halves were real, and the second was total.

    `wireForms` intercepts every POST form on the page and re-posts it with
    `fetch`. It was written for the relay panel, whose answers come back over
    the socket and need no reload. It also took the language switch — and a
    submit button's name/value IS NOT IN `FormData(form)`, by spec. So the
    browser sent `next=/app` with no `language` at all, the server answered
    422, and nothing happened. Caught by reading the POST body in a real
    browser, which is the only place this path exists.

    Even had the field survived, posting it in the background would have
    stored the choice and left every word on screen unchanged — which is the
    same button-does-nothing from the other direction. It navigates now.
    """

    SCRIPT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/phone.js")

    def test_the_switch_is_marked_to_navigate(self, client):
        import re

        body = client.get("/app").text
        form = re.search(r'<form[^>]*action="/language"[^>]*>', body)
        assert form, "the language switch should still be a plain form"
        assert "data-navigate" in form.group(0)

    def test_the_live_wiring_leaves_navigating_forms_alone(self):
        source = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        assert "form.dataset.navigate" in source

    def test_an_intercepted_form_keeps_the_pressed_buttons_value(self):
        """The bug class, not just this instance: any form whose value lives
        on the submit button would have died the same way."""
        source = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        assert "ev.submitter" in source
        assert "data.append(hit.name, hit.value)" in source

    def test_the_buttons_are_big_enough_to_hit(self, client):
        """Measured at 43x26 in a real browser. 44 is the smallest target a
        thumb hits reliably, and half of "not very responsive" was simply
        having to aim."""
        import re

        body = client.get("/app").text
        css = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        rule = re.search(r"#launcher \.seg button \{[^}]*\}", css)
        assert rule, "the switch should still be styled"
        assert "min-height:44px" in rule.group(0)
        assert "min-width:56px" in rule.group(0)

    def test_the_route_still_answers_a_plain_post(self, client):
        """What the browser now sends, unmediated."""
        r = client.post("/language", data={"language": "en", "next": "/app"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/app"
        assert LANGUAGE_COOKIE in r.cookies

    def test_a_post_without_the_language_is_refused_not_ignored(self, client):
        """The 422 the browser was getting. Worth pinning: a route that
        silently accepted a missing field would have hidden this for longer."""
        r = client.post("/language", data={"next": "/app"},
                        follow_redirects=False)
        assert r.status_code == 422


class TestTheFilterShowsWhatItIsSetTo:
    """Reported from the field: after choosing an agency the phone shows the
    agency and the portal still says "Filtrar por agencia".

    The chosen value lands in the same EditText whose text is withheld —
    correctly, everywhere else, because editable text is what has been typed
    and on a credential screen that is a password or the code just texted.
    The exception is narrow and lives in `apt_log.controls`: a control that
    table has positively identified, by app and shape and place, on a screen
    not asking for a credential, is a CHOOSER. Its content was picked from a
    list the app drew.

    Identification moved next to the raw text on purpose. The same object
    decides what a box IS and whether its words may be shown, so the two
    answers cannot drift apart.
    """

    PKG = "com.inmyteam.inmyteam"
    SIZE = (720, 1600)
    FILTER = ('<node class="android.widget.EditText" resource-id="" '
              'text="{}" clickable="true" bounds="[13,146][707,183]"/>')

    def _first(self, xml, package=None):
        from apt_log import feed

        return feed.elements(xml, label=True, package=package or self.PKG,
                             size=self.SIZE)[0]

    def test_the_chosen_agency_is_shown(self):
        el = self._first(self.FILTER.format("HOME CARE ON CALL, LLC"))
        assert el["txt"] == "HOME CARE ON CALL, LLC"

    def test_an_empty_filter_still_gets_the_portals_name(self):
        el = self._first(self.FILTER.format(""))
        assert el["name_key"] == "papp.imt.agency_filter"
        assert el["txt"] == ""

    def test_a_typed_code_is_never_shown(self):
        """The line this whole exception is balanced against. A code screen
        carries one unnamed EditText too, and its contents are the thing this
        system exists to keep out of the document."""
        code = ('<node class="android.widget.TextView" '
                'text="Verify Your Account" bounds="[0,296][720,340]"/>'
                + self.FILTER.format("863048"))
        el = self._first(code)
        assert el.get("txt") is None
        assert not el.get("name_key")

    def test_the_words_of_the_walk_are_enough_on_their_own(self):
        for marker in ("Enter your code", "Sign in with your phone number"):
            xml = (f'<node class="android.widget.TextView" text="{marker}" '
                   'bounds="[0,296][720,340]"/>'
                   + self.FILTER.format("secret"))
            assert self._first(xml).get("txt") is None

    def test_another_apps_field_is_not_disclosed(self):
        el = self._first(self.FILTER.format("whatever"),
                         package="com.hhaexchange.mobile")
        assert el.get("txt") is None

    def test_a_field_the_app_named_is_not_disclosed(self):
        """The table only knows the NAMELESS one. A field with an id is some
        other control and its contents stay withheld."""
        named = self.FILTER.format("typed").replace(
            'resource-id=""', 'resource-id="app:id/search"')
        assert self._first(named).get("txt") is None


class TestTheAppsChromeSpeaksHerLanguage:
    """Reported from the field: not everything on the page is in Spanish.

    Most of what is left is the app's own WORDS — "Today", the tab captions,
    a patient's name — and those stay exactly as the app says them, because
    translating a live care app's content would mean inventing words the
    record does not contain.

    The drawer handle is not that. "Open navigation drawer" is Android's own
    description, inherited untranslated, and it is chrome: the portal owns its
    chrome and says it in her language. Renamed for every app, not just this
    one.
    """

    def _nav_words(self, lang):
        from apt_log import feed
        from apt_log.ui.screenview import build, label_keys
        from apt_log.ui.i18n import Translator

        xml = ('<node class="android.widget.ImageButton" resource-id="" '
               'content-desc="Open navigation drawer" clickable="true" '
               'bounds="[5,64][42,106]"/>'
               '<node class="android.widget.TextView" text="Visits" '
               'bounds="[52,76][88,94]"/>')
        doc = {"id": "x", "size": [720, 1600],
               "h_app": "com.inmyteam.inmyteam",
               "elements": feed.elements(xml, label=True,
                                         package="com.inmyteam.inmyteam",
                                         size=(720, 1600)),
               "statics": feed.statics(xml)}
        m = label_keys(build(doc), Translator(lang))
        nav = m["nav"]
        return [b.get("txt") for b in
                ([nav["back"]] if nav.get("back") else []) + nav["trailing"]]

    def test_the_drawer_is_named_in_spanish(self):
        assert self._nav_words("es") == ["Menú"]

    def test_and_in_english(self):
        assert self._nav_words("en") == ["Menu"]

    def test_a_chrome_name_replaces_the_apps_caption(self):
        """Unlike the filter, where the app's words are the ANSWER and the
        portal's name only fills a blank."""
        from apt_log import controls

        assert controls.replaces(controls.DRAWER) is True
        assert controls.replaces(controls.AGENCY_FILTER) is False


class TestNamingSurvivesAWalkedPage:
    """The fix shipped and the filter was still blank, because elements are
    built in TWO places and only one of them was taught.

    A screen the portal has walked end to end is published from the stitch
    cache, not from the viewport capture — and the walk's own capture called
    `elements()` with no package and no size, so every portal name came back
    empty. Today Visits is walked, which is why the one screen the report kept
    coming from was the one screen that could not work.
    """

    def test_without_the_screens_context_nothing_is_named(self):
        """The failure mode itself, pinned. Naming needs to know which app and
        how big the screen is; absent either, it names nothing rather than
        guessing — which is the safe direction and also the silent one."""
        from apt_log import feed

        xml = ('<node class="android.widget.EditText" resource-id="" text="" '
               'clickable="true" bounds="[13,146][707,183]"/>')
        assert not feed.elements(xml, label=True)[0].get("name_key")
        named = feed.elements(xml, label=True,
                              package="com.inmyteam.inmyteam",
                              size=(720, 1600))[0]
        assert named["name_key"] == "papp.imt.agency_filter"

    def test_the_walk_hands_the_context_through(self):
        """Source-level, because the walk needs a live driver — but the thing
        that broke was exactly this call losing its arguments."""
        import re

        source = (Path(__file__).resolve().parents[1]
                  / "src/apt_log/macros.py").read_text(encoding="utf-8")
        call = re.search(r"feed_mod\.elements\(src,[^)]*\)", source)
        assert call, "the walk should still build elements from the source"
        assert "package=package" in call.group(0)
        # `screen_wh`, never `size` — see TestABadSizeNamesNothingRatherThanCrashing.
        assert "size=screen_wh" in call.group(0)

    def test_a_size_that_cannot_be_read_names_nothing(self):
        from apt_log import macros

        class Broken:
            def get_window_size(self):
                raise RuntimeError("no session")

        assert macros._screen_size(Broken()) == (0, 0)


class TestABadSizeNamesNothingRatherThanCrashing:
    """A whole page scan died on `"height" * 0.12`.

    The walk's own function rebinds `size` to the driver's
    {"width": …, "height": …} dict a few lines below where the naming's size
    was set, and `capture` is a closure that reads it at CALL time. So naming
    was handed a dict, unpacked its KEYS, and multiplied the string "height"
    by a float. The rescan failed outright and the page never rebuilt.

    The name collision is fixed. This pins the other half: three callers feed
    this a size, and none of them should be able to take a scan down.
    """

    XML = ('<node class="android.widget.EditText" resource-id="" text="" '
           'clickable="true" bounds="[13,146][707,183]"/>')

    @pytest.mark.parametrize("bad", [
        {"width": 720, "height": 1600},          # the one that actually broke
        None, "nope", (), (720,), (720, 1600, 1), ("720", "1600"),
    ])
    def test_a_size_that_is_not_a_pair_of_numbers_names_nothing(self, bad):
        from apt_log import feed

        el = feed.elements(self.XML, label=True,
                           package="com.inmyteam.inmyteam", size=bad)[0]
        assert not el.get("name_key")
        assert el.get("txt") is None      # and discloses nothing either

    @pytest.mark.parametrize("good", [(720, 1600), [720, 1600]])
    def test_a_real_size_still_names(self, good):
        from apt_log import feed

        el = feed.elements(self.XML, label=True,
                           package="com.inmyteam.inmyteam", size=good)[0]
        assert el["name_key"] == "papp.imt.agency_filter"

    def test_the_walk_does_not_reuse_the_drivers_own_size_name(self):
        """The collision itself. `capture` closes over the enclosing scope, so
        a name reused later in the same function is read at call time."""
        import re

        source = (Path(__file__).resolve().parents[1]
                  / "src/apt_log/macros.py").read_text(encoding="utf-8")
        call = re.search(r"feed_mod\.elements\(src,[^)]*\)", source, re.S)
        assert "size=size" not in call.group(0)
        assert "size=screen_wh" in call.group(0)


class TestTheCoveredCard:
    """One button, shown only while there is something for it to do.

    Asked for after the shade sat over inMyTeam and the portal had nothing to
    press: "maybe we need a macro on the phone peek we can press if we run
    into edge cases like this?" Contextual rather than permanent on purpose —
    a standing "fix the phone" button is a button she learns to reach for, and
    on every ordinary screen there is nothing for it to fix.
    """

    SCRIPT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/phone.js")

    def test_the_card_is_in_the_page(self, client):
        body = client.get("/app").text
        assert 'id="covered"' in body
        assert 'id="covered-clear"' in body

    def test_it_is_hidden_until_the_screen_is_covered(self, client):
        import re

        css = client.get("/app").text
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        rule = re.search(r"#covered \{[^}]*\}", css)
        assert rule and "display:none" in rule.group(0)
        assert "body.covered #covered { display:flex; }" in css

    def test_and_the_sketch_is_hidden_while_it_shows(self, client):
        """Not dimmed like a slept screen: the panel over the app is the
        owner's own notifications, and a wireframe of those is not something
        this page should be drawing."""
        import re

        css = re.sub(r"/\*.*?\*/", "", client.get("/app").text, flags=re.S)
        assert "body.covered #screenwrap" in css
        rule = re.search(r"body\.covered #screenwrap[^{]*\{[^}]*\}", css)
        assert "display:none" in rule.group(0)

    def test_the_loading_skeleton_does_not_argue_with_it(self, client):
        """A screen behind the shade is stale by definition — the watcher has
        been reading the panel. "Syncing" over the card would be two answers
        to one question."""
        css = client.get("/app").text
        assert ("body.stale:not(.asleep):not(.offapp):not(.covered)"
                ":not(.walled)") in css

    def test_the_button_runs_the_macro(self):
        source = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        assert "covered-clear" in source
        assert "clear_screen" in source

    def test_the_class_follows_the_published_flag(self):
        source = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        assert "body.classList.toggle('covered', !!meta.covered)" in source

    def test_the_flag_reaches_the_browser(self):
        """The socket sends what the document said; a flag nobody forwards is
        a flag that does nothing."""
        source = (Path(__file__).resolve().parents[1]
                  / "src/apt_log/ui/app.py").read_text(encoding="utf-8")
        assert '"covered": bool(screen_doc.get("covered"))' in source

    @pytest.mark.parametrize("key", ["papp.covered", "papp.covered_hint",
                                     "papp.covered_action", "papp.clearing",
                                     "macro.clear_screen"])
    def test_both_languages_say_it(self, key):
        import json

        base = Path(__file__).resolve().parents[1] / "src/apt_log/ui/locales"
        for name in ("en.json", "es.json"):
            words = json.loads((base / name).read_text(encoding="utf-8"))
            assert words.get(key), f"{name} is missing {key}"


class TestVisitDetailDress:
    """How the scrubbed Visit Detail is drawn — the half of the fix that
    lives in the template rather than in the reflow."""

    FRAGMENT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/_screen.html")

    def test_the_actions_row_has_its_own_shape(self):
        markup = self.FRAGMENT.read_text(encoding="utf-8")
        assert "row.actions" in markup
        assert "a-actbtn" in markup

    def test_the_last_action_leads(self, client):
        """An app's primary action is filled. Here that is Note & Check out,
        the one that closes the visit."""
        markup = self.FRAGMENT.read_text(encoding="utf-8")
        assert "loop.last" in markup
        assert ".a-actbtn.primary" in client.get("/app").text

    def test_the_current_segment_is_not_dressed_as_unavailable(self, client):
        """It is disabled because she is already there. The greyed-out dress
        said the section on screen was broken."""
        import re

        css = re.sub(r"/\*.*?\*/", "", client.get("/app").text, flags=re.S)
        rule = re.search(r"\.a-segbtn:disabled \{[^}]*\}", css)
        assert rule and "opacity:1" in rule.group(0)

    def test_the_current_segment_says_so_to_a_screen_reader(self):
        markup = self.FRAGMENT.read_text(encoding="utf-8")
        assert 'aria-current="page"' in markup

    def test_a_section_switch_spans_its_row(self, client):
        """The segment was written for the care plan's tick pair, which hugs
        the right edge of a list cell. As a whole row it fills it."""
        css = client.get("/app").text
        assert ".a-seg:only-child" in css


class TestEveryShippedScriptParses:
    """The gap this closes: 1153 tests passed over a phone.js that did not
    parse at all.

    Every existing JS guard reads the source as TEXT — greps it for a
    identifier, an option, a call. Text is blind to the one failure that
    takes the whole page down at once: a duplicate `const` is a SyntaxError,
    so not one line of the file runs, the socket never opens and the splash
    it lifts on stays up forever. Reported as "I can't open the app".

    A parser is the only thing that catches that, so the gate now runs one.
    The deploy gate runs on the Pi, which has node; where node is missing
    this skips rather than lying about having checked.
    """

    SCRIPTS = sorted((Path(__file__).resolve().parents[1]
                      / "src/apt_log/ui/static").glob("*.js"))

    def test_there_are_scripts_to_check(self):
        """A glob that silently matches nothing would make every case below
        pass without reading a byte."""
        assert self.SCRIPTS, "no shipped scripts found to parse"

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_it_parses(self, script):
        import shutil
        import subprocess

        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            pytest.skip("no JavaScript engine available to parse with")
        done = subprocess.run([node, "--check", str(script)],
                              capture_output=True, timeout=60)
        assert done.returncode == 0, (
            f"{script.name} does not parse:\n"
            + done.stderr.decode("utf-8", "replace")[:800])


class TestTheChecklistDress:
    """The measured half of the fix. Before: a task row 111px tall and the
    page 2144px. After: 44px and 1259px — the whole check-out page in the
    space three tasks used to take."""

    PAGE = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/phone.html")
    FRAGMENT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/_screen.html")

    def test_a_tick_box_is_drawn_as_a_box(self):
        markup = self.FRAGMENT.read_text(encoding="utf-8")
        assert "a-check" in markup
        assert 'role="checkbox"' in markup

    def test_it_says_its_state_to_a_screen_reader(self):
        assert 'aria-checked' in self.FRAGMENT.read_text(encoding="utf-8")

    def test_the_checklist_row_never_wraps(self):
        """A checklist that wraps is not a checklist, it is a stack of
        paragraphs with switches in it."""
        import re

        css = re.sub(r"/\*.*?\*/", "", self.PAGE.read_text(encoding="utf-8"),
                     flags=re.S)
        rule = re.search(r"\.a-row\.a-checklist \{[^}]*\}", css)
        assert rule and "flex-wrap:nowrap" in rule.group(0)

    def test_the_task_name_carries_no_block_of_its_own(self):
        """The heading class was painting the app bar's background behind
        every task name."""
        import re

        css = re.sub(r"/\*.*?\*/", "", self.PAGE.read_text(encoding="utf-8"),
                     flags=re.S)
        rule = re.search(r"\.a-row\.a-checklist > \.a-label \{[^}]*\}", css)
        assert rule and "background:none" in rule.group(0)

    def test_the_tick_keeps_a_fingers_worth_of_target(self):
        """24px reads right beside a line of text; 24px is not tappable. The
        box stays small and the target grows underneath it."""
        import re

        css = re.sub(r"/\*.*?\*/", "", self.PAGE.read_text(encoding="utf-8"),
                     flags=re.S)
        assert re.search(r"\.a-check::before \{[^}]*inset:-10px", css)


class TestTheModalExitIsDrawn:
    FRAGMENT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/_screen.html")
    PAGE = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/phone.html")

    def test_the_fragment_draws_it(self):
        markup = self.FRAGMENT.read_text(encoding="utf-8")
        assert "m.dismiss" in markup and "a-dismissbtn" in markup

    def test_it_sits_above_the_sheets_own_controls(self):
        """Where the phone puts it, and where it has to be found BEFORE
        anything is pressed."""
        markup = self.FRAGMENT.read_text(encoding="utf-8")
        assert markup.index("m.dismiss") < markup.index("row.actions")

    def test_it_is_styled(self):
        import re

        css = re.sub(r"/\*.*?\*/", "", self.PAGE.read_text(encoding="utf-8"),
                     flags=re.S)
        assert re.search(r"\.a-dismissbtn \{[^}]*\}", css)

    def test_it_renders_with_a_real_model(self):
        from apt_log.ui import screenview
        from apt_log.ui.app import templates

        class T:
            language = "en"
            def __call__(self, k):
                return "Close" if k == "papp.close_sheet" else k

        m = screenview.build({
            "id": "f1", "size": [720, 1600], "blocked": "", "notice": "",
            "elements": [
                {"rid": "touch_outside", "cls": "View", "b": [0, 64, 720, 1561],
                 "txt": "", "checked": False, "focused": False},
                {"rid": "", "cls": "View", "b": [13, 946, 45, 978],
                 "txt": "", "checked": False, "focused": False},
                {"rid": "", "cls": "View", "b": [172, 1514, 274, 1545],
                 "txt": "", "checked": False, "focused": False}],
            "statics": [{"cls": "TextView", "b": [217, 1522, 245, 1537],
                         "txt": "Done"}]})
        html = templates.get_template("_screen.html").render(
            m=screenview.label_keys(m, T()), t=T())
        assert "a-dismissbtn" in html and ">Close<" in html

    @pytest.mark.parametrize("name", ["en.json", "es.json"])
    def test_both_languages_say_it(self, name):
        import json

        base = Path(__file__).resolve().parents[1] / "src/apt_log/ui/locales"
        assert json.loads((base / name).read_text(encoding="utf-8")
                          ).get("papp.close_sheet")


class TestTheSignaturePadIsSelfContained:
    """Three field reports about the pad, in one place.

    "Can't scroll down on the signature pad to dismiss."
    "Would also like the signature controls embedded into the pad instead of
    switching between phone peek and front end."
    "Signature with breaks or not linking lines have issues drawing."
    """

    SCRIPT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/phone.js")
    PAGE = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/phone.html")

    def _js(self):
        return strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))

    # ------------------------------------------------------------- dismissing
    def test_a_sheet_can_be_swiped_away(self):
        js = self._js()
        assert "swipeToDismiss" in js
        assert "touchstart" in js and "touchmove" in js

    def test_the_pad_is_one_of_them(self):
        assert "swipeToDismiss(document.getElementById('signsheet')" in self._js()

    def test_a_swipe_on_the_canvas_is_ink_not_a_dismiss(self):
        """Every touch that lands on the canvas belongs to the signature."""
        assert "closest('canvas" in self._js()

    def test_a_sheet_scrolled_down_still_scrolls(self):
        """Only from the top, or a sheet with content below the fold could
        never be read."""
        assert "sheet.scrollTop > 0" in self._js()

    # -------------------------------------------------------- the app's own
    def test_the_pad_has_a_slot_for_the_apps_buttons(self):
        assert 'id="sign-appbtns"' in self.PAGE.read_text(encoding="utf-8")

    def test_they_are_filled_from_the_screen_payload(self):
        assert "meta.sheet_actions" in self._js()

    def test_they_press_through_the_ordinary_verified_tap(self):
        js = self._js()
        slot = js[js.index("sheet_actions"):]
        assert "bindAims(slot)" in slot

    def test_only_an_affirmative_button_leads(self):
        """The last one led at first, and on this sheet the last one is
        Clear — the pad filled in the destructive button."""
        js = self._js()
        assert "slot.lastChild.classList.add('primary')" not in js
        assert "done|save|salvar" in js

    def test_the_legacy_coordinate_row_steps_aside(self):
        assert "sign-legacyrow" in self._js()

    # ------------------------------------------------------------- drawing
    def test_a_refused_pointer_capture_does_not_eat_the_stroke(self):
        """Capture throws if the browser has already let go of that pointer
        id — a quick lift and re-touch — and unguarded it aborted the handler
        before the stroke was created."""
        js = self._js()
        assert "try { c.setPointerCapture" in js
        idx = js.index("setPointerCapture")
        assert "catch" in js[idx:idx + 120]

    def test_the_stroke_is_still_recorded_after_the_guard(self):
        js = self._js()
        idx = js.index("setPointerCapture")
        after = js[idx:idx + 300]
        assert "pad.strokes.push" in after

    def test_visit_details_actions_never_reach_the_pad(self):
        """That row is "Check in" and "Note & Check out". A sheet is required,
        not merely an actions row."""
        from apt_log.ui import screenview
        from apt_log.ui.app import _sheet_actions

        doc = {"id": "f", "size": [720, 1600], "blocked": "", "notice": "",
               "canvas": True,
               "elements": [
                   {"rid": "", "cls": "View", "b": [172, 1510, 274, 1549],
                    "txt": "", "checked": False, "focused": False},
                   {"rid": "", "cls": "View", "b": [446, 1508, 548, 1550],
                    "txt": "", "checked": False, "focused": False}],
               "statics": [
                   {"cls": "TextView", "b": [199, 1522, 247, 1537],
                    "txt": "Check in"},
                   {"cls": "TextView", "b": [467, 1515, 528, 1543],
                    "txt": "Note & Check out"}]}
        assert _sheet_actions(doc, screenview.build(doc)) == []

    def test_a_flickering_canvas_flag_does_not_hide_them(self):
        """Caught live: the flag read False while the sheet was plainly open
        with Done and Clear on it. The buttons would have come and gone."""
        from apt_log.ui import screenview
        from apt_log.ui.app import _sheet_actions

        doc = {"id": "f", "size": [720, 1600], "blocked": "", "notice": "",
               "canvas": False,
               "elements": [
                   {"rid": "touch_outside", "cls": "View",
                    "b": [0, 64, 720, 1561], "txt": "", "checked": False,
                    "focused": False},
                   {"rid": "", "cls": "View", "b": [13, 946, 45, 978],
                    "txt": "", "checked": False, "focused": False},
                   {"rid": "", "cls": "View", "b": [172, 1514, 274, 1545],
                    "txt": "", "checked": False, "focused": False},
                   {"rid": "", "cls": "View", "b": [446, 1514, 548, 1545],
                    "txt": "", "checked": False, "focused": False}],
               "statics": [
                   {"cls": "TextView", "b": [217, 1522, 245, 1537],
                    "txt": "Done"},
                   {"cls": "TextView", "b": [489, 1522, 520, 1537],
                    "txt": "Clear"}]}
        got = _sheet_actions(doc, screenview.build(doc))
        # ERASE FIRST, AFFIRMATIVE LAST — the pad's order, not the app's.
        # inMyTeam draws Done on the LEFT and Clear on the right; HHAeXchange+
        # draws Borrar on the left and Enviar on the right. Showing each app's
        # own order put the affirmative on a different side depending on which
        # app was behind the pad, and the wrong press here wipes a signature.
        assert [a["txt"] for a in got] == ["Clear", "Done"]

    def test_the_pair_reads_the_same_whichever_app_is_behind_it(self):
        """The property, stated once: whatever the app calls its buttons and
        whatever order it draws them in, the pad erases on the left and
        affirms on the right."""
        from apt_log.ui.app import _pad_order

        for given in (["Done", "Clear"], ["Clear", "Done"],
                      ["Enviar", "Borrar"], ["Borrar", "Enviar"]):
            got = [a["txt"] for a in
                   _pad_order([{"txt": w, "aim": {}} for w in given])]
            assert got[0].lower() in ("clear", "borrar"), given
            assert got[-1].lower() in ("done", "enviar"), given

    def test_a_word_it_does_not_know_is_not_reordered_away(self):
        """Guessing at an unrecognised caption is how a pad ends up
        emphasising the wrong button."""
        from apt_log.ui.app import _pad_order

        given = [{"txt": "Anular", "aim": {}}, {"txt": "Revisar", "aim": {}},
                 {"txt": "Done", "aim": {}}]
        got = [a["txt"] for a in _pad_order(given)]
        assert got[0] == "Anular"          # a known erase word leads
        assert got.index("Revisar") < got.index("Done")

    def test_the_legacy_row_can_actually_hide(self):
        """`.padrow { display:flex }` beats the UA's `[hidden]`, so the
        coordinate pair stayed on screen beside the app's real buttons — and
        pressing it answered "this screen has no signature box" while the
        app's own Clear worked right next to it."""
        import re

        css = re.sub(r"/\*.*?\*/", "", self.PAGE.read_text(encoding="utf-8"),
                     flags=re.S)
        assert re.search(r"\.padrow\[hidden\] \{[^}]*display:none", css)


class TestThePadSaysWhereEachButtonActs:
    """"User error on the clear signature thing, it's just a bit confusing."

    It was: five buttons in three equal grey pills, and TWO of them said
    "Clear" — one meaning her own drawing, one meaning the canvas on the
    phone — with nothing on the page saying which acted where.
    """

    PAGE = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/phone.html")

    def _css(self):
        import re

        return re.sub(r"/\*.*?\*/", "", self.PAGE.read_text(encoding="utf-8"),
                      flags=re.S)

    def test_the_sheet_is_two_numbered_steps(self, client):
        body = client.get("/app").text
        assert body.count('class="signstep-n"') == 2

    def test_her_erase_and_the_apps_clear_are_different_words(self):
        """The whole confusion in one line."""
        import json

        base = Path(__file__).resolve().parents[1] / "src/apt_log/ui/locales"
        for name in ("en.json", "es.json"):
            w = json.loads((base / name).read_text(encoding="utf-8"))
            assert w["sign.erase"] != w["sign.appclear"]
            assert w["sign.erase"] != w["signature.clear"]

    def test_her_own_tools_are_quiet(self, client):
        """Undoing a stroke is not a decision worth a filled pill, and giving
        it one is what made five buttons look like five choices."""
        body = client.get("/app").text
        assert 'id="sign-undo" class="padtool"' in body
        assert 'id="sign-clear" class="padtool"' in body
        rule = self._css()
        assert ".padtool {" in rule and "background:none" in rule

    def test_step_one_has_exactly_one_action(self, client):
        body = client.get("/app").text
        assert 'id="sign-send" class="signsend"' in body

    def test_the_words_say_where_each_row_acts_without_a_glyph(self):
        """The row that presses the PHONE carried a drawn handset. It was
        reported as broken twice — as icons that "don't show anything", and
        again as "the icon is broke in the hecho button" — because at 13 by
        20 pixels an outlined rectangle with a dash in it is an empty box
        whatever it was meant to be.

        The heading above the row says the same thing in words, which is
        what it was doing the work of anyway. Dropped rather than redrawn a
        third time."""
        css = self._css()
        assert ".padrow.onphone button::before" not in css
        assert ".padrow.onphone button::after" not in css
        assert "sign.step_phone" in self.PAGE.read_text(encoding="utf-8")

    def test_the_phone_step_is_across_a_rule(self, client):
        css = self._css()
        assert "border-top:1px solid var(--line)" in css
        assert 'class="padrow onphone" id="sign-appbtns"' \
            in client.get("/app").text

    def test_the_buttons_are_still_the_ones_the_script_wires(self, client):
        """A redesign that renamed an id would silently unwire the pad."""
        body = client.get("/app").text
        for ident in ("sign-undo", "sign-clear", "sign-send", "signpad",
                      "signpreview", "sign-appbtns", "sign-legacyrow",
                      "app-clear", "app-save"):
            assert f'id="{ident}"' in body

    @pytest.mark.parametrize("key", ["sign.step_draw", "sign.step_phone",
                                     "sign.erase"])
    def test_both_languages_say_it(self, key):
        import json

        base = Path(__file__).resolve().parents[1] / "src/apt_log/ui/locales"
        for name in ("en.json", "es.json"):
            assert json.loads((base / name).read_text(encoding="utf-8")).get(key)


class TestTheAppsOwnButtonsWaitForTheInk:
    """"It's about timing. If I pressed send to phone too fast then the same
    happens — the first part gets captured, then only the end of the line —
    but if I wait about 5-10 seconds the whole signature makes it."

    Her reading was right and mine were not. Measured on the phone, the
    canvas reads 0 ink at two seconds after Send, 1764 at four, and complete
    at six: a replay takes five to six seconds. What is supposed to cover
    that window is the busy overlay, and it does not — `#busy` is
    `position:absolute; z-index:6`, bounded by the phone stage, while the pad
    sheet is `position:fixed; z-index:8` OVER it.

    Harmless until the app's own Done was moved INTO the pad, inches under
    Send. After that, Send-then-Done reads as one gesture, and a Done pressed
    at two seconds commits whatever ink has landed so far — one initial, or
    the bridge of an A without its arch. Which is the report, exactly.

    So the controls that reach the phone go dead until the replay says it
    finished. A half-drawn signature on a visit record is not cosmetic.
    """

    SCRIPT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/phone.js")
    PAGE = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/phone.html")

    def _js(self):
        return strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))

    def _css(self):
        import re

        return re.sub(r"/\*.*?\*/", "", self.PAGE.read_text(encoding="utf-8"),
                      flags=re.S)

    # ------------------------------------------------------------ the lock
    def test_there_is_a_lock(self):
        assert "function padWaiting(" in self._js()

    def test_it_kills_both_phone_side_rows(self):
        js = self._js()
        body = js[js.index("function padWaiting("):]
        body = body[:body.index("\n  }")]
        assert "sign-appbtns" in body and "sign-legacyrow" in body
        assert "disabled" in body

    def test_and_send_itself(self):
        """Two replays racing each other onto one canvas would interleave."""
        js = self._js()
        body = js[js.index("function padWaiting("):]
        assert "sign-send" in body[:body.index("\n  }")]

    def test_a_replay_takes_the_lock(self):
        js = self._js()
        send = js[js.index("function padSend("):js.index("function applySign(")]
        assert "padWaiting(true)" in send

    def test_and_the_replay_finishing_gives_it_back(self):
        js = self._js()
        done = js[js.index("function applySign("):]
        done = done[:done.index("\n  }")]
        assert "padWaiting(false)" in done

    def test_the_apps_own_actions_take_it_too(self):
        """Clear on the phone is a replay of the same kind — seconds long,
        and a Done pressed into the middle of one saves what survived it."""
        js = self._js()
        act = js[js.index("const appAction ="):]
        act = act[:act.index("const appClear")]
        assert "padWaiting(true)" in act

    def test_a_rebuilt_row_is_locked_again(self):
        """The app-button row is redrawn from the screen payload, and the
        payload changes WHILE the ink lands — a rebuild mid-replay would hand
        back live buttons."""
        js = self._js()
        head = js[:js.index("function padSend(")]
        assert "if (pad.waitingId) padWaiting(true);" in head

    # ------------------------------------------------------- and it says why
    def test_the_dead_buttons_say_why_they_are_dead(self, client):
        """A row of buttons that does nothing, with nothing explaining it,
        reads as the portal being broken."""
        assert 'class="padwait"' in client.get("/app").text

    def test_the_sentence_only_shows_while_it_waits(self):
        css = self._css()
        assert ".padwait { display:none; }" in css
        assert "#signsheet.waiting .padwait" in css

    def test_disabled_looks_disabled(self):
        """Every button in the pad carries a press animation; one that still
        pops under a finger reads as pressed."""
        css = self._css()
        assert "#signsheet button[disabled]" in css
        assert "#signsheet button[disabled]:active" in css

    # ------------------------------------------------------- and it lets go
    def test_the_lock_has_a_ceiling(self):
        """The busy overlay carries one for the same reason, in this file's
        own words: a stuck overlay is worse than a missing one. The lock lifts
        on a status push, and a push can be missed — a dropped socket, a
        controller restarted mid-replay. Without a ceiling that leaves step
        two dead for good and no way at all to press the app's own Done."""
        js = self._js()
        assert "PAD_LOCK_CEILING" in js
        body = js[js.index("function padWaiting("):]
        body = body[:body.index("\n  }")]
        assert "setTimeout" in body and "PAD_LOCK_CEILING" in body

    def test_the_ceiling_clears_far_past_any_real_replay(self):
        """Six seconds of drawing, plus the settle before the ink goes in and
        up to two redraws of a stroke that left none."""
        js = self._js()
        line = [l for l in js.splitlines() if "PAD_LOCK_CEILING =" in l][0]
        assert int(line.split("=")[1].strip().rstrip(";")) >= 20000

    def test_a_lapse_gives_the_id_back_too(self):
        """A lock released with the id still held would swallow the status
        push that finally arrives, and the toast with it."""
        js = self._js()
        body = js[js.index("padLockTimer = setTimeout("):]
        body = body[:body.index("PAD_LOCK_CEILING);")]
        assert "pad.waitingId = ''" in body
        assert "padWaiting(false)" in body

    def test_the_clock_is_not_restarted_by_a_rebuild(self):
        """The row is re-locked every time it is rebuilt, and the payload
        changes while the ink lands — re-arming there would let a chatty
        screen hold the lock open indefinitely."""
        js = self._js()
        body = js[js.index("function padWaiting("):]
        body = body[:body.index("\n  }")]
        assert "if (padLockTimer) return;" in body

    def test_the_lapse_says_something(self):
        js = self._js()
        assert "i18n.signLockLapsed" in js

    @pytest.mark.parametrize("key", ["sign.wait_phone", "sign.lock_lapsed"])
    def test_both_languages_say_it(self, key):
        base = Path(__file__).resolve().parents[1] / "src/apt_log/ui/locales"
        for name in ("en.json", "es.json"):
            assert json.loads(
                (base / name).read_text(encoding="utf-8")).get(key)

    def test_the_lapse_sentence_reaches_the_script(self, client):
        """A key the template never passes is an empty toast."""
        assert "signLockLapsed:" in client.get("/app").text


class TestTypingAName:
    """A search box that could not be typed into.

    Tapping one focused the field ON THE PHONE and stopped there: the
    phone's keyboard came up on a screen nobody is looking at, and from the
    portal there was no way to put a name in. The type bar was held back on
    purpose — a "type here" prompt over the patients list read as the
    sign-in code asking again — but the fix for that is the field's OWN
    words, which is exactly what the folded hint carries.
    """

    SCRIPT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/phone.js")

    def _js(self):
        return strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))

    def test_a_field_that_names_itself_opens_the_bar(self):
        assert "openTypeBar(aim, el.dataset.hint, 'search')" in self._js()

    def test_the_code_screen_still_comes_first(self):
        """A code-asking screen keeps the OTP path, hint or no hint."""
        js = self._js()
        assert js.index("c[oó]digo") < js.index("el.dataset.hint")

    def test_a_field_with_no_words_of_its_own_still_just_taps(self):
        """No hint, no bar: an unnamed box is not a search box, and a
        prompt over one is the OTP confusion coming back."""
        assert "if (el.dataset.hint)" in self._js()

    def test_a_name_keeps_its_spaces(self):
        """The old sanitizer was OTP-shaped and stripped everything but
        letters and digits, which turned "Rojas Batista" into a search that
        matches nothing."""
        js = self._js()
        assert "typeKind === 'search'" in js
        assert r"\p{L}" in js

    def test_and_a_code_is_still_a_code(self):
        assert "[^A-Za-z0-9]" in self._js()

    def test_nothing_a_shell_reads_gets_through_the_browser_either(self):
        """Belt to the server's braces. The character class is an allow
        list — letters, digits, space, and the three marks a name carries."""
        js = self._js()
        line = next(l for l in js.splitlines() if r"\p{L}" in l)
        for ch in ("$", "`", ";", "|", "&", "*"):
            assert ch not in line


class TestTheSearchBoxAsDrawn:
    """One control, drawn as one: the placeholder inside the box and the
    app's own magnifier in the corner it occupies on the phone."""

    PAGE = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/_screen.html")
    STYLE = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/phone.html")

    def _markup(self):
        return self.PAGE.read_text(encoding="utf-8")

    def test_the_hint_reaches_the_script_through_the_markup(self):
        """The script decides whether to offer the type bar from this
        attribute; without it a search box is untypable again."""
        assert 'data-hint="{{ it.hint }}"' in self._markup()

    def test_an_empty_box_shows_its_placeholder(self):
        assert "{{ it.txt or it.hint or '' }}" in self._markup()

    def test_placeholder_grey_is_not_ink(self):
        """Words she did not type must not read as a filled-in field."""
        css = self.STYLE.read_text(encoding="utf-8")
        assert ".a-field.empty" in css and "var(--muted)" in css

    def test_the_magnifier_is_drawn_where_the_app_puts_it(self):
        assert "a-fieldgo" in self._markup()
        assert ".a-fieldgo { position:absolute; right:4px" \
            in self.STYLE.read_text(encoding="utf-8")

    def test_it_is_a_real_control_with_a_real_aim(self):
        assert "data-aim='{{ it.submit | tojson }}'" in self._markup()

    def test_and_it_never_sits_on_top_of_the_words(self):
        css = self.STYLE.read_text(encoding="utf-8")
        assert ":has(.a-fieldgo) .a-field { padding-right" in css


class TestTheButtonThatTicksTheTasks:
    """A macro she cannot press is not a feature.

    `check_tasks` has been reachable only through the API since the day it
    was written. It belongs on the plan of care and nowhere else — a control
    that is always there and usually does nothing is a control pressed at
    the wrong moment, which is the argument this project already makes about
    the sign-in macros — so it is offered on the strength of the page's own
    count and hidden everywhere else.
    """

    SCRIPT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/phone.js")
    PAGE = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/phone.html")

    def _js(self):
        return strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))

    # --------------------------------------------------------- the counting
    def _doc(self, count=14, app="com.hhaexchange.uma"):
        els = []
        for i in range(count):
            top = 150 + i * 51
            els.append({"rid": "poc_task_item_status_completed_false",
                        "cls": "View", "b": [636, top, 667, top + 29],
                        "checked": False, "focused": False, "enabled": True})
            els.append({"rid": "poc_task_item_status_refused_false",
                        "cls": "View", "b": [667, top, 698, top + 29],
                        "checked": False, "focused": False, "enabled": True})
        return {"id": "f", "size": [720, 1600], "app": app,
                "elements": els, "statics": []}

    def test_the_count_is_what_the_macro_would_tick(self):
        from apt_log.ui.app import _pending_task_count

        assert _pending_task_count(self._doc()) == 14

    def test_the_refused_column_is_not_counted(self):
        """Half the elements on that page are the other column."""
        from apt_log.ui.app import _pending_task_count

        assert _pending_task_count(self._doc(count=3)) == 3

    def test_every_other_screen_counts_zero(self):
        from apt_log.ui.app import _pending_task_count

        assert _pending_task_count({"id": "f", "size": [720, 1600],
                                    "app": "com.hhaexchange.uma",
                                    "elements": [], "statics": []}) == 0

    def test_a_page_shaped_unexpectedly_costs_the_button_not_the_frame(self):
        """One field of a payload that carries the whole screen."""
        from apt_log.ui.app import _pending_task_count

        assert _pending_task_count({"size": "nonsense"}) == 0
        assert _pending_task_count({}) == 0

    # ---------------------------------------------------------- the offering
    def test_the_button_ships_hidden(self, client):
        body = client.get("/app").text
        assert 'id="btn-tasks" hidden' in body

    def test_it_appears_only_where_there_is_something_to_tick(self):
        js = self._js()
        assert "tasksBtn.hidden = left <= 0" in js

    def test_it_says_how_many(self):
        """The number is the reason the button is there — she can see the
        work before pressing anything."""
        js = self._js()
        assert "btn-tasks-n" in js
        assert ".tasks-n" in self.PAGE.read_text(encoding="utf-8")

    def test_pressing_it_runs_the_macro_and_nothing_else(self):
        js = self._js()
        run = js[js.index("const tasksRun ="):]
        run = run[:run.index("const scanClose")]
        assert "name: 'check_tasks'" in run
        assert "'/macro'" in run

    def test_the_wait_is_named_in_both_languages(self, client):
        assert "checkingTasks:" in client.get("/app").text

    def test_the_payload_carries_the_count(self, client):
        """Without this the button never appears, whatever the page holds."""
        from pathlib import Path as P

        src = (P(__file__).resolve().parents[1]
               / "src/apt_log/ui/app.py").read_text(encoding="utf-8")
        assert '"tasks": _pending_task_count(screen_doc)' in src


class TestThePadOnAFullScreenSignature:
    """inMyTeam puts its pad in a bottom sheet; HHAeXchange+ gives it a whole
    screen. The pad's step two — the app's OWN Borrar and Enviar — was gated
    on the sheet, so on HHAeXchange+ it fell back to the LEGACY pair: two
    presses at coordinates derived for a different app's rotated page.
    """

    def _doc(self, canvas=True):
        return {"id": "f", "size": [720, 1600], "canvas": canvas,
                "elements": [
                    {"rid": "signature_screen_clear_button", "cls": "View",
                     "b": [14, 656, 44, 681]},
                    {"rid": "signature_screen_submit_button", "cls": "View",
                     "b": [1474, 656, 1529, 681]},
                    {"rid": "menu_top_bar_back_button", "cls": "View",
                     "b": [6, 17, 31, 42]},
                ],
                "statics": [
                    {"cls": "TextView", "b": [14, 662, 44, 675],
                     "txt": "Borrar"},
                    {"cls": "TextView", "b": [1488, 663, 1515, 674],
                     "txt": "Enviar"},
                ]}

    def _actions(self, doc, model=None):
        from apt_log.ui.app import _sheet_actions

        return _sheet_actions(doc, model)

    def test_the_apps_own_buttons_reach_the_pad(self):
        assert [a["txt"] for a in self._actions(self._doc())] \
            == ["Borrar", "Enviar"]

    def test_they_are_real_aims_not_coordinates(self):
        aims = [a["aim"]["rid"] for a in self._actions(self._doc())]
        assert aims == ["signature_screen_clear_button",
                        "signature_screen_submit_button"]

    def test_the_submit_comes_last_so_the_pad_fills_it(self):
        assert self._actions(self._doc())[-1]["txt"] == "Enviar"

    def test_the_caption_comes_from_the_label_over_the_button(self):
        """These buttons carry no text of their own — "Borrar" and "Enviar"
        are separate labels drawn on top of them."""
        assert [a["txt"] for a in self._actions(self._doc())] \
            == ["Borrar", "Enviar"]

    def test_a_button_is_not_dropped_for_want_of_its_label(self):
        """AN EMPTY LIST IS NOT "NO BUTTONS" — it is the pad falling back to
        the legacy coordinate pair, which off the legacy app presses nothing.

        The label is a separate TextView laid over the button, so a dump that
        arrives mid-repaint can be missing it while the button is plainly
        there. The resource-id has already named both the screen and the
        action by then, so the action's own word stands in and the button
        stays pressable.
        """
        doc = self._doc()
        doc["statics"] = []
        got = self._actions(doc)
        assert [a["txt"] for a in got] == ["Borrar", "Enviar"]
        assert [a["aim"]["rid"] for a in got] == [
            "signature_screen_clear_button", "signature_screen_submit_button"]

    def test_a_flickering_canvas_flag_does_not_take_the_buttons_away(self):
        """That flag comes from a mark in the hierarchy and it flickers —
        caught live reading False while the signature screen was plainly
        open. Gating the pad's only working step-two controls on it dropped
        the pad onto the legacy pair mid-signature. The ids are the gate."""
        assert [a["txt"] for a in self._actions(self._doc(canvas=False))] \
            == ["Borrar", "Enviar"]

    def test_an_ordinary_page_still_offers_nothing(self):
        doc = self._doc(canvas=False)
        doc["elements"] = [{"rid": "menu_top_bar_back_button", "cls": "View",
                            "b": [6, 17, 31, 42]}]
        doc["statics"] = []
        assert self._actions(doc) == []

    def test_a_generic_save_id_is_not_taken_for_the_pads(self):
        """The legacy visit page's own `button_save`. An id has to name the
        signature screen before it counts."""
        doc = self._doc()
        doc["elements"] = [{"rid": "button_save", "cls": "Button",
                            "b": [642, 74, 678, 110]}]
        doc["statics"] = [{"cls": "TextView", "b": [646, 80, 674, 100],
                           "txt": "Salvar"}]
        assert self._actions(doc) == []

    def test_the_legacy_coordinate_pair_shows_only_where_it_can_press(self):
        """IT USED TO FILL IN WHENEVER THE NAMED BUTTONS CAME BACK EMPTY, ON
        ANY APP.

        `sign.button_targets` derives that coordinate only for the legacy
        app and answers None everywhere else, so on HHAeXchange+ the pad
        offered a Borrar and a Salvar whose every press could only answer
        "no_canvas". Reported as the pad's step-two button doing nothing,
        and finished from the phone view instead — which worked, because
        that path taps the element.
        """
        from pathlib import Path

        js = strip_js_comments((Path(__file__).resolve().parents[1]
                                / "src/apt_log/ui/static/phone.js")
                               .read_text(encoding="utf-8"))
        assert "legacyUsable = !sheetActions.length && !!meta.legacy_pad" in js
        assert "legacy.hidden = !legacyUsable" in js

    def test_only_the_legacy_app_gets_the_coordinate_pair(self):
        from apt_log import sign as sign_mod
        from apt_log.ui.app import _legacy_pad

        assert _legacy_pad("com.hhaexchange.uma") is False
        assert _legacy_pad("com.inmyteam.inmyteam") is False
        assert _legacy_pad("") is False
        assert _legacy_pad(sign_mod.ROTATED_CANVAS_APPS[0]) is True

    def test_the_flag_agrees_with_what_the_controller_will_do(self):
        """The pad must not offer a button the controller answers None for.
        Checked against `button_targets` itself rather than a repeated list.
        """
        from apt_log import sign as sign_mod
        from apt_log.ui.app import _legacy_pad

        for package in ("com.hhaexchange.uma", "com.inmyteam.inmyteam"):
            assert sign_mod.button_targets("<hierarchy/>", package) is None
            assert _legacy_pad(package) is False

    def test_enviar_leads_in_the_pad(self):
        """The pad emphasises an affirmative caption, and this app's is not
        "save" — it is "send"."""
        from pathlib import Path

        js = (Path(__file__).resolve().parents[1]
              / "src/apt_log/ui/static/phone.js").read_text(encoding="utf-8")
        line = next(l for l in js.splitlines() if "aceptar" in l)
        assert "enviar" in line


class TestTheCanvasOpensThePad:
    PAGE = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/_screen.html")
    SCRIPT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/phone.js")

    def test_the_canvas_carries_the_marker(self):
        assert 'data-sign="1"' in self.PAGE.read_text(encoding="utf-8")

    def test_and_it_is_not_an_aim(self):
        """It must not tap through: a tap on a canvas is a dot of ink."""
        markup = self.PAGE.read_text(encoding="utf-8")
        block = markup[markup.index("it.kind == 'canvas'"):]
        block = block[:block.index("{% elif")]
        assert "data-aim" not in block

    def test_the_script_opens_the_pad_from_it(self):
        js = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        assert "closest('[data-sign]')" in js

    def test_the_handler_is_delegated(self):
        """The page is re-rendered from the socket; a handler bound to the
        element itself would go with it on the first repaint."""
        js = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        assert "signSurface.addEventListener('click'" in js

    def test_it_says_what_it_is_in_both_languages(self):
        base = Path(__file__).resolve().parents[1] / "src/apt_log/ui/locales"
        for name in ("en.json", "es.json"):
            assert json.loads(
                (base / name).read_text(encoding="utf-8")).get("sign.here")


class TestAWideFrameInAPortraitBox:
    """"As soon as she went to the signature screen and the phone changed
    orientation the only thing I could see is BLACK on the phone peek."

    Not dark mode, though it is a fair thing to suspect: the peek's bed was
    a hard-coded #000 in both themes. It was invisible for a portrait
    photograph, which covers the box edge to edge — and a landscape one
    covers 22% of it, leaving 78% flat black under a strip of screen.
    """

    PAGE = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/phone.html")
    CSS = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/design.css")
    SCRIPT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/phone.js")

    def test_the_arithmetic_that_made_it_black(self):
        """400px wide box, 800 tall — roughly her phone."""
        covered = (400 * 720 / 1600) / 800
        assert covered < 0.25

    def test_a_wide_frame_is_fitted_rather_than_stretched(self):
        css = self.PAGE.read_text(encoding="utf-8")
        assert "body.wide:not(.sideways) #peek" in css
        assert "max-height:100%" in css

    def test_and_centred_in_what_is_left(self):
        css = self.PAGE.read_text(encoding="utf-8")
        assert "body.peeking.wide:not(.sideways) #peekwrap" in css

    def test_the_rotated_case_is_left_exactly_as_it_was(self):
        """Its transform and negative margins are measured against a
        full-width block box; fitting one would move it off the stage."""
        css = self.PAGE.read_text(encoding="utf-8")
        assert "body.sideways #peek {" in css
        assert "margin: -88.6% 0" in css
        # Both new rules exclude it by name.
        for rule in ("body.wide:not(.sideways) #peek",
                     "body.peeking.wide:not(.sideways) #peekwrap"):
            assert rule in css

    def test_the_bed_is_a_surface_not_a_hard_coded_void(self):
        assert "background:var(--peek-bed)" in self.PAGE.read_text(
            encoding="utf-8")

    def test_the_bed_is_defined_in_both_themes(self):
        css = self.CSS.read_text(encoding="utf-8")
        assert css.count("--peek-bed:") == 2
        light, dark = css.split("prefers-color-scheme: dark")
        assert "--peek-bed:" in light and "--peek-bed:" in dark

    def test_the_page_knows_when_the_frame_is_wide(self):
        js = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        assert "toggle('wide', !!meta.landscape)" in js

    def test_wide_and_turned_are_still_two_different_facts(self):
        """One says the photograph is shaped differently; the other says it
        is the wrong way up. Conflating them is what went black first."""
        js = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        assert "toggle('sideways', !!meta.turn)" in js
        assert "toggle('wide', !!meta.landscape)" in js


class TestCoachMode:
    """"I also want to see how her front end looks like as she navigates so
    I can coach her. Is this possible?"

    It was — and so was reaching over from another state and pressing Enviar
    on her behalf, because every control on the page drives ONE phone.

    So: watch without touching. The CSS makes it visible and unreachable;
    the script makes it true. The two halves matter separately — a mode that
    only greys things out is a mode that fails silently the first time a
    handler is bound somewhere new.
    """

    SCRIPT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/phone.js")
    PAGE = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/phone.html")

    def _js(self):
        return strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))

    # ------------------------------------------------- every road to the phone
    def test_every_path_that_moves_the_phone_asks_first(self):
        """The list is derived from the source, not written down here, so a
        NEW way to reach the phone fails this test instead of slipping past
        it. Each call site must have a `driving()` check above it inside its
        own handler."""
        js = self._js()
        reaches = [m for m in
                   ("socket.send(JSON.stringify({ type: 'tap'",
                    "socket.send(JSON.stringify({ type: 'text'",
                    "name: 'read_page'", "name: 'clear_screen'",
                    "name: 'check_tasks'", "'/sign'", "'/sign-action'",
                    "type: 'device'")
                   if m in js]
        assert len(reaches) >= 7, f"only found {reaches}"
        for marker in reaches:
            if marker == "name: 'read_page'":
                continue          # reading a page never touches it
            before = js[:js.index(marker)]
            assert "driving()" in before[-1400:], \
                f"nothing holds back {marker}"

    def test_reading_the_page_is_not_touching_it(self):
        """The scan scrolls the phone and reads it back — no state changes,
        and it is the one thing a coach most wants while talking her through
        a screen."""
        css = self.PAGE.read_text(encoding="utf-8")
        assert "body.coaching #btn-scan" in css
        assert "pointer-events:auto" in css

    def test_the_photograph_is_not_touching_it_either(self):
        css = self.PAGE.read_text(encoding="utf-8")
        assert "body.coaching #btn-peek" in css

    def test_a_held_press_says_why(self):
        """Silence would read as the portal being broken."""
        js = self._js()
        body = js[js.index("function driving()"):]
        assert "coachHeld" in body[:body.index("}\n")+200]

    # -------------------------------------------------------------- the switch
    def test_the_page_carries_the_switch(self, client):
        assert 'id="coach"' in client.get("/app").text

    def test_it_is_loud_when_it_is_on(self):
        """A mode that silently swallows taps is worse than no mode: it must
        be impossible to be in it and not know."""
        css = self.PAGE.read_text(encoding="utf-8")
        assert "body.coaching .hdr-sub .coachbtn { background:var(--tint)" in css

    def test_it_survives_a_reload(self):
        """A reload in the middle of watching somebody work must not quietly
        hand the phone back."""
        js = self._js()
        assert "sessionStorage.setItem('aptlog-coach'" in js
        assert "sessionStorage.getItem('aptlog-coach')" in js

    def test_the_words_exist_in_both_languages(self):
        base = Path(__file__).resolve().parents[1] / "src/apt_log/ui/locales"
        for name in ("en.json", "es.json"):
            words = json.loads((base / name).read_text(encoding="utf-8"))
            for key in ("papp.coach", "papp.coach_on", "papp.coach_off",
                        "papp.coach_held"):
                assert words.get(key), f"{name} is missing {key}"


class TestWhoElseIsOn:
    """One phone, two sets of hands, in two states. "Somebody else is here"
    is worth knowing before reaching for a button."""

    SCRIPT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/phone.js")

    def test_the_count_reaches_the_page(self):
        from pathlib import Path as P

        src = (P(__file__).resolve().parents[1]
               / "src/apt_log/ui/app.py").read_text(encoding="utf-8")
        assert 'payload["viewers"] = last["viewers"] = _viewers' in src

    def test_it_is_hidden_when_it_is_only_you(self):
        """A badge reading "1" all day is furniture."""
        js = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        assert "badge.hidden = n < 2" in js

    def test_the_stamp_survives_a_restart_of_this_process(self):
        """The counter cannot: a deploy resets it to zero, and every deploy
        took auto sign-in offline until a browser happened to reconnect."""
        from pathlib import Path as P

        src = (P(__file__).resolve().parents[1]
               / "src/apt_log/ui/app.py").read_text(encoding="utf-8")
        body = src[src.index("def _publish_viewers"):]
        body = body[:body.index("\ntemplates =")]
        assert "VIEWERS_PATH.read_text" in body      # reads the old stamp
        assert '"seen": seen' in body

    def test_the_stamp_only_moves_while_somebody_is_on(self):
        from pathlib import Path as P

        src = (P(__file__).resolve().parents[1]
               / "src/apt_log/ui/app.py").read_text(encoding="utf-8")
        body = src[src.index("def _publish_viewers"):]
        assert "if _viewers > 0:" in body[:body.index("\ntemplates =")]


class TestTheTickThatSaysWhatItTicks:
    """The markup half of the named EVV marks. A name that never reaches the
    page is a name nobody reads."""

    PAGE = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/_screen.html")
    STYLE = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/templates/phone.html")

    def test_the_name_is_drawn_beside_the_tick(self):
        markup = self.PAGE.read_text(encoding="utf-8")
        assert markup.count('<span class="a-mark-t">{{ m.txt }}</span>') == 2

    def test_both_places_marks_are_drawn_carry_it(self):
        """A row's marks and an info row's marks are two render sites, and a
        fix applied to one of them is a fix half done."""
        markup = self.PAGE.read_text(encoding="utf-8")
        assert markup.count("{% if m.txt %} named{% endif %}") == 2

    def test_a_nameless_mark_is_unchanged(self):
        """Every other state mark is a glyph the app drew, which already
        says what it means — it must not grow an empty label."""
        markup = self.PAGE.read_text(encoding="utf-8")
        assert "{% if m.txt %}<span" in markup

    def test_the_named_pill_has_room_for_a_word(self):
        css = self.STYLE.read_text(encoding="utf-8")
        assert ".a-mark.named" in css and ".a-mark-t" in css

    def test_the_marks_are_translated_with_the_rest(self):
        """`label_keys` walks items; marks are not items and were skipped."""
        from pathlib import Path as P

        src = (P(__file__).resolve().parents[1]
               / "src/apt_log/ui/screenview.py").read_text(encoding="utf-8")
        body = src[src.index("def label_keys"):]
        assert 'walk(it.get("marks"))' in body[:body.index("\n    for row")]


class TestTheUpdateWallCard:
    """Mobile Caregiver+ raised its forced-update wall and the app became
    unusable. Rendered as an ordinary page it is one button — and that button
    opens the Play Store, where the containment watchdog bounces her back
    within five seconds, so from this side it is a loop with no way out.

    The card is the way out, and it is contextual for the same reason the
    covered card is: a standing "replace this app" button is not a thing to
    have within reach.
    """

    SCRIPT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/phone.js")

    def test_the_card_is_in_the_page(self, client):
        body = client.get("/app").text
        assert 'id="walled"' in body
        assert 'id="walled-update"' in body

    def test_it_is_hidden_until_an_update_is_being_demanded(self, client):
        import re

        css = re.sub(r"/\*.*?\*/", "", client.get("/app").text, flags=re.S)
        rule = re.search(r"#walled \{[^}]*\}", css)
        assert rule and "display:none" in rule.group(0)
        assert "body.walled:not(.covered) #walled { display:flex; }" in css

    def test_the_loading_skeleton_does_not_argue_with_it_either(self, client):
        """A screen behind an update wall is stale by definition — the app is
        never going to draw anything else. "Syncing" beside the card would be
        two answers to one question, and it is the wrong one."""
        css = client.get("/app").text
        assert ("body.stale:not(.asleep):not(.offapp):not(.covered)"
                ":not(.walled)") in css

    def test_a_shade_over_a_wall_is_still_a_shade(self, client):
        """Both cards answer "the app in front cannot be used", and only one
        of them can be the answer. The panel wins: nothing she taps reaches
        the app underneath it, including this card's own button."""
        import re

        css = re.sub(r"/\*.*?\*/", "", client.get("/app").text, flags=re.S)
        assert "body.walled:not(.covered) #screenwrap" in css

    def test_the_button_runs_the_macro(self):
        source = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        assert "walled-update" in source
        assert "update_app" in source

    def test_it_asks_before_replacing_the_app(self):
        """The only button on this page that does. There is no going back to
        the old build from the phone, and every rule the portal has for
        reading that app was written against the one being removed."""
        source = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        assert "window.confirm(ask)" in source
        assert 'data-confirm' in client_body_of()

    def test_the_wait_outlasts_a_download(self):
        """A spinner that gives up while Play is still working is how a
        finished update gets reported as a failure."""
        source = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        assert "480000" in source

    def test_the_class_follows_the_published_flag(self):
        source = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        assert "body.classList.toggle('walled', !!meta.walled)" in source

    def test_the_flag_reaches_the_browser(self):
        source = (Path(__file__).resolve().parents[1]
                  / "src/apt_log/ui/app.py").read_text(encoding="utf-8")
        assert '"walled": _update_wall(screen_doc)' in source

    def test_the_flag_is_the_macros_own_answer(self):
        """Not a second opinion about the same screen. The card appears on
        exactly the reading the macro will act on."""
        from apt_log.ui.app import _update_wall

        wall = {"app": "com.tellus.evv.v2",
                "statics": [{"txt": "Actualizar ahora"}],
                "elements": [{"txt": "Actualizar ahora"}]}
        assert _update_wall(wall) is True
        assert _update_wall(
            {"app": "com.tellus.evv.v2",
             "statics": [{"txt": "Mis visitas"}], "elements": []}) is False

    def test_a_page_shaped_unexpectedly_costs_the_card_not_the_frame(self):
        from apt_log.ui.app import _update_wall

        assert _update_wall({"app": "com.tellus.evv.v2",
                             "statics": "not a list"}) is False

    @pytest.mark.parametrize("key", ["papp.walled", "papp.walled_hint",
                                     "papp.walled_action", "papp.updating",
                                     "macro.update_app",
                                     "macro.sure.update_app"])
    def test_both_languages_say_it(self, key):
        import json

        base = Path(__file__).resolve().parents[1] / "src/apt_log/ui/locales"
        for name in ("en.json", "es.json"):
            words = json.loads((base / name).read_text(encoding="utf-8"))
            assert words.get(key), f"{key} missing from {name}"


def client_body_of() -> str:
    """The app page's markup, for a test that needs both it and the script."""
    from fastapi.testclient import TestClient

    from apt_log.ui.app import app as fastapi_app

    return TestClient(fastapi_app).get("/app").text


class TestTheSuiteLeavesTheMachineAlone:
    """conftest opens by saying nothing here may touch the machine's real
    state. That was true of preferences and false of five other paths, which
    is a thing you can only find by looking.

    How it was found is worth recording: delete /var/lib/aptlog, run the
    suite, see what grows back. It grew back four times before it stopped.

    Two of those mattered beyond tidiness. `flight.jsonl` is the flight
    recorder, so the deploy gate had been interleaving test fixtures with real
    recorded visits on the live machine. `viewers.json` is what
    `someone_is_watching` reads to decide whether auto sign-in may run
    unattended — a safety interlock, written by the test suite, on the running
    controller.

    Neither ever failed anywhere: both are written inside try/except, so on CI
    they silently did nothing and on the Pi they silently worked.
    """

    REAL = Path("/var/lib/aptlog")

    def test_every_state_path_points_somewhere_temporary(self):
        import importlib

        from apt_log import flight, macros, prefs, versions
        from apt_log.ui import state

        watched = {
            "prefs": prefs.PREFS_PATH,
            "versions": versions.VERSIONS_PATH,
            "paused flag": state.PAUSED_FLAG,
            "state dir": state.STATE_DIR,
            "flight recorder": flight.FLIGHT_PATH,
            "viewers (macros)": macros.VIEWERS_PATH,
            "viewers (web)": importlib.import_module(
                "apt_log.ui.app").VIEWERS_PATH,
        }
        for name, path in watched.items():
            assert self.REAL not in Path(path).parents and path != self.REAL, (
                f"{name} still points at the machine's own state: {path}")


class TestTheCodeSheSentComesBack:
    """Reported from the field: the prompt updates when she sends the code,
    the digits never appear, and there is no way to tell a mistyped code from
    a wrong one.

    The cause is a rule this project keeps on purpose — editable text is left
    out of the published screen, because that is where typed credentials live
    — so the reflow redraws the code box empty a moment after she fills it.
    The fix is not to publish it. The browser that sent the value already has
    it, so it echoes its own copy and nothing new crosses the wire.
    """

    SCRIPT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/phone.js")

    def source(self):
        return strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))

    def test_the_value_is_kept_when_a_code_is_sent(self):
        assert "sentEcho" in self.source()

    def test_it_is_painted_back_after_every_render(self):
        """The reflow is rebuilt from the server on each frame and knows
        nothing about the echo, so re-applying it is the whole mechanism."""
        source = self.source()
        assert "paintSentCode()" in source
        # ...specifically after the screen is written, not once at startup.
        after_render = source.split("root.innerHTML = html;", 1)[-1][:200]
        assert "paintSentCode()" in after_render

    def test_only_codes_are_echoed_never_searches(self):
        """A patient's name typed into a search box is not something to leave
        painted on the page afterwards."""
        source = self.source()
        assert "typeKind === 'code'" in source

    def test_the_aims_are_compared_through_the_same_encoder(self):
        """The server writes data-aim with Python's json — a space after
        every colon — and JSON.stringify writes none. Comparing the raw
        strings matches nothing and the echo silently never appears."""
        source = self.source()
        assert "JSON.stringify(JSON.parse(a))" in source

    def test_it_is_never_written_to_storage(self):
        """A one-time code should not outlive the page that used it."""
        source = self.source()
        window = source[source.index("sentEcho"):]
        assert "sessionStorage" not in window.split("function renderAppTabs")[0]
        assert "localStorage" not in window.split("function renderAppTabs")[0]

    def test_it_is_labelled_as_the_portals_own_echo(self, client):
        """Not as something the phone reported back. "The app has my code" is
        the wrong thing to believe when the next choice is Verify or ask for
        another."""
        assert "a-senttag" in self.source()
        assert "a-senttag" in client.get("/app").text


class TestVerifyIsTheThingToPress:
    def test_verify_reads_as_a_call_to_action(self):
        """It is the only thing to do on that screen, at the one moment she
        is holding a code that expires — and it was rendering as an ordinary
        row because this list decides which control gets the filled pill."""
        from apt_log.ui.screenview import _looks_like_cta

        for word in ("Verify", "Verificar", "VERIFY", "Verify code"):
            assert _looks_like_cta(word) is True

    def test_resend_is_deliberately_not_one(self):
        """Two filled pills would be the page shouting two instructions at
        somebody under time pressure."""
        from apt_log.ui.screenview import _looks_like_cta

        for word in ("Resend code", "Reenviar código"):
            assert _looks_like_cta(word) is False


class TestAskingForANewCode:
    """The app offers no way out of an expired code: its code screen has a
    field and a submit and nothing else. The only exit anybody had found was
    to force-stop the app by hand and sign in again."""

    SCRIPT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/phone.js")

    CODE_DOC = {
        "app": "com.inmyteam.inmyteam",
        "elements": [{"cls": "EditText", "b": [0, 0, 100, 40], "txt": ""}],
        "statics": [{"cls": "TextView", "b": [0, 0, 100, 20],
                     "txt": "Enter your code"}],
    }

    def test_the_code_screen_is_recognised(self):
        from apt_log.ui.app import _code_screen

        assert _code_screen(self.CODE_DOC) is True

    def test_the_number_screen_is_not(self):
        """Both screens are one field and a button; only one of them says
        'code'. Typing a phone number into the other one is the mistake this
        distinction exists to prevent."""
        from apt_log.ui.app import _code_screen

        doc = dict(self.CODE_DOC)
        doc["statics"] = [{"cls": "TextView", "b": [0, 0, 100, 20],
                           "txt": "Enter your cell phone number"}]
        assert _code_screen(doc) is False

    def test_another_apps_code_screen_is_not_offered_this(self):
        from apt_log.ui.app import _code_screen

        doc = dict(self.CODE_DOC)
        doc["app"] = "com.tellus.evv.v2"
        assert _code_screen(doc) is False

    def test_a_page_shaped_unexpectedly_costs_the_button_not_the_frame(self):
        from apt_log.ui.app import _code_screen

        assert _code_screen({"app": "com.inmyteam.inmyteam",
                             "elements": "not a list"}) is False

    def test_the_button_is_on_the_page_and_asks_first(self, client):
        """It closes the app, signs in again and sends a real text message —
        and the code she is already holding stops working."""
        import re

        body = client.get("/app").text
        button = re.search(r"<button[^>]*id=\"resend-code\"[^>]*>", body)
        assert button, "the resend button is not on the page"
        assert "data-confirm=" in button.group(0)

    def test_it_runs_the_macro(self):
        source = strip_js_comments(self.SCRIPT.read_text(encoding="utf-8"))
        assert "inmyteam_resend_code" in source

    def test_it_is_hidden_off_the_code_screen(self, client):
        import re

        css = re.sub(r"/\*.*?\*/", "", client.get("/app").text, flags=re.S)
        rule = re.search(r"#resend \{[^}]*\}", css)
        assert rule and "display:none" in rule.group(0)
        assert "body.codescreen" in css

    def test_the_flag_reaches_the_browser(self):
        source = (Path(__file__).resolve().parents[1]
                  / "src/apt_log/ui/app.py").read_text(encoding="utf-8")
        assert '"code_screen": _code_screen(screen_doc)' in source

    def test_it_sits_under_the_screen_not_a_scroll_below_it(self, client):
        """`#screenwrap` is min-height:100% so the reflow fills the glass,
        which puts anything after it off the bottom: measured at y=942 on a
        932-tall viewport, so the way out of an expiring code sat behind a
        screenful of nothing. Caught by rendering it, not by reading it."""
        import re

        css = re.sub(r"/\*.*?\*/", "", client.get("/app").text, flags=re.S)
        assert "body.codescreen #screenwrap { min-height:0; }" in css


# ============================================================== the schedule
class TestTheNextVisitOnTheHomeScreen:
    """The module she opens the portal to read.

    Every name here is invented, as in test_schedule.py and for the same
    reason: the real round is health information and lives on the device.
    """

    def _plan(self, *visits, zone="America/New_York"):
        from apt_log import schedule as sched

        return sched.parse({"zone": zone, "visits": list(visits)})

    def _one(self, patient="Ada", start="06:00", end="09:00",
             days=("mon", "tue", "wed", "thu", "fri", "sat", "sun"),
             app="com.hhaexchange.uma", **extra):
        return {"patient": patient, "app": app, "start": start, "end": end,
                "days": list(days), **extra}

    def _rendered(self, client, plan):
        # English asked for explicitly. The portal follows the browser when
        # nothing has been chosen, and the test client's default is not
        # English — which made these assertions pass or fail on a header
        # rather than on the markup.
        with patch("apt_log.schedule.load", return_value=plan):
            return client.get("/app", headers={"Accept-Language": "en"}).text

    def test_the_next_visit_is_on_the_page_before_any_javascript_runs(
            self, client):
        """Server-rendered on purpose. This is what she came for, and a
        skeleton that fills in half a second later looks broken on the kind
        of connection a phone in a car actually has."""
        page = self._rendered(client, self._plan(self._one("Ada")))
        assert "Ada" in page
        assert 'id="upnext"' in page

    def test_a_visit_in_progress_is_marked_as_happening_now(self, client):
        from apt_log import schedule as sched

        page = self._rendered(client, self._plan(
            self._one("Ada", "00:00", "23:59")))
        assert 'data-running="1"' in page
        assert Translator("en").t("sched.now") in page

    def test_a_schedule_that_will_not_parse_says_so(self, client):
        """An empty week looks exactly like a day off. Those two must never
        render the same."""
        from apt_log import schedule as sched

        with patch("apt_log.schedule.load",
                   side_effect=sched.BadSchedule("line 4 is nonsense")):
            page = client.get("/app", headers={"Accept-Language": "en"}).text
        assert Translator("en").t("sched.unreadable") in page
        assert "line 4 is nonsense" in page

    def test_a_controller_with_no_schedule_still_renders(self, client):
        """It ships without one, and the portal is not for this module."""
        page = self._rendered(client, self._plan())
        assert "<html" in page.lower()
        assert Translator("en").t("sched.nothing") in page

    def test_the_five_minutes_that_are_in_no_app_are_labelled(self, client):
        """A caregiver who notices the difference between the portal and the
        app should find it explained rather than wonder which is wrong."""
        page = self._rendered(client, self._plan(
            self._one("Ada", "05:00", "06:00"),
            self._one("Bea", "06:00", "09:00")))
        assert Translator("en").t("sched.buffered") in page

    def test_the_second_card_is_hidden_when_there_is_no_second_visit(
            self, client):
        """One visit in the whole week: a card with nothing on it should not
        be on the page."""
        page = self._rendered(client, self._plan(
            self._one("Ada", days=["mon"])))
        head = page[page.index('id="after"'):page.index('id="after"') + 220]
        assert "hidden" in head

    def test_the_full_week_is_grouped_by_day(self, client):
        page = self._rendered(client, self._plan(
            self._one("Ada", "06:00", "09:00", days=["mon"]),
            self._one("Bea", "10:00", "12:00", days=["tue"])))
        assert 'id="scheduleview"' in page
        assert page.count('class="dayhead') >= 2

    def test_there_is_a_way_back_to_the_home_view(self, client):
        """On the full-schedule screen, in the same place the phone view puts
        its way back."""
        page = self._rendered(client, self._plan(self._one("Ada")))
        assert 'id="btn-sched-home"' in page

    def test_and_a_way_into_it_from_the_home_view(self, client):
        page = self._rendered(client, self._plan(self._one("Ada")))
        assert 'id="btn-schedule"' in page


class TestTheScheduleApi:
    def _plan(self, *visits):
        from apt_log import schedule as sched

        return sched.parse({"zone": "America/New_York",
                            "visits": list(visits)})

    def test_it_answers_with_the_round(self, client):
        plan = self._plan({"patient": "Ada", "app": "com.hhaexchange.uma",
                           "start": "06:00", "end": "09:00",
                           "days": ["mon", "tue", "wed", "thu", "fri",
                                    "sat", "sun"]})
        with patch("apt_log.schedule.load", return_value=plan):
            doc = client.get("/api/schedule").json()
        assert doc["ok"] is True
        assert doc["week"] and doc["week"][0]["patient"] == "Ada"

    def test_the_package_is_translated_into_the_name_she_uses(self, client):
        """The file names packages because that is what has to be opened.
        A tile says HHAeXchange+."""
        plan = self._plan({"patient": "Ada", "app": "com.hhaexchange.uma",
                           "start": "06:00", "end": "09:00",
                           "days": ["mon", "tue", "wed", "thu", "fri",
                                    "sat", "sun"]})
        with patch("apt_log.schedule.load", return_value=plan):
            doc = client.get("/api/schedule").json()
        assert doc["week"][0]["app"] == "HHAeXchange+"

    def test_times_are_formatted_on_the_server(self, client):
        """The phone reading this page may be in another state — Texas has
        been used for exactly this project's testing — and a browser
        formatting an instant would render Eastern visits in whatever zone
        the reader is standing in."""
        plan = self._plan({"patient": "Ada", "app": "com.hhaexchange.uma",
                           "start": "06:00", "end": "09:00",
                           "days": ["mon", "tue", "wed", "thu", "fri",
                                    "sat", "sun"]})
        with patch("apt_log.schedule.load", return_value=plan):
            doc = client.get("/api/schedule").json()
        # AND IT SAYS WHICH ZONE. Rendering the right hour is only half of
        # it: an hour with no zone on it is one the reader has to guess at,
        # and the guess is wrong for everyone outside Florida. Reported from
        # Central — "I'm getting confused with my CDT local time".
        assert doc["week"][0]["starts"].startswith("6:00 am")
        assert doc["week"][0]["starts"].split()[-1] in ("EDT", "EST")

    def test_a_bad_schedule_is_reported_rather_than_thrown(self, client):
        from apt_log import schedule as sched

        with patch("apt_log.schedule.load",
                   side_effect=sched.BadSchedule("nope")):
            r = client.get("/api/schedule")
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert r.json()["error"] == "nope"




class TestTheCardUnderIt:
    """The one visit after the one above, and nothing else.

    This was a drum that turned on a timer for exactly one session. Asked to
    stop: "I don't like the behaviour of the wheel, maybe simple is better
    and just displays the next patient." Motion nobody asked for, on the part
    of the page somebody is actually trying to read, was the wrong answer —
    and simpler is also less to go wrong.
    """

    def _js(self) -> str:
        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def _css(self) -> str:
        return Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")

    def test_no_timer_moves_anything_on_the_page(self):
        """Three intervals are left and each earns it: re-reading the
        schedule, the clock (which is a clock), and the sign-in code's age.

        The property is not the COUNT — it is that no timer animates a
        control. So the allowed set is named rather than tallied, which says
        what is actually being defended and makes the next addition an
        argument somebody has to write down instead of a number to bump.

        The code's age earns its place the same way the clock does: it is a
        reading that goes stale on a page nobody is touching, and a code
        labelled "1 minute ago" that is really nine is the one failure that
        card exists to prevent.
        """
        js = self._js()
        intervals = [l.strip() for l in js.splitlines() if "setInterval" in l]
        allowed = ("refreshSchedule", "tick", "refreshCode")
        for line in intervals:
            assert any(name in line for name in allowed), (
                f"unexplained timer: {line}")
        for name in allowed:
            assert any(name in line for line in intervals), f"{name} timer lost"

    def test_the_wheel_is_gone_rather_than_hidden(self):
        js = self._js()
        for ghost in ("wheel__inner", "turnWheelTo", "layOutWheel",
                      "startWheel", "DWELL"):
            assert ghost not in js

    def test_it_is_padded_like_the_card_above_it(self):
        """The drum's faces sat flush to the edge and the two cards read as
        belonging to different pages — "text is too close to the edges, make
        it uniform like the current and upcoming patient"."""
        css = self._css()
        top = css[css.index("  .upnext {"):css.index("  .upnext .cap")]
        after = css[css.index("  .after {"):css.index("  .after .cap")]
        assert "padding:14px 16px" in top
        assert "padding:14px 16px" in after

    def _painters(self):
        js = self._js()
        return (js[js.index("function paintUpNext"):js.index("function paintAfter")],
                js[js.index("function paintAfter"):js.index("function refreshSchedule")])

    def test_a_patients_name_goes_in_as_text_never_as_markup(self):
        """A patient's name is somebody else's words and this page has no
        business reading them as markup. Both cards now build the name beside
        a badge, so the name is a text NODE rather than an assignment — the
        property being guarded is the same one."""
        for body in self._painters():
            assert "createTextNode(v.patient)" in body
            # Aimed at the NAME's own element rather than at the word
            # anywhere in the painter: clearing an empty caption with
            # innerHTML is not the hazard this is about.
            assert "who.innerHTML" not in body

    def test_the_app_badge_rides_the_name_on_both_cards(self):
        """Asked for repeatedly, and it was only ever in the small grey line
        at the foot of the card — the last thing read and the first thing
        skipped — while the card is the way INTO that app. Two cards putting
        the same fact in two different places is something that has to be
        learned instead of seen, so the badge sits on the name on both."""
        for body in self._painters():
            assert "who.appendChild(badge)" in body
            assert "'appbadge'" in body

    def test_the_badge_is_rebuilt_for_the_visit_on_the_card_now(self):
        """The visit on these cards changes, and the app changes with it. A
        badge left in place would say iMT over a patient who is now on
        Exchange+ — the same class of mistake as the card that opened the
        wrong app because it kept an hour-old package."""
        for body in self._painters():
            assert "who.textContent = ''" in body

    def test_the_times_line_no_longer_holds_the_badge(self):
        """It used to, which is why rewriting that line needed a dance around
        the chip. Out of the line, the hazard is gone rather than handled."""
        _, after = self._painters()
        assert "sub.textContent" in after
        assert "chip" not in after


class TestAVisitIsAWayIntoItsApp:
    """Asked for: pressing the current or upcoming patient should send you to
    the app they belong in. Reusing `launch` rather than re-implementing it is
    what makes the already-in-front shortcut and the sign-in ceremony behave
    the same wherever she pressed."""

    def _plan(self, *visits):
        from apt_log import schedule as sched

        return sched.parse({"zone": "America/New_York",
                            "visits": list(visits)})

    def _one(self, patient, app, start="06:00", end="09:00"):
        return {"patient": patient, "app": app, "start": start, "end": end,
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}

    def _page(self, client, plan):
        with patch("apt_log.schedule.load", return_value=plan):
            return client.get("/app", headers={"Accept-Language": "en"}).text

    def test_the_card_carries_the_app_it_belongs_to(self, client):
        page = self._page(client, self._plan(
            self._one("Ada", "com.hhaexchange.uma")))
        card = page[page.index('id="upnext"') - 200:page.index('id="upnext"') + 300]
        assert 'data-package="com.hhaexchange.uma"' in card
        assert 'data-macro="hhax_uma_login"' in card

    def _two_apps(self):
        return self._plan(
            self._one("Ada", "com.hhaexchange.uma"),
            self._one("Bea", "com.tellus.evv.v2", "10:00", "12:00"))

    def _after(self, client, running, *upcoming):
        """The after-card, for a plan told outright what is running and what
        is still to come.

        THE CLOCK IS NOT A FIXTURE. The page picks a different visit for the
        card below depending on whether a visit is running, and both answers
        are correct layouts — so a test that lets the hour decide is testing
        whichever one it happened to run in. The earlier version asked for a
        block at 10:00–12:00 and passed every time except between ten and
        noon, when it reported a bug the page does not have.

        `running` and `upcoming` are real `Visit`s, lifted off a day far
        enough away that today's hour cannot reach them.
        """
        from apt_log import schedule as sched

        plan = self._two_apps()
        with patch.object(sched.Schedule, "current", return_value=running), \
             patch.object(sched.Schedule, "upcoming",
                          return_value=list(upcoming)):
            page = self._page(client, plan)
        return page[page.index('id="after"'):page.index('id="btn-schedule"')]

    def _ada_and_bea(self):
        from datetime import date, timedelta

        day = self._two_apps().on(date.today() + timedelta(days=3))
        return (next(v for v in day if v.patient == "Ada"),
                next(v for v in day if v.patient == "Bea"))

    def test_the_second_card_carries_its_own(self, client):
        """The two visits are usually on different apps, so the second card
        cannot borrow the first's. Nothing running: the cards are the first
        and the second visit still to come."""
        ada, bea = self._ada_and_bea()
        after = self._after(client, None, ada, bea)
        assert 'data-package="com.tellus.evv.v2"' in after
        assert 'data-macro="mobile_caregiver_pin"' in after

    def test_and_while_a_visit_is_running_it_carries_the_genuine_next(
            self, client):
        """THIS IS THE CASE THAT WAS BROKEN.

        While a visit is RUNNING the card above holds the current one, and the
        card below was taking `queue[0]` — the visit after NEXT. The genuine
        next visit appeared nowhere on the home screen. Ada is running here, so
        the card below must be Bea rather than whoever follows her.
        """
        ada, bea = self._ada_and_bea()
        after = self._after(client, ada, bea, ada)
        assert 'data-package="com.tellus.evv.v2"' in after
        assert 'data-macro="mobile_caregiver_pin"' in after

    def test_the_press_goes_through_the_same_launch_the_tiles_use(self):
        js = strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))
        body = js[js.index("function openVisitsApp"):js.index("function wireSchedule")]
        assert "launch(target)" in body

    def test_a_visit_on_an_app_with_no_tile_carries_no_macro(self, client):
        """A schedule can name a package that is not one of the three. The
        card still renders — it is information — and there is nothing to
        press."""
        page = self._page(client, self._plan(
            self._one("Ada", "com.example.notinstalled")))
        card = page[page.index('id="upnext"') - 200:page.index('id="upnext"') + 300]
        assert 'data-macro=""' in card


class TestTheAppsFitOnOneRow:
    def test_three_across(self, client):
        """Two columns left a hole where a fourth app used to be and pushed
        everything below it down a whole tile."""
        page = client.get("/app").text
        css = page[page.index("  .springboard {"):page.index("  .tile {")]
        assert "repeat(3, 1fr)" in css


class TestBackStaysInsideTheApp:
    """Reported plainly: "navigating back or forward on the Exchange+ app is
    very unpredictable, the back button even takes me to the previous app I
    was on, which is not ok".

    The first version of the guard only noticed Back landing on the LAUNCHER,
    which is one of the two ways Android leaves an app and not the common
    one. Back from an app's root pops the task stack, and what is under it is
    whatever she was in before.
    """

    def _js(self) -> str:
        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def test_the_test_is_whether_the_app_changed(self):
        js = self._js()
        assert "currentPackage !== wasPackage" in js

    def test_and_not_merely_whether_this_is_the_launcher(self):
        """The old condition. If it comes back, so does the bug."""
        js = self._js()
        assert "if (onLauncher && wasScreen" not in js

    def test_drifting_into_another_care_app_does_not_eject_her(self):
        """Only the launcher sends her to the picker. Landing in a second
        care app is the phone doing something she did not ask for, and the
        answer is to put her back rather than to throw her out."""
        js = self._js()
        # The guard is `onLauncher`, so no other arrival can reach the eject
        # at all. Pinned as the condition rather than as a whole line: the
        # branch has since gained a second clause, holding the screen view so
        # the home-screen card's way back is actually seen.
        i = js.index("} else if (onLauncher && body.dataset.view === 'screen'")
        assert "view('launcher')" in js[i:i + 900]

    def test_leaving_on_purpose_still_works(self):
        """Home is how you leave. It is wired separately and does not go
        through the bounce at all."""
        js = self._js()
        assert "getElementById('btn-home')" in js


class TestTheWayBackToAnAppsOwnFirstPage:
    def test_the_control_is_on_the_phone_view(self, client):
        page = client.get("/app").text
        assert 'id="btn-apphome"' in page

    def test_it_runs_the_macro_that_navigates(self):
        js = strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))
        block = js[js.index("getElementById('btn-apphome')"):]
        assert "name: 'app_home'" in block[:600]

    def test_it_is_refused_while_watching_rather_than_driving(self):
        """Every control that touches the phone is, and this one moves the
        app somebody else may be reading."""
        js = strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))
        block = js[js.index("getElementById('btn-apphome')"):]
        assert "if (!driving()) return;" in block[:300]


class TestTheArmingPage:
    """A control page for what the scheduler may act on. Everything ships
    off, and the page says what a switch means today rather than implying a
    capability that is not built."""

    def _plan(self, *visits):
        from apt_log import schedule as sched

        return sched.parse({"zone": "America/New_York",
                            "visits": list(visits)})

    def _one(self, patient, app="com.hhaexchange.uma", start="06:00",
             end="09:00", **extra):
        return {"patient": patient, "app": app, "start": start, "end": end,
                "days": ["mon", "wed"], **extra}

    def _page(self, client, plan):
        with patch("apt_log.schedule.load", return_value=plan):
            return client.get("/app", headers={"Accept-Language": "en"}).text

    def test_every_recurring_block_gets_a_switch(self, client):
        page = self._page(client, self._plan(
            self._one("Ada"), self._one("Bea", start="10:00", end="12:00")))
        arm = page[page.index('id="armview"'):]
        assert arm.count('class="sw"') == 2

    def test_one_row_per_block_and_not_per_day(self, client):
        """The week view lists a Monday-and-Wednesday visit twice. Arming is
        a standing decision and would be asked twice over."""
        page = self._page(client, self._plan(self._one("Ada")))
        arm = page[page.index('id="armview"'):]
        assert arm.count('class="sw"') == 1

    def test_everything_starts_off(self, client):
        page = self._page(client, self._plan(self._one("Ada")))
        arm = page[page.index('id="armview"'):]
        assert 'aria-pressed="true"' not in arm

    def test_the_page_says_what_a_switch_actually_does(self, client):
        """IT USED TO SAY NOTHING FIRES YET, and that has stopped being true.
        A page still carrying that sentence over switches that now write EVV
        records would be the most dangerous thing on the portal."""
        page = self._page(client, self._plan(self._one("Ada")))
        t = Translator("en")
        assert t.t("arm.note") in page
        assert "not fire" not in t.t("arm.note").lower()
        assert "nothing" not in t.t("arm.note").lower()

    def test_and_says_that_arming_is_a_claim_about_where_she_is(self, client):
        """REQ-5.9. Arming is an attestation of presence, not a preference,
        and a page that presents it as a setting gets a switch thrown for the
        wrong reason."""
        page = self._page(client, self._plan(self._one("Ada")))
        assert Translator("en").t("arm.means") in page

    def test_a_switch_that_cannot_fire_says_so_on_its_face(self, client):
        """Reading "armed" over a visit nobody is going to check in is worse
        than reading nothing at all.

        The example used to be HHAeXchange+, whose check-in was unwalked
        until 2026-08-21. The retired legacy app carries the rule now — the
        point was never that one particular app was unwalked, it is that an
        unwalked one must not wear a live-looking switch.
        """
        page = self._page(client, self._plan(
            self._one("Ada", app="com.hhaexchange.caregiver")))
        arm = page[page.index('id="armview"'):]
        assert "inert" in arm
        # The broader reason: nobody has mapped this app at all. The narrower
        # "control_not_walked" was for an app whose screens were walked but
        # whose check-in button had never been seen — which described
        # HHAeXchange+ exactly, until it was walked.
        assert Translator("en").t("arm.why.app_not_walked") in arm

    def test_a_switch_that_can_fire_carries_no_warning(self, client):
        page = self._page(client, self._plan(
            self._one("Ada", app="com.tellus.evv.v2")))
        arm = page[page.index('id="armview"'):]
        assert "inert" not in arm
        assert Translator("en").t("arm.why.control_not_walked") not in arm

    def test_the_switch_is_identified_by_a_key_not_by_a_name(self, client):
        """What gets POSTed, stored and logged is a hash. The patient's name
        is on the row beside it and in the switch's own aria-label, which is
        right — a bare toggle with no label is worse for somebody using a
        screen reader than one that says whose visit it is. The rule is about
        what travels, not about what is displayed."""
        import re

        page = self._page(client, self._plan(self._one("Ada")))
        arm = page[page.index('id="armview"'):]
        key = re.search(r'data-key="([^"]*)"', arm).group(1)
        assert key and "ada" not in key.lower()
        assert all(c in "0123456789abcdef" for c in key)

    def test_there_is_a_way_in_and_a_way_home(self, client):
        page = client.get("/app").text
        assert 'id="btn-arm"' in page
        assert 'id="btn-arm-home"' in page


class TestThrowingASwitchFromThePage:
    def test_it_records_the_choice(self, client):
        from apt_log import arming

        r = client.post("/schedule/arm", data={"key": "abc123", "on": "1"})
        assert r.status_code == 200
        assert r.json()["armed"] is True
        assert "abc123" in arming.armed()

    def test_and_takes_it_back(self, client):
        from apt_log import arming

        client.post("/schedule/arm", data={"key": "abc123", "on": "1"})
        r = client.post("/schedule/arm", data={"key": "abc123", "on": "0"})
        assert r.json()["armed"] is False
        assert arming.armed() == set()

    def test_anything_that_is_not_a_one_is_off(self, client):
        """A missing or odd value must not arm something."""
        r = client.post("/schedule/arm", data={"key": "abc123"})
        assert r.json()["armed"] is False

    def test_a_long_key_is_cut_rather_than_stored(self, client):
        from apt_log import arming

        client.post("/schedule/arm", data={"key": "x" * 500, "on": "1"})
        assert all(len(k) <= 64 for k in arming.armed())

    def test_the_switch_waits_for_the_server(self):
        """An optimistic flip leaves a control reading "on" over a machine
        that never recorded it — which, for this switch, is the worst
        possible way to be wrong."""
        js = strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))
        body = js[js.index("function wireArming"):]
        # The attribute is only written from the server's answer.
        assert "sw.setAttribute('aria-pressed', doc.armed" in body
        assert body.index("fetch('/schedule/arm'") < body.index("setAttribute")


class TestTheHomeScreenUsesItsHeight:
    """Asked for: "there needs to be more vertical spacing for the content in
    the home page, everything is too close to the top and neglects the white
    space below closer to the language controls"."""

    def _css(self) -> str:
        return Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")

    def test_the_schedule_block_takes_the_slack_at_both_ends(self):
        """Three auto margins on one flex column is the browser doing the
        arithmetic, which beats a number that would be wrong on the next
        screen size."""
        css = self._css()
        rule = css[css.index("  #upnext-wrap {"):css.index("  #launcher footer {")]
        assert "margin-top:auto" in rule
        assert "margin-bottom:auto" in rule

    def test_every_control_on_the_launcher_is_styled(self, client):
        """A <button> with no rule of its own paints the user agent's own
        control chrome — a half-width white box on this background. It has
        happened twice now: once to the drum's faces, once to the
        full-schedule button when the drum's CSS block was cut out around
        it."""
        page = client.get("/app").text
        launcher = page[page.index('<div class="view" id="launcher">'):
                        page.index('<div class="view" id="screenview">')]
        import re

        classes = set()
        for tag in re.findall(r"<button[^>]*>", launcher):
            found = re.search(r'class="([^"]*)"', tag)
            classes.update((found.group(1) if found else "").split())
        assert classes, "no buttons found — the slice is wrong"
        for name in classes:
            assert ".%s {" % name in page or ".%s{" % name in page, \
                f"{name} has no rule of its own"


class TestTheWeekIsMadeOfWaysIntoApps:
    """Asked for: the app's badge on every row, and pressing the row opens
    that patient's app — with its agency, for the one app that carries more
    than one."""

    def _plan(self, *visits):
        from apt_log import schedule as sched

        return sched.parse({"zone": "America/New_York",
                            "visits": list(visits)})

    def _one(self, patient, app="com.hhaexchange.uma", start="06:00",
             end="09:00", **extra):
        return {"patient": patient, "app": app, "start": start, "end": end,
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                **extra}

    def _week(self, client, plan):
        with patch("apt_log.schedule.load", return_value=plan):
            page = client.get("/app", headers={"Accept-Language": "en"}).text
        return page[page.index('id="scheduleview"'):page.index('id="armview"')]

    def test_every_row_carries_its_apps_badge(self, client):
        week = self._week(client, self._plan(
            self._one("Ada", "com.hhaexchange.uma"),
            self._one("Bea", "com.tellus.evv.v2", "10:00", "12:00")))
        assert "HX+" in week and "MC" in week

    def test_every_row_is_a_way_into_its_app(self, client):
        week = self._week(client, self._plan(self._one("Ada")))
        assert 'data-macro="hhax_uma_login"' in week

    def test_a_row_carries_the_agency_it_belongs_to(self, client):
        week = self._week(client, self._plan(
            self._one("Ada", agency="Some Agency")))
        assert 'data-agency="Some Agency"' in week

    def test_pressing_a_multi_agency_row_switches_agency_rather_than_signing_in(
            self):
        """One macro, not two. The agency walk activates the app itself, so
        running the sign-in as well would race a provider switch against a
        sign-in ceremony on the same phone."""
        js = strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))
        body = js[js.index("function openVisitsApp"):js.index("function wireSchedule")]
        assert "name: 'uma_agency_for', arg: agency" in body
        assert body.index("return;") < body.index("launch(target)")

    def test_a_single_agency_app_just_launches(self):
        js = strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))
        body = js[js.index("function openVisitsApp"):js.index("function wireSchedule")]
        assert "launch(target)" in body

    def test_the_week_listens_once_rather_than_forty_times(self):
        """Forty rows, one listener."""
        js = strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))
        assert "querySelector('#scheduleview .body')" in js


class TestTheWayBackToAgencySelection:
    """HHAeXchange+ carries Rainbow and Fatima on one account and the round
    crosses between them, so reaching the other one is routine — and it was
    four taps deep behind a menu whose rows name nothing useful.

    Pressing a patient already routes to the right agency; this is the
    control for the times nobody pressed a patient.
    """

    def _js(self):
        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def _page(self):
        return Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")

    def test_the_toolbar_carries_the_control(self):
        assert 'id="btn-agency"' in self._page()

    def test_it_starts_hidden(self):
        """Shown-then-hidden flashes a control into the toolbar on first
        paint and takes it away a tick later — the same reason the pencil
        starts hidden."""
        page = self._page()
        button = page[page.index('id="btn-agency"'):]
        assert "hidden" in button[:button.index(">")]

    def test_it_presses_the_macro_that_stops_at_the_picker(self):
        """`uma_agency`, not `uma_agency_for`. From the toolbar there is no
        patient to read an agency off, and guessing "the other one" would be
        wrong the moment a third appears."""
        js = self._js()
        body = js[js.index("const agencyRun"):]
        body = body[:body.index("const scanClose")]
        assert "name: 'uma_agency'" in body
        assert "uma_agency_for" not in body

    def test_it_refuses_while_somebody_else_is_driving(self):
        js = self._js()
        body = js[js.index("const agencyRun"):]
        assert "if (!driving()) return;" in body[:body.index("fetch(")]

    def test_it_is_offered_only_where_there_is_an_agency_to_switch_to(self):
        """Absent on the two single-agency apps rather than present and
        failing — a button that works on one app in three teaches that it
        does not work."""
        js = self._js()
        assert "agencyBtn.hidden = !meta.agency_app || onLauncher" in js

    def test_the_server_says_which_apps_have_more_than_one_agency(self):
        from apt_log import macros

        assert macros.MULTI_AGENCY_APPS == ("com.hhaexchange.uma",)

    def test_the_page_and_the_server_name_the_same_multi_agency_app(self):
        """Two copies of one fact, and they have to agree: the toolbar gate
        is served per screen while the schedule card's routing happens on a
        view with no screen document to read it from, so the constant cannot
        be deleted — only kept honest."""
        from apt_log import macros

        js = self._js()
        line = next(l for l in js.splitlines() if "const MULTI_AGENCY" in l)
        assert macros.MULTI_AGENCY_APPS[0] in line

    def test_the_walk_does_not_call_going_forward_going_back(self):
        """It reported `navigating`, which renders as "Going back" while the
        phone walks four taps FORWARD through a menu."""
        from apt_log import macros

        src = Path("src/apt_log/macros.py").read_text(encoding="utf-8")
        body = src[src.index("def _walk_to_agency_picker"):]
        body = body[:body.index("def _by_id")]
        assert 'report("macro.step.opening_the_agencies")' in body
        assert 'report("macro.step.navigating")' not in body

    def test_both_agency_steps_are_written_in_both_languages(self):
        import json

        for code in ("en", "es"):
            words = json.loads(Path(
                f"src/apt_log/ui/locales/{code}.json").read_text(
                    encoding="utf-8"))
            assert words["macro.step.opening_the_agencies"]
            assert words["macro.step.switching_agency"]

    def _meta_for(self, client, tmp_path, package):
        """What the socket tells the page about the app in front.

        It crosses as `screen`; the page binds it to a local called `meta`,
        which is the name the toolbar gates read.
        """
        doc = {"at": "2026-08-29T16:00:00", "app": package,
               "activity": "homeactivity", "blocked": "",
               "elements": [], "size": [1080, 2340],
               "statics": [{"cls": "TextView", "b": [0, 100, 500, 140],
                            "txt": "agosto 29, 2026"}]}
        (tmp_path / "screen.json").write_text(json.dumps(doc),
                                              encoding="utf-8")
        with patch.object(state_mod, "STATE_DIR", tmp_path):
            with client.websocket_connect("/ws") as ws:
                msg = ws.receive_json()
        return msg.get("screen") or {}

    def test_the_socket_says_hhaexchange_has_an_agency_to_switch_to(
            self, client, tmp_path):
        meta = self._meta_for(client, tmp_path, "com.hhaexchange.uma")
        assert meta.get("agency_app") is True

    def test_the_socket_says_the_single_agency_apps_do_not(
            self, client, tmp_path):
        """inMyTeam and Mobile Caregiver+ hold one agency each, so the
        picker the control walks to does not exist on either."""
        for package in ("com.inmyteam.inmyteam", "com.tellus.evv.v2"):
            meta = self._meta_for(client, tmp_path, package)
            assert meta.get("agency_app") is False, package


class TestASplitVisitReadsAsEnterAndLeave:
    """The agency's rule on the page: enter on the first half, leave on the
    last, nothing at the seam."""

    def _plan(self, *visits):
        from apt_log import schedule as sched

        return sched.parse({"zone": "America/New_York",
                            "visits": list(visits)})

    def _week(self, client, plan):
        with patch("apt_log.schedule.load", return_value=plan):
            page = client.get("/app", headers={"Accept-Language": "en"}).text
        return page[page.index('id="scheduleview"'):page.index('id="armview"')]

    def _pair(self):
        base = {"app": "com.hhaexchange.uma", "days": ["mon"],
                "patient": "Ada"}
        return self._plan({**base, "start": "20:05", "end": "21:05",
                           "part": 1, "of": 2},
                          {**base, "start": "21:05", "end": "22:05",
                           "part": 2, "of": 2})

    def test_the_halves_are_labelled_differently(self, client):
        week = self._week(client, self._pair())
        assert Translator("en").t("sched.check_in") in week
        assert Translator("en").t("sched.check_out") in week

    def test_the_check_out_shows_the_hour_it_happens(self, client):
        """Which is the END of the later half, not its start — the one time
        on the page that is not the row's own fire time."""
        week = self._week(client, self._pair())
        assert "10:05 pm" in week

    def test_a_single_block_says_neither(self, client):
        week = self._week(client, self._plan(
            {"patient": "Ada", "app": "com.hhaexchange.uma", "days": ["mon"],
             "start": "20:05", "end": "22:05"}))
        assert Translator("en").t("sched.check_in") not in week

    def test_the_api_carries_the_two_moments(self, client):
        with patch("apt_log.schedule.load", return_value=self._pair()):
            doc = client.get("/api/schedule",
                             headers={"Accept-Language": "en"}).json()
        halves = [v for v in doc["week"] if v["of"] == 2]
        assert [h["does_entry"] for h in halves] == [True, False]
        assert [h["does_exit"] for h in halves] == [False, True]
        assert halves[0]["exit_at"] == "" and halves[1]["entry_at"] == ""


class TestTheDaysAreTranslated:
    """`strftime("%A")` answers in the C locale, which is English — and a
    Spanish page reading "Thursday" is the kind of miss that survives for
    months because everything around it is translated."""

    def _plan(self):
        from apt_log import schedule as sched

        return sched.parse({"zone": "America/New_York", "visits": [
            {"patient": "Ada", "app": "com.hhaexchange.uma",
             "start": "06:00", "end": "09:00",
             "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}]})

    def test_the_week_speaks_spanish_on_a_spanish_page(self, client):
        # The rendered week only. The page's own stylesheet has English prose
        # in its comments, which is not what anybody reads off the screen —
        # a guard that cannot tell content from commentary forbids explaining
        # the thing it exists to protect, and this project has been round
        # that loop three times already.
        with patch("apt_log.schedule.load", return_value=self._plan()):
            page = client.get("/app", headers={"Accept-Language": "es"}).text
        week = page[page.index('id="scheduleview"'):page.index('id="armview"')]
        assert "Thursday" not in week and "Monday" not in week
        assert any(d in week for d in ("Lunes", "Jueves", "Domingo"))

    def test_and_english_on_an_english_one(self, client):
        with patch("apt_log.schedule.load", return_value=self._plan()):
            page = client.get("/app", headers={"Accept-Language": "en"}).text
        week = page[page.index('id="scheduleview"'):page.index('id="armview"')]
        assert any(d in week for d in ("Monday", "Thursday", "Sunday"))

    def test_the_arming_page_too(self, client):
        with patch("apt_log.schedule.load", return_value=self._plan()):
            page = client.get("/app", headers={"Accept-Language": "es"}).text
        arm = page[page.index('id="armview"'):]
        assert "Mon" not in arm and "Wed" not in arm

    def test_the_api_follows_the_readers_language(self, client):
        """It repaints the card an hour later; the day must not switch
        language when it does."""
        with patch("apt_log.schedule.load", return_value=self._plan()):
            doc = client.get("/api/schedule",
                             headers={"Accept-Language": "es"}).json()
        assert doc["week"][0]["day"] in ("Lunes", "Martes", "Miércoles",
                                         "Jueves", "Viernes", "Sábado",
                                         "Domingo")


class TestThePadsButtonDoesNotLookLikeTheFinish:
    """Six signature replays in seven minutes and not one press of the app's
    own button. She drew, pressed what read as Send, watched the signature be
    redrawn, and did it again — because the pad's button said "Enviar al
    teléfono" while the app's button two rows down said "Enviar".

    The pad's button DRAWS. It has never submitted anything.
    """

    def _js(self) -> str:
        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def test_the_word_send_is_gone_from_the_pads_own_button(self):
        for lang in ("en", "es"):
            word = Translator(lang).t("sign.send").lower()
            assert "send" not in word and "enviar" not in word

    def test_it_says_what_it_actually_does(self):
        assert "draw" in Translator("en").t("sign.send").lower()
        assert "dibujar" in Translator("es").t("sign.send").lower()

    def test_once_the_ink_lands_the_button_renames_itself(self):
        """Pressing it again only redraws, which is the loop that happened."""
        js = self._js()
        assert "markDrawn" in js
        body = js[js.index("function markDrawn"):]
        assert "signSendAgain" in body[:500]

    def test_and_a_line_appears_saying_where_the_finish_is(self, client):
        page = client.get("/app", headers={"Accept-Language": "en"}).text
        assert 'id="sign-hint"' in page
        assert Translator("en").t("sign.now_press") in page

    def test_the_hint_is_hidden_until_then(self, client):
        page = client.get("/app").text
        hint = page[page.index('id="sign-hint"') - 60:
                    page.index('id="sign-hint"') + 60]
        assert "hidden" in hint

    def test_drawing_again_puts_the_sheet_back_to_step_one(self):
        js = self._js()
        assert "markDrawn(false)" in js

    def test_there_is_no_handset_glyph_left_to_break(self):
        """Two attempts at drawing one, two reports of it rendering as an
        empty box. The row's own heading says to finish on the phone, and a
        sentence cannot be misdrawn."""
        css = Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")
        assert ".padrow.onphone button::before" not in css
        assert ".padrow.onphone button::after" not in css

    def test_step_two_is_the_one_emphasised_once_the_ink_has_landed(self):
        css = Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")
        # The id, not a class of the same name — the sheet is
        # `<div id="signsheet" class="sheet">`, and `.signsheet` matched
        # nothing at all. Caught in a screenshot: the button stayed a filled
        # primary after the ink had landed, which is the exact thing this
        # change exists to stop.
        assert "#signsheet.drawn .approw" in css
        assert "#signsheet.drawn .signsend" in css


class TestTheHomeScreenNeverSkipsAVisit:
    """Found by a test that started failing at 07:35 on a Friday and looked
    like a clock-dependent flake. The hour was only what made it visible.

    `#upnext` shows the CURRENT visit when there is one, so `#after` has to
    show `next` — not `queue[0]`, which is the visit after next. During a
    visit the home screen was showing the current one and the one after next,
    and the genuine next visit appeared nowhere at all: the single moment she
    is least able to go hunting for it.
    """

    def _plan(self, *visits):
        from apt_log import schedule as sched

        return sched.parse({"zone": "America/New_York",
                            "visits": list(visits)})

    def _every_day(self, patient, app, start, end):
        return {"patient": patient, "app": app, "start": start, "end": end,
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}

    def test_the_after_card_binds_to_next_while_one_is_running(self):
        from pathlib import Path

        markup = (Path(__file__).resolve().parents[1]
                  / "src/apt_log/ui/templates/phone.html").read_text(
                      encoding="utf-8")
        assert "plan.next if plan.current" in markup

    def test_the_two_cards_are_never_the_same_visit(self, client):
        """Whatever the hour, the two cards hold two different visits."""
        import re

        plan = self._plan(
            self._every_day("Ada", "com.hhaexchange.uma", "06:00", "09:00"),
            self._every_day("Bea", "com.tellus.evv.v2", "10:00", "12:00"))
        with patch("apt_log.schedule.load", return_value=plan):
            page = client.get("/app",
                              headers={"Accept-Language": "en"}).text
        upnext = page[page.index('id="upnext"'):page.index('id="after"')]
        after = page[page.index('id="after"'):page.index('id="btn-schedule"')]
        first = re.search(r'data-package="([^"]*)"', upnext).group(1)
        second = re.search(r'data-package="([^"]*)"', after).group(1)
        assert first and second
        assert first != second


class TestTheAppsOwnTrail:
    """Asked for: "leverage the fragments in our back button so we don't
    escape apps", and "display the fragments as linked list breadcrumbs".

    `dumpsys activity` publishes the fragment back stack, which gives three
    things the atlas cannot: the screen's own name, the trail behind it, and
    — from an empty stack — the fact that the next Back press would leave the
    app, known BEFORE it is sent rather than after it has happened.
    """

    def _doc(self, trail, says, depth):
        return {"id": "f1", "size": [720, 1600], "app": "com.inmyteam.inmyteam",
                "nav": {"at": trail[-1], "at_says": says[-1], "trail": trail,
                        "says": says, "depth": depth, "rooted": depth == 0},
                "elements": [], "statics": []}

    def test_a_trail_of_one_is_not_a_trail(self):
        from apt_log.ui import screenview

        assert screenview._crumbs(
            self._doc(["VisitsFragment"], ["Visits"], 0)) == []

    def _walked(self, screenview):
        """Stand on the list, then open the work log — which is how the
        portal learns what each screen calls itself. See
        `screenview.remember_screen_name`."""
        screenview._crumbs(self._doc(["VisitsFragment"], ["Visits"], 0),
                           "Visitas")
        return screenview._crumbs(
            self._doc(["VisitsFragment", "MyWorksFragment"],
                      ["Visits", "My Works"], 1), "Mis trabajos")

    def test_each_step_says_how_many_pops_away_it_is(self):
        from apt_log.ui import screenview

        crumbs = self._walked(screenview)
        assert [c["back"] for c in crumbs] == [1, 0]

    def test_the_last_step_is_where_she_is(self):
        """Nothing should offer to take her where she already stands."""
        from apt_log.ui import screenview

        assert [c["here"] for c in self._walked(screenview)] == [False, True]

    def test_the_trail_is_spelled_in_the_apps_own_words(self):
        """IT WAS SPELLED IN CLASS NAMES, and that is what a fragment stack
        gives you: "Visits Route › Visits", English, inside a Spanish app, in
        a word the app never uses and only its developers have ever seen.
        Reported from the phone as a breadcrumb with no direction.

        The app does say what a screen is called — it writes it in the title
        bar — so the title is remembered against the fragment standing on it
        and the trail is spelled with those words afterwards.
        """
        from apt_log.ui import screenview

        assert [c["says"] for c in self._walked(screenview)] == [
            "Visitas", "Mis trabajos"]

    def test_a_step_nobody_ever_stood_on_is_left_out(self):
        """The stack carries container fragments that are not screens — a
        "route" holding the pages inside it. They have no title because they
        were never drawn as a place, and printing their class name is the
        thing being fixed. Dropping one changes no other step's `back`,
        which is counted from the stack, not from what is shown."""
        from apt_log.ui import screenview

        screenview._crumbs(self._doc(["VisitsFragment"], ["Visits"], 0),
                           "Visitas")
        crumbs = screenview._crumbs(
            self._doc(["VisitsRouteFragment", "VisitsFragment",
                       "MyWorksFragment"],
                      ["Visits Route", "Visits", "My Works"], 2),
            "Mis trabajos")
        assert [c["says"] for c in crumbs] == ["Visitas", "Mis trabajos"]
        # The pops still count the whole stack, container included.
        assert [c["back"] for c in crumbs] == [1, 0]

    def test_the_sign_in_is_not_somewhere_to_walk_back_to(self):
        """READ OFF THE LIVE PHONE: inMyTeam's back stack was TWENTY-TWO
        entries deep and began with the whole sign-in — Visits, Intro,
        Intro, Intro, …, Login, Verify Code, Schedule, Schedule Visit
        Detail. Not a parsing mistake; the app really does push every step
        and never pops one. So the trail was factually right and completely
        useless, and it offered to walk her back into the passcode screen
        and the one where the texted code goes.

        A breadcrumb is how to step back, not everything since launch.
        """
        from apt_log.ui import screenview

        whole = ["VisitsFragment", "IntroFragment", "LoginFragment",
                 "VerifyCodeFragment", "ScheduleFragment",
                 "ScheduleVisitDetailFragment"]
        says = ["Visits", "Intro", "Login", "Verify Code", "Schedule",
                "Schedule Visit Detail"]
        pkg = "com.inmyteam.inmyteam"
        screenview._crumbs(
            {"id": "f", "app": pkg,
             "nav": {"trail": whole[:5], "says": says[:5]}}, "Programación")
        crumbs = screenview._crumbs(
            {"id": "f", "app": pkg, "nav": {"trail": whole, "says": says}},
            "Detalle de la Visita")
        assert [c["says"] for c in crumbs] == ["Programación",
                                               "Detalle de la Visita"]
        assert not any(w in c["says"].lower()
                       for c in crumbs
                       for w in ("login", "verify", "código", "intro"))

    def test_the_pops_still_count_the_whole_stack(self):
        """What is SHOWN is a question of usefulness; what a step costs to
        walk back to is a fact about the phone. Confusing the two would send
        Back the wrong number of times — and this trail hides four steps."""
        from apt_log.ui import screenview

        whole = ["VisitsFragment", "IntroFragment", "LoginFragment",
                 "VerifyCodeFragment", "ScheduleFragment",
                 "ScheduleVisitDetailFragment"]
        pkg = "com.inmyteam.inmyteam"
        screenview._crumbs(
            {"id": "f", "app": pkg, "nav": {"trail": whole[:5], "says": []}},
            "Programación")
        crumbs = screenview._crumbs(
            {"id": "f", "app": pkg, "nav": {"trail": whole, "says": []}},
            "Detalle de la Visita")
        # Schedule is one pop from the detail, not one from the front of a
        # shortened list.
        assert [c["back"] for c in crumbs] == [1, 0]

    def test_a_trail_is_short_by_nature(self):
        from apt_log.ui import screenview

        deep = [f"Step{i}Fragment" for i in range(12)]
        pkg = "com.deep.app"
        for i, name in enumerate(deep):
            screenview.remember_screen_name(pkg, name, f"Paso {i}")
        crumbs = screenview._crumbs(
            {"id": "f", "app": pkg, "nav": {"trail": deep, "says": []}},
            "Paso 11")
        assert len(crumbs) == screenview.CRUMB_MAX
        assert crumbs[-1]["back"] == 0
        assert crumbs[0]["back"] == screenview.CRUMB_MAX - 1

    def test_one_app_never_borrows_another_apps_words(self):
        """Fragment names are per-package: two apps can each have a
        `HomeFragment` meaning quite different things."""
        from apt_log.ui import screenview

        screenview.remember_screen_name("com.a", "HomeFragment", "Inicio")
        assert screenview.screen_name("com.b", "HomeFragment") == ""

    def test_an_app_that_publishes_nothing_shows_no_trail(self):
        """Two of the three care apps are not built this way, and a missing
        trail must render as nothing rather than as "you are nowhere"."""
        from apt_log.ui import screenview

        assert screenview._crumbs({"id": "f1"}) == []

    def test_the_class_name_is_spaced_not_renamed(self):
        """Inventing a friendlier word is how a breadcrumb starts lying."""
        from apt_log import feed

        assert feed._pretty_fragment("MyWorksFragment") == "My Works"
        assert feed._pretty_fragment("PlanOfCareFragment") == "Plan Of Care"

    def test_the_reader_names_the_screen_and_counts_the_pops(self,
                                                             monkeypatch):
        from apt_log import feed

        dump = """
          Added Fragments:
            #0: MyWorksFragment{66b57e2 (8fd) id=0x7f0a01ba}
          Back Stack:
            #0: BackStackEntry{ecd42a9 #9 ddf}
              Operations:
                Op #0: UNSET_PRIMARY_NAV VisitsFragment{d1b64d7 (70e)}
                Op #1: REMOVE VisitsFragment{d1b64d7 (70e)}
                Op #2: ADD MyWorksFragment{66b57e2 (8fd)}
        """
        monkeypatch.setattr(feed, "_adb", lambda *a, **k: type(
            "R", (), {"stdout": dump.encode()})())
        got = feed.nav_state("com.inmyteam.inmyteam")
        assert got["at"] == "MyWorksFragment"
        assert got["trail"] == ["VisitsFragment", "MyWorksFragment"]
        assert got["depth"] == 1
        assert got["rooted"] is False

    def test_an_empty_back_stack_is_the_escape_guard(self, monkeypatch):
        """Checked live: the visits hub reports VisitsFragment with zero
        entries, and Back from there pops the activity out of the app."""
        from apt_log import feed

        dump = "Added Fragments:\n  #0: VisitsFragment{d1b64d7 (70e)}\n"
        monkeypatch.setattr(feed, "_adb", lambda *a, **k: type(
            "R", (), {"stdout": dump.encode()})())
        got = feed.nav_state("com.inmyteam.inmyteam")
        assert got["depth"] == 0
        assert got["rooted"] is True

    def test_a_phone_that_will_not_say_answers_nothing(self, monkeypatch):
        from apt_log import feed

        def boom(*a, **k):
            raise OSError("no adb")
        monkeypatch.setattr(feed, "_adb", boom)
        assert feed.nav_state("com.inmyteam.inmyteam") == {}

    def test_back_is_refused_only_when_the_app_says_it_would_leave(
            self, monkeypatch):
        from apt_log.ui.app import _back_would_leave
        from apt_log import feed

        monkeypatch.setattr(feed, "current_focus",
                            lambda: "com.inmyteam.inmyteam/.MainActivity")
        monkeypatch.setattr(feed, "nav_state", lambda p: {"rooted": True})
        assert _back_would_leave() is True
        monkeypatch.setattr(feed, "nav_state", lambda p: {"rooted": False})
        assert _back_would_leave() is False

    def test_an_app_that_does_not_answer_is_pressed_as_before(self,
                                                              monkeypatch):
        """Two of the three care apps publish no fragment stack. "No idea"
        must mean "press it and see", never "refuse" — the alternative is a
        Back button that silently stops working on those apps."""
        from apt_log.ui.app import _back_would_leave
        from apt_log import feed

        monkeypatch.setattr(feed, "current_focus",
                            lambda: "com.hhaexchange.uma/.HomeActivity")
        monkeypatch.setattr(feed, "nav_state", lambda p: {})
        assert _back_would_leave() is False

    def test_and_a_phone_outside_the_care_apps_is_never_refused(self,
                                                                monkeypatch):
        from apt_log.ui.app import _back_would_leave
        from apt_log import feed

        monkeypatch.setattr(feed, "current_focus",
                            lambda: "com.android.launcher3/.Launcher")
        monkeypatch.setattr(feed, "nav_state",
                            lambda p: {"rooted": True})
        assert _back_would_leave() is False

    def test_the_reading_is_tied_to_the_tree_not_to_the_frame(self,
                                                              monkeypatch):
        """MEASURED AT 100ms A CALL on the Pi. The document is published every
        second and the tree behind it is read about half as often, so asking
        per frame would spend a tenth of a core on a question whose answer
        cannot have changed."""
        from apt_log import feed

        calls = []
        dump = "Added Fragments:\n  #0: VisitsFragment{d1b}\n"
        monkeypatch.setattr(feed, "_adb", lambda *a, **k: (
            calls.append(1) or type("R", (), {"stdout": dump.encode()})()))
        feed._nav_seen = ()
        for _ in range(4):
            feed.nav_state("com.inmyteam.inmyteam", stamp=1234.5)
        assert len(calls) == 1
        feed.nav_state("com.inmyteam.inmyteam", stamp=1240.0)
        assert len(calls) == 2

    def test_but_the_back_guard_always_looks_afresh(self, monkeypatch):
        """It is deciding whether to send a press right now, and a cached
        answer from two seconds ago is a press sent on stale information."""
        from apt_log import feed

        calls = []
        dump = "Added Fragments:\n  #0: VisitsFragment{d1b}\n"
        monkeypatch.setattr(feed, "_adb", lambda *a, **k: (
            calls.append(1) or type("R", (), {"stdout": dump.encode()})()))
        feed._nav_seen = ()
        feed.nav_state("com.inmyteam.inmyteam", stamp=1234.5)
        feed.nav_state("com.inmyteam.inmyteam")
        feed.nav_state("com.inmyteam.inmyteam")
        assert len(calls) == 3


class TestEveryIconShapeActuallyDraws:
    """A `/` at the end of one line and its `>` at the start of the next does
    NOT self-close the tag.

    HTML only honours the self-closing flag when the slash is immediately
    followed by the bracket. Split across a line break the slash is dropped,
    the element stays open, and everything after it becomes its CHILD — and a
    child of a `<path>` or a `<rect>` is not rendered at all.

    It reached the owner's phone as a work-log button drawn as nothing but a
    clipboard's clip: one rect, with six shapes nested inside it, invisible.
    The same style had been quietly eating the last two strokes of the
    plan-of-care icon for as long as it has existed.
    """

    def test_no_template_splits_a_self_closing_tag(self):
        import re
        from pathlib import Path

        bad = {}
        for f in sorted(Path("src/apt_log/ui").rglob("*.html")):
            hits = re.findall(r'/\s*\n\s*>', f.read_text(encoding="utf-8"))
            if hits:
                bad[str(f)] = len(hits)
        assert bad == {}

    def test_the_icons_carry_the_shapes_they_are_drawn_from(self):
        """Counted rather than eyeballed: the failure is silent, and it looks
        exactly like an icon somebody drew badly."""
        import re
        from pathlib import Path

        page = Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")

        def shapes(button_id):
            i = page.index(button_id)
            svg = page[page.index("<svg", i):page.index("</svg>", i)]
            return len(re.findall(r'<(?:path|rect|circle|line|polyline)\b', svg))

        # Every shape has to be a sibling, so the count in the source is the
        # count that draws.
        assert shapes('id="btn-checks"') == 7
        assert shapes('id="btn-tasks"') == 4
        assert shapes('id="btn-sign"') == 2
        # Two tracks and two arrowheads: swap, not reload. One head short and
        # it reads as the refresh glyph, which is a different button.
        assert shapes('id="btn-agency"') == 4


class TestTheDeselectAllIsOfferedBesideTheSelectAll:
    """"Some we have a select all we should also have a deselect all"."""

    def _page(self):
        from pathlib import Path

        return Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")

    def _js(self):
        from pathlib import Path

        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def test_the_toolbar_carries_it(self):
        assert 'id="btn-untasks"' in self._page()

    def test_it_starts_hidden(self):
        page = self._page()
        button = page[page.index('id="btn-untasks"'):]
        assert "hidden" in button[:button.index(">")]

    def test_it_is_offered_only_where_there_is_a_tick_to_take_off(self):
        """Under the same rule as the button beside it, so neither ever
        stands there doing nothing."""
        assert "untasksBtn.hidden = on <= 0" in self._js()

    def test_it_presses_the_clearing_macro(self):
        js = self._js()
        body = js[js.index("const untasksRun"):]
        body = body[:body.index("// The work log")] if "// The work log" in body \
            else body[:2000]
        assert "name: 'clear_tasks'" in body
        assert "check_tasks" not in body

    def test_it_refuses_while_somebody_else_is_driving(self):
        js = self._js()
        body = js[js.index("const untasksRun"):]
        assert "if (!driving()) return;" in body[:body.index("fetch(")]

    def test_the_socket_says_how_many_ticks_are_on(self, client, tmp_path):
        """The count is the server's, read the same way the macro reads it,
        so the button cannot appear over a page the macro would decline."""
        doc = {"at": "2026-08-29T18:00:00", "app": "com.inmyteam.inmyteam",
               "activity": "mainactivity", "blocked": "",
               "size": [1080, 2340],
               "elements": [{"rid": "", "cls": "CheckBox", "txt": "",
                             "b": [26, 369, 58, 401], "checked": True,
                             "enabled": True, "focused": False,
                             "has_text": False}],
               "statics": [{"cls": "TextView", "b": [10, 372, 22, 398],
                            "txt": "*"}]}
        (tmp_path / "screen.json").write_text(json.dumps(doc),
                                              encoding="utf-8")
        with patch.object(state_mod, "STATE_DIR", tmp_path):
            with client.websocket_connect("/ws") as ws:
                msg = ws.receive_json()
        assert (msg.get("screen") or {}).get("ticked") == 1

    def test_both_words_are_written_in_both_languages(self):
        import json as _json
        from pathlib import Path

        for code in ("en", "es"):
            words = _json.loads(Path(
                f"src/apt_log/ui/locales/{code}.json").read_text(
                    encoding="utf-8"))
            for key in ("papp.clear_tasks", "macro.clear_tasks",
                        "macro.step.clearing_tasks",
                        "macro.step.nothing_to_clear",
                        "papp.task_col", "papp.refused_col"):
                assert words[key], key


class TestThePadKnowsWhoseSignatureItIsCollecting:
    """Four things from the room, on the screen where getting them wrong
    puts one person's mark on another person's record.

    Read off the live phone 2026-08-29 with inMyTeam's patient pad open:
    dismiss False, `signer_named` empty, and the app's own Hecho and Borrar
    sitting in an actions row the pad could not reach.
    """

    PAD = {"at": "2026-08-29T18:30:00", "app": "com.inmyteam.inmyteam",
           "activity": "mainactivity", "blocked": "", "size": [1080, 2340],
           "canvas": True,
           "elements": [
               {"rid": "", "cls": "View", "txt": "", "b": [292, 2258, 394, 2295],
                "checked": False, "enabled": True, "focused": False,
                "has_text": False},
               {"rid": "", "cls": "View", "txt": "", "b": [686, 2258, 788, 2295],
                "checked": False, "enabled": True, "focused": False,
                "has_text": False}],
           "statics": [{"cls": "TextView", "b": [13, 1484, 152, 1505],
                        "txt": "Firma del Paciente"},
                       {"cls": "TextView", "b": [324, 2265, 378, 2288],
                        "txt": "Hecho"},
                       {"cls": "TextView", "b": [719, 2265, 770, 2288],
                        "txt": "Borrar"}]}

    def _msg(self, client, tmp_path, doc):
        (tmp_path / "screen.json").write_text(json.dumps(doc),
                                              encoding="utf-8")
        with patch.object(state_mod, "STATE_DIR", tmp_path):
            with client.websocket_connect("/ws") as ws:
                return ws.receive_json()

    def _meta(self, client, tmp_path, doc):
        return self._msg(client, tmp_path, doc).get("screen") or {}

    def _html(self, client, tmp_path, doc):
        # `screen_html` rides BESIDE `screen`, not inside it — reading it off
        # the meta gave "" and made an assertion that nothing was rendered
        # twice pass over a page that was never rendered at all.
        return self._msg(client, tmp_path, doc).get("screen_html") or ""

    def _js(self):
        from pathlib import Path

        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def test_the_role_reaches_the_page(self, client, tmp_path):
        meta = self._meta(client, tmp_path, self.PAD)
        assert meta.get("signer_role") == "patient"

    def test_the_apps_own_buttons_reach_the_pad(self, client, tmp_path):
        """THE SWITCHING THIS DRAWER EXISTS TO REMOVE. The gate asked for a
        dismiss control, and this pad has none — so Hecho and Borrar were
        three lines away on a page she had to switch views to reach."""
        meta = self._meta(client, tmp_path, self.PAD)
        words = [a.get("txt") for a in (meta.get("sheet_actions") or [])]
        assert "Hecho" in words

    def test_a_page_with_no_signature_heading_still_gets_nothing(self):
        """The gate exists to keep Visit Detail's "Check in" and "Note &
        Check out" out of the signature pad, and widening it must not let
        them in. That page carries no signature heading."""
        import importlib

        uiapp = importlib.import_module("apt_log.ui.app")
        doc = {"app": "com.inmyteam.inmyteam", "statics": [
            {"txt": "Detalle de la Visita"}, {"txt": "Entrada"}]}
        model = {"dismiss": None,
                 "rows": [{"actions": True,
                           "items": [{"txt": "Entrada",
                                      "aim": {"rid": "", "cls": "View",
                                              "b": [0, 0, 10, 10]}}]}]}
        assert uiapp._sheet_actions(doc, model) == []

    def test_the_pad_opens_itself_on_arrival(self):
        """"When I click on paciente or persona I want the pencil to trigger
        automatically" — reaching the sheet and then hunting for a pencil is
        a step with no decision in it."""
        js = self._js()
        assert "if (signerRole && !wasRole && padHere && !coaching) openPad();" \
            in js

    def test_it_opens_once_per_arrival_and_not_again(self):
        """`wasRole` is empty on every other screen, so this fires on the
        transition ONTO a pad. If she closes it, it stays closed."""
        js = self._js()
        assert "const wasRole = signerRole;" in js
        assert "function openPad()" in js
        assert "if (body.classList.contains('signing')) return;" in js

    def test_the_wrong_party_is_not_offered_at_all(self):
        """Dimming it was the first answer and it was not enough: "I clicked
        on patient, why is Sadia Amselem auto signature button still
        rendered?" A button on the screen can be pressed, and what it
        presses is one person's signature onto another person's record."""
        js = self._js()
        assert "b.hidden = (signerAdopted ? !mine" in js
        assert "b.dataset.role !== signerRole" in js

    def test_an_unresolved_signer_offers_nobody_rather_than_everybody(self):
        """THE HIDING WAS KEYED ON THE WRONG FACT, and the caregiver's
        signature was offered on the patient's pad because of it.

        The first version hid a pill only when the server had resolved the
        sheet to a person and it was somebody else. On the patient's pad it
        often cannot resolve one: `_role_signer` refuses to guess unless the
        screen itself carries the patient's name, and inMyTeam's check-out
        sheet does not. Read off the live phone at that pad — role 'patient',
        signer '' — so nothing was "wrong" and every pill stood.

        Not-yet-known is not permission. The sheet is asking for the patient
        either way, so the gate is that it is ASKING.
        """
        js = self._js()
        # The old fact must not be what decides it any more.
        assert "const wrong = !!signerAdopted && !mine;" not in js
        # An unresolved signer falls through to the ROLE, which is knowable.
        assert "signerRole && b.dataset.role" in js
        # With nobody left to offer, the caption over the row goes too.
        assert "wrap.hidden = !shown;" in js

    def test_the_patients_own_signature_survives_on_the_patients_pad(self):
        """THE OVERCORRECTION, AND IT COST THE FEATURE. Hiding every party
        the server had not identified took the PATIENT's adopted signature
        off the patient's pad — the one that was wanted — because that pad
        is exactly where the identity cannot be resolved. "The auto sign is
        not even available on the pencil drawer anymore for either."

        The side is knowable where the identity is not, so a patient's pad
        drops only the caregiver.
        """
        js = self._js()
        i = js.index("b.hidden = (signerAdopted ? !mine")
        clause = js[i:i + 220]
        # No role data, or no role on the sheet: nobody is hidden. That is the
        # branch that must not silently empty the row.
        assert "b.dataset.role" in clause and "signerRole" in clause
        assert "!==" in clause

    def test_the_roster_says_which_side_each_party_is_on(self, client):
        """The page cannot work this out: it would need the schedule and the
        same name tolerance the store uses. Both live on the server, so the
        answer does."""
        from unittest.mock import patch as _patch
        from types import SimpleNamespace

        parties = [{"name": "Carmen Villalon"}, {"name": "Sadia Amselem"}]
        with _patch("apt_log.enrolled.roster", return_value=parties), \
             _patch("apt_log.schedule.load",
                    return_value=SimpleNamespace(caregiver="Sadia Amselem")):
            got = client.get("/signature/roster").json()["parties"]
        roles = {p["name"]: p["role"] for p in got}
        assert roles == {"Carmen Villalon": "patient",
                         "Sadia Amselem": "staff"}

    def test_a_scoped_adoption_is_still_labelled(self, client):
        """THE FIRST VERSION OF THIS SHIPPED INERT, and the live roster is
        what said so: every party came back with an empty role.

        It asked `who_signs`, which also decides whether an adoption COVERS
        the app in front — and with no package to hand it refuses every
        adoption that names apps. The caregiver's is scoped to inMyTeam, so
        she went unmatched and nobody could be labelled. Whether her
        signature may be used on an app is a different question from whether
        she is the caregiver.
        """
        from unittest.mock import patch as _patch
        from types import SimpleNamespace

        parties = [{"name": "Carmen Villalon", "apps": []},
                   {"name": "Sadia Amselem",
                    "apps": ["com.inmyteam.inmyteam"]}]
        with _patch("apt_log.enrolled.roster", return_value=parties), \
             _patch("apt_log.schedule.load",
                    return_value=SimpleNamespace(caregiver="Sadia Amselem")):
            got = client.get("/signature/roster").json()["parties"]
        assert {p["name"]: p["role"] for p in got} == {
            "Carmen Villalon": "patient", "Sadia Amselem": "staff"}

    def test_the_resolved_signer_is_looked_up_for_the_app_in_front(self):
        """A SECOND PLACE THE PACKAGE WAS BEING DROPPED, and this one was
        never mine — it predates the role work and disabled the sharpest
        behaviour the pad has.

        `signer_adopted` is what lets the pad mark the one party this sheet
        wants. It asked `who_signs` with no package, so a scoped adoption
        could never resolve: on the caregiver's own pad, inside the very app
        her adoption is scoped to, it answered "".
        """
        src = (Path(__file__).resolve().parents[1]
               / "src/apt_log/ui/app.py").read_text(encoding="utf-8")
        i = src.index('"signer_adopted"')
        clause = src[i:i + 300]
        assert "package=" in clause, "the app in front must be passed"

    def test_an_unnamed_caregiver_labels_nobody_rather_than_everybody(
            self, client):
        """Falling back to "everyone is a patient" would take the caregiver's
        own signature off her own pad the moment the schedule failed to load.
        No answer is the safe answer: the page then offers everyone."""
        from unittest.mock import patch as _patch

        with _patch("apt_log.enrolled.roster",
                    return_value=[{"name": "Carmen Villalon"}]), \
             _patch("apt_log.schedule.load", side_effect=OSError("no file")):
            got = client.get("/signature/roster").json()["parties"]
        assert [p["role"] for p in got] == [""]

    def test_the_roster_still_never_carries_a_signature(self, client):
        """The role is one more safe field on a view that is defined by what
        it leaves out. Adding to it must not become a way in."""
        body = client.get("/signature/roster").text
        for leak in ("strokes", "points", "path", "aspect"):
            assert leak not in body

    def test_the_apps_buttons_leave_the_page_when_the_pad_takes_them(
            self, client, tmp_path):
        """"I'm still getting the actions for hecho and borrar on the front
        end." Widening the gate put them in the drawer without taking them
        off the page, so one press away from a signature there were two live
        copies of the control that wipes it."""
        msg = self._msg(client, tmp_path, self.PAD)
        meta = msg.get("screen") or {}
        assert "Hecho" in [a.get("txt") for a in meta.get("sheet_actions") or []]
        html = msg.get("screen_html") or ""
        assert html, "the page must have rendered for this to mean anything"
        assert "a-actbtn" not in html
        assert ">Hecho<" not in html and ">Borrar<" not in html

    def test_the_page_behind_the_pad_says_what_it_is_waiting_for(
            self, client, tmp_path):
        """"On the front end we can display something like esperando firma
        personal or esperando firma del paciente sort of like a loading
        screen because the pencil drawer is the main front end component
        driving in this case." The role is what names it: patient's pad and
        caregiver's pad are otherwise the same page."""
        html = self._html(client, tmp_path, self.PAD)
        assert "a-waiting" in html
        assert "Esperando la firma del paciente" in html

    def test_a_page_the_pad_did_not_take_keeps_its_own_actions(
            self, client, tmp_path):
        """The waiting card stands in for controls that MOVED. A page whose
        actions stayed put still needs them — Visit Detail's "Check in" is
        the only way to start a visit and no drawer is holding it."""
        doc = dict(self.PAD)
        doc["statics"] = [{"cls": "TextView", "b": [13, 1484, 152, 1505],
                           "txt": "Detalle de la Visita"},
                          {"cls": "TextView", "b": [324, 2265, 378, 2288],
                           "txt": "Entrada"},
                          {"cls": "TextView", "b": [719, 2265, 770, 2288],
                           "txt": "Salida"}]
        msg = self._msg(client, tmp_path, doc)
        assert not ((msg.get("screen") or {}).get("sheet_actions") or [])
        html = msg.get("screen_html") or ""
        assert "a-waiting" not in html
        assert "Entrada" in html

    def test_a_hidden_party_cannot_be_applied_by_a_stray_press(self):
        """The row is delegated, so the guard belongs at the handler too and
        not only in the drawing."""
        js = self._js()
        assert "if (!b || !b.dataset.name || b.hidden) return;" in js

    def test_the_patient_comes_from_the_screen_and_not_from_the_clock(self):
        """THE BUG THAT REACHED THE ROOM. The first version asked the
        schedule for the visit in hand — running now, else next up. At 18:37,
        with a patient's check-out sheet open, her visit had ended, so "next"
        was somebody else's and the pad announced it was asking for the wrong
        person's signature over the right person's record. "Wrong patient.
        This is not Marina."

        A signature belongs to the visit whose sheet is open, and the clock
        does not know which that is. The screen does.
        """
        import importlib
        from types import SimpleNamespace

        uiapp = importlib.import_module("apt_log.ui.app")
        plan = SimpleNamespace(
            caregiver="A Caregiver",
            blocks=[SimpleNamespace(patient="ROSA EJEMPLO"),
                    SimpleNamespace(patient="OTRA PERSONA")])
        onscreen = {"statics": [{"txt": "ROSA EJEMPLO"},
                                {"txt": "Firma del Paciente"}]}
        with patch("apt_log.schedule.load", return_value=plan):
            assert uiapp._role_signer("patient", onscreen) == "ROSA EJEMPLO"

    def test_a_patient_the_screen_does_not_name_is_not_guessed(self):
        """The whole failure in one line: no corroboration, no name. The pad
        then says which ROLE it waits for and names nobody, which is what it
        did before any of this existed."""
        import importlib
        from types import SimpleNamespace

        uiapp = importlib.import_module("apt_log.ui.app")
        plan = SimpleNamespace(
            caregiver="A Caregiver",
            blocks=[SimpleNamespace(patient="ROSA EJEMPLO"),
                    SimpleNamespace(patient="OTRA PERSONA")])
        with patch("apt_log.schedule.load", return_value=plan):
            assert uiapp._role_signer(
                "patient", {"statics": [{"txt": "Firma del Paciente"}]}) == ""

    def test_two_patients_on_one_screen_name_nobody(self):
        import importlib
        from types import SimpleNamespace

        uiapp = importlib.import_module("apt_log.ui.app")
        plan = SimpleNamespace(
            caregiver="A Caregiver",
            blocks=[SimpleNamespace(patient="ROSA EJEMPLO"),
                    SimpleNamespace(patient="OTRA PERSONA")])
        both = {"statics": [{"txt": "ROSA EJEMPLO"}, {"txt": "OTRA PERSONA"}]}
        with patch("apt_log.schedule.load", return_value=plan):
            assert uiapp._role_signer("patient", both) == ""

    def test_the_caregiver_needs_no_corroboration(self):
        """One caregiver on the round, named in the schedule itself, and
        nobody to confuse her with."""
        import importlib
        from types import SimpleNamespace

        uiapp = importlib.import_module("apt_log.ui.app")
        plan = SimpleNamespace(caregiver="A Caregiver", blocks=[])
        with patch("apt_log.schedule.load", return_value=plan):
            assert uiapp._role_signer("staff", {"statics": []}) == "A Caregiver"

    def test_the_pad_no_longer_offers_to_register_a_signature(self):
        """"That's why we have our signature mapping page, we don't need
        this feature in this context of signing." Registering and collecting
        are different jobs, and having both on this sheet put "whose
        signature am I saving" three inches from "whose am I collecting"."""
        from pathlib import Path

        page = Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")
        pad = page[page.index('id="signpad"'):page.index('id="sign-approw"')]
        assert "sign-adopt-open" not in pad
        assert "adopt-name" not in pad

    def test_registering_one_is_still_possible_on_the_mapping_page(self):
        """Removed from the pad, not from the portal — the mapping page has
        a registration sheet of its own, and it is the page that can ask
        BEFORE a visit whether somebody is set up."""
        from pathlib import Path

        page = Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")
        assert 'id="enrolsheet"' in page
        assert 'id="enrol-name"' in page and 'id="enrol-save"' in page

    def test_the_sheet_says_what_it_is_waiting_for(self):
        """With no name, the role still says what the screen is for — and
        the drawer is what she is working in, so the words belong there."""
        js = self._js()
        assert "(i18n.signWaiting || {})[signerRole]" in js

    def test_both_waiting_sentences_are_written_in_both_languages(self):
        import json as _json
        from pathlib import Path

        for code in ("en", "es"):
            words = _json.loads(Path(
                f"src/apt_log/ui/locales/{code}.json").read_text(
                    encoding="utf-8"))
            assert words["sign.waiting.patient"]
            assert words["sign.waiting.staff"]


class TestEveryHourSaysWhichZoneItIsIn:
    """Reported from Central: "I'm getting confused with my CDT local time,
    would like to avoid any confusion on my end."

    Every hour this project reasons about is the building's, and the building
    is in Eastern. The people reading the page are not all in it. An hour
    with no zone on it is one the reader has to guess at, and outside Florida
    the guess is wrong — a 5am arm read as 4am local is the same page saying
    two different things to two people.

    The zone is taken from the value rather than written down, so it says EDT
    in August and EST in January with nobody remembering to change it.
    """

    def _plan(self, **over):
        from apt_log import schedule as sched

        visit = {"patient": "Ada", "app": "com.inmyteam.inmyteam",
                 "start": "05:00", "end": "06:00",
                 "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
        visit.update(over)
        return sched.parse({"zone": "America/New_York", "visits": [visit]})

    def test_a_visit_time_carries_its_zone(self):
        from apt_log.ui.app import _clock

        plan = self._plan()
        visit = plan.upcoming(datetime.now(plan.zone), limit=1)[0]
        said = _clock(visit.starts)
        assert said.startswith("5:00 am")
        assert said.split()[-1] in ("EDT", "EST")

    def test_the_zone_comes_off_the_value_not_off_a_constant(self):
        """August and January are different words for the same schedule."""
        from datetime import datetime as dt
        from zoneinfo import ZoneInfo
        from apt_log.ui.app import _clock

        east = ZoneInfo("America/New_York")
        assert _clock(dt(2026, 8, 21, 5, 0, tzinfo=east)) == "5:00 am EDT"
        assert _clock(dt(2026, 1, 21, 5, 0, tzinfo=east)) == "5:00 am EST"

    def test_a_bare_clock_reading_is_told_its_zone(self):
        """The arming rows keep hours with no date on them, so they cannot
        answer this for themselves and must be handed the answer."""
        from datetime import time as _time
        from apt_log.ui.app import _clock

        assert _clock(_time(5, 0), "EDT") == "5:00 am EDT"

    def test_and_is_never_labelled_with_a_guess(self):
        """A value that does not know its zone and was not told one is left
        alone. A wrong zone is worse than none: it is believed."""
        from datetime import time as _time
        from apt_log.ui.app import _clock

        assert _clock(_time(5, 0)) == "5:00 am"

    def test_the_translator_labels_what_it_can(self):
        from datetime import datetime as dt
        from zoneinfo import ZoneInfo
        from apt_log.ui.i18n import Translator

        t = Translator("en")
        said = t.time(dt(2026, 8, 21, 17, 30, tzinfo=ZoneInfo("America/New_York")))
        assert said.endswith("EDT")
        # Naive, so unknowable, so unlabelled rather than mislabelled.
        assert t.time(dt(2026, 8, 21, 17, 30)) == "05:30 PM"

    def test_the_page_hands_the_launcher_clock_the_buildings_zone(self,
                                                                  client):
        """THE CLOCK WAS THE READER'S. It mirrors a phone standing in
        Florida, and rendered in a browser it showed whatever zone the person
        looking happened to be in — which is how a 5am Eastern arm came to be
        compared against a 4am local one."""
        with patch("apt_log.schedule.load", return_value=self._plan()):
            page = client.get("/app", headers={"Accept-Language": "en"}).text
        assert '"America/New_York"' in page

    def test_every_visit_time_the_api_publishes_says_its_zone(self, client):
        with patch("apt_log.schedule.load", return_value=self._plan()):
            doc = client.get("/api/schedule").json()
        rows = [r for r in doc["week"] if r]
        assert rows
        for row in rows:
            for field in ("starts", "ends", "fires"):
                assert row[field].split()[-1] in ("EDT", "EST"), (field, row)


class TestASecondHalfDoesNotLookLikeACheckIn:
    """Reported while watching the evening: "why is caridad at 9pm showing up
    as next, we're only supposed to check in for caridad on her first block
    not the second one."

    Both halves are real visits she works, so both belong on the page. What
    was missing was any way to tell them apart: the 9:05 card was identical
    to the 8:05 one, so the half that gets no check-in read as the next thing
    to be checked in.
    """

    def _split(self):
        from apt_log import schedule as sched

        common = {"patient": "Ada", "app": "com.inmyteam.inmyteam",
                  "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
        return sched.parse({"zone": "America/New_York", "visits": [
            dict(common, start="20:05", end="21:05", part=1, of=2),
            dict(common, start="21:05", end="22:05", part=2, of=2)]})

    def test_the_api_says_which_half_gets_the_entry(self, client):
        with patch("apt_log.schedule.load", return_value=self._split()):
            doc = client.get("/api/schedule").json()
        halves = {r["part"]: r for r in doc["week"] if r["patient"] == "Ada"}
        assert halves[1]["does_entry"] is True
        assert halves[2]["does_entry"] is False

    def test_the_arming_row_refuses_the_second_half_by_name(self, client):
        from apt_log.ui.app import _arming_model
        from apt_log.ui.i18n import Translator

        with patch("apt_log.schedule.load", return_value=self._split()):
            model = _arming_model(Translator("en"))
        rows = {r["part"]: r for r in model["blocks"]}
        assert rows[1]["why"] == ""
        assert rows[1]["fires"] is True
        assert rows[2]["why"] == "entry_is_the_first_half"
        assert rows[2]["fires"] is False

    def test_and_says_where_the_check_in_actually_is(self, client):
        """"Not here" without "there" leaves somebody hunting for a switch
        that does exist, one row up."""
        from apt_log.ui.app import _arming_model
        from apt_log.ui.i18n import Translator

        with patch("apt_log.schedule.load", return_value=self._split()):
            model = _arming_model(Translator("en"))
        second = next(r for r in model["blocks"] if r["part"] == 2)
        assert "8:05 pm" in second["why_says"]

    def test_the_card_marks_a_visit_that_gets_no_check_in(self, client):
        with patch("apt_log.schedule.load", return_value=self._split()):
            page = client.get("/app", headers={"Accept-Language": "en"}).text
        assert "noentry" in page


class TestCopyingTheSignInCode:
    """Tapping the digits copies them.

    The alternative is reading six digits off one phone while typing them
    into another, which is where a 6 becomes an 8 — and on inMyTeam a wrong
    code clears the field, raises a dialog the tree cannot see, and spends
    one of a limited number of attempts.
    """

    def _js(self) -> str:
        return Path("src/apt_log/ui/static/phone.js").read_text(
            encoding="utf-8")

    def _html(self) -> str:
        return Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")

    def test_the_digits_are_a_button(self):
        """Not a span with a click handler. A button is reachable from a
        keyboard, announces itself as pressable, and this page has a rule
        about not drawing controls that are not controls."""
        html = self._html()
        assert 'class="codedigits" id="code-digits"' in html
        assert "<button type=\"button\" class=\"codedigits\"" in html

    def test_it_says_the_digits_can_be_tapped(self):
        """A number on a card reads as a label; nobody touches it unless the
        page says to."""
        assert "code.copy" in self._html()

    def test_there_is_a_fallback_for_an_insecure_context(self):
        """`navigator.clipboard` needs a secure context. The portal has one
        through `tailscale serve`, but a plain-http preview and some in-app
        browsers do not — and a copy button that silently does nothing is
        worse than no copy button."""
        js = self._js()
        assert "navigator.clipboard" in js
        assert "isSecureContext" in js
        assert "execCommand" in js

    def test_the_fallback_pad_is_offscreen_not_hidden(self):
        """A hidden element cannot be selected, and the copy then fails
        silently — the exact failure the fallback exists to avoid. So the pad
        is moved off-screen instead.

        Read with the comments stripped: the first version of this test
        searched the raw source for "display:none" and tripped over the
        comment explaining why it is not used.
        """
        js = strip_js_comments(self._js())
        body = js.split("function copyText")[1][:700]
        assert "-1000px" in body
        assert "position = 'fixed'" in body
        assert "style.display" not in body

    def test_a_refused_copy_says_so(self):
        js = self._js()
        assert "codeCopyFailed" in js

    def test_both_languages_carry_every_copy_string(self):
        for lang in ("en", "es"):
            cat = json.loads(Path(
                f"src/apt_log/ui/locales/{lang}.json").read_text(
                    encoding="utf-8"))
            for key in ("code.copy", "code.copied", "code.copy_failed"):
                assert key in cat, f"{lang} is missing {key}"


class TestTheCodeAndItsButtonAreOneSlot:
    """The home screen was crowded, and this was most of it.

    A full-width "Text me the sign-in code" button AND a four-line card
    stacked under it came to roughly 190px on the one screen whose job is to
    show the next visit — reported as content being pushed out of reach.

    They are the same feature in two states, and there is never a live code
    and no live code at the same moment, so exactly one of them is on the page
    at a time.
    """

    def _js(self) -> str:
        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def _html(self) -> str:
        return Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")

    def test_showing_the_card_hides_the_button(self):
        js = self._js()
        assert "function showCodeAs(" in js
        body = js.split("function showCodeAs(", 1)[1].split("\n  }", 1)[0]
        assert "card.hidden = !hasCode" in body
        assert "btn.hidden = !!hasCode" in body

    def test_both_states_go_through_that_one_function(self):
        """A second place hiding the code card is how the button and the card
        end up on screen together again — `refreshCode` must ask, not set.

        Scoped to `refreshCode` rather than the whole file: `paintAfter` hides
        a different card entirely, and a guard that cannot tell two cards
        apart forbids the page having two.
        """
        body = self._js().split("function refreshCode(", 1)[1].split(
            "\n  }", 1)[0]
        assert "card.hidden" not in body
        assert body.count("showCodeAs(") == 2

    def test_the_button_is_what_shows_with_no_code(self):
        """With nothing to display, "send the latest one" is the only thing
        left that can help — and it is what the page looks like before the
        first fetch answers, so it is not hidden in the markup."""
        html = self._html()
        assert '<button class="fullbtn" id="btn-code" type="button">' in html

    def test_a_failed_poll_leaves_the_card_alone(self):
        """Hiding on a dropped request takes a live code off the page from
        somebody halfway through reading it."""
        js = self._js()
        after = js.split("function refreshCode(", 1)[1]
        assert ".catch(() => {})" in after.split("\n  }", 1)[0]

    def test_the_card_still_carries_a_way_to_send(self):
        """The full-width button is gone while the card is up, so the card
        has to keep the broadcast reachable."""
        html = self._html()
        assert 'id="btn-code-again"' in html
        assert "code.send_short" in html

    def test_both_send_controls_share_one_handler(self):
        """Two copies of a fetch that texts three real people is two places
        for the disable-while-running guard to be forgotten in."""
        js = self._js()
        assert "function broadcastCode(" in js
        assert js.count("'/code/broadcast'") == 1
        assert "['btn-code', 'btn-code-again']" in js

    def test_the_press_that_ran_is_the_press_re_enabled(self):
        """Re-enabling a fixed id would leave whichever control was actually
        pressed disabled for good."""
        js = self._js()
        body = js.split("function broadcastCode(", 1)[1].split("\n    }", 1)[0]
        assert "pressed.disabled = true" in body
        assert "pressed.disabled = false" in body

    def test_the_digits_stay_a_full_size_target(self):
        """Smaller type must not mean a smaller thing to hit: 44px is the
        tap size the rest of this page holds to."""
        html = self._html()
        block = html.split(".codedigits {", 1)[1].split("}", 1)[0]
        assert "min-height:44px" in block

    def test_the_short_caption_exists_in_both_languages(self):
        import json

        for code in ("en", "es"):
            catalogue = json.loads(
                Path(f"src/apt_log/ui/locales/{code}.json").read_text(
                    encoding="utf-8"))
            assert catalogue["code.send_short"].strip()


class TestThePencilOnlyWhereThereIsSomethingToSign:
    """It stood in the toolbar on every screen in both apps.

    Most of the time it opened a pad onto a page with nothing to draw on: the
    strokes went in, "Draw it on the phone" replayed them, and the answer came
    back "this screen has no signature box". A control that is present
    everywhere and works in one place teaches that it does not work.
    """

    def _js(self) -> str:
        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def test_it_is_gated_on_a_drawing_surface(self):
        assert "signBtn.hidden = !padHere || onLauncher" in self._js()

    def test_the_gate_survives_a_portrait_pad(self):
        """`meta.canvas` means canvas AND SIDEWAYS. inMyTeam's "Firma del
        Paciente" sheet is PORTRAIT, so gating on it hid the pencil on the one
        screen the pad exists for — reported from the room, with the sheet
        open and an adopted signature unreachable."""
        js = self._js()
        assert "meta.pad" in js
        app = Path("src/apt_log/ui/app.py").read_text(encoding="utf-8")
        assert '"pad": bool(screen_doc.get("canvas")),' in app

    def test_no_single_signal_can_hide_it(self):
        """The canvas flag FLICKERS — `_sheet_actions` refuses to gate on it
        and says so. A union means a flicker cannot take the pencil away while
        the app's own buttons are plainly there."""
        js = self._js()
        assert ("const padHere = !!sheetActions.length || legacyUsable"
                " || !!meta.pad;") in js

    def test_the_gate_can_see_what_it_reads(self):
        """`legacyUsable` was scoped to the block that draws step two. Read
        from the pencil's gate it would throw — at exactly the moment a
        signature screen appears and nowhere else."""
        js = self._js()
        assert js.count("const legacyUsable") == 1
        assert js.index("const legacyUsable") < js.index("const padHere")

    def test_it_starts_hidden_in_the_markup(self):
        """Shown-then-hidden flashes it into the toolbar on first paint."""
        html = Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")
        assert '<button id="btn-sign" hidden' in html

    def test_the_drawn_canvas_is_still_a_way_in(self):
        """Her eye is already on the box; tapping it must keep opening the
        pad, or gating the toolbar button would strand her."""
        js = self._js()
        assert "closest('[data-sign]')" in js


class TestThePadNamesWhoIsSigning:
    """Two identical pads, back to back, and the sheet looked the same on both.

    inMyTeam's exit asks the patient first and the caregiver second. Same
    canvas, same buttons; the app names the signer in its title bar and
    nothing was reading it. A row of adopted names with nothing marking which
    one this screen wants is how somebody's signature goes onto another
    person's record.
    """

    def _js(self) -> str:
        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def _html(self) -> str:
        return Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")

    def test_the_socket_carries_both_answers(self):
        """What the APP said, and which roster entry that resolves to. They
        answer different questions: one is a heading, one marks a button."""
        app = Path("src/apt_log/ui/app.py").read_text(encoding="utf-8")
        assert '"signer": _signer' in app
        # Asked FOR THE APP IN FRONT. Pinned without the package originally,
        # which is how a scoped adoption came to be unresolvable in the very
        # app it was scoped to — see
        # `test_the_resolved_signer_is_looked_up_for_the_app_in_front`.
        assert 'enrolled.who_signs(' in app
        i = app.index('"signer_adopted"')
        assert "package=" in app[i:i + 300]

    def test_the_matching_is_not_done_in_the_browser(self):
        """Tolerance for a middle initial belongs in one tested place, and
        that place refuses to answer when two parties could be meant."""
        js = self._js()
        assert "normalize(" not in js.split("function markSigner(", 1)[1][:800]
        assert "b.dataset.name === signerAdopted" in js

    def test_it_marks_and_does_not_press(self):
        """REQ-10.6a rests on the press belonging to the party it belongs to.
        Nothing here may apply a signature."""
        body = self._js().split("function markSigner(", 1)[1].split(
            "\n  }", 1)[0]
        assert "applyAdopted" not in body
        assert "click()" not in body

    def test_every_party_stays_pressable(self):
        """A match this got wrong has to be correctable by the person looking
        at it, so nothing is disabled and nothing is removed."""
        body = self._js().split("function markSigner(", 1)[1].split(
            "\n  }", 1)[0]
        assert "disabled" not in body
        assert "remove()" not in body

    def test_the_row_falls_back_to_even_handed(self):
        """With no resolved party every pill stays filled — an honest picture
        of two people who could sign. `aimed` is what changes that."""
        assert "row.classList.toggle('aimed', !!signerAdopted)" in self._js()
        html = self._html()
        assert ".adopted-row.aimed button {" in html
        assert ".adopted-row.aimed button.primary {" in html

    def test_the_heading_is_said_in_both_languages(self):
        import json

        for code in ("en", "es"):
            catalogue = json.loads(
                Path(f"src/apt_log/ui/locales/{code}.json").read_text(
                    encoding="utf-8"))
            assert "{who}" in catalogue["sign.whose"]

    def test_the_pad_does_not_stack_two_bands_under_its_canvas(self):
        """"Anything we can do to avoid scrolling on the pencil drawer, keep
        things tidy in this component."

        Measured at 390x844 with a saved-signature pill and the app's own
        buttons showing: 665px of content against 675px of sheet — ten pixels
        from scrolling, which is why it read as crowded. The two bands under
        the canvas were 117px of it: a row of text buttons, then a caption
        and a mostly-empty rectangle. Both belong to the canvas and both are
        small, so they share one line.
        """
        html = (Path(__file__).resolve().parents[1]
                / "src/apt_log/ui/templates/phone.html").read_text(
                    encoding="utf-8")
        pad = html.split('id="signsheet"', 1)[1].split("</div>\n<div", 1)[0]
        # One strip holding both, rather than the two siblings it replaced.
        strip = pad.index('class="padstrip"')
        assert strip < pad.index('class="signpreview-wrap"')
        assert strip < pad.index('class="padtools"')
        # And the label is what gives way when the phone is narrow, never the
        # tap targets.
        rule = html[html.index(".padtools {"):]
        assert "flex:none" in rule[:rule.index("}")]

    def test_the_name_goes_in_as_text(self):
        """It is the app's rendering of somebody's legal name, arriving over a
        socket and going onto a page."""
        body = self._js().split("function markSigner(", 1)[1].split(
            "\n  }", 1)[0]
        # It lands on step one's own heading now — the separate line under it
        # said nearly the same thing and the sheet could not spare it. Still
        # textContent, which is what this test is actually about.
        assert "title.textContent" in body
        assert "innerHTML" not in body
        assert "innerHTML" not in body


class TestTheSignatureMapping:
    """Who will be asked to sign, and who has nothing on file yet.

    Adoption used to happen in one place only — inside the pad, which opens on
    a signature screen — so the question worth asking BEFORE a visit had
    nowhere to be asked: is this patient set up, or do we find out standing in
    her living room?
    """

    def _plan(self, tmp_path, *patients):
        import json as _json

        from apt_log import schedule as sched

        path = tmp_path / "schedule.json"
        path.write_text(_json.dumps({
            "zone": "America/New_York",
            "visits": [{"patient": p, "app": "inmyteam",
                        "days": ["mon"], "start": "09:00", "end": "10:00"}
                       for p in patients]}), encoding="utf-8")
        return path

    def _enrol(self, tmp_path, *names):
        from apt_log import enrolled as enrolled_mod

        store = tmp_path / "sig.json"
        for n in names:
            enrolled_mod.enroll(
                n, [[[0.1, 0.5], [0.3, 0.2], [0.5, 0.6]], [[0.6, 0.3], [0.9, 0.4]]],
                witness="witnessed at the table", path=store)
        return store

    def test_it_lists_the_schedule_not_the_store(self, client, tmp_path,
                                                 monkeypatch):
        """The roster answers the wrong half: a list of the finished ones can
        never tell you what is left."""
        from apt_log import enrolled as enrolled_mod
        from apt_log import schedule as sched

        monkeypatch.setattr(sched, "SCHEDULE_PATH",
                            self._plan(tmp_path, "Maria Garcia", "Beto Sosa"))
        monkeypatch.setattr(enrolled_mod, "STORE_PATH",
                            self._enrol(tmp_path, "Maria Garcia"))
        people = client.get("/signature/map").json()["people"]
        assert [p["name"] for p in people] == ["Maria Garcia", "Beto Sosa"]
        assert [p["adopted"] for p in people] == [True, False]

    def test_a_middle_initial_still_counts_as_adopted(self, client, tmp_path,
                                                      monkeypatch):
        """The schedule and the adoption are typed by different people. The
        row must not read "not adopted" over a signature that is on file."""
        from apt_log import enrolled as enrolled_mod
        from apt_log import schedule as sched

        monkeypatch.setattr(sched, "SCHEDULE_PATH",
                            self._plan(tmp_path, "MARIA X GARCIA"))
        monkeypatch.setattr(enrolled_mod, "STORE_PATH",
                            self._enrol(tmp_path, "Maria Garcia"))
        row = client.get("/signature/map").json()["people"][0]
        assert row["adopted"] is True
        assert row["adopted_as"] == "Maria Garcia"

    def test_an_adoption_off_the_schedule_still_shows(self, client, tmp_path,
                                                      monkeypatch):
        """An adoption is a record. One that stopped matching a schedule entry
        must not quietly vanish from the only screen that shows it."""
        from apt_log import enrolled as enrolled_mod
        from apt_log import schedule as sched

        monkeypatch.setattr(sched, "SCHEDULE_PATH",
                            self._plan(tmp_path, "Beto Sosa"))
        monkeypatch.setattr(enrolled_mod, "STORE_PATH",
                            self._enrol(tmp_path, "Maria Garcia"))
        people = client.get("/signature/map").json()["people"]
        stray = [p for p in people if not p["on_schedule"]]
        assert [p["name"] for p in stray] == ["Maria Garcia"]

    def test_it_never_returns_strokes(self, client, tmp_path, monkeypatch):
        """The same rule the roster follows, and the reason those two
        functions are separate."""
        from apt_log import enrolled as enrolled_mod
        from apt_log import schedule as sched

        monkeypatch.setattr(sched, "SCHEDULE_PATH",
                            self._plan(tmp_path, "Maria Garcia"))
        monkeypatch.setattr(enrolled_mod, "STORE_PATH",
                            self._enrol(tmp_path, "Maria Garcia"))
        body = client.get("/signature/map").text
        assert "strokes" not in body
        assert "points" not in body

    def test_an_unreadable_schedule_still_shows_the_store(self, client,
                                                          tmp_path,
                                                          monkeypatch):
        """Saying nothing at all would read as "nobody has adopted one"."""
        from apt_log import enrolled as enrolled_mod
        from apt_log import schedule as sched

        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(sched, "SCHEDULE_PATH", bad)
        monkeypatch.setattr(enrolled_mod, "STORE_PATH",
                            self._enrol(tmp_path, "Maria Garcia"))
        people = client.get("/signature/map").json()["people"]
        assert [p["name"] for p in people] == ["Maria Garcia"]

    def test_nothing_at_all_is_an_empty_list(self, client, tmp_path,
                                             monkeypatch):
        from apt_log import enrolled as enrolled_mod
        from apt_log import schedule as sched

        monkeypatch.setattr(sched, "SCHEDULE_PATH", tmp_path / "none.json")
        monkeypatch.setattr(enrolled_mod, "STORE_PATH", tmp_path / "no.json")
        assert client.get("/signature/map").json()["people"] == []


class TestTheSignatureMappingView:
    def _js(self) -> str:
        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def _html(self) -> str:
        return Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")

    def test_the_names_are_never_server_rendered(self):
        """This template is cached by a service worker; patients' names are
        not going into it."""
        html = self._html()
        block = html.split('id="signaturesview"', 1)[1].split("</div>", 1)[0]
        assert "{% for" not in block

    def test_the_rows_are_built_as_text(self):
        body = self._js().split("function renderMap(", 1)[1].split(
            "\n  }", 1)[0]
        assert "innerHTML" not in body
        assert "textContent = p.name" in body

    def test_the_button_opens_the_registration_sheet(self):
        """NOT the check-out pad. That pad is built around a patient signing
        at the end of a visit — numbered steps, "Draw it on the phone", the
        app's own confirm — and none of it applies to registering one."""
        body = self._js().split("function adoptFrom(", 1)[1].split(
            "\n  }", 1)[0]
        assert "classList.add('enrolling')" in body
        assert "classList.remove('signing')" in body
        assert "getElementById('enrol-name')" in body

    def test_it_carries_the_name_and_saves_nothing(self):
        """Adoption still means the pad, the person and a witness. This only
        saves somebody typing the name a second time."""
        body = self._js().split("function adoptFrom(", 1)[1].split(
            "\n  }", 1)[0]
        assert "field.value = name" in body
        assert "/signature/enroll" not in body

    def test_it_opens_onto_an_empty_pad(self):
        """The two pads share one stroke list, so whatever was on the other
        one is not this person's signature."""
        body = self._js().split("function adoptFrom(", 1)[1].split(
            "\n  }", 1)[0]
        assert "pad.strokes = []" in body

    def test_the_count_shows_zero_rather_than_hiding(self):
        """Nought of three is the reading worth seeing."""
        body = self._js().split("function renderMap(", 1)[1].split(
            "\n  }", 1)[0]
        assert "have + '/' + people.length" in body

    def test_it_is_re_read_after_an_adoption(self):
        """Otherwise the front page keeps reporting the state from before the
        adoption that just happened in front of two people."""
        js = self._js()
        after = js.split("i18n.adoptSaved", 1)[1][:300]
        assert "loadMap()" in after

    def test_the_front_page_gains_no_height(self):
        """The page was reported as crowded; a second full-width button would
        have spent the room the code card just gave back."""
        html = self._html()
        assert '<div class="fullrow">' in html
        assert '.fullrow { display:flex;' in html

    def test_every_word_of_it_is_translated(self):
        import json

        for code in ("en", "es"):
            catalogue = json.loads(
                Path(f"src/apt_log/ui/locales/{code}.json").read_text(
                    encoding="utf-8"))
            for key in ("sigmap.title", "sigmap.note", "sigmap.nobody",
                        "sigmap.on_file", "sigmap.missing", "sigmap.adopt",
                        "sigmap.replace", "sigmap.not_scheduled",
                        "sigmap.filed_as"):
                assert catalogue[key].strip(), f"{code} {key}"


class TestTheMappingNamesAppsTheWayTheTilesDo:
    """"com.hhaexchange.uma" is not an app name.

    It is a package identifier, and on a caregiver's screen it is noise. The
    schedule names an app however its author found convenient — a package on
    one line, a tile id on another — so the row resolves whichever it got.
    """

    def test_a_package_becomes_its_brand(self):
        from apt_log.ui.app import _app_called

        assert _app_called("com.hhaexchange.uma") == "HHAeXchange+"
        assert _app_called("com.inmyteam.inmyteam") == "inMyTeam"

    def test_a_tile_id_resolves_too(self):
        from apt_log.ui.app import _app_called

        assert _app_called("inmyteam") == "inMyTeam"
        assert _app_called("mobile_caregiver") == "Mobile Caregiver+"

    def test_the_retired_app_is_still_named(self):
        """A schedule entry written before the migration is still a schedule
        entry somebody has to read."""
        from apt_log.ui.app import _app_called

        assert _app_called("com.hhaexchange.caregiver") == "HHAeXchange"

    def test_an_unknown_reference_survives_rather_than_blanking(self):
        """A row for an app this build does not know must stay legible."""
        from apt_log.ui.app import _app_called

        assert _app_called("com.example.new") == "com.example.new"
        assert _app_called("") == ""


class TestStepTwoHasOneButton:
    """Step two carries the app's own buttons, and BOTH of them.

    The clear was filtered out of this row on the grounds that the replay
    wipes the canvas itself, so redrawing is pressing Send again and the
    clear has nothing left to do. True for redrawing, and it left the other
    case with no answer at all: a signature already on the phone that should
    not be there, which the app's Borrar undoes and nothing else does.
    Reported from the room — "what happens to the borrar button for the
    signature? I only see Hecho" — so it comes through, dressed as what it
    is rather than as a second way to finish.
    """

    def _js(self) -> str:
        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def test_the_clear_reaches_the_row(self):
        js = self._js()
        assert "const sheetActions = (meta.sheet_actions || []);" in js
        assert "CLEAR_WORDS.test((a.txt" not in js

    def test_it_is_not_dressed_as_the_finish(self):
        """Wiping the signature on the phone is not a thing to press by
        reaching for the button that finishes, so only the affirmative is
        filled and the clear takes the warning colour."""
        js = self._js()
        assert "b.classList.add('wipe')" in js
        assert "CLEAR_WORDS.test(word)" in js
        css = Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")
        assert ".padrow .wipe" in css and "var(--bad)" in css

    def test_the_payload_is_read_once(self):
        """A second reader downstream is a reader that could see a different
        row from the one drawn."""
        assert self._js().count("meta.sheet_actions") == 1

    def test_the_caption_must_be_the_whole_word(self):
        """Dropping a button on a substring match is how an affirmative with
        an unlucky name disappears from the only row that finishes a
        signature."""
        import re

        pattern = re.compile(r"^(borrar|clear|limpiar)$", re.I)
        assert pattern.match("Borrar")
        assert not pattern.match("Borrar todo")
        assert not pattern.match("No borrar")

    def test_the_two_lists_of_clear_words_agree(self):
        """The controller wipes on `sign._CLEAR_WORDS` and the pad hides on
        this one. They disagreeing means the replay clearing a canvas whose
        button the pad still shows, or the pad hiding a button nothing
        wipes."""
        from apt_log import sign as sign_mod

        js = self._js()
        pattern = js.split("const CLEAR_WORDS = /^(", 1)[1].split(")$/", 1)[0]
        assert sorted(pattern.split("|")) == sorted(sign_mod._CLEAR_WORDS)

    def test_the_legacy_row_keeps_its_clear(self):
        """The replay's wipe presses the app's clear ELEMENT, and that row
        exists for the screen that publishes no elements to press."""
        html = Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")
        legacy = html.split('id="sign-legacyrow"', 1)[1].split("</div>", 1)[0]
        assert 'id="app-clear"' in legacy


class TestRegisteringASignatureIsItsOwnSheet:
    """Registering a signature and giving one are two different moments.

    The check-out pad is built entirely around the second: it draws, replays
    onto the app's canvas, and hands over to the app's own confirm. Opening it
    to REGISTER a signature showed "Draw it on the phone", a numbered step two
    about an app that was not in front, and a preview rehearsing a replay that
    was never going to happen — the wrong component for the job.
    """

    def _js(self) -> str:
        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def _html(self) -> str:
        return Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")

    def _sheet(self) -> str:
        html = self._html()
        return html.split('<div id="enrolsheet"', 1)[1].split(
            "\n</div>", 1)[0]

    def test_the_sheet_exists_and_has_its_own_pad(self):
        html = self._html()
        assert '<div id="enrolsheet" class="sheet">' in html
        assert '<canvas id="enrolpad"' in html

    def test_it_has_no_phone_in_it(self):
        """Every part of the check-out pad that is about an app on a handset
        has to be absent, or this is the same mistake with a new id."""
        sheet = self._sheet()
        for absent in ("sign-send", "sign-approw", "signstep", "signpreview",
                       "sign-appbtns", "sign-legacyrow", "sign-hint"):
            assert absent not in sheet, absent

    def test_it_asks_for_the_one_thing_the_requirement_needs(self):
        """Whose signature it is. That is the whole of what has to be typed.

        It asked for a witness too, until the field was removed: in front of a
        patient it put a question whose answer was always the same two people,
        and a box filled in the same way every time is not a record. The date
        is kept by the machine and never typed."""
        sheet = self._sheet()
        assert 'id="enrol-name"' in sheet
        assert "witness" not in sheet

    def test_the_name_is_editable(self):
        """The schedule's spelling is the right default because it is what
        the matcher looks for. It is still a person's name, not a constant."""
        sheet = self._sheet()
        assert "readonly" not in sheet
        assert "disabled" not in sheet

    def test_there_is_a_way_out_that_is_not_saving(self):
        assert 'id="enrol-cancel"' in self._sheet()

    def test_both_pads_are_wired(self):
        """A handler bound to whichever canvas happened to be in front at boot
        leaves the other one dead."""
        js = self._js()
        assert "padWire(document.getElementById('signpad'))" in js
        assert "padWire(document.getElementById('enrolpad'))" in js

    def test_the_live_pad_is_the_open_one(self):
        body = self._js().split("function padCanvas(", 1)[1].split(
            "\n  }", 1)[0]
        assert "classList.contains('enrolling')" in body
        assert "'enrolpad'" in body and "'signpad'" in body

    def test_the_context_follows_the_open_pad(self):
        """A context cached from the other canvas draws into a surface nobody
        is looking at."""
        body = self._js().split("function padRedraw(", 1)[1].split(
            "\n  }", 1)[0]
        assert "pad.ctx = c.getContext('2d')" in body

    def test_the_replay_preview_is_not_drawn_while_registering(self):
        """"How this will land in the app's box" is a rehearsal for a replay,
        and registration replays nothing."""
        body = self._js().split("function previewRedraw(", 1)[1].split(
            "\n  }", 1)[0]
        assert "classList.contains('enrolling')" in body

    def test_one_post_serves_both_moments(self):
        """Two copies would be two places for the witness check, the empty-pad
        check and the refresh afterwards to drift — on the one feature where
        the refusals ARE the requirement."""
        js = self._js()
        assert "function postEnrolment(" in js
        assert js.count("'/signature/enroll'") == 1

    def test_closing_leaves_no_strokes_behind(self):
        """The next person to register one must not find the last person's
        signature waiting on the canvas."""
        body = self._js().split("function closeEnrol(", 1)[1].split(
            "\n  }", 1)[0]
        assert "pad.strokes = []" in body
        assert "classList.remove('enrolling')" in body

    def test_nothing_of_the_witness_field_is_left_behind(self):
        """Removed, not hidden: a field still in the markup is a field that
        comes back the next time somebody restyles the sheet."""
        html = self._html()
        assert 'id="enrol-witness"' not in html
        assert 'id="adopt-witness"' not in html
        assert "sign.adopt_witness" not in html

    def test_every_word_of_it_is_translated(self):
        import json

        for code in ("en", "es"):
            catalogue = json.loads(
                Path(f"src/apt_log/ui/locales/{code}.json").read_text(
                    encoding="utf-8"))
            for key in ("sigmap.enrol_title", "sigmap.enrol_note",
                        "common.cancel"):
                assert catalogue[key].strip(), f"{code} {key}"


class TestNothingIsSelectableWhileSigning:
    """A signature is a long drag, and a long drag selects text.

    Watched on the caregiver's own phone: drawing highlighted the heading and
    the note above the pad in blue, and on iOS a press that lingers on the
    canvas raises the callout menu. Either one interrupts the stroke being
    drawn — the exact failure this pad already has a bad history with.

    `touch-action:none` stops the browser SCROLLING and says nothing about
    selection, which is why the pad shipped like this and nobody caught it
    until a real signature was drawn on it.
    """

    def _html(self) -> str:
        return Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")

    def test_both_sheets_are_unselectable(self):
        html = self._html()
        assert "#signsheet, #enrolsheet { -webkit-user-select:none;" in html
        assert "user-select:none" in html

    def test_the_callout_menu_is_off_too(self):
        """On iOS the long press raises a menu over the pad, which is its own
        interruption even when nothing gets selected."""
        html = self._html()
        block = html.split("#signsheet, #enrolsheet {", 1)[1].split("}", 1)[0]
        assert "-webkit-touch-callout:none" in block

    def test_both_canvases_say_it_themselves(self):
        """Safari has historically needed the rule on the element the gesture
        starts on, not only on an ancestor."""
        html = self._html()
        assert "#signpad, #enrolpad { -webkit-user-select:none;" in html

    def test_the_name_field_is_still_editable(self):
        """A typed name has to be selectable or it cannot be corrected."""
        html = self._html()
        assert ("#signsheet input, #enrolsheet input { -webkit-user-select:text;"
                in html)

    def test_the_canvas_still_refuses_to_scroll(self):
        """The rule that was already right. Selection and scrolling are two
        different behaviours and this fix must not have replaced one with the
        other."""
        html = self._html()
        for pad in ("#signpad {", "#enrolpad {"):
            block = html.split(pad, 1)[1].split("}", 1)[0]
            assert "touch-action:none" in block, pad


class TestTheSaveSaysWhichMachineFailed:
    def _js(self) -> str:
        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def test_an_unwritable_store_gets_its_own_sentence(self):
        js = self._js()
        assert "out.error === 'store_unwritable'" in js
        assert "i18n.adoptNoStore" in js

    def test_the_sentence_says_it_is_not_the_phone(self):
        """The old one said "that didn't reach the phone" over a fault that
        was entirely the Pi's disk — pointing her at the one thing that was
        working."""
        import json

        for code, word in (("en", "phone"), ("es", "teléfono")):
            catalogue = json.loads(
                Path(f"src/apt_log/ui/locales/{code}.json").read_text(
                    encoding="utf-8"))
            assert word in catalogue["sign.adopt_no_store"]


class TestWithdrawingAnAdoptionFromTheRow:
    """The first signature ever registered here was a test scribble, saved
    under a patient's name by accident.

    There was no way to take it off the machine FROM the machine — the only
    route to `/signature/forget` was a shell over the tailnet. A signature
    under the wrong person's name is the exact failure this whole feature is
    careful about, so undoing it cannot be the one thing that needs a laptop.
    """

    def _js(self) -> str:
        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def _html(self) -> str:
        return Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")

    def test_the_row_offers_it(self):
        body = self._js().split("function renderMap(", 1)[1].split(
            "\n  }", 1)[0]
        assert "rm.dataset.forgetFor" in body
        assert "i18n.sigForget" in body

    def test_only_on_rows_that_have_one(self):
        """A Remove button over "not registered" is a control with nothing to
        act on."""
        body = self._js().split("function renderMap(", 1)[1].split(
            "\n  }", 1)[0]
        assert "if (p.adopted) {" in body
        assert ".sigrow.none .sigforget { display:none; }" in self._html()

    def test_it_takes_two_presses(self):
        """It deletes the only copy: the strokes are not archived anywhere, so
        there is no undo and the person has to be in the room again to make
        another."""
        body = self._js().split("function forgetPressed(", 1)[1].split(
            "\n  }", 1)[0]
        assert "classList.contains('armed')" in body
        assert "i18n.sigForgetSure" in body
        # The first press must return without deleting anything.
        first = body.split("classList.contains('armed')", 1)[1].split(
            "return;", 1)[0]
        assert "fetch(" not in first

    def test_it_disarms_itself(self):
        """A press somebody thought better of must not sit there armed."""
        js = self._js()
        assert "const FORGET_ARMED = 4000;" in js
        assert "setTimeout(disarmForget, FORGET_ARMED)" in js

    def test_pressing_anything_else_disarms_it(self):
        """Safer than leaving it primed behind whatever she does next."""
        js = self._js()
        handler = js.split("sigList.addEventListener", 1)[1].split(
            "\n    });", 1)[0]
        assert "disarmForget();" in handler

    def test_it_forgets_by_the_name_the_store_holds(self):
        """The row shows the schedule's spelling and the store may hold
        another — forgetting the wrong string would silently do nothing."""
        body = self._js().split("function renderMap(", 1)[1].split(
            "\n  }", 1)[0]
        assert "rm.dataset.forgetFor = p.adopted_as || p.name" in body

    def test_both_lists_are_re_read_afterwards(self):
        """The pad's own adopted row must stop offering a signature that no
        longer exists."""
        body = self._js().split("function forgetPressed(", 1)[1].split(
            "\n  }", 1)[0]
        assert "loadMap();" in body
        assert "loadAdopted();" in body

    def test_the_armed_state_is_visible(self):
        """The state change is what makes the second press deliberate rather
        than a double tap."""
        assert ".sigrow .sigforget.armed {" in self._html()

    def test_it_is_said_in_both_languages(self):
        import json

        for code in ("en", "es"):
            catalogue = json.loads(
                Path(f"src/apt_log/ui/locales/{code}.json").read_text(
                    encoding="utf-8"))
            for key in ("sigmap.forget", "sigmap.forget_sure",
                        "sigmap.forgotten"):
                assert catalogue[key].strip(), f"{code} {key}"


class TestSeeingWhatWasSaved:
    """The sheet does not close on a save. It shows what went in.

    The first registration on this machine was a test scribble stored under a
    patient's name, and it was found out afterwards. The moment to catch that
    is while both people are still sitting there.
    """

    def _js(self) -> str:
        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def _html(self) -> str:
        return Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")

    def test_saving_shows_rather_than_closes(self):
        js = self._js()
        assert "postEnrolment(" in js
        body = js.split("function enrolSave(", 1)[1].split("\n  }", 1)[0]
        assert "enrolDone" in body
        assert "closeEnrol" not in body

    def test_the_preview_is_the_strokes_already_in_hand(self):
        """NOTHING is fetched back. REQ-10.6a condition 2 — no route returns a
        signature — does not bend for a picture, so the preview can only ever
        be of what this browser just sent."""
        body = self._js().split("function enrolDone(", 1)[1].split(
            "\n  }", 1)[0]
        assert "fetch(" not in body
        assert "strokes" not in body

    def test_no_route_hands_back_a_signature(self):
        """The guard that matters. If a preview route ever appears, this is
        what should stop it."""
        app = Path("src/apt_log/ui/app.py").read_text(encoding="utf-8")
        for route in ('"/signature/preview"', '"/signature/image"',
                      '"/signature/strokes"'):
            assert route not in app, route

    def test_the_pad_goes_inert(self):
        """The strokes on it are now a record of something stored, and a
        stroke added here would be added to nothing."""
        html = self._html()
        assert "body.enrolled #enrolpad { pointer-events:none;" in html

    def test_everything_that_could_change_it_stands_down(self):
        html = self._html()
        block = html.split("body.enrolled #enrol-save,", 1)[1].split(
            "}", 1)[0]
        for gone in ("#enrol-cancel", ".padtools", "input"):
            assert gone in block, gone

    def test_it_offers_a_redraw(self):
        """"That is not her signature" has to be answerable in the room, not
        afterwards from a shell."""
        js = self._js()
        assert "function enrolAgain(" in js
        body = js.split("function enrolAgain(", 1)[1].split("\n  }", 1)[0]
        assert "pad.strokes = []" in body
        assert "classList.remove('enrolled')" in body

    def test_opening_never_starts_in_the_saved_state(self):
        """Opening this sheet is always the start of a registration, never the
        end of the last one."""
        body = self._js().split("function adoptFrom(", 1)[1].split(
            "\n  }", 1)[0]
        assert "classList.remove('enrolled')" in body

    def test_closing_clears_it(self):
        body = self._js().split("function closeEnrol(", 1)[1].split(
            "\n  }", 1)[0]
        assert "classList.remove('enrolled')" in body
        assert "pad.strokes = []" in body

    def test_it_names_who_it_was_saved_for(self):
        """A confirmation that does not say whose it is confirms nothing —
        the accident this exists to catch was a signature under the wrong
        name."""
        body = self._js().split("function enrolDone(", 1)[1].split(
            "\n  }", 1)[0]
        assert "i18n.sigSavedFor" in body
        assert "said.textContent" in body

    def test_it_is_said_in_both_languages(self):
        import json

        for code in ("en", "es"):
            catalogue = json.loads(
                Path(f"src/apt_log/ui/locales/{code}.json").read_text(
                    encoding="utf-8"))
            assert "{who}" in catalogue["sigmap.saved_for"]
            for key in ("sigmap.saved_again", "sigmap.saved_ok"):
                assert catalogue[key].strip(), f"{code} {key}"


class TestTheCaregiverIsOnTheList:
    """She signs every check-out and appeared on none of them.

    inMyTeam's exit asks the PATIENT at Paso 2 de 3 and asks HER at Paso 3 de
    3. The mapping was built from `visits`, and every visit names a patient
    while none names her — she is the same person on all of them — so the
    second signature of every exit had nothing behind it.
    """

    def _plan(self, tmp_path, patients, caregiver=None):
        import json as _json

        path = tmp_path / "schedule.json"
        doc = {"zone": "America/New_York",
               "visits": [{"patient": p, "app": "inmyteam", "days": ["mon"],
                           "start": "09:00", "end": "10:00"} for p in patients]}
        if caregiver is not None:
            doc["caregiver"] = caregiver
        path.write_text(_json.dumps(doc), encoding="utf-8")
        return path

    def _enrol(self, tmp_path, *names):
        from apt_log import enrolled as enrolled_mod

        store = tmp_path / "sig.json"
        for n in names:
            enrolled_mod.enroll(
                n, [[[0.1, 0.5], [0.3, 0.2], [0.5, 0.6]], [[0.6, 0.3], [0.9, 0.4]]],
                path=store)
        return store

    def _map(self, client, tmp_path, monkeypatch, patients, caregiver=None,
             enrolled_names=()):
        from apt_log import enrolled as enrolled_mod
        from apt_log import schedule as sched

        monkeypatch.setattr(sched, "SCHEDULE_PATH",
                            self._plan(tmp_path, patients, caregiver))
        monkeypatch.setattr(enrolled_mod, "STORE_PATH",
                            self._enrol(tmp_path, *enrolled_names))
        return client.get("/signature/map").json()["people"]

    def test_she_is_on_it(self, client, tmp_path, monkeypatch):
        people = self._map(client, tmp_path, monkeypatch,
                           ["Maria Garcia"], caregiver="Ana Ruiz")
        assert "Ana Ruiz" in [p["name"] for p in people]

    def test_she_is_first(self, client, tmp_path, monkeypatch):
        """She is on every visit, so hers is the one row whose absence would
        break all of them."""
        people = self._map(client, tmp_path, monkeypatch,
                           ["Maria Garcia", "Beto Sosa"], caregiver="Ana Ruiz")
        assert people[0]["name"] == "Ana Ruiz"

    def test_the_rows_say_which_is_which(self, client, tmp_path, monkeypatch):
        """A list of bare names cannot say which signature a row is for, on
        the one screen where confusing them registers a signature against the
        wrong party."""
        people = self._map(client, tmp_path, monkeypatch,
                           ["Maria Garcia"], caregiver="Ana Ruiz")
        roles = {p["name"]: p["role"] for p in people}
        assert roles["Ana Ruiz"] == "staff"
        assert roles["Maria Garcia"] == "patient"

    def test_she_gets_a_row_for_every_app_she_signs_in(self, client, tmp_path,
                                                        monkeypatch):
        """One row per app, not one row listing them. She signs two of them
        differently, so a single row could only tell the truth about one:
        "registered" would mean "somewhere", and the app she has not done
        would read as done."""
        import json as _json

        from apt_log import enrolled as enrolled_mod
        from apt_log import schedule as sched

        path = tmp_path / "schedule.json"
        path.write_text(_json.dumps({
            "zone": "America/New_York",
            "caregiver": "Ana Ruiz",
            "visits": [
                {"patient": "Maria Garcia", "app": "inmyteam",
                 "days": ["mon"], "start": "09:00", "end": "10:00"},
                {"patient": "Beto Sosa", "app": "com.hhaexchange.uma",
                 "days": ["tue"], "start": "09:00", "end": "10:00"}]}),
            encoding="utf-8")
        monkeypatch.setattr(sched, "SCHEDULE_PATH", path)
        monkeypatch.setattr(enrolled_mod, "STORE_PATH", tmp_path / "none.json")
        people = client.get("/signature/map").json()["people"]
        staff = [p for p in people if p["role"] == "staff"]
        assert [p["app"] for p in staff] == ["HHAeXchange+", "inMyTeam"]

    def test_her_adoption_is_matched_like_anybody_else(self, client, tmp_path,
                                                        monkeypatch):
        people = self._map(client, tmp_path, monkeypatch, ["Maria Garcia"],
                           caregiver="ANA X RUIZ",
                           enrolled_names=("Ana Ruiz",))
        assert people[0]["adopted"] is True
        assert people[0]["adopted_as"] == "Ana Ruiz"

    def test_a_schedule_without_one_has_no_staff_row(self, client, tmp_path,
                                                     monkeypatch):
        """Every schedule written before today looks like this, and none of
        them should grow an empty row."""
        people = self._map(client, tmp_path, monkeypatch, ["Maria Garcia"])
        assert [p["role"] for p in people] == ["patient"]

    def test_a_blank_caregiver_is_the_same_as_none(self, client, tmp_path,
                                                   monkeypatch):
        people = self._map(client, tmp_path, monkeypatch, ["Maria Garcia"],
                           caregiver="   ")
        assert [p["name"] for p in people] == ["Maria Garcia"]

    def test_she_is_not_repeated_as_a_stray(self, client, tmp_path,
                                            monkeypatch):
        """Her adoption matches her schedule row, so it must not also appear
        in the "not on the schedule" tail."""
        people = self._map(client, tmp_path, monkeypatch, ["Maria Garcia"],
                           caregiver="Ana Ruiz", enrolled_names=("Ana Ruiz",))
        assert [p["name"] for p in people].count("Ana Ruiz") == 1

    def test_the_chip_is_said_in_both_languages(self):
        import json

        for code in ("en", "es"):
            catalogue = json.loads(
                Path(f"src/apt_log/ui/locales/{code}.json").read_text(
                    encoding="utf-8"))
            assert catalogue["sigmap.role_patient"].strip()
            assert catalogue["sigmap.role_staff"].strip()


class TestARowPerAppBecauseAMarkIsPerApp:
    """The caregiver signs inMyTeam one way and the other two another.

    A single row per person could only ever tell the truth about one of them:
    "registered" would mean "somewhere", and the row for the app she has not
    done yet would read as done — which is how somebody walks into a check-out
    believing a signature is on file that is not.
    """

    def _js(self) -> str:
        return strip_js_comments(
            Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8"))

    def test_the_row_carries_its_app_into_the_registration(self):
        js = self._js()
        assert "b.dataset.adoptApp = p.package" in js
        assert "adoptFrom(hit.dataset.adoptFor, hit.dataset.adoptApp)" in js

    def test_the_registration_is_scoped_to_that_app(self):
        js = self._js()
        assert "apps: adoptForApp ? [adoptForApp] : []" in js

    def test_the_sheet_says_which_app_it_is_for(self):
        """Somebody registering the second of two marks has to be able to see
        which one they are drawing."""
        js = self._js()
        assert "i18n.sigForApp" in js
        html = Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")
        assert 'id="enrol-forapp"' in html

    def test_withdrawing_takes_the_adoptions_own_apps(self):
        """Not the row's. Removing the mark she uses for two apps from one of
        their rows must take that mark and nothing else of hers."""
        js = self._js()
        assert "rm.dataset.forgetApps = JSON.stringify(p.adopted_for" in js
        body = js.split("function forgetPressed(", 1)[1].split("\n  }", 1)[0]
        assert "btn.dataset.forgetApps" in body
        assert "apps: apps" in body

    def test_the_apply_picks_by_the_app_in_front(self):
        """A lookup by name alone draws whichever mark was registered last
        onto all three apps."""
        app = Path("src/apt_log/ui/app.py").read_text(encoding="utf-8")
        assert "strokes_for(name, package=package)" in app
        assert "digest_for(name, package=package)" in app

    def test_a_patient_still_gets_one_row(self, client, tmp_path,
                                          monkeypatch):
        import json as _json

        from apt_log import enrolled as enrolled_mod
        from apt_log import schedule as sched

        path = tmp_path / "schedule.json"
        path.write_text(_json.dumps({
            "zone": "America/New_York",
            "visits": [{"patient": "Maria Garcia", "app": "inmyteam",
                        "days": ["mon"], "start": "09:00", "end": "10:00"}]}),
            encoding="utf-8")
        monkeypatch.setattr(sched, "SCHEDULE_PATH", path)
        monkeypatch.setattr(enrolled_mod, "STORE_PATH", tmp_path / "none.json")
        people = client.get("/signature/map").json()["people"]
        assert len(people) == 1
        assert people[0]["app"] == "inMyTeam"

    def test_one_app_registered_does_not_mark_the_other(self, client,
                                                        tmp_path, monkeypatch):
        """The whole reason the rows split."""
        import json as _json

        from apt_log import enrolled as enrolled_mod
        from apt_log import schedule as sched

        path = tmp_path / "schedule.json"
        path.write_text(_json.dumps({
            "zone": "America/New_York",
            "caregiver": "Ana Ruiz",
            "visits": [
                {"patient": "Maria Garcia", "app": "com.inmyteam.inmyteam",
                 "days": ["mon"], "start": "09:00", "end": "10:00"},
                {"patient": "Beto Sosa", "app": "com.hhaexchange.uma",
                 "days": ["tue"], "start": "09:00", "end": "10:00"}]}),
            encoding="utf-8")
        store = tmp_path / "sig.json"
        enrolled_mod.enroll(
            "Ana Ruiz",
            [[[0.1, 0.5], [0.3, 0.2], [0.5, 0.6]], [[0.6, 0.3], [0.9, 0.4]]],
            apps=["com.inmyteam.inmyteam"], path=store)
        monkeypatch.setattr(sched, "SCHEDULE_PATH", path)
        monkeypatch.setattr(enrolled_mod, "STORE_PATH", store)
        staff = {p["app"]: p["adopted"]
                 for p in client.get("/signature/map").json()["people"]
                 if p["role"] == "staff"}
        assert staff == {"inMyTeam": True, "HHAeXchange+": False}


class TestBackNeverLeavesTheAppBehind:
    """Reported: pressing Back on Exchange+ landed in another app.

    The refusal that exists for this only fires on an app that ANSWERS.
    HHAeXchange+ does not — it is Compose, publishes no fragment stack, and
    `nav_state` returns nothing for it. Nor can the task be read instead:
    that app keeps its schedule AND its visit detail in one activity and
    swallows Back itself, so "one activity, root of its task" is just as
    true on the page where Back correctly goes back.

    Unpredictable is not the same as unrecoverable.
    """

    UMA = "com.hhaexchange.uma"

    def _feed(self, monkeypatch, focuses):
        import sys

        from apt_log import feed as feed_mod

        ui = sys.modules["apt_log.ui.app"]
        seen = list(focuses)
        sent = []
        monkeypatch.setattr(feed_mod, "current_focus",
                            lambda: seen.pop(0) if len(seen) > 1 else seen[0])
        monkeypatch.setattr(feed_mod, "_adb",
                            lambda args, *a, **k: sent.append(args))
        monkeypatch.setattr(ui, "BACK_SETTLE", 0)
        return ui, sent

    def test_a_back_that_left_brings_the_app_straight_back(self, monkeypatch):
        ui, sent = self._feed(monkeypatch, [
            "com.inmyteam.inmyteam/.MainActivity"])
        assert ui._return_if_back_left(self.UMA) is True
        assert any("monkey" in a and self.UMA in a for a in sent)

    def test_a_back_that_stayed_is_left_alone(self, monkeypatch):
        """Re-launching an app that never left would throw away the screen
        she was on."""
        ui, sent = self._feed(monkeypatch, [
            self.UMA + "/com.hhaexchange.carehub.ui.activities.HomeActivity"])
        assert ui._return_if_back_left(self.UMA) is False
        assert sent == []

    def test_a_press_is_given_a_moment_to_land(self, monkeypatch):
        """A Back does not take effect instantly, and reading the focus too
        early would call every press an escape."""
        ui, sent = self._feed(monkeypatch, [
            "",                                   # nothing focused yet
            self.UMA + "/HomeActivity",           # ...and it was there
            self.UMA + "/HomeActivity"])
        assert ui._return_if_back_left(self.UMA) is False
        assert sent == []


class TestTheWayBackFromTheHomeScreen:
    """"Clicking back took me out of the app thinking it would just close the
    menu ... I end up on the android Home Screen and it appears stuck."

    Back from an app's first page pops it off the task stack and the phone
    lands on the launcher. The containment watchdog deliberately leaves the
    home screen alone — it exists to stop the phone WANDERING, and a launcher
    is where a phone rests — so nothing came to the rescue. Reproduced from
    this end three times in one evening, and after every deploy.
    """

    def _js(self):
        return (Path(__file__).resolve().parents[1]
                / "src/apt_log/ui/static/phone.js").read_text(encoding="utf-8")

    def _html(self):
        return (Path(__file__).resolve().parents[1]
                / "src/apt_log/ui/templates/phone.html").read_text(
                    encoding="utf-8")

    def test_the_card_offers_the_way_back(self, client):
        body = client.get("/app").text
        assert 'id="offapp-return"' in body

    def test_it_is_the_leading_choice_and_the_picker_still_stands(self, client):
        """The picker is for choosing a DIFFERENT app. This is the far
        commoner case, which is not a choice at all — she did not mean to
        leave — so it leads."""
        body = client.get("/app").text
        assert body.index('id="offapp-return"') < body.index('id="offapp-apps"')
        assert 'id="offapp-apps"' in body

    def test_it_names_the_app_it_returns_to(self):
        js = self._js()
        assert "i18n.backToApp" in js
        assert "{app}" in js[js.index("i18n.backToApp"):
                              js.index("i18n.backToApp") + 120]
        assert "backToApp:" in self._html()

    def test_it_reopens_rather_than_signing_in(self):
        """`appOpen` is the app's open-only macro: it activates and waits,
        and presses nothing inside the app. A stray press here costs a second
        and cannot touch a visit."""
        js = self._js()
        block = js[js.index("const goBack = document.getElementById"):]
        assert "appOpen[pkg]" in block[:900]
        # And it must not fall through to a sign-in walk.
        assert "dataset.macro" not in block[:900]

    def test_nothing_to_return_to_offers_no_button(self):
        """A phone that has never been in a care app has nowhere to go back
        to, and the card must not offer a press that would do nothing."""
        js = self._js()
        block = js[js.index("function paintReturn"):]
        assert "btn.hidden = !ready" in block[:900]

    def test_the_app_is_remembered_across_a_reload(self):
        """A page opened fresh onto a home screen would otherwise have
        nothing to offer but the picker."""
        js = self._js()
        assert "localStorage.setItem('aptlog-last-app'" in js
        assert "localStorage.getItem('aptlog-last-app')" in js

    def test_blocked_storage_does_not_break_the_card(self):
        """A private window throws on the accessor itself."""
        js = self._js()
        i = js.index("localStorage.setItem('aptlog-last-app'")
        assert "catch" in js[i:i + 200]

    def test_she_is_not_ejected_past_the_card(self):
        """EJECTING HER HID THE WAY BACK. The card lives in the screen view;
        switching to the picker meant she never saw it, and the two-press
        hunt through the tiles is what "it appears stuck" was. The screen
        view holds when the app she just left is one we can reopen."""
        js = self._js()
        i = js.index("} else if (onLauncher && body.dataset.view === 'screen'")
        assert "!appOpen[wasPackage]" in js[i:i + 140]

    def test_an_app_we_cannot_reopen_still_sends_her_to_the_picker(self):
        """The branch keeps the case it was written for: with nowhere to
        return to, holding the screen view would strand her on a card whose
        only offer is the picker anyway."""
        js = self._js()
        i = js.index("} else if (onLauncher && body.dataset.view === 'screen'")
        assert "view('launcher')" in js[i:i + 900]
