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
        body = client.get("/").text.lower()
        for phrase in ("record anyway", "registrar de todos modos",
                       "force", "forzar", "override"):
            assert phrase not in body

    def test_only_the_declared_write_routes_exist(self):
        posts = {
            r.path for r in app.routes
            if getattr(r, "methods", None) and "POST" in r.methods
        }
        assert posts == {"/language", "/signature", "/relay", "/device", "/tap",
                         "/macro", "/acknowledge", "/control", "/sign"}

    def test_no_route_accepts_a_raw_coordinate_or_keycode(self, client):
        """/tap takes an element from a named frame; /device takes an action
        name from an allow-list. Neither takes a number that means "here"."""
        assert client.post("/tap", json={"frame": "f", "x": 100, "y": 200}
                           ).status_code in (400, 409)
        assert client.post("/device", data={"action": "66"},
                           follow_redirects=False
                           ).headers["location"] == "/?device=failed"


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
        body = client.get("/").text
        assert "PT-0042" in body      # REQ-10.3: she must see what she is signing for
        assert "Firme" in body
        queue.cancel()


class TestRelayRoute:
    def test_a_token_request_renders_a_code_field_in_spanish(self, client):
        queue.request_token("PT-0042", datetime(2026, 8, 14, 20, 0))
        body = client.get("/").text
        assert "PT-0042" in body          # she must see which visit this is for
        assert "Token de seguridad" in body
        assert 'name="value"' in body
        queue.cancel()

    def test_the_token_panel_does_not_claim_to_prove_where_she_is(self, client):
        """Her token device travels with her. It is the reason the app never
        runs a location check, not evidence of one — and the natural misreading
        goes the other way, so the page says so where she can see it."""
        queue.request_token("PT-0042", None)
        body = client.get("/").text
        assert "no indica dónde está usted" in body
        assert "el teléfono está en el edificio" in body
        queue.cancel()

    def test_a_choice_renders_the_apps_own_words(self, client):
        """Relayed, not reworded — she is answering on a screen the controller
        is looking at, and a paraphrase here would be a different screen."""
        queue.request_choice("PT-0042", None, ("GPS", "token de seguridad"))
        body = client.get("/").text
        assert "GPS" in body and "token de seguridad" in body
        queue.cancel()

    def test_choosing_location_carries_a_warning_about_where_the_phone_is(self, client):
        queue.request_choice("PT-0042", None, ("GPS", "token de seguridad"))
        body = client.get("/").text
        assert "el teléfono no está con usted" in body
        queue.cancel()

    def test_an_option_the_app_never_offered_is_refused(self, client):
        """The route must not widen what the queue allows."""
        nonce = queue.request_choice("PT-0042", None, ("token de seguridad",))
        r = client.post("/relay", follow_redirects=False,
                        data={"nonce": nonce, "kind": KIND_CHOICE, "value": "GPS"})
        assert r.headers["location"] == "/?relay=refused"
        assert queue.current() is not None      # still outstanding, not consumed
        queue.cancel()

    def test_an_offered_option_is_carried(self, client):
        nonce = queue.request_choice("PT-0042", None, ("token de seguridad",))
        r = client.post("/relay", follow_redirects=False,
                        data={"nonce": nonce, "kind": KIND_CHOICE,
                              "value": "token de seguridad"})
        assert r.headers["location"] == "/?relay=sent"
        assert queue.wait(0.5).value == "token de seguridad"

    def test_a_token_is_carried(self, client):
        nonce = queue.request_token("PT-0042", None)
        r = client.post("/relay", follow_redirects=False,
                        data={"nonce": nonce, "kind": KIND_TOKEN, "value": "4821-77"})
        assert r.headers["location"] == "/?relay=sent"
        assert queue.wait(0.5).value == "482177"

    def test_a_stale_nonce_is_told_so_rather_than_silently_dropped(self, client):
        r = client.post("/relay", follow_redirects=False,
                        data={"nonce": "nope", "kind": KIND_TOKEN, "value": "482177"})
        assert r.headers["location"] == "/?relay=expired"

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
    def test_an_unpublished_mirror_says_it_does_not_know(self, client):
        """Patched rather than assumed.

        Reading the real /var/lib/aptlog/mirror.json made this pass on a laptop
        and fail on any machine where the feed is running -- which is every
        deployed one. It blocked a legitimate deploy on the Florida unit, and a
        gate that rejects good revisions for environmental reasons is a gate
        people learn to bypass.
        """
        with patch.object(mirror_mod, "read", return_value=Mirror()):
            body = client.get("/").text
        assert "no ha informado" in body

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
        assert r.headers["location"] == "/?device=failed"

    def test_a_raw_keycode_is_not_an_action(self, client):
        r = client.post("/device", follow_redirects=False,
                        data={"action": "66"})
        assert r.headers["location"] == "/?device=failed"

    def test_navigation_is_offered_so_no_screen_is_a_dead_end(self, client):
        """A keypad came up with its nav bar outside the tappable set, leaving
        no way back from a thousand miles away."""
        body = client.get("/").text
        for act in ("wake", "back", "home", "recents"):
            assert f'name="action" value="{act}"' in body

    def test_the_page_still_never_offers_a_raw_keycode(self, client):
        body = client.get("/").text
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
        body = client.get("/").text
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
        assert client.get("/").status_code == 200
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
        assert r.headers["location"] == "/?macro=started"

    def test_an_unknown_macro_is_refused(self, client, tmp_path):
        with patch("apt_log.macros.REQUEST_PATH", tmp_path / "req.json"):
            r = client.post("/macro", follow_redirects=False,
                            data={"name": "sudo-rm-rf"})
        assert r.headers["location"] == "/?macro=unknown"
        assert not (tmp_path / "req.json").exists()

    def test_the_page_offers_only_registered_macros(self, client):
        from apt_log import macros
        body = client.get("/").text
        for name in macros.MACROS:
            assert f'value="{name}"' in body

    def test_the_page_says_shortcuts_never_clock_in(self, client):
        """The line, stated where she reads it rather than only in a docstring."""
        body = client.get("/").text
        assert "registran la entrada" in body


