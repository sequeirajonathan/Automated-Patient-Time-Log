"""The H.264 stream, and the one thing it must refuse to record.

The JPEG mirror decides whether to capture before every single frame. A video
stream has no such decision point, so the credential guard has to be the stream
itself — it stops, and the picture freezes. That is the behaviour, not a bug, and
most of these tests exist to keep it.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

from apt_log import video

ANNEX_B = b"\x00\x00\x00\x01\x67\x64\x00\x29" + b"\xff" * 64


def _fake_proc(chunks, block: threading.Event | None = None):
    proc = MagicMock()
    queue = list(chunks)

    def read(_n):
        if queue:
            return queue.pop(0)
        if block is not None:
            block.wait(2)
        return b""

    proc.stdout.read.side_effect = read
    return proc


def _drain(streamer, seen, want=1, timeout=3.0):
    deadline = time.monotonic() + timeout
    while len(seen) < want and time.monotonic() < deadline:
        time.sleep(0.01)


class TestViewerLifecycle:
    def test_nothing_runs_until_someone_watches(self):
        """screenrecord cannot run twice at once, so it must not run for nobody."""
        with patch.object(video.Streamer, "_spawn") as spawn:
            s = video.Streamer(on_data=lambda _b: None)
            assert s.watching == 0
            spawn.assert_not_called()

    def test_the_first_viewer_starts_it_and_the_last_stops_it(self):
        seen = []
        with patch.object(video.Streamer, "_spawn",
                          return_value=_fake_proc([ANNEX_B])):
            s = video.Streamer(on_data=seen.append)
            s.attach()
            s.attach()
            _drain(s, seen)
            assert s.watching == 2
            s.detach()
            assert s.watching == 1
            s.detach()
            assert s.watching == 0

    def test_data_reaches_the_sink(self):
        seen = []
        with patch.object(video.Streamer, "_spawn",
                          return_value=_fake_proc([ANNEX_B])):
            s = video.Streamer(on_data=seen.append)
            s.attach()
            _drain(s, seen)
            s.detach()
        assert ANNEX_B in seen


class TestCredentialGuard:
    def test_it_will_not_record_a_credential_screen(self):
        """The reason this module needed a guard of its own. The JPEG path vetoes
        per frame; a video stream has no per-frame veto, so the veto is the
        stream."""
        seen = []
        with patch.object(video.Streamer, "_spawn") as spawn:
            s = video.Streamer(on_data=seen.append, unsafe=lambda: True)
            s.attach()
            time.sleep(0.2)
            s.detach()
        spawn.assert_not_called()
        assert seen == []

    def test_it_reports_being_blocked_rather_than_looking_broken(self):
        """A frozen picture and a dead process look identical from Texas."""
        with patch.object(video.Streamer, "_spawn"):
            s = video.Streamer(on_data=lambda _b: None, unsafe=lambda: True)
            s.attach()
            deadline = time.monotonic() + 2
            while not s.blocked and time.monotonic() < deadline:
                time.sleep(0.01)
            assert s.blocked is True
            s.detach()

    def test_it_starts_once_the_credential_screen_is_gone(self):
        unsafe = {"now": True}
        seen = []
        with patch.object(video.Streamer, "_spawn",
                          return_value=_fake_proc([ANNEX_B])):
            s = video.Streamer(on_data=seen.append,
                               unsafe=lambda: unsafe["now"])
            s.attach()
            time.sleep(0.2)
            assert seen == []
            unsafe["now"] = False
            _drain(s, seen)
            s.detach()
        assert ANNEX_B in seen


class TestResilience:
    def test_a_dead_encoder_is_restarted_not_given_up_on(self):
        """screenrecord exits on its own at the phone's 180s cap, and the restart
        has to be invisible — the new stream opens with its own SPS/PPS/IDR."""
        spawns = []

        def spawn(_self):
            spawns.append(1)
            return _fake_proc([ANNEX_B])

        with patch.object(video.Streamer, "_spawn", spawn):
            s = video.Streamer(on_data=lambda _b: None)
            s.attach()
            deadline = time.monotonic() + 2
            while len(spawns) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            s.detach()
        assert len(spawns) >= 2

    def test_a_failing_sink_does_not_kill_the_stream(self):
        """A viewer's socket dropping mid-write must not take the encoder with
        it — other viewers are on the same stream."""
        calls = []

        def angry(chunk):
            calls.append(chunk)
            raise RuntimeError("socket gone")

        with patch.object(video.Streamer, "_spawn",
                          return_value=_fake_proc([ANNEX_B, ANNEX_B])):
            s = video.Streamer(on_data=angry)
            s.attach()
            _drain(s, calls, want=2)
            s.detach()
        assert len(calls) >= 2

    def test_a_spawn_failure_is_survived(self):
        with patch.object(video.Streamer, "_spawn",
                          side_effect=OSError("no adb")):
            s = video.Streamer(on_data=lambda _b: None)
            s.attach()
            time.sleep(0.15)
            s.detach()          # must not raise


class TestStreamShape:
    def test_the_restart_window_is_inside_the_phones_limit(self):
        """--time-limit caps at 180s with no way to ask for more. Restarting
        after it would mean discovering the end by watching the stream close."""
        assert video.RESTART_AFTER < video.TIME_LIMIT

    def test_the_guard_looks_faster_than_a_person_can_type(self):
        assert video.GUARD_EVERY <= 0.5
