"""The mirror feed, and the screenshots it refuses to take.

Every test here is about the refusals. Capturing a screen is easy; the reason
this module needed writing at all is that it runs outside the process holding
`capture.suppressed()`, so REQ-3's interlock had to be rebuilt from scratch.
"""

from __future__ import annotations

import io
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from pathlib import Path

from apt_log import feed, prefs

HOME = "com.hhaexchange.caregiver/com.hhaexchange.caregiver.HomeActivity"
SIGNIN = "com.hhaexchange.caregiver/com.hhaexchange.caregiver.SignInActivity"
PNG = b"\x89PNG\r\n\x1a\n fake"


class TestLoginDetection:
    @pytest.mark.parametrize("focus", [
        SIGNIN,
        "com.other.app/.LoginActivity",
        "com.other.app/.AuthenticationActivity",
        "com.other.app/.EnterPasscodeActivity",
        "com.x/.CredentialPromptActivity",
    ])
    def test_anything_login_shaped_is_refused(self, focus):
        """Four apps means four login screens and three are unseen. Matching
        substrings catches them without anyone remembering to add them."""
        assert feed.looks_like_a_login_screen(focus) is True

    @pytest.mark.parametrize("focus", [HOME, "com.x/.TodayScheduleActivity"])
    def test_ordinary_screens_are_not(self, focus):
        assert feed.looks_like_a_login_screen(focus) is False


class TestCaptureRefusals:
    def test_refuses_when_the_focus_cannot_be_read(self):
        """Knowing nothing is not permission."""
        with patch.object(feed, "window_state", return_value=("", True, "")):
            png, _, reason = feed.capture()
        assert png is None
        assert reason == feed.NO_FOCUS

    def test_refuses_on_a_login_screen(self):
        with patch.object(feed, "window_state", return_value=(SIGNIN, True, "")):
            png, _, reason = feed.capture()
        assert png is None
        assert reason == feed.LOGIN_ACTIVITY

    def test_refuses_when_a_password_field_is_anywhere_on_screen(self):
        """Anywhere, not merely focused. This runs on a timer with no idea what
        is about to happen, and the picture ends up on a web page."""
        with patch.object(feed, "window_state", return_value=(HOME, True, "")):
            png, _, reason = feed.capture(
                hierarchy='<node password="true" bounds="[0,0][1,1]"/>')
        assert png is None
        assert reason == feed.PASSWORD_FIELD

    def test_captures_when_the_hierarchy_is_unreadable_but_activity_is_safe(self):
        """The documented weak point: UiAutomator2 holds the dump service during
        an Appium session, which is exactly when watching matters most."""
        with patch.object(feed, "window_state", return_value=(HOME, True, "")), \
             patch.object(feed, "_adb") as adb:
            adb.return_value.returncode = 0
            adb.return_value.stdout = PNG
            png, _, reason = feed.capture(hierarchy=None)
        assert png == PNG and reason == feed.CAPTURED

    def test_a_refused_capture_is_reported_not_silent(self):
        with patch.object(feed, "window_state", return_value=(HOME, True, "")), \
             patch.object(feed, "_adb") as adb:
            adb.return_value.returncode = 0
            adb.return_value.stdout = b""
            png, _, reason = feed.capture(hierarchy=None)
        assert png is None and reason == feed.SECURE_SCREEN


class TestScreenMapping:
    LEGACY = "com.hhaexchange.caregiver"

    @pytest.mark.parametrize("focus,expected", [
        (HOME, "home"),
        (SIGNIN, "login"),
        (f"{LEGACY}/.TodayScheduleActivity", "today"),
        (f"{LEGACY}/.VisitDetailActivity", "visit"),
        (f"{LEGACY}/.AppLaunchActivity", "startup"),
        (f"{LEGACY}/.SomethingNew", "unknown"),
        ("", "unknown"),
    ])
    def test_maps_into_the_mirror_vocabulary(self, focus, expected):
        assert feed.screen_for(focus) == expected

    def test_the_atlas_is_per_package_not_a_flat_table(self):
        """Another app's HomeActivity is not the legacy app's home page —
        page names belong to the app that owns them."""
        assert feed.screen_for("com.example.other/.HomeActivity") == "unknown"

    @pytest.mark.parametrize("focus,expected", [
        # The login markers answer for care apps the atlas has not mapped:
        # HHAeXchange+ hosts its form under an "auth" activity, Mobile
        # Caregiver+ parks behind a "pin" one. This is what lets landing on
        # them auto-trigger their sign-in macros.
        ("com.hhaexchange.uma/.AuthenticationActivity", "login"),
        ("com.tellus.evv.v2/.PinCodeActivity", "login"),
        # ...but a random app's "auth"-named screen is not a care login.
        ("com.example.other/.AuthActivity", "unknown"),
    ])
    def test_unmapped_care_login_screens_are_still_called_logins(
            self, focus, expected):
        assert feed.screen_for(focus) == expected

    def test_the_phone_home_screen_is_a_launcher_not_a_page(self):
        assert feed.screen_for(
            "com.android.launcher3/.uioverrides.QuickstepLauncher") == "launcher"


class TestWriteFrame:
    def test_writes_the_png_and_publishes_a_frame(self, tmp_path):
        shot = tmp_path / "screen.png"
        with patch.object(feed, "capture", return_value=(PNG, HOME, "")), \
             patch.object(feed.mirror_mod, "publish") as publish:
            feed.write_frame(shot)
        assert shot.read_bytes() == PNG
        assert publish.call_args.kwargs["screen"] == "home"

    def test_publishes_a_frame_even_when_capture_is_refused(self, tmp_path):
        """Silence looks identical to the process being dead. "On the sign-in
        screen, no picture available" is information."""
        shot = tmp_path / "screen.png"
        with patch.object(feed, "capture",
                          return_value=(None, SIGNIN, feed.LOGIN_ACTIVITY)), \
             patch.object(feed.mirror_mod, "publish") as publish:
            feed.write_frame(shot)
        assert not shot.exists()
        assert publish.call_args.kwargs["screen"] == "login"

    def test_a_refused_picture_is_not_reported_as_a_stuck_controller(self, tmp_path):
        """It published step "blocked" — "it has stopped and cannot continue on
        its own" — directly above a sign-in screen the controller was walking
        through perfectly well. Whether a picture may be taken says nothing
        about whether anything is stuck."""
        shot = tmp_path / "screen.png"
        with patch.object(feed, "capture",
                          return_value=(None, SIGNIN, feed.LOGIN_ACTIVITY)), \
             patch.object(feed.mirror_mod, "publish") as publish:
            feed.write_frame(shot)
        assert publish.call_args.kwargs["step"] != "blocked"

    def test_no_partial_image_is_left_for_the_page_to_read(self, tmp_path):
        shot = tmp_path / "screen.png"
        with patch.object(feed, "capture", return_value=(PNG, HOME, "")), \
             patch.object(feed.mirror_mod, "publish"):
            feed.write_frame(shot)
        assert not (tmp_path / "screen.tmp").exists()


class TestRun:
    def test_the_tick_looks_for_a_code_to_pass_on(self, tmp_path):
        """This loop is the only thing in the system that would notice a
        sign-in code arriving for a login the portal never started —
        somebody signing in from their own phone makes inMyTeam text THIS
        one, and nothing else here is watching."""
        from apt_log import sms

        looked = []
        with patch.object(feed, "write_frame", return_value="ok"), \
             patch.object(feed, "read_hierarchy", return_value=None), \
             patch.object(sms, "forward_any_new",
                          side_effect=lambda *a, **k: looked.append(1) or 0), \
             patch.object(feed.time, "sleep"):
            feed.run(tmp_path / "s.png", interval=0, iterations=3)
        assert len(looked) == 3

    def test_a_phone_that_will_not_answer_does_not_stop_the_watcher(self,
                                                                   tmp_path):
        """The same rule the version check lives under. A courtesy that can
        fail must not take the picture down with it."""
        from apt_log import sms

        frames = []
        with patch.object(feed, "write_frame",
                          side_effect=lambda *a, **k: frames.append(1) or "ok"), \
             patch.object(feed, "read_hierarchy", return_value=None), \
             patch.object(sms, "forward_any_new",
                          side_effect=RuntimeError("no adb")), \
             patch.object(feed.time, "sleep"):
            feed.run(tmp_path / "s.png", interval=0, iterations=3)
        assert len(frames) == 3

    def test_one_bad_frame_does_not_stop_the_watcher(self, tmp_path):
        calls = []

        def flaky(_path, _serial=None, _hierarchy=None, _h_at=0.0,
                  _h_focus=""):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("adb hiccup")
            return "ok"

        with patch.object(feed, "write_frame", side_effect=flaky), \
             patch.object(feed, "read_hierarchy", return_value=None), \
             patch.object(feed.time, "sleep"):
            feed.run(tmp_path / "s.png", interval=0, iterations=3)
        assert len(calls) == 3

    def test_a_slow_hierarchy_never_delays_the_picture(self, tmp_path):
        """The whole point of the separate thread. A failed Appium connect takes
        25 seconds; on the picture's thread that freezes the thing she is
        actually looking at."""
        frames = []

        def glacial(*_a, **_k):
            time.sleep(5)          # real sleep, on the other thread
            return "<node/>"

        with patch.object(feed, "write_frame",
                          side_effect=lambda *a, **k: frames.append(1) or "ok"), \
             patch.object(feed, "read_hierarchy", side_effect=glacial):
            started = time.monotonic()
            feed.run(tmp_path / "s.png", interval=0.01, iterations=5)
            elapsed = time.monotonic() - started
        assert len(frames) == 5
        assert elapsed < 2.0, "the picture waited on the hierarchy"

    def test_the_picture_uses_the_last_good_hierarchy(self, tmp_path):
        """A read that fails leaves the previous overlay standing rather than
        blanking it — stale boxes beat no boxes.

        Polled rather than slept. The first version waited a fixed 150ms, which
        passed on a laptop and lost the race on the Pi, where the focus read the
        loop makes is a real 173ms adb call. The deploy gate caught it. A test
        whose result depends on the speed of the machine under it is the same
        bug twice in one session.
        """
        good = '<node clickable="true" bounds="[0,0][10,10]"/>'
        h = feed._Hierarchy(None, every=0.01)
        with patch.object(feed, "read_hierarchy",
                          side_effect=[good] + [None] * 50), \
             patch.object(feed, "window_state", return_value=("com.x/.Home", True, "")):
            h.start()
            deadline = time.monotonic() + 5
            while h.xml is None and time.monotonic() < deadline:
                time.sleep(0.01)
            h.stop()
        assert h.xml == good


class TestElements:
    """The overlay's data, which is also what a tap posts back.

    Built to hold nothing worth protecting: the words live in the screenshot,
    where they are already visible to whoever has the page open. The structure is
    what crosses the wire a second time and lands in a log.
    """

    XML = (
        '<node class="android.widget.Button" resource-id="com.x:id/btn_clock_in"'
        ' text="Registrar Entrada" clickable="true" focused="true"'
        ' selected="false" bounds="[19,744][731,804]" />'
        '<node class="android.widget.TextView" resource-id="com.x:id/lbl_patient_name"'
        ' text="PACIENTE FICTICIA" clickable="false" bounds="[0,100][720,160]" />'
        '<node class="android.view.ViewGroup" resource-id="" text=""'
        ' clickable="true" bounds="[0,200][720,400]" />'
        '<node class="android.widget.View" resource-id="com.x:id/zero"'
        ' clickable="true" bounds="[5,5][5,5]" />'
    )

    def test_only_clickable_things_are_offered(self):
        rids = [e["rid"] for e in feed.elements(self.XML)]
        assert "btn_clock_in" in rids
        assert "lbl_patient_name" not in rids

    def test_no_text_survives_anywhere_in_the_output(self):
        """The one property that matters. A patient name reaching this map would
        put it in a POST body and a log line."""
        import json
        blob = json.dumps(feed.elements(self.XML))
        for secret in ("PACIENTE", "FICTICIA", "Registrar", "Entrada"):
            assert secret not in blob

    def test_text_presence_is_kept_as_a_boolean(self):
        """Enough to draw the box; not enough to identify anyone."""
        btn = next(e for e in feed.elements(self.XML) if e["rid"] == "btn_clock_in")
        assert btn["has_text"] is True

    def test_bounds_and_state_are_carried(self):
        btn = next(e for e in feed.elements(self.XML) if e["rid"] == "btn_clock_in")
        assert btn["b"] == [19, 744, 731, 804]
        assert btn["focused"] is True and btn["selected"] is False

    def test_an_element_with_no_area_is_dropped(self):
        """A zero-size box cannot be aimed at and would only add noise."""
        assert "zero" not in [e["rid"] for e in feed.elements(self.XML)]

    def test_unnamed_containers_are_kept(self):
        """Rows are often a bare clickable ViewGroup — dropping them would make
        the visit list untappable, which is the screen that matters most."""
        assert any(e["cls"] == "ViewGroup" and e["rid"] == ""
                   for e in feed.elements(self.XML))

    def test_garbage_in_does_not_raise(self):
        assert feed.elements("") == []
        assert feed.elements("<node bounds='nonsense' clickable='true'/>") == []


class TestBlindScreens:
    """What she is shown when the picture is refused.

    The refusal itself was never in doubt. What was missing is that a refusal
    used to leave her with an unlabelled rectangle where a dialog was — and the
    *previous* screen's capture still on disk underneath it.
    """

    # A sign-in screen with the app's alert on top of it. Both of the app's
    # alert ids, a password field carrying what was typed into it, and a
    # credential in an ordinary field: everything this rule has to sort out.
    ALERT = (
        '<node class="android.widget.TextView" resource-id="com.x:id/lbl_message"'
        ' text="Debes de iniciar sesión." clickable="false"'
        ' bounds="[40,800][680,900]" />'
        '<node class="android.widget.Button" resource-id="com.x:id/btn_negative"'
        ' text="DE ACUERDO" clickable="true" bounds="[28,940][692,1030]" />'
        '<node class="android.widget.EditText" resource-id="com.x:id/txt_password"'
        ' text="hunter2" password="true" clickable="true"'
        ' bounds="[40,600][680,660]" />'
    )

    def test_a_credential_screen_may_speak(self):
        assert feed.text_is_disclosable(feed.LOGIN_ACTIVITY) is True
        assert feed.text_is_disclosable(feed.PASSWORD_FIELD) is True

    def test_a_screen_the_app_sealed_may_not(self):
        """FLAG_SECURE is the app's own choice and can land anywhere, including
        on a patient. A credential screen is reached before any patient is."""
        assert feed.text_is_disclosable(feed.SECURE_SCREEN) is False
        assert feed.text_is_disclosable(feed.NO_FOCUS) is False

    def test_a_captured_screen_may_not(self):
        """Where there is a picture, the words are already in it, and a second
        copy in a JSON file on disk buys nothing."""
        assert feed.text_is_disclosable(feed.CAPTURED) is False

    def test_the_dialog_message_comes_across(self):
        assert feed.alert_message(self.ALERT) == "Debes de iniciar sesión."

    def test_no_dialog_means_no_message(self):
        """The fallback picks the longest text on screen, which is only sound
        under a modal. Without one it must not volunteer anything at all."""
        plain = ('<node class="android.widget.TextView" text="PACIENTE FICTICIA"'
                 ' clickable="false" bounds="[0,0][720,60]" />')
        assert feed.alert_message(plain) == ""

    def test_the_screen_behind_a_system_dialog_is_not_read_out(self):
        """The save-password prompt is `android:id/...` sitting on top of a
        sign-in screen. Taking the longest text anywhere — the first version of
        this — would have read out the device registration id instead of the
        question actually being asked."""
        behind = (
            '<node class="android.widget.TextView"'
            ' resource-id="com.x:id/text_view_mobile_device_id"'
            ' text="ID del dispostivo movil: 67F6DEB4E22DEB87" clickable="false"'
            ' bounds="[40,1500][680,1560]" />'
            '<node class="android.widget.TextView"'
            ' resource-id="android:id/autofill_save_title"'
            ' text="Guardar contraseña?" clickable="false"'
            ' bounds="[40,1280][680,1340]" />'
            '<node class="android.widget.Button"'
            ' resource-id="android:id/autofill_save_no" text="No, gracias"'
            ' clickable="true" bounds="[54,1357][252,1438]" />'
        )
        assert feed.alert_message(behind) == "Guardar contraseña?"

    def test_a_dialog_with_no_known_message_id_still_speaks(self):
        """Four apps, four dialog layouts. Recognising the buttons is what makes
        this work on the three that have never been opened."""
        other = (
            '<node class="android.widget.TextView" resource-id="com.y:id/blurb"'
            ' text="Your session has ended. Please sign in again."'
            ' clickable="false" bounds="[0,300][720,400]" />'
            '<node class="android.widget.TextView" resource-id="com.y:id/footer"'
            ' text="Below the buttons, so not the message." clickable="false"'
            ' bounds="[0,600][720,660]" />'
            '<node class="android.widget.Button" resource-id="com.y:id/button1"'
            ' text="OK" clickable="true" bounds="[400,500][700,560]" />'
        )
        assert "session has ended" in feed.alert_message(other)

    def test_buttons_are_labelled_when_there_is_no_picture(self):
        els = feed.elements(self.ALERT, label=True)
        button = next(e for e in els if e["rid"] == "btn_negative")
        assert button["txt"] == "DE ACUERDO"

    def test_a_typed_field_is_never_disclosed(self):
        """The whole reason the picture was refused. An EditText's own node
        carries what has been typed into it, so labelling is not extended to
        one under any rule."""
        els = feed.elements(self.ALERT, label=True)
        field = next(e for e in els if e["rid"] == "txt_password")
        assert "txt" not in field
        assert "hunter2" not in json.dumps(els)

    def test_labelling_is_off_unless_asked_for(self):
        assert all("txt" not in e for e in feed.elements(self.ALERT))

    def test_the_frame_carries_the_reason_and_the_words(self, tmp_path):
        shot = tmp_path / "screen.png"
        with patch.object(feed, "window_state", return_value=(SIGNIN, True, "")), \
             patch.object(feed, "screen_size", return_value=[720, 1600]), \
             patch.object(feed.mirror_mod, "publish"):
            feed.write_frame(shot, hierarchy=self.ALERT)
        frame = json.loads((tmp_path / "frame.json").read_text())
        assert frame["blocked"] == feed.LOGIN_ACTIVITY
        assert frame["notice"] == "Debes de iniciar sesión."
        assert frame["captured"] is False

    def test_a_sealed_screen_says_why_but_stays_quiet(self, tmp_path):
        shot = tmp_path / "screen.png"
        with patch.object(feed, "capture",
                          return_value=(None, HOME, feed.SECURE_SCREEN)), \
             patch.object(feed, "screen_size", return_value=[720, 1600]), \
             patch.object(feed.mirror_mod, "publish"):
            feed.write_frame(shot, hierarchy=self.ALERT)
        frame = json.loads((tmp_path / "frame.json").read_text())
        assert frame["blocked"] == feed.SECURE_SCREEN
        assert frame["notice"] == ""
        assert all("txt" not in e for e in frame["elements"])

    def test_a_captured_frame_carries_no_words_at_all(self, tmp_path):
        """The rule that was already there, pinned against the new field."""
        shot = tmp_path / "screen.png"
        with patch.object(feed, "capture", return_value=(PNG, HOME, feed.CAPTURED)), \
             patch.object(feed, "screen_size", return_value=[720, 1600]), \
             patch.object(feed.mirror_mod, "publish"):
            feed.write_frame(shot, hierarchy=TestElements.XML)
        frame = json.loads((tmp_path / "frame.json").read_text())
        assert frame["blocked"] == ""
        assert frame["notice"] == ""
        blob = json.dumps(frame)
        for secret in ("PACIENTE", "FICTICIA", "Registrar"):
            assert secret not in blob

    def test_labels_do_not_move_her_aim(self, tmp_path):
        """Frame identity is the tappable structure. If a label changed it, the
        screen would look like it moved every time a countdown ticked."""
        assert (feed.frame_id(feed.elements(self.ALERT, label=True))
                == feed.frame_id(feed.elements(self.ALERT)))