class TestStaleIsVisible:
    """A page that has stopped listening must not look like a quiet one.

    She sat in front of a page frozen at load time with no way to tell: the
    picture was two minutes old and the "Taken" line beside it, rendered once by
    the server, said so without meaning to.
    """

    def test_the_page_carries_an_offline_notice(self, client):
        body = client.get("/").text
        assert "offline-note" in body
        assert "Sin conexión con el controlador" in body

    def test_tapping_is_disabled_while_offline(self, client):
        """Aiming at a frozen picture is how a tap lands somewhere she did not
        choose. The overlay stops accepting clicks rather than trusting it."""
        body = client.get("/").text
        assert "body.offline .hit { pointer-events:none; }" in body

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
    """

    def test_the_page_can_name_every_refusal(self, client):
        """The reason crosses as a code, so the sentence has to be on the page
        already. A code with no sentence falls back rather than saying nothing.
        """
        body = client.get("/").text
        for code in ("no_focus", "login_activity", "password_field",
                     "secure_screen", "capture_failed"):
            assert code + ":" in body
        assert "blockedOther" in body

    def test_the_wrong_picture_is_hidden_rather_than_shown(self, client):
        body = client.get("/").text
        assert "body.blind #shot { visibility:hidden; }" in body

    def test_a_box_with_no_picture_under_it_carries_its_own_label(self, client):
        body = client.get("/").text
        assert "body.blind .hit .label" in body

    def test_the_app_gets_to_say_what_it_said(self, client):
        body = client.get("/").text
        assert "screen-said" in body
        assert "La aplicación muestra este mensaje" in body

    def test_the_overlay_exists_before_any_picture_ever_has(self, client):
        """A controller on a sign-in screen since boot has never written a
        capture. That used to render "no recent picture" and nothing else — no
        boxes and no way through, on the one screen that needs a way through."""
        # Patched rather than assumed. The first version of this test relied on
        # no capture existing, which is true on a build machine and false on the
        # controller — where the suite also runs, as the deploy gate, and where
        # it duly failed. A test that depends on the machine is testing the
        # machine.
        from apt_log.ui import state as state_mod

        with patch.object(state_mod, "SCREENSHOT_PATH",
                          Path("/nonexistent/never-captured.jpg")):
            body = client.get("/").text
        assert 'id="overlay"' in body
        assert "No hay ninguna imagen reciente" in body


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


class TestFormsDoNotNavigate:
    """The bug that made this a bug report.

    Every /device form carries a hidden <input name="action">, and a form
    control named "action" shadows the form's own action property. So
    `form.action` returned that input element, `form.action.endsWith` threw, the
    submit listener died before preventDefault, and the browser posted the form
    for real — reloading the page. Pause and Resume had it too, for the same
    reason. Asserted against the shipped script because the failure was in the
    one line of it that nothing else can reach.
    """

    SCRIPT = Path(__file__).resolve().parents[1] / (
        "src/apt_log/ui/static/live.js")

    def test_the_action_attribute_is_read_not_the_property(self):
        source = self.SCRIPT.read_text(encoding="utf-8")
        assert "getAttribute('action')" in source

    def test_the_shadowed_property_is_never_dereferenced(self):
        """`form.action` is a trap on precisely the forms that most need to be
        intercepted, so it is not used at all.

        Comments are stripped first — the one above the fix names the trap in
        order to explain it, and a guard that cannot tell code from prose would
        forbid describing the bug it exists to prevent.
        """
        code = [line for line in self.SCRIPT.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith(("//", "*", "/*"))]
        source = "\n".join(code)
        for trap in ("form.action.", "form.action)", "form.action,"):
            assert trap not in source

    def test_every_form_that_names_a_field_action_is_covered(self, client):
        """Names the forms this applies to, so a new one cannot quietly join
        them. Any form posting a field called "action" hits the same trap."""
        body = client.get("/").text
        # Four device buttons, and pause/resume — which renders one form whose
        # value flips, not two.
        assert body.count('name="action"') == 5


class TestPauseSurvivesItsOwnPress:
    """One button whose meaning inverts, and nothing was inverting it.

    The server had been pushing `paused` over the socket the whole time and no
    client code listened. So after a Pause the button still read "Pause" and
    still posted `pause`: pressing it again paused a second time, and there was
    no way to resume without reloading the page — the one thing this page is
    supposed to have stopped needing. Verified against the live controller,
    which is how it was found.
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

    def test_both_labels_ship_so_the_client_never_writes_one(self, client):
        """The rule the whole live stream is built on: a page that renders in
        Spanish until it updates itself into English is worse than one that
        never updates. So the server sends both words and the client picks."""
        body = client.get("/").text
        assert 'data-paused="Reanudar"' in body
        assert 'data-running="Pausar"' in body

    def test_the_notice_is_hidden_rather_than_absent(self, client):
        """It has to be revealable without the client building the element,
        which would mean the client owning the sentence."""
        body = client.get("/").text
        assert 'id="paused-notice"' in body
        assert "El programa está en pausa" in body

    def test_the_client_listens_for_it(self):
        source = (Path(__file__).resolve().parents[1]
                  / "src/apt_log/ui/static/live.js").read_text(encoding="utf-8")
        assert "applyPaused" in source
        assert "msg.paused" in source


