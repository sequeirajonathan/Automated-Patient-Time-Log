"""Drawing a signature through scrcpy's control channel, behind a switch.

WHY. A stored signature used to be replayed one stroke at a time through
Appium's W3C actions, and after each stroke the canvas was screenshotted to
see whether the ink had landed. That works, and it is slow. Most of the time
goes to waiting and checking, not to drawing: an HTTP round trip per stroke,
a 0.45 s gap, a 0.4 s pause to let the stroke paint, and a full PNG
screenshot, for every stroke. A six-stroke signature spent most of its ten
seconds proving each stroke arrived.

scrcpy's server (Apache-2.0, Genymobile) is a small jar run with
`app_process`. Started with `video=false audio=false control=true`, it opens
one socket and injects every 32-byte touch message it is sent, at the
coordinates it is given, on display 0. Nothing is captured and nothing
streams. The coordinates are the ones `sign.build_paths` already produces, so
the strokes are the same strokes, sent down one socket that stays open.

WHAT IS KEPT FROM THE OLD PATH, because each was paid for:

- the sideways lead-in at the start of every stroke (`sign.CLAIM_NUDGE`), so
  the bottom sheet holding the pad does not take the stroke as a dismiss;
- the three-pixel mark for a one-point stroke (`sign.DOT_MARK`);
- the per-move pace (`sign.MOVE_MS`). A pad that sets line width from speed
  draws hairlines when it is sent a stroke all at once.

WHAT IS CHECKED. One screenshot before the signature and one after. Each
stroke is judged from where it should be: along its own points, did dark
pixels appear that were not there before? A stroke that did not land is
sent once more, then handed to the old Appium path. A signature is never
cleared here, so a stroke that landed is never at risk.

THE SWITCH. This is a FEATURE FLAG, and it can be undone without a deploy:

- `/debug` has a "Signature engine" row: scrcpy or Appium.
- Any failure on the fast path falls back, within the same replay, to the
  old one. The signature is still drawn; it just takes the old time.
- TRIP_AFTER failures in a row turn the flag off by themselves and send the
  owner one notification. It stays off until somebody turns it back on.

State lives in one small JSON file beside the others, read fresh on every
signature, so both the portal and the feed process agree on it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from urllib.request import urlopen

log = logging.getLogger(__name__)

STATE_DIR = Path("/var/lib/aptlog")
FLAG_PATH = STATE_DIR / "sign-engine.json"

VERSION = "5.0.1"
SERVER_SHA256 = "764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536"
SERVER_URL = ("https://github.com/Genymobile/scrcpy/releases/download/"
              f"v{VERSION}/scrcpy-server-v{VERSION}")
SERVER_CACHE = STATE_DIR / "scrcpy" / f"scrcpy-server-v{VERSION}"
# Pushed per signature and deleted by the server itself when it exits
# (cleanup=true), so nothing is left on the phone between signatures.
DEVICE_JAR = "/data/local/tmp/aptlog-ink.jar"

SCRCPY = "scrcpy"
APPIUM = "appium"
ENGINES = (SCRCPY, APPIUM)
DEFAULT_ENGINE = SCRCPY

# Failures in a row before the switch turns itself off. Two, not one: a
# single failure is already handled by the in-replay fallback and costs her
# nothing. Two in a row means something has changed on the phone, and a
# third signature should not spend time finding that out again.
TRIP_AFTER = 2

# How long the server gets to start listening. Measured at well under a
# second on the caregiver's phone; five seconds is the point where waiting
# costs more than falling back.
CONNECT_TIMEOUT = 5.0

# Pen timing. PEN_HOLD is the pause after touch-down and before lift; the
# old path used 0.10 per pause through an HTTP chain. A pen lift between
# strokes is about what a hand takes.
PEN_HOLD = 0.05
LIFT_GAP = 0.12
# How long the app gets to paint the last stroke before the after-shot.
PAINT_WAIT = 0.5

# Verification. A point counts as inked when dark pixels appeared within
# INK_RADIUS of it; a stroke counts as landed when at least LANDED_SHARE of
# its points are. Strokes cross each other, so a missing stroke can borrow a
# few inked points from its neighbours; half is far above that.
DARK = 100
INK_RADIUS = 5
LANDED_SHARE = 0.5
# At most this many points are checked per stroke.
SAMPLES = 60

# scrcpy control-message constants (server v5.0.1, control_msg.h).
_TYPE_INJECT_TOUCH = 2
ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
# POINTER_ID_GENERIC_FINGER: a finger on the touchscreen, never a mouse.
_FINGER = -2
_TOUCH = struct.Struct(">BBqiiHHHII")


class InkUnavailable(RuntimeError):
    """The fast path cannot be used for this signature. Always caught."""


# ------------------------------------------------------------------ the switch

def _blank() -> dict:
    return {"engine": DEFAULT_ENGINE, "fails": 0}


def state(path: Path | None = None) -> dict:
    """The switch and its history. Never raises: a file that cannot be read
    gives the default."""
    target = path or FLAG_PATH
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _blank()
    if not isinstance(doc, dict):
        return _blank()
    if doc.get("engine") not in ENGINES:
        doc["engine"] = DEFAULT_ENGINE
    if not isinstance(doc.get("fails"), int):
        doc["fails"] = 0
    return doc


_lock = threading.Lock()


def _save(doc: dict, path: Path | None = None) -> None:
    target = path or FLAG_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("could not write the signature engine switch: %s", exc)


def engine(path: Path | None = None) -> str:
    return state(path)["engine"]


def set_engine(name: str, path: Path | None = None) -> str:
    """Set the switch by hand. Clears the failure count and the trip note,
    because a person choosing is a fresh start."""
    wanted = name if name in ENGINES else APPIUM
    with _lock:
        doc = state(path)
        doc.update(engine=wanted, fails=0, tripped="", set_at=time.time())
        _save(doc, path)
    log.warning("signature engine set to %s by hand", wanted)
    return wanted


def note_success(detail: str = "", path: Path | None = None) -> None:
    with _lock:
        doc = state(path)
        doc.update(fails=0, last_ok=time.time(), last=detail[:200])
        _save(doc, path)


def note_failure(reason: str, path: Path | None = None,
                 notify=None) -> bool:
    """Count one failure. Returns True if this one turned the switch off."""
    with _lock:
        doc = state(path)
        doc["fails"] = int(doc.get("fails", 0)) + 1
        doc.update(last_fail=time.time(), last=reason[:200])
        tripped = doc["engine"] == SCRCPY and doc["fails"] >= TRIP_AFTER
        if tripped:
            doc.update(engine=APPIUM, tripped=reason[:200],
                       tripped_at=time.time())
        _save(doc, path)
    if tripped:
        log.warning("fast signature drawing failed %d times in a row (%s); "
                    "switched back to the Appium path", TRIP_AFTER, reason)
        try:
            if notify is None:
                from apt_log.notify import send as notify
            notify("Signature drawing: the fast path failed twice in a row, "
                   "so it switched itself back to the old way. Signatures "
                   "still work, just slower. Turn it back on in /debug. "
                   "Last reason: " + reason[:120])
        except Exception as exc:  # noqa: BLE001 — never breaks a signature
            log.warning("could not send the engine notice: %s", exc)
    return tripped


# ------------------------------------------------------------------ the server

def ensure_server(cache: Path | None = None, fetch=None) -> Path:
    """The server jar on this machine, verified. Downloaded once.

    Raises InkUnavailable if it cannot be had or does not match. A jar
    that does not match the pinned checksum is never pushed to the phone.
    """
    target = cache or SERVER_CACHE
    try:
        if target.exists():
            data = target.read_bytes()
            if hashlib.sha256(data).hexdigest() == SERVER_SHA256:
                return target
            log.warning("cached scrcpy server does not match; fetching again")
        if fetch is None:
            def fetch(url):
                with urlopen(url, timeout=30) as r:  # noqa: S310 — pinned URL
                    return r.read()
        data = fetch(SERVER_URL)
    except Exception as exc:  # noqa: BLE001
        raise InkUnavailable("no scrcpy server: %s" % exc) from exc
    if hashlib.sha256(data).hexdigest() != SERVER_SHA256:
        raise InkUnavailable("scrcpy server checksum mismatch")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError as exc:
        raise InkUnavailable("cannot store the scrcpy server: %s" % exc) from exc
    return target


def touch_message(action: int, x: int, y: int, width: int = 0,
                  height: int = 0, pressure: float = 1.0) -> bytes:
    """One INJECT_TOUCH_EVENT, 32 bytes, as scrcpy's own client writes it."""
    p = max(0, min(0xFFFF, int(round(pressure * 0xFFFF))))
    return _TOUCH.pack(_TYPE_INJECT_TOUCH, action, _FINGER, int(x), int(y),
                       max(0, min(0xFFFF, int(width))),
                       max(0, min(0xFFFF, int(height))), p, 0, 0)