class TestFrameId:
    """Identity of a screen for aiming purposes."""

    def test_the_same_structure_is_the_same_frame(self):
        els = feed.elements(TestElements.XML)
        assert feed.frame_id(els) == feed.frame_id(feed.elements(TestElements.XML))

    def test_a_moved_target_changes_the_frame(self):
        els = feed.elements(TestElements.XML)
        moved = [dict(e) for e in els]
        moved[0]["b"] = [19, 900, 731, 960]
        assert feed.frame_id(moved) != feed.frame_id(els)

    def test_cosmetic_change_does_not_invalidate_her_aim(self):
        """A clock ticking repaints the screen without moving anything tappable.
        Invalidating an aim for that would make the control unusable on any
        screen with a timer, which includes every visit screen in this app."""
        els = feed.elements(TestElements.XML)
        same = [dict(e) for e in els]
        for e in same:
            e["focused"] = not e["focused"]
            e["has_text"] = not e["has_text"]
        assert feed.frame_id(same) == feed.frame_id(els)

    def test_a_vanished_target_changes_the_frame(self):
        els = feed.elements(TestElements.XML)
        assert feed.frame_id(els[1:]) != feed.frame_id(els)


class TestTap:
    """Tapping through the mirror.

    Every test here is a refusal. Landing a tap correctly is the easy half; the
    reason this exists is that a tap aimed at a screen that has since moved must
    not land at all.
    """

    XML = TestElements.XML

    def _frame(self, tmp_path, els=None, age=0.0):
        import datetime as dt
        path = tmp_path / "frame.json"
        path.write_text(json.dumps({
            "at": (dt.datetime.now() - dt.timedelta(seconds=age)).isoformat(),
            "elements": self._current() if els is None else els,
        }), encoding="utf-8")
        return path

    def _current(self):
        return feed.elements(self.XML)

    def _fid(self):
        return feed.frame_id(self._current())

    def _button(self):
        return next(e for e in self._current() if e["rid"] == "btn_clock_in")

    def test_taps_the_centre_of_the_element(self, tmp_path):
        with patch.object(feed, "_adb") as adb:
            adb.return_value.returncode = 0
            out = feed.tap(self._fid(), self._button(),
                           frame_path=self._frame(tmp_path))
        sent = adb.call_args.args[0]
        assert sent[:3] == ["shell", "input", "tap"]
        assert sent[3:] == ["375", "774"]          # centre of [19,744][731,804]
        assert out["tapped"]["rid"] == "btn_clock_in"

    def test_refuses_when_the_target_is_gone(self, tmp_path):
        """The whole point. A blind coordinate would land on whatever occupies
        that spot now, which on this app can be the verification prompt."""
        moved_on = feed.elements(
            self.XML.replace('bounds="[19,744][731,804]"',
                             'bounds="[19,900][731,960]"'))
        with patch.object(feed, "_adb") as adb:
            with pytest.raises(feed.StaleAim):
                feed.tap(self._fid(), self._button(),
                         frame_path=self._frame(tmp_path, moved_on))
            adb.assert_not_called()

    def test_refuses_an_element_that_was_never_on_screen(self, tmp_path):
        """A matching frame id proves the screen is unchanged. It does not prove
        the posted rectangle was ever part of it — and a tap at arbitrary
        coordinates is precisely what this is built not to be."""
        forged = {"rid": "btn_clock_in", "cls": "Button", "b": [0, 0, 720, 1600]}
        with patch.object(feed, "_adb") as adb:
            with pytest.raises((feed.NotOnScreen, feed.StaleAim)):
                feed.tap(self._fid(), forged, frame_path=self._frame(tmp_path))
            adb.assert_not_called()

    def test_refuses_when_the_screen_cannot_be_read(self, tmp_path):
        with patch.object(feed, "_adb") as adb:
            with pytest.raises(feed.StaleAim):
                feed.tap(self._fid(), self._button(),
                         frame_path=tmp_path / "nothing.json")
            adb.assert_not_called()

    def test_an_element_matching_bounds_but_not_identity_is_refused(self, tmp_path):
        """Same rectangle, different widget — the layout was rebuilt underneath
        with something else in that spot."""
        impostor = dict(self._button())
        impostor["rid"] = "btn_cancel"
        with patch.object(feed, "_adb") as adb:
            with pytest.raises((feed.NotOnScreen, feed.StaleAim)):
                feed.tap(self._fid(), impostor, frame_path=self._frame(tmp_path))
            adb.assert_not_called()


class TestStableHierarchy:
    """The adb fallback path. uiautomator dump is not a snapshot.

    Measured on a static Settings screen, four consecutive dumps returned 20, 2,
    2 and 22 elements. Nothing in a result says which kind you got.
    """

    RICH = TestElements.XML
    PARTIAL = ('<node class="android.widget.View" resource-id="com.x:id/only"'
               ' clickable="true" bounds="[0,0][720,60]" />')

    def test_two_agreeing_dumps_are_trusted_immediately(self):
        with patch.object(feed, "read_hierarchy_via_adb", side_effect=[self.RICH, self.RICH]), \
             patch.object(feed.time, "sleep"):
            assert feed.read_stable_hierarchy() == self.RICH

    def test_the_richest_dump_wins_when_none_agree(self):
        """A partial capture is strictly a subset, so "most elements" is the
        least-wrong answer rather than an arbitrary tiebreak."""
        with patch.object(feed, "read_hierarchy_via_adb",
                          side_effect=[self.PARTIAL, self.RICH, self.PARTIAL,
                                       self.RICH, self.PARTIAL]), \
             patch.object(feed.time, "sleep"):
            assert feed.read_stable_hierarchy() == self.RICH

    def test_all_reads_failing_gives_none(self):
        with patch.object(feed, "read_hierarchy_via_adb", return_value=None), \
             patch.object(feed.time, "sleep"):
            assert feed.read_stable_hierarchy() is None

    def test_an_empty_dump_is_never_treated_as_agreement(self):
        """Two identical empty reads agree on nothing useful, and accepting them
        would publish "this screen has no targets" for a screen full of them."""
        with patch.object(feed, "read_hierarchy_via_adb", side_effect=["", "", self.RICH, self.RICH]), \
             patch.object(feed.time, "sleep"):
            assert feed.read_stable_hierarchy() == self.RICH


class TestCompression:
    """The wire size decides whether the portal gets used at all.

    Measured on the Pi: 121 KB for a light screen, 925 KB for a busy one, both
    landing near 32 KB once downscaled. She reads this on cellular between
    floors; a portal that stalls is one she walks past.
    """

    def _png(self, w=720, h=1600):
        """Noisy on purpose. A flat colour is a degenerate case: PNG stores it in
        a few bytes and any JPEG of it is larger, so the size assertion would be
        measuring the fixture rather than the compression."""
        from PIL import Image
        import io, random
        rng = random.Random(0)
        img = Image.new("RGB", (w, h))
        img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                     for _ in range(w * h)])
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def test_a_frame_gets_much_smaller(self):
        raw = self._png()
        assert len(feed.compress(raw)) < len(raw)

    def test_it_is_downscaled_to_the_mirror_width(self):
        from PIL import Image
        import io
        out = Image.open(io.BytesIO(feed.compress(self._png())))
        assert out.width == feed.MIRROR_WIDTH

    def test_aspect_ratio_survives(self):
        from PIL import Image
        import io
        out = Image.open(io.BytesIO(feed.compress(self._png(720, 1600))))
        assert abs(out.height / out.width - 1600 / 720) < 0.01

    def test_undecodable_input_comes_back_unchanged(self):
        """A large picture is a slow portal; no picture is one she cannot use."""
        junk = b"not an image at all"
        assert feed.compress(junk) == junk


class TestHierarchySource:
    """Appium first for speed, adb so the overlay never simply vanishes."""

    def test_appium_is_preferred(self):
        from apt_log import resident
        with patch.object(resident, "page_source", return_value="<node/>"), \
             patch.object(feed, "read_stable_hierarchy") as fallback:
            assert feed.read_hierarchy() == "<node/>"
            fallback.assert_not_called()

    def test_it_falls_back_rather_than_going_blank(self):
        """Session creation fails on the first attempt after an idle spell and
        has been seen to hang past 90s. An overlay that disappears because a
        session would not open is not acceptable; the fallback's 12.7s costs a
        staler overlay, not a frozen picture, now that it runs on its own thread.
        """
        from apt_log import resident
        with patch.object(resident, "page_source", return_value=None), \
             patch.object(feed, "read_stable_hierarchy", return_value="<node b=2/>"):
            assert feed.read_hierarchy() == "<node b=2/>"


class TestHierarchyRegression:
    """A read can succeed and come back structurally empty.

    Observed on the launcher: 16 targets, 16, then 0, 0, 0, with the screen
    unchanged throughout. Guarding only against None let each empty result
    overwrite a good one, so the overlay kept blinking out under her hand.
    """

    RICH = TestElements.XML
    EMPTY = "<hierarchy/>"

    def test_an_empty_read_does_not_erase_a_good_overlay(self):
        h = feed._Hierarchy(None, every=10)
        h._xml, h._focus = self.RICH, "com.x/.Home"
        assert h._accept(self.EMPTY, "com.x/.Home") is False

    def test_an_empty_read_is_believed_when_the_screen_changed(self):
        """Some screens really have nothing to tap, and refusing to ever show
        that would be its own lie."""
        h = feed._Hierarchy(None, every=10)
        h._xml, h._focus = self.RICH, "com.x/.Home"
        assert h._accept(self.EMPTY, "com.x/.Somewhere") is True

    def test_a_good_read_always_wins(self):
        h = feed._Hierarchy(None, every=10)
        h._xml, h._focus = self.EMPTY, "com.x/.Home"
        assert h._accept(self.RICH, "com.x/.Home") is True

    def test_empty_replaces_empty_so_it_cannot_wedge(self):
        h = feed._Hierarchy(None, every=10)
        h._xml, h._focus = self.EMPTY, "com.x/.Home"
        assert h._accept(self.EMPTY, "com.x/.Home") is True


class TestBothXmlDialects:
    """uiautomator dump and Appium's page_source do not agree on the shape.

    `uiautomator dump`  ->  <node class="android.widget.Button" .../>
    Appium page_source  ->  <android.widget.Button class="..." .../>

    Matching only <node> parsed zero elements out of a perfectly good 19 KB
    Appium document. The read succeeded every time, so nothing looked broken
    except an overlay that was simply never there.
    """

    APPIUM = (
        '<hierarchy rotation="0">'
        '<android.widget.TextView index="0" package="com.android.launcher3"'
        ' class="android.widget.TextView" text="OfferUp" content-desc="OfferUp"'
        ' clickable="true" focused="false" selected="false"'
        ' bounds="[42,1180][162,1300]" />'
        '</hierarchy>'
    )

    def test_appium_dialect_parses(self):
        els = feed.elements(self.APPIUM)
        assert len(els) == 1
        assert els[0]["cls"] == "TextView"
        assert els[0]["b"] == [42, 1180, 162, 1300]

    def test_uiautomator_dialect_still_parses(self):
        assert len(feed.elements(TestElements.XML)) == 2

    def test_the_hierarchy_root_is_not_a_target(self):
        assert all(e["cls"] != "hierarchy" for e in feed.elements(self.APPIUM))

    def test_appium_text_still_never_escapes(self):
        """Appium puts text and content-desc on every node; the map must carry
        neither, or a patient name reaches a POST body by a new route."""
        import json
        blob = json.dumps(feed.elements(self.APPIUM))
        assert "OfferUp" not in blob


class TestRawScreencap:
    """The phone should not be compressing anything.

    Measured on the Pi: `screencap -p` costs 2,416 ms and sends 1.27 MB;
    `screencap` costs 649 ms and sends 4.61 MB. Four times the bytes and nearly
    four times faster, because a budget MediaTek CPU compresses far slower than
    USB moves data.
    """

    def _raw(self, w=8, h=4, header=16):
        import struct
        return (struct.pack("<III", w, h, 1) + b"\x00" * (header - 12)
                + bytes([90, 120, 150, 255]) * (w * h))

    def test_a_raw_framebuffer_becomes_a_jpeg(self):
        from PIL import Image
        out = feed.compress(self._raw())
        assert Image.open(io.BytesIO(out)).format == "JPEG"

    def test_the_older_twelve_byte_header_also_works(self):
        """The header grew by four bytes at some Android version; guessing wrong
        shears the image rather than failing, which is far worse to diagnose."""
        from PIL import Image
        assert Image.open(io.BytesIO(feed.compress(self._raw(header=12)))).format == "JPEG"

    def test_png_input_still_works(self):
        """Some paths still hand us a PNG, and a mirror that only understands one
        encoding breaks silently when the other shows up."""
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (16, 8), (10, 20, 30)).save(buf, "PNG")
        assert Image.open(io.BytesIO(feed.compress(buf.getvalue()))).format == "JPEG"

    def test_a_truncated_frame_is_refused_rather_than_sheared(self):
        import pytest as _p
        with _p.raises(ValueError):
            feed._decode(self._raw()[:-40])


class TestTapUsesThePublishedFrame:
    """A tap is checked against the overlay she was looking at.

    Reading the device again was wrong twice. Slow: the tap runs in the UI
    process, which had to open a second Appium session while the feed held the
    only one UiAutomator2 allows — 14 seconds of contention per tap. And wrong:
    a fresh read says what is on the screen *now*, when what makes a tap safe is
    that it was on the screen *she saw*.
    """

    def test_it_reads_no_device_at_all(self, tmp_path):
        import datetime as dt
        els = feed.elements(TestElements.XML)
        btn = next(e for e in els if e["rid"] == "btn_clock_in")
        path = tmp_path / "frame.json"
        path.write_text(json.dumps({"at": dt.datetime.now().isoformat(),
                                    "elements": els}), encoding="utf-8")
        with patch.object(feed, "read_hierarchy") as appium, \
             patch.object(feed, "read_stable_hierarchy") as adb_only, \
             patch.object(feed, "_adb") as adb:
            adb.return_value.returncode = 0
            feed.tap(feed.frame_id(els), btn, frame_path=path)
        appium.assert_not_called()
        adb_only.assert_not_called()

    def test_a_frame_nobody_is_updating_stops_being_tappable(self, tmp_path):
        """An overlay nothing is refreshing is one she cannot trust either."""
        import datetime as dt
        els = feed.elements(TestElements.XML)
        btn = next(e for e in els if e["rid"] == "btn_clock_in")
        path = tmp_path / "frame.json"
        old = dt.datetime.now() - dt.timedelta(seconds=feed.TAP_FRAME_MAX_AGE + 5)
        path.write_text(json.dumps({"at": old.isoformat(), "elements": els}),
                        encoding="utf-8")
        with patch.object(feed, "_adb") as adb:
            with pytest.raises(feed.StaleAim, match="old"):
                feed.tap(feed.frame_id(els), btn, frame_path=path)
            adb.assert_not_called()