class TestPhoneAppView:
    """The full-screen view she bookmarks. A skin, not a capability.

    Everything here rides machinery the dashboard already has — same socket,
    same tap verification, same macro allow-list — so these tests are about the
    skin keeping the rules, not about new rules.
    """

    def test_it_renders_in_her_language(self, client):
        body = client.get("/app").text
        assert "Registro de Horas de Pacientes" in body
        assert "Aplicaciones" in body

    def test_all_four_apps_are_offered(self, client):
        body = client.get("/app").text
        for name in ("HHAeXchange", "HHAeXchange+", "Mobile Caregiver+",
                     "inMyTeam"):
            assert name in body

    def test_every_tile_runs_an_allow_listed_macro(self, client):
        """The tile posts a name; the name must be one macros.MACROS holds.
        A tile that invented its own would be remote scripting with a nicer
        icon."""
        from apt_log.macros import MACROS
        from apt_log.ui.app import PHONE_APPS

        for entry in PHONE_APPS:
            assert entry["macro"] in MACROS

    def test_it_is_installable(self, client):
        body = client.get("/app").text
        assert "manifest.webmanifest" in body
        assert "apple-mobile-web-app-capable" in body
        r = client.get("/static/manifest.webmanifest")
        assert r.status_code == 200
        manifest = json.loads(r.text)
        assert manifest["start_url"] == "/app"
        assert client.get("/static/icons/icon-180.png").status_code == 200

    def test_no_service_worker_anywhere(self, client):
        """Offline caching would keep copies of what the phone's screen said on
        her phone. This page is a window, not a document."""
        body = client.get("/app").text
        assert "serviceWorker" not in body
        js = (Path(__file__).resolve().parents[1]
              / "src/apt_log/ui/static/phone.js").read_text(encoding="utf-8")
        assert "serviceWorker" not in js

    def test_the_client_owns_no_action_sentences(self):
        """phone.js may hold choreography, not prose: everything readable
        arrives rendered from the catalog."""
        js = (Path(__file__).resolve().parents[1]
              / "src/apt_log/ui/static/phone.js").read_text(encoding="utf-8")
        assert "getAttribute('action')" in js       # the shadowing trap, again


