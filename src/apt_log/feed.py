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
import shlex
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
# The app underneath, when the focused WINDOW is not an activity.
#
# A dropdown, a spinner list, a popup menu and a context menu are all their own
# window, and Android titles that window rather than naming a package:
#
#     mCurrentFocus=Window{3d46066 u0 Pop-Up Window}
#     mFocusedApp=ActivityRecord{a816218 u0 com.inmyteam.inmyteam/.view…}
#
# So the focus read came back with the literal string "Window", and every check
# downstream took the phone to be somewhere that is not a care app. The
# containment watchdog then did what it is for and brought inMyTeam back —
# every twenty seconds, for as long as the dropdown was open, each return
# resyncing the app. Reported from the field as "the app gets stuck on
# syncing", and visible in the feed's own log as
# `foreground is Window — returning to com.inmyteam.inmyteam`, over and over.
#
# The answer was already in the same dump: mFocusedApp names the activity that
# OWNS the popup. This is the same distinction §6's permission-dialog fix
# turns on — a surface the app in front raised is not the phone wandering —
# and it generalises past inMyTeam's agency filter to every dropdown in all
# four apps.
_FOCUSED_APP = re.compile(
    r"mFocusedApp=\S*ActivityRecord\{[^}]*?\s([A-Za-z0-9_.]+/[A-Za-z0-9_.]+)")


# System panels that sit OVER the app rather than belonging to it. These are
# bare tokens like the popups below, and the owner substitution must not touch
# them: the notification shade is not inMyTeam's surface, it is the system's,
# covering inMyTeam — and reporting the app underneath told every check that
# the phone was fine while a wall of the owner's private notifications sat on
# the screen. The containment watchdog is what used to recover that, and
# naming the app is exactly what stopped it doing so.
_SYSTEM_WINDOWS = ("notificationshade", "statusbar", "volumedialog",
                   "screendecoroverlay", "navigationbar", "shadecarrier",
                   "inputmethod", "assistpreviewpanel")


def _is_a_system_panel(focus: str) -> bool:
    lowered = (focus or "").replace(" ", "").replace("-", "").lower()
    return any(name in lowered for name in _SYSTEM_WINDOWS)


# The panels she can be genuinely STUCK behind — a strict subset of the above,
# because the two lists answer different questions. The list above asks "is
# this window the app's?", and for the owner substitution the keyboard is not.
# This one asks "is she looking at something she cannot get out of?", and the
# keyboard is not that either: it is up because a field was tapped, it belongs
# to what she is doing, and offering to clear the screen every time she types
# would be furniture. The shade that sat over inMyTeam until it was cleared by
# hand is the case this exists for.
_COVERING_WINDOWS = ("notificationshade", "statusbar", "volumedialog",
                     "shadecarrier")


def screen_is_covered(focus: str) -> bool:
    """True when a system panel is over the app rather than beside it.

    Nothing the portal sends reaches the app while this holds — the taps go to
    the panel, and the app underneath is not the thing with the focus. The
    page is told so it can stop presenting a screen that will not answer and
    offer the one act that does.
    """
    lowered = (focus or "").replace(" ", "").replace("-", "").lower()
    return any(name in lowered for name in _COVERING_WINDOWS)


def _is_a_window_title(focus: str) -> bool:
    """True when the focus read named a window rather than an activity.

    A real focus is `package/activity` and always carries both a slash and a
    dot. "Pop-Up Window" collapses to the bare word `Window`, which has
    neither. Deliberately NOT a list of known popup titles: the ANR dialog
    already proved that matching Android's window prose breaks the first time
    the wording or the locale changes, and an unknown popup should degrade to
    "the app underneath" rather than to "somewhere else entirely".
    """
    token = focus or ""
    return bool(token) and "/" not in token and "." not in token
# Android's "<app> isn't responding — Close app / Wait" dialog owns the focus
# while it is up, and its window title names the wedged package. This is the
# ONLY reliable signal for it: the dialog is a system window the accessibility
# tree does not hand over, and an ANR takes UiAutomator2 down with it — the
# feed logged "Appium did not open a session within 40s" on repeat while the
# app hung. So detection cannot depend on a tree; it rides the dumpsys read
# the focus already costs. Seen live: the legacy app wedged on its own
# inactivity dialog, and the portal went on publishing that dialog's buttons
# as if they worked.
_ANR = re.compile(
    r"mCurrentFocus=Window\{[^}]*Application Not Responding:\s*([\w.]+)")
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
# The app is wedged behind Android's not-responding dialog. Unlike every other
# refusal here, this one means the buttons DO NOT work — the other messages all
# end "and they still work", and saying that here would be a lie she would
# discover by tapping.
APP_NOT_RESPONDING = "app_not_responding"

# The two refusals that mean "a credential can be typed here" -- as opposed to
# FLAG_SECURE, which is the app's own choice and can be any screen at all.
CREDENTIAL_REFUSALS = (LOGIN_ACTIVITY, PASSWORD_FIELD)

# Fields whose contents are whatever has been typed into them. Never disclosed,
# on any screen, under any rule below: this is where a password lives.
EDITABLE = ("EditText", "AutoCompleteTextView", "SearchView")

# Classes whose content-desc is a picture's ALT TEXT rather than a label.
# Alt text is written for somebody who cannot see the image; read as a
# statement it produces the app's logo announcing itself where the page title
# belongs. On a clickable node the same attribute names a control and is kept
# — see statics(), which is the only place this applies.
DECORATIVE = ("ImageView", "ImageButton")

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

# RETIRED, NOT DEMOLISHED.
#
# The legacy HHAeXchange app had one patient on it, and that patient has been
# migrated to HHAeXchange+ — the app itself said so for months, on the very
# interstitial this project mapped as "startup": "If you do not see your
# Agency below, please use the HHAeXchange+ Application."
#
# So it stops being somewhere the phone GOES. It is off the picker, it never
# signs itself in, and nothing here reopens it.
#
# It does NOT stop being an app this code can read, and everything that reads
# it stays exactly where it is:
#
#   * the page atlas below, which is the record of what its screens were;
#   * `presentation_rotated` and ROTATED_CANVAS_APPS in sign.py — this is the
#     ONLY app that draws its signature page a quarter turn round inside a
#     portrait activity, and that fact is the whole reason the peek and the
#     ink carry separate "wide" and "turned" answers. Delete it and the next
#     person to read that code cannot tell why either flag exists;
#   * the EVV check marks on its visits list, and their entry/exit names;
#   * its session-expiry wordings, and its sign-in macro, which stays
#     registered so a record can still be fetched deliberately.
#
# The cost of keeping all of that is a table nobody reads. The cost of
# deleting it is losing the evidence for rules that still run.
RETIRED_APPS = ("com.hhaexchange.caregiver",)


def retired(package: str) -> bool:
    """Whether this app is one the portal no longer takes the phone into."""
    return (package or "") in RETIRED_APPS

# Android's own permission dialogs. Not a place the phone can wander to —
# only a care app asking for something can raise one, and it is raised OVER
# that app, mid-flow. Sanctioned for exactly that reason: bouncing it would
# cancel the request that opened it.
PERMISSION_APPS = (
    "com.google.android.permissioncontroller",
    "com.android.permissioncontroller",
    "com.android.packageinstaller",
)

# The phone's own Settings. Somewhere the portal can be ASKED to go — wifi,
# sound, the phone's own display size — which is different from somewhere it
# wandered. Without this the watchdog bounced it back to a care app five
# seconds after the button that opened it did its job.
SETTINGS_APPS = (
    "com.android.settings",
    "com.samsung.android.settings",
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
        # Two screens under one activity. It DOES flash past after sign-in on
        # an account with a single provider — which is what it was mapped
        # from — but on an account with more than one it stops there and
        # asks, and the console said "the app is starting up" while she was
        # being asked to choose. The activity cannot tell them apart, so
        # `screen_for` looks at the page. See PICKER_MARKS.
        "onboardingactivity": "startup",
        "homeactivity": "home",
    },
    # Walked live and confirmed by the flight recorder. The passcode screen
    # already answered "login" through the activity markers — "pin" is one of
    # them — but the atlas is where a page's name belongs, and the dashboard
    # had no name at all: the console said "unknown" about this app's main
    # screen for as long as it has been installed.
    "com.tellus.evv.v2": {
        "pinactivity": "login",
        "dashboardactivity": "home",
    },
    # One entry, and it earns its place twice over. The console called this
    # app's main screen "unknown" for as long as it has been installed — the
    # same complaint Mobile Caregiver+'s dashboard had — and `_app_home` needs
    # a name for a front page or it walks an app backwards out of itself
    # looking for one it can never recognise.
    #
    # Read off the live device: com.inmyteam.inmyteam.view.activities
    # .MainActivity is what the app sits on once it is signed in.
    "com.inmyteam.inmyteam": {
        "mainactivity": "home",
    },
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