class TestScreenDocument:
    """The wireframe's render feed, and its disclosure policy.

    Two files, two policies. frame.json is the loggable one and its rules are
    unchanged. screen.json carries the screen's words because components need
    them as strings where a photograph carries them as pixels — it sits beside
    last-screen.jpg, which holds the same words, and is treated with the same
    care: never logged, and typed-field contents withheld everywhere.
    """

    XML = (
        '<node class="android.widget.TextView" resource-id="com.x:id/heading"'
        ' text="Visitas de hoy" clickable="false" bounds="[0,100][720,160]" />'
        '<node class="android.widget.Button" resource-id="com.x:id/btn_go"'
        ' text="Entrar" clickable="true" bounds="[19,744][731,804]" />'
        '<node class="android.widget.EditText" resource-id="com.x:id/txt_pin"'
        ' text="4321" clickable="true" bounds="[40,600][680,660]" />'
        '<node class="android.widget.EditText" resource-id="com.x:id/txt_note"'
        ' text="typed words" clickable="false" bounds="[40,300][680,360]" />'
        '<node class="android.widget.CheckBox" resource-id="com.x:id/chk"'
        ' text="" clickable="true" checked="true" bounds="[40,400][100,460]" />'
    )

    def _write(self, tmp_path, hierarchy=None, reason=feed.CAPTURED, png=PNG):
        shot = tmp_path / "screen.png"
        with patch.object(feed, "capture", return_value=(png, HOME, reason)), \
             patch.object(feed, "screen_size", return_value=[720, 1600]), \
             patch.object(feed.mirror_mod, "publish"):
            feed.write_frame(shot, hierarchy=hierarchy or self.XML)
        return (json.loads((tmp_path / "frame.json").read_text()),
                json.loads((tmp_path / "screen.json").read_text()))

    def test_statics_carry_the_screens_words(self, tmp_path):
        _, screen = self._write(tmp_path)
        assert any(s["txt"] == "Visitas de hoy" for s in screen["statics"])

    def test_buttons_carry_their_labels(self, tmp_path):
        _, screen = self._write(tmp_path)
        btn = next(e for e in screen["elements"] if e["rid"] == "btn_go")
        assert btn["txt"] == "Entrar"

    def test_typed_text_is_withheld_from_both_files_everywhere(self, tmp_path):
        """An EditText's text is whatever was typed into it — a PIN here. It
        must not appear clickable or not, captured screen or not."""
        frame, screen = self._write(tmp_path)
        blob = json.dumps(frame) + json.dumps(screen)
        assert "4321" not in blob
        assert "typed words" not in blob

    def test_frame_json_stays_textless_on_ordinary_screens(self, tmp_path):
        """The loggable file's policy did not move."""
        frame, _ = self._write(tmp_path)
        assert all("txt" not in e for e in frame["elements"])
        assert "statics" not in frame

    def test_checked_state_is_carried(self, tmp_path):
        """A wireframe switch has to know which way it is thrown — this is
        also what a check-all-tasks macro will read back one day."""
        _, screen = self._write(tmp_path)
        box = next(e for e in screen["elements"] if e["rid"] == "chk")
        assert box["checked"] is True

    def test_checked_does_not_move_her_aim(self):
        before = self.XML
        after = self.XML.replace('checked="true"', 'checked="false"')
        assert (feed.frame_id(feed.elements(before))
                == feed.frame_id(feed.elements(after)))

    def test_statics_are_bounded(self):
        node = ('<node class="android.widget.TextView" text="x{i}"'
                ' clickable="false" bounds="[0,{i}][10,{j}]" />')
        xml = "".join(node.replace("{i}", str(i)).replace("{j}", str(i + 1))
                      for i in range(0, 400))
        assert len(feed.statics(xml)) == feed.MAX_STATICS


class TestHierarchyPoke:
    def test_a_poke_cuts_the_wait_short(self, tmp_path):
        """After a tap she is certainly watching; the interval's job is to be
        interrupted then."""
        poke = tmp_path / "poke"
        watcher = feed._Hierarchy(None, every=5.0, poke_path=poke)
        poke.write_text("1")
        start = time.monotonic()
        watcher._wait()
        assert time.monotonic() - start < 1.0

    def test_one_poke_is_one_interruption(self, tmp_path):
        poke = tmp_path / "poke"
        watcher = feed._Hierarchy(None, every=0.6, poke_path=poke)
        poke.write_text("1")
        watcher._wait()                       # consumes the poke
        start = time.monotonic()
        watcher._wait()                       # must wait the interval out
        assert time.monotonic() - start >= 0.5

    def test_a_tap_pokes_the_watcher(self, tmp_path):
        frame = {"at": __import__("datetime").datetime.now().isoformat(),
                 "elements": [{"rid": "go", "cls": "Button", "b": [0, 0, 10, 10]}]}
        (tmp_path / "frame.json").write_text(json.dumps(frame))
        with patch.object(feed, "_adb") as adb, \
             patch("apt_log.ui.state.STATE_DIR", tmp_path):
            adb.return_value.returncode = 0
            feed.tap("f", {"rid": "go", "cls": "Button", "b": [0, 0, 10, 10]},
                     frame_path=tmp_path / "frame.json")
        assert (tmp_path / feed.POKE_NAME).exists()


class TestEntitiesAreUnescaped:
    def test_a_line_break_entity_does_not_render_literally(self):
        """Seen on the real visit screen: a title carrying "&#10;" as text."""
        xml = ('<node class="android.widget.TextView" resource-id="com.x:id/hdr"'
               ' text="Detalle de Visita &#10;PACIENTE FICTICIA" clickable="false"'
               ' bounds="[0,0][720,120]" />')
        assert feed.statics(xml)[0]["txt"] == "Detalle de Visita PACIENTE FICTICIA"

    def test_ampersands_come_back_as_themselves(self):
        xml = ('<node class="android.widget.Button" resource-id="com.x:id/b"'
               ' text="Care &amp; Support" clickable="true"'
               ' bounds="[0,0][300,80]" />')
        el = feed.elements(xml, label=True)[0]
        assert el["txt"] == "Care & Support"


class TestScreenDocumentKnowsItsApp:
    def test_the_foreground_package_is_published(self, tmp_path):
        """So a launcher tile can be a switch instead of a ceremony: pressing
        the tile of the app already in front must not run its sign-in walk."""
        shot = tmp_path / "screen.png"
        with patch.object(feed, "capture", return_value=(PNG, HOME, feed.CAPTURED)), \
             patch.object(feed, "screen_size", return_value=[720, 1600]), \
             patch.object(feed.mirror_mod, "publish"):
            feed.write_frame(shot, hierarchy="<node/>")
        screen = json.loads((tmp_path / "screen.json").read_text())
        assert screen["app"] == "com.hhaexchange.caregiver"


class TestSleepKeepsTheLastScreen:
    def test_a_dark_display_does_not_wipe_the_sketch(self):
        """The owner's ask, verbatim: it is nice to know what the last screen
        was before it went black, so a dark phone does not read as broken. A
        sleeping device returns a junk read with an empty focus, and the
        focus "changing" to nothing used to count as a new screen."""
        watcher = feed._Hierarchy(None, every=1.0)
        good = ('<node class="android.widget.Button" resource-id="c:id/go"'
                ' clickable="true" bounds="[0,0][100,50]" text="Entrar"/>')
        junk = '<node class="android.view.View" bounds="[0,0][720,1600]"/>'
        watcher._xml, watcher._focus = good, "com.x/.HomeActivity"
        assert watcher._accept(junk, "") is False
        assert watcher._accept(junk, "com.other/.RealScreen") is True


class TestSleepIsNotLive:
    """A sleeping phone keeps its focused window, so focus alone misses most
    sleeps — seen on the owner's phone as a green Live over a photograph of
    a black screen. The display's own state travels with the focus."""

    def test_window_state_reads_the_display_state(self):
        out = f"  mCurrentFocus=Window{{abc u0 {HOME}}}\n  mAwake=false\n"
        with patch.object(feed, "_adb") as adb:
            adb.return_value.stdout = out.encode()
            focus, awake, _ = feed.window_state()
        assert focus == HOME and awake is False

    def test_capture_refuses_while_the_display_is_off(self):
        """The focused app of a black screen is not what anyone is seeing."""
        with patch.object(feed, "window_state", return_value=(HOME, False, "")):
            png, focus, reason = feed.capture()
        assert png is None and reason == feed.NO_FOCUS

    def test_the_kept_sketch_survives_a_sleeping_read(self):
        """The junk a sleeping device returns must not wipe the memory the
        page shows as 'last on screen before it turned off'."""
        w = feed._Hierarchy(None, 1.0)
        good = '<node clickable="true" bounds="[0,0][10,10]"/>'
        assert w._accept(good, HOME, awake=False) is False


class TestAPopupIsStillTheAppUnderneath:
    """Reported from the field: tapping inMyTeam's agency filter left the app
    "stuck on syncing".

    A dropdown is its own window and Android TITLES that window rather than
    naming a package, so the focus read came back with the literal string
    "Window". Every check downstream then took the phone to be somewhere that
    is not a care app, and the containment watchdog did exactly what it is for
    — it brought inMyTeam back. Every twenty seconds, for as long as the
    dropdown stayed open, each return resyncing the app. The feed's own log
    said so plainly once anyone looked:

        foreground is Window — returning to com.inmyteam.inmyteam

    The answer was in the same dumpsys read the whole time: mFocusedApp names
    the activity that owns the popup. Same distinction the permission-dialog
    fix turns on — a surface the app in front raised is not the phone
    wandering off.
    """

    # Copied from the live phone with the agency filter open.
    POPUP = (
        "  mCurrentFocus=Window{3d46066 u0 Pop-Up Window}\n"
        "  mFocusedApp=ActivityRecord{a816218 u0 "
        "com.inmyteam.inmyteam/.view.activities.MainActivity} t958}\n"
        "  mAwake=true\n"
    )

    def _read(self, out):
        with patch.object(feed, "_adb") as adb:
            adb.return_value.stdout = out.encode()
            return feed.window_state()

    def test_the_popups_owner_is_reported_instead_of_the_window(self):
        focus, awake, anr = self._read(self.POPUP)
        assert focus.split("/")[0] == "com.inmyteam.inmyteam"
        assert awake is True and not anr

    def test_the_watchdog_leaves_a_dropdown_alone(self):
        """The whole point. With the owner reported, the package is a care app
        and the containment check returns before it can bounce anything."""
        focus, _, _ = self._read(self.POPUP)
        assert focus.split("/")[0] in feed.CARE_APPS

    def test_a_real_focus_is_never_second_guessed(self):
        out = (f"  mCurrentFocus=Window{{abc u0 {HOME}}}\n"
               "  mFocusedApp=ActivityRecord{x u0 com.other/.Thing}\n"
               "  mAwake=true\n")
        focus, _, _ = self._read(out)
        assert focus == HOME

    def test_the_not_responding_dialog_still_wins(self):
        """Its window title ENDS IN the wedged package, so it is a bare token
        too — but it means the opposite of "the app is fine underneath", and
        reading it as the owner would wave a wedged app straight through."""
        out = ("  mCurrentFocus=Window{a u0 Application Not Responding: "
               "com.hhaexchange.mobile}\n"
               "  mFocusedApp=ActivityRecord{x u0 "
               "com.hhaexchange.mobile/.Home}\n  mAwake=true\n")
        _, _, anr = self._read(out)
        assert anr == "com.hhaexchange.mobile"

    def test_an_unreadable_owner_leaves_the_window_as_it_was(self):
        out = ("  mCurrentFocus=Window{3d46066 u0 Pop-Up Window}\n"
               "  mFocusedApp=null\n  mAwake=true\n")
        focus, _, _ = self._read(out)
        assert focus == "Window"

    @pytest.mark.parametrize("token, is_title", [
        ("Window", True),
        ("Pop-Up", True),
        ("com.inmyteam.inmyteam/.view.activities.MainActivity", False),
        ("com.hhaexchange.mobile", False),      # the ANR shape: has a dot
        ("", False),
    ])
    def test_what_counts_as_a_window_title(self, token, is_title):
        assert feed._is_a_window_title(token) is is_title


class TestSketchOwnership:
    """screen.json carries two apps on purpose: `app` is the focus of this
    moment, `h_app` is whose screen the elements were read under. During an
    app switch they disagree, and the page must say syncing rather than
    dress the old app's rows in the new app's name."""

    def _frame(self):
        import datetime as dt
        return {"id": "f1", "img": "", "at": dt.datetime.now().isoformat(),
                "size": [720, 1600], "notice": ""}

    def test_the_doc_says_whose_sketch_it_carries(self, tmp_path):
        target = tmp_path / "screen.json"
        feed.write_screen(
            target, self._frame(), "home", "", None, focus=HOME,
            hierarchy_focus="com.android.launcher3/.QuickstepLauncher")
        doc = json.loads(target.read_text())
        assert doc["app"] == "com.hhaexchange.caregiver"
        assert doc["h_app"] == "com.android.launcher3"

    def test_agreement_is_the_ordinary_case(self, tmp_path):
        target = tmp_path / "screen.json"
        feed.write_screen(target, self._frame(), "home", "", None,
                          focus=HOME, hierarchy_focus=HOME)
        doc = json.loads(target.read_text())
        assert doc["app"] == doc["h_app"]


class TestFocusNudge:
    """The window manager can strand the phone awake with a resumed app and
    no focused window — watched live for seventeen minutes of 'Syncing'.
    Persistent null focus while awake earns one gentle re-activation of the
    app already on top; blips and sleeping phones are left alone."""

    def _reset(self):
        feed._no_focus_since[0] = 0.0
        feed._last_nudge[0] = 0.0

    def test_a_persistent_null_focus_gets_one_nudge(self):
        self._reset()
        with patch.object(feed, "_refocus") as refocus, \
             patch.object(feed.time, "time", side_effect=[1000.0, 1070.0]):
            feed._watch_focus("", True)
            feed._watch_focus("", True)
        refocus.assert_called_once()

    def test_a_transition_blip_is_left_alone(self):
        self._reset()
        with patch.object(feed, "_refocus") as refocus, \
             patch.object(feed.time, "time", side_effect=[1000.0, 1005.0]):
            feed._watch_focus("", True)
            feed._watch_focus("", True)
        refocus.assert_not_called()

    def test_a_sleeping_phone_is_never_nudged(self):
        self._reset()
        with patch.object(feed, "_refocus") as refocus:
            for _ in range(5):
                feed._watch_focus("", False)
        refocus.assert_not_called()

    def test_focus_returning_resets_the_clock(self):
        self._reset()
        with patch.object(feed, "_refocus") as refocus, \
             patch.object(feed.time, "time",
                          side_effect=[1000.0, 1200.0]):
            feed._watch_focus("", True)
            feed._watch_focus(HOME, True)      # recovered on its own
            feed._watch_focus("", True)        # a fresh episode starts
        refocus.assert_not_called()


class TestWebviewHonesty:
    """A webview's accessibility tree underreports — the migration pitch
    renders a title and a banner that never appear in it, verified against
    the pixels. The document says so."""

    def test_a_webview_screen_is_flagged(self, tmp_path):
        import datetime as dt

        target = tmp_path / "screen.json"
        frame = {"id": "f1", "img": "", "at": dt.datetime.now().isoformat(),
                 "size": [720, 1600], "notice": ""}
        xml = ('<node class="android.webkit.WebView" clickable="false" '
               'bounds="[0,0][720,1600]"/>')
        feed.write_screen(target, frame, "unknown", "", xml, focus=HOME)
        assert json.loads(target.read_text())["webview"] is True

    def test_a_native_screen_is_not(self, tmp_path):
        import datetime as dt

        target = tmp_path / "screen.json"
        frame = {"id": "f1", "img": "", "at": dt.datetime.now().isoformat(),
                 "size": [720, 1600], "notice": ""}
        xml = ('<node class="android.widget.TextView" text="Hola" '
               'clickable="false" bounds="[0,0][100,50]"/>')
        feed.write_screen(target, frame, "home", "", xml, focus=HOME)
        assert json.loads(target.read_text())["webview"] is False


