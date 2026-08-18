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
                         "/settings/density/clear"}

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

    def test_the_reboot_is_the_only_thing_that_asks(self, client):
        """Everything else is one press. A confirmation on a control that can
        be undone by pressing it again is a step people learn to click
        through, which is how the one that matters gets clicked through too."""
        import re

        from apt_log import macros

        body = client.get("/console").text
        asked = re.findall(r'data-confirm="[^"]*"[^>]*>\s*<input[^>]*value="([a-z_]+)"',
                           body)
        assert set(asked) == set(macros.CONFIRM)

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