# WHERE THE APP THINKS IT IS, IN ITS OWN WORDS.
#
# Asked directly: "does adb expose views on the app?" It does, and what it
# exposes is better than the map we were considering building. `dumpsys
# activity` publishes the FragmentManager, and inside it three things this
# project has never had:
#
#   * the fragment on screen now, by class name — `VisitsFragment` on the
#     visits hub, `MyWorksFragment` on the work log. The ATLAS CANNOT SAY
#     THIS: it keys on the activity, and inMyTeam has exactly one for all of
#     them, so `screen_for` answers "home" wherever it stands.
#   * the back stack, one entry per fragment pushed. EMPTY MEANS THE NEXT
#     BACK LEAVES THE APP, which is the fact every Back press in this project
#     has had to discover by pressing and looking at what happened.
#   * each entry's operations, which name the fragment removed and the one
#     added — a directed edge, so the entries in order are the trail walked
#     to get here.
#
# It cannot go stale the way a hand-drawn map would: it is the app answering,
# not us remembering, so an app update changes the answer rather than quietly
# invalidating it.
#
# WHAT IT IS NOT is a count of Back presses. Watched live: two presses on the
# work log were swallowed undoing its tab selection and popped nothing, while
# the screen's own Back arrow popped cleanly. Depth counts POPS REMAINING;
# presses have to be made one at a time and checked.
_FRAGMENT_ON_SCREEN = re.compile(r"#\d+: ([A-Za-z]\w*Fragment)\{")
_BACK_ENTRY = re.compile(r"#\d+: BackStackEntry\{")
_BACK_OP = re.compile(r"Op #\d+: (ADD|REMOVE) ([A-Za-z]\w*Fragment)\{")

# Carried by every Jetpack screen; says nothing about where anybody is.
PLUMBING_FRAGMENTS = ("ReportFragment", "NavHostFragment",
                      "SupportRequestManagerFragment")


def _pretty_fragment(name: str) -> str:
    """`MyWorksFragment` as "My Works" — the app's own word for the screen,
    spaced so a person can read it. Never translated: it is a class name, and
    inventing a friendlier one is how a breadcrumb starts lying."""
    stem = name[:-len("Fragment")] if name.endswith("Fragment") else name
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", stem).strip()


_nav_seen: tuple = ()


def nav_state(package: str, serial: str | None = None,
              stamp: float | None = None) -> dict:
    """Where the app is, how it got there, and whether Back would leave.

    `{"at": "MyWorksFragment", "trail": [...], "depth": 1, "rooted": False}`
    — and `{}` when the phone will not say, which callers must read as "no
    idea" rather than as any particular answer.

    `stamp` ties the answer to the hierarchy read it belongs with. MEASURED
    ON THE PI AT 100ms A CALL, and the document is published every second
    while the tree behind it is read about half as often — so without this
    the page would spend a tenth of a core asking a question whose answer
    cannot have changed. Pass no stamp to force a fresh look, which is what
    the Back guard wants: it is deciding whether to send a press right now.
    """
    global _nav_seen

    if not package:
        return {}
    if stamp is not None and _nav_seen[:2] == (package, stamp):
        return _nav_seen[2]
    try:
        out = _adb(["shell", "dumpsys", "activity", package], serial
                   ).stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return {}
    here = [n for n in _FRAGMENT_ON_SCREEN.findall(out)
            if n not in PLUMBING_FRAGMENTS]
    if not here:
        return {}
    depth = len(_BACK_ENTRY.findall(out))
    # Each entry removes the fragment it left and adds the one it opened, so
    # the removals in order are the trail behind, and where we stand is last.
    trail, seen = [], set()
    for kind, name in _BACK_OP.findall(out):
        if kind == "REMOVE" and name not in seen:
            seen.add(name)
            trail.append(name)
    if here[0] not in trail:
        trail.append(here[0])
    state = {
        "at": here[0],
        "at_says": _pretty_fragment(here[0]),
        "trail": trail,
        "says": [_pretty_fragment(n) for n in trail],
        "depth": depth,
        # THE ESCAPE GUARD. Nothing left to pop means the next Back press
        # pops the activity itself and lands in whatever was underneath.
        "rooted": depth == 0,
    }
    if stamp is not None:
        _nav_seen = (package, stamp, state)
    return state