class TestFreshStitch:
    """When the whole-page document still counts as the page in front.

    The exact frame-id match proved brittle live: a ticking clock re-hashed
    the viewport the moment a walk finished, and a four-capture document
    was thrown away unpublished. The near-match accepts one volatile node;
    it must still refuse a different page, a different app, and old news.
    """

    ELS = [{"cls": "Button", "rid": "go", "txt": "Go", "b": [10, 100, 700, 160]},
           {"cls": "TextView", "rid": "", "txt": "Hola", "b": [10, 200, 700, 260]},
           {"cls": "TextView", "rid": "", "txt": "Visitas", "b": [10, 300, 700, 360]},
           {"cls": "TextView", "rid": "", "txt": "Pacientes", "b": [10, 400, 700, 460]},
           {"cls": "Button", "rid": "menu", "txt": "", "b": [10, 500, 700, 560]},
           {"cls": "TextView", "rid": "clock", "txt": "22:41", "b": [10, 600, 700, 660]}]

    def _write(self, tmp_path, name="aaaa", els=None, **extra):
        doc = {"step0": name, "app": "com.hhaexchange.caregiver",
               "elements": [dict(e, vb=e["b"], step=0)
                            for e in (els or self.ELS)],
               "statics": [], "steps": 2, **extra}
        root = tmp_path / feed.STITCH_DIRNAME
        root.mkdir(exist_ok=True)
        (root / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")
        return doc

    def test_the_exact_frame_id_is_the_fast_path(self, tmp_path):
        self._write(tmp_path)
        assert feed._fresh_stitch(tmp_path, "aaaa") is not None

    def test_one_volatile_node_does_not_void_the_walk(self, tmp_path):
        self._write(tmp_path)
        els = [dict(e) for e in self.ELS[:-1]] + \
              [dict(self.ELS[-1], txt="22:42")]        # the clock ticked
        got = feed._fresh_stitch(tmp_path, "bbbb", els=els,
                                 app="com.hhaexchange.caregiver")
        assert got is not None

    def test_a_different_page_of_the_same_app_is_refused(self, tmp_path):
        self._write(tmp_path)
        els = [{"cls": "Button", "rid": "other", "txt": "No", "b": [0, 0, 100, 50]},
               {"cls": "TextView", "rid": "", "txt": "Otra", "b": [0, 60, 100, 110]}]
        assert feed._fresh_stitch(tmp_path, "bbbb", els=els,
                                  app="com.hhaexchange.caregiver") is None

    def test_another_apps_screen_never_wears_the_document(self, tmp_path):
        self._write(tmp_path)
        assert feed._fresh_stitch(tmp_path, "bbbb", els=list(self.ELS),
                                  app="com.android.systemui") is None

    def test_without_the_viewport_no_near_match_is_attempted(self, tmp_path):
        self._write(tmp_path)
        assert feed._fresh_stitch(tmp_path, "bbbb") is None

    def test_a_more_folded_viewport_refuses_the_unfolded_scan(self, tmp_path):
        """Coming back from the visit details re-folds today's cards. Same
        page, different STATE — and dressing the folded phone in the
        expanded document had the owner tapping a card the phone no longer
        showed. More folded chevrons in front than in the scan means the
        page moved on."""
        chev = "\uf054"
        self._write(tmp_path, statics=[
            {"cls": "TextView", "txt": chev, "b": [694, 104, 700, 115],
             "vb": [694, 104, 700, 115], "step": 0}])   # one folded card
        els = [dict(e) for e in self.ELS[:-1]] + \
              [dict(self.ELS[-1], txt="22:42")]
        folded_two = [
            {"cls": "TextView", "txt": chev, "b": [694, 104, 700, 115]},
            {"cls": "TextView", "txt": chev, "b": [694, 304, 700, 315]}]
        assert feed._fresh_stitch(tmp_path, "bbbb", els=els,
                                  app="com.hhaexchange.caregiver",
                                  sts=folded_two) is None

    def test_an_equally_folded_viewport_still_matches(self, tmp_path):
        chev = "\uf054"
        self._write(tmp_path, statics=[
            {"cls": "TextView", "txt": chev, "b": [694, 104, 700, 115],
             "vb": [694, 104, 700, 115], "step": 0}])
        els = [dict(e) for e in self.ELS[:-1]] + \
              [dict(self.ELS[-1], txt="22:42")]
        same = [{"cls": "TextView", "txt": chev, "b": [694, 104, 700, 115]}]
        assert feed._fresh_stitch(tmp_path, "bbbb", els=els,
                                  app="com.hhaexchange.caregiver",
                                  sts=same) is not None

    def test_a_structural_twin_with_different_words_is_refused(self, tmp_path):
        """inMyTeam builds every page from the same anonymous containers,
        so element identity alone let the patients tab wear the
        dashboard's scan and the Tomorrow list wear Today's. The words
        are the page."""
        self._write(tmp_path, statics=[
            {"cls": "TextView", "txt": "Today Visits",
             "b": [30, 70, 200, 90], "vb": [30, 70, 200, 90], "step": 0},
            {"cls": "TextView", "txt": "MONDAY, AUGUST 17",
             "b": [200, 300, 520, 320], "vb": [200, 300, 520, 320],
             "step": 0},
            {"cls": "TextView", "txt": "Carmen Villalon",
             "b": [56, 400, 300, 420], "vb": [56, 400, 300, 420],
             "step": 0}])
        els = [dict(e) for e in self.ELS[:-1]] + \
              [dict(self.ELS[-1], txt="22:42")]
        other_words = [
            {"cls": "TextView", "txt": "Tomorrow Visits", "b": [30, 70, 200, 90]},
            {"cls": "TextView", "txt": "TUESDAY, AUGUST 18",
             "b": [200, 300, 520, 320]},
            {"cls": "TextView", "txt": "Carmen Villalon", "b": [56, 400, 300, 420]}]
        assert feed._fresh_stitch(tmp_path, "bbbb", els=els,
                                  app="com.hhaexchange.caregiver",
                                  sts=other_words) is None
        same_words = [dict(s) for s in other_words]
        same_words[0]["txt"] = "Today Visits"
        same_words[1]["txt"] = "MONDAY, AUGUST 17"
        assert feed._fresh_stitch(tmp_path, "bbbb", els=els,
                                  app="com.hhaexchange.caregiver",
                                  sts=same_words) is not None

    def test_a_wide_open_chevron_is_not_a_fold(self, tmp_path):
        """The open state draws the same glyph rotated — wider than tall.
        An expanded viewport must not read as folded."""
        chev = "\uf054"
        self._write(tmp_path, statics=[])
        els = [dict(e) for e in self.ELS[:-1]] + \
              [dict(self.ELS[-1], txt="22:42")]
        wide = [{"cls": "TextView", "txt": chev, "b": [692, 106, 703, 112]}]
        assert feed._fresh_stitch(tmp_path, "bbbb", els=els,
                                  app="com.hhaexchange.caregiver",
                                  sts=wide) is not None

    def test_an_old_document_is_not_trusted_even_on_exact_match(self, tmp_path):
        import os
        self._write(tmp_path)
        old = time.time() - feed.STITCH_MAX_AGE - 5
        os.utime(tmp_path / feed.STITCH_DIRNAME / "aaaa.json", (old, old))
        assert feed._fresh_stitch(tmp_path, "aaaa") is None

    def test_switching_apps_does_not_lose_the_other_pages_scan(self, tmp_path):
        """One file per scanned page: another app's page being scanned must
        not throw this one's away — seen live as a page re-scanning itself
        on every switch back."""
        self._write(tmp_path)                     # this page, scanned earlier
        other = [{"cls": "View", "rid": "", "txt": "Otra app",
                  "b": [0, 0, 720, 100]}]
        self._write(tmp_path, name="bbbb", els=other,
                    app="com.hhaexchange.uma")    # the app she switched to
        assert feed._fresh_stitch(tmp_path, "aaaa") is not None
        got = feed._fresh_stitch(tmp_path, "zzzz", els=list(self.ELS),
                                 app="com.hhaexchange.caregiver")
        assert got is not None and got["step0"] == "aaaa"


class TestCanvasNeverCached:
    """A signature screen is never dressed from the stitch cache. The
    canvas overlay lives inside the same activity as the page beneath it
    and shares most of that page's tree, so the near-match served the
    task list's stitched walk over the live canvas — no clear button, no
    save, no box to sign (seen live, first field test)."""

    def test_a_signature_canvas_is_recognised(self):
        for rid in ("gesturePatientSignature", "firma_view", "sign_pad",
                    "signpad", "layout_tab_content_signature"):
            xml = f'<node resource-id="a:id/{rid}" bounds="[0,0][720,650]"/>'
            assert feed._has_canvas(xml) is True

    def test_a_canvas_class_counts_too(self):
        xml = ('<node class="com.app.SignatureView" resource-id="" '
               'bounds="[0,0][720,650]"/>')
        assert feed._has_canvas(xml) is True

    def test_a_navigation_drawer_is_not_a_canvas(self):
        xml = ('<node class="androidx.drawerlayout.widget.DrawerLayout" '
               'resource-id="a:id/drawer_layout" bounds="[0,0][720,1600]"/>')
        assert feed._has_canvas(xml) is False

    def test_an_ordinary_page_is_not_a_canvas(self):
        assert feed._has_canvas(None) is False
        assert feed._has_canvas(
            '<node resource-id="a:id/btn_clock_out" text="Registrar" '
            'bounds="[0,0][720,60]"/>') is False


class TestHierarchyPackage:
    """The tree's own package attributes outrank the focus recorded at
    read time: through an app switch the window manager names the new app
    while the accessibility tree still serves the old one's nodes — seen
    live as the legacy home screen published under HHAeXchange+'s name."""

    def test_the_trees_majority_names_the_app(self):
        xml = ('<node package="com.hhaexchange.caregiver"/>' * 5
               + '<node package="com.android.systemui"/>' * 9)
        assert feed.hierarchy_package(xml) == "com.hhaexchange.caregiver"

    def test_no_tree_is_no_answer(self):
        assert feed.hierarchy_package(None) == ""
        assert feed.hierarchy_package("") == ""

    def test_the_screen_document_prefers_the_trees_answer(self, tmp_path):
        xml = '<node package="com.hhaexchange.caregiver" bounds="[0,0][720,100]"/>'
        frame = {"id": "x", "img": "", "at": "now", "size": [720, 1600]}
        feed.write_screen(tmp_path / "screen.json", frame, "home", "", xml,
                          focus="com.hhaexchange.uma/.HomeActivity",
                          hierarchy_at=1.0,
                          hierarchy_focus="com.hhaexchange.uma/.HomeActivity")
        doc = json.loads((tmp_path / "screen.json").read_text())
        assert doc["app"] == "com.hhaexchange.uma"
        assert doc["h_app"] == "com.hhaexchange.caregiver"


class TestTypeInto:
    """Typing a short code into a field, under tap's refuse-if-moved
    contract. Exists for the OTP moment: a verification code lands on a
    family member's phone and someone types it in from the portal."""

    FIELD = {"cls": "android.widget.EditText", "rid": "otp",
             "b": [11, 1051, 709, 1085], "step": 0}

    def _publish(self, tmp_path, els):
        from datetime import datetime
        frame = {"id": "f1", "at": datetime.now().isoformat(),
                 "elements": els}
        p = tmp_path / "frame.json"
        p.write_text(json.dumps(frame), encoding="utf-8")
        return p

    def test_the_code_is_typed_after_focusing_the_field(self, tmp_path):
        p = self._publish(tmp_path, [dict(self.FIELD)])
        calls = []
        with patch.object(feed, "_adb",
                          side_effect=lambda a, *_, **__: calls.append(a)
                          or MagicMock(returncode=0)):
            out = feed.type_into("f1", dict(self.FIELD), "123456",
                                 frame_path=p)
        assert out["typed"]["chars"] == 6
        # Another test's watcher thread may interleave its own adb call
        # into the focus-settle pause; only the input calls are ours.
        inputs = [c for c in calls if c[:2] == ["shell", "input"]]
        assert inputs[0][:3] == ["shell", "input", "tap"]
        assert inputs[1] == ["shell", "input", "text", "123456"]

    def test_a_moved_field_is_refused(self, tmp_path):
        p = self._publish(tmp_path, [dict(self.FIELD,
                                          b=[11, 900, 709, 934])])
        with pytest.raises(feed.StaleAim):
            feed.type_into("f1", dict(self.FIELD), "123456", frame_path=p)

    def test_only_fields_take_text(self, tmp_path):
        row = dict(self.FIELD, cls="android.view.View")
        p = self._publish(tmp_path, [row])
        with pytest.raises(feed.StaleAim):
            feed.type_into("f1", dict(row), "123456", frame_path=p)

    def test_nothing_a_shell_reads_as_syntax_travels(self, tmp_path):
        p = self._publish(tmp_path, [dict(self.FIELD)])
        for bad in ("", "semi;colon", "x" * 33, "$(rm)", "a`b`", "a|b",
                    "a&b", "a>b", 'a"b', "a\\b", "a\nb", "a*b", " lead",
                    "trail ", "-dash"):
            with pytest.raises(ValueError):
                feed.type_into("f1", dict(self.FIELD), bad, frame_path=p)

    # ------------------------------------------------------------ and a NAME
    def test_a_name_with_a_space_in_it_travels(self, tmp_path):
        """The patients tab searches by name, and a name has a space in it.
        The old rule stripped everything but letters and digits, which turned
        "Rojas Batista" into "RojasBatista" — a search that matches nothing.
        """
        p = self._publish(tmp_path, [dict(self.FIELD)])
        calls = []
        with patch.object(feed, "_adb",
                          side_effect=lambda a, *_, **__: calls.append(a)
                          or MagicMock(returncode=0)):
            feed.type_into("f1", dict(self.FIELD), "Rojas Batista",
                           frame_path=p)
        inputs = [c for c in calls if c[:2] == ["shell", "input"]]
        assert inputs[1][:3] == ["shell", "input", "text"]

    def test_and_it_arrives_as_ONE_word_to_the_phones_shell(self, tmp_path):
        """`adb shell` has no argv — it joins what it is given and hands the
        line to sh on the phone. Unquoted, "Rojas Batista" arrives there as
        two arguments and only the first is typed."""
        p = self._publish(tmp_path, [dict(self.FIELD)])
        calls = []
        with patch.object(feed, "_adb",
                          side_effect=lambda a, *_, **__: calls.append(a)
                          or MagicMock(returncode=0)):
            feed.type_into("f1", dict(self.FIELD), "Rojas Batista",
                           frame_path=p)
        import shlex

        sent = [c for c in calls if c[:2] == ["shell", "input"]][1][3]
        assert " " not in shlex.split("input text " + sent)[2] or \
            shlex.split("input text " + sent)[2] == "Rojas Batista"
        assert len(shlex.split("input text " + sent)) == 3

    def test_a_name_that_carries_marks_travels(self, tmp_path):
        p = self._publish(tmp_path, [dict(self.FIELD)])
        with patch.object(feed, "_adb",
                          side_effect=lambda *a, **k: MagicMock(returncode=0)):
            for good in ("María Ángeles", "O'Brien", "Ana-Lucía", "St. John",
                         "123456"):
                feed.type_into("f1", dict(self.FIELD), good, frame_path=p)

    def test_an_apostrophe_cannot_break_out_of_the_quoting(self, tmp_path):
        """The one allowed character that quoting has to actually handle."""
        import shlex

        p = self._publish(tmp_path, [dict(self.FIELD)])
        calls = []
        with patch.object(feed, "_adb",
                          side_effect=lambda a, *_, **__: calls.append(a)
                          or MagicMock(returncode=0)):
            feed.type_into("f1", dict(self.FIELD), "O'Brien", frame_path=p)
        sent = [c for c in calls if c[:2] == ["shell", "input"]][1][3]
        assert shlex.split("input text " + sent) == ["input", "text",
                                                     "O'Brien"]

    def test_the_value_is_still_capped_short(self, tmp_path):
        """A field takes a code or a name, never a message."""
        p = self._publish(tmp_path, [dict(self.FIELD)])
        with patch.object(feed, "_adb",
                          side_effect=lambda *a, **k: MagicMock(returncode=0)):
            feed.type_into("f1", dict(self.FIELD), "a" * 32, frame_path=p)
        with pytest.raises(ValueError):
            feed.type_into("f1", dict(self.FIELD), "a" * 33, frame_path=p)


class TestNotificationShade:
    """The shade only ever arrives by accident on a phone nobody holds,
    and its content is the owner's private notifications. The watcher
    collapses it; the parsers never publish its nodes."""

    SHADE_NODE = ('<node class="android.widget.TextView" text="Gmail" '
                  'package="com.android.systemui" clickable="true" '
                  'bounds="[0,300][720,360]" resource-id='
                  '"com.android.systemui:id/qs_tile"/>')
    APP_NODE = ('<node class="android.widget.TextView" text="Hola" '
                'package="com.hhaexchange.uma" clickable="true" '
                'bounds="[0,500][720,560]"/>')

    def test_shade_nodes_never_publish(self):
        xml = self.SHADE_NODE + self.APP_NODE
        els = feed.elements(xml, label=True)
        assert len(els) == 1
        shade_static = self.SHADE_NODE.replace('clickable="true"',
                                               'clickable="false"')
        sts = feed.statics(shade_static + self.APP_NODE)
        assert all("Gmail" not in (s.get("txt") or "") for s in sts)

    def test_an_open_shade_is_collapsed_once(self):
        feed._last_collapse[0] = 0.0
        calls = []
        # Real bytes on stdout: the collapse CHECKS whether the shade went,
        # because the command reports success either way on the live phone.
        gone = b"  mCurrentFocus=Window{a u0 com.inmyteam.inmyteam/.Main}\n"

        with patch.object(feed, "_adb",
                          side_effect=lambda a, *_, **__: calls.append(a)
                          or MagicMock(returncode=0, stdout=gone)):
            feed._watch_shade('<node resource-id="com.android.systemui:id/'
                              'quick_qs_panel"/>')
            feed._watch_shade('<node resource-id="com.android.systemui:id/'
                              'quick_qs_panel"/>')   # within cooldown
        collapses = [c for c in calls if c[:3] == ["shell", "cmd", "statusbar"]]
        assert len(collapses) == 1

    def test_an_ordinary_screen_is_left_alone(self):
        feed._last_collapse[0] = 0.0
        with patch.object(feed, "_adb") as adb:
            feed._watch_shade(self.APP_NODE)
        adb.assert_not_called()


class TestContainment:
    """No command from the portal may leave the phone anywhere but the
    four care apps. A verified tap can still fire an intent — the dialer,
    Maps, Android settings — so the watcher brings the last care app back
    once an unsanctioned package holds the foreground past the dwell."""

    def _reset(self):
        feed._out_since[0] = 0.0
        feed._last_return[0] = 0.0
        feed._last_care_app[0] = ""

    def _run(self, focuses, dt=3.0, status_state="idle"):
        """Feed a sequence of focus sightings dt seconds apart; return the
        monkey relaunches issued."""
        self._reset()
        calls = []
        clock = [1000.0]

        class S:  # macro status stub
            state = status_state

        with patch.object(feed.time, "time", side_effect=lambda: clock[0]), \
             patch.object(feed, "_adb",
                          side_effect=lambda a, *_, **__: calls.append(a)
                          or MagicMock(returncode=0)):
            import apt_log.macros as macros_mod
            with patch.object(macros_mod, "read_status", return_value=S()):
                for f in focuses:
                    feed._watch_containment(f)
                    clock[0] += dt
        return [c for c in calls if c[:2] == ["shell", "monkey"]]

    CARE = "com.hhaexchange.uma/com.hhaexchange.carehub.ui.HomeActivity"
    # A stray app is one nothing here can have asked for. Android's Settings
    # used to play this part and no longer can: the control centre has a
    # button that opens it deliberately (see TestSettingsIsSomewhereItWasSent).
    STRAY = "com.android.vending/.AssetBrowserActivity"
    SETTINGS = "com.android.settings/.Settings"

    def test_a_stray_screen_is_brought_back(self):
        out = self._run([self.CARE, self.STRAY, self.STRAY, self.STRAY])
        assert len(out) == 1
        assert out[0][2:4] == ["monkey", "-p"] or "-p" in out[0]
        assert "com.hhaexchange.uma" in out[0]

    def test_a_momentary_crossing_is_left_alone(self):
        """One sighting inside the dwell is a transition, not a departure."""
        out = self._run([self.CARE, self.STRAY, self.CARE], dt=2.0)
        assert out == []

    def test_settings_is_somewhere_it_was_sent_not_somewhere_it_wandered(self):
        """The control centre offers a Phone settings button. Without this the
        watchdog undid it five seconds later, which is a button that appears
        to work and does not."""
        out = self._run([self.CARE, self.SETTINGS, self.SETTINGS,
                         self.SETTINGS])
        assert out == []

    def test_chromes_custom_tab_is_part_of_the_flow(self):
        chrome = ("com.android.chrome/org.chromium.chrome.browser."
                  "customtabs.CustomTabActivity")
        out = self._run([self.CARE, chrome, chrome, chrome, chrome])
        assert out == []

    def test_the_full_browser_is_not(self):
        chrome = ("com.android.chrome/com.google.android.apps.chrome.Main")
        out = self._run([self.CARE, chrome, chrome, chrome, chrome])
        assert len(out) == 1

    def test_the_launcher_is_scenery(self):
        launcher = "com.android.launcher3/.Launcher"
        out = self._run([self.CARE, launcher, launcher, launcher])
        assert out == []

    def test_a_running_macro_suspends_the_guard(self):
        out = self._run([self.CARE, self.SETTINGS, self.SETTINGS,
                         self.SETTINGS], status_state="running")
        assert out == []

    def test_nowhere_to_return_means_nothing_happens(self):
        out = self._run([self.SETTINGS, self.SETTINGS, self.SETTINGS])
        assert out == []


class TestPerAppDensity:
    """Every app is different (the owner's observation): HHAeXchange+
    needs 84 so its week fits one capture, while inMyTeam's sparse pages
    gain nothing from tiny text — and during the signature moment humans
    handle the physical phone, where bigger is a better pen. Density
    follows the app in front, never mid-scan, never mid-macro.

    The app-level value is what an UNMEASURED page of that app gets. The
    schedule screen no longer takes it — it was measured on this phone and
    carries its own number (TestDensityIsMeasuredPerScreen) — so these use a
    page with no measurement of its own, which is what the app-level value is
    for. `.HomeActivity` was the example here until that measurement existed.
    """

    def _run(self, focuses, current="84", scan=False, macro="idle"):
        feed._density_now[0] = 0
        calls = []

        def adb(a, *_, **__):
            calls.append(a)
            m = MagicMock(returncode=0)
            m.stdout = f"Physical density: 320\nOverride density: {current}\n".encode()
            return m

        import apt_log.macros as macros_mod

        class S:
            state = macro

        with patch.object(feed, "_adb", side_effect=adb), \
             patch.object(macros_mod, "read_status", return_value=S()), \
             patch.object(macros_mod.SCAN_ACTIVE, "is_set",
                          return_value=scan):
            for f in focuses:
                feed._watch_density(f)
        return [c for c in calls if c[:3] == ["shell", "wm", "density"]
                and len(c) == 4]

    UMA = "com.hhaexchange.uma/.VisitActivity"
    UMA_SCHEDULE = "com.hhaexchange.uma/.HomeActivity"
    IMT = "com.inmyteam.inmyteam/.MainActivity"

    # 172 is what an unmeasured uma page asks for on this phone, so it is
    # what the device is standing at when these start.
    def test_the_apps_density_is_applied_when_it_comes_forward(self):
        sets = self._run([self.UMA, self.IMT], current="172")
        assert sets == [["shell", "wm", "density", "105"]]

    def test_an_already_right_density_is_left_alone(self):
        assert self._run([self.UMA, self.UMA], current="172") == []

    def test_a_measured_screen_is_applied_the_same_way(self):
        """A page's own number travels the same path as an app's: applied as
        it comes forward, once."""
        assert self._run([self.UMA_SCHEDULE, self.UMA_SCHEDULE],
                         current="172") == [
            ["shell", "wm", "density", "130"]]

    def test_never_mid_scan(self):
        assert self._run([self.IMT], scan=True) == []

    def test_never_mid_macro(self):
        assert self._run([self.IMT], macro="running") == []

    def test_a_foreign_app_changes_nothing(self):
        assert self._run(["com.android.settings/.Settings"]) == []


class TestSignatureDensity:
    """The legacy app goes landscape for exactly one thing — the
    signature canvas, where humans draw with a finger on the physical
    phone. That moment gets its own density; portrait pages keep the
    app default."""

    LEGACY = "com.hhaexchange.caregiver/.VisitDetailActivity"
    SIDEWAYS = '<node bounds="[1400,600][1580,700]" class="Button"/>'
    UPRIGHT = '<node bounds="[10,600][700,700]" class="Button"/>'

    def _run(self, focus, hierarchy, current="84"):
        feed._density_now[0] = 0
        calls = []

        def adb(a, *_, **__):
            calls.append(a)
            m = MagicMock(returncode=0)
            m.stdout = f"Override density: {current}\n".encode()
            return m

        import apt_log.macros as macros_mod

        class S:
            state = "idle"

        with patch.object(feed, "_adb", side_effect=adb), \
             patch.object(macros_mod, "read_status", return_value=S()), \
             patch.object(macros_mod.SCAN_ACTIVE, "is_set",
                          return_value=False):
            feed._watch_density(focus, hierarchy=hierarchy)
        return [c for c in calls if c[:3] == ["shell", "wm", "density"]
                and len(c) == 4]

    def test_the_sideways_screen_gets_the_signature_density(self):
        sets = self._run(self.LEGACY, self.SIDEWAYS)
        assert sets == [["shell", "wm", "density",
                         str(feed.SIGNATURE_DENSITY)]]

    def test_an_upright_legacy_page_keeps_the_default(self):
        assert self._run(self.LEGACY, self.UPRIGHT) == []

    def test_the_flag_is_published_with_the_screen(self):
        assert feed._looks_landscape(self.SIDEWAYS) is True
        assert feed._looks_landscape(self.UPRIGHT) is False

    def test_the_quarter_turned_portrait_page_also_counts_as_sideways(self):
        """The legacy caregiver-signature page never rotates its activity —
        it draws the UI turned inside a portrait screen, betrayed by
        single-line labels in boxes taller than wide. The peek needs the
        same turn as the truly-landscape screen."""
        from apt_log import sign
        page = ('<node class="android.view.View" text="" '
                'resource-id="a:id/gestureSignature" '
                'bounds="[28,90][700,1560]" />'
                '<node class="android.widget.TextView" '
                'text="Firma del cuidador" bounds="[700,90][716,160]" />'
                '<node class="android.widget.TextView" '
                'text="Sadia Amselem" bounds="[20,95][36,160]" />')
        assert feed._looks_landscape(page) is False
        assert sign.presentation_rotated(page) is True


class TestAnonymousAim:
    """Bounds and class are not identity for a nameless element. The legacy
    home's menu rows are anonymous twins, and while the app finished
    loading its layout shifted one slot — "Horario para hoy" was tapped
    and the unscheduled-visit screen opened instead (seen live, first
    field test). For an aim with no resource-id, the claimed frame is
    enforced: if the tappable structure moved, the tap refuses and she
    aims at the fresh frame. Named aims keep the relaxed rule — and the
    frame id hashes only tappable structure, so a ticking clock still
    invalidates nothing."""

    ROW = {"rid": "", "cls": "LinearLayout", "b": [0, 158, 720, 194],
           "step": 0}
    NAMED = {"rid": "btn_go", "cls": "Button", "b": [0, 300, 720, 340],
             "step": 0}

    def _frame(self, tmp_path, els, fid=None):
        import datetime as dt
        path = tmp_path / "frame.json"
        path.write_text(json.dumps({
            "at": dt.datetime.now().isoformat(),
            "id": fid if fid is not None else feed.frame_id(els),
            "elements": els,
        }), encoding="utf-8")
        return path

    def test_an_anonymous_aim_from_a_moved_frame_refuses(self, tmp_path):
        """The screen she aimed from had an extra loading row; by tap time
        a DIFFERENT anonymous row occupies the same rectangle."""
        old = [{"rid": "", "cls": "LinearLayout", "b": [0, 122, 720, 158],
                "step": 0}, dict(self.ROW), dict(self.NAMED)]
        now = [dict(self.ROW), dict(self.NAMED)]
        path = self._frame(tmp_path, now)
        with patch.object(feed, "_adb") as adb:
            with pytest.raises(feed.StaleAim):
                feed.tap(feed.frame_id(old), dict(self.ROW), frame_path=path)
            adb.assert_not_called()

    def test_an_anonymous_aim_from_the_current_frame_taps(self, tmp_path):
        els = [dict(self.ROW), dict(self.NAMED)]
        path = self._frame(tmp_path, els)
        with patch.object(feed, "_adb") as adb:
            adb.return_value.returncode = 0
            out = feed.tap(feed.frame_id(els), dict(self.ROW),
                           frame_path=path)
        assert out["tapped"]["cls"] == "LinearLayout"

    def test_a_named_aim_keeps_the_relaxed_rule(self, tmp_path):
        """A resource-id IS identity; whole-frame equality refusing named
        taps is the measurement that relaxed this in the first place."""
        els = [dict(self.ROW), dict(self.NAMED)]
        path = self._frame(tmp_path, els)
        with patch.object(feed, "_adb") as adb:
            adb.return_value.returncode = 0
            out = feed.tap("somewhere-else-entirely", dict(self.NAMED),
                           frame_path=path)
        assert out["tapped"]["rid"] == "btn_go"


ANR_DUMP = ("  mCurrentFocus=Window{64be9f9 u0 Application Not Responding: "
            "com.hhaexchange.caregiver}\n  mAwake=true\n")
LIVE_DUMP = ("  mCurrentFocus=Window{d617003 u0 com.hhaexchange.caregiver/"
             "com.hhaexchange.caregiver.HomeActivity}\n  mAwake=true\n")


class TestNotResponding:
    """Android's "<app> isn't responding" dialog, seen live on the legacy app.

    It is invisible three ways at once: the dialog is a system window the
    accessibility tree does not publish, the ANR takes the Appium session
    down with it, and the dialog's window title ends in the package name —
    so the focus reads as that app running normally. Meanwhile the wedged
    app's own last dialog goes on being published as live buttons. The
    dumpsys read the focus already costs is what tells the truth.
    """

    def setup_method(self):
        feed._anr_since.clear()
        feed._anr_last_fix.clear()

    teardown_method = setup_method

    def _state(self, dump):
        with patch.object(feed, "_adb") as adb:
            adb.return_value.stdout = dump.encode()
            return feed.window_state()

    def test_the_wedged_package_is_read_from_the_dialog(self):
        focus, awake, anr = self._state(ANR_DUMP)
        assert anr == "com.hhaexchange.caregiver"
        assert awake is True

    def test_a_healthy_screen_names_no_wedge(self):
        focus, _awake, anr = self._state(LIVE_DUMP)
        assert anr == ""
        assert focus.endswith("HomeActivity")

    def test_capture_refuses_and_names_the_fault(self):
        with patch.object(feed, "window_state",
                          return_value=("com.hhaexchange.caregiver", True,
                                        "com.hhaexchange.caregiver")), \
             patch.object(feed, "_watch_anr") as watch, \
             patch.object(feed, "_adb") as adb:
            png, _focus, reason = feed.capture()
        assert reason == feed.APP_NOT_RESPONDING
        assert png is None
        watch.assert_called_once()
        adb.assert_not_called()          # no screencap of a dead screen

    def test_the_grace_period_is_waited_out_before_anything_is_killed(self):
        with patch.object(feed, "_adb") as adb:
            feed._watch_anr("com.hhaexchange.caregiver")
            adb.assert_not_called()      # first sighting only starts the clock

    def test_after_the_grace_the_app_is_restarted(self):
        pkg = "com.hhaexchange.caregiver"
        feed._anr_since[pkg] = time.time() - feed.ANR_GRACE - 1
        with patch.object(feed, "_adb") as adb:
            feed._watch_anr(pkg)
        sent = [call.args[0] for call in adb.call_args_list]
        assert ["shell", "am", "force-stop", pkg] in sent
        assert any(c[:3] == ["shell", "monkey", "-p"] and c[3] == pkg
                   for c in sent), "the app must come back up"

    def test_a_second_wedge_is_not_thrashed(self):
        pkg = "com.hhaexchange.caregiver"
        feed._anr_since[pkg] = time.time() - feed.ANR_GRACE - 1
        feed._anr_last_fix[pkg] = time.time()
        with patch.object(feed, "_adb") as adb:
            feed._watch_anr(pkg)
            adb.assert_not_called()

    def test_the_system_ui_is_never_force_stopped(self):
        pkg = "com.android.systemui"
        feed._anr_since[pkg] = time.time() - feed.ANR_GRACE - 1
        with patch.object(feed, "_adb") as adb:
            feed._watch_anr(pkg)
            adb.assert_not_called()

    def test_a_stranger_is_cleared_but_not_relaunched(self):
        """Only the apps this portal drives are put back up; where the phone
        belongs is the containment watchdog's decision, not this one's."""
        pkg = "com.android.settings"
        feed._anr_since[pkg] = time.time() - feed.ANR_GRACE - 1
        with patch.object(feed, "_adb") as adb:
            feed._watch_anr(pkg)
        sent = [call.args[0] for call in adb.call_args_list]
        assert ["shell", "am", "force-stop", pkg] in sent
        assert not any("monkey" in c for c in sent)

    def test_a_wedged_screen_publishes_nothing_to_aim_at(self, tmp_path):
        """The legacy app wedged on its own inactivity dialog, and that
        dialog's button went on being published as pressable."""
        xml = ('<node class="android.widget.Button" resource-id="a:id/'
               'btn_negative" text="DE ACUERDO" bounds="[6,843][713,864]"/>')
        frame = {"id": "x", "img": "", "at": "now", "size": [720, 1600]}
        feed.write_screen(tmp_path / "screen.json", frame, "unknown",
                          feed.APP_NOT_RESPONDING, xml,
                          focus="com.hhaexchange.caregiver")
        doc = json.loads((tmp_path / "screen.json").read_text())
        assert doc["elements"] == []
        assert doc["blocked"] == feed.APP_NOT_RESPONDING

    def test_recovery_forgets_the_wedge(self):
        """So a later ANR waits out its own grace instead of inheriting an
        expired one and being killed on sight."""
        feed._anr_since["com.hhaexchange.caregiver"] = time.time() - 999
        with patch.object(feed, "window_state",
                          return_value=("com.x/.Home", True, "")), \
             patch.object(feed, "_watch_shade"), \
             patch.object(feed, "_watch_containment"), \
             patch.object(feed, "_watch_density"), \
             patch.object(feed, "_adb") as adb:
            adb.return_value.returncode = 0
            adb.return_value.stdout = b"\x89PNG"
            feed.capture()
        assert feed._anr_since == {}


class TestContentDescriptionIsALabel:
    """A view that draws its own content has no text for the tree to carry,
    and Android's answer is content-desc. Mobile Caregiver+ builds its whole
    visits list that way — every row a bare View whose only words are the
    sentence a screen reader would say — so a portal reading text alone
    showed three identical empty strips where the phone showed three named
    visits."""

    ROW = ('<node class="android.view.View" resource-id="" clickable="true" '
           'text="" content-desc="La visita está programada para ATANASIO '
           'MEDEROS TORRIEN" bounds="[0,146][720,185]"/>')

    def test_a_description_becomes_the_label(self):
        (el,) = feed.elements(self.ROW, label=True)
        assert "ATANASIO" in el["txt"]
        assert el["has_text"] is True

    def test_a_description_is_words_on_the_screen_too(self):
        quiet = self.ROW.replace('clickable="true"', 'clickable="false"')
        (st,) = feed.statics(quiet)
        assert "ATANASIO" in st["txt"]

    def test_text_wins_when_a_node_carries_both(self):
        """visits_event0_title says 'mar, ago 18' twice; a row that repeats
        itself must not be read twice."""
        both = ('<node class="android.widget.TextView" clickable="false" '
                'text="mar, ago 18" content-desc="mar, ago 18" '
                'bounds="[6,130][49,140]"/>')
        (st,) = feed.statics(both)
        assert st["txt"] == "mar, ago 18"

    def test_a_description_still_obeys_the_disclosure_rule(self):
        """It is text by every rule that matters: unspoken unless labelled."""
        (el,) = feed.elements(self.ROW)          # label=False
        assert "txt" not in el
        assert el["has_text"] is True

    def test_an_editable_field_never_lends_its_description(self):
        typed = ('<node class="android.widget.EditText" clickable="true" '
                 'text="" content-desc="hunter2" bounds="[0,0][100,50]"/>')
        (el,) = feed.elements(typed, label=True)
        assert el.get("txt") is None or el["txt"] == ""
        assert feed.statics(typed) == []


class TestAPicturesDescriptionIsNotAStatement:
    """Alt text is written for somebody who cannot see the image.

    Read as words on the screen it produced the app's own logo announcing
    itself in the page title — "Logotipo de H H AeXchange +", the brand
    spelled out letter by letter for a screen reader — in the one slot
    somebody reads without looking for it.
    """

    LOGO = ('<node class="android.widget.ImageView" resource-id="com.x:id/logo" '
            'clickable="false" text="" content-desc="Logotipo de H H '
            'AeXchange +" bounds="[40,60][300,120]"/>')

    def test_a_logos_alt_text_is_not_words_on_the_screen(self):
        assert [s["txt"] for s in feed.statics(self.LOGO)] == [""]

    def test_the_image_still_rides_along_by_its_name(self):
        """The EVV check marks are recognised by resource-id, and dropping
        the description must not drop the image with it."""
        (st,) = feed.statics(self.LOGO)
        assert st["rid"] == "logo"

    def test_an_anonymous_decoration_still_stays_out(self):
        bare = self.LOGO.replace('resource-id="com.x:id/logo"', 'resource-id=""')
        assert feed.statics(bare) == []

    def test_a_tappable_image_keeps_its_description(self):
        """On something clickable the same attribute names the control —
        Back, Search, the tab you are on — and that is a real label."""
        button = ('<node class="android.widget.ImageButton" clickable="true" '
                  'text="" content-desc="Atrás" bounds="[0,0][60,60]"/>')
        (el,) = feed.elements(button, label=True)
        assert el["txt"] == "Atrás"

    def test_an_image_that_really_does_carry_text_keeps_it(self):
        """`text=` on an ImageView is not alt text; it is text."""
        odd = self.LOGO.replace('text=""', 'text="Agosto 18"')
        (st,) = feed.statics(odd)
        assert st["txt"] == "Agosto 18"


class TestTheBarSurvivesTruncation:
    """A pinned tab bar is the LAST thing in the tree, and truncation used
    to cut the tail: the 370-message inbox spent its whole budget on
    messages and published no captions at all, so the portal lost the app's
    navigation. She could read the inbox and had no way back to the visits
    list except the phone's own Back button."""

    def _screen(self, messages: int) -> str:
        rows = "".join(
            f'<node class="android.widget.TextView" clickable="false" '
            f'text="Planned Downtime {i}" '
            f'bounds="[0,{100 + i * 10}][720,{108 + i * 10}]"/>'
            for i in range(messages))
        bar = "".join(
            f'<node class="android.widget.TextView" clickable="false" '
            f'text="{cap}" bounds="[{x},1556][{x + 40},1565]"/>'
            for cap, x in (("Visitas", 259), ("Beneficiarios", 337),
                           ("Mensajes", 436)))
        return rows + bar

    def test_the_captions_outlive_a_flood_of_content(self):
        found = feed.statics(self._screen(300))
        assert len(found) <= feed.MAX_STATICS
        assert [s["txt"] for s in found[-3:]] == ["Visitas", "Beneficiarios",
                                                  "Mensajes"]

    def test_the_content_is_what_gets_cut(self):
        """The head is kept, so the top of the page still reads normally."""
        found = feed.statics(self._screen(300))
        assert found[0]["txt"] == "Planned Downtime 0"

    def test_a_real_bar_survives_whole(self):
        """A tab is not one node. This app spends three on each — the cell's
        description, the icon, the caption — and hangs an unread count
        beside one, so its three-tab bar runs to ten. Reserving less than
        the bar is worth loses the caption that ends up first in line."""
        rows = "".join(
            f'<node class="android.widget.TextView" clickable="false" '
            f'text="msg {i}" bounds="[0,{100 + i}][720,{108 + i}]"/>'
            for i in range(300))
        bar = ""
        for cap, x in (("Visitas", 226), ("Beneficiarios", 315),
                       ("Mensajes", 404)):
            bar += (f'<node class="android.widget.FrameLayout" '
                    f'clickable="false" text="" content-desc="{cap}" '
                    f'bounds="[{x},1539][{x + 89},1568]"/>'
                    f'<node class="android.widget.ImageView" '
                    f'resource-id="app:id/icon_{cap}" clickable="false" '
                    f'text="" bounds="[{x + 30},1542][{x + 43},1555]"/>'
                    f'<node class="android.widget.TextView" '
                    f'clickable="false" text="{cap}" '
                    f'bounds="[{x + 20},1556][{x + 60},1565]"/>')
        bar += ('<node class="android.widget.TextView" clickable="false" '
                'text="370" bounds="[447,1539][463,1548]"/>')
        published = [s["txt"] for s in feed.statics(rows + bar)]
        for cap in ("Visitas", "Beneficiarios", "Mensajes"):
            assert published.count(cap) >= 1, f"{cap} was truncated away"

    def test_a_short_screen_is_untouched(self):
        found = feed.statics(self._screen(5))
        assert len(found) == 8
        assert found[0]["txt"] == "Planned Downtime 0"


class TestPermissionDialogIsNotWandering:
    """HHAeXchange+ asks for location at check-in and Android answers with
    its own dialog, from its own package. Read as wandering, the watchdog
    bounced it after five seconds — taking the permission prompt with it
    and stopping the very check-in it was raised for. Recovered from the
    flight recorder: grantpermissionsactivity, mid-flow, between the
    schedule and the GPS screen."""

    def setup_method(self):
        feed._last_care_app[0] = "com.hhaexchange.uma"
        feed._out_since[0] = 0.0
        feed._last_return[0] = 0.0

    teardown_method = setup_method

    def _run(self, focus, dwell=True):
        with patch.object(feed, "_adb") as adb:
            feed._watch_containment(focus)
            if dwell:
                feed._out_since[0] = time.time() - feed.CONTAIN_DWELL - 1
                feed._watch_containment(focus)
            return [c.args[0] for c in adb.call_args_list]

    def test_the_location_prompt_is_left_standing(self):
        sent = self._run("com.google.android.permissioncontroller/"
                         "com.android.permissioncontroller.permission.ui."
                         "GrantPermissionsActivity")
        assert sent == [], "the prompt the app raised must not be bounced"

    def test_a_stranger_is_still_bounced(self):
        sent = self._run("com.android.vending/.AssetBrowserActivity")
        assert any("monkey" in c for c in sent)


class TestDisabledControls:
    """HHAeXchange+ ships the visit screen with a clock-out button that
    exists from the moment of check-in and does nothing until the visit is
    over — `visit_details_clock_out_button_disabled`, straight from the
    recorder. Published as an ordinary control it read as a live call to
    action: press, nothing happens, and the portal wears the fault."""

    def test_a_greyed_control_is_published_as_greyed(self):
        xml = ('<node class="android.view.View" clickable="true" '
               'enabled="false" text="" '
               'resource-id="a:id/visit_details_clock_out_button_disabled" '
               'bounds="[11,1532][709,1557]"/>')
        (el,) = feed.elements(xml)
        assert el["enabled"] is False

    def test_absent_means_enabled(self):
        xml = ('<node class="android.widget.Button" clickable="true" '
               'text="Continuar" bounds="[11,1532][709,1557]"/>')
        (el,) = feed.elements(xml)
        assert el["enabled"] is True


class TestTheShadeIsNotTheAppUnderneath:
    """Reported from the field: "somehow inMyTeam ended up in the phone
    settings and I didn't do anything". It was the notification shade —
    quick-settings tiles and a wall of the owner's private notifications —
    sitting over the app for twenty minutes.

    Two faults, and the second is mine from the same day.

    `cmd statusbar collapse` returns success and does nothing on this phone.
    So do KEYCODE_BACK, KEYCODE_HOME and `service call statusbar 2`; all four
    were tried against the shade while it was actually up. Only the gesture
    closes it. The collapse now checks and escalates.

    And the popup-owner rule — which reports the app beneath a dropdown —
    was reporting the app beneath the SHADE. That is not the app's own
    surface, it is the system's, over the app. Naming the app told every
    check the phone was fine and, worse, told the containment watchdog there
    was nothing to recover: the bounce that used to clear a stuck shade
    stopped happening the moment that rule shipped.
    """

    SHADE = ("  mCurrentFocus=Window{8030035 u0 NotificationShade}\n"
             "  mFocusedApp=ActivityRecord{a1 u0 "
             "com.inmyteam.inmyteam/.view.activities.MainActivity} t9}\n"
             "  mAwake=true\n")

    def _read(self, out):
        with patch.object(feed, "_adb") as adb:
            adb.return_value.stdout = out.encode()
            return feed.window_state()

    def test_the_shade_is_reported_as_the_shade(self):
        focus, _, _ = self._read(self.SHADE)
        assert focus == "NotificationShade"

    def test_so_the_watchdog_can_still_see_it(self):
        """The whole point: a care app's package here means "nothing to do"."""
        focus, _, _ = self._read(self.SHADE)
        assert focus.split("/")[0] not in feed.CARE_APPS

    def test_a_real_popup_still_reports_its_owner(self):
        """The dropdown fix must survive: an app's own chooser IS the app."""
        out = ("  mCurrentFocus=Window{3d46066 u0 Pop-Up Window}\n"
               "  mFocusedApp=ActivityRecord{a1 u0 "
               "com.inmyteam.inmyteam/.view.activities.MainActivity} t9}\n"
               "  mAwake=true\n")
        focus, _, _ = self._read(out)
        assert focus.split("/")[0] == "com.inmyteam.inmyteam"

    @pytest.mark.parametrize("panel", [
        "NotificationShade", "StatusBar", "VolumeDialog",
        "ScreenDecorOverlay", "NavigationBar",
    ])
    def test_every_system_panel_is_left_named_as_itself(self, panel):
        out = (f"  mCurrentFocus=Window{{a u0 {panel}}}\n"
               "  mFocusedApp=ActivityRecord{a1 u0 com.x/.Home} t9}\n"
               "  mAwake=true\n")
        focus, _, _ = self._read(out)
        assert focus == panel

    def test_the_collapse_escalates_to_the_gesture(self):
        """The command reports success either way, so it is checked."""
        calls = []

        def fake_adb(args, serial=None, **kw):
            calls.append(args)
            out = type("R", (), {"stdout": self.SHADE.encode(),
                                 "returncode": 0})()
            return out

        feed._last_collapse[0] = 0.0
        with patch.object(feed, "_adb", side_effect=fake_adb), \
             patch.object(feed, "screen_size", return_value=[720, 1600]), \
             patch.object(feed.time, "sleep"):
            feed._watch_shade('<node resource-id="a:id/notification_stack"/>')
        assert ["shell", "cmd", "statusbar", "collapse"] in calls
        swipes = [c for c in calls if "swipe" in c]
        assert swipes, "a shade that ignores the command must be swiped away"
        assert swipes[0][:3] == ["shell", "input", "swipe"]

    def test_a_shade_that_obeys_is_not_swiped(self):
        """No gesture on a phone where the clean command works."""
        gone = ("  mCurrentFocus=Window{a u0 com.inmyteam.inmyteam/.Main}\n"
                "  mAwake=true\n")
        calls = []

        def fake_adb(args, serial=None, **kw):
            calls.append(args)
            return type("R", (), {"stdout": gone.encode(), "returncode": 0})()

        feed._last_collapse[0] = 0.0
        with patch.object(feed, "_adb", side_effect=fake_adb), \
             patch.object(feed, "screen_size", return_value=[720, 1600]), \
             patch.object(feed.time, "sleep"):
            feed._watch_shade('<node resource-id="a:id/notification_stack"/>')
        assert not [c for c in calls if "swipe" in c]


class TestCoveredScreen:
    """A system panel over the app, said out loud so the page can act on it.

    The shade came down over inMyTeam and stayed — the collapse command lies
    on this phone, and the popup-owner rule had just taught every check that
    the app underneath was in front. From the portal it looked like a normal
    screen that had stopped answering, and there was nothing to press. The
    document now carries the fact, and the page turns it into one button.
    """

    @pytest.mark.parametrize("panel", [
        "NotificationShade", "StatusBar", "VolumeDialog", "Shade Carrier",
    ])
    def test_a_covering_panel_is_reported(self, panel):
        assert feed.screen_is_covered(panel)

    @pytest.mark.parametrize("focus", [
        "com.inmyteam.inmyteam/.view.activities.MainActivity",
        "com.hhaexchange.mobile/.MainActivity",
        "",
    ])
    def test_an_ordinary_screen_is_not(self, focus):
        assert not feed.screen_is_covered(focus)

    def test_the_keyboard_is_not_a_thing_to_escape_from(self):
        """It is up because a field was tapped. Offering to clear the screen
        every time she types would be furniture — and the type bar, not this,
        is the way through that moment."""
        assert not feed.screen_is_covered("InputMethod")
        # Still not the app's own window, though: the owner substitution must
        # go on leaving it alone.
        assert feed._is_a_system_panel("InputMethod")

    def test_the_flag_reaches_the_document(self, tmp_path):
        target = tmp_path / "screen.json"
        frame = {"id": "a", "img": "", "at": "now", "size": [720, 1600]}
        feed.write_screen(target, frame, "unknown", "", "<node/>",
                          focus="NotificationShade")
        assert json.loads(target.read_text())["covered"] is True

    def test_and_is_false_on_an_ordinary_screen(self, tmp_path):
        target = tmp_path / "screen.json"
        frame = {"id": "a", "img": "", "at": "now", "size": [720, 1600]}
        feed.write_screen(target, frame, "unknown", "", "<node/>",
                          focus="com.inmyteam.inmyteam/.Main")
        assert json.loads(target.read_text())["covered"] is False


class TestTheVisitNoteIsReadable:
    """"The additional note is missing text that the phone has."

    It was withheld by the rule that withholds ALL typed text — written for
    passwords and for the code inMyTeam texts — which here blanked the one
    field she was trying to read back off her own screen.
    """

    W, H = 720, 1600
    NOTE = ('<node class="android.widget.EditText" resource-id=""'
            ' clickable="true" text="Left the key with the neighbour"'
            ' bounds="[16,882][704,948]" />')

    def _el(self, xml):
        return feed.elements(xml, label=True,
                             package="com.inmyteam.inmyteam",
                             size=(self.W, self.H))[0]

    def test_the_note_shows_what_she_typed(self):
        el = self._el(f"<hierarchy>{self.NOTE}</hierarchy>")
        assert el["txt"] == "Left the key with the neighbour"

    def test_and_is_named_by_the_portal(self):
        el = self._el(f"<hierarchy>{self.NOTE}</hierarchy>")
        assert el["name_key"] == "papp.imt.note"

    def test_the_same_box_on_a_code_screen_says_nothing(self):
        """The credential markers answer first, and they answer for every
        box on the screen. A code is the one kind of typed text that must
        never travel, and this rule may not become the hole it goes through."""
        xml = ("<hierarchy>please enter your code"
               '<node class="android.widget.EditText" resource-id=""'
               ' clickable="true" text="246252"'
               ' bounds="[16,882][704,948]" /></hierarchy>')
        el = self._el(xml)
        assert not el.get("txt")
        assert not el.get("name_key")

    def test_a_single_line_field_is_not_a_note(self):
        """A code screen carries one unnamed EditText too. Height and full
        width are what tell them apart."""
        xml = ('<hierarchy><node class="android.widget.EditText"'
               ' resource-id="" clickable="true" text="246252"'
               ' bounds="[220,700][500,740]" /></hierarchy>')
        el = self._el(xml)
        assert not el.get("txt")

    def test_another_apps_note_shaped_box_is_not_disclosed(self):
        """This table names controls for ONE app, by sight. Nothing here
        generalises to a field it has never seen."""
        el = feed.elements(f"<hierarchy>{self.NOTE}</hierarchy>", label=True,
                           package="com.hhaexchange.caregiver",
                           size=(self.W, self.H))[0]
        assert not el.get("txt")


class TestAControlThatDoesNotSayItIsOne:
    """HHAeXchange+'s Funciones page — the care-plan tasks, ticked off at the
    end of every visit.

    Each task's Se realizó / No realizado pair is a plain View with
    `clickable="false"`. They ARE controls: the app hand-writes their
    description as "no seleccionado  Se realizó %1$s  Toca dos veces para
    alternar" — TalkBack's own sentence for a node that HAS a click action,
    with the selection state in front of it.

    Read by the attribute alone, not one of the fourteen pairs reached the
    portal: the page was a wall of task names with nothing to press and no
    way to finish a visit. Found with the caregiver standing in a patient's
    home with the page open in front of her.
    """

    DONE = "no seleccionado Se realizó %1$s Toca dos veces para alternar"
    ON = "seleccionado Se realizó %1$s Toca dos veces para alternar"
    NOT = "no seleccionado No realizado %1$s Toca dos veces para alternar"

    @staticmethod
    def _node(b, desc="", text="", click="false"):
        return (f'<node class="android.view.View" resource-id="" '
                f'clickable="{click}" text="{text}" content-desc="{desc}" '
                f'bounds="[{b[0]},{b[1]}][{b[2]},{b[3]}]" '
                f'package="com.hhaexchange.uma" />')

    def _xml(self):
        return "<hierarchy>" + "".join([
            self._node([37, 150, 110, 160], text="Hair Care-Shampoo"),
            self._node([636, 150, 667, 179], desc=self.DONE),
            self._node([667, 150, 699, 179], desc=self.NOT),
            self._node([37, 201, 73, 211], text="Toilet Use"),
            self._node([636, 201, 667, 230], desc=self.ON),
            self._node([667, 201, 699, 230], desc=self.NOT),
        ]) + "</hierarchy>"

    def _els(self, label=False):
        return feed.elements(self._xml(), label=label,
                             package="com.hhaexchange.uma", size=(720, 1600))

    def test_every_tick_reaches_the_portal(self):
        assert len(self._els()) == 4

    def test_the_state_comes_from_the_sentence(self):
        """The app never sets the attribute — "no seleccionado" IS the state,
        so a task already ticked has to be read out of the words."""
        assert [e["checked"] for e in self._els()] \
            == [False, False, True, False]

    def test_they_are_drawn_as_the_choice_they_are(self):
        assert all(e.get("role") == "toggle" for e in self._els())

    def test_the_class_is_still_what_the_phone_published(self):
        """Half of what a tap verifies itself against. The role is about the
        drawing and nothing else."""
        assert all(e["cls"] == "View" for e in self._els())

    def test_one_node_does_not_arrive_twice(self):
        """Once as something to press and once as a caption beside it."""
        spoken = [s for s in feed.statics(self._xml())
                  if "seleccionado" in (s.get("txt") or "")]
        assert spoken == []

    def test_the_task_name_beside_it_is_still_a_caption(self):
        assert [s["txt"] for s in feed.statics(self._xml())] \
            == ["Hair Care-Shampoo", "Toilet Use"]

    # ------------------------------------------------------------- the words
    def test_the_sentence_is_read_down_to_what_it_means(self):
        assert feed.reader_control(self.DONE) == ("Se realizó", False)
        assert feed.reader_control(self.ON) == ("Se realizó", True)
        assert feed.reader_control(self.NOT) == ("No realizado", False)

    def test_an_unfilled_placeholder_is_not_part_of_the_name(self):
        """%1$s — the argument the app forgot to fill, sitting where the
        task's own name was meant to go."""
        assert "%" not in feed.reader_control(self.DONE)[0]

    def test_an_ordinary_description_is_not_a_control(self):
        assert feed.reader_control("La visita está programada para las 8:00")\
            is None
        assert feed.reader_control("") is None

    def test_english_says_it_too(self):
        assert feed.reader_control("not selected Performed, double tap to "
                                   "toggle") == ("Performed", False)

    # ------------------------------- named by the portal, in her own language
    def test_the_pair_is_named_by_the_portal(self):
        """The app's words arrive only inside a screen reader's sentence, and
        the words of a page with a patient on it do not cross the wire. These
        two captions are fixed chrome — the same on every task of every visit
        — so the portal says them itself."""
        keys = [e.get("name_key") for e in self._els()]
        assert keys == [feed.TASK_DONE_KEY, feed.TASK_NOT_DONE_KEY,
                        feed.TASK_DONE_KEY, feed.TASK_NOT_DONE_KEY]

    def test_not_done_is_not_read_as_done(self):
        """"no realizado" contains "realizado"."""
        assert feed.task_key("No realizado") == feed.TASK_NOT_DONE_KEY
        assert feed.task_key("Se realizó") == feed.TASK_DONE_KEY

    def test_a_control_this_does_not_recognise_gets_no_name(self):
        assert feed.task_key("Abrir el mapa") == ""

    def test_both_languages_have_the_words(self):
        import json as json_mod
        from pathlib import Path

        base = Path(__file__).resolve().parents[1] / "src/apt_log/ui/locales"
        for name in ("en.json", "es.json"):
            words = json_mod.loads((base / name).read_text(encoding="utf-8"))
            assert words.get(feed.TASK_DONE_KEY)
            assert words.get(feed.TASK_NOT_DONE_KEY)

    # ------------------------------------------------------ and end to end
    def test_the_page_comes_out_as_task_rows_with_a_choice_on_each(self):
        from apt_log.ui import screenview

        model = screenview.build({
            "id": "f", "size": [720, 1600], "blocked": "", "notice": "",
            "elements": self._els(), "statics": feed.statics(self._xml())})
        segments = [it for row in model["rows"] for it in row["items"]
                    if it["kind"] == "segment"]
        assert len(segments) == 2
        assert [p["checked"] for p in segments[0]["parts"]] == [False, False]
        assert all(p.get("aim") for p in segments[0]["parts"])


class TestATickBoxIsSmall:
    """The other half of the same signature screen.

    HHAeXchange+'s canvas carries a double-tap description too — half the
    screen of it — and reading that as a control turned the surface a patient
    signs on into an ON/OFF SWITCH. A screen reader's "double tap" says a
    node ACCEPTS a tap, which a drawing surface certainly does; it does not
    say the node is a checkbox.
    """

    CANVAS = ('<node class="android.view.View" resource-id="" '
              'clickable="{click}" text="" '
              'content-desc="Firma, toca dos veces para firmar" '
              'bounds="[18,171][1518,525]" package="com.hhaexchange.uma"/>')
    TICK = ('<node class="android.view.View" resource-id="" clickable="false" '
            'text="" content-desc="no seleccionado Se realizó %1$s Toca dos '
            'veces para alternar" bounds="[636,150][667,179]" '
            'package="com.hhaexchange.uma"/>')

    def _els(self, xml):
        return feed.elements(xml, label=False, package="com.hhaexchange.uma",
                             size=(720, 1600))

    def test_a_canvas_is_never_promoted_to_a_toggle(self):
        els = self._els("<hierarchy>"
                        + self.CANVAS.format(click="true") + "</hierarchy>")
        assert els and els[0].get("role") is None

    def test_a_real_tick_still_is(self):
        els = self._els("<hierarchy>" + self.TICK + "</hierarchy>")
        assert els and els[0]["role"] == "toggle"

    def test_a_big_node_the_sentence_let_in_is_still_published(self):
        """Losing the ROLE is the whole correction. Dropping the element was
        the first version of this guard, and it took the signature canvas off
        the page altogether — the surface is not clickable in the tree, so
        that sentence is the only thing saying it is there, and without it
        the portal drew a signature screen with nothing to sign on."""
        els = self._els("<hierarchy>"
                        + self.CANVAS.format(click="false") + "</hierarchy>")
        assert len(els) == 1
        assert els[0].get("role") is None
        assert els[0]["b"] == [18, 171, 1518, 525]

    def test_a_clickable_canvas_is_still_published(self):
        """It is a real control — the app says so in the ordinary way — and
        the reflow needs it there to draw the signing surface at all."""
        els = self._els("<hierarchy>"
                        + self.CANVAS.format(click="true") + "</hierarchy>")
        assert len(els) == 1 and els[0]["b"] == [18, 171, 1518, 525]

    def test_the_ceiling_sits_well_clear_of_both(self):
        """A care-plan tick is 31x29 on 720x1600 — 0.078% of the screen. The
        canvas beside it is 46% of the same screen. The ceiling has 64x of
        room under it and 9x over it, which is what makes it a boundary
        rather than a tuning."""
        tick = (31 * 29) / (720 * 1600)
        canvas = (1500 * 354) / (720 * 1600)
        assert tick * 60 < feed.READER_CONTROL_MAX_SHARE < canvas / 9


class TestWhenThePhotographNeedsTurning:
    """"Once I get to the signature part everything goes BLACK."

    Two questions had one answer between them, and answering them together
    is what turned the page black on the one screen the turn was written for.

    "Is the screen wide?" and "is the photograph the wrong way up?" are the
    same question ONLY for the legacy app, which draws its signature page
    rotated inside a portrait activity: the screencap comes back portrait
    with sideways content in it. HHAeXchange+ genuinely rotates the device —
    the screencap arrives landscape and already upright — and the peek turned
    it anyway, rotating a wide photograph a quarter turn and scaling it by a
    factor computed for a portrait one. What is left on the stage is nearly
    nothing.
    """

    UMA_PAD = ('<node class="android.view.View" resource-id="" '
               'clickable="false" text="" '
               'content-desc="Firma, toca dos veces para firmar" '
               'bounds="[18,171][1518,525]"/>'
               '<node class="android.view.View" '
               'resource-id="app:id/signature_screen_submit_button" '
               'clickable="true" text="" bounds="[1474,656][1529,681]"/>')

    def test_a_genuinely_landscape_screen_is_wide(self):
        assert feed._looks_landscape(self.UMA_PAD)

    def test_but_its_photograph_is_already_upright(self):
        """`sign.sideways` is the answer the INK turns by, and the file says
        the peek and the ink must never disagree. It says no here."""
        from apt_log import sign

        assert not sign.sideways(self.UMA_PAD, package="com.hhaexchange.uma")

    def test_the_legacy_pages_photograph_does_need_turning(self):
        """Portrait bounds, a canvas on it, and an app that draws its
        signature page a quarter turn round inside a portrait activity."""
        from apt_log import sign

        legacy = ('<node class="android.widget.FrameLayout" '
                  'resource-id="app:id/gesturePatientSignature" '
                  'bounds="[0,492][720,1142]"/>')
        assert not feed._looks_landscape(legacy)
        assert sign.sideways(legacy, package="com.hhaexchange.caregiver")

    def test_the_two_answers_travel_as_two_fields(self):
        """A page that has only one of them has to guess, and guessing is
        what went black."""
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1]
               / "src/apt_log/feed.py").read_text(encoding="utf-8")
        assert '"landscape": _looks_landscape(hierarchy),' in src
        assert '"turn": bool(_has_canvas(hierarchy)' in src

    def test_the_peek_turns_by_the_photographs_answer(self):
        from pathlib import Path

        js = (Path(__file__).resolve().parents[1]
              / "src/apt_log/ui/static/phone.js").read_text(encoding="utf-8")
        assert "toggle('sideways', !!meta.turn)" in js
        assert "toggle('sideways', !!meta.landscape)" not in js