class TestWireframeOverTheSocket:
    def _screen_doc(self, tmp_path):
        doc = {
            "id": "abc123", "img": "", "at": "2026-08-15T10:00:00",
            "size": [720, 1600], "screen": "login", "blocked": "login_activity",
            "notice": "Debes de iniciar sesión.",
            "elements": [{"rid": "btn_login", "cls": "Button",
                          "b": [19, 744, 731, 804], "focused": False,
                          "selected": False, "checked": False,
                          "has_text": True, "txt": "Iniciar sesión"}],
            "statics": [{"cls": "TextView", "b": [0, 100, 720, 160],
                         "txt": "Bienvenida"}],
        }
        (tmp_path / "screen.json").write_text(json.dumps(doc))
        return doc

    def test_the_wireframe_is_pushed_as_rendered_html(self, client, tmp_path):
        with patch.object(state_mod, "STATE_DIR", tmp_path):
            self._screen_doc(tmp_path)
            with client.websocket_connect("/ws") as ws:
                msg = ws.receive_json()
        assert "screen_html" in msg
        assert "Iniciar sesión" in msg["screen_html"]
        assert "Bienvenida" in msg["screen_html"]
        assert msg["screen"]["blocked"] == "login_activity"

    def test_a_wireframe_button_carries_the_same_aim_a_tap_posts(self, client, tmp_path):
        """rid, class, bounds — the identity the server re-verifies. The
        wireframe changes what she sees, not what a tap may do."""
        with patch.object(state_mod, "STATE_DIR", tmp_path):
            self._screen_doc(tmp_path)
            with client.websocket_connect("/ws") as ws:
                msg = ws.receive_json()
        assert 'data-aim=' in msg["screen_html"]
        assert '"rid": "btn_login"' in msg["screen_html"]
        assert '[19, 744, 731, 804]' in msg["screen_html"]

    def test_an_unchanged_screen_is_not_re_pushed(self, client, tmp_path):
        """The timestamp moves on every write; the comparison must not follow
        it, or "did the screen change" becomes "has a second passed".

        A frame change carries the second message, because a tick with nothing
        to say sends nothing — which is itself the behaviour under test.
        """
        with patch.object(state_mod, "STATE_DIR", tmp_path):
            doc = self._screen_doc(tmp_path)
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
                # Screen: timestamp only. Frame: a real change to ride on.
                doc["at"] = "2026-08-15T10:00:05"
                (tmp_path / "screen.json").write_text(json.dumps(doc))
                (tmp_path / "frame.json").write_text(json.dumps(
                    {"id": "different", "img": "", "size": [720, 1600],
                     "elements": [], "blocked": "", "notice": ""}))
                ws.send_json({"type": "noop"})
                second = ws.receive_json()
        assert "frame" in second
        assert "screen_html" not in second


class TestSignRoute:
    def test_strokes_are_queued_for_the_feed(self, client, tmp_path):
        from apt_log import sign as sign_mod

        with patch.object(sign_mod, "REQUEST_PATH", tmp_path / "req.json"):
            r = client.post("/sign", json={
                "strokes": [[[0.1, 0.2, 0], [0.5, 0.5, 40]]], "aspect": 2.2})
        assert r.status_code == 200
        assert (tmp_path / "req.json").exists()

    def test_garbage_is_refused_at_the_door(self, client):
        r = client.post("/sign", json={"strokes": [[[5, 5, 0]]]})
        assert r.status_code == 400
        r = client.post("/sign", json={"strokes": []})
        assert r.status_code == 400

    def test_the_page_offers_the_pad(self, client):
        body = client.get("/app").text
        assert 'id="signpad"' in body
        assert "Firmar" in body
        # The sentence that keeps the line where it is: the app's own save
        # button is hers, not the replay's.
        assert "guardar de la propia aplicación" in body
