"""The mirror feed, and the screenshots it refuses to take.

Every test here is about the refusals. Capturing a screen is easy; the reason
this module needed writing at all is that it runs outside the process holding
`capture.suppressed()`, so REQ-3's interlock had to be rebuilt from scratch.
"""

from __future__ import annotations

import io
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

        def flaky(_path, _serial=None, _hierarchy=None):
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
             patch.object(feed, "current_focus", return_value="com.x/.Home"):
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


class TestTap:
    """Tapping through the mirror.

    Every test here is a refusal. Landing a tap correctly is the easy half; the
    reason this exists is that a tap aimed at a screen that has since moved must
    not land at all.
    """

    XML = TestElements.XML

    def _current(self):
        return feed.elements(self.XML)

    def _fid(self):
        return feed.frame_id(self._current())

    def _button(self):
        return next(e for e in self._current() if e["rid"] == "btn_clock_in")

    def test_taps_the_centre_of_the_element(self):
        with patch.object(feed, "read_stable_hierarchy", return_value=self.XML), \
             patch.object(feed, "_adb") as adb:
            adb.return_value.returncode = 0
            out = feed.tap(self._fid(), self._button())
        sent = adb.call_args.args[0]
        assert sent[:3] == ["shell", "input", "tap"]
        assert sent[3:] == ["375", "774"]          # centre of [19,744][731,804]
        assert out["tapped"]["rid"] == "btn_clock_in"

    def test_refuses_when_the_target_is_gone(self):
        """The whole point. A blind coordinate would land on whatever occupies
        that spot now, which on this app can be the verification prompt."""
        moved_on = self.XML.replace('bounds="[19,744][731,804]"',
                                    'bounds="[19,900][731,960]"')
        with patch.object(feed, "read_stable_hierarchy", return_value=moved_on), \
             patch.object(feed, "_adb") as adb:
            with pytest.raises(feed.StaleAim):
                feed.tap(self._fid(), self._button())
            adb.assert_not_called()

    def test_refuses_an_element_that_was_never_on_screen(self):
        """A matching frame id proves the screen is unchanged. It does not prove
        the posted rectangle was ever part of it — and a tap at arbitrary
        coordinates is precisely what this is built not to be."""
        forged = {"rid": "btn_clock_in", "cls": "Button", "b": [0, 0, 720, 1600]}
        with patch.object(feed, "read_stable_hierarchy", return_value=self.XML), \
             patch.object(feed, "_adb") as adb:
            with pytest.raises((feed.NotOnScreen, feed.StaleAim)):
                feed.tap(self._fid(), forged)
            adb.assert_not_called()

    def test_refuses_when_the_screen_cannot_be_read(self):
        with patch.object(feed, "read_stable_hierarchy", return_value=None), \
             patch.object(feed, "_adb") as adb:
            with pytest.raises(feed.StaleAim):
                feed.tap(self._fid(), self._button())
            adb.assert_not_called()

    def test_an_element_matching_bounds_but_not_identity_is_refused(self):
        """Same rectangle, different widget — the layout was rebuilt underneath
        with something else in that spot."""
        impostor = dict(self._button())
        impostor["rid"] = "btn_cancel"
        with patch.object(feed, "read_stable_hierarchy", return_value=self.XML), \
             patch.object(feed, "_adb") as adb:
            with pytest.raises((feed.NotOnScreen, feed.StaleAim)):
                feed.tap(self._fid(), impostor)
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


class TestTapUsesTheOverlaySource:
    """The tap and the overlay must read the screen the same way.

    They did not: the feed published Appium's page_source while tap() verified
    against the adb dump. Different dialects, different element sets, so every
    tap came back stale — and took 20.6s doing it.
    """

    def test_tap_verifies_through_the_same_reader_as_the_feed(self):
        seen = []
        with patch.object(feed, "read_hierarchy",
                          side_effect=lambda *a: seen.append("shared") or TestElements.XML), \
             patch.object(feed, "read_stable_hierarchy") as adb_only, \
             patch.object(feed, "_adb") as adb:
            adb.return_value.returncode = 0
            els = feed.elements(TestElements.XML)
            btn = next(e for e in els if e["rid"] == "btn_clock_in")
            feed.tap(feed.frame_id(els), btn)
        assert seen == ["shared"]
        adb_only.assert_not_called()