class TestRetiringTheLegacyApp:
    """Its one patient was migrated to HHAeXchange+ — the app itself had been
    saying so for months, on the very interstitial this project mapped as
    "startup": "If you do not see your Agency below, please use the
    HHAeXchange+ Application."

    Retired, not demolished. It stops being somewhere the phone GOES; it does
    not stop being an app this code can read.
    """

    LEGACY = "com.hhaexchange.caregiver"

    def test_it_is_named_as_retired(self):
        assert feed.retired(self.LEGACY)

    def test_the_apps_she_uses_are_not(self):
        for app in ("com.hhaexchange.uma", "com.tellus.evv.v2",
                    "com.inmyteam.inmyteam"):
            assert not feed.retired(app)

    def test_it_is_off_the_picker(self):
        """A tile is an invitation, and there is nothing on the other side
        of that one any more."""
        from apt_log.ui.app import PHONE_APPS

        assert self.LEGACY not in [a["package"] for a in PHONE_APPS]

    def test_but_what_it_was_is_still_written_down(self):
        """The tile was the only place this project recorded that app's
        identity — package, brand, colour."""
        from apt_log.ui.app import RETIRED_TILE

        assert RETIRED_TILE["package"] == self.LEGACY

    def test_it_never_signs_itself_in(self):
        from apt_log import macros
        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)

        provider = MemorySecretProvider(**{APP_USERNAME: "u",
                                           APP_PASSWORD: "p"})
        assert macros.auth_macro_for(self.LEGACY, provider) is None

    # ------------------------------------ what deliberately did NOT move
    def test_its_pages_are_still_in_the_atlas(self):
        """The record of what its screens were."""
        assert feed.ACTIVITY_SCREENS.get(self.LEGACY)

    def test_the_rotated_signature_rule_stays(self):
        """This is the ONLY app that draws its signature page a quarter turn
        round inside a portrait activity, and that fact is the whole reason
        the peek and the ink carry separate "wide" and "turned" answers.
        Delete it and nobody can tell why either flag exists."""
        from apt_log import sign

        assert self.LEGACY in sign.ROTATED_CANVAS_APPS

    def test_its_visits_list_marks_still_have_their_names(self):
        from apt_log.ui import screenview

        assert screenview.IMAGE_MARK_KEYS.get("imgstarttime")
        assert screenview.IMAGE_MARK_KEYS.get("imgendtime")

    def test_it_can_still_be_opened_deliberately(self):
        """Fetching an old record on purpose is the case retirement should
        still allow."""
        from apt_log import macros

        assert "open_hhax_legacy" in macros.MACROS
        assert "hhax_legacy_login" in macros.MACROS

    def test_its_screens_still_read_as_a_care_app(self):
        """If it does come to the front, the portal renders it properly
        rather than as a stranger."""
        assert self.LEGACY in feed.CARE_APPS


