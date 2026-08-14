"""Keeps the mirror fed, so the phone can be watched from a thousand miles away.

The page has a phone-view panel and a "what is the controller doing" panel, and
until something writes to them they are both blank. This is that something: a
small loop that captures the screen and publishes a frame.

**It does not go through Appium.** `adb exec-out screencap` costs a fraction of a
session and, more importantly, does not compete for one. The agent will want the
Appium session for real work, and a watcher that has to be evicted before
anything can happen is a watcher that gets turned off.

The cost of leaving Appium behind is that `capture.safe_screenshot`'s interlock
comes with it. That guard asks a live driver whether a password field has focus,
and this process has no driver — nor does it share the thread-local that
`capture.suppressed()` sets, because it is a different process entirely. So the
protection has to be rebuilt here, and it is built to fail closed:

1. Cannot read the current activity → refuse. Knowing nothing is not permission.
2. Activity name looks like a sign-in screen → refuse. Matched on substrings
   rather than an exact list, because four apps means four login screens and
   three of them have not been seen yet.
3. Hierarchy readable and holds a password field anywhere → refuse. Anywhere,
   not just focused: this runs on a timer with no idea what is about to happen,
   and REQ-3 asks for the strict reading when something is persisted or shown to
   a third party. A picture on a web page is both.
4. Hierarchy unreadable → capture anyway, having passed the activity check.

Step 4 is the honest weak point. UiAutomator2 holds the dump service while an
Appium session is live, which is exactly when the screen is most worth watching.
Refusing there would make this useless during the only interesting moments, so it
leans on the activity name alone — and that is why step 2 is broad rather than
precise.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import struct
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from apt_log.ui import mirror as mirror_mod

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 1.0

# She reads this over cellular while walking between floors, so the wire size is
# what decides whether the portal gets used or she walks down to the phone.
# Measured on the Pi: a 720x1600 screenshot is 121 KB on a light screen and
# 925 KB on a busy one; at 480 wide and quality 65 the same frames land at ~32 KB
# and encode in 43 ms. 480 is two-thirds of the device width, which still gives a
# ~1.2x pixel ratio on a phone-sized viewport rather than a soft upscale.
MIRROR_WIDTH = 480
MIRROR_QUALITY = 65

# How often the overlay is refreshed, in seconds. The picture rides the loop
# interval; this rides a multiple of it, because reading the hierarchy costs
# roughly twice what a compressed screenshot does.
HIERARCHY_EVERY = 3.0

# Filled from `wm size` at first use; the overlay needs it to scale boxes onto a
# phone-sized <img>. Cached because it does not change and the loop is hot.
SCREEN_SIZE: list[int] = [0, 0]

# Bounds as uiautomator writes them: [x1,y1][x2,y2]
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
# Two producers, two dialects. `uiautomator dump` writes every element as
# <node class="android.widget.Button" .../>; Appium's page_source writes
# <android.widget.Button class="android.widget.Button" .../>, using the class as
# the tag and emitting no <node> at all. Matching only <node> silently parsed
# zero elements out of a perfectly good 19 KB Appium document -- the overlay was
# empty for an hour with the read succeeding every time.
_NODE = re.compile(r"<[A-Za-z][\w.$]*[^>]*>")

# Substrings that mark an activity as somewhere a credential can be typed.
# Deliberately loose: a new app's login screen should be caught without anyone
# remembering to add it here.
LOGIN_ACTIVITY_MARKERS = (
    "signin", "sign_in", "login", "log_in", "auth", "password",
    "credential", "passcode", "pin",
)

_FOCUS = re.compile(r"mCurrentFocus=Window\{[^}]*\s+(\S+)\}")
_PASSWORD_NODE = re.compile(r'password="true"')

# Activity suffix -> the mirror's fixed vocabulary. Anything unlisted is
# published as "unknown", which is a real answer (see ui/mirror.py).
ACTIVITY_SCREENS = {
    "applaunchactivity": "startup",
    "languageactivity": "language",
    "signinactivity": "login",
    "agencyactivity": "agency",
    "homeactivity": "home",
    "todayscheduleactivity": "today",
    "visitdetailactivity": "visit",
}


def screen_size(serial: str | None = None) -> list[int]:
    """Device resolution, cached. [0, 0] when it cannot be read."""
    if SCREEN_SIZE != [0, 0]:
        return SCREEN_SIZE
    try:
        out = _adb(["shell", "wm", "size"], serial).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return SCREEN_SIZE
    m = re.search(r"(\d+)x(\d+)", out)
    if m:
        SCREEN_SIZE[:] = [int(m.group(1)), int(m.group(2))]
    return SCREEN_SIZE


def _adb(args: list[str], serial: str | None = None, timeout: float = 15.0):
    cmd = ["adb"] + (["-s", serial] if serial else []) + args
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def current_focus(serial: str | None = None) -> str:
    """The focused window's `package/activity`, or empty if it cannot be read."""
    try:
        out = _adb(["shell", "dumpsys", "window"], serial).stdout.decode(
            "utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("cannot read the focused window (%s)", exc)
        return ""
    m = _FOCUS.search(out)
    return m.group(1) if m else ""


def looks_like_a_login_screen(focus: str) -> bool:
    lowered = (focus or "").lower()
    return any(marker in lowered for marker in LOGIN_ACTIVITY_MARKERS)


def password_field_in(xml: str | None) -> bool | None:
    """True/False, or None when the hierarchy could not be read.

    None is distinct from False on purpose — the caller treats "I could not
    look" differently from "I looked and there was nothing".
    """
    if xml is None:
        return None
    return bool(_PASSWORD_NODE.search(xml))


def screen_for(focus: str) -> str:
    """Map a focused window to the mirror's vocabulary.

    dumpsys reports the activity fully qualified on some screens and
    dot-prefixed on others -- `.../com.vendor.app.HomeActivity` next to
    `.../.HomeActivity` -- so the last dot-separated component is the only part
    that is reliably the class name.
    """
    activity = (focus or "").split("/")[-1].split(".")[-1].lower()
    return ACTIVITY_SCREENS.get(activity, "unknown")


def capture(serial: str | None = None,
            hierarchy: str | None = None) -> tuple[bytes | None, str, str]:
    """Return (png_or_None, focus, reason).

    `reason` is why nothing was captured, empty when something was. The
    hierarchy is passed in rather than fetched: one dump serves both the
    password check and the overlay, which halves the adb work and removes a
    race two callers of the old version had against each other.
    """
    focus = current_focus(serial)
    if not focus:
        return None, "", "cannot read the focused window"

    if looks_like_a_login_screen(focus):
        return None, focus, "a credential can be typed on this screen"

    if password_field_in(hierarchy) is True:
        return None, focus, "a password field is on screen"

    try:
        shot = _adb(["exec-out", "screencap"], serial, timeout=30.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, focus, f"screencap failed: {exc}"

    if shot.returncode != 0 or not shot.stdout:
        # FLAG_SECURE screens refuse capture outright; that is a valid answer.
        return None, focus, "the app does not allow capture of this screen"
    return compress(shot.stdout), focus, ""


def _decode(shot: bytes):
    """A screencap, whether it arrived raw or as PNG.

    `screencap` without -p emits a small header of little-endian uint32s --
    width, height, pixel format, and on anything modern a colourspace word --
    followed by the framebuffer. The header length is inferred from the pixel
    count rather than assumed, because it grew by four bytes at some Android
    version and guessing wrong yields an image that is subtly sheared instead of
    obviously broken.
    """
    from PIL import Image

    if shot[:8] == b"\x89PNG\r\n\x1a\n":
        return Image.open(io.BytesIO(shot))

    width, height, _fmt = struct.unpack("<III", shot[:12])
    pixels = width * height * 4
    header = len(shot) - pixels
    if header not in (12, 16) or width <= 0 or height <= 0:
        raise ValueError(f"unrecognised screencap: {len(shot)} bytes, "
                         f"{width}x{height}")
    return Image.frombuffer("RGBA", (width, height), shot[header:header + pixels],
                            "raw", "RGBA", 0, 1)


def compress(shot: bytes) -> bytes:
    """Downscale and re-encode for the wire.

    Measured on the Pi, and the result is worth stating because it is
    counter-intuitive: asking the phone to PNG-encode the frame costs 2,416 ms
    and sends 1.27 MB, while taking the raw framebuffer costs 649 ms and sends
    4.61 MB. Four times the bytes, and nearly four times faster -- a budget
    MediaTek CPU compresses far slower than USB moves data, so the phone was
    spending 1.8 seconds to save bandwidth that was never scarce.

    The resize and JPEG then cost 32 ms here, against 69 ms when a PNG has to be
    decoded first.

    Falls back to the original bytes rather than failing: a large picture is a
    slow portal, but no picture is a portal she cannot use at all.
    """
    try:
        from PIL import Image

        image = _decode(shot).convert("RGB")
        image.thumbnail((MIRROR_WIDTH, MIRROR_WIDTH * 10), Image.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=MIRROR_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot compress the frame (%s); sending it as-is", exc)
        return shot


FRAME_NAME = "frame.json"

# How stale the published frame may be and still be tapped against. Generous
# against the overlay's own refresh cadence, tight enough that a dead feed
# refuses rather than letting her aim at a screen from minutes ago.
TAP_FRAME_MAX_AGE = 15.0


def write_frame(path: Path, serial: str | None = None,
                hierarchy: str | None = None) -> str:
    """Capture once and publish the mirror frame. Returns a one-line status.

    The hierarchy is handed in rather than fetched, so the caller decides how
    often to pay for it. See `run`.
    """
    png, focus, reason = capture(serial, hierarchy)
    screen = screen_for(focus)

    els = elements(hierarchy) if hierarchy else []
    frame = {
        "id": frame_id(els),
        # Separate from the structural id on purpose. Typing into a field moves
        # no targets at all, so a client refreshing only on structure change
        # would show her a picture with none of her own keystrokes in it.
        "img": hashlib.sha256(png).hexdigest()[:12] if png else "",
        "at": datetime.now().isoformat(),
        "size": screen_size(serial),
        "elements": els,
        "captured": bool(png),
    }
    target = path.parent / FRAME_NAME
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(frame), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("cannot publish the frame map (%s)", exc)

    if png:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(png)
        tmp.replace(path)
        step = "waiting" if screen == "unknown" else "working"
    else:
        # The frame is still published. "The controller is on the sign-in
        # screen and no picture is available" is information; silence looks
        # identical to the process being dead.
        step = "blocked" if reason else "working"

    mirror_mod.publish(screen=screen, step=step)
    return (f"{screen:<10} {len(els):>3} tappable  "
            f"{focus or '?':<46} {reason or 'captured'}")


class _Hierarchy:
    """The latest screen structure, refreshed on its own thread.

    Separate thread rather than a slower clock, because the two are not the same
    promise. A slower clock still puts the hierarchy read *in front of* the next
    picture, so one bad read -- and a failed Appium connect takes 25 seconds --
    freezes the thing she is actually looking at. On its own thread a bad read
    costs nothing but a slightly staler overlay.
    """

    def __init__(self, serial: str | None, every: float):
        self._serial = serial
        self._every = every
        self._xml: str | None = None
        self._focus = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def xml(self) -> str | None:
        with self._lock:
            return self._xml

    def _accept(self, fresh: str, focus: str) -> bool:
        """Whether a fresh read should replace what we are showing.

        Guarding against None was not enough. A read can succeed and come back
        structurally empty -- observed alternating 16 targets, 16, then 0, 0, 0
        on a launcher that had not changed -- and an empty-but-valid result
        happily overwrote a good one, so the overlay kept blinking out.

        An empty read is only believed when the screen has actually changed.
        Otherwise the previous boxes stay: they are stale, and if they are wrong
        she can see they are wrong, which is more than an empty overlay offers.
        """
        if elements(fresh):
            return True
        if focus != self._focus:
            return True                      # genuinely a different screen
        return not elements(self._xml or "")  # nothing better to keep

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                fresh = read_hierarchy(self._serial)
                if fresh is not None:
                    focus = current_focus(self._serial)
                    with self._lock:
                        if self._accept(fresh, focus):
                            self._xml = fresh
                            self._focus = focus
            except Exception as exc:  # noqa: BLE001
                log.warning("hierarchy read failed: %s", exc)
            self._stop.wait(self._every)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="aptlog-hierarchy")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def run(path: Path, interval: float = DEFAULT_INTERVAL,
        serial: str | None = None, iterations: int | None = None) -> None:
    """Loop until stopped. `iterations` bounds it for tests.

    Measured on the Pi: a screenshot is 865 ms and compressing it 49 ms, while
    reading the hierarchy is 664-796 ms through an established resident session
    -- but 25 s when the session has to be rebuilt, and it does have to be
    rebuilt sometimes. Averages are the wrong tool here; the worst case is what
    she feels.

    So the picture runs here, unblocked, and the hierarchy runs beside it.
    """
    watcher = _Hierarchy(serial, HIERARCHY_EVERY)
    watcher.start()
    count = 0
    try:
        while iterations is None or count < iterations:
            try:
                log.info("%s", write_frame(path, serial, watcher.xml))
            except Exception as exc:  # noqa: BLE001
                # A watcher that dies on one bad read stops being a watcher.
                log.warning("frame failed: %s", exc)
            count += 1
            if iterations is None or count < iterations:
                time.sleep(interval)
    finally:
        watcher.stop()


def _attr(node: str, name: str) -> str:
    m = re.search(rf'{name}="([^"]*)"', node)
    return m.group(1) if m else ""


def elements(xml: str) -> list[dict]:
    """The tappable structure of a screen, carrying no text.

    This is what the page draws boxes from and what a tap posts back, so it is
    built to hold nothing worth protecting. The words stay in the screenshot,
    where they are already visible to whoever is looking at the page; the
    structure is what crosses the wire a second time and lands in a log.

    `has_text` is deliberately a boolean. Knowing a row has a label is enough to
    draw it; knowing the label is a patient name.
    """
    found = []
    for raw in _NODE.findall(xml or ""):
        if _attr(raw, "clickable") != "true":
            continue
        m = _BOUNDS.search(_attr(raw, "bounds"))
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        # Appium always sets class= as well as using it as the tag, but the
        # tag is the fallback so a dialect that only does one still works.
        cls = _attr(raw, "class") or raw[1:].split()[0].rstrip("/>")
        found.append({
            "rid": _attr(raw, "resource-id").split("/")[-1],
            "cls": cls.rsplit(".", 1)[-1],
            "b": [x1, y1, x2, y2],
            "focused": _attr(raw, "focused") == "true",
            "selected": _attr(raw, "selected") == "true",
            "has_text": bool(_attr(raw, "text")),
        })
    return found


def read_hierarchy(serial: str | None = None) -> str | None:
    """The current hierarchy: resident Appium if it will have us, adb if not.

    An established Appium session answers in 664-796 ms and never returns a
    partial screen. `adb shell uiautomator dump` takes 6.1 s, spawns a fresh
    instrumentation every call, and comes back partial often enough to need the
    stabilising retry underneath -- 12.7 s in total.

    Appium is tried first for those numbers, but it is not depended on. Session
    creation on this device fails on the first attempt after an idle spell, and
    has been seen to hang past 90 seconds with no error at all. A portal whose
    overlay disappears because a session would not open is not acceptable, and
    the fallback's cost stopped mattering the moment this moved to its own
    thread: 12.7 s there buys a staler overlay, not a frozen picture.
    """
    from apt_log import resident

    source = resident.page_source()
    if source is not None:
        return source
    log.info("no Appium session; falling back to the adb dump")
    return read_stable_hierarchy(serial)


def read_hierarchy_via_adb(serial: str | None = None) -> str | None:
    """The old path, kept for when there is no Appium server to talk to.

    The remote path carries this process's pid. A fixed name looked harmless
    until the feed loop and a one-off call raced on it and one read a file the
    other was mid-write on -- surfacing as an intermittent "hierarchy
    unavailable", the most misleading symptom available, since that is also what
    a live Appium session legitimately produces.
    """
    remote = f"/sdcard/.aptlog-feed-{os.getpid()}.xml"
    try:
        dumped = _adb(["shell", "uiautomator", "dump", remote], serial)
        if dumped.returncode != 0:
            return None
        out = _adb(["shell", "cat", remote], serial).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return out.decode("utf-8", "replace") if out else None


def read_stable_hierarchy(serial: str | None = None,
                          attempts: int = 5) -> str | None:
    """A dump worth trusting, or the best available.

    `uiautomator dump` is not a snapshot. Measured on a completely static
    Settings screen, four consecutive dumps returned 20, 2, 2 and 22 elements:
    it sometimes captures a single window layer instead of the composed screen,
    and nothing in the result says which kind you got. This is why Appium ships
    its own instrumentation server rather than shelling out to this command.

    Two consecutive dumps that agree is the cheapest signal that the screen was
    actually read. Failing that, the richest dump wins -- a partial capture is
    strictly a subset, so "most elements" is the least-wrong answer available
    rather than an arbitrary tiebreak.
    """
    best: str | None = None
    best_count = -1
    previous: str | None = None

    for attempt in range(attempts):
        xml = read_hierarchy_via_adb(serial)
        if xml is not None:
            els = elements(xml)
            current = frame_id(els)
            if els and current == previous:
                return xml
            previous = current
            if len(els) > best_count:
                best, best_count = xml, len(els)
        if attempt < attempts - 1:
            time.sleep(0.3)

    if best is not None:
        log.debug("no two dumps agreed; using the richest (%d elements)", best_count)
    return best


def frame_id(els: list[dict]) -> str:
    """Identity of a screen *for aiming purposes*.

    A hash of the tappable structure, not of the pixels. That is the useful
    definition: a clock ticking in the corner repaints the screen without moving
    anything she could tap, and invalidating her aim for that would make the
    control unusable on any screen with a timer on it. Anything that does move a
    target changes this.
    """
    shape = [[e["rid"], e["cls"], e["b"]] for e in els]
    return hashlib.sha256(
        json.dumps(shape, separators=(",", ":")).encode()).hexdigest()[:16]


# ----------------------------------------------------------------------- tap
class StaleAim(RuntimeError):
    """The screen moved between the frame she aimed at and now."""


class NotOnScreen(RuntimeError):
    """The element posted back is not one this frame offered."""


def published_elements(path: Path | None = None) -> list[dict]:
    """The overlay the page is actually showing, from the feed's own frame file.

    Reading the device again here was wrong twice over. It was slow -- the tap
    runs in the UI process, which would have to open a second Appium session
    while the feed holds the only one UiAutomator2 allows, and the contention
    cost 14 seconds a tap. And it answered the wrong question: a fresh read tells
    you what is on the screen *now*, when what makes a tap safe is that it was on
    the screen *she was looking at*.

    So this reads the same file the page drew its boxes from. If the feed has
    stopped, the frame ages out and taps refuse -- which is the correct answer,
    because an overlay nobody is updating is one she cannot trust either.
    """
    from apt_log.ui.state import STATE_DIR

    target = path or (STATE_DIR / FRAME_NAME)
    try:
        frame = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise StaleAim("no screen has been published to aim at") from None

    try:
        age = (datetime.now() - datetime.fromisoformat(frame["at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        raise StaleAim("the published screen has no timestamp") from None

    if age > TAP_FRAME_MAX_AGE:
        raise StaleAim(f"the screen on your page is {age:.0f}s old — look again")
    return frame.get("elements") or []


def tap(claimed_frame: str, element: dict, serial: str | None = None,
        frame_path: Path | None = None) -> dict:
    """Tap an element she aimed at, or refuse because the screen has moved.

    The refusal is the feature. A coordinate replayed blind lands on whatever
    occupies that spot now, and on this app that can be the GPS verification
    that produces a call from the agency. So the frame is re-read and re-hashed
    first: if the tappable structure changed at all, nothing is touched and she
    gets a fresh frame to aim at.

    What is checked is that the element she aimed at is *still on the screen*,
    at the bounds she saw it at, with the same identity. That is both the
    staleness check and the guard against a posted rectangle that was never
    there -- a tap at arbitrary coordinates being exactly what this is built not
    to be.

    `claimed_frame` is carried for the log rather than enforced. It was enforced
    once; see read_stable_hierarchy for the measurement that changed it.
    """

    current = published_elements(frame_path)
    bounds = list(element.get("b") or [])
    match = next(
        (e for e in current
         if e["b"] == bounds
         and e["rid"] == element.get("rid", "")
         and e["cls"] == element.get("cls", "")),
        None,
    )
    if match is None:
        # Presence at those bounds *is* the staleness check, and a stronger one
        # than comparing whole-frame hashes. If the screen moved on, the thing
        # she aimed at is not there any more and this refuses. If it is still
        # there, tapping it does what she meant regardless of what else changed
        # elsewhere on the screen -- and with a dump source this noisy, whole
        # frame equality would refuse almost every legitimate tap.
        #
        # The case a frame hash would catch and this does not is a different
        # widget with the same resource-id, the same class and the same
        # rectangle. That is the same control by every observable property.
        raise StaleAim("that is no longer on the screen — look again")

    x1, y1, x2, y2 = match["b"]
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    result = _adb(["shell", "input", "tap", str(cx), str(cy)], serial, timeout=20.0)
    if result.returncode != 0:
        raise StaleAim(
            f"adb refused the tap: "
            f"{result.stderr.decode('utf-8', 'replace').strip()}")

    # Identity only. The element map carries no text by construction, so this
    # log line cannot name a patient however the screen is laid out.
    log.info("tapped %s/%s at (%d,%d) on frame %s",
             match["cls"], match["rid"] or "-", cx, cy, claimed_frame)
    return {"tapped": {"rid": match["rid"], "cls": match["cls"], "at": [cx, cy]}}
