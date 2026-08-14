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
        with patch.object(feed, "current_focus", return_value=HOME), \
             patch.object(feed, "password_field_on_screen", return_value=True):
            png, _, reason = feed.capture()
        assert png is None
        assert "password" in reason

    def test_captures_when_the_hierarchy_is_unreadable_but_activity_is_safe(self):
        """The documented weak point: UiAutomator2 holds the dump service during
        an Appium session, which is exactly when watching matters most."""
        with patch.object(feed, "current_focus", return_value=HOME), \
             patch.object(feed, "password_field_on_screen", return_value=None), \
             patch.object(feed, "_adb") as adb:
            adb.return_value.returncode = 0
            adb.return_value.stdout = PNG
            png, _, reason = feed.capture()
        assert png == PNG and reason == ""

    def test_a_refused_capture_is_reported_not_silent(self):
        with patch.object(feed, "current_focus", return_value=HOME), \
             patch.object(feed, "password_field_on_screen", return_value=None), \
             patch.object(feed, "_adb") as adb:
            adb.return_value.returncode = 0
            adb.return_value.stdout = b""
            png, _, reason = feed.capture()
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