class TestTheProviderPickerIsNotASplash:
    """HHAeXchange+ puts its provider picker inside `onboardingactivity`,
    which on a single-provider account really is a splash — so the atlas is
    right about the activity and wrong about this page. The console read
    "the app is starting up" while she was being asked which agency to work
    under."""

    FOCUS = ("com.hhaexchange.uma/"
             "com.hhaexchange.carehub.ui.activities.OnboardingActivity")
    PICKER = ('<node resource-id="app:id/'
              'agency_configuration_screen_add_connection_button" '
              'bounds="[11,1532][709,1557]"/>')

    def test_the_picker_is_called_what_it_is(self):
        assert feed.screen_for(self.FOCUS, self.PICKER) == "agency"

    def test_the_same_activity_without_it_is_still_a_splash(self):
        """It really does flash past after sign-in on an account with one
        provider, which is what it was mapped from."""
        assert feed.screen_for(self.FOCUS, "<hierarchy/>") == "startup"

    def test_and_with_no_hierarchy_at_all_the_atlas_still_answers(self):
        assert feed.screen_for(self.FOCUS) == "startup"

    def test_no_other_page_is_second_guessed(self):
        """The atlas is the cheap answer and the one that survives a page
        nobody has read yet — only an activity known to hold two pages is
        looked into."""
        home = ("com.hhaexchange.uma/"
                "com.hhaexchange.carehub.ui.activities.HomeActivity")
        assert feed.screen_for(home, self.PICKER) == "home"

    def test_the_console_has_a_word_for_it(self):
        import json as json_mod
        from pathlib import Path

        base = Path(__file__).resolve().parents[1] / "src/apt_log/ui/locales"
        for name in ("en.json", "es.json"):
            words = json_mod.loads((base / name).read_text(encoding="utf-8"))
            assert words.get("mirror.screen.agency")


