"""The fast signature path, and above all its way back.

scrcpy_ink draws a stored signature through scrcpy's control socket instead
of Appium. What these tests pin is less the drawing than the safety around
it: the switch, the in-replay fallback to the old path, the automatic trip
after repeated failures and the one notice it sends. Nothing here starts a
server, touches adb or reaches the network: conftest sets the switch to the
old path and blocks the download for every test, and these tests turn the
fast path on themselves against doubles.
"""

from __future__ import annotations

import hashlib
import json
import struct
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, ImageDraw

from apt_log import scrcpy_ink as ink
from apt_log import sign

BOUNDS = [100, 200, 500, 400]


# --------------------------------------------------------------- the message

class TestTouchMessage:
    def test_layout_matches_scrcpys_own_serializer(self):
        """The expected bytes are copied from scrcpy v5.0.1's
        app/tests/test_control_msg_serialize.c, so the layout is the one the
        server reads, not one this project guessed at."""
        expected = bytes([
            0x02, 0x00,
            0x12, 0x34, 0x56, 0x78, 0x87, 0x65, 0x43, 0x21,
            0x00, 0x00, 0x00, 0x64, 0x00, 0x00, 0x00, 0xc8,
            0x04, 0x38, 0x07, 0x80,
            0xff, 0xff,
            0x00, 0x00, 0x00, 0x01,
            0x00, 0x00, 0x00, 0x01,
        ])
        packed = ink._TOUCH.pack(2, 0, 0x1234567887654321, 100, 200,
                                 1080, 1920, 0xFFFF, 1, 1)
        assert packed == expected

    def test_a_touch_is_a_finger_with_no_buttons(self):
        msg = ink.touch_message(ink.ACTION_DOWN, 100, 200, 1080, 2340)
        assert len(msg) == 32
        kind, action, pointer, x, y, w, h, p, ab, b = ink._TOUCH.unpack(msg)
        assert (kind, action, x, y, w, h) == (2, 0, 100, 200, 1080, 2340)
        # -2 is scrcpy's generic finger; -1 would be a mouse.
        assert pointer == -2
        assert p == 0xFFFF and ab == 0 and b == 0

    def test_lift_has_no_pressure(self):
        msg = ink.touch_message(ink.ACTION_UP, 1, 1, pressure=0.0)
        assert struct.unpack(">H", msg[22:24])[0] == 0


# ----------------------------------------------------------------- the switch

class TestSwitch:
    def test_a_fresh_install_is_on(self, tmp_path):
        assert ink.engine(tmp_path / "none.json") == ink.SCRCPY

    def test_an_unreadable_file_is_the_default(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        assert ink.engine(path) == ink.SCRCPY
        path.write_text(json.dumps({"engine": "warp"}))
        assert ink.engine(path) == ink.SCRCPY

    def test_two_failures_in_a_row_switch_it_off_and_say_so_once(
            self, tmp_path):
        path = tmp_path / "e.json"
        sent = []
        assert ink.note_failure("one", path, notify=sent.append) is False
        assert ink.engine(path) == ink.SCRCPY and sent == []
        assert ink.note_failure("two", path, notify=sent.append) is True
        assert ink.engine(path) == ink.APPIUM
        assert len(sent) == 1 and "two" in sent[0]
        # Already off: further failures notify nobody.
        assert ink.note_failure("three", path, notify=sent.append) is False
        assert len(sent) == 1
        assert ink.state(path)["tripped"] == "two"

    def test_a_success_between_failures_resets_the_count(self, tmp_path):
        path = tmp_path / "e.json"
        sent = []
        ink.note_failure("one", path, notify=sent.append)
        ink.note_success("ok", path)
        ink.note_failure("two", path, notify=sent.append)
        assert ink.engine(path) == ink.SCRCPY and sent == []

    def test_a_person_choosing_clears_the_trip(self, tmp_path):
        path = tmp_path / "e.json"
        ink.note_failure("a", path, notify=lambda m: None)
        ink.note_failure("b", path, notify=lambda m: None)
        assert ink.set_engine("scrcpy", path) == ink.SCRCPY
        doc = ink.state(path)
        assert doc["fails"] == 0 and doc["tripped"] == ""

    def test_an_unknown_name_means_the_old_path(self, tmp_path):
        assert ink.set_engine("rm -rf", tmp_path / "e.json") == ink.APPIUM

    def test_the_trip_uses_the_real_notifier_by_default(self, tmp_path):
        path = tmp_path / "e.json"
        with patch("apt_log.notify.send") as send:
            ink.note_failure("a", path)
            ink.note_failure("b", path)
        send.assert_called_once()


# ------------------------------------------------------------------ the server

class TestServerJar:
    def test_a_jar_that_does_not_match_is_refused(self, tmp_path):
        with pytest.raises(ink.InkUnavailable):
            ink.ensure_server(tmp_path / "jar", fetch=lambda url: b"evil")
        assert not (tmp_path / "jar").exists()

    def test_a_matching_jar_is_kept_and_reused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ink, "SERVER_SHA256",
                            hashlib.sha256(b"jar").hexdigest())
        calls = []

        def fetch(url):
            calls.append(url)
            return b"jar"

        path = ink.ensure_server(tmp_path / "jar", fetch=fetch)
        assert path.read_bytes() == b"jar"
        ink.ensure_server(tmp_path / "jar", fetch=fetch)
        assert len(calls) == 1
        assert calls[0].startswith("https://github.com/Genymobile/scrcpy/")

    def test_no_network_is_unavailable_not_a_crash(self, tmp_path):
        # conftest blocks urlopen for every test.
        with pytest.raises(ink.InkUnavailable):
            ink.ensure_server(tmp_path / "jar")


