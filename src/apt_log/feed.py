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

**A refusal has to say something.** Refusing the picture used to be the whole
answer, and it left her looking at an unlabelled rectangle where the app had put
a dialog — no words, no button label, no way to know whether the thing under her
thumb said "sign in again" or something that mattered. Meanwhile the *previous*
screen's capture was still on disk and still being served, so the page drew this
screen's boxes over that screen's picture. So a refusal now publishes its reason
as a code, the page hides the picture rather than showing the wrong one, and on
credential screens — and only there — the words come across so the boxes can
label themselves. See `text_is_disclosable`.
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
import uuid
from datetime import datetime
from pathlib import Path

from apt_log import sign as sign_mod
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

# How often the overlay is refreshed, in seconds. It used to trail the picture
# because it only decorated it; in the wireframe view the hierarchy *is* the
# picture, so it now leads. A resident-session read costs ~0.7s, which this
# cadence sustains without starving anything.
HIERARCHY_EVERY = 1.2

# Touched after a tap so the watcher re-reads immediately instead of waiting out
# its interval. A tap is the one moment she is certainly watching for the screen
# to move, and a fixed cadence puts the wait in exactly the wrong place.
POKE_NAME = "hierarchy-poke"

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
# The display's own state, from the same dump. Android keeps mCurrentFocus
# while the screen is off, so "no focus" catches only some sleeps — observed
# on the owner's phone as a green Live over a photograph of a black screen.
_AWAKE = re.compile(r"mAwake=(true|false)")
_PASSWORD_NODE = re.compile(r'password="true"')

# Why no picture was published, as a code rather than a sentence. The prose is
# for the log; this is what the page has to reason about, and a page matching on
# English prose would have broken the first time the wording was improved.
CAPTURED = ""
NO_FOCUS = "no_focus"
LOGIN_ACTIVITY = "login_activity"
PASSWORD_FIELD = "password_field"
SECURE_SCREEN = "secure_screen"
CAPTURE_FAILED = "capture_failed"

# The two refusals that mean "a credential can be typed here" -- as opposed to
# FLAG_SECURE, which is the app's own choice and can be any screen at all.
CREDENTIAL_REFUSALS = (LOGIN_ACTIVITY, PASSWORD_FIELD)

# Fields whose contents are whatever has been typed into them. Never disclosed,
# on any screen, under any rule below: this is where a password lives.
EDITABLE = ("EditText", "AutoCompleteTextView", "SearchView")

# A label longer than this is not a label. Bounds the damage if some screen
# turns out to put a paragraph where this expects a sentence.
MAX_TEXT = 240

# The four apps the portal exists for. Everything else in front is either
# supporting cast (Chrome hosting HHAeXchange+'s web sign-in, a permission
# dialog) or noise (the launcher, whatever the phone drifted onto).
CARE_APPS = (
    "com.hhaexchange.caregiver",
    "com.hhaexchange.uma",
    "com.tellus.evv.v2",
    "com.inmyteam.inmyteam",
)

# The phone's own home screen. Its reflow is a grid of icon glyphs — noise
# wearing a care app's clothes. The page shows a plain card for it instead.
LAUNCHER_APPS = (
    "com.android.launcher3",
    "com.google.android.apps.nexuslauncher",
    "com.sec.android.app.launcher",
    "com.miui.home",
)

