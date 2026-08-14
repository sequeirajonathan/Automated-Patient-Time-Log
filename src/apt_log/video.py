"""H.264 straight off the phone's hardware encoder.

The JPEG mirror sends a whole picture every time anything changes: 77 KB a frame,
about two frames a second, and every frame costs the same whether the screen
moved or not. Measured against it, `screenrecord` produced **34 KB for three
seconds** of a static launcher — because a video codec sends differences, and a
screen that is not changing is almost free. The encoding is done by dedicated
silicon on the phone rather than by its CPU, which is why this is fast on
hardware where PNG encoding took 1.8 seconds a frame.

The stream is Annex B (`00 00 00 01` start codes) carrying SPS, PPS, an IDR
keyframe and then P slices. WebCodecs decodes that directly.

Three things shape the implementation.

**It only runs while someone is watching.** `screenrecord` cannot run twice at
once, and a decoder joining mid-stream cannot start without SPS, PPS and a
keyframe — which the encoder only emits at the beginning. Starting the recorder
when the first viewer attaches solves both: every viewer gets a stream that
begins at a keyframe, and nothing competes for the encoder when nobody is
looking.

**It restarts itself before the phone stops it.** `screenrecord --time-limit`
caps at 180 seconds and there is no way to ask for more. A restart is invisible
to the decoder because the new stream opens with its own SPS/PPS/IDR.

**It stops for credential screens.** This is the one that matters. The JPEG path
checks for a password field before every single frame; a video stream has no
per-frame decision point, so it would happily record her typing a password. The
guard therefore runs beside the stream and kills it — the picture freezing while
she signs in is the correct behaviour, not a defect.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

# The phone's own cap. Restart a little early rather than discovering the end of
# the stream by watching it close.
TIME_LIMIT = 180
RESTART_AFTER = 170.0

# 2 Mbps is generous for a phone UI, which is mostly flat colour and text. It is
# a ceiling, not a target: the static launcher above used about 90 kbps of it.
BIT_RATE = "2M"

# How often the credential guard looks. A quarter second is far quicker than a
# person can focus a field and type into it, and it costs one adb call.
GUARD_EVERY = 0.25

CHUNK = 16384


class Streamer:
    """Runs `screenrecord` while at least one viewer is attached.

    The callback receives raw Annex B bytes as they arrive. Chunk boundaries are
    arbitrary — they are not NAL boundaries — because a decoder fed a byte stream
    does its own framing and buffering here would only add latency.
    """

    def __init__(self, on_data: Callable[[bytes], None],
                 unsafe: Callable[[], bool] | None = None,
                 serial: str | None = None):
        self._on_data = on_data
        self._unsafe = unsafe
        self._serial = serial
        self._lock = threading.Lock()
        self._viewers = 0
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._blocked = False

    # -------------------------------------------------------------- viewers
    def attach(self) -> None:
        with self._lock:
            self._viewers += 1
            if self._viewers == 1:
                self._begin()

    def detach(self) -> None:
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            if self._viewers == 0:
                self._end()

    @property
    def watching(self) -> int:
        with self._lock:
            return self._viewers

    @property
    def blocked(self) -> bool:
        """True when the stream is deliberately stopped for a credential screen."""
        return self._blocked

    # --------------------------------------------------------------- process
    def _spawn(self) -> subprocess.Popen:
        cmd = ["adb"] + (["-s", self._serial] if self._serial else []) + [
            "exec-out",
            f"screenrecord --output-format=h264 --time-limit {TIME_LIMIT} "
            f"--bit-rate {BIT_RATE} -",
        ]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _begin(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name="aptlog-video")
        self._thread.start()

    def _end(self) -> None:
        self._stop.set()
        self._kill()

    def _pump(self) -> None:
        """Read the encoder forever, restarting before the phone cuts us off."""
        while not self._stop.is_set():
            if self._unsafe is not None and self._unsafe():
                # Not an error. A frozen picture while she signs in is the
                # point: there is no per-frame veto in a video stream, so the
                # veto has to be the stream itself.
                if not self._blocked:
                    log.info("video paused: a credential screen is showing")
                self._blocked = True
                self._kill()
                self._stop.wait(GUARD_EVERY)
                continue

            if self._blocked:
                log.info("video resumed")
                self._blocked = False

            try:
                self._proc = self._spawn()
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("cannot start screenrecord (%s)", exc)
                self._stop.wait(2.0)
                continue

            started = time.monotonic()
            stream = self._proc.stdout
            while not self._stop.is_set():
                if time.monotonic() - started > RESTART_AFTER:
                    break
                if self._unsafe is not None and self._unsafe():
                    break
                try:
                    data = stream.read(CHUNK) if stream else b""
                except (OSError, ValueError):
                    break
                if not data:
                    break
                try:
                    self._on_data(data)
                except Exception as exc:  # noqa: BLE001
                    log.warning("video sink failed: %s", exc)
            self._kill()
        self._kill()