# ------------------------------------------------------------------ the stroke

class _Sock:
    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(ink._TOUCH.unpack(data))


def _pen():
    pen = ink.Pen(size=(1080, 2340))
    pen.sock = _Sock()
    return pen


class TestStroke:
    def test_same_shape_as_the_appium_stroke(self):
        pen = _pen()
        pen.stroke([(10, 10), (12, 30), (14, 50)], sign.CLAIM_NUDGE,
                   sign.DOT_MARK, sign.MOVE_MS, sleep=lambda s: None)
        moves = [(a, x, y) for _k, a, _p, x, y, *_r in pen.sock.sent]
        assert moves[0] == (ink.ACTION_DOWN, 10, 10)
        # Sideways first, toward the stroke's travel, then back.
        assert moves[1] == (ink.ACTION_MOVE, 10 + sign.CLAIM_NUDGE, 10)
        assert moves[2] == (ink.ACTION_MOVE, 10, 10)
        assert moves[3:5] == [(ink.ACTION_MOVE, 12, 30),
                              (ink.ACTION_MOVE, 14, 50)]
        assert moves[-1] == (ink.ACTION_UP, 14, 50)

    def test_a_leftward_stroke_leads_left(self):
        pen = _pen()
        pen.stroke([(50, 10), (20, 30)], sign.CLAIM_NUDGE, sign.DOT_MARK,
                   sign.MOVE_MS, sleep=lambda s: None)
        assert pen.sock.sent[1][3] == 50 - sign.CLAIM_NUDGE

    def test_a_dot_gets_a_mark_not_a_dash(self):
        pen = _pen()
        pen.stroke([(40, 40)], sign.CLAIM_NUDGE, sign.DOT_MARK, sign.MOVE_MS,
                   sleep=lambda s: None)
        assert pen.sock.sent[1][3] == 40 + sign.DOT_MARK

    def test_it_is_paced_not_dumped(self):
        """A pad that sets width from speed draws hairlines from a stroke
        sent all at once."""
        pen = _pen()
        now = [0.0]
        slept = []

        def sleep(s):
            slept.append(s)
            now[0] += s

        pen.stroke([(0, 0)] + [(i, i) for i in range(1, 21)],
                   sign.CLAIM_NUDGE, sign.DOT_MARK, sign.MOVE_MS,
                   sleep=sleep, clock=lambda: now[0])
        assert now[0] >= 22 * sign.MOVE_MS / 1000.0

    def test_a_closed_pen_refuses(self):
        with pytest.raises(ink.InkUnavailable):
            ink.Pen().touch(ink.ACTION_DOWN, 1, 1)


# --------------------------------------------------------------- verification

A = [(120, 250), (200, 260), (300, 270), (400, 280)]
B = [(150, 350), (250, 340), (350, 330), (450, 320)]


def _canvas(*strokes):
    img = Image.new("L", (BOUNDS[2] - BOUNDS[0], BOUNDS[3] - BOUNDS[1]), 255)
    d = ImageDraw.Draw(img)
    for s in strokes:
        d.line([(x - BOUNDS[0], y - BOUNDS[1]) for x, y in s], fill=0,
               width=4)
    return img