def window_state(serial: str | None = None) -> tuple[str, bool, str]:
    """(focused window `package/activity`, display awake, wedged package).

    All three from one dumpsys read because they answer the same question —
    "what is on the screen right now?" — and none of them is optional. A
    sleeping phone keeps its focused window, so focus alone reports an app on
    a screen that is showing nobody anything. And an app behind the
    not-responding dialog looks, through the focus alone, exactly like that
    app running normally: the dialog's window title ends in the package name,
    so the focus pattern reads it as a bare package and every check downstream
    waves it through. The third value is what makes the difference visible.
    """
    try:
        out = _adb(["shell", "dumpsys", "window"], serial).stdout.decode(
            "utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("cannot read the focused window (%s)", exc)
        return "", True, ""
    m = _FOCUS.search(out)
    awake = _AWAKE.search(out)
    anr = _ANR.search(out)
    focus = m.group(1) if m else ""
    # A popup owns the focus while it is open and Android titles that window
    # instead of naming a package, so the read comes back "Window" and the app
    # underneath disappears from every check downstream. mFocusedApp still
    # names it. Only when the window is not an activity at all — never over a
    # real focus, and never over the ANR dialog, whose title does end in the
    # wedged package and which the caller must keep seeing.
    if _is_a_window_title(focus) and not anr and not _is_a_system_panel(focus):
        owner = _FOCUSED_APP.search(out)
        if owner:
            focus = owner.group(1)
    return (focus,
            awake.group(1) == "true" if awake else True,
            anr.group(1) if anr else "")


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


# A screen that ASKS, under an activity whose name says it is still loading.
# HHAeXchange+ puts its provider picker inside `onboardingactivity`, which on
# a single-provider account really is a splash — so the atlas is right about
# the activity and wrong about this page, and the console reported "the app
# is starting up" while she was being asked which agency to work under.
#
# The id is the app's own and appears nowhere else.
PICKER_MARKS = ("agency_configuration_screen_add_connection_button",)


# A PASSCODE KEYPAD IS A CREDENTIAL SCREEN WHEREVER IT IS DRAWN.
#
# Every credential test in this file reads the ACTIVITY's name, and Mobile
# Caregiver+ puts its keypad up under two different ones: `PinActivity`,
# which the markers catch, and `DashboardActivity`, which they do not. Caught
# live — a complete keypad, ten digits and a delete key, published as
# `screen=home, blocked=''`. Nothing recognised it: no credential refusal, so
# the picture was taken and auto sign-in never even reached its own gate.
# Reported as "Mobile Care+ needs better auto auth", and this is most of it.
#
# The shape is unmistakable and needs no activity's help: eight or more
# BUTTONS whose whole text is one digit. A list of numbers is not buttons; a
# keypad missing a key or two while it draws still counts, which is the
# point.
#
# Read one node at a time rather than with a single pattern spanning both
# attributes, because the two dialects order them differently — `uiautomator
# dump` writes text before class, Appium's page_source writes class before
# text. A pattern that wanted one order would have found the keypad on one
# read path and missed it on the other, which is the worse of the two bugs:
# a credential screen recognised only sometimes.
_NODE_TAG = re.compile(r"<[A-Za-z][^>]*>")
_BUTTON_CLASS = re.compile(r'\bclass="[^"]*Button"')
_DIGIT_TEXT = re.compile(r'\btext="(\d)"')
KEYPAD_MIN_KEYS = 8


def keypad_on_screen(xml: str | None) -> bool:
    """Whether a numeric passcode keypad is on screen."""
    digits = set()
    for tag in _NODE_TAG.findall(xml or ""):
        if not _BUTTON_CLASS.search(tag):
            continue
        key = _DIGIT_TEXT.search(tag)
        if key:
            digits.add(key.group(1))
    return len(digits) >= KEYPAD_MIN_KEYS


def screen_for(focus: str, hierarchy: str | None = None) -> str:
    """Map a focused window to the mirror's vocabulary, package by package.

    The atlas answers first. For a care app whose page is not in it yet, the
    login markers still answer — the same substrings that refuse the picture
    mean the same thing here (HHAeXchange+ hosts its form under an "auth"
    activity, Mobile Caregiver+ parks behind a "pin" one), so a sign-in
    screen is called one even before anyone has mapped that app's pages.

    The hierarchy is consulted only where an activity is known to hold more
    than one page — never as a general rule, because the atlas is the cheap
    answer and the one that survives a page nobody has read yet.
    """
    package = (focus or "").split("/")[0]
    if package in LAUNCHER_APPS:
        return "launcher"
    activity = activity_of(focus)
    # A keypad outranks the atlas, because the atlas names ACTIVITIES and
    # this app draws its passcode under whichever one it happens to be in.
    # Called "home" over a lock screen is worse than "unknown" ever was.
    if package in CARE_APPS and keypad_on_screen(hierarchy):
        return "login"
    named = ACTIVITY_SCREENS.get(package, {}).get(activity)
    if named == "startup" and hierarchy and any(
            mark in hierarchy for mark in PICKER_MARKS):
        return "agency"
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
    collapse_shade(serial)


def collapse_shade(serial: str | None = None) -> bool:
    """Get the shade off the screen, and say whether it went.

    Shared with the `clear_screen` macro, which is the hand-operated version
    of this: the automatic one runs on a cooldown and gives up quietly, and
    the day it could not do the job there was nothing a person could press.
    """
    try:
        # The clean way first. It is the documented one and it works on most
        # devices — and on THIS phone it returns success and does nothing at
        # all, as do KEYCODE_BACK, KEYCODE_HOME and `service call statusbar 2`.
        # All four were tried against the shade while it was actually up.
        _adb(["shell", "cmd", "statusbar", "collapse"], serial)
        time.sleep(0.6)
        if not _shade_has_focus(serial):
            return True
        # What does work is the gesture a person would use. Off the screen's
        # own size rather than a constant, and from low enough to be inside
        # the shade's empty area — a swipe that starts on a notification
        # drags the notification.
        w, h = screen_size(serial) or (0, 0)
        if not w or not h:
            return False
        log.info("the shade ignored the collapse — swiping it away")
        _adb(["shell", "input", "swipe", str(w // 2), str(int(h * 0.85)),
              str(w // 2), str(int(h * 0.08)), "250"], serial)
        time.sleep(0.6)
        return not _shade_has_focus(serial)
    except Exception as exc:  # noqa: BLE001
        # Broad on purpose. This runs inside the capture loop on every frame,
        # and a surprise from adb or the window read must cost a warning and
        # a shade left up — never the mirror.
        log.warning("could not collapse the shade (%s)", exc)
        return False


def _shade_has_focus(serial: str | None = None) -> bool:
    """Whether the shade still owns the screen, asked of the window manager.

    Checked rather than assumed, because the command that is supposed to
    close it reports nothing either way — it succeeded and the shade stayed
    up for twenty minutes, retried every twenty seconds, while the owner's
    notifications sat over the app and the portal reported the app in front.
    """
    focus, _, _ = window_state(serial)
    return _is_a_system_panel(focus) and "shade" in (focus or "").lower()


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
    # Measured on this phone 2026-08-29, when the app could finally be signed
    # into. At the inherited DEFAULT_DENSITY of 84 its rows are 46 px tall on
    # a 2340 px screen and its passcode keys are 34 px wide — a keypad no
    # thumb can use, on the one screen a person is most likely to have to
    # answer by hand. At 200 the same row is 111 px and the whole page still
    # sits above the tab bar at 2239.
    #
    # 200 rather than the 300 its sibling got, because what has to fit is
    # different: this app's default period is "Hoy", which is one to three
    # rows, and its visit detail is a short list. Nothing here needs the
    # room a six-day schedule needs.
    "com.tellus.evv.v2": 200,
    # 172, not 84, and not a guess: it is the value that was already in force
    # on this phone, set by hand from the console as an app-level override
    # once 84 turned out to be unreadable at 1080x2340. It is written here
    # because an override outranks every table below it (`_density_wanted`
    # step 2 beats step 3), so while it stood, no measured per-screen value
    # could ever reach the phone -- the schedule screen kept 172 and the
    # measurement had no effect. Promoting it into the code keeps the choice
    # it records for every unmeasured uma page and lets the override be
    # cleared, which is what uncovers PAGE_DENSITY below.
    "com.hhaexchange.uma": 172,
}

# PER SCREEN, MEASURED, ON THIS PHONE.
#
# The numbers above were tuned against the OLD handset — 720x1600 — and the
# comment beside them says what for: HHAeXchange+'s SIX-DAY schedule, which
# "only fits one capture that small". This phone is 1080x2340 at a physical
# density of 450, and the requirement changed with it: what has to fit now is
# TODAY, fanned out in one view, not the week.
#
# Those are very different bars, and a blanket value cannot serve both. So a
# screen that has been measured gets its own number and the rest keep the
# app's default until somebody measures them — applying an unmeasured value
# everywhere is the thing this table exists to stop.
#
# Measured live on the schedule screen, with today holding two visits and the
# first one expanded, reading how far down the page the NEXT day's header
# lands (past 2340 means today is cut off and the app scrolls):
#
#     density 450 (physical) .. today runs off the screen, one card visible
#     density 400 ............. still off the screen
#     density 340 ............. next day at 1725 — fits
#     density 300 ............. next day at 1538 — fits, ~800px to spare
#     density 200 ............. next day at  973 — fits, and small
#
# 340 was the ceiling and 300 was the choice, on the reading that legibility
# on the HANDSET was the tie-breaker. SUPERSEDED — see the note above the
# table: the bar became the whole week with today opened, which is a question
# about captures rather than about type size, and the portal draws its own
# type anyway. The sweep is kept because it is still the honest record of
# what fits at what size on this screen.
#
# Keyed on a fragment of the activity, matched case-insensitively, because
# that is what `_density_wanted` has in hand and it is stable across the
# app's own renames of everything else.
#
# NOT KEYED HERE: THE FUNCIONES (plan-of-care) PAGE, and the measurement is
# written down because the reason is the answer, not a gap.
#
# It was asked for on the same terms as the schedule — every checkbox in one
# view, no scrolling — and it was measured live on a fourteen-task plan:
#
#     14 task rows, pitch 314 px at density 300 (390 for the two whose
#     category label wraps to two lines), a header block of 279 px, and
#     Guardar/Cancelar PINNED to the bottom taking 337 px that no amount of
#     shrinking gives back.
#
#     content 4757 px against 1795 px of room. Fitting it needs ~132.
#
# 132 is not a smaller version of the same trade. Each row carries two
# controls a person presses by hand — "Se realizó" and "No realizado" — and
# at 132 they are about fifty pixels wide. The rule this whole table serves
# is that she has to be able to drive the phone by hand when something goes
# wrong; a density that costs her that is the trade the hand-back exists to
# refuse. And the page is not read by eye in the first place: `check_tasks`
# ticks the starred rows and scrolls to reach them, which is what a list this
# long wants.
#
# So the page keeps the app's value until somebody decides that trade
# deliberately. This comment is here so the next person measures nothing.
# 130, AND THE NUMBER IS ABOUT THE PORTAL, NOT THE PHONE.
#
# 300 was chosen against a different bar: today's cards legible in one view
# ON THE HANDSET. The bar changed — "expand the patient cards for today
# before we consider the page loaded" and "render all blocks for the week" —
# and those two together are a question about how much fits in ONE capture,
# because everything past the first capture costs a scroll, and this app
# FORGETS an opened card the moment it scrolls out of view. A page walked in
# three captures cannot hold today open; a page walked in one never has to.
#
# Measured live, whole week, today's two visits expanded first:
#
#     200 .. 2 captures   7 days, 9 visits, 6 expanded rows
#     160 .. 2 captures   7 days, 9 visits, 6 expanded rows
#     130 .. 1 CAPTURE    7 days, 9 visits, 6 expanded rows
#
# The portal draws its own type from the tree, so shrinking the phone does
# not shrink what she reads — it only decides how much arrives at once. 130
# is 29% of this panel's native size, which is where the old 720x1600
# handset ran for months (84 of 320, 26%), so it is not new territory for a
# thumb either.
PAGE_DENSITY = {
    "com.hhaexchange.uma": (
        ("homeactivity", 130),
    ),
}
# The signature moment gets its own value, found by its fingerprint: the
# legacy app is the only care app that goes LANDSCAPE, and it does so for
# exactly one thing — the signature canvas, where the sister and then the
# patient draw with a finger on the physical phone. 105 is the density
# the one proven signature landed at; the sweet spot is tuned here.
SIGNATURE_DENSITY = 105

# HANDING THE PHONE BACK AT ITS OWN SIZE.
#
# `wm density` is a DISPLAY setting, not an app setting — Android has no
# per-app density and this comment used to say as much in passing: "each
# application re-lays-out every app on the phone". So while a care app was in
# front the launcher, Settings, the dialer and every system dialog were laid
# out at 84 too, and nothing ever put them back. Leaving a care app left the
# phone shrunk.
#
# That is the opposite of what the density is for. The owner's rule: she has
# to be able to drive the phone by hand to intervene when something goes
# wrong, and buttons she cannot press are what make that impossible. A
# density that helps the automation read a schedule and costs her the ability
# to recover is a bad trade in exactly the moment it matters.
#
# So the density belongs to the care apps and is given back on the way out.
# `wm density reset` restores the panel's own physical value, which is the
# only correct "what it was" — a remembered number would be a guess about a
# phone that can change under us.
DENSITY_RESET = -2

# ...but not on the way THROUGH. Switching apps crosses the launcher, and a
# re-layout of every app on the phone is not free; done on each crossing it
# would thrash the device during the exact sequences that matter. So the
# hand-back waits for her to actually be out.
HANDBACK_DWELL = 4.0
_left_at = [0.0]
# THE WINDOW'S OWN SHAPE, not a number that happens to be true on one phone.
#
# This used to hunt for an x-coordinate of 800 or more, which reads as
# "sideways" only because a 720-wide screen has none of those in portrait.
# On a 1080-wide phone every portrait screen carries coordinates past 800, so
# the rule would have fired on all of them — and `landscape` turns the
# console's phone preview a quarter turn and puts the app page in its wide
# layout, so every screen would have rendered sideways, always.
#
# Found by auditing for a faster device rather than by watching it happen,
# which is the only reason this is not a story about a morning.
#
# Comparing the tree's own extent asks the actual question and cannot go out
# of date: a window wider than it is tall is sideways, at any resolution.
_BOUNDS_PAIR = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_density_now = [0]


def screen_extent(xml: str | None) -> tuple[int, int]:
    """The widest and tallest coordinates the tree mentions, or (0, 0).

    Read off the bounds rather than asked of the device: this runs on every
    frame, the hierarchy is already in hand, and an adb round trip per frame
    to learn something the document already states would be a poor trade.
    """
    max_x = max_y = 0
    for _x1, _y1, x2, y2 in _BOUNDS_PAIR.findall(xml or ""):
        max_x = max(max_x, int(x2))
        max_y = max(max_y, int(y2))
    return max_x, max_y


def _looks_landscape(hierarchy: str | None) -> bool:
    """Whether the window itself is sideways.

    The reflow has recognised the signature screen this way since its first
    real clock-out — the legacy app is the one that turns, and it turns for
    exactly that moment.
    """
    width, height = screen_extent(hierarchy)
    return bool(width and height and width > height)


def _density_wanted(focus: str, hierarchy: str | None = None) -> int | None:
    """What this screen should be laid out at, or None to leave it alone.

    Four sources, most specific first, and the order is the whole point:

      1. an override set for THIS page of this app,
      2. an override set for this app,
      3. the code's own table below — the values tuned against the real
         screens, which no slider can overwrite because overrides live in a
         different place and clearing one uncovers this again,
      4. a global override, for a screen this system has no table for: the
         case where somebody borrows the phone for something else entirely.

    The landscape signature bump sits inside (3) rather than above it. A
    person who has deliberately set a value for the signature page means it
    for the signature page.
    """
    pkg = (focus or "").split("/")[0]
    if not pkg:
        return None
    page = focus.split("/", 1)[1] if "/" in (focus or "") else ""
    try:
        from apt_log import prefs

        chosen = prefs.density_for(pkg, page)
        if chosen is not None:
            return chosen
    except Exception:  # noqa: BLE001
        # A preference file that cannot be read must not stop the phone
        # being laid out at the value that is known to work.
        chosen = None
    if pkg in CARE_APPS:
        _left_at[0] = 0.0
        if pkg == "com.hhaexchange.caregiver" and _looks_landscape(hierarchy):
            return SIGNATURE_DENSITY
        # A measured screen beats the app's blanket value — see PAGE_DENSITY.
        low = (page or "").casefold()
        for mark, value in PAGE_DENSITY.get(pkg, ()):
            if mark in low:
                return value
        return APP_DENSITY.get(pkg, DEFAULT_DENSITY)
    # Outside the care apps. A density somebody set deliberately for the whole
    # phone still wins — that is a person saying what they want — but with no
    # such override the phone goes back to its own size. See DENSITY_RESET.
    try:
        from apt_log import prefs

        chosen = prefs.global_density()
    except Exception:  # noqa: BLE001
        chosen = None
    if chosen is not None:
        _left_at[0] = 0.0
        return chosen
    now = time.time()
    if not _left_at[0]:
        _left_at[0] = now
        return None
    if now - _left_at[0] < HANDBACK_DWELL:
        return None
    return DENSITY_RESET


def _physical_density(serial: str | None = None) -> int:
    """What the panel is actually built at, or -1 if it will not say.

    -1 rather than a number, because every caller compares it against a
    wanted value and an invented default would compare EQUAL to something —
    silently skipping a change that was needed.
    """
    try:
        out = _adb(["shell", "wm", "density"], serial).stdout.decode(
            "utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return -1
    m = re.search(r"Physical density: (\d+)", out)
    return int(m.group(1)) if m else -1


def _watch_density(focus: str, serial: str | None = None,
                   hierarchy: str | None = None) -> None:
    pkg = (focus or "").split("/")[0]
    want = _density_wanted(focus, hierarchy)
    if want is None or _density_now[0] == want:
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
    if want == DENSITY_RESET:
        log.info("density handed back to the phone (left %s)", pkg or "?")
        try:
            _adb(["shell", "wm", "density", "reset"], serial)
            # THE SENTINEL, not the panel's number, and this is the whole bug
            # that was here: what `_density_now` records is what was last
            # ASKED FOR, because the only thing it is ever compared against
            # is the next `want`. Recording the physical density instead
            # meant the guard above compared 450 against DENSITY_RESET, found
            # them different, and reset again — every frame, for as long as
            # she was outside the care apps. Seen in the log as one
            # "density handed back" line every 1.5 seconds for a minute,
            # each one re-laying-out every app on the phone while she was
            # trying to use it.
            #
            # Reading the panel back was not wrong about the panel. It was
            # the wrong thing to store in a slot that means "the last thing
            # this asked for".
            _density_now[0] = DENSITY_RESET
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not hand the density back (%s)", exc)
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


def last_care_app() -> str:
    """The last care app the watchdog saw in front, or "" if none yet.

    Exposed because the `clear_screen` macro needs somewhere to return to
    once it has dismissed whatever was covering the screen, and this is
    already the record of that — kept by the watchdog for exactly the same
    purpose."""
    return _last_care_app[0]


# WRITTEN DOWN BECAUSE THE PORTAL IS A DIFFERENT PROCESS.
#
# The home screen's way back has to name an app, and the browser can only
# name one it has seen for itself this session — a page opened fresh onto a
# launcher had nothing to offer but the app picker. This is the same record
# the watchdog already keeps, put where the UI can read it.
#
# A NAME, AND NOTHING ELSE. The file holds one package name and a stamp: no
# patient, no visit, nothing about what was on the screen.
LAST_APP_NAME = "last-care-app.json"


def _publish_last_care_app(pkg: str) -> None:
    """Record which care app was last in front, for the UI to read."""
    try:
        from apt_log.ui.state import STATE_DIR

        target = STATE_DIR / LAST_APP_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({"app": pkg, "at": time.time()}),
                       encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        log.warning("cannot record the last care app (%s)", exc)


def _watch_containment(focus: str, serial: str | None = None) -> None:
    pkg = (focus or "").split("/")[0]
    if pkg in CARE_APPS:
        # Only on a change: this runs on every tick, and the file exists so
        # a page loading cold has somewhere to point, not to be rewritten
        # twice a second for as long as she is in an app.
        if _last_care_app[0] != pkg:
            _publish_last_care_app(pkg)
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
    if pkg in SETTINGS_APPS:
        # Opened on purpose, from the control centre. The watchdog exists to
        # stop the phone WANDERING; it must not undo somewhere it was sent.
        _out_since[0] = 0.0
        return
    if pkg in PERMISSION_APPS:
        # The care app asked for this. HHAeXchange+ requests location at
        # check-in and Android answers with its own dialog, from its own
        # package — which this watchdog would have read as wandering and
        # bounced after five seconds, taking the permission prompt with it
        # and stopping the very check-in she asked for. Recovered from the
        # flight recorder afterwards: grantpermissionsactivity, mid-flow,
        # between the schedule and the GPS screen.
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


# How long a wedged app is given to come back on its own. Android dismisses
# its own dialog if the app catches up, and a slow app mid-visit is worth a
# short wait before its screen is thrown away. Short, because the dialog does
# not time out: the one seen live sat there for minutes, blocking everything,
# with nobody in the room to answer it.
ANR_GRACE = 20.0
# And how long before the same app may be restarted again. A second wedge
# right after a restart is a deeper fault than a restart can fix; thrashing
# the app would only keep the screen unusable while looking busy.
ANR_COOLDOWN = 120.0
# Packages never force-stopped, whatever they do. Killing the system UI or the
# system process is a bigger hammer than any screen is worth, and an ANR there
# is not something this controller should be swinging at.
ANR_UNTOUCHABLE = ("com.android.systemui", "android", "system")
_anr_since: dict[str, float] = {}
_anr_last_fix: dict[str, float] = {}


def _watch_anr(pkg: str, serial: str | None = None) -> None:
    """Clear Android's not-responding dialog by restarting the wedged app.

    The dialog offers "Wait" and "Close app", and neither is reachable: it is
    a system window the tree does not publish, and the ANR takes the Appium
    session down with it, so there is nothing to drive and nothing to tap.
    What still answers is adb, and the recipe is the one the expired-session
    dialog already proved — force-stop, then relaunch through the launcher
    intent, never through the wedged driver. Verified against the live wedge:
    force-stop cleared the dialog, the relaunch came up on the sign-in screen,
    and auto-auth carried it from there.

    Restarting is the whole of the cure and it is destructive, so it waits out
    ANR_GRACE first and never repeats inside ANR_COOLDOWN.
    """
    now = time.time()
    first = _anr_since.get(pkg)
    if first is None:
        _anr_since[pkg] = now
        log.warning("%s is not responding", pkg)
        return
    if now - first < ANR_GRACE:
        return
    if now - _anr_last_fix.get(pkg, 0.0) < ANR_COOLDOWN:
        return
    if pkg in ANR_UNTOUCHABLE:
        log.error("%s is not responding and will not be restarted from here",
                  pkg)
        _anr_last_fix[pkg] = now
        return
    _anr_last_fix[pkg] = now
    _anr_since.pop(pkg, None)
    log.warning("restarting %s to clear the not-responding dialog", pkg)
    try:
        _adb(["shell", "am", "force-stop", pkg], serial, timeout=30.0)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not stop the wedged app (%s)", exc)
        return
    # Only the apps this portal exists to drive are put back up. Anything else
    # wedged is cleared and left closed; the containment watchdog is what
    # decides where the phone belongs.
    if pkg not in CARE_APPS:
        return
    try:
        _adb(["shell", "monkey", "-p", pkg,
              "-c", "android.intent.category.LAUNCHER", "1"], serial,
             timeout=30.0)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not relaunch %s (%s)", pkg, exc)


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
    focus, awake, anr = window_state(serial)
    _watch_focus(focus, awake, serial)
    if anr:
        # Nothing else is worth doing while the phone is wedged: the density
        # and the notification shade are cosmetics on a screen that answers
        # no taps, and the containment watchdog would see a care app in front
        # and be satisfied. The refusal is what keeps the page honest —
        # without it the portal publishes the wedged app's own dialog as live
        # buttons, which is exactly what it did the first time this happened.
        _watch_anr(anr, serial)
        return None, focus, APP_NOT_RESPONDING
    _anr_since.clear()
    if awake:
        _watch_shade(hierarchy, serial)
        _watch_containment(focus, serial)
        _watch_density(focus, serial, hierarchy)
    if not focus or not awake:
        # A dark display and a missing focus are the same fact for the page:
        # the phone is not showing anyone anything. Publishing the focused
        # app of a black screen is how "Live" ended up over darkness.
        return None, "", NO_FOCUS

    if looks_like_a_login_screen(focus) or keypad_on_screen(hierarchy):
        # The keypad half is the one the activity name misses. Refusing the
        # picture is right either way — nobody needs a photograph of a
        # passcode screen — and it is what lets the words through, which is
        # how auto sign-in knows there is something here to answer.
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

# THE RAW TREE, WRITTEN DOWN WHERE ANYONE CAN READ IT WITHOUT ASKING THE PHONE.
#
# Mapping a new screen needs the document underneath the reflow — a
# resource-id on a node nobody taps, an attribute the reflow drops. Getting it
# used to mean `adb shell uiautomator dump`, which spawns a fresh
# instrumentation and fights the resident session for the same service; doing
# that during setup work wedged Appium hard enough to need a restart. The
# other obvious route is worse: calling `read_hierarchy` from a second process
# opens a SECOND Appium session, and UiAutomator2 allows exactly one.
#
# The feed already holds the tree every time it reads one. Writing it down
# costs nothing anybody notices and means every other reader — a tool, a
# script, a person over ssh — gets it from a file instead of from the device.
HIERARCHY_NAME = "hierarchy.xml"

# What is typed is never what gets written. The published document withholds
# the contents of editable fields on purpose — that is where a passcode and a
# texted code live while she is typing them — and a raw tree on disk would
# quietly keep what the rest of the system refuses to carry. Blanked at the
# moment of writing rather than by whoever reads it, so there is one place to
# get it right instead of one per reader.
_TYPED_TEXT = re.compile(r'( text=")[^"]*(")')


def hush_typed(xml: str | None) -> str:
    """The tree with the contents of editable and password fields blanked.

    Matched on the NODE rather than on the value: an EditText's text IS its
    typed contents, and a password field's is that plus a reason to care. The
    node, its id and its bounds all survive — only what was typed does not.
    """
    out = []
    for node in re.split(r"(?=<[A-Za-z])", xml or ""):
        if 'password="true"' in node or "EditText" in node:
            node = _TYPED_TEXT.sub(r"\1\2", node)
        out.append(node)
    return "".join(out)


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


# The marks of a signature canvas in a raw hierarchy. Narrower than the
# finder's own hints on purpose — "draw" is left out because "drawer" would
# match every navigation drawer in every app; the ids and classes the care
# apps actually ship all carry one of these.
_CANVAS_MARK = re.compile(
    r'(?:resource-id|class)="[^"]*(?:signature|firma|sign_?pad|sketch)',
    re.IGNORECASE)


def _has_canvas(hierarchy: str | None) -> bool:
    """Whether the screen in front holds a signature canvas."""
    return bool(hierarchy and _CANVAS_MARK.search(hierarchy))


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


# The fields of a control that change while the PAGE does not: whether a box
# is ticked, whether a row is selected, whether a control has gone dead. A
# stitched page is a photograph of layout, and layout is all it should be
# trusted for.
LIVE_STATES = ("checked", "selected", "enabled", "focused")


# WHICH COLUMN A TICK WENT INTO.
#
# The refresh line below counts corrected FIELDS, and on a check-out sheet
# that reads as a tick count — but only if you already know the sheet has one
# tick per task. inMyTeam's has TWO: the task's own box on the left, "el
# paciente se niega" on the right. So "22" meant either twenty-two tasks
# ticked, or eleven tasks ticked twice over, and those are opposite readings
# of the same number. Reconstructing a real check-out afterwards, that
# ambiguity could not be resolved from the journal at all.
#
# Reported BY LEFT EDGE and not by meaning. Which column says what is the
# reflow's judgement (see REFUSAL_WORDS), it is read off captions, and the
# feed has no business guessing at it — an x coordinate is a fact.
TICKABLE = ("CheckBox", "RadioButton")

# The last tally logged, so a journal is a record of CHANGES rather than the
# same line every couple of seconds. A list because this is a module-level
# cell, the same shape `_FOLDS_OPENED_AT` uses in the runner.
_LAST_TICKS: list = [None]


def _tick_columns(els: list[dict]) -> None:
    """Log how many boxes are ticked in each column, when that changes.

    Quiet on every screen without tick boxes, and quiet while a sheet sits
    untouched — it speaks only when a tick goes in or comes out. That makes
    the drop this exists to catch (ticks going DOWN while she is putting
    them in) a line in the journal rather than an inference from one.
    """
    try:
        boxes = [e for e in els
                 if (e.get("cls") or "").split(".")[-1] in TICKABLE
                 and e.get("b")]
        if not boxes:
            _LAST_TICKS[0] = None
            return
        tally: dict[int, list[int]] = {}
        for e in boxes:
            col = tally.setdefault(e["b"][0], [0, 0])
            col[1] += 1
            if e.get("checked"):
                col[0] += 1
        shape = tuple(sorted((x, on) for x, (on, _total) in tally.items()))
        if shape == _LAST_TICKS[0]:
            return
        _LAST_TICKS[0] = shape
        log.info("ticks %s", ", ".join(
            f"{on}/{total} at x={x}"
            for x, (on, total) in sorted(tally.items())))
    except Exception as exc:  # noqa: BLE001 — a log line never breaks a tick
        log.debug("cannot tally ticks (%s)", exc)


def _merge_live_states(doc: dict, els: list[dict]) -> dict:
    """The stitched page, wearing the live viewport's control states.

    A WHOLE-PAGE DOCUMENT ONLY REBUILDS WHEN THE WALKER RUNS, AND THE WALKER
    STANDS DOWN WHILE SHE IS TAPPING.

    That is the right rule — nobody wants the page scrolling under their
    fingers — but on a checklist it locks: every tap resets the quiet
    window, so a page of twenty-one boxes never gets the pause it needs to
    refresh. Reported from a live check-out, and the consequence is worse
    than a stale screen. She ticks a box, the page still shows it empty, she
    taps it again — and the second tap UNTICKS it. Measured on the phone: the
    ticked count went down while she was trying to put ticks in.

    The viewport captures never stopped; they are read every couple of
    seconds throughout. So the layout comes from the walk, as before, and
    whether each control is ON comes from the live read.

    MATCHED ON EXACT DEVICE BOUNDS, and only where that match is UNIQUE. A
    stitched page holds several scroll steps, and two rows captured at
    different steps can land on the same pixels; an ambiguous match is
    skipped rather than guessed, because guessing here would show a tick
    against the wrong task.
    """
    if not els or not doc.get("elements"):
        return doc
    seen: dict[tuple, list[dict]] = {}
    for e in doc["elements"]:
        seen.setdefault(
            (e.get("cls", ""), e.get("rid", ""), tuple(e.get("b") or ())),
            []).append(e)
    live: dict[tuple, list[dict]] = {}
    for e in els:
        live.setdefault(
            (e.get("cls", ""), e.get("rid", ""), tuple(e.get("b") or ())),
            []).append(e)
    fresh = 0
    for key, mine in live.items():
        theirs = seen.get(key)
        if len(mine) != 1 or not theirs or len(theirs) != 1:
            continue
        for field in LIVE_STATES:
            if field in mine[0] and theirs[0].get(field) != mine[0][field]:
                theirs[0][field] = mine[0][field]
                fresh += 1
    if fresh:
        log.info("refreshed %d control state(s) from the live viewport", fresh)
    return doc


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
        # Where the app says it is, how it got there, and whether Back would
        # leave — see `nav_state`. Empty for an app that does not answer, and
        # every reader must take that as "no idea" rather than as a verdict.
        "nav": nav_state((focus or "").split("/")[0],
                         stamp=hierarchy_at),
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
        # bounds) and the legacy signature pages, which draw their content
        # turned a quarter turn inside a portrait activity. The tree shows
        # none of that turn (the sideways captions are laid out as
        # ordinary wide boxes), so for the apps that do it, a signature
        # canvas on a portrait screen IS the evidence. Same rule the
        # stroke replay turns by — the peek and the ink must agree.
        #
        # TWO QUESTIONS, NOT ONE, and answering them together turned the
        # portal black on the signature screen it was written for.
        #
        # "Is the screen wide?" and "is the photograph the wrong way up?"
        # are the same question only for the LEGACY app, which draws its
        # signature page rotated inside a portrait activity: the screencap
        # comes back portrait with sideways content in it, and the peek has
        # to turn it. HHAeXchange+ genuinely rotates the device — the
        # screencap arrives landscape and already upright — and turning it
        # anyway rotated a wide photograph by a quarter turn and scaled it
        # by a factor computed for a portrait one, leaving an all but empty
        # stage. Reported, exactly, as "once I get to the signature part
        # everything goes black".
        "landscape": _looks_landscape(hierarchy),
        # Whether the PHOTOGRAPH needs turning, which is `sign.sideways` and
        # nothing else — the answer the ink already turns by, and the one
        # the file says the peek and the ink must never disagree on.
        "turn": bool(_has_canvas(hierarchy)
                     and sign_mod.sideways(
                         hierarchy or "",
                         package=(hierarchy_package(hierarchy)
                                  or (hierarchy_focus or "")
                                  .split("/")[0]))),
        # A signature canvas is in front. The walker must not swipe (a
        # swipe on a canvas is ink), and the portal can dress the page
        # for signing instead of reading.
        "canvas": _has_canvas(hierarchy),
        # Something of the SYSTEM'S is over the care app — the notification
        # shade, the volume dialog, the keyboard. The elements below are then
        # not the app's page, and nothing on the portal will move the app,
        # because the app is not what has the focus. The watchdog tries to
        # clear this on its own; the flag exists so that when it cannot (this
        # phone ignores every collapse command but a swipe, and the shade sat
        # over inMyTeam until it was cleared by hand), the page can offer the
        # one button that does — rather than leaving her tapping a screen
        # that will not answer.
        "covered": screen_is_covered(focus),
        # Whether this document is the WHOLE page (a stitched walk) or the
        # viewport. Full documents leave nothing to wonder about.
        "full": bool(stitched),
        "elements": (stitched["elements"] if stitched
                     else elements(
                         hierarchy, label=True,
                         package=(hierarchy_package(hierarchy)
                                  or (focus or "").split("/")[0]),
                         size=tuple(frame["size"] or (0, 0)),
                     ) if hierarchy else []),
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


def _publish_hierarchy(where: Path, hierarchy: str | None) -> None:
    """Write the tree down for other readers. Never fatal: this is a
    convenience for whoever is working on the machine, and a frame must not
    be lost because a disk was full."""
    if not hierarchy:
        return
    try:
        target = where / HIERARCHY_NAME
        tmp = target.with_suffix(".tmp")
        tmp.write_text(hush_typed(hierarchy), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log.debug("cannot publish the hierarchy: %s", exc)


def write_frame(path: Path, serial: str | None = None,
                hierarchy: str | None = None,
                hierarchy_at: float = 0.0,
                hierarchy_focus: str = "") -> str:
    """Capture once and publish the mirror frame. Returns a one-line status.

    The hierarchy is handed in rather than fetched, so the caller decides how
    often to pay for it. See `run`.
    """
    png, focus, reason = capture(serial, hierarchy)
    screen = screen_for(focus, hierarchy)
    _publish_hierarchy(path.parent, hierarchy)

    speak = text_is_disclosable(reason)
    els = elements(hierarchy, label=speak,
                   package=(hierarchy_package(hierarchy)
                            or (focus or "").split("/")[0]),
                   size=tuple(screen_size(serial) or (0, 0))) if hierarchy else []

    # A wedged app publishes NOTHING to aim at. Its last tree is still there
    # and still parses — the legacy app was wedged on its own inactivity
    # dialog, and that dialog's "DE ACUERDO" went on being published as a live
    # button nobody could press. Boxes that answer no tap are worse than no
    # boxes: she taps, nothing happens, and the fault looks like the portal's.
    if reason == APP_NOT_RESPONDING:
        els = []

    # What the sheet in front says about itself, before any of the caching
    # below. Read off the LIVE viewport and not the stitched page, because a
    # stitch is a photograph of layout and the question here is what is
    # ticked right now — and because a page nobody has walked still needs
    # answering for.
    _tick_columns(els)

    # The whole page, when the runner has walked it: while the stitched
    # document's first capture still matches what is in front, the portal
    # renders and aims at everything, not just the viewport. The moment the
    # screen moves on, the match fails and the viewport is the truth again.
    # A signature screen is never served from the cache: the canvas overlay
    # lives inside the same activity as the page beneath it and shares most
    # of that page's tree, so the near-match dressed the live canvas in the
    # task list's stitched walk — no clear button, no save, no box to sign
    # (seen live, first field test). A canvas moment is one viewport; there
    # is nothing a stitch could add and everything it could hide.
    stitched = (_fresh_stitch(path.parent, frame_id(els), els=els,
                              app=(focus or "").split("/")[0],
                              sts=statics(hierarchy) if hierarchy else [])
                if not reason and not _has_canvas(hierarchy) else None)
    # Layout from the walk, control states from the live read — see
    # `_merge_live_states`. Without this a tick she puts in is invisible
    # until the walker gets a quiet moment, and her second tap takes it
    # back out.
    if stitched is not None:
        stitched = _merge_live_states(stitched, els)
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
                    focus, awake, _ = window_state(self._serial)
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
    from apt_log import sms as sms_mod
    from apt_log import versions as versions_mod

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
            try:
                # Free on all but one tick in a few hundred — the timer is
                # inside `check`. This is the only thing on either machine
                # that would notice Play replacing an app underneath us, so
                # it rides the loop that is always running.
                versions_mod.check(serial)
            except Exception as exc:  # noqa: BLE001
                log.debug("version check failed: %s", exc)
            try:
                # Free on all but one tick in a few, for the same reason and
                # by the same means — the timer lives inside the call. This is
                # what makes a forwarded code true of sign-ins the portal
                # never started: somebody logging in from their own phone
                # makes inMyTeam text THIS one, and nothing else here would
                # ever notice that happened.
                sms_mod.forward_any_new(serial)
            except Exception as exc:  # noqa: BLE001
                log.debug("code forward failed: %s", exc)
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


def _label(raw: str) -> str:
    """What this node SAYS — its text, or the description standing in for it.

    A view that draws its own content has no text for the tree to carry, and
    Android's answer is content-desc: the sentence a screen reader would say.
    Mobile Caregiver+ builds its whole visits list that way — every row is a
    bare View whose only words are "La visita está programada para <patient>
    … y su estado es <status>" — so a portal reading text alone showed three
    identical empty strips where the phone showed three named visits.

    A description is text by every rule that matters: it is disclosed exactly
    where text is (see text_is_disclosable), it is excluded from editable
    fields exactly as text is, and it never rides in `has_text` as anything
    but a boolean. Text wins when a node carries both, which is also what
    keeps a node that repeats itself from being read twice.
    """
    return _attr(raw, "text") or _attr(raw, "content-desc")


# A CONTROL THAT DOES NOT SAY IT IS ONE.
#
# HHAeXchange+'s "Funciones" page — the care-plan tasks, ticked off at the
# end of every visit — draws each task's Se realizó / No realizado pair as
# plain Views with `clickable="false"`. They ARE controls: the app hand-writes
# their description as "no seleccionado  Se realizó %1$s  Toca dos veces para
# alternar", which is TalkBack's own sentence for a node that HAS a click
# action, plus the selection state in front of it.
#
# Read by the attribute alone, none of the fourteen pairs reached the portal
# and the whole page was a wall of text with nothing to press. Found while
# the caregiver was standing in a patient's home with the page open.
#
# So the description is taken at its word: a node that tells a screen reader
# to double-tap it is a node that can be tapped. The tap lands on the pixel
# either way — `input tap` goes to a coordinate, not to a node — so this
# publishes what the phone already responds to.
_READER_TAPPABLE = ("toca dos veces", "toque dos veces", "pulsa dos veces",
                    "pulse dos veces", "double tap", "double-tap")
# The state the same sentence opens with. Order matters: "no seleccionado"
# contains "seleccionado".
_READER_OFF = ("no seleccionado", "no marcado", "not selected", "unselected",
               "not checked")
_READER_ON = ("seleccionado", "marcado", "selected", "checked")


def reader_control(desc: str) -> tuple[str, bool] | None:
    """A screen reader's sentence, read back as (label, checked).

    None when the sentence is not one of these. The label is what is left
    once the state in front and the instruction behind are taken off, with
    the format placeholder the app forgot to fill dropped as well.
    """
    text = (desc or "").strip()
    low = text.lower()
    if not any(m in low for m in _READER_TAPPABLE):
        return None
    checked = False
    for word in _READER_OFF:
        if low.startswith(word):
            text, low = text[len(word):].strip(), low[len(word):].strip()
            break
    else:
        for word in _READER_ON:
            if low.startswith(word):
                checked = True
                text, low = text[len(word):].strip(), low[len(word):].strip()
                break
    for marker in _READER_TAPPABLE:
        cut = low.find(marker)
        if cut >= 0:
            text, low = text[:cut].strip(), low[:cut].strip()
            break
    # "%1$s" — an unfilled format argument, sitting in the middle of the
    # sentence where the app meant to put the task's own name.
    text = re.sub(r"%\d*\$?[sd]", " ", text)
    # The comma that joined the label to the instruction behind it goes with
    # the instruction: "Performed, double tap to toggle" names a control
    # called "Performed".
    return " ".join(text.split()).strip(" ,.;:"), checked


# THE PORTAL'S OWN WORDS FOR THE PAIR.
#
# The app's arrive only inside a screen reader's sentence, and the words of a
# page with a patient on it do not cross the wire — `text_is_disclosable` says
# text travels on credential screens and nowhere else, which is right and is
# not being changed here. Named this way instead, through the same `name_key`
# path the agency filter uses: these two captions are the app's fixed chrome,
# identical on every task of every visit, so the portal can say them itself
# and say them in her language.
#
# Not-done is tested first: "no realizado" contains "realizado".
_TASK_NOT_DONE_WORDS = ("no realizado", "no se realiz", "no realiz",
                        "not performed", "not done", "not completed")
_TASK_DONE_WORDS = ("se realiz", "realizado", "performed", "done", "completed")
TASK_DONE_KEY = "papp.task.done"
TASK_NOT_DONE_KEY = "papp.task.not_done"

# The largest share of a screen a spoken control may take up. There is a lot
# of room between the two things it separates: HHAeXchange+'s care-plan ticks
# are 31x29 on a 720x1600 screen — 0.078% — and the signature canvas on the
# other side of it is 46% of the same screen. This sits 64x above the one and
# 9x below the other, which is what makes it a boundary rather than a tuning.
READER_CONTROL_MAX_SHARE = 0.05


def task_key(label: str) -> str:
    """Which half of a care-plan task's pair this is, or ""."""
    low = (label or "").strip().lower()
    if any(low.startswith(w) for w in _TASK_NOT_DONE_WORDS):
        return TASK_NOT_DONE_KEY
    if any(low.startswith(w) for w in _TASK_DONE_WORDS):
        return TASK_DONE_KEY
    return ""


def elements(xml: str, label: bool = False, package: str = "",
             size: tuple[int, int] | None = None) -> list[dict]:
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
    from apt_log import controls as controls_mod

    # Defensively: a dict here unpacks to its keys, which is how a whole page
    # scan died on `"height" * 0.12`. controls coerces, and this refuses to
    # unpack anything that is not a pair.
    pair = size if isinstance(size, (tuple, list)) and len(size) == 2 else (0, 0)
    w, h = pair
    naming = controls_mod.naming(xml or "", package, w, h)
    found = []
    for raw in _NODE.findall(xml or ""):
        # Clickable, or telling a screen reader to double-tap it — which is
        # the same claim, made in the only place this app makes it. See
        # `reader_control`.
        spoken = reader_control(_attr(raw, "content-desc"))
        if _attr(raw, "clickable") != "true" and spoken is None:
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
            # A control the app has greyed out. HHAeXchange+ ships the visit
            # screen with `visit_details_clock_out_button_disabled` — clock
            # out exists from the moment of check-in and does nothing until
            # the visit is over — and published as an ordinary control it
            # read as a live call to action: press, nothing happens, and the
            # portal wears the fault. Absent means enabled, which is how
            # Android writes it.
            "enabled": _attr(raw, "enabled") != "false",
            "has_text": bool(_label(raw)),
        }
        # A TICK BOX IS SMALL. HHAeXchange+'s signature canvas carries a
        # double-tap description too — half the screen of it — and reading
        # that as a control turned the thing she signs on into an on/off
        # switch. A screen reader's "double tap" says a node ACCEPTS a tap,
        # which a drawing surface certainly does; it does not say the node is
        # a checkbox. Size is what tells them apart, and nothing on these
        # screens comes near the boundary from either side: the care-plan
        # ticks are 0.078% of the screen and the canvas is 46% of it.
        if spoken is not None and w and h:
            share = ((x2 - x1) * (y2 - y1)) / float(w * h)
            if share > READER_CONTROL_MAX_SHARE:
                # NOT a toggle — and still published. Dropping it outright was
                # the first version of this guard and it took the signature
                # canvas off the page altogether: the surface is not clickable
                # in the tree, so the spoken sentence is the ONLY thing that
                # says it is there, and without it the portal drew a signature
                # screen with nothing to sign on. Losing the role is the whole
                # correction; losing the element is a bigger fault than the
                # one being fixed.
                spoken = None
        if spoken is not None:
            # The state is IN the sentence — "no seleccionado Se realizó" —
            # because the app never sets the attribute. Taken from there, so
            # a task already ticked comes through ticked.
            entry["checked"] = entry["checked"] or spoken[1]
            entry["has_text"] = bool(spoken[0])
            # And a role, so the reflow can draw a pair of these as the
            # choice they are rather than as two mystery boxes. Not the
            # class — the class is what the tap verifies against, and it has
            # to stay exactly what the phone published.
            entry["role"] = "toggle"
        # The portal's own name for a control the app ships nameless, decided
        # here rather than in the reflow because this is where the raw text
        # is and where the disclosure below has to be decided with it.
        name_key = naming.key(short, entry["rid"], entry["b"], _label(raw))
        if not name_key and spoken is not None:
            name_key = task_key(spoken[0])
        if name_key:
            entry["name_key"] = name_key
        if label and (short not in EDITABLE or _is_showing_hint(raw)
                      or naming.discloses(short, entry["rid"], entry["b"])):
            # The spoken sentence never travels whole: what it means is the
            # label at the middle of it, and the rest is state and stage
            # directions. "no seleccionado Se realizó %1$s Toca dos veces
            # para alternar" is a control named "Se realizó".
            entry["txt"] = (_clean(spoken[0]) if spoken is not None
                            else _clean(_label(raw)))
        found.append(entry)
    return found


def _is_showing_hint(raw: str) -> bool:
    """Whether a field's text is its PLACEHOLDER rather than its contents.

    Android tracks this itself and Appium publishes it as `showing-hint`, so
    the one question that matters here — "is this string something a person
    typed?" — has a real answer instead of a guess. A field showing its hint
    holds nothing, so its text is a label the app drew, and labelling the box
    with it can leak nothing: the moment anything is typed the flag goes false
    and the text stops being disclosed again.

    This is the honest version of a rule that was previously all-or-nothing.
    Editable text was excluded outright because it "is what has been typed",
    which is true right up until the field is empty and Android is drawing
    "Enter your code" into it — and then a blank box is all the page can show.

    Absent means false. The adb dump does not carry the attribute at all
    (only Appium's page source does), so a hierarchy read through the adb
    fallback labels nothing here, which is the safe direction.
    """
    return _attr(raw, "showing-hint") == "true"


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
        # Published as a control instead — one node must not arrive twice,
        # once as something to press and once as a caption beside it.
        if reader_control(_attr(raw, "content-desc")) is not None:
            continue
        if _attr(raw, "package") == "com.android.systemui":
            continue                      # see elements(): never care content
        cls = (_attr(raw, "class") or raw[1:].split()[0].rstrip("/>")).rsplit(".", 1)[-1]
        if cls in EDITABLE and not _is_showing_hint(raw):
            continue
        text = _clean(_label(raw))
        rid = _attr(raw, "resource-id").split("/")[-1]
        if cls in DECORATIVE and not _attr(raw, "text"):
            # A picture's description is ALT TEXT — what a screen reader says
            # in place of an image somebody can see. It is not a statement the
            # screen is making, and treating it as one put the HHAeXchange+
            # logo's own alt text where the page title goes: "Logotipo de H H
            # AeXchange +", spelled out letter by letter for a reader that is
            # not us. The image still rides along by resource-id below, which
            # is how the EVV check marks are recognised.
            #
            # Only the description is dropped, and only on a NON-CLICKABLE
            # image: a description on something tappable names the control
            # (Back, Search, the tab you are on), and that is a real label.
            text = ""
        if not text and not (cls in DECORATIVE and rid):
            continue
        m = _BOUNDS.search(_attr(raw, "bounds"))
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        entry = {"cls": cls, "b": [x1, y1, x2, y2], "txt": text}
        # THE ID IS IDENTITY, AND IT WAS BEING THROWN AWAY WHENEVER A NODE
        # HAD WORDS. It used to ride only on textless nodes, because the only
        # reader was the mark table — but the stitch identifies an item by
        # (class, id, text, left, right), so every row saying the same thing
        # in the same column had the SAME identity, and its dedupe drops a
        # match within 150 virtual pixels seen in another capture.
        #
        # A week of one patient's visits is exactly that: six rows reading
        # "NIEVES C MASTRAPA" at the same x. Two of them vanished from the
        # portal — septiembre 3 and septiembre 5 kept their times and lost
        # their names — and the schedule quietly showed visits belonging to
        # nobody. The app had drawn all six; it names each one
        # `schedule_screen_accordion_title_<uuid>`, which is unique per
        # visit and was the one thing that could have told them apart.
        #
        # Carried for text nodes too, now. The two readers of this field ask
        # about textless nodes first (see `_mark_for`, `_mark_key`), so
        # nothing that looks at a mark can see a difference; only the
        # stitch's notion of "the same thing twice" gets sharper.
        if rid or not text:
            entry["rid"] = rid
        out.append(entry)
        if len(out) >= STATICS_CEILING:
            break
    return _keep_the_bar(out)


# A hard stop while reading, so a pathological tree cannot be walked forever.
# Well above MAX_STATICS: what is read past the cap is not published, it is
# only looked at long enough to find the bar pinned at the bottom.
STATICS_CEILING = 600


# How much of the tail is held back from the cap for the app's own bottom
# bar. A tab is not one node: Mobile Caregiver+ spends three on each — the
# cell's own description, the icon, and the caption — and hangs an unread
# count beside one, so its three-tab bar alone runs to ten. Sized for five
# tabs at that rate, because guessing short costs the navigation and the
# content gives up sixteen rows it has over a hundred of.
BAR_TAIL = 16


def _keep_the_bar(found: list[dict]) -> list[dict]:
    """Trim to MAX_STATICS without dropping the app's own bottom bar.

    Truncation used to cut the tail, and a pinned bar is the LAST thing in
    the tree — so the 370-message inbox, which spends its whole budget on
    messages, published no captions at all and the portal lost the app's
    navigation. She could read the inbox and had no way back to the visits
    list except the phone's own Back button.

    The tail is kept by POSITION IN THE TREE rather than by where it sits on
    the screen. A long list is exactly the case this exists for, and a long
    list is also where a node's bounds stop being a reliable guide: the rows
    below the fold carry coordinates that run off the bottom of the screen,
    so "whatever is lowest" points into the list, not at the bar pinned over
    it.
    """
    if len(found) <= MAX_STATICS:
        return found
    return found[:MAX_STATICS - BAR_TAIL] + found[-BAR_TAIL:]


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


def published_frame(path: Path | None = None) -> dict:
    """The whole published frame, freshness-checked. See published_elements."""
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
    return frame


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
    frame = published_frame(path)
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

    `claimed_frame` is carried for the log on NAMED aims rather than enforced —
    it was enforced for everything once, and a ticking clock re-hashing the
    frame refused almost every legitimate tap (see read_stable_hierarchy).
    For an ANONYMOUS aim — no resource-id — it is enforced again: bounds and
    class are not identity there. The legacy home's menu rows are nameless
    twins, and while the app finished loading, its layout shifted one slot —
    "Horario para hoy" was tapped and "Visita no programada" opened, an
    unscheduled-visit screen nobody asked for (seen live, first field test).
    The words she read live in the frame's structure hash; if the structure
    moved on, an anonymous tap refuses and she aims at a fresh frame.
    """

    frame = published_frame(frame_path)
    current = frame.get("elements") or []
    bounds = list(element.get("b") or [])
    match = next(
        (e for e in current
         if e["b"] == bounds
         and e["rid"] == element.get("rid", "")
         and e["cls"] == element.get("cls", "")),
        None,
    )
    if (match is not None
            and not (element.get("rid") or "")
            and claimed_frame and frame.get("id")
            and claimed_frame != frame.get("id")):
        raise StaleAim("the screen changed under that button — look again")
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
# Two shapes go into a field, and no third.
#
# A verification code, which is what this channel was built for: letters and
# digits, nothing that needs quoting at all.
#
# And a NAME, typed into a search box — which is a real need ("some input
# fields like search for patient that we should try to integrate") and
# cannot be spelled without spaces and accents. So the allowed set widens
# to letters in any language, digits, spaces, and the three marks names
# carry: apostrophe, hyphen, full stop. It does NOT widen to anything a
# shell reads — no $ ` \ " ; | & ( ) < > * ? newline — because the value
# crosses `adb shell`, where the device's own sh parses the line. The
# quoting below is the guard that matters; this is the belt.
#
# Still capped short. A field takes a code or a name, never a message.
# How long the field is given to take focus before anything is typed into it.
# Named rather than a bare 0.6 so the suite can stand it down: five typed
# values in one test is three seconds of the deploy gate spent watching a
# mock.
TYPE_SETTLE = 0.6

_TYPABLE_VALUE = re.compile(r"^[^\W_](?:[\w .'\-]{0,30}[^\W_])?$", re.UNICODE)


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
    time.sleep(TYPE_SETTLE)
    # QUOTED FOR THE DEVICE'S OWN SHELL. `adb shell` does not take an argv —
    # it joins what it is given into a command line and hands it to sh on the
    # phone. An unquoted "Rojas Batista" arrives there as two words and only
    # the first one is typed; anything sh reads as syntax would be read as
    # syntax. `_TYPABLE_VALUE` already refuses every such character, and this
    # is the guard that does not depend on that list staying right.
    result = _adb(["shell", "input", "text", shlex.quote(value)], serial,
                  timeout=20.0)
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