class TestAKeypadIsALockWhereverItIsDrawn:
    """Mobile Caregiver+ raises its passcode keypad under two activities.

    `PinActivity` the login markers catch. `DashboardActivity` they do not —
    and that is where it was found live: ten digit keys and a delete key,
    published as `screen=home, blocked=''`. Nothing refused the picture and
    auto sign-in never reached its own gate, because everything that decides
    "this is a credential screen" was reading the activity's NAME.

    So the keypad answers for itself. Both dialects are checked because the
    two read paths disagree on attribute order, and a lock recognised on only
    one of them is worse than one recognised on neither.
    """

    MC = "com.tellus.evv.v2"
    DASHBOARD = f"{MC}/com.tellus.evv.v2.ui.dashboard.DashboardActivity"

    @staticmethod
    def _dumped(digits="0123456789") -> str:
        """`uiautomator dump`'s shape: text BEFORE class."""
        keys = "".join(
            f'<node index="{i}" text="{d}" resource-id="" '
            f'class="android.widget.Button" package="com.tellus.evv.v2" '
            f'bounds="[{i * 60},1000][{i * 60 + 50},1060]"/>'
            for i, d in enumerate(digits))
        return f'<hierarchy rotation="0">{keys}</hierarchy>'

    @staticmethod
    def _sourced(digits="0123456789") -> str:
        """Appium page_source's shape: class BEFORE text, tag named."""
        keys = "".join(
            f'<android.widget.Button index="{i}" package="com.tellus.evv.v2" '
            f'class="android.widget.Button" text="{d}" '
            f'bounds="[{i * 60},1000][{i * 60 + 50},1060]" />'
            for i, d in enumerate(digits))
        return f'<hierarchy rotation="0">{keys}</hierarchy>'

    def test_the_keypad_is_seen_in_both_dialects(self):
        assert feed.keypad_on_screen(self._dumped()) is True
        assert feed.keypad_on_screen(self._sourced()) is True

    def test_a_keypad_still_being_drawn_counts(self):
        """Two keys short is still a lock. Waiting for all ten is how the
        picture gets taken during the frame the app is laying out."""
        assert feed.keypad_on_screen(self._dumped("01234567")) is True

    def test_a_handful_of_numbered_buttons_is_not_a_keypad(self):
        assert feed.keypad_on_screen(self._dumped("0123")) is False

    def test_numbers_that_are_not_buttons_are_not_a_keypad(self):
        """A schedule is full of digits. Only BUTTONS carrying a single digit
        as their whole text make a keypad."""
        rows = "".join(
            f'<node index="{i}" text="{i}" class="android.widget.TextView" '
            f'bounds="[0,{i * 60}][720,{i * 60 + 50}]"/>' for i in range(10))
        assert feed.keypad_on_screen(f"<hierarchy>{rows}</hierarchy>") is False

    def test_a_missing_hierarchy_is_not_a_keypad(self):
        assert feed.keypad_on_screen(None) is False
        assert feed.keypad_on_screen("") is False

    def test_the_keypad_outranks_the_atlas(self):
        """The atlas maps `dashboardactivity` to "home", and it is right about
        the activity. It is wrong about this screen, and calling a lock screen
        "home" is worse than the "unknown" it replaced."""
        assert feed.screen_for(self.DASHBOARD) == "home"
        assert feed.screen_for(self.DASHBOARD, self._dumped()) == "login"

    def test_the_real_dashboard_is_still_home(self):
        tree = ('<hierarchy rotation="0"><node index="0" text="Visitas" '
                'class="android.widget.TextView" bounds="[0,0][720,80]"/>'
                '</hierarchy>')
        assert feed.screen_for(self.DASHBOARD, tree) == "home"

    def test_another_app_showing_digits_is_not_second_guessed(self):
        """The calculator is not a care app and its keys are not a lock."""
        calc = "com.android.calculator2/.Calculator"
        assert feed.screen_for(calc, self._dumped()) == "unknown"

    def test_the_picture_is_refused_over_a_keypad(self):
        with patch.object(feed, "window_state",
                          return_value=(self.DASHBOARD, True, "")):
            png, _, reason = feed.capture(hierarchy=self._dumped())
        assert png is None
        assert reason == feed.LOGIN_ACTIVITY

    def test_and_the_words_are_let_through_so_the_macro_can_answer(self):
        """The refusal is what makes the screen disclosable: no photograph,
        but the page and the auth trigger both get to see there is a lock."""
        assert feed.text_is_disclosable(feed.LOGIN_ACTIVITY) is True


