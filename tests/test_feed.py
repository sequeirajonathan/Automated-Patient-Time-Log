"""The mirror feed, and the screenshots it refuses to take.

Every test here is about the refusals. Capturing a screen is easy; the reason
this module needed writing at all is that it runs outside the process holding
`capture.suppressed()`, so REQ-3's interlock had to be rebuilt from scratch.
"""

from __future__ import annotations

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
        with patch.object(feed, "current_focus", return_value=""):
            png, _, reason = feed.capture()
        assert png is None
        assert "focused window" in reason

    def test_refuses_on_a_login_screen(self):
        with patch.object(feed, "current_focus", return_value=SIGNIN):
            png, _, reason = feed.capture()
        assert png is None
        assert "credential" in reason

    def test_refuses_when_a_password_field_is_anywhere_on_screen(self):
        """Anywhere, not merely focused. This runs on a timer with no idea what
        is about to happen, and the picture ends up on a web page."""
        with patch.object(feed, "current_focus", return_value=HOME):
            png, _, reason = feed.capture(
                hierarchy='<node password="true" bounds="[0,0][1,1]"/>')
        assert png is None
        assert "password" in reason

    def test_captures_when_the_hierarchy_is_unreadable_but_activity_is_safe(self):
        """The documented weak point: UiAutomator2 holds the dump service during
        an Appium session, which is exactly when watching matters most."""
        with patch.object(feed, "current_focus", return_value=HOME), \
             patch.object(feed, "_adb") as adb:
            adb.return_value.returncode = 0
            adb.return_value.stdout = PNG
            png, _, reason = feed.capture(hierarchy=None)
        assert png == PNG and reason == ""

    def test_a_refused_capture_is_reported_not_silent(self):
        with patch.object(feed, "current_focus", return_value=HOME), \
             patch.object(feed, "_adb") as adb:
            adb.return_value.returncode = 0
            adb.return_value.stdout = b""
            png, _, reason = feed.capture(hierarchy=None)
        assert png is None and "does not allow capture" in reason


class TestScreenMapping:
    @pytest.mark.parametrize("focus,expected", [
        (HOME, "home"),
        (SIGNIN, "login"),
        ("x/.TodayScheduleActivity", "today"),
        ("x/.VisitDetailActivity", "visit"),
        ("x/.AppLaunchActivity", "startup"),
        ("x/.SomethingNew", "unknown"),
        ("", "unknown"),
    ])
    def test_maps_into_the_mirror_vocabulary(self, focus, expected):
        assert feed.screen_for(focus) == expected


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
                          return_value=(None, SIGNIN, "a credential can be typed")), \
             patch.object(feed.mirror_mod, "publish") as publish:
            feed.write_frame(shot)
        assert not shot.exists()
        assert publish.call_args.kwargs["screen"] == "login"
        assert publish.call_args.kwargs["step"] == "blocked"

    def test_no_partial_image_is_left_for_the_page_to_read(self, tmp_path):
        shot = tmp_path / "screen.png"
        with patch.object(feed, "capture", return_value=(PNG, HOME, "")), \
             patch.object(feed.mirror_mod, "publish"):
            feed.write_frame(shot)
        assert not (tmp_path / "screen.tmp").exists()


class TestRun:
    def test_one_bad_frame_does_not_stop_the_watcher(self, tmp_path):
        calls = []

        def flaky(_path, _serial=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("adb hiccup")
            return "ok"

        with patch.object(feed, "write_frame", side_effect=flaky), \
             patch.object(feed.time, "sleep"):
            feed.run(tmp_path / "s.png", interval=0, iterations=3)
        assert len(calls) == 3


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
        ' text="CARIDAD ROJAS" clickable="false" bounds="[0,100][720,160]" />'
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
        for secret in ("CARIDAD", "ROJAS", "Registrar", "Entrada"):
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
