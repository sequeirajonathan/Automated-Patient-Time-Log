"""The mirror feed, and the screenshots it refuses to take.

Every test here is about the refusals. Capturing a screen is easy; the reason
this module needed writing at all is that it runs outside the process holding
`capture.suppressed()`, so REQ-3's interlock had to be rebuilt from scratch.
"""

from __future__ import annotations

import io
import json
import time
from unittest.mock import patch

import pytest

from apt_log import feed

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
        with patch.object(feed, "window_state", return_value=("", True)):
            png, _, reason = feed.capture()
        assert png is None
        assert reason == feed.NO_FOCUS

    def test_refuses_on_a_login_screen(self):
        with patch.object(feed, "window_state", return_value=(SIGNIN, True)):
            png, _, reason = feed.capture()
        assert png is None
        assert reason == feed.LOGIN_ACTIVITY

    def test_refuses_when_a_password_field_is_anywhere_on_screen(self):
        """Anywhere, not merely focused. This runs on a timer with no idea what
        is about to happen, and the picture ends up on a web page."""
        with patch.object(feed, "window_state", return_value=(HOME, True)):
            png, _, reason = feed.capture(
                hierarchy='<node password="true" bounds="[0,0][1,1]"/>')
        assert png is None
        assert reason == feed.PASSWORD_FIELD

    def test_captures_when_the_hierarchy_is_unreadable_but_activity_is_safe(self):
        """The documented weak point: UiAutomator2 holds the dump service during
        an Appium session, which is exactly when watching matters most."""
        with patch.object(feed, "window_state", return_value=(HOME, True)), \
             patch.object(feed, "_adb") as adb:
            adb.return_value.returncode = 0
            adb.return_value.stdout = PNG
            png, _, reason = feed.capture(hierarchy=None)
        assert png == PNG and reason == feed.CAPTURED

    def test_a_refused_capture_is_reported_not_silent(self):
        with patch.object(feed, "window_state", return_value=(HOME, True)), \
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
             patch.object(feed, "window_state", return_value=("com.x/.Home", True)):
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
        with patch.object(feed, "window_state", return_value=(SIGNIN, True)), \
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
            focus, awake = feed.window_state()
        assert focus == HOME and awake is False

    def test_capture_refuses_while_the_display_is_off(self):
        """The focused app of a black screen is not what anyone is seeing."""
        with patch.object(feed, "window_state", return_value=(HOME, False)):
            png, focus, reason = feed.capture()
        assert png is None and reason == feed.NO_FOCUS

    def test_the_kept_sketch_survives_a_sleeping_read(self):
        """The junk a sleeping device returns must not wipe the memory the
        page shows as 'last on screen before it turned off'."""
        w = feed._Hierarchy(None, 1.0)
        good = '<node clickable="true" bounds="[0,0][10,10]"/>'
        assert w._accept(good, HOME, awake=False) is False


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