# Activity suffix -> the mirror's fixed vocabulary, per package: an atlas of
# which pages belong to which app. Anything unlisted is published as
# "unknown", which is a real answer (see ui/mirror.py) — and lands in the
# flight recorder with its activity name, which is how this table grows.
# Only the legacy app's pages have been walked; the other maps fill in as
# recordings come back.
ACTIVITY_SCREENS = {
    "com.hhaexchange.caregiver": {
        "applaunchactivity": "startup",
        "languageactivity": "language",
        "signinactivity": "login",
        "agencyactivity": "agency",
        "agencyselectionactivity": "agency",   # the name the device reports
        # The migrate-to-the-new-app interstitial after sign-in. "startup"
        # is honest: the app is still on its way in.
        "migrationwebviewactivity": "startup",
        "homeactivity": "home",
        "todayscheduleactivity": "today",
        "visitdetailactivity": "visit",
    },
    # Walked in the second discovery session: sign-in end to end.
    "com.hhaexchange.uma": {
        "authenticationactivity": "login",
        "onboardingactivity": "startup",   # flashes past after sign-in
        "homeactivity": "home",
    },
    "com.tellus.evv.v2": {},
    "com.inmyteam.inmyteam": {},
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


def window_state(serial: str | None = None) -> tuple[str, bool]:
    """(focused window `package/activity`, display awake) — one dumpsys read.

    Both from the same dump because they answer the same question — "what is
    on the screen right now?" — and the second half is not optional: a
    sleeping phone keeps its focused window, so focus alone reports an app on
    a screen that is showing nobody anything.
    """
    try:
        out = _adb(["shell", "dumpsys", "window"], serial).stdout.decode(
            "utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("cannot read the focused window (%s)", exc)
        return "", True
    m = _FOCUS.search(out)
    awake = _AWAKE.search(out)
    return (m.group(1) if m else "",
            awake.group(1) == "true" if awake else True)


def current_focus(serial: str | None = None) -> str:
    """The focused window's `package/activity`, or empty if it cannot be read."""
    return window_state(serial)[0]


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


def activity_of(focus: str) -> str:
    """The focused window's bare activity class name, lowercased.

    dumpsys reports the activity fully qualified on some screens and
    dot-prefixed on others -- `.../com.vendor.app.HomeActivity` next to
    `.../.HomeActivity` -- so the last dot-separated component is the only part
    that is reliably the class name.
    """
    return (focus or "").split("/")[-1].split(".")[-1].lower()


def screen_for(focus: str) -> str:
    """Map a focused window to the mirror's vocabulary, package by package.

    The atlas answers first. For a care app whose page is not in it yet, the
    login markers still answer — the same substrings that refuse the picture
    mean the same thing here (HHAeXchange+ hosts its form under an "auth"
    activity, Mobile Caregiver+ parks behind a "pin" one), so a sign-in
    screen is called one even before anyone has mapped that app's pages.
    """
    package = (focus or "").split("/")[0]
    if package in LAUNCHER_APPS:
        return "launcher"
    activity = activity_of(focus)
    named = ACTIVITY_SCREENS.get(package, {}).get(activity)
    if named:
        return named
    if package in CARE_APPS and looks_like_a_login_screen(activity):
        return "login"
    return "unknown"


# The window manager can strand the phone awake with a resumed app and NO
# focused window (mCurrentFocus=null) — watched live for seventeen minutes:
# the feed reported blind on every tick and the portal sat on "Syncing".
# Re-activating the app that is already on top restores focus without
# changing what is on screen. Patient thresholds, because a second of null
# focus during an ordinary transition is normal life.
NO_FOCUS_NUDGE_AFTER = 60.0
NO_FOCUS_NUDGE_COOLDOWN = 300.0
_no_focus_since = [0.0]
_last_nudge = [0.0]
_RESUMED = re.compile(r"topResumedActivity=ActivityRecord\{\S+ \S+ ([\w.]+)/")


def _refocus(serial: str | None = None) -> None:
    """Restore window focus to whatever activity is already resumed."""
    try:
        out = _adb(["shell", "dumpsys", "activity", "activities"],
                   serial).stdout.decode("utf-8", "replace")
        m = _RESUMED.search(out)
        if not m:
            return
        log.info("focus is null with %s resumed — re-activating it", m.group(1))
        _adb(["shell", "monkey", "-p", m.group(1),
              "-c", "android.intent.category.LAUNCHER", "1"], serial)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not nudge focus back (%s)", exc)


# The notification shade, pulled over whatever she was doing. On a phone
# nobody holds it only ever arrives by accident (an edge swipe misread, a
# stray tap in the status bar), it is never a destination, and its content
# is the owner's private notifications — which must not travel to the
# portal. Collapsed automatically, the way lost focus is nudged back.
_SHADE_MARKS = ("notification_stack", "quick_qs_panel", "qs_tile",
                "NotificationShade")
SHADE_COLLAPSE_COOLDOWN = 20.0
_last_collapse = [0.0]


def _watch_shade(hierarchy: str | None, serial: str | None = None) -> None:
    if not hierarchy or not any(m in hierarchy for m in _SHADE_MARKS):
        return
    now = time.time()
    if now - _last_collapse[0] < SHADE_COLLAPSE_COOLDOWN:
        return
    _last_collapse[0] = now
    log.info("the notification shade is over the screen — collapsing it")
    try:
        _adb(["shell", "cmd", "statusbar", "collapse"], serial)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not collapse the shade (%s)", exc)


# Density, PER APP. One global density (84) was chosen for HHAeXchange+,
# whose six-day schedule only fits one capture that small — but every app
# is different (the owner's observation), and inMyTeam's sparse pages
# gain nothing from tiny text while losing something real: during the
# signature moment the sister and the patient handle the PHYSICAL phone,
# and a bigger UI is a better pen. The watcher applies each care app's
# density as it comes to the front — never mid-scan, never mid-macro,
# and only when the value actually changes.
DEFAULT_DENSITY = 84
APP_DENSITY = {
    "com.inmyteam.inmyteam": 105,
}
# The signature moment gets its own value, found by its fingerprint: the
# legacy app is the only care app that goes LANDSCAPE, and it does so for
# exactly one thing — the signature canvas, where the sister and then the
# patient draw with a finger on the physical phone. 105 is the density
# the one proven signature landed at; the sweet spot is tuned here.
SIGNATURE_DENSITY = 105
_LANDSCAPE_X = re.compile(r"\[(?:8\d{2}|9\d{2}|1\d{3}),")
_density_now = [0]


def _looks_landscape(hierarchy: str | None) -> bool:
    """A bounds x-coordinate past the portrait width means the screen is
    sideways — the reflow has recognised the signature screen this way
    since its first real clock-out."""
    return bool(hierarchy and _LANDSCAPE_X.search(hierarchy))


def _watch_density(focus: str, serial: str | None = None,
                   hierarchy: str | None = None) -> None:
    pkg = (focus or "").split("/")[0]
    if pkg not in CARE_APPS:
        return
    want = APP_DENSITY.get(pkg, DEFAULT_DENSITY)
    if pkg == "com.hhaexchange.caregiver" and _looks_landscape(hierarchy):
        want = SIGNATURE_DENSITY
    if _density_now[0] == want:
        return
    try:
        from apt_log import macros as macros_mod

        if macros_mod.SCAN_ACTIVE.is_set():
            return
        if macros_mod.read_status().state == "running":
            return
    except Exception:  # noqa: BLE001
        pass
    if not _density_now[0]:
        # First sight since this process started: learn what the device
        # is actually set to, so an already-right value is not re-applied
        # (each application re-lays-out every app on the phone).
        try:
            out = _adb(["shell", "wm", "density"], serial).stdout.decode(
                "utf-8", "replace")
            m = re.search(r"Override density: (\d+)", out)
            _density_now[0] = int(m.group(1)) if m else -1
        except (OSError, subprocess.SubprocessError, ValueError):
            _density_now[0] = -1
        if _density_now[0] == want:
            return
    log.info("density %d for %s", want, pkg)
    try:
        _adb(["shell", "wm", "density", str(want)], serial)
        _density_now[0] = want
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not set density (%s)", exc)


# Containment: no command from the portal should ever LEAVE her anywhere
# but the four care apps. Taps are verified against the published frame,
# but a verified tap can still fire an intent — a phone number opens the
# dialer, an address opens Maps, a help link opens the browser, and
# inMyTeam once opened Android's own settings. The owner's rule, verbatim:
# "no command from the front end should land me anywhere unknown besides
# the 4 known app containers." So the watcher tracks the last care app in
# front, and when the foreground is an unsanctioned package for more than
# a moment, it brings that app back.
#
# Sanctioned besides the four: the launcher (scenery — the picker
# experience owns it, and bouncing it would fight the client's own Back
# handling); Chrome CUSTOM TABS (the HHAeXchange+ sign-in lives in one,
# and a custom tab is an app-embedded surface, part of the app's own
# flow) — the full Chrome browser is not; and any moment a macro is
# running, because macros drive the phone through surfaces on purpose.
CONTAIN_DWELL = 5.0
CONTAIN_COOLDOWN = 20.0
_out_since = [0.0]
_last_return = [0.0]
_last_care_app = [""]


def _watch_containment(focus: str, serial: str | None = None) -> None:
    pkg = (focus or "").split("/")[0]
    if pkg in CARE_APPS:
        _last_care_app[0] = pkg
        _out_since[0] = 0.0
        return
    if (not pkg or pkg in LAUNCHER_APPS or pkg == "com.android.systemui"
            or not _last_care_app[0]):
        _out_since[0] = 0.0
        return
    if pkg == "com.android.chrome" and "CustomTabActivity" in (focus or ""):
        _out_since[0] = 0.0
        return
    now = time.time()
    if not _out_since[0]:
        _out_since[0] = now
        return
    if now - _out_since[0] < CONTAIN_DWELL:
        return
    if now - _last_return[0] < CONTAIN_COOLDOWN:
        return
    try:
        from apt_log import macros as macros_mod

        if macros_mod.read_status().state == "running":
            return
    except Exception:  # noqa: BLE001 — an unreadable status never blocks
        pass
    _last_return[0] = now
    _out_since[0] = 0.0
    log.info("foreground is %s — returning to %s", pkg, _last_care_app[0])
    try:
        _adb(["shell", "monkey", "-p", _last_care_app[0],
              "-c", "android.intent.category.LAUNCHER", "1"], serial)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not return to the care app (%s)", exc)


def _watch_focus(focus: str, awake: bool, serial: str | None = None) -> None:
    """Track awake-but-focusless ticks and nudge when they persist."""
    if focus or not awake:
        _no_focus_since[0] = 0.0
        return
    now = time.time()
    if not _no_focus_since[0]:
        _no_focus_since[0] = now
        return
    if (now - _no_focus_since[0] > NO_FOCUS_NUDGE_AFTER
            and now - _last_nudge[0] > NO_FOCUS_NUDGE_COOLDOWN):
        _last_nudge[0] = now
        _refocus(serial)


def capture(serial: str | None = None,
            hierarchy: str | None = None) -> tuple[bytes | None, str, str]:
    """Return (png_or_None, focus, reason).

    `reason` is one of the codes above — why nothing was captured, empty when
    something was. A code rather than a sentence because the page acts on it:
    what it may show her differs between "a password can be typed here" and
    "the app forbids pictures of this screen", and matching that distinction on
    English prose would break the first time the wording improved.

    The hierarchy is passed in rather than fetched: one dump serves both the
    password check and the overlay, which halves the adb work and removes a
    race two callers of the old version had against each other.
    """
    focus, awake = window_state(serial)
    _watch_focus(focus, awake, serial)
    if awake:
        _watch_shade(hierarchy, serial)
        _watch_containment(focus, serial)
        _watch_density(focus, serial, hierarchy)
    if not focus or not awake:
        # A dark display and a missing focus are the same fact for the page:
        # the phone is not showing anyone anything. Publishing the focused
        # app of a black screen is how "Live" ended up over darkness.
        return None, "", NO_FOCUS

    if looks_like_a_login_screen(focus):
        return None, focus, LOGIN_ACTIVITY

    if password_field_in(hierarchy) is True:
        return None, focus, PASSWORD_FIELD

    try:
        shot = _adb(["exec-out", "screencap"], serial, timeout=30.0)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("screencap failed: %s", exc)
        return None, focus, CAPTURE_FAILED

    if shot.returncode != 0 or not shot.stdout:
        # FLAG_SECURE screens refuse capture outright; that is a valid answer.
        return None, focus, SECURE_SCREEN
    return compress(shot.stdout), focus, CAPTURED


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
SCREEN_NAME = "screen.json"


STITCH_DIRNAME = "stitched"


# A stitched page older than this is walked again rather than trusted: the
# identity check below sees structure, not words, and ten minutes is long
# enough for a page's text to have moved on even where its rows have not.
STITCH_MAX_AGE = 600.0

# The share of the viewport's element identities that must appear in the
# stitched document for it to still count as this page. Less than all of
# them, because one volatile node — a clock, a badge count — must not throw
# away a whole-page walk; most of them, because a genuinely different page
# must never wear another page's document.
STITCH_MIN_OVERLAP = 0.8

# The schedule's accordion chevron (see macros.EXPAND_GLYPH): drawn taller
# than wide while a card is folded, rotated once it opens. Identity says
# which PAGE this is; the chevrons say which STATE it is in.
_FOLD_GLYPH = "\uf054"


def _folded_count(statics: list[dict] | None) -> int:
    """How many cards the tree shows still folded shut."""
    n = 0
    for s in statics or []:
        if (s.get("txt") or "").strip() != _FOLD_GLYPH:
            continue
        b = s.get("b") or []
        if len(b) == 4 and (b[3] - b[1]) > (b[2] - b[0]):
            n += 1
    return n


def _fresh_stitch(directory: Path, viewport_id: str,
                  els: list[dict] | None = None,
                  app: str = "", sts: list[dict] | None = None) -> dict | None:
    """The stitched document describing this screen, from the scan cache.

    One file per scanned page, so switching apps does not throw away the
    other app's scans — come back to an unchanged page and it is still
    whole, no re-scan. The exact frame-id match is the fast path. It is
    also brittle on purpose everywhere else — one changed character
    re-hashes the frame — which here meant a ticking clock invalidated a
    walk the moment it finished. So a near-match is accepted too: same
    app in front and nearly every element identity currently in the
    viewport present in the stitched page. Identity is structure and
    place, not words alone, so a different page of the same app — even
    in a single-activity app — does not pass.
    """
    if not viewport_id:
        return None
    root = directory / STITCH_DIRNAME
    now = time.time()
    docs: list[dict] = []
    try:
        files = sorted(root.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for f in files:
        try:
            if now - f.stat().st_mtime > STITCH_MAX_AGE:
                continue
            docs.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    for doc in docs:
        if doc.get("step0") == viewport_id:
            return doc
    if not els or not app:
        return None
    from apt_log import stitch as stitch_mod

    mine = {stitch_mod._key(e) for e in els}
    if not mine:
        return None
    for doc in docs:
        if doc.get("app") != app:
            continue
        # Same page is not enough — same STATE. Coming back from the visit
        # details re-folds today's cards, and the collapsed viewport's
        # elements are a subset of the expanded document's, so the identity
        # test alone dressed a folded phone in an unfolded scan: the owner
        # tapped a card the phone no longer showed, and the tap refused.
        # A viewport more folded than the document means the page moved on;
        # the honest viewport (and the rescan already underway) take over.
        if (sts is not None
                and _folded_count(sts) > _folded_count(doc.get("statics"))):
            continue
        stitched = {stitch_mod._key(e) for e in doc.get("elements") or []}
        if not stitched or len(mine & stitched) / len(mine) < STITCH_MIN_OVERLAP:
            continue
        # And the WORDS must agree, not just the shapes. inMyTeam builds
        # every page from the same anonymous containers, so the element
        # identities of its five tabs are near-identical — the patients
        # tab wore the dashboard's scan, and the Tomorrow list wore
        # Today's. The statics carry each page's actual text; most of the
        # viewport's must appear in the document before it counts.
        if sts is not None and len(sts) >= 3:
            words = {stitch_mod._key(s) for s in sts}
            doc_words = {stitch_mod._key(s) for s in doc.get("statics") or []}
            if len(words & doc_words) / len(words) < STITCH_MIN_OVERLAP:
                continue
        return doc
    return None


def write_screen(target: Path, frame: dict, screen: str, reason: str,
                 hierarchy: str | None, focus: str = "",
                 hierarchy_at: float = 0.0, hierarchy_focus: str = "",
                 stitched: dict | None = None) -> None:
    """Publish the render feed for the wireframe view.

    Two files on purpose, with two policies. `frame.json` is the one that can
    be logged, cached, or pasted into a console: structural, textless outside
    credential screens, unchanged. This one carries the screen's words —
    labels, headings, list rows — because components need them as strings where
    a photograph carries them as pixels.

    Its exposure class is the screenshot's, and it is treated the same way: it
    sits on the same disk beside `last-screen.jpg`, which holds the same words,
    travels only over the tailnet to the same viewer, and is never written to a
    log. Typed-field contents are withheld here exactly as everywhere else —
    the one kind of on-screen text that is worse than what the picture shows,
    because a password field's pixels are dots and its node is not.
    """
    doc = {
        "id": frame["id"],
        "img": frame["img"],
        "at": frame["at"],
        "size": frame["size"],
        "screen": screen,
        # Which app is in front, so a launcher tile can be a switch instead of
        # a ceremony: pressing the tile of the app already showing must not
        # run its sign-in walk over a screen that is already signed in.
        "app": (focus or "").split("/")[0],
        # The bare activity class. A page the atlas cannot name goes into the
        # flight recorder with this attached, which is exactly the datum a
        # future ACTIVITY_SCREENS row is made of.
        "activity": activity_of(focus),
        # When the hierarchy behind this document was last actually read from
        # the device. The document is written every second regardless; this is
        # the number that stops a kept sketch passing as a current one.
        "h_at": hierarchy_at,
        # Whose screen the sketch actually is. `app` above is the focus of
        # this moment; the elements below were read earlier, possibly under a
        # different app. During every app switch the two disagree, and the
        # page must say "syncing", not dress the old app's rows in the new
        # app's name — seen live as the launcher's search row rendered under
        # a title saying HHAeXchange, "finished loading". The tree's own
        # package attributes outrank the focus recorded at read time: the
        # tree can lag the focus through a switch (see hierarchy_package).
        "h_app": (hierarchy_package(hierarchy)
                  or (hierarchy_focus or "").split("/")[0]),
        "blocked": reason,
        "notice": frame.get("notice", ""),
        # A webview's accessibility tree underreports: the migration pitch
        # renders a title and a banner that never appear in it, verified
        # against the pixels. The page says so instead of letting a partial
        # list pass for the whole screen.
        "webview": bool(hierarchy and "android.webkit.WebView" in hierarchy),
        # Whether the page scrolls: the offer to read it end to end only
        # makes sense when there is something below the fold to read.
        "scrollable": bool(hierarchy and 'scrollable="true"' in hierarchy),
        # Sideways (the signature screens are the one place a care app goes
        # landscape): the peek photograph needs turning to be readable.
        # Two shapes of sideways exist — a truly rotated display (wide
        # bounds) and the legacy signature page, which draws its content
        # turned a quarter turn inside a portrait activity (rotated labels
        # betray it). The peek needs the same turn for both.
        "landscape": (_looks_landscape(hierarchy)
                      or sign_mod.presentation_rotated(hierarchy or "")),
        # Whether this document is the WHOLE page (a stitched walk) or the
        # viewport. Full documents leave nothing to wonder about.
        "full": bool(stitched),
        "elements": (stitched["elements"] if stitched
                     else elements(hierarchy, label=True) if hierarchy else []),
        "statics": (stitched["statics"] if stitched
                    else statics(hierarchy) if hierarchy else []),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".stmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("cannot publish the screen document (%s)", exc)

    # Every distinct structure goes to the flight recorder — textless, so an
    # unseen app's ugly screen can be replayed and tuned offline later.
    try:
        from apt_log import flight

        flight.record(doc)
    except Exception as exc:  # noqa: BLE001
        log.warning("flight recorder: %s", exc)

# How stale the published frame may be and still be tapped against. Generous
# against the overlay's own refresh cadence, tight enough that a dead feed
# refuses rather than letting her aim at a screen from minutes ago.
TAP_FRAME_MAX_AGE = 15.0


def write_frame(path: Path, serial: str | None = None,
                hierarchy: str | None = None,
                hierarchy_at: float = 0.0,
                hierarchy_focus: str = "") -> str:
    """Capture once and publish the mirror frame. Returns a one-line status.

    The hierarchy is handed in rather than fetched, so the caller decides how
    often to pay for it. See `run`.
    """
    png, focus, reason = capture(serial, hierarchy)
    screen = screen_for(focus)

    speak = text_is_disclosable(reason)
    els = elements(hierarchy, label=speak) if hierarchy else []

    # The whole page, when the runner has walked it: while the stitched
    # document's first capture still matches what is in front, the portal
    # renders and aims at everything, not just the viewport. The moment the
    # screen moves on, the match fails and the viewport is the truth again.
    stitched = (_fresh_stitch(path.parent, frame_id(els), els=els,
                              app=(focus or "").split("/")[0],
                              sts=statics(hierarchy) if hierarchy else [])
                if not reason else None)
    frame = {
        "id": frame_id(els),
        # Why there is no picture, so the page can say something better than
        # nothing. Empty when there is one.
        "blocked": reason,
        # The words of a dialog she would otherwise be facing blind. Empty
        # unless this is a credential screen with an alert on it.
        "notice": alert_message(hierarchy) if speak else "",
        # Separate from the structural id on purpose. Typing into a field moves
        # no targets at all, so a client refreshing only on structure change
        # would show her a picture with none of her own keystrokes in it.
        "img": hashlib.sha256(png).hexdigest()[:12] if png else "",
        "at": datetime.now().isoformat(),
        "size": screen_size(serial),
        # Stitched elements carry original bounds plus their scroll step,
        # so a below-the-fold aim verifies here exactly like any other.
        "elements": stitched["elements"] if stitched else els,
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

    write_screen(path.parent / SCREEN_NAME, frame, screen, reason, hierarchy,
                 focus, hierarchy_at, hierarchy_focus, stitched=stitched)

    if png:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(png)
        tmp.replace(path)
    # The frame is published either way. "On the sign-in screen, no picture
    # available" is information; silence looks identical to the process being
    # dead.
    #
    # A refusal used to publish step "blocked", which the page renders as "it
    # has stopped and cannot continue on its own" — printed directly above a
    # sign-in screen the controller was walking through perfectly well. Whether
    # a picture may be taken says nothing about whether anything is stuck, and
    # conflating the two turns a working system into an alarming one.
    step = "waiting" if screen == "unknown" else "working"

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

    def __init__(self, serial: str | None, every: float,
                 poke_path: Path | None = None):
        self._serial = serial
        self._every = every
        self._poke = poke_path
        self._poked_at = 0.0
        self._xml: str | None = None
        self._read_at = 0.0
        self._focus = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def xml(self) -> str | None:
        with self._lock:
            return self._xml

    @property
    def read_at(self) -> float:
        """When a read last *succeeded* — kept xml can be much older.

        The published sketch was once minutes old while the document carrying
        it was stamped fresh every second: the resident session was down, the
        fallback dumps were failing, and the watcher was serving its last good
        hierarchy — a modal that had long since been dismissed — under a green
        "Live". The sketch's honesty has to travel with the sketch's age.
        """
        with self._lock:
            return self._read_at

    @property
    def focus(self) -> str:
        """The focused window that came with the kept hierarchy.

        Not the focus of this moment — the focus of the sketch. The two
        diverge during every app switch, and publishing the sketch under the
        new app's name was how the launcher's rows appeared under a title
        saying HHAeXchange the instant the loading overlay dropped.
        """
        with self._lock:
            return self._focus

    def _accept(self, fresh: str, focus: str, awake: bool = True) -> bool:
        """Whether a fresh read should replace what we are showing.

        Guarding against None was not enough. A read can succeed and come back
        structurally empty -- observed alternating 16 targets, 16, then 0, 0, 0
        on a launcher that had not changed -- and an empty-but-valid result
        happily overwrote a good one, so the overlay kept blinking out.

        An empty read is only believed when the screen has actually changed.
        Otherwise the previous boxes stay: they are stale, and if they are wrong
        she can see they are wrong, which is more than an empty overlay offers.
        """
        if not focus or not awake:
            # The display turned off. There is no screen now, so nothing can
            # replace the last one — which is exactly what the page shows as
            # "the last thing on screen before it went dark". The junk read a
            # sleeping device returns (one stray node) used to be accepted
            # here because the focus "changed", wiping the memory the owner
            # asked to keep. `awake` matters separately: a sleeping phone
            # keeps its focused window, so focus alone misses most sleeps.
            return False
        if elements(fresh):
            return True
        if focus != self._focus:
            return True                      # genuinely a different screen
        return not elements(self._xml or "")  # nothing better to keep

    def _loop(self) -> None:
        from apt_log import macros as macros_mod

        while not self._stop.is_set():
            # A scan owns the one Appium session while it walks the page.
            # Interleaving a dump here made the scan crawl and made the live
            # view lurch through the scroll. Hold the last complete frame
            # instead — the read_at is refreshed so it does not read as
            # stale (the screen IS being read, just aggregated), and the
            # scanning animation already says work is in progress.
            if macros_mod.SCAN_ACTIVE.is_set():
                with self._lock:
                    self._read_at = time.time()
                self._wait()
                continue
            try:
                fresh = read_hierarchy(self._serial)
                if fresh is not None:
                    focus, awake = window_state(self._serial)
                    with self._lock:
                        self._read_at = time.time()
                        if self._accept(fresh, focus, awake):
                            self._xml = fresh
                            self._focus = focus
            except Exception as exc:  # noqa: BLE001
                log.warning("hierarchy read failed: %s", exc)
            self._wait()

    def _wait(self) -> None:
        """Sleep out the interval — unless a tap pokes us awake.

        The poke crosses from the UI process as a file mtime, because the two
        processes share a disk and nothing else. Slices rather than a single
        wait so the poke is noticed within a fraction of a second, which is the
        whole point of it: after a tap she is certainly watching.
        """
        deadline = time.monotonic() + self._every
        while not self._stop.is_set() and time.monotonic() < deadline:
            if self._poke is not None:
                try:
                    stamp = self._poke.stat().st_mtime
                    if stamp > self._poked_at:
                        self._poked_at = stamp
                        return
                except OSError:
                    pass
            self._stop.wait(0.15)

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
    watcher = _Hierarchy(serial, HIERARCHY_EVERY, poke_path=path.parent / POKE_NAME)
    watcher.start()

    # Macros run here rather than in the web process, because this is where the
    # resident Appium session lives and UiAutomator2 allows exactly one. A second
    # session in the UI cost 14 seconds a tap when it was tried.
    from apt_log.macros import Runner

    runner = Runner()
    runner.start()

    count = 0
    try:
        while iterations is None or count < iterations:
            try:
                log.info("%s", write_frame(path, serial, watcher.xml,
                                            watcher.read_at, watcher.focus))
            except Exception as exc:  # noqa: BLE001
                # A watcher that dies on one bad read stops being a watcher.
                log.warning("frame failed: %s", exc)
            count += 1
            if iterations is None or count < iterations:
                time.sleep(interval)
    finally:
        watcher.stop()
        runner.stop()


def _attr(node: str, name: str) -> str:
    m = re.search(rf'{name}="([^"]*)"', node)
    return m.group(1) if m else ""


def hierarchy_package(xml: str | None) -> str:
    """The app the TREE itself belongs to, by majority of its nodes.

    The focus recorded when the hierarchy was read is not the same fact:
    during an app switch the window manager names the new app while the
    accessibility tree still serves the old one's nodes — watched live as
    the legacy home screen published under HHAeXchange+'s name, exactly
    the mislabeled-sketch lie h_app exists to prevent. Every node carries
    a package attribute; the tree's own majority is the honest answer.
    System overlay nodes (the status bar's) are not candidates.
    """
    if not xml:
        return ""
    counts: dict[str, int] = {}
    for pkg in re.findall(r' package="([^"]*)"', xml):
        if pkg and pkg != "com.android.systemui":
            counts[pkg] = counts.get(pkg, 0) + 1
    return max(counts, key=counts.__getitem__) if counts else ""


def elements(xml: str, label: bool = False) -> list[dict]:
    """The tappable structure of a screen, carrying no text.

    This is what the page draws boxes from and what a tap posts back, so it is
    built to hold nothing worth protecting. The words stay in the screenshot,
    where they are already visible to whoever is looking at the page; the
    structure is what crosses the wire a second time and lands in a log.

    `has_text` is deliberately a boolean. Knowing a row has a label is enough to
    draw it; knowing the label is a patient name.

    `label=True` adds `txt` — and is only ever passed when there is no
    screenshot to read the words off, on a screen with no patient in it. See
    `text_is_disclosable`, which is the only caller allowed to decide that.
    Editable fields are excluded even then: their text is what has been typed.
    """
    found = []
    for raw in _NODE.findall(xml or ""):
        if _attr(raw, "clickable") != "true":
            continue
        # The status bar's and shade's own nodes are never care content —
        # and the shade's are the owner's private notifications, which a
        # reflow once painted into the portal as shredded columns.
        if _attr(raw, "package") == "com.android.systemui":
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
        short = cls.rsplit(".", 1)[-1]
        entry = {
            "rid": _attr(raw, "resource-id").split("/")[-1],
            "cls": short,
            "b": [x1, y1, x2, y2],
            "focused": _attr(raw, "focused") == "true",
            "selected": _attr(raw, "selected") == "true",
            # The wireframe draws a switch as a switch, so it has to know which
            # way it is thrown. Not part of frame identity: flipping a checkbox
            # must not invalidate her aim at the one next to it.
            "checked": _attr(raw, "checked") == "true",
            "has_text": bool(_attr(raw, "text")),
        }
        if label and short not in EDITABLE:
            entry["txt"] = _clean(_attr(raw, "text"))
        found.append(entry)
    return found


# The wireframe draws labels too, and an unbounded screen would make the pushed
# fragment unbounded with it. A phone screen holds nowhere near this many.
MAX_STATICS = 120


def statics(xml: str) -> list[dict]:
    """The non-tappable text of a screen — labels, headings, list rows.

    This is what makes the wireframe readable rather than a field of grey
    boxes. Editable fields are excluded here for the same reason they are
    excluded from labels: their text is whatever has been typed into them,
    and on a sign-in screen that is a credential.

    Named ImageViews ride along with no text: the visits list marks each
    verified EVV record with a drawable check (imgStartTime/imgEndTime),
    and an image's identity is its resource-id — the renderer decides
    which ids mean something. Anonymous decoration stays out.
    """
    out = []
    for raw in _NODE.findall(xml or ""):
        if _attr(raw, "clickable") == "true":
            continue
        if _attr(raw, "package") == "com.android.systemui":
            continue                      # see elements(): never care content
        cls = (_attr(raw, "class") or raw[1:].split()[0].rstrip("/>")).rsplit(".", 1)[-1]
        if cls in EDITABLE:
            continue
        text = _clean(_attr(raw, "text"))
        rid = _attr(raw, "resource-id").split("/")[-1]
        if not text and not (cls == "ImageView" and rid):
            continue
        m = _BOUNDS.search(_attr(raw, "bounds"))
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        entry = {"cls": cls, "b": [x1, y1, x2, y2], "txt": text}
        if not text:
            entry["rid"] = rid
        out.append(entry)
        if len(out) >= MAX_STATICS:
            break
    return out


# ------------------------------------------------------------------- alerts
# Android's own alert ids, plus this app's. A dialog is recognised by its
# buttons rather than by its message, because the buttons are the part that is
# structural: `btn_negative` is what this app calls the single "DE ACUERDO" on
# every alert it raises, and `button1`/`button2` are what the platform's
# AlertDialog calls its own.
ALERT_BUTTONS = ("btn_negative", "btn_positive", "button1", "button2", "button3",
                 "autofill_save_no", "autofill_save_yes")

# Where an alert keeps its sentence, most specific first.
ALERT_MESSAGES = ("lbl_message", "message", "alertTitle", "lbl_title",
                  "autofill_save_title")


def text_is_disclosable(reason: str) -> bool:
    """Whether the words on this screen may be sent to her page.

    The element map carries no text, and that rule earns its keep on every
    screen the mirror can photograph — because there the words are already in
    the picture, and a second copy in a JSON file on disk is pure exposure with
    no benefit.

    On a screen the mirror *refuses* to photograph, the arithmetic inverts. She
    is shown nothing at all: an unlabelled rectangle where a dialog is, and no
    way to know whether the button under her thumb says "sign in again" or
    "delete". That is how a caregiver ends up walking four floors to read one
    sentence, and it is what this predicate exists to prevent.

    So text is disclosed exactly where a picture is refused *for being a
    credential screen* — a screen the app reaches before any patient is loaded,
    where there is no name to leak. FLAG_SECURE refusals are excluded: that is
    the app's own choice and can land on any screen, including a patient's.

    Editable fields are never disclosed regardless (see `elements`), because a
    password's own node carries what has been typed into it.
    """
    return reason in CREDENTIAL_REFUSALS


def _clean(text: str) -> str:
    # Unescape first: attribute values arrive XML-escaped, and a title with a
    # line break in it otherwise renders a literal "&#10;" — seen on the real
    # visit screen the first time the wireframe drew one.
    import html as html_mod

    return re.sub(r"\s+", " ", html_mod.unescape(text or "")).strip()[:MAX_TEXT]


def _nodes(xml: str | None) -> list[dict]:
    """Every node, with its resource-id kept whole.

    `elements` throws the package away, and here it is the discriminator: a
    dialog raised by the platform is `android:id/...` sitting on top of a screen
    that is `com.somebody:id/...`, and telling those apart is what stops the
    sentence behind a modal being read out as the modal's own.
    """
    out = []
    for raw in _NODE.findall(xml or ""):
        rid = _attr(raw, "resource-id")
        package, _, name = rid.rpartition("/")
        m = _BOUNDS.search(_attr(raw, "bounds"))
        out.append({
            # removesuffix, not rstrip: rstrip takes a character set, and
            # "android:id".rstrip(":id") is "andro".
            "package": package.removesuffix(":id"),
            "name": name,
            "clickable": _attr(raw, "clickable") == "true",
            "cls": (_attr(raw, "class") or "").rsplit(".", 1)[-1],
            "text": _clean(_attr(raw, "text")),
            "b": [int(g) for g in m.groups()] if m else None,
        })
    return out


def alert_showing(xml: str | None) -> bool:
    """True when a dialog's buttons are on screen."""
    return any(n["clickable"] and n["name"] in ALERT_BUTTONS for n in _nodes(xml))


def alert_message(xml: str | None) -> str:
    """The sentence a dialog is showing, or empty.

    Prefers the known message ids. Where the layout is one nobody has seen — and
    three of the four apps are — it falls back to the static text sitting
    directly above the dialog's buttons, which is where every dialog on this
    platform puts its message.

    Both paths are confined to the dialog's own package. The first version took
    the longest text anywhere on screen, which on a real sign-in screen behind
    the system's save-password prompt would have read out the device
    registration id instead of the question being asked.
    """
    nodes = _nodes(xml)
    buttons = [n for n in nodes
               if n["clickable"] and n["name"] in ALERT_BUTTONS and n["b"]]
    if not buttons:
        return ""

    packages = {n["package"] for n in buttons}
    top = min(b["b"][1] for b in buttons)

    static = [n for n in nodes
              if not n["clickable"] and n["text"] and n["cls"] not in EDITABLE
              and n["package"] in packages]

    for wanted in ALERT_MESSAGES:
        for node in static:
            if node["name"] == wanted:
                return node["text"]

    # Nearest above the buttons; longest breaks a tie, since a title and its
    # message often share a baseline and the message is the useful half.
    above = [n for n in static if n["b"] and n["b"][3] <= top]
    if not above:
        return ""
    return max(above, key=lambda n: (n["b"][3], len(n["text"])))["text"]


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
    if match is not None and int(match.get("step") or 0) > 0:
        # Below the fold: the runner replays the scroll, re-verifies the
        # element against a fresh dump at its step, and taps the FOUND
        # bounds — the same refuse-if-moved promise, extended past the
        # viewport. This process cannot dump the screen itself (the feed
        # holds the only UiAutomator2 session), so the work crosses on a
        # request file like every macro does.
        return _deep_tap(claimed_frame, match)
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

    # Wake the hierarchy watcher in the feed process so the next wireframe
    # arrives as soon as the screen settles, not an interval later.
    try:
        from apt_log.ui.state import STATE_DIR

        (STATE_DIR / POKE_NAME).write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass
    return {"tapped": {"rid": match["rid"], "cls": match["cls"], "at": [cx, cy]}}


TYPABLE = ("EditText", "AutoCompleteTextView", "SearchView",
           "MultiAutoCompleteTextView")
# A short plain token: the case this exists for is a verification code
# read off a family member's phone. Letters and digits only — nothing
# that needs shell quoting, nothing essay-length.
_TYPABLE_VALUE = re.compile(r"^[A-Za-z0-9]{1,32}$")


def type_into(claimed_frame: str, element: dict, value: str,
              serial: str | None = None,
              frame_path: Path | None = None) -> dict:
    """Type into a field she aimed at, under tap's refuse-if-moved contract.

    Only fields take text; the field is focused with a tap first, then the
    value travels over `input text`. Honest limitation, same as the phone
    PIN: `input text` takes the value as an argv element, so it is briefly
    visible in `ps` on this host and on the phone. The value is never
    logged here, and the length cap keeps this a token channel — a
    verification code, not a message pipe.
    """
    if not isinstance(value, str) or not _TYPABLE_VALUE.match(value or ""):
        raise ValueError("only a short plain code can be typed")
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
        raise StaleAim("that is no longer on the screen — look again")
    if int(match.get("step") or 0) > 0:
        raise StaleAim("scroll the field into view first")
    if not any((match.get("cls") or "").endswith(t) for t in TYPABLE):
        raise StaleAim("that is not a text field")

    x1, y1, x2, y2 = match["b"]
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    focus = _adb(["shell", "input", "tap", str(cx), str(cy)], serial,
                 timeout=20.0)
    if focus.returncode != 0:
        raise StaleAim("adb refused to focus the field")
    time.sleep(0.6)
    result = _adb(["shell", "input", "text", value], serial, timeout=20.0)
    if result.returncode != 0:
        raise StaleAim("adb refused the text")

    # Identity and length only — never the value.
    log.info("typed %d chars into %s/%s on frame %s",
             len(value), match["cls"], match["rid"] or "-", claimed_frame)
    try:
        from apt_log.ui.state import STATE_DIR

        (STATE_DIR / POKE_NAME).write_text(str(time.time()),
                                           encoding="utf-8")
    except OSError:
        pass
    return {"typed": {"rid": match["rid"], "cls": match["cls"],
                      "chars": len(value)}}


def _deep_tap(claimed_frame: str, match: dict) -> dict:
    """Hand a below-the-fold tap to the runner and wait for its verdict."""
    from apt_log import macros as macros_mod

    rid = uuid.uuid4().hex[:12]
    aim = {"rid": match.get("rid", ""), "cls": match.get("cls", ""),
           "b": list(match["b"]), "step": int(match.get("step") or 0)}
    try:
        macros_mod.DEEPTAP_REQUEST_PATH.parent.mkdir(parents=True,
                                                     exist_ok=True)
        tmp = macros_mod.DEEPTAP_REQUEST_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"id": rid, "aim": aim,
                                   "at": time.time()}), encoding="utf-8")
        os.replace(tmp, macros_mod.DEEPTAP_REQUEST_PATH)
    except OSError as exc:
        raise StaleAim(f"could not reach the controller ({exc})") from None

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        try:
            result = json.loads(macros_mod.DEEPTAP_RESULT_PATH.read_text(
                encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result = None
        if result and result.get("id") == rid:
            if result.get("ok"):
                log.info("deep-tapped %s/%s at step %d on frame %s",
                         aim["cls"], aim["rid"] or "-", aim["step"],
                         claimed_frame)
                return {"deep": True}
            raise StaleAim(result.get("error")
                           or "that is no longer on the screen — look again")
        time.sleep(0.3)
    raise StaleAim("the tap below the fold timed out — look again")