class TestSidewaysAtAnyResolution:
    """`landscape` turns the console's phone preview a quarter turn and puts
    the app page in its wide layout, so getting it wrong is not subtle.

    It used to be decided by hunting for an x-coordinate of 800 or more,
    which reads as "sideways" only because a 720-wide screen has none of
    those in portrait. Every portrait screen on a 1080-wide phone carries
    them, so the rule would have fired on all of them — every screen
    rendered sideways, always. Found by auditing before swapping the device
    rather than afterwards.
    """

    @staticmethod
    def _tree(width, height):
        return (f'<hierarchy rotation="0"><node bounds="[0,0][{width},{height}]">'
                f'<node bounds="[10,20][{width - 10},{height // 2}]"/>'
                f'</node></hierarchy>')

    @pytest.mark.parametrize("width,height", [(720, 1600), (1080, 2400),
                                              (1440, 3120)])
    def test_portrait_is_portrait_on_every_panel(self, width, height):
        assert feed._looks_landscape(self._tree(width, height)) is False

    @pytest.mark.parametrize("width,height", [(1600, 720), (2400, 1080)])
    def test_sideways_is_sideways_on_every_panel(self, width, height):
        assert feed._looks_landscape(self._tree(width, height)) is True

    def test_the_phone_this_was_written_on_still_reads_the_same(self):
        """The 720x1600 device in Florida must not change behaviour."""
        assert feed._looks_landscape(self._tree(720, 1600)) is False
        assert feed._looks_landscape(self._tree(1600, 720)) is True

    def test_a_1080_portrait_screen_is_the_case_that_used_to_break(self):
        """Named on its own because it is the whole reason for the change:
        the old rule matched any x past 800, and this tree is full of them."""
        tree = self._tree(1080, 2400)
        assert "[1070," in tree                     # coordinates past 800
        assert feed._looks_landscape(tree) is False

    def test_nothing_to_read_is_not_sideways(self):
        for empty in (None, "", "<hierarchy/>"):
            assert feed._looks_landscape(empty) is False

    def test_the_extent_is_the_widest_and_tallest_the_tree_mentions(self):
        assert feed.screen_extent(self._tree(720, 1600)) == (720, 1600)
        assert feed.screen_extent(None) == (0, 0)


class TestTheTreeIsWrittenDownWithoutWhatWasTyped:
    """Mapping a screen needs the document under the reflow, and both ways of
    asking the phone for it fight the feed: `uiautomator dump` spawns a fresh
    instrumentation competing for the same service (it wedged Appium hard
    enough to need a restart during setup work), and calling `read_hierarchy`
    from a second process opens a SECOND Appium session, which UiAutomator2
    does not allow.

    The feed already holds the tree every time it reads one. Writing it down
    means every other reader gets it from a file.
    """

    TYPED = (
        '<hierarchy rotation="0">'
        '<node index="0" text="Enter your code" class="android.widget.TextView"'
        ' bounds="[24,1040][100,1052]"/>'
        '<node index="1" text="604820" resource-id="app:id/code"'
        ' class="android.widget.EditText" bounds="[13,1042][707,1087]"/>'
        '<node index="2" text="hunter2" class="android.widget.EditText"'
        ' password="true" bounds="[13,1100][707,1145]"/>'
        '</hierarchy>')

    def test_a_typed_code_never_reaches_the_file(self):
        hushed = feed.hush_typed(self.TYPED)
        assert "604820" not in hushed
        assert "hunter2" not in hushed

    def test_but_the_node_survives_whole(self):
        """Blanking the value, not dropping the field: its id and its bounds
        are how the page aims at it."""
        hushed = feed.hush_typed(self.TYPED)
        assert 'resource-id="app:id/code"' in hushed
        assert 'bounds="[13,1042][707,1087]"' in hushed
        assert 'class="android.widget.EditText"' in hushed

    def test_the_labels_around_it_are_left_alone(self):
        """Only what was TYPED is withheld. The app's own words are the whole
        reason to read the tree."""
        assert "Enter your code" in feed.hush_typed(self.TYPED)

    def test_nothing_to_hush_is_not_an_error(self):
        assert feed.hush_typed(None) == ""
        assert feed.hush_typed("") == ""

    def test_the_frame_writes_it_beside_the_screen(self, tmp_path):
        with patch.object(feed, "capture",
                          return_value=(None, HOME, feed.NO_FOCUS)):
            feed.write_frame(tmp_path / "last-screen.jpg",
                             hierarchy=self.TYPED)
        written = (tmp_path / feed.HIERARCHY_NAME).read_text(encoding="utf-8")
        assert "Enter your code" in written
        assert "604820" not in written

    def test_a_frame_is_never_lost_because_the_tree_could_not_be_written(
            self, tmp_path):
        """It is a convenience for whoever is working on the machine. A full
        disk must cost the convenience, not the mirror."""
        with patch.object(feed, "capture",
                          return_value=(None, HOME, feed.NO_FOCUS)), \
                patch.object(feed, "hush_typed",
                             side_effect=OSError("no space")):
            try:
                feed.write_frame(tmp_path / "last-screen.jpg",
                                 hierarchy=self.TYPED)
            except OSError:
                pytest.fail("a write failure must not reach the caller")


class TestTheDensityIsHandedBack:
    """`wm density` is a DISPLAY setting, and nothing ever put it back.

    Android has no per-app density — the code's own comment said so in
    passing: "each application re-lays-out every app on the phone". So while
    a care app was in front the launcher, Settings, the dialer and every
    system dialog were laid out at 84 as well, and leaving the app left the
    phone shrunk.

    That is the opposite of what the density is for. The caregiver has to be
    able to drive the phone by hand when something goes wrong, and buttons she
    cannot press are what make that impossible — in exactly the moment it
    matters most.
    """

    def setup_method(self):
        feed._left_at[0] = 0.0

    # `prefs` is imported INSIDE the function under test, so the name to
    # patch is the module's own, not an attribute of `feed` — which does not
    # have one, and said so the first time this ran.

    def test_a_care_app_still_gets_its_density(self):
        assert feed._density_wanted(
            "com.hhaexchange.uma/.MainActivity") == 172
        assert feed._density_wanted(
            "com.inmyteam.inmyteam/.MainActivity") == 105

    def test_leaving_hands_it_back(self, monkeypatch):
        monkeypatch.setattr(prefs, "global_density", lambda: None)
        feed._density_wanted("com.android.launcher/.Home")     # arms the dwell
        feed._left_at[0] -= feed.HANDBACK_DWELL + 1
        assert feed._density_wanted(
            "com.android.launcher/.Home") == feed.DENSITY_RESET

    def test_it_does_not_thrash_on_the_way_through(self, monkeypatch):
        """Switching apps crosses the launcher, and a re-layout of every app
        on the phone is not free. Done on each crossing it would thrash the
        device during the exact sequences that matter."""
        monkeypatch.setattr(prefs, "global_density", lambda: None)
        assert feed._density_wanted("com.android.launcher/.Home") is None
        assert feed._density_wanted("com.android.launcher/.Home") is None

    def test_going_back_into_an_app_cancels_the_hand_back(self, monkeypatch):
        monkeypatch.setattr(prefs, "global_density", lambda: None)
        feed._density_wanted("com.android.launcher/.Home")
        assert feed._left_at[0]
        feed._density_wanted("com.inmyteam.inmyteam/.MainActivity")
        assert feed._left_at[0] == 0.0

    def test_a_deliberate_global_override_still_wins(self, monkeypatch):
        """A person setting a density for the whole phone is a person saying
        what they want, and the hand-back must not argue with them."""
        monkeypatch.setattr(prefs, "global_density", lambda: 120)
        assert feed._density_wanted("com.android.launcher/.Home") == 120

    def test_the_reset_lets_the_panel_decide(self):
        """`wm density reset` exists because the panel decides. The hand-back
        must ask for that rather than name a number of its own."""
        from conftest import strip_py_comments

        source = strip_py_comments(
            Path("src/apt_log/feed.py").read_text(encoding="utf-8"))
        body = source.split("def _watch_density(", 1)[1]
        assert '"wm", "density", "reset"' in body

    def test_the_hand_back_happens_once_not_every_frame(self):
        """IT RESET THE PHONE EVERY FRAME, and the log said so: one "density
        handed back" line every 1.5 seconds for a minute, each one
        re-laying-out every app on the phone while she was using it.

        `_density_now` records what was last ASKED FOR — it is only ever
        compared against the next `want`. The hand-back stored the panel's
        physical density instead, so the guard compared 450 against
        DENSITY_RESET, found them different, and handed back again. And
        again. Reading the panel back was not wrong about the panel; it was
        the wrong thing to put in a slot meaning "the last thing this asked
        for".
        """
        feed._density_now[0] = 0
        feed._left_at[0] = 0.0
        calls = []

        def adb(a, *_, **__):
            calls.append(a)
            m = MagicMock(returncode=0)
            m.stdout = b"Physical density: 450\nOverride density: 300\n"
            return m

        import apt_log.macros as macros_mod

        class S:
            state = "idle"

        with patch.object(feed, "_adb", side_effect=adb), \
             patch.object(macros_mod, "read_status", return_value=S()), \
             patch.object(macros_mod.SCAN_ACTIVE, "is_set",
                          return_value=False), \
             patch.object(prefs, "global_density", return_value=None):
            # Out of the care apps, and past the dwell, so the hand-back is due.
            feed._watch_density("com.android.settings/.Settings")
            feed._left_at[0] -= feed.HANDBACK_DWELL + 1
            for _ in range(6):
                feed._watch_density("com.android.settings/.Settings")
        resets = [c for c in calls if c[:4] == ["shell", "wm", "density",
                                                "reset"]]
        assert len(resets) == 1, f"handed back {len(resets)} times"

    def test_a_care_app_after_a_hand_back_is_applied_again(self):
        """The sentinel must not make the next real density look already-set."""
        feed._density_now[0] = feed.DENSITY_RESET
        feed._left_at[0] = 0.0
        calls = []

        def adb(a, *_, **__):
            calls.append(a)
            m = MagicMock(returncode=0)
            m.stdout = b"Physical density: 450\nOverride density: 450\n"
            return m

        import apt_log.macros as macros_mod

        class S:
            state = "idle"

        with patch.object(feed, "_adb", side_effect=adb), \
             patch.object(macros_mod, "read_status", return_value=S()), \
             patch.object(macros_mod.SCAN_ACTIVE, "is_set",
                          return_value=False):
            feed._watch_density("com.inmyteam.inmyteam/.MainActivity")
        assert ["shell", "wm", "density", "105"] in calls

    def test_an_unreadable_physical_density_is_minus_one(self, monkeypatch):
        """Not a plausible default: every caller compares it against a wanted
        value, and an invented number would compare EQUAL to something and
        silently skip a change that was needed."""
        def boom(*a, **k):
            raise OSError("no device")

        monkeypatch.setattr(feed, "_adb", boom)
        assert feed._physical_density() == -1


class TestDensityIsMeasuredPerScreen:
    """The blanket value was tuned for a different phone and a different job.

    84 was chosen against the OLD handset — 720x1600 — and the comment beside
    it says what for: HHAeXchange+'s SIX-DAY schedule, which "only fits one
    capture that small". This phone is 1080x2340 at a physical density of 450,
    and what has to fit now is TODAY, fanned out in one view.

    Measured live, reading how far down the page the next day's header lands
    with today holding two visits and the first expanded:

        450 (physical) .. today runs off the screen, one card visible
        400 ............. still off
        340 ............. next day at 1725 — fits
        300 ............. next day at 1538 — fits, ~800px spare
        200 ............. next day at  973 — fits, and small
    """

    def setup_method(self):
        feed._left_at[0] = 0.0

    def test_the_schedule_screen_uses_its_measured_value(self):
        assert feed._density_wanted(
            "com.hhaexchange.uma/com.hhaexchange.carehub.ui.activities"
            ".HomeActivity") == 130

    def test_the_whole_week_fits_one_capture(self):
        """THE BAR MOVED, and it moved from type size to captures.

        340 was once the ceiling, on the reading that what had to fit was
        today, legible on the handset. Then: expand today's cards before the
        page counts as loaded, and render every block of the week. Those two
        together are a question about ONE CAPTURE, because everything past
        the first costs a scroll and this app forgets an opened card the
        moment it scrolls out of view.

        Measured live with today's two visits expanded: 200 and 160 each
        needed two captures; 130 needed one. The portal draws its own type
        from the tree, so this is not a legibility trade — it decides how
        much arrives at once.
        """
        for _mark, value in feed.PAGE_DENSITY["com.hhaexchange.uma"]:
            assert value <= 130

    def test_an_unmeasured_screen_is_left_alone(self):
        """Applying an unmeasured value everywhere is the thing this table
        exists to stop. An unmeasured uma page keeps the app's own value."""
        assert feed._density_wanted(
            "com.hhaexchange.uma/com.hhaexchange.carehub.ui.activities"
            ".VisitActivity") == feed.APP_DENSITY["com.hhaexchange.uma"]

    def test_another_app_is_untouched(self):
        assert feed._density_wanted(
            "com.inmyteam.inmyteam/.MainActivity") == 105

    def test_the_match_ignores_case(self):
        """The key is a fragment of the activity, and the activity's own
        capitalisation is the app's business."""
        assert feed._density_wanted(
            "com.hhaexchange.uma/HOMEACTIVITY") == 130

    def test_a_page_override_still_outranks_it(self, monkeypatch):
        """Somebody setting a value for this page means it for this page —
        the tuned table is a default, not a ceiling on her judgement."""
        monkeypatch.setattr(prefs, "density_for",
                            lambda app, page="", **k: 210)
        assert feed._density_wanted(
            "com.hhaexchange.uma/HomeActivity") == 210

    def test_an_app_override_hides_the_measurement_and_that_is_by_design(
            self, monkeypatch):
        """Why the app value moved into the code rather than staying an
        override.

        An APP-level override outranks this table too — the ordering does not
        care that one is broader than the other — so while uma carried a
        stored 172, the schedule screen went on being laid out at 172 and the
        measurement reached the phone as nothing at all. The fix is not to
        reorder the steps (a person's setting should win) but to stop keeping
        the app's own value somewhere that outranks its pages."""
        # An app-level override answers for every page of that app, which is
        # exactly how `density_for` resolves one: page key first, app key
        # next, and the app key catches everything.
        monkeypatch.setattr(prefs, "density_for", lambda app, page="", **k: 172)
        assert feed._density_wanted(
            "com.hhaexchange.uma/HomeActivity") == 172
        monkeypatch.setattr(prefs, "density_for", lambda app, page="", **k: None)
        assert feed._density_wanted(
            "com.hhaexchange.uma/HomeActivity") == 130


class TestAStaticKeepsItsName:
    """The id is IDENTITY, and it used to be dropped whenever a node had
    words — carried only for textless nodes, because the only reader was the
    mark table.

    The stitch identifies an item by (class, id, text, left, right), so a
    week of one patient's visits arrived as six rows with ONE identity, and
    two of them were deduped out of the portal. See TestARepeatedNameIsNotOneName.
    """

    def _xml(self, rid, text):
        return ('<node class="android.widget.TextView" '
                f'resource-id="com.hhaexchange.uma:id/{rid}" text="{text}" '
                'clickable="false" bounds="[69,1800][567,1868]"/>')

    def test_a_row_with_words_keeps_its_id(self):
        got = feed.statics(self._xml("schedule_screen_accordion_title_aaa",
                                     "NIEVES C MASTRAPA"))
        assert got and got[0]["rid"] == "schedule_screen_accordion_title_aaa"
        assert got[0]["txt"] == "NIEVES C MASTRAPA"

    def test_two_rows_saying_the_same_thing_are_told_apart(self):
        rows = feed.statics(
            self._xml("schedule_screen_accordion_title_aaa", "NIEVES C MASTRAPA")
            + self._xml("schedule_screen_accordion_title_bbb", "NIEVES C MASTRAPA"))
        assert len({r["rid"] for r in rows}) == 2

    def test_a_nameless_row_still_arrives(self):
        """Most nodes have no id at all, and they must still be published —
        with an empty one rather than none, so every reader sees one shape."""
        got = feed.statics('<node class="android.widget.TextView" '
                           'text="agosto 29" clickable="false" '
                           'bounds="[24,245][553,313]"/>')
        assert got and got[0]["txt"] == "agosto 29"
        assert got[0].get("rid", "") == ""