class TestVerification:
    def test_each_stroke_is_judged_along_its_own_points(self):
        before = _canvas()
        assert ink.missing(before, _canvas(A, B), [A, B], BOUNDS) == []
        assert ink.missing(before, _canvas(A), [A, B], BOUNDS) == [1]
        assert ink.missing(before, _canvas(), [A, B], BOUNDS) == [0, 1]

    def test_ink_that_was_already_there_does_not_count(self):
        """A printed line under the pad is dark before and after."""
        baseline = _canvas(B)
        assert ink.missing(baseline, baseline, [B], BOUNDS) == [0]

    def test_a_dot(self):
        dot = [(300, 300)]
        assert ink.missing(_canvas(), _canvas([(300, 300), (302, 300)]),
                           [dot], BOUNDS) == []
        assert ink.missing(_canvas(), _canvas(), [dot], BOUNDS) == [0]


# --------------------------------------------------- the replay and its way back

class _FakePen:
    """Stands in for ink.Pen. `drawn` is shared so the screen double can
    paint whatever was sent."""

    def __init__(self, serial=None, size=(0, 0), fail_open=None,
                 fail_at=None, deaf=()):
        self.fail_open = fail_open
        self.fail_at = fail_at
        self.deaf = set(deaf)
        self.drawn = []
        self.closed = False
        self.n = 0

    def open(self):
        if self.fail_open:
            raise ink.InkUnavailable(self.fail_open)
        return self

    def close(self):
        self.closed = True

    def stroke(self, path, *_a, **_k):
        self.n += 1
        if self.fail_at is not None and self.n > self.fail_at:
            raise ink.InkUnavailable("socket closed")
        if tuple(path[0]) not in self.deaf:
            self.drawn.append(path)


@pytest.fixture
def fast(monkeypatch, tmp_path):
    """The switch ON, no real pauses, and a screen that shows what the
    fake pen drew."""
    ink.set_engine(ink.SCRCPY)
    monkeypatch.setattr(sign.time, "sleep", lambda s: None)
    box = {}

    def make(**kw):
        pen = _FakePen(**kw)
        box["pen"] = pen
        monkeypatch.setattr(ink, "Pen", lambda serial=None, size=(0, 0): pen)
        monkeypatch.setattr(ink, "grab",
                            lambda bounds, serial=None: _canvas(*pen.drawn))
        return pen

    return make