def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("socket closed")
        buf += chunk
    return buf


def _run(cmd, timeout=10.0):
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


class Pen:
    """One scrcpy server for one signature. Use as a context manager.

    Every step that can fail raises InkUnavailable, and `close` always
    leaves the phone as it was: the server exits when its socket closes,
    deletes its own jar, and the port forward is removed.
    """

    def __init__(self, serial: str | None = None, size=(0, 0),
                 jar: Path | None = None):
        self.serial = serial
        self.size = size
        self.jar = jar
        self.sock = None
        self.proc = None
        self.port = None

    def _adb(self, *args) -> list[str]:
        return ["adb"] + (["-s", self.serial] if self.serial else []) + list(args)

    def open(self) -> "Pen":
        jar = self.jar or ensure_server()
        try:
            r = _run(self._adb("push", str(jar), DEVICE_JAR), timeout=20)
            if r.returncode != 0:
                raise InkUnavailable("push failed")
            scid = "%08x" % (int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF)
            r = _run(self._adb("forward", "tcp:0",
                               "localabstract:scrcpy_" + scid))
            port = (r.stdout or b"").decode(errors="replace").strip()
            if r.returncode != 0 or not port.isdigit():
                raise InkUnavailable("forward failed")
            self.port = int(port)
            args = ["scid=" + scid, "log_level=warn", "tunnel_forward=true",
                    "video=false", "audio=false", "control=true",
                    "cleanup=true", "power_on=false",
                    "clipboard_autosync=false", "show_touches=false",
                    "stay_awake=false"]
            cmd = ("CLASSPATH=%s app_process / com.genymobile.scrcpy.Server "
                   "%s %s" % (DEVICE_JAR, VERSION, " ".join(args)))
            self.proc = subprocess.Popen(self._adb("shell", cmd),
                                         stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT)
            self.sock = self._connect()
            # The device name. Read and discarded: it only proves the
            # server got as far as talking.
            _recv_exact(self.sock, 64)
            self.sock.settimeout(None)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except InkUnavailable:
            self.close()
            raise
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise InkUnavailable("scrcpy did not start: %s" % exc) from exc
        return self

    def _connect(self):
        # The forward accepts at once, before the server is listening; a
        # connection that closes without the dummy byte means "not yet".
        deadline = time.monotonic() + CONNECT_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise InkUnavailable("scrcpy server exited at start")
            s = None
            try:
                s = socket.create_connection(("127.0.0.1", self.port), 2)
                s.settimeout(2)
                if _recv_exact(s, 1) == b"\x00":
                    return s
            except (OSError, EOFError):
                pass
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
            time.sleep(0.1)
        raise InkUnavailable("scrcpy server never accepted")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.proc is not None:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            self.proc = None
        if self.port is not None:
            try:
                _run(self._adb("forward", "--remove", "tcp:%d" % self.port),
                     timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self.port = None

    def __enter__(self):
        return self.open() if self.sock is None else self

    def __exit__(self, *exc):
        self.close()
        return False

    def touch(self, action: int, x: int, y: int) -> None:
        if self.sock is None:
            raise InkUnavailable("pen is not open")
        w, h = self.size or (0, 0)
        try:
            self.sock.sendall(touch_message(
                action, x, y, w, h, 0.0 if action == ACTION_UP else 1.0))
        except OSError as exc:
            raise InkUnavailable("scrcpy socket closed: %s" % exc) from exc

    def stroke(self, path, nudge: int, dot: int, move_ms: int,
               sleep=time.sleep, clock=time.monotonic) -> None:
        """One stroke, the same shape `sign._draw` gives Appium: down,
        sideways and back, through the points, up."""
        x0, y0 = path[0]
        self.touch(ACTION_DOWN, x0, y0)
        sleep(PEN_HOLD)
        way = 1 if path[-1][0] >= x0 else -1
        lead = nudge if len(path) > 1 else dot
        step = move_ms / 1000.0
        due = clock()
        for x, y in [(x0 + way * lead, y0), (x0, y0)] + list(path[1:]):
            due += step
            self.touch(ACTION_MOVE, x, y)
            wait = due - clock()
            if wait > 0:
                sleep(wait)
        sleep(PEN_HOLD)
        last = path[-1]
        self.touch(ACTION_UP, last[0], last[1])


# --------------------------------------------------------------- verification

def grab(bounds, serial=None):
    """The canvas as a greyscale image, or None if the screen cannot be read."""
    from apt_log import feed as feed_mod

    try:
        shot = feed_mod._adb(["exec-out", "screencap", "-p"], serial,
                             timeout=20.0)
        if shot.returncode != 0 or not shot.stdout:
            return None
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(shot.stdout)) as im:
            grey = im.convert("L").crop(tuple(bounds))
            grey.load()
            return grey
    except Exception as exc:  # noqa: BLE001
        log.info("cannot read the canvas (%s)", exc)
        return None


def dark_count(img) -> int:
    return sum(img.histogram()[:DARK])


def _dark_near(img, x: int, y: int, r: int) -> int:
    w, h = img.size
    box = (max(0, x - r), max(0, y - r), min(w, x + r + 1), min(h, y + r + 1))
    if box[0] >= box[2] or box[1] >= box[3]:
        return 0
    return sum(img.crop(box).histogram()[:DARK])


def stroke_landed(before, after, path, bounds) -> bool:
    """Whether this stroke's own points gained ink between the two shots."""
    ox, oy = bounds[0], bounds[1]
    pts = list(path)
    if len(pts) > SAMPLES:
        stride = len(pts) / SAMPLES
        pts = [pts[int(i * stride)] for i in range(SAMPLES)]
    hit = 0
    for x, y in pts:
        cx, cy = int(x) - ox, int(y) - oy
        if _dark_near(after, cx, cy, INK_RADIUS) > _dark_near(
                before, cx, cy, INK_RADIUS):
            hit += 1
    return hit >= max(1, LANDED_SHARE * len(pts))


def missing(before, after, paths, bounds) -> list[int]:
    return [i for i, p in enumerate(paths)
            if p and not stroke_landed(before, after, p, bounds)]
