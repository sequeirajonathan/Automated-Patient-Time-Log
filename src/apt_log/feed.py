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

import logging
import re
import subprocess
import time
from pathlib import Path

from apt_log.ui import mirror as mirror_mod

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 5.0

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


def password_field_on_screen(serial: str | None = None) -> bool | None:
    """True/False, or None when the hierarchy could not be read.

    None is distinct from False on purpose — the caller treats "I could not
    look" differently from "I looked and there was nothing".
    """
    try:
        dumped = _adb(["shell", "uiautomator", "dump", "/sdcard/.aptlog-feed.xml"],
                      serial)
        if dumped.returncode != 0:
            return None
        xml = _adb(["shell", "cat", "/sdcard/.aptlog-feed.xml"], serial).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if not xml:
        return None
    return bool(_PASSWORD_NODE.search(xml.decode("utf-8", "replace")))


def screen_for(focus: str) -> str:
    """Map a focused window to the mirror's vocabulary.

    dumpsys reports the activity fully qualified on some screens and
    dot-prefixed on others -- `.../com.vendor.app.HomeActivity` next to
    `.../.HomeActivity` -- so the last dot-separated component is the only part
    that is reliably the class name.
    """
    activity = (focus or "").split("/")[-1].split(".")[-1].lower()
    return ACTIVITY_SCREENS.get(activity, "unknown")


def capture(serial: str | None = None) -> tuple[bytes | None, str, str]:
    """Return (png_or_None, focus, reason).

    `reason` is why nothing was captured, empty when something was.
    """
    focus = current_focus(serial)
    if not focus:
        return None, "", "cannot read the focused window"

    if looks_like_a_login_screen(focus):
        return None, focus, "a credential can be typed on this screen"

    if password_field_on_screen(serial) is True:
        return None, focus, "a password field is on screen"

    try:
        shot = _adb(["exec-out", "screencap", "-p"], serial, timeout=30.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, focus, f"screencap failed: {exc}"

    if shot.returncode != 0 or not shot.stdout:
        # FLAG_SECURE screens refuse capture outright; that is a valid answer.
        return None, focus, "the app does not allow capture of this screen"
    return shot.stdout, focus, ""


def write_frame(path: Path, serial: str | None = None) -> str:
    """Capture once and publish the mirror frame. Returns a one-line status."""
    png, focus, reason = capture(serial)
    screen = screen_for(focus)

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
    return f"{screen:<10} {focus or '?':<50} {reason or 'captured'}"


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