class TestEngineChoice:
    def test_switch_off_is_exactly_the_old_path(self):
        with patch.object(sign, "_perform", return_value=(0, 9, "+9/1")) as p:
            out = sign._engine_perform("drv", [A], bounds=BOUNDS)
        p.assert_called_once_with("drv", [A], bounds=BOUNDS, serial=None)
        assert out == (0, 9, "+9/1", "appium")

    def test_no_canvas_bounds_is_the_old_path(self, fast):
        fast()
        with patch.object(sign, "_perform", return_value=(None, None, "")) as p:
            sign._engine_perform("drv", [A], bounds=None)
        p.assert_called_once()

    def test_fast_path_draws_everything_and_never_calls_appium(self, fast):
        pen = fast()
        with patch.object(sign, "_perform") as p:
            was, now, marks, note = sign._engine_perform(
                "drv", [A, B], bounds=BOUNDS)
        p.assert_not_called()
        assert pen.drawn == [A, B] and pen.closed
        assert marks == "s,s" and note.startswith("scrcpy ")
        assert was == 0 and now > 0
        assert ink.state()["fails"] == 0

    def test_a_server_that_will_not_start_falls_back_whole(self, fast):
        fast(fail_open="push failed")
        with patch.object(sign, "_perform",
                          return_value=(0, 9, "+9/1,+9/1")) as p:
            out = sign._engine_perform("drv", [A, B], bounds=BOUNDS)
        p.assert_called_once_with("drv", [A, B], bounds=BOUNDS, serial=None)
        assert out[3].startswith("appium(fallback:")
        assert ink.state()["fails"] == 1

    def test_a_stroke_that_never_lands_is_finished_by_appium_alone(
            self, fast):
        """Retried once on the fast path, then handed over — only that
        stroke, so nothing that landed is drawn twice."""
        pen = fast(deaf=[B[0]])
        with patch.object(sign, "_perform") as p, \
             patch.object(sign, "_canvas_ink", return_value=500):
            _was, _now, marks, note = sign._engine_perform(
                "drv", [A, B], bounds=BOUNDS)
        p.assert_called_once_with("drv", [B], bounds=BOUNDS, serial=None)
        assert pen.n == 3          # A, B, B again
        assert marks == "s,a" and note.startswith("scrcpy+appium(")
        assert ink.state()["fails"] == 1

    def test_a_retry_that_lands_is_a_success(self, fast, monkeypatch):
        pen = fast()
        first = {"B": True}
        real = pen.stroke

        def flaky(path, *a, **k):
            if path is B and first["B"]:
                first["B"] = False
                pen.n += 1
                return
            real(path, *a, **k)

        monkeypatch.setattr(pen, "stroke", flaky)
        with patch.object(sign, "_perform") as p:
            _was, _now, marks, _note = sign._engine_perform(
                "drv", [A, B], bounds=BOUNDS)
        p.assert_not_called()
        assert marks == "s,s2"
        assert ink.state()["fails"] == 0

    def test_breaking_mid_signature_redraws_only_what_is_missing(self, fast):
        pen = fast(fail_at=1)
        with patch.object(sign, "_perform") as p, \
             patch.object(sign, "_canvas_ink", return_value=500):
            sign._engine_perform("drv", [A, B], bounds=BOUNDS)
        assert pen.drawn == [A] and pen.closed
        p.assert_called_once_with("drv", [B], bounds=BOUNDS, serial=None)

    def test_breaking_blind_redraws_from_the_broken_stroke_on(
            self, fast, monkeypatch):
        pen = fast(fail_at=1)
        shots = iter([_canvas()])
        monkeypatch.setattr(ink, "grab",
                            lambda bounds, serial=None: next(shots, None))
        with patch.object(sign, "_perform") as p, \
             patch.object(sign, "_canvas_ink", return_value=None):
            sign._engine_perform("drv", [A, B], bounds=BOUNDS)
        assert pen.drawn == [A]
        p.assert_called_once_with("drv", [B], bounds=BOUNDS, serial=None)

    def test_an_unreadable_canvas_is_not_counted_against_it(
            self, fast, monkeypatch):
        fast()
        monkeypatch.setattr(ink, "grab", lambda bounds, serial=None: None)
        with patch.object(sign, "_perform", return_value=(None, None, "")) as p:
            sign._engine_perform("drv", [A], bounds=BOUNDS)
        p.assert_called_once()
        assert ink.state()["fails"] == 0

    def test_two_bad_signatures_turn_it_off_and_notify(self, fast):
        fast(fail_open="push failed")
        with patch.object(sign, "_perform", return_value=(0, 9, "")), \
             patch("apt_log.notify.send") as send:
            sign._engine_perform("drv", [A], bounds=BOUNDS)
            sign._engine_perform("drv", [A], bounds=BOUNDS)
            # The third goes straight to the old path.
            out = sign._engine_perform("drv", [A], bounds=BOUNDS)
        assert ink.engine() == ink.APPIUM
        send.assert_called_once()
        assert out[3] == "appium"


# ------------------------------------------------------------------ the portal

class TestDebugSwitch:
    @pytest.fixture
    def client(self, tmp_path):
        from fastapi.testclient import TestClient

        from apt_log.ui import state as state_mod
        from apt_log.ui.app import app

        (tmp_path / "screen.json").write_text("{}", encoding="utf-8")
        def no_adb(*_a, **_k):
            raise OSError("no adb in tests")

        with patch.object(state_mod, "STATE_DIR", tmp_path), \
             patch("apt_log.ui.phonesettings.subprocess.run", no_adb):
            yield TestClient(app)

    def test_one_press_turns_it_off_and_on(self, client):
        r = client.post("/debug/sign-engine", data={"engine": "appium"},
                        follow_redirects=False)
        assert r.status_code == 303 and "saved=engine" in r.headers["location"]
        assert ink.engine() == ink.APPIUM
        client.post("/debug/sign-engine", data={"engine": "scrcpy"},
                    follow_redirects=False)
        assert ink.engine() == ink.SCRCPY

    def test_the_page_shows_the_switch_and_why_it_tripped(self, client):
        ink.set_engine(ink.SCRCPY)
        ink.note_failure("push failed", notify=lambda m: None)
        ink.note_failure("push failed", notify=lambda m: None)
        body = client.get("/debug").text
        assert 'action="/debug/sign-engine"' in body
        assert "push failed" in body
