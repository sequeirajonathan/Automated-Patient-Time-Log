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
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from apt_log.ui import mirror as mirror_mod

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 5.0

# Filled from `wm size` at first use; the overlay needs it to scale boxes onto a
# phone-sized <img>. Cached because it does not change and the loop is hot.
SCREEN_SIZE: list[int] = [0, 0]

# Bounds as uiautomator writes them: [x1,y1][x2,y2]
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_NODE = re.compile(r"<node[^>]*>")

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
        shot = _adb(["exec-out", "screencap", "-p"], serial, timeout=30.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, focus, f"screencap failed: {exc}"

    if shot.returncode != 0 or not shot.stdout:
        # FLAG_SECURE screens refuse capture outright; that is a valid answer.
        return None, focus, "the app does not allow capture of this screen"
    return shot.stdout, focus, ""


FRAME_NAME = "frame.json"


def write_frame(path: Path, serial: str | None = None) -> str:
    """Capture once and publish the mirror frame. Returns a one-line status."""
    hierarchy = read_hierarchy(serial)
    png, focus, reason = capture(serial, hierarchy)
    screen = screen_for(focus)

    els = elements(hierarchy) if hierarchy else []
    frame = {
        "id": frame_id(els),
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


def run(path: Path, interval: float = DEFAULT_INTERVAL,
        serial: str | None = None, iterations: int | None = None) -> None:
    """Loop until stopped. `iterations` bounds it for tests."""
    count = 0
    while iterations is None or count < iterations:
        try:
            log.info("%s", write_frame(path, serial))
        except Exception as exc:  # noqa: BLE001
            # A watcher that dies on one bad read stops being a watcher.
            log.warning("frame failed: %s", exc)
        count += 1
        if iterations is None or count < iterations:
            time.sleep(interval)


# --------------------------------------------------------------------- overlay
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
        found.append({
            "rid": _attr(raw, "resource-id").split("/")[-1],
            "cls": _attr(raw, "class").rsplit(".", 1)[-1],
            "b": [x1, y1, x2, y2],
            "focused": _attr(raw, "focused") == "true",
            "selected": _attr(raw, "selected") == "true",
            "has_text": bool(_attr(raw, "text")),
        })
    return found


def read_hierarchy(serial: str | None = None) -> str | None:
    """Raw page source, or None when it cannot be read.

    The remote path carries this process's pid. A fixed name looked harmless
    until the feed loop and a one-off call raced on it and one of them read a
    file the other was mid-write on -- which surfaced as an intermittent "the
    hierarchy is unavailable", the single most misleading symptom available,
    since it is also what a live Appium session legitimately produces.
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
