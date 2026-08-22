"""Named sequences she can start with one button.

This is the automation that survived. What got abandoned was a machine deciding
*when* to record a visit; what is left is a machine doing the tedious parts *when
she asks*, on a screen she is watching. Nothing here fires on a timer and nothing
here decides anything.

**Macros navigate and fill. They do not commit.**

Signing in is twelve taps and a password through a mirrored screen, with no
judgement anywhere in it — exactly what a button is for. Clocking in is one tap
that produces a record and, if it is wrong, a phone call from the agency to her.
That one stays hers, on the real screen, with her looking at it. The line is not
about difficulty; it is about which mistakes are recoverable.

**The page can only ask for a name.** Macros live here, in code, and the UI is
handed a list. It cannot post steps. A macro that accepted a sequence from a
browser would be arbitrary remote scripting with a friendlier label, and the
argument that the portal cannot do anything she did not ask for would collapse
into "the client is well-behaved".

They run in the feed process because that is where the resident Appium session
lives. The UI writes a request file and reads a status file; a second session in
the web process is not available at any price, as fourteen seconds a tap
demonstrated.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

STATE_DIR = Path("/var/lib/aptlog")
REQUEST_PATH = STATE_DIR / "macro-request.json"
STATUS_PATH = STATE_DIR / "macro-status.json"

# WHEN EACH AUTO-AUTH LAST FIRED, ON DISK.
#
# The cooldowns below are the only thing standing between a login screen and a
# sign-in attempt every few seconds, and for inMyTeam every attempt SENDS A
# TEXT MESSAGE to a real person. They used to live in the Runner's memory,
# which meant a restart forgot them — and the feed restarts on every deploy.
#
# What that cost, reported from the field as "over 100 notifications" with a
# lock screen full of identical "inMyTeam needs the code": each deploy dropped
# a fresh Runner onto a phone parked at a login screen, the fifteen-minute
# guard read as never-fired, the walk pressed Sign in, and another code went
# out. Ten deploys in an afternoon is ten texts nobody asked for and ten steps
# toward the app's rate limit.
#
# Wall clock rather than monotonic, because monotonic means nothing across a
# restart — which is the whole point of writing it down.
AUTH_SEEN_PATH = STATE_DIR / "auto-auth.json"

# A request older than this is ignored rather than run. The feed can be down when
# a button is pressed, and a sign-in that fires when the process comes back
# minutes later is a surprise on a phone nobody is holding.
REQUEST_MAX_AGE = 60.0

POLL_EVERY = 1.0

# How often the scheduler looks for something to fire. Far coarser than the
# loop's own tick because a fire has a five-minute window either side of it,
# and re-parsing the schedule every second to answer "not yet" sixty times a
# minute is work nobody asked for.
FIRE_EVERY = 5.0

# The lead-window walk NAVIGATES the phone, so it asks even less often than
# the fire check does. It only ever runs once per visit anyway; this is the
# floor on how often it bothers to look.
PREPARE_EVERY = 20.0

# ------------------------------------------------------------ auto sign-in
# The app expires its session mid-use: the alert's only button lands on the
# sign-in screen, which the portal (correctly) will not photograph and she
# cannot fill. The owner's rule: nobody types credentials, anywhere, ever —
# so landing on that screen IS the request to sign in, and the runner treats
# it as one without waiting for a button.
#
# Only the app whose sign-in sequence is proven. The cooldown is what stands
# between this and a retry storm on the day the credentials go bad: one quiet
# failure per interval, visible in the macro status, not a loop.
AUTO_AUTH_APP = "com.hhaexchange.caregiver"
AUTO_AUTH_MACRO = "hhax_legacy_login"
AUTO_AUTH_COOLDOWN = 90.0
# And how long before a macro that TEXTS SOMEBODY may try again. Fifteen
# minutes rather than ninety seconds, because the cost of a retry here is not
# a wasted second, it is another message on a real person's phone. The common
# repeat is free anyway — the walk no-ops once the app is already asking for
# a code — so this only bounds the case where the app falls back to the
# number screen with nobody answering.
SMS_AUTH_COOLDOWN = 15 * 60.0
SENDS_A_MESSAGE = ("inmyteam_login",)
# Screens flash through login on their own during app startup; only a login
# screen the feed has seen *recently and still* is a real landing.
AUTO_AUTH_FRESH = 6.0
SCREEN_PATH = STATE_DIR / "screen.json"

# Where a full-page reading lands: every text line of a scrolled-through
# page, in reading order, for the portal's reading sheet.
SCAN_PATH = STATE_DIR / "scan.json"

# The stitched whole-page documents (see apt_log.stitch) — a small cache,
# one file per scanned page keyed by its top frame id, and the request/
# result pair for taps below the fold — which must run here, where the
# resident session lives, because they re-verify against a fresh dump
# after replaying the scroll. A cache rather than a single file because a
# single file meant every app switch threw away the other app's scan:
# come back and the page scanned itself all over again, which the owner
# rightly asked about ("the already scanned content was gone — why??").
STITCH_DIR = STATE_DIR / "stitched"
STITCH_CACHE_MAX = 12
STITCH_CACHE_TTL = 600.0
DEEPTAP_REQUEST_PATH = STATE_DIR / "deeptap-request.json"
DEEPTAP_RESULT_PATH = STATE_DIR / "deeptap-result.json"
DEEPTAP_MAX_AGE = 15.0
STITCH_MAX_STEPS = 10
# The floor between walk attempts ON THE SAME PAGE. A page whose text
# ticks (a clock, a countdown) re-hashes every frame, and without a floor
# the phone would visibly scroll itself over and over for the same page.
# A page TRANSITION pays no floor at all: the owner's spec is that every
# page is scanned whole the moment it appears — waiting out a cooldown
# on a fresh page read as "the front end is missing things".
STITCH_COOLDOWN = 45.0
# How long a walk's swipes settle. Shorter than a macro's waits on
# purpose: the walk presses nothing, so the worst a rushed read costs is
# one re-walk, while every extra second is the owner staring at a page
# that says it is still being read.
STITCH_SETTLE = 0.35

# Set while a scan is walking the page, so the hierarchy watcher yields the
# one Appium session to it instead of interleaving its own dumps. That
# interleaving was the whole cause of two live complaints at once: the scan
# crawled (every watcher dump stole a turn from the scan), and the live
# view lurched to whatever half-scrolled position the scan was mid-swipe on
# ("it shows where the scan stopped"). While this is set the watcher holds
# the last complete frame instead — stable under the scanning animation —
# and the scan owns the session, so it finishes faster.
SCAN_ACTIVE = threading.Event()
# No scanning over her fingers: a tap in the last few seconds means
# someone is driving the phone, and the walk waits its turn.
STITCH_TAP_QUIET = 4.0

# The schedule's visit cards fold their details behind an accordion: a
# collapsed card shows name and time with a sideways chevron; tapping the
# row unfolds the EVV records and the details button beneath it. A scan
# that never opens them publishes a page that is honestly incomplete —
# the owner asked for the opposite ("open accordion elements so we get an
# accurate front end"). So the walk unfolds them, and leaves them
# unfolded: the phone then matches the portal, and the next scan has
# nothing left to open.
#
# ONLY TODAY'S CARDS. The owner's scoping, verbatim: "We don't care
# about the past or future schedules for patients we just care about
# today!" — and today is also where every actionable thing lives.
# Expanding the whole week grew the page past one screen; today's
# section (however many patients are in it) keeps the page short enough
# to publish whole in one or two captures, and the taps down to a couple.
#
# The chevron is the state signal, verified live on the device: the same
# icon-font glyph is drawn ROTATED when open — collapsed it is taller
# than wide (6x11 at the row's trailing edge), expanded it is wider than
# tall (11x6). Only rows whose chevron still stands sideways are tapped.
#
# Tapping mid-scan is only trusted where the page is known to fold rather
# than navigate: the schedule list, recognised by its run of date
# headers. Everywhere else a trailing chevron means "opens another page",
# and a scan must never walk away from the screen it was asked to read.
EXPAND_GLYPH = "\uf054"
EXPAND_APPS = {"com.hhaexchange.uma"}
EXPAND_MAX_TAPS = 16
EXPAND_MIN_DATE_HEADERS = 3
_DATE_HEADER = re.compile(r"\d{1,2},\s*20\d{2}")
# How the schedule marks today's header, in the app's two locales.
_TODAY_MARKS = ("hoy", "today")

# Cache-warming: right after a sign-in, the app's other tabs have never
# been opened, so their virtualized lists have never been materialised and
# their scans cannot exist yet. The warm sweep opens each sibling tab once
# and scans it, so her FIRST visit to any tab is instant instead of a
# fresh scroll. It runs only just after a sign-in (she is not yet
# interacting), only on a tabbed screen, and yields the phone the instant
# a real action arrives. WARM_ENABLED turns the whole behaviour off in one
# place if the autonomous movement is ever unwanted.
#
# OFF, deliberately: warming navigates the phone through tabs on its own,
# and on HHAeXchange+ the "Menú" tab opens a sub-page whose return proved
# fragile enough to strand the owner on a settings screen. The per-page
# scan cache already makes a tab instant the moment she opens it herself;
# pre-opening tabs for her was not worth driving the phone unattended. The
# machinery is kept, flag-gated, for a future where the return is proven.
WARM_ENABLED = False
# A tab label is a short text in the bottom band, narrower than half the
# screen. The band starts at 0.82 of the height, where the four apps keep
# their tab bars (HHAeXchange+'s captions sit at 0.89).
WARM_TAB_BAND = 0.82
WARM_TAB_MAX_WIDTH = 0.5

# How far inside a webview's bottom edge its footer button sits, as a share
# of that view's own height. Measured at 80px on the 1406-tall migration
# webview; kept as a fraction so it means the same on a different panel.
MIGRATION_FOOT = 80 / 1406
WARM_SETTLE = 0.9
# The sign-in's activate_app can leave the app mid-reload when the sweep
# starts, its tab bar not yet drawn; wait this long for it before
# concluding the screen simply is not tabbed.
WARM_TABBAR_WAIT = 6.0

# Sign in only while someone is actually watching. Unwatched, the app's own
# inactivity timer signs the session back out and the two loop all night —
# observed as sign-in / agency picker / "due to inactivity" / sign-out on
# repeat. The UI publishes its socket count and refreshes the file's mtime on
# its slow tick, so a crashed UI reads as nobody watching, not somebody.
VIEWERS_PATH = STATE_DIR / "viewers.json"
VIEWERS_FRESH = 40.0

# A BROWSER THAT BACKGROUNDED IS NOT A PERSON WHO LEFT.
#
# The count above is live sockets, and a phone's browser drops its socket for
# all the ordinary reasons: the screen locks, she switches to the camera, iOS
# suspends the tab. Read strictly, that is "nobody is watching" — so on the
# night this was found, the app's fifteen-minute timer signed the session out
# mid-visit, the phone was left standing on the sign-in page, and the one
# thing that exists to fix that refused to run. Reported as: "instead of an
# auth trigger we were stranded on the signin page so I had to leave and
# click on the app for her to trigger an auth."
#
# So a recent watcher counts. This does not reopen the case the gate was
# built for — an unwatched phone looping sign-in against its own inactivity
# timer all night — because at three in the morning nobody has been watching
# for hours. It is deliberately longer than the app's own timeout: the whole
# point is to still be inside the window when that timer fires.
ATTENDED_WINDOW = 20 * 60.0


def someone_is_watching(path: Path | None = None) -> bool:
    """Whether anybody is on the portal, or was recently enough to count."""
    target = path or VIEWERS_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        # Valid JSON is not the same as the shape this expects, and every
        # refusal in this file goes the same way: nobody is watching.
        return False
    try:
        if time.time() - target.stat().st_mtime <= VIEWERS_FRESH:
            if int(payload.get("n", 0)) > 0:
                return True
    except (OSError, TypeError, ValueError):
        pass
    # Nobody on a socket this second. The stamp says when somebody last was,
    # and it carries its own age, so a crashed UI cannot make it look newer
    # than it is the way an mtime could.
    try:
        seen = float(payload.get("seen") or 0)
    except (TypeError, ValueError):
        return False
    return bool(seen) and 0 <= time.time() - seen <= ATTENDED_WINDOW


@dataclass
class Macro:
    name: str
    label_key: str          # i18n key; the UI never sees English from here
    run: object             # callable(driver, report) -> None
    # Whether this macro is handed the request's `arg` as a third parameter.
    # Opt-in and declared here rather than sniffed from the callable, so that
    # "this macro takes something from the page" is a fact you can read off
    # the registry instead of one you have to go and check.
    #
    # An arg is NOT a widening of what the page may ask for: the name is
    # still from the allow-list, and the only macro that takes one uses it to
    # pick between rows the app itself is displaying. It cannot name a
    # control that is not on screen.
    takes_arg: bool = False


@dataclass
class Status:
    id: str = ""
    name: str = ""
    state: str = "idle"      # idle | running | done | failed
    step: str = ""           # i18n key for what it is doing
    error: str = ""
    at: str = field(default_factory=lambda: datetime.now().isoformat())


# Android's own "Save password?" prompt, by resource-id. It appears over the app
# the moment credentials are submitted, and because it belongs to the system
# rather than to the app, nothing in screens/session.py was looking for it -- the
# sign-in macro reached the agency step and then waited twenty seconds at a
# screen that was not going to change.
#
# Dismissed with "no", deliberately. Storing the agency password in the phone's
# autofill is a credential decision, and taking it silently in the affirmative on
# someone else's work phone is not this code's call to make.
#
# This does not contradict the rule about unread dialogs. That rule is about
# dialogs whose *message* is unrecognised; this one is identified exactly, by id,
# and both of its answers are known.
AUTOFILL_DECLINE = "android:id/autofill_save_no"


def dismiss_autofill(driver) -> bool:
    """Decline the system offer to save the password. True if one was showing."""
    found = driver.find_elements("id", AUTOFILL_DECLINE)
    if not found:
        return False
    found[0].click()
    log.info("declined the system offer to save the password")
    return True


def wake_display() -> None:
    """Turn the screen on before driving the app.

    Appium happily drives an app with the display off — the sketch showed a
    homepage while the physical phone and its photograph were black, which
    reads as a haunting, not a portal. The display is in a resident's room
    and lighting it briefly is harmless; a screen that is on also behaves
    like the screen the app was built for.
    """
    from apt_log.device import send_ui_action

    try:
        send_ui_action("wake")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not wake the display (%s)", exc)


def wait_for(predicate, timeout: float = 20.0, poll: float = 0.5) -> bool:
    """Poll until `predicate()` is true, or give up.

    Every step here needs one. The first run of the sign-in macro submitted the
    credentials, then asked whether the agency screen was showing *before the
    app had drawn it*, concluded it was not, skipped the selection and failed on
    the home check -- with the phone sitting on the agency screen the whole time.

    The manual walkthrough that "proved" these steps had print statements
    between them, which was enough delay to hide it. A macro is faster than a
    person reading output, and that is precisely what makes it need waits.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            pass          # a screen mid-transition reads as an error, not a no
        time.sleep(poll)
    return False


# --------------------------------------------------------------------- macros
def _hhax_legacy_login(driver, report) -> None:
    """Bring the app to the front and sign in only if it asks to be signed in.

    This used to walk the whole ceremony — gates, credentials, agency
    selection, a home-screen check — on every press, over an app that was
    usually already signed in. The owner's correction was precise: the button
    handles auth and nothing else. Sign in only when auth inputs are actually
    on screen (authenticate_if_needed keys on the password field, which is
    exactly that test), and never choose an agency — which agency to enter is
    her tap, on the screen, like every other decision in this portal.
    """
    from apt_log.screens import login as login_mod
    from apt_log.screens import session as session_mod
    from apt_log.secrets import FileSecretProvider

    report("macro.step.launching")
    wake_display()
    driver.activate_app("com.hhaexchange.caregiver")

    # A cold launch shows a splash before anything is readable.
    wait_for(lambda: bool(driver.current_activity), timeout=15.0)

    report("macro.step.clearing")
    from selenium.common.exceptions import StaleElementReferenceException

    # Two attempts, because the expiry dialog dismisses ITSELF: the
    # inactivity countdown runs out and the app swaps the screen between
    # this macro reading the dialog and tapping it — seen live as a
    # StaleElementReferenceException at exactly this step. A dialog that
    # left on its own needed no dismissing; read again and move on.
    for _attempt in range(2):
        try:
            message = session_mod.modal_message(driver)
            if message:
                if not session_mod.is_expired(driver):
                    # Refusing to dismiss what nothing has read is the same
                    # rule the session module holds, and a macro is not an
                    # excuse to break it.
                    raise RuntimeError("an unrecognised dialog is on screen")
                session_mod.dismiss(driver)
            break
        except StaleElementReferenceException:
            time.sleep(1.0)

    report("macro.step.signing_in")
    authed = login_mod.authenticate_if_needed(driver, FileSecretProvider())

    if authed:
        # A system dialog on top means the app cannot advance; the credential
        # submit is the moment Android offers to save the password.
        wait_for(lambda: dismiss_autofill(driver), timeout=6.0, poll=0.5)

        # Done means the credentials took: the password field is gone.
        # Wherever the app lands next — agency picker, home — is hers.
        report("macro.step.checking")
        screen = login_mod.LoginScreen(driver)
        if not wait_for(lambda: not screen.is_displayed(), timeout=15.0):
            raise RuntimeError("still on the sign-in screen after signing in")

    _skip_migration_pitch(driver, report)


def _skip_migration_pitch(driver, report) -> None:
    """Tap 'Recordarme más tarde' on the migrate-to-the-new-app interstitial.

    After sign-in the legacy app now parks on MigrationWebViewActivity — a
    webview pitching HHAeXchange+ — between the login and the agency
    picker. Back RETREATS to the login screen (walked live), so the only
    way past is the choice the page itself recommends for anyone
    mid-shift: «Recordarme más tarde».

    The webview's mood decides the aim. Some visits it exposes real
    buttons once the page has been scrolled (seen live: «Recordarme más
    tarde» as a Button after one swipe); other visits its content never
    reaches the accessibility tree at all. So: scroll and look for the
    button by name first, and only when the tree stays empty fall back to
    the bottom-anchored coordinate tap from the first discovery (the
    later-link sits just above the webview's bottom edge, well clear of
    the blue setup button ~120px higher). Either way the landing is
    verified: still on the migration screen afterwards is a loud failure,
    never a shrug. Skipping the pitch changes nothing about her account;
    the app repeats the offer freely.
    """
    def on_migration():
        return "migration" in (driver.current_activity or "").lower()

    if not wait_for(on_migration, timeout=6.0, poll=1.0):
        return
    report("macro.step.checking")

    later = ('//*[contains(@text,"Recordarme") '
             'or contains(@text,"Remind me later")]')
    cx, y_top, y_bot = _swipe_geometry(driver)
    target = None
    # To the bottom, deterministically: the pitch is a one-screen page, so
    # the later swipes are rubber-band no-ops — and each stop is another
    # chance for the webview to surface its buttons.
    for attempt in range(3):
        found = driver.find_elements("xpath", later)
        if found:
            target = found[0]
            break
        _swipe(driver, cx, y_bot, y_top, 300)
        time.sleep(1.0)
    if target is not None:
        rect = target.rect
        driver.tap([(rect["x"] + rect["width"] // 2,
                     rect["y"] + rect["height"] // 2)])
    else:
        views = driver.find_elements(
            "xpath", '//*[contains(@resource-id,"migration_webview")]')
        if not views:
            raise RuntimeError("the migration screen offers nothing to aim at")
        rect = views[0].rect
        # A share of the webview's OWN height rather than eighty pixels: the
        # button sits just inside its bottom edge, and "just inside" is a
        # proportion of the box, not a count of pixels on one phone. 80 of
        # the 1406-tall webview this was measured on is what MIGRATION_FOOT
        # reproduces there.
        driver.tap([(rect["x"] + rect["width"] // 2,
                     int(rect["y"] + rect["height"]
                         * (1 - MIGRATION_FOOT)))])
    if not wait_for(lambda: not on_migration(), timeout=10.0):
        raise RuntimeError("the migration pitch did not close")


def _hhax_uma_login(driver, report) -> None:
    """HHAeXchange+ — sign in through its web form, only if it asks.

    Walked end-to-end in the second discovery session (the first one's notes
    were wrong in every particular that mattered):

    - The auth screen is Jetpack Compose behind a ProgressBar: it exposes
      NOTHING for its first seconds, then id-less Views. The sign-in control
      is found by its inner text ("Iniciar sesión…" / "Sign in…" — both
      locales matched, since text is all Compose gives us here).
    - The form is a Chrome Custom Tab (secure.hhaexchange.com). Its fields
      carry literal web ids `email`/`password` — which `find_elements("id")`
      can NEVER match, because UiAutomator2 prefixes bare ids with the app
      package. XPath on the attribute is the only locator that works.
    - The email field arrives PREFILLED with the remembered account; typing
      without clearing appends to it.
    - The submit is an id-less, text-less wide button below the password
      field; the narrow eye-toggle inside the field is the trap the width
      test exists for.
    - Success: Chrome hands back to the app, OnboardingActivity flashes
      past, HomeActivity settles. No OTP, no second factor.

    Same contract as the legacy macro: auth only — if the app opens onto
    anything but its sign-in screen or that pending Chrome form, the
    session is alive and this is done.
    """
    from apt_log.secrets import (APP_PASSWORD, APP_USERNAME, UMA_PASSWORD,
                                 UMA_USERNAME, FileSecretProvider,
                                 SecretNotFound)

    report("macro.step.launching")
    wake_display()
    driver.activate_app("com.hhaexchange.uma")
    wait_for(lambda: bool(driver.current_activity), timeout=15.0)

    # Three honest starting points, not two. The first run of this macro
    # treated "not on the auth activity" as "signed in" — and reported done
    # while the phone sat on the sign-in form, because the form lives in a
    # Chrome Custom Tab and Chrome's activity is not the app's. The app
    # reopens onto that pending tab, so the form in front IS the ask.
    def on_auth_screen():
        return "auth" in (driver.current_activity or "").lower()

    def on_web_form():
        try:
            return (driver.current_package or "") == "com.android.chrome"
        except Exception:  # noqa: BLE001
            return False

    # The app's own expiry dialog ("Desconectado — se ha cerrado la sesión")
    # parks over the home activity; its only exit is the button back to the
    # login screen. Recognised by its wording, like every dialog this
    # project is allowed to touch. Text on any descendant: Compose hangs
    # the words on plain Views as often as on TextViews.
    expiry_exit = ('//*[@clickable="true"]'
                   '[contains(@text,"Regresar al inicio")'
                   ' or .//*[contains(@text,"Regresar al inicio")'
                   ' or contains(@text,"Return to login")'
                   ' or contains(@text,"Back to login")]]')

    def on_expiry_dialog():
        return bool(driver.find_elements("xpath", expiry_exit))

    # A freshly woken Compose UI exposes an almost-empty accessibility tree
    # for several seconds. "I see nothing" must never read as "signed in" —
    # it read exactly that way once, reporting done over a still-open
    # expiry dialog. A screen with real content has several tappable
    # nodes; an unready tree has none.
    def tree_has_substance():
        try:
            return (driver.page_source or "").count('clickable="true"') >= 3
        except Exception:  # noqa: BLE001
            return False

    # Both HHAeXchange apps front the same account, so the legacy
    # credentials answer here too. UMA_* keys exist only for the day the
    # vendor splits the accounts — set them and they win; unset, nothing
    # more needs configuring than the legacy app already had. Either way
    # the read raises before any tap if nothing is configured.
    secrets = FileSecretProvider()
    try:
        email = secrets.get(UMA_USERNAME)
        password = secrets.get(UMA_PASSWORD)
    except SecretNotFound:
        email = secrets.get(APP_USERNAME)
        password = secrets.get(APP_PASSWORD)

    report("macro.step.signing_in")

    # The app's Compose auth screen and the Chrome form race each other: a
    # cold start draws its ProgressBar for longer than any fixed guess, or
    # the app skips straight to the pending Chrome tab — the first version
    # chose a branch in its first four seconds and failed overnight on
    # exactly that ("the sign-in control never appeared", 06:20). One loop
    # now watches for whichever appears. The Compose control is clicked at
    # most once; and if Chrome is in front but the form stays unreachable,
    # something is covering it (the page-info sheet, seen live) — one BACK
    # closes a sheet, and if there was none it closes the tab and hands
    # back to the app's own screen, which this same loop handles.
    sign_in = ('//android.view.View[@clickable="true"]'
               '[.//android.widget.TextView['
               'contains(@text,"Iniciar sesi") or contains(@text,"Sign in")]]')

    # XPath on the literal resource-id: the "id" strategy silently prefixes
    # the app package and never matches web ids.
    def field(web_id):
        found = driver.find_elements("xpath",
                                     f'//*[@resource-id="{web_id}"]')
        return found[0] if found else None

    restarted_expiry = False
    clicked_expiry = False
    clicked_sign_in = False
    sign_in_sightings = 0
    settled_sightings = 0
    nudged_back = False
    covered_since = 0.0
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if field("email") is not None:
            break
        # The app passes THROUGH its auth activity on every cold resume
        # while it checks the stored session — an alive session settles on
        # home a few seconds later. Settling on a SUBSTANTIVE screen that
        # is not the auth screen, before this macro has touched anything,
        # means signed in: walk away quietly instead of signing in over a
        # live session (watched happen: a tile press opened the web form
        # the owner never needed). Two consecutive sightings, same as the
        # click — a transition frame must not read as a verdict — and the
        # tree must actually show content: an unready Compose tree shows
        # nothing at all, and nothing is not a home screen.
        if (not clicked_expiry and not clicked_sign_in
                and not on_web_form() and not on_auth_screen()
                and not on_expiry_dialog() and tree_has_substance()):
            settled_sightings += 1
            if settled_sightings >= 2:
                report("macro.step.checking")
                return
        else:
            settled_sightings = 0
        if not clicked_expiry:
            exits = driver.find_elements("xpath", expiry_exit)
            if exits:
                if not restarted_expiry:
                    # A FRESH STACK is the recovery. Clicking the dialog's
                    # exit (log_out_button) walks into the stale activity
                    # stack, which swallows the sign-in redirect — watched
                    # live twice: Chrome submits, the app resurfaces the
                    # same dialog, not signed in. But the restart must not
                    # go through the driver: terminate_app against exactly
                    # this wedged app hung inside its HTTP call for an
                    # hour, freezing the whole runner. Plain adb touches
                    # neither — force-stop kills the process outright, the
                    # launcher intent brings it back, and a cold start
                    # lands directly on the pending Chrome sign-in form
                    # (verified live), which the ordinary walk fills.
                    subprocess.run(
                        ["adb", "shell", "am", "force-stop",
                         "com.hhaexchange.uma"],
                        capture_output=True, timeout=15, check=True)
                    time.sleep(1.0)
                    subprocess.run(
                        ["adb", "shell", "monkey", "-p",
                         "com.hhaexchange.uma",
                         "-c", "android.intent.category.LAUNCHER", "1"],
                        capture_output=True, timeout=15, check=True)
                    restarted_expiry = True
                    time.sleep(1.0)
                    continue
                # Still the dialog on a stack that is now fresh: clicking
                # is then the only exit left, and a fresh stack is the one
                # place it has nothing stale to strand the redirect in.
                exits[0].click()
                clicked_expiry = True
                time.sleep(1.0)
                continue
        if not clicked_sign_in:
            controls = driver.find_elements("xpath", sign_in)
            # Two consecutive sightings before believing it: the session
            # check can flash the sign-in control for a frame on its way
            # to home, and clicking that frame starts an auth nobody asked
            # for. A control that is really being offered is still there a
            # second later.
            sign_in_sightings = sign_in_sightings + 1 if controls else 0
            if controls and sign_in_sightings >= 2:
                controls[0].click()
                clicked_sign_in = True
                time.sleep(1.0)
                continue
        if on_web_form():
            covered_since = covered_since or time.monotonic()
            if not nudged_back and time.monotonic() - covered_since > 8.0:
                driver.press_keycode(4)      # BACK, once
                nudged_back = True
        time.sleep(1.0)
    else:
        raise RuntimeError(
            "neither the sign-in control nor the web form appeared")
    box = field("email")
    box.clear()                # arrives prefilled with the remembered account
    box.send_keys(email)
    field("password").send_keys(password)

    # The submit control carries no id and no text in the accessibility tree.
    # Discovery's recording: it is the one wide button below the password
    # field — the eye toggle beside the field is narrow. Width is the
    # discriminator, measured against the FORM, not the screen: at tablet
    # densities the web page draws its form as a narrow centred column
    # (213 px on a 720 px screen at density 72), and a screen-relative
    # test refused the real submit. The submit spans the field it sits
    # under; the eye toggle never comes close.
    pw = field("password").rect
    candidates = [
        b for b in driver.find_elements("class name", "android.widget.Button")
        if b.rect["width"] >= pw["width"] * 0.8
        and b.rect["y"] > pw["y"]
    ]
    if not candidates:
        raise RuntimeError("no submit-shaped button below the password field")
    candidates[0].click()

    wait_for(lambda: dismiss_autofill(driver), timeout=6.0, poll=0.5)

    report("macro.step.checking")
    # Done means the app is back AND actually signed in — a substantive
    # screen that is neither the auth activity nor the expiry dialog.
    # "The package returned" once passed as success while the app sat on
    # its auth screen with the sign-in silently failed behind it.
    def signed_in():
        try:
            return (driver.current_package == "com.hhaexchange.uma"
                    and not on_auth_screen()
                    and not on_expiry_dialog()
                    and tree_has_substance())
        except Exception:  # noqa: BLE001
            return False

    if not wait_for(signed_in, timeout=30.0):
        raise RuntimeError("the app came back but did not land signed in")


# The form's controls are found by the TAIL of their resource-id, never by a
# bare id. UiAutomator2 prefixes a bare id with the app's package, and this
# app's package is not the namespace its ids live in: the application is
# com.tellus.evv.v2 while its classes and resources are com.tellus.evv, so
# every bare-id lookup came back empty ("the sign-in form is not where it was
# walked") on a form that was plainly there. The same trap the HHAeXchange+
# web form documented, sprung a second way. Anchoring on ":id/" keeps the
# match to a whole id segment, so login_button cannot catch its neighbours.
_MC_USERNAME_BOX = ('//android.widget.EditText'
                    '[contains(@resource-id, ":id/login_username_input")]')
_MC_PASSWORD_BOX = ('//android.widget.EditText'
                    '[contains(@resource-id, ":id/login_password_input")]')
_MC_SIGN_IN = ('//android.widget.Button'
               '[contains(@resource-id, ":id/login_button")]')


# A WALL THAT IS NOT A LOCK.
#
# Found on the live phone while looking at this macro: Mobile Caregiver+ was
# sitting on its dashboard behind a dialog with one button — "Actualizar
# ahora", "Existe una nueva versión disponible en la Play Store". Not a
# passcode, not an expired session, nothing this macro was written to answer,
# and nothing it could see: it looked for a keypad, found none, looked for
# the expiry wording, found none, waited six seconds for a sign-in form that
# was never coming, and reported DONE over an app that could not be used.
#
# A macro reporting success over a wall is worse than one that fails, because
# the next thing anybody does is trust it.
#
# The button is NOT pressed HERE. It leads to the Play Store, and installing a
# new version of an app this project reads by resource-id is a change to every
# assumption in this file — a decision for a person on a morning when
# somebody can check afterwards, not for a macro at 6am.
#
# There is now a way for that person to say yes: `_update_app`, offered on the
# app page only while this wall is up, and confirmed before it runs. That is
# the same reasoning, not a reversal of it — the objection was never to the
# act, it was to the act happening unattended.
UPDATE_WALL_MARKERS = {
    "com.tellus.evv.v2": (
        "actualizar ahora", "nueva versión disponible",
        "nueva version disponible", "update now", "new version is available",
    ),
}


def update_wall_on_screen(doc: dict | None) -> bool:
    """Whether the app in front is blocked behind a forced-update dialog."""
    markers = UPDATE_WALL_MARKERS.get((doc or {}).get("app") or "")
    if not markers:
        return False
    words = " ".join(
        (n.get("txt") or "")
        for n in ((doc or {}).get("statics") or [])
        + ((doc or {}).get("elements") or [])).lower()
    return any(m in words for m in markers)


# THE KEYPAD, READ ONCE.
#
# Mobile Caregiver+'s passcode screen is ten Buttons whose whole text is one
# digit, a backspace ImageButton beside the zero, and "Log in as a new user"
# under them. It was typed by asking the driver for each digit in turn — four
# hierarchy dumps for a four-digit code, on a phone where one dump costs
# seconds — and the first of those dumps is the dangerous one: a keypad caught
# mid-draw answers with SOME of its buttons. The flight recorder has one, two
# buttons into a ten-button keypad, and against that the old path raised "the
# keypad is not where discovery saw it" about a keypad that was simply not
# finished yet.
#
# So: one read, and only once every digit the code needs is on it.
MC_KEYPAD_DIGITS = 10
MC_KEYPAD_READY = 25.0


def _mc_keypad(driver) -> dict:
    """digit -> where to tap, read from a single hierarchy."""
    from apt_log import feed as feed_mod

    keys: dict[str, list[int]] = {}
    for el in feed_mod.elements(driver.page_source or "", label=True):
        if not el.get("cls", "").endswith("Button"):
            continue
        text = (el.get("txt") or "").strip()
        if len(text) == 1 and text.isdigit() and text not in keys:
            keys[text] = el["b"]
    return keys


def _mc_backspace(driver) -> list[int] | None:
    """The keypad's own delete key: the ImageButton sitting among the digits.

    Whatever is half-typed when this macro arrives is not this macro's, and
    appending to it produces a WRONG passcode — which on an app that locks
    after a few of those is a worse outcome than not trying. Cleared first,
    always; backspace on an empty field does nothing.
    """
    from apt_log import feed as feed_mod

    digits = [b for b in _mc_keypad(driver).values()]
    if not digits:
        return None
    top = min(b[1] for b in digits)
    bottom = max(b[3] for b in digits)
    for el in feed_mod.elements(driver.page_source or "", label=True):
        if el.get("cls", "").endswith("ImageButton") and top <= el["b"][1] \
                and el["b"][3] <= bottom + (bottom - top):
            return el["b"]
    return None


def _mc_tap(bounds: list[int]) -> None:
    _tap_xy((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)


def _mobile_caregiver_pin(driver, report) -> None:
    """Mobile Caregiver+ — answer whichever of its two locks is up.

    The app has TWO, and they are not interchangeable. Discovery found the
    first: a PIN keypad ("Introduce un código de acceso", four dots, digits
    0-9), whose buttons carry their digits as text, so the passcode is typed
    by tapping them and the screen advances by itself on the last one.

    Walking it live found the second. The passcode only unlocks the app on
    this phone; when the SERVER session lapses the dashboard opens, says
    "Sesión caducada", and drops to its own username-and-password form —
    which no number of keypad taps can answer. Landing there with only a PIN
    stored is how an auth macro spins without ever signing anything in.

    So the screen in front decides. Either lock may be up, and clearing the
    first can reveal the second, which is why the passcode path falls through
    to the password path rather than declaring victory.
    """
    from apt_log.secrets import (MC_PASSWORD, MC_PIN, MC_USERNAME,
                                 FileSecretProvider, SecretNotFound)

    report("macro.step.launching")
    wake_display()
    driver.activate_app("com.tellus.evv.v2")
    wait_for(lambda: bool(driver.current_activity), timeout=15.0)

    def activity() -> str:
        return (driver.current_activity or "").lower()

    def on_pin_screen() -> bool:
        return "pin" in activity()

    def on_login_screen() -> bool:
        return "login" in activity()

    # ------------------------------------------------------------- the PIN
    #
    # A COLD START IS NOT A MISSING KEYPAD. Four seconds was the whole budget
    # for this app to draw its passcode screen, and this app opens on a splash
    # — so a slow morning skipped the passcode path entirely and fell through
    # to the password form, which is not what was on screen. The wait is now
    # long enough to lose an argument with a cold start, and it waits for the
    # KEYPAD rather than for the activity's name.
    keys: dict = {}
    if wait_for(on_pin_screen, timeout=MC_KEYPAD_READY, poll=0.6):
        # Read only once the keypad is actually up, and raised rather
        # than returned: an unlocked app must not even open the file.
        pin = FileSecretProvider().get(MC_PIN)
        report("macro.step.signing_in")

        def keypad_ready() -> bool:
            nonlocal keys
            keys = _mc_keypad(driver)
            # Every digit this passcode needs, not merely SOME buttons. A
            # keypad two buttons into being drawn satisfied "find the 4" and
            # failed on the "7" a moment later, and said the keypad had moved.
            return all(d in keys for d in pin)

        if not wait_for(keypad_ready, timeout=MC_KEYPAD_READY, poll=0.6):
            raise RuntimeError(
                f"the passcode keypad drew {len(keys)} of "
                f"{MC_KEYPAD_DIGITS} keys and stopped")

        # Whatever is half-typed is not this macro's, and appending to it
        # makes a WRONG passcode — which on an app that locks after a few is
        # worse than not trying at all.
        back = _mc_backspace(driver)
        if back:
            for _ in range(len(pin) + 2):
                _mc_tap(back)
                time.sleep(0.12)

        for digit in pin:
            _mc_tap(keys[digit])
            time.sleep(0.18)
        report("macro.step.checking")
        if not wait_for(lambda: not on_pin_screen(), timeout=20.0):
            # Deliberately NOT retried. A second attempt at a passcode that
            # did not take is a second wrong passcode if the first was wrong,
            # and this app locks. A person is told instead.
            raise RuntimeError(
                "still on the passcode screen after entering the code")

    # ------------------------------------------------- the expired session
    # "Sesión caducada" has one button and it only acknowledges; the form is
    # behind it. Dismissing it is not a commitment — it is the only way to
    # reach the screen that can be answered. But only THAT dialog: pressing
    # whatever sits at android:id/button1 would press the positive button of
    # any alert the app happened to be showing, and this system does not
    # confirm things it cannot read. The wording is checked first, from the
    # same per-app list the expiry watcher uses.
    words = (driver.page_source or "").lower()
    if any(m in words for m in EXPIRY_MARKERS.get("com.tellus.evv.v2", ())):
        for alert in driver.find_elements(
                "xpath", '//android.widget.Button[@resource-id='
                         '"android:id/button1"]'):
            alert.click()
            break
    wait_for(on_login_screen, timeout=6.0, poll=0.5)
    if not on_login_screen():
        # Not the sign-in form, and not necessarily nothing wrong. See
        # UPDATE_WALL_MARKERS: this app can be sitting on its dashboard
        # behind a dialog it will not move past, and "done" is a lie there.
        if any(m in words for m in
               UPDATE_WALL_MARKERS.get("com.tellus.evv.v2", ())):
            report("macro.step.update_required")
            raise RuntimeError(
                "Mobile Caregiver+ is blocked behind its own update prompt")
        report("macro.step.checking")
        return

    try:
        password = FileSecretProvider().get(MC_PASSWORD)
    except SecretNotFound:
        # Nothing typed, nothing half-filled: an unanswerable screen is left
        # exactly as it was found, for a person to answer.
        raise RuntimeError(
            "Mobile Caregiver+ wants its password and none is stored")

    report("macro.step.signing_in")
    fields = driver.find_elements("xpath", _MC_PASSWORD_BOX)
    if not fields:
        raise RuntimeError("the sign-in form is not where it was walked")
    # The username is remembered between sessions; it is only typed when the
    # app has forgotten it, and only if one is stored.
    for box in driver.find_elements("xpath", _MC_USERNAME_BOX):
        if not (box.text or "").strip():
            try:
                box.clear()
                box.send_keys(FileSecretProvider().get(MC_USERNAME))
            except SecretNotFound:
                pass
        break
    fields[0].clear()
    fields[0].send_keys(password)
    for button in driver.find_elements("xpath", _MC_SIGN_IN):
        button.click()
        break

    report("macro.step.checking")
    if not wait_for(lambda: not on_login_screen(), timeout=30.0):
        raise RuntimeError("still on the sign-in form after signing in")


def _read_page(driver, report) -> None:
    """Read a scrolling page end to end, and change nothing.

    The accessibility tree only carries the viewport, so the reflow can
    never show more than one screenful — the owner's ask: the front end
    should have everything. This walks the page with mid-screen swipes
    (they move the page and can press nothing), collecting every line of
    text in reading order, then returns the page to its top. The result is
    a READING, not a tappable surface: below-the-fold controls cannot be
    tapped at coordinates that are no longer on screen, and pretending
    otherwise would break the one promise the tap machinery makes.
    """
    from apt_log import feed as feed_mod

    report("macro.step.reading")
    size = driver.get_window_size()
    cx = size["width"] // 2
    top_y = int(size["height"] * 0.33)
    bottom_y = int(size["height"] * 0.66)

    from apt_log.ui.screenview import BULLETS

    def snapshot() -> list[str]:
        rows = feed_mod.statics(driver.page_source or "")
        rows.sort(key=lambda s: (s["b"][1], s["b"][0]))
        return [s["txt"] for s in rows
                if s.get("txt") and s["txt"].strip() not in BULLETS]

    # To the top first: a reading starts at the beginning.
    prev: list[str] = []
    for _ in range(8):
        texts = snapshot()
        if texts == prev:
            break
        prev = texts
        driver.swipe(cx, top_y, cx, bottom_y, 260)
        time.sleep(0.8)

    lines: list[str] = []
    prev = []
    steps_down = 0
    for _ in range(14):
        texts = snapshot()
        # Overlap with the PREVIOUS capture is the scroll's doing and is
        # dropped; a value repeating further down the page (the same
        # patient on two visit rows) is content and is kept.
        carried = set(prev)
        lines.extend(t for t in texts if t not in carried)
        if texts == prev:
            break
        prev = texts
        driver.swipe(cx, bottom_y, cx, top_y, 260)
        steps_down += 1
        time.sleep(0.8)

    for _ in range(steps_down):        # leave the page as found: at its top
        driver.swipe(cx, top_y, cx, bottom_y, 260)
        time.sleep(0.4)

    report("macro.step.checking")
    doc = {"at": datetime.now().isoformat(),
           "app": (driver.current_package or ""), "lines": lines}
    try:
        SCAN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SCAN_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, SCAN_PATH)
    except OSError as exc:
        raise RuntimeError("could not save the page reading") from exc


def _screen_size(driver) -> tuple[int, int]:
    """The device's own size, for the control naming. Degrades to (0, 0),
    which names nothing rather than naming something wrongly."""
    try:
        size = driver.get_window_size()
        return int(size["width"]), int(size["height"])
    except Exception:  # noqa: BLE001
        return (0, 0)


def _swipe_geometry(driver) -> tuple[int, int, int]:
    size = driver.get_window_size()
    return (size["width"] // 2,
            int(size["height"] * 0.33), int(size["height"] * 0.66))


def _swipe(driver, x: int, y1: int, y2: int, ms: int = 260) -> None:
    """One vertical swipe, with a plain-adb fallback.

    UiAutomator2 refuses W3C action chains outright now and then
    ("Unable to perform W3C actions") while `input swipe` on the same
    device at the same moment works fine. The gesture is uncontroversial —
    mid-screen, presses nothing — so the walk falls back instead of dying
    on the driver's mood.
    """
    try:
        driver.swipe(x, y1, x, y2, ms)
    except Exception:  # noqa: BLE001 — the fallback is the point
        subprocess.run(["adb", "shell", "input", "swipe",
                        str(x), str(y1), str(x), str(y2), str(ms)],
                       capture_output=True, timeout=20, check=True)


def _page_folds(statics: list[dict]) -> bool:
    """Whether this page is trusted to fold rather than navigate on a row
    tap: the schedule list, recognised by its run of date headers. One or
    two dates on screen is any details page; a run of them is the week."""
    dates = sum(1 for s in statics
                if _DATE_HEADER.search(s.get("txt") or ""))
    return dates >= EXPAND_MIN_DATE_HEADERS


def _today_span(statics: list[dict]) -> tuple[int, int] | None:
    """The y-range of TODAY's section: from its marked header ("agosto 17,
    2026 (Hoy)") down to the next date header, or None when today is not
    on screen. However many patients share the day, they all live in this
    span — that is the multi-patient case handled by construction."""
    dates = sorted(
        ((s["b"][1], (s.get("txt") or "").lower()) for s in statics
         if s.get("b") and _DATE_HEADER.search(s.get("txt") or "")),
        key=lambda pair: pair[0])
    for i, (y, txt) in enumerate(dates):
        if any(mark in txt for mark in _TODAY_MARKS):
            bottom = dates[i + 1][0] if i + 1 < len(dates) else 10**9
            return y, bottom
    return None


def _collapsed_rows(elements: list[dict], statics: list[dict],
                    width: int) -> list[list[int]]:
    """Bounds of TODAY's full-width rows still folded shut, top to bottom.

    A collapsed accordion is a trailing-edge chevron glyph standing
    taller than wide (the open state draws the same glyph rotated),
    sitting inside a row that spans the screen — and inside today's
    section, the only day whose cards are opened (see EXPAND_GLYPH).
    """
    span = _today_span(statics)
    if span is None:
        return []
    top, bottom = span
    rows: list[list[int]] = []
    for s in statics:
        if (s.get("txt") or "").strip() != EXPAND_GLYPH:
            continue
        b = s.get("b") or []
        if len(b) != 4 or (b[3] - b[1]) <= (b[2] - b[0]):
            continue                          # wide = already open
        if b[0] < width * 0.75:
            continue                          # trailing edge only
        cy = (b[1] + b[3]) // 2
        if not top <= cy < bottom:
            continue                          # another day's card
        for e in elements:
            eb = e.get("b") or []
            if (len(eb) == 4 and eb[2] - eb[0] >= width * 0.7
                    and eb[1] <= cy <= eb[3]):
                rows.append(eb)
                break
    rows.sort(key=lambda eb: eb[1])
    return rows


def _chevron_count(statics: list[dict]) -> int:
    return sum(1 for s in statics
               if (s.get("txt") or "").strip() == EXPAND_GLYPH)


def _tap_xy(x: int, y: int) -> None:
    """A coordinate tap over plain adb — same channel the portal's own taps
    use, and immune to the driver's W3C moods that _swipe falls back from."""
    subprocess.run(["adb", "shell", "input", "tap", str(x), str(y)],
                   capture_output=True, timeout=10, check=True)


def _scroll_to_top(driver, cx: int, y_top: int, y_bot: int) -> None:
    prev = None
    for _ in range(8):
        src = driver.page_source or ""
        if src == prev:
            break
        prev = src
        _swipe(driver, cx, y_top, y_bot)
        time.sleep(STITCH_SETTLE)


def _stitch_walk(driver, assume_top: bool = False) -> bool:
    """Walk the current page and write the stitched whole-page document.

    Swipes move the page and can press nothing; the page is returned to
    its top, which is also the scroll state every below-the-fold tap
    replay starts from.

    ``assume_top`` skips the scroll-to-top probe. A page entered by a
    fresh transition — a tab tapped, an activity opened — is already at
    its top, and the probe swipe plus its settle was pure latency on the
    common case. A re-scan of a page that may have been left scrolled
    still pays it.
    """
    from apt_log import feed as feed_mod
    from apt_log import stitch as stitch_mod

    # Own the session for the duration: the watcher holds its last complete
    # frame while this is set, so the scan is not slowed by interleaved
    # dumps and the live view does not lurch through the scroll.
    SCAN_ACTIVE.set()
    try:
        # On a signature canvas a swipe is INK. The walk decision reads the
        # published doc, which can be a page old — the checklist's Salvar
        # landed on the caregiver signature screen while a walk was already
        # due, and the scroll gesture drew a straight line down the canvas
        # she was about to sign (seen live, first field test). The screen
        # in front at swipe time is the only one that counts.
        src = driver.page_source
        if isinstance(src, str) and feed_mod._has_canvas(src):
            log.info("signature canvas in front; the walker keeps its "
                     "hands off")
            return False
        cx, y_top, y_bot = _swipe_geometry(driver)

        # The package and the screen size, so the scan names controls exactly
        # as a viewport capture does. Without them a STITCHED page — which is
        # what a walked screen becomes — came back with every portal name
        # missing, and the agency filter drew blank again on the one screen
        # that gets walked most. The naming has to happen wherever elements
        # are built, and this is the second place.
        # NOT `size`: this function rebinds that name to the driver's own
        # {"width": …, "height": …} dict further down, and `capture` is a
        # closure that reads it at CALL time — so naming was handed a dict,
        # unpacked its keys, and tried to multiply the string "height" by a
        # float. A distinct name is the fix; the coercion in `controls` is
        # the belt.
        package = driver.current_package or ""
        screen_wh = _screen_size(driver)

        def capture() -> dict:
            src = driver.page_source or ""
            return {"elements": feed_mod.elements(src, label=True,
                                                  package=package,
                                                  size=screen_wh),
                    "statics": feed_mod.statics(src)}

        def settled_capture() -> dict:
            """A capture the page has stopped moving under.

            The schedule ANIMATES: expansion unfolds over a few hundred
            milliseconds, and recompose after a swipe rebuilds cards in
            stages. A single dump on a fixed settle caught trees mid-
            animation — name lines with a card's height of nothing under
            them, chevrons absent — and stitching half-built trees is
            where the duplicated day headers actually came from. Stable
            means two consecutive dumps agree, the same test the
            scroll-to-top probe has always trusted.
            """
            cap = capture()
            for _ in range(3):
                time.sleep(STITCH_SETTLE)
                again = capture()
                if again == cap:
                    return cap
                cap = again
            return cap

        # Accordions are opened as the walk reaches them, so the scan
        # captures what they hide (see EXPAND_GLYPH above). Budgeted per
        # walk, gated to the proven app and page shape, and guarded: a tap
        # that made the page's chevrons vanish navigated somewhere instead
        # of unfolding — one Back returns, and the walk stops trusting taps.
        #
        # And only on a FRESH ENTRY (assume_top: a tab tapped, an app
        # opened). A re-scan of the same page means someone changed it —
        # and if what changed is that she CLOSED a card, a scan that
        # reopens it is the phone fighting her hand. Unfolding is part of
        # arriving at a page, never part of watching one.
        size = driver.get_window_size()
        taps_left = EXPAND_MAX_TAPS
        expanding = assume_top and (driver.current_package or "") in EXPAND_APPS
        opened = 0

        def open_folds(cap: dict) -> dict:
            nonlocal taps_left, expanding, opened
            if not expanding or not _page_folds(cap["statics"]):
                return cap
            while taps_left > 0:
                rows = _collapsed_rows(cap["elements"], cap["statics"],
                                       size["width"])
                if not rows:
                    break
                eb = rows[0]
                before = _chevron_count(cap["statics"])
                taps_left -= 1
                try:
                    _tap_xy((eb[0] + eb[2]) // 2, (eb[1] + eb[3]) // 2)
                except Exception as exc:  # noqa: BLE001
                    log.info("accordion tap failed (%s); scanning as-is", exc)
                    expanding = False
                    break
                time.sleep(STITCH_SETTLE)
                fresh = settled_capture()
                # An unfolded card keeps the page's date headers and its
                # chevrons (one merely rotated); a page missing them is
                # wherever the tap navigated to instead.
                if (not _page_folds(fresh["statics"])
                        or _chevron_count(fresh["statics"]) < before - 2):
                    log.warning("a fold tap navigated instead of unfolding; "
                                "backing out")
                    expanding = False
                    driver.press_keycode(4)
                    time.sleep(STITCH_SETTLE)
                    return capture()
                opened += 1
                cap = fresh
            return cap

        # ONLY the first viewport's folds are opened, before the first
        # capture. Two designs failed live before this one: opening cards
        # as the walk reached them moved content between captures and the
        # stitch published duplicated day headers; and a second clean
        # sweep after an expansion pass corrupted identically, because
        # the app FORGETS an opened card once it scrolls out of view (12
        # opened at one walk, 8 found folded again minutes later) — a
        # fully-open page does not exist to be rescanned. What is stable:
        # taps that all land before anything is captured. The first
        # viewport is today's cards, the ones she is working with; deeper
        # cards keep their tappable header rows and open on request.
        if not assume_top:
            _scroll_to_top(driver, cx, y_top, y_bot)
        captures: list[dict] = []
        prev: dict | None = None
        for step in range(STITCH_MAX_STEPS):
            cap = settled_capture()
            if step == 0:
                cap = open_folds(cap)
                if opened:
                    log.info("opened %d folded cards in the first viewport",
                             opened)
            if prev is not None and cap == prev:
                break
            captures.append(cap)
            prev = cap
            _swipe(driver, cx, y_bot, y_top)
            time.sleep(STITCH_SETTLE)

        if not captures or not captures[0]["elements"]:
            for _ in range(max(len(captures) - 1, 0)):
                _swipe(driver, cx, y_top, y_bot)
                time.sleep(0.2)
            return False
        doc = stitch_mod.stitch(captures, nominal_dy=y_bot - y_top)
        doc.update({
            "step0": feed_mod.frame_id(captures[0]["elements"]),
            # Whose page this is: the feed's freshness check refuses to dress
            # another app's screen in this document, however similar.
            "app": driver.current_package or "",
            "at": datetime.now().isoformat(),
        })
        # Published BEFORE the walk back to the top: the near-match in
        # _fresh_stitch recognises the still-scrolled viewport as this page,
        # so the portal fills in whole seconds earlier than it used to.
        try:
            STITCH_DIR.mkdir(parents=True, exist_ok=True)
            target = STITCH_DIR / f"{doc['step0']}.json"
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc), encoding="utf-8")
            os.replace(tmp, target)
            _prune_stitched()
        except OSError as exc:
            log.warning("cannot write the stitched page (%s)", exc)
            return False
        log.info("stitched %d captures into a whole-page document",
                 len(captures))

        for _ in range(max(len(captures) - 1, 0)):   # leave page at its top
            _swipe(driver, cx, y_top, y_bot)
            time.sleep(0.2)
        return True
    finally:
        SCAN_ACTIVE.clear()


def _prune_stitched() -> None:
    """Keep the scan cache small and current: newest few pages, none old."""
    try:
        files = sorted(STITCH_DIR.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    now = time.time()
    for i, f in enumerate(files):
        try:
            if i >= STITCH_CACHE_MAX or now - f.stat().st_mtime > STITCH_CACHE_TTL:
                f.unlink()
        except OSError:
            pass


def _forget_stitched(app: str) -> None:
    """Drop cached scans of one app's pages — after a tap changed them."""
    try:
        for f in STITCH_DIR.glob("*.json"):
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
                if not app or doc.get("app") == app:
                    f.unlink()
            except (OSError, json.JSONDecodeError):
                f.unlink(missing_ok=True)
    except OSError:
        pass


def _tab_slots(driver) -> list[dict]:
    """The current screen's tab bar, left to right, or [].

    Each slot is ``{"point": (x, y), "selected": bool}``. Detection and
    aim come from two different nodes, because on Compose they disagree:

    - The tab LABELS enumerate the slots. Every tab carries its caption,
      selected or not, so the labels see all the slots — including the
      selected one, which is how the sweep knows when it is home.
    - The clickable CONTAINER is the tap point AND the selected marker.
      Tapping the label's own centre does nothing — verified live, the
      caption sits in the tab's lower strip where the touch does not take;
      the container's centre, up in the icon zone, is where the tab
      responds. Compose does not give the SELECTED tab a container, so a
      label with no container beneath it is the tab already in front —
      which is the only "am I home?" signal that does not depend on a
      frame the app keeps changing under us.
    """
    from apt_log import feed as feed_mod

    size = driver.get_window_size()
    w, h = size["width"], size["height"]
    src = driver.page_source or ""
    band = h * WARM_TAB_BAND
    narrow = w * WARM_TAB_MAX_WIDTH
    labels = [s for s in feed_mod.statics(src)
              if s["b"][1] > band and (s.get("txt") or "").strip()
              and (s["b"][2] - s["b"][0]) < narrow]
    labels.sort(key=lambda s: s["b"][0])
    containers = [e for e in feed_mod.elements(src)
                  if e["b"][3] > band and (e["b"][2] - e["b"][0]) < narrow]

    # WHERE THE SELECTED TAB IS PRESSED, borrowed from its neighbours.
    #
    # The selected tab has no container of its own, so its aim has to be
    # inferred — and it used to be inferred as "forty pixels above the
    # caption", which is only the icon zone on a 720-wide screen at the
    # density this phone happens to run. The tabs all sit in ONE row, so the
    # neighbours' containers already state the answer at whatever resolution
    # the device draws: same y, no arithmetic, nothing to re-tune.
    row_y = 0
    if containers:
        mids = sorted((e["b"][1] + e["b"][3]) // 2 for e in containers)
        row_y = mids[len(mids) // 2]

    slots: list[dict] = []
    for s in labels:
        cx = (s["b"][0] + s["b"][2]) // 2
        holder = next((e for e in containers
                       if e["b"][0] <= cx <= e["b"][2]), None)
        if holder:
            slots.append({"point": ((holder["b"][0] + holder["b"][2]) // 2,
                                    (holder["b"][1] + holder["b"][3]) // 2),
                          "selected": False})
        else:
            # No neighbour to copy means a one-tab bar, which is not a bar
            # worth sweeping; the caption's own top is the honest fallback.
            slots.append({"point": (cx, row_y or s["b"][1]), "selected": True})
    return slots


def _warm_sweep(driver, request_path, deep_path, poke_path) -> int:
    """Open each sibling tab once and scan it, then return to the landing
    tab. Non-committing throughout (tab switches change no records), and
    it bails the instant a real action is waiting. Returns how many tabs
    it warmed."""
    # The app may still be settling from the sign-in's activate_app when
    # the sweep starts; wait for the tab bar rather than mistaking a
    # half-drawn screen for an untabbed one.
    slots = _tab_slots(driver)
    deadline = time.monotonic() + WARM_TABBAR_WAIT
    while len(slots) < 2 and time.monotonic() < deadline:
        if someone_wants_the_phone(request_path, deep_path, poke_path):
            return 0
        time.sleep(0.5)
        slots = _tab_slots(driver)
    if len(slots) < 2:
        return 0                       # not a tabbed screen; nothing to warm

    warmed = 0
    for slot in slots[1:]:             # slot 0 is the landing (leftmost)
        if someone_wants_the_phone(request_path, deep_path, poke_path):
            break
        driver.tap([slot["point"]])
        time.sleep(WARM_SETTLE)
        if _stitch_walk(driver, assume_top=True):
            warmed += 1
    _return_to_landing(driver)
    return warmed


def _return_to_landing(driver) -> None:
    """Select the leftmost tab, so the sweep never leaves her on a tab she
    did not choose. Home is 'the leftmost tab is selected' — no container
    under its label — never a remembered frame (the app changes those) and
    never Back (that walked out of the app entirely, to the launcher).
    Bounded, and it acts only on a visible tab bar, so a screen without one
    is left as-is rather than escaped."""
    for _ in range(4):
        slots = _tab_slots(driver)
        if not slots or slots[0]["selected"]:
            return
        driver.tap([slots[0]["point"]])
        time.sleep(WARM_SETTLE)


def take_deep_tap() -> dict | None:
    """Claim a pending below-the-fold tap request, removing it."""
    try:
        payload = json.loads(DEEPTAP_REQUEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        DEEPTAP_REQUEST_PATH.unlink()
    except OSError:
        pass
    if time.time() - float(payload.get("at", 0)) > DEEPTAP_MAX_AGE:
        return None
    if not isinstance(payload.get("aim"), dict):
        return None
    return payload


def _open_app(package: str):
    """Bring an app to the front and wait for it to settle. Nothing more.

    Android keeps app state, so for an app she is already signed into this is
    the whole of "switching to it". For the three apps whose sign-in flows have
    never been walked, it is also the most a button may honestly do: opening a
    screen nobody has mapped is safe exactly because it does nothing on it.
    """
    def run(driver, report) -> None:
        report("macro.step.launching")
        wake_display()
        driver.activate_app(package)
        wait_for(lambda: bool(driver.current_activity), timeout=15.0)
        report("macro.step.checking")
        # Settled means the foreground package is the one asked for — not a
        # judgement about which of its screens it landed on.
        if not wait_for(lambda: driver.current_package == package, timeout=10.0):
            raise RuntimeError("the app did not come to the front")
    return run


def _area(element) -> float:
    """How big a control is, for choosing between nested matches. An element
    that will not report its size sorts last rather than first: unknown is
    not a reason to press something."""
    try:
        rect = element.rect
        return float(rect["width"]) * float(rect["height"])
    except Exception:  # noqa: BLE001
        return float("inf")


def _inmyteam_login(driver, report) -> None:
    """inMyTeam — get as far as the code, and stop there."""
    _inmyteam_walk(driver, report, resend=False)


def _inmyteam_resend_code(driver, report) -> None:
    """Ask inMyTeam to text a NEW code, when the one she has has expired.

    The app offers no way back. Its code screen has a field and a submit and
    nothing else — no "send another", no way back to the number — so an
    expired code is a dead end reached at the worst moment: the walk is one
    step from done and the only exit anybody had found was to force-stop the
    app and start the whole sign-in again.

    Which is exactly what this does, deliberately and in one press, instead
    of leaving it as folklore. The app is stopped, relaunched, walked
    splash → number → Sign in, and the app texts a fresh code.

    DELIBERATE, never automatic, and it asks first. `_inmyteam_login`'s
    docstring already spells out why: a retry here is a second real text
    message to a real person and a step toward a rate limit that would lock
    the account out of the app entirely. That reasoning forbids a macro
    deciding to do this on its own. It does not forbid a person deciding to.
    """
    from apt_log import feed as feed_mod

    package = "com.inmyteam.inmyteam"
    report("macro.step.clearing")
    # Force-stopped rather than navigated: there is nothing to navigate. The
    # code screen's only controls are its field and its submit, and Back on
    # it either does nothing or retreats to a splash that resumes straight
    # into the same expired screen.
    _force_stop(package)
    _forget_stitched(package)
    time.sleep(1.5)
    _inmyteam_walk(driver, report, resend=True)


def _inmyteam_walk(driver, report, resend: bool = False) -> None:
    """The sign-in walk. `resend` refuses to stop at a code screen.

    This app had no sign-in macro at all, so its tile ran the open-only one:
    the app came to the front, landed on its marketing splash, and waited for
    a tap nobody was going to give it. Reported from the field as "it should
    have entered a phone number waiting for the OTP but it's not getting me
    to that step".

    Three screens, and the third is the point:

    1. **The splash.** "THE FUTURE of home care agencies" with one control,
       "Let's Get Started". No id, no text on the node itself — the caption
       is a separate view inside it — so it is found by the words it
       contains.
    2. **The number.** One EditText ("Enter your cell phone number") and a
       "Sign in" button. The number comes from INMYTEAM_PHONE.
    3. **The code.** Pressing Sign in sends a real SMS, and this macro stops
       here on purpose. The portal's type bar exists for this moment — she
       aims at the code field and types the code herself, which is the same
       contract every other short credential goes through. A macro cannot
       invent a code, and pretending otherwise would mean retrying, which
       means a second text to a real person and a walk toward a rate limit.

    Auth only, like the others: if the app opens onto anything but this
    walk, the session is alive and there is nothing to do.
    """
    from apt_log.secrets import (INMYTEAM_PHONE, FileSecretProvider,
                                 SecretNotFound)

    package = "com.inmyteam.inmyteam"
    report("macro.step.launching")
    wake_display()
    driver.activate_app(package)
    # Wait for THIS app to be in front, not merely for some activity to be
    # reported. `current_activity` answers the moment the driver knows
    # anything, including the app the phone is leaving — and the first live
    # run read Mobile Caregiver+'s tree, found no field in it, and reported
    # "already signed in" over an inMyTeam splash that had not drawn yet.
    if not wait_for(lambda: driver.current_package == package, timeout=20.0):
        raise RuntimeError("the app did not come to the front")

    def field():
        """The number box, if this screen has one."""
        found = [e for e in driver.find_elements(
            "xpath", '//*[@class="android.widget.EditText"]')
            if e.is_displayed()]
        return found[0] if found else None

    def by_words(*words):
        """The SMALLEST control containing the words. Smallest is the whole
        point.

        Compose gives these screens no ids and hangs the caption on a child
        view, so the text of a descendant is the only handle there is — but
        the screen's own root is clickable too, and it contains every word on
        it. Asking for "Sign in" and taking the first match pressed the
        full-screen container, because the heading reads "Sign in with your
        phone number": the number went in, nothing happened, and the macro
        looked like it had never typed anything. The button is the tightest
        node that still contains the words.
        """
        for word in words:
            hits = [e for e in driver.find_elements(
                "xpath",
                f'//*[@clickable="true"][contains(@text,"{word}")'
                f' or .//*[contains(@text,"{word}")]]')
                if e.is_displayed()]
            if hits:
                return min(hits, key=_area)
        return None

    def start_button():
        return by_words("Get Started", "Comenzar", "Empezar")

    # A freshly launched app exposes almost nothing for a second or two, and
    # an empty tree must never read as "signed in" — the trap the
    # HHAeXchange+ macro was bitten by and wrote down, sprung here anyway.
    # Wait for one of the two screens this walk knows before deciding.
    wait_for(lambda: field() is not None or start_button() is not None,
             timeout=20.0)

    # Already at the code, from an earlier run or from somebody's own tap.
    # This is the check that has to come FIRST, because both screens are one
    # EditText and a button: without it the walk reads the code box as the
    # number box, clears the code she may be part-way through typing, puts a
    # phone number in its place, and presses whatever looks like submit. The
    # code screen is where this macro is trying to get to — arriving to find
    # it already there is success, not a reason to start over.
    if _asks_for_a_code(driver):
        if not resend:
            report("macro.step.awaiting_code")
            return
        # A RESEND that is still looking at the code screen has not got back
        # to the start, and must not carry on. Both screens are one EditText
        # and a button, so the next few lines would clear the code box, type
        # a PHONE NUMBER into it and press submit — the trap the check above
        # exists to prevent, sprung by the one caller allowed past it.
        #
        # The force-stop should have left the app at its splash. If it did
        # not, that is a fact about this app worth surfacing rather than
        # working around blind.
        raise RuntimeError(
            "inMyTeam came back to the code screen; it did not reset to the "
            "number screen, so no new code was requested")

    # ---------------------------------------------------------- the splash
    if field() is None:
        start = start_button()
        if start is not None:
            report("macro.step.starting")
            start.click()
            wait_for(lambda: field() is not None, timeout=15.0)

    box = field()
    if box is None:
        # No field and no splash. Either the app is past the walk — signed
        # in, on its own home screen — or its tree is still unreadable, and
        # those two must not be confused. A screen with real content has
        # several tappable nodes; an unready one has none.
        if len(driver.find_elements("xpath", '//*[@clickable="true"]')) < 2:
            raise RuntimeError("the app is not showing anything yet")
        report("macro.step.finished")
        return

    # ---------------------------------------------------------- the number
    report("macro.step.signing_in")
    try:
        number = FileSecretProvider().get(INMYTEAM_PHONE)
    except SecretNotFound as exc:
        raise RuntimeError(
            "no phone number is stored for inMyTeam") from exc

    # Cleared first: the app remembers the last number, and typing into a
    # prefilled box appends to it — the trap the HHAeXchange+ web form
    # already sprang once.
    box.clear()
    box.send_keys(number)

    submit = by_words("Sign in", "Iniciar")
    if submit is None:
        raise RuntimeError("the sign-in button is not on this screen")
    # Stamped before the press, so "a code that arrived after we asked" has a
    # meaning. Anything older than this instant is a previous attempt's.
    sent_at = time.time()
    submit.click()

    # ------------------------------------------------------------ the code
    # Done means the number was accepted and the app is asking for the code.
    # Not "the screen changed": a rejected number also changes the screen,
    # and reporting success over an error message is how a macro teaches
    # somebody to distrust it.
    report("macro.step.awaiting_code")
    if not wait_for(lambda: _asks_for_a_code(driver), timeout=25.0):
        raise RuntimeError("the app did not ask for a code")

    # THE CODE, IF IT CAME HERE. When the number on the account belongs to
    # the phone this controller drives, the text is sitting in its inbox and
    # the relay through a person is six digits being retyped from two feet
    # away. See sms.py for what it does and does not read.
    #
    # `sent` is taken BEFORE the wait, not inside it: a code already in the
    # inbox is the one that was rejected a minute ago, and typing it burns an
    # attempt on an app that limits them.
    if _fill_in_the_code(driver, report, sent=sent_at):
        return

    # Back to what is actually true. The read is over and it found nothing,
    # so the state is "waiting for a person" again — leaving the console on
    # "reading the texted code" would describe something that stopped.
    report("macro.step.awaiting_code")

    # The text has been sent, so somebody has to be told. This is the one
    # step in the whole system that cannot be waited out or refused when the
    # code lands on somebody else's phone — and a portal that sits there
    # silently asking is a portal nobody discovers is asking.
    _say_the_code_is_waiting()


# The portal opens on its app picker, and a notification that lands there has
# failed at the one job it has: the phone is holding a code screen open and the
# tap has to arrive AT IT. The view is asked for in the URL because the front
# end otherwise restores it from sessionStorage, and a window a notification
# opened is a fresh session with none — which is exactly how the first version
# came out on the picker, reported from the field that way.
CODE_DEEP_LINK = "/app?view=screen"

# Where the notification sends her. The tailnet name rather than an address:
# addresses change, and this string ends up on a lock screen where a wrong
# one is a dead end nobody can debug from.
#
# FULLY QUALIFIED, which the first version was not. `tailscale serve` publishes
# this portal at the node's whole MagicDNS name and holds a certificate for
# exactly that name, so the bare host fails validation rather than resolving to
# something friendlier — a tap that goes nowhere, on the one notification whose
# entire job is to be tapped. Read off `tailscale serve status` on the live
# machine rather than assumed a second time.
def _portal_url() -> str:
    from apt_log import push

    return os.environ.get("APTLOG_PORTAL_URL",
                          f"{push.PORTAL_ORIGIN}{CODE_DEEP_LINK}")


PORTAL_URL = _portal_url()

# The sentence itself. Deliberately says nothing about which patient, which
# agency, or what the code is — it goes to a public relay and lands on a lock
# screen, and neither of those is a place for any of that. It says what to do
# and where, which is all a notification is for.
PUSH_TITLE = "inMyTeam needs the code"
CODE_WAITING = ("inMyTeam texted you a sign-in code. Open the portal and "
                "type it in — the app is waiting on it.")

# When the code came to the phone the controller drives, it has already been
# typed by the time anyone reads this — so the sentence reports rather than
# asks. It carries the CODE, which the sentence above deliberately does not,
# and that is only safe because it goes by Web Push: encrypted to a specific
# subscription rather than to a public topic on a relay. See `_push_the_code`.
CODE_ARRIVED = ("inMyTeam code {code} — entered for you. Nothing to do "
                "unless the app is still asking.")


def _fill_in_the_code(driver, report, sent: float) -> bool:
    """Read the texted code off this phone and type it. True if signed in.

    Only possible when the account's number belongs to the phone the
    controller drives. Where it does not — which is where this started — the
    inbox holds nothing from that sender, this returns False in a couple of
    seconds, and the walk carries on to notify a human exactly as before.
    That fallback is the point: this feature can be wrong about the phone
    without being wrong about the outcome.

    The code is pushed as well as typed. Web Push and NOT the relay: push is
    encrypted to a specific subscription and opens the portal, while the
    relay is a public topic on somebody else's server, which is not a place
    for a live second factor. `notify` is deliberately not called here.
    """
    from apt_log import sms

    report("macro.step.reading_the_code")
    code = sms.wait_for_code(after=sent, timeout=CODE_WAIT)
    if not code:
        return False

    # PASSED ON BEFORE IT IS TYPED, and before anything that can fail. The
    # people on that list are the fallback for this walk going wrong, so
    # forwarding after a successful sign-in would send the code only in the
    # case where nobody needed it. Forced past the poll interval because a
    # code has demonstrably just landed; the dedup still holds, so the tick
    # behind this will not send it a second time.
    _pass_the_code_on()

    box = _code_box(driver)
    if box is None:
        # The screen moved under us between asking and answering. Better to
        # hand back to the human path than to type six digits at whatever is
        # in front now.
        log.warning("a code arrived but the code box is gone")
        return False

    report("macro.step.signing_in")
    try:
        box.clear()
        box.send_keys(code)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not type the code (%s)", exc)
        return False

    _push_the_code(code)

    verify = _verify_button(driver)
    if verify is not None:
        verify.click()
    # Signed in means the app stopped asking. A wrong or expired code leaves
    # the screen exactly where it was — inMyTeam's own complaint is a dialog
    # that never reaches the tree — so the absence of the question is the
    # only honest signal available.
    if wait_for(lambda: not _asks_for_a_code(driver), timeout=25.0):
        report("macro.step.finished")
        return True
    log.warning("the code was typed and the app is still asking")
    return False


# How long to wait for the text after pressing Sign in. Generous: a carrier
# can take half a minute on a bad day, and the cost of giving up early is
# falling back to the human path, which is where this started.
CODE_WAIT = 75.0


def _pass_the_code_on() -> None:
    """Text the code to whoever is on the list, if anybody is.

    Never fatal, and never allowed to stop the walk: this is a courtesy to
    people who are not standing at the phone, and the walk that IS at the
    phone is one step from signing in. A carrier problem must not cost that.

    Silent when no list is configured, which is the shipped state — see
    `secrets.CODE_RECIPIENTS`.
    """
    try:
        from apt_log import sms

        sent = sms.forward_any_new(force=True)
        if sent:
            log.info("code texted onward to %d", sent)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not text the code onward (%s)", exc)


def _push_the_code(code: str) -> None:
    """Send the code to whoever subscribed, and only to them.

    Chosen over the relay deliberately — see `_fill_in_the_code`. The relay
    is a public topic; this is encrypted per subscription and opens the
    portal. It is a convenience and a record, never the primary path: the
    code has already been typed by the time this runs.
    """
    try:
        from apt_log import push

        sent = push.send(PUSH_TITLE, CODE_ARRIVED.format(code=code),
                         url=CODE_DEEP_LINK, tag="otp")
        log.info("code pushed to %d subscriber(s)", sent)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not push the code (%s)", exc)


def _say_the_code_is_waiting() -> None:
    """Tell her the code is waiting, by both roads that exist.

    Web Push first and above all: it comes from the portal, so tapping it
    opens the portal — the app she installed, at the phone view the code
    screen is showing on rather than at the app picker (CODE_DEEP_LINK; the
    first version landed on the picker and was reported that way). A relay's
    notification can only open Safari at a URL, which is the wrong app on the
    one notice whose entire job is to be tapped.

    The relay stays as the fallback, and it is not redundant. Push reaches
    only phones that have subscribed, and only where iOS granted it; the
    relay reaches whoever configured it, from a machine that does not care
    whether this portal is healthy. Neither is enough on its own.
    """
    if _told_recently():
        # A SECOND GUARD, BEHIND THE COOLDOWN AND NOT INSTEAD OF IT.
        #
        # The cooldown stops the walk RUNNING again; this stops the same
        # sentence being sent twice if it does — by a hand-pressed macro, by
        # a second walk, by anything future. The reported symptom was a lock
        # screen of identical notices, and identical is the operative word:
        # the second one tells her nothing the first did not, and iOS does
        # not reliably collapse them by tag the way the service worker asks.
        log.info("code notice: already sent recently, not repeating")
        return

    pushed = 0
    try:
        from apt_log import push

        pushed = push.send(PUSH_TITLE, CODE_WAITING, url=CODE_DEEP_LINK,
                           tag="otp")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not push the code notice (%s)", exc)

    from apt_log import notify

    notify.send(CODE_WAITING, url=PORTAL_URL)
    _mark_told()
    log.info("code notice: pushed to %d subscriber(s)", pushed)


# How long one "the code is waiting" notice speaks for. The same window as
# the sign-in cooldown, because it is the same event: a code that has been
# asked for and not yet used.
TOLD_QUIET = SMS_AUTH_COOLDOWN
TOLD_PATH = STATE_DIR / "code-notice.json"


def _told_recently(path: Path | None = None) -> bool:
    try:
        when = float(json.loads(
            (path or TOLD_PATH).read_text(encoding="utf-8"))["at"])
    except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError):
        return False
    since = time.time() - when
    # A future timestamp is a clock that moved, not a notice from later.
    return 0 <= since < TOLD_QUIET


def _mark_told(path: Path | None = None) -> None:
    try:
        target = path or TOLD_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"at": time.time()}), encoding="utf-8")
    except OSError as exc:
        log.debug("cannot record the code notice (%s)", exc)


# The words every version of this screen uses. Matched loosely because the
# app switches language with the phone and this is the one screen nobody has
# a second chance at: getting it wrong strands the walk one step from done.
_CODE_WORDS = ("code", "código", "codigo", "verification", "verificación",
               "otp", "sms")


def _asks_for_a_code(driver) -> bool:
    """Whether the screen in front is asking for the texted code.

    A field plus the words for one. The field alone is not enough — the
    number screen has a field too, and the two look identical to anything
    that only counts boxes.
    """
    try:
        boxes = [e for e in driver.find_elements(
            "xpath", '//*[@class="android.widget.EditText"]')
            if e.is_displayed()]
        if not boxes:
            return False
        words = (driver.page_source or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return any(w in words for w in _CODE_WORDS)


def _code_box(driver):
    """The field the code goes in, or None.

    The same test `_asks_for_a_code` uses, kept separate because arriving at
    the screen and typing into it are different moments: the screen can move
    between them, and typing six digits at whatever is in front now is the
    failure this exists to avoid.
    """
    try:
        boxes = [e for e in driver.find_elements(
            "xpath", '//*[@class="android.widget.EditText"]')
            if e.is_displayed()]
    except Exception:  # noqa: BLE001
        return None
    return boxes[0] if len(boxes) == 1 else None


# What the button says, in the two languages this phone is ever in. Read off
# the live screen: inMyTeam labels it "Verify", with "Call" beneath it as a
# separate row — which is why this matches the word and not merely a button
# near the bottom of the screen.
_VERIFY_WORDS = ("verify", "verificar")


def _verify_button(driver):
    """inMyTeam's own Verify, or None.

    None is a real answer, not a failure: on the live screen the control is a
    clickable View carrying its caption in a child, and pressing Enter on the
    field submits too. The caller treats a missing button as "typed, now see
    whether it stopped asking" rather than as an error.
    """
    lowered = " ".join(
        f'contains(translate(@text,"VERIFYCA","verifyca"),"{word}")'
        for word in _VERIFY_WORDS)
    try:
        found = driver.find_elements(
            "xpath", f'//*[@clickable="true" and ({lowered})]')
    except Exception:  # noqa: BLE001
        return None
    return found[0] if found else None


# --------------------------------------------------------------- operations
# The sign-in macros above are the ones that took the most work and the ones
# nobody presses any more: they run themselves, when a session expires. What a
# person actually reaches for is the short list below — the things you do when
# something is stuck and you are two thousand kilometres from the phone.
#
# All of them are deliberately blunt. A stuck app does not need a clever
# recovery; it needs the same three or four moves someone in the room would
# make, available to someone who is not.

def _front_package() -> str:
    from apt_log import feed as feed_mod

    return (feed_mod.current_focus() or "").split("/")[0]


def _force_stop(package: str) -> None:
    """Kill an app the way the ANR recovery does — through adb, never through
    the driver, because a wedged app takes the driver down with it."""
    from apt_log import feed as feed_mod

    feed_mod._adb(["shell", "am", "force-stop", package])


# THE UPDATE WALL, ANSWERED — BY A PERSON, ON PURPOSE.
#
# `UPDATE_WALL_MARKERS` above says why the wall is never pressed automatically:
# installing a new version of an app this project reads by resource-id changes
# every assumption in this file, and that is a decision for a morning when
# somebody can check afterwards, not for a macro at 6am.
#
# What that reasoning does NOT justify is having no way to do it at all. Mobile
# Caregiver+ raised its wall and the app was simply unusable — no visit can be
# recorded on a screen whose only control leads to the Play Store — and the
# alternative to this macro is a person driving the Store through the phone
# peek, on a device with the containment watchdog bouncing them back to the
# care app every five seconds.
#
# So: deliberate, confirmed, and only offered where an update is actually
# being demanded. It runs as a macro, which is what pauses containment — the
# watchdog already stands down for a macro in flight, and resumes the moment
# this one ends, however it ends.
STORE_PACKAGE = "com.android.vending"

# Play's own button, in the two languages this phone is ever in. Matched on
# the wording rather than a resource-id because the Store's ids are generated
# and change between its own releases — the one app here guaranteed to be
# newer than anything written about it.
#
# WHOLE labels, not substrings, and that is not fussiness. The listing this
# was walked against carries "Last updated Aug 10, 2026" a few rows under the
# button; anything matching on `contains` finds that too, and the clickable
# thing wrapping it is a different control entirely. An exact label is the
# cheapest way to be sure which word was pressed.
STORE_UPDATE_LABELS = ("update", "actualizar", "update now", "actualizar ahora")

# ...and the labels that read almost the same and mean something else.
# "Update all" on a Store landing page updates eleven apps nobody asked about;
# the settings row toggles automatic updates for the whole phone.
STORE_NOT_UPDATE = ("actualizar todo", "update all", "actualizaciones",
                    "auto-actualizar", "auto-update", "updates",
                    "actualizar todas", "update all apps")

# An APK over Wi-Fi, with the download and the install both inside it. Long,
# because the failure mode of a short wait here is a macro that reports
# failure over an update that then lands anyway.
STORE_INSTALL_TIMEOUT = 420.0
STORE_POLL = 6.0

# The listing draws in stages and the button is not in the first of them.
STORE_BUTTON_TRIES = 6
STORE_BUTTON_WAIT = 2.0


def _rect(element) -> dict | None:
    """An element's box, or None if it will not give one up."""
    try:
        r = element.rect
        return {"x": int(r["x"]), "y": int(r["y"]),
                "w": int(r["width"]), "h": int(r["height"])}
    except Exception:  # noqa: BLE001 — a stale node has no box
        return None


def _encloses(outer: dict, inner: dict) -> bool:
    return (outer["x"] <= inner["x"]
            and outer["y"] <= inner["y"]
            and outer["x"] + outer["w"] >= inner["x"] + inner["w"]
            and outer["y"] + outer["h"] >= inner["y"] + inner["h"])


def _store_update_button(driver):
    """Where to tap to update this app, as a box, or None.

    THE LABEL AND THE BUTTON ARE DIFFERENT NODES. Walked live on the Store
    that ships on this phone: the button is a clickable View carrying no text
    at all, and the word sits in a child that is not clickable —

        clickable View  [831, 93][1137, 118]  ''
        View            [972,100][ 996, 111]  'Update'

    — so looking for a clickable element whose OWN text says Update finds
    nothing, which is exactly what the first version of this did on a screen
    that plainly had the button on it.

    So: find the word, then take the smallest clickable box that encloses it.
    Smallest because the whole page encloses it too, and tapping the page is
    not pressing the button. This is the same shape as the caption-pairing
    the screen reflow already does, for the same reason — apps draw a control
    as a box with a label loose inside it.
    """
    labels = []
    for node in driver.find_elements("xpath", "//*[@text or @content-desc]"):
        try:
            words = ((node.text or "")
                     or (node.get_attribute("content-desc") or ""))
        except Exception:  # noqa: BLE001 — a stale node is not a label
            continue
        words = words.strip().lower()
        if words not in STORE_UPDATE_LABELS or words in STORE_NOT_UPDATE:
            continue
        box = _rect(node)
        if box:
            labels.append(box)
    if not labels:
        return None

    best = None
    for node in driver.find_elements("xpath", '//*[@clickable="true"]'):
        box = _rect(node)
        if not box or not box["w"] or not box["h"]:
            continue
        if not any(_encloses(box, label) for label in labels):
            continue
        if best is None or box["w"] * box["h"] < best["w"] * best["h"]:
            best = box
    return best


def _update_app(driver, report) -> None:
    """Update whatever care app is in front. The update wall's own button."""
    _update(driver, report, _last_care_package())


def _update_app_for(package: str):
    """Update ONE named app, whether or not it is the one on screen.

    The wall card above can only ever offer the app that raised a wall, and
    only Mobile Caregiver+ has wall wording written down — so on the other
    two there would be no way to update at all until one of them blocked
    itself, which is the moment you least want to be discovering the path.

    Offered from the console's version panel instead: the operator's page,
    beside the build number it is about, asking first.
    """
    def run(driver, report) -> None:
        _update(driver, report, package)
    return run


def _update(driver, report, package: str) -> None:
    """Take one app through the Play Store's update, and come back to it.

    Watched to completion by the VERSION, not by the Store's own screen: the
    button that says "Open" when it is done is the same button that said
    "Update" a minute ago, drawn by an app that redesigns itself, while
    `dumpsys package` answers the actual question — is a different build
    installed than the one that was installed when this started.
    """
    from apt_log import feed as feed_mod
    from apt_log import versions as versions_mod

    if package not in feed_mod.CARE_APPS:
        raise RuntimeError("the phone is not showing one of the care apps")
    if feed_mod.retired(package):
        raise RuntimeError("that app has been retired")

    was = versions_mod.of(package)
    report("macro.step.opening_store")
    wake_display()
    feed_mod._adb(["shell", "am", "start", "-a", "android.intent.action.VIEW",
                   "-d", f"market://details?id={package}"])
    if not wait_for(lambda: _front_package() == STORE_PACKAGE, timeout=25.0):
        raise RuntimeError("the Play Store did not open")

    report("macro.step.updating")
    button = None
    for _ in range(STORE_BUTTON_TRIES):
        button = _store_update_button(driver)
        if button is not None:
            break
        time.sleep(STORE_BUTTON_WAIT)
    if button is None:
        # Said plainly rather than waited out. "The Store is not offering one"
        # is a real answer — it is what a phone whose update already landed in
        # the background looks like — and it is not the same as a failure.
        _back_to(package, report)
        raise RuntimeError("the Play Store is not offering an update for this app")

    driver.tap([(button["x"] + button["w"] // 2,
                 button["y"] + button["h"] // 2)])

    report("macro.step.installing")
    end = time.time() + STORE_INSTALL_TIMEOUT
    while time.time() < end:
        time.sleep(STORE_POLL)
        now = versions_mod.of(package)
        if now and now.get("code") and now.get("code") != was.get("code"):
            break
    else:
        _back_to(package, report)
        raise RuntimeError("the update did not finish in time")

    # The new build may draw its pages differently, and the whole-page cache
    # is keyed by app rather than by version — so a stitched document from
    # five minutes ago is now a picture of software that is no longer on the
    # phone.
    _forget_stitched(package)
    _back_to(package, report)
    # Records the change and logs it, so the console marks which app moved.
    versions_mod.check(force=True)


def _back_to(package: str, report) -> None:
    """Out of the Store and back into the care app, whatever happened."""
    from apt_log import feed as feed_mod

    report("macro.step.launching")
    feed_mod._adb(["shell", "monkey", "-p", package,
                   "-c", "android.intent.category.LAUNCHER", "1"])
    wait_for(lambda: _front_package() == package, timeout=30.0)


def _close_app(driver, report) -> None:
    """Close the app in front, and leave the phone on the launcher.

    For the state that no amount of tapping fixes: a screen the app will not
    leave, a spinner that never resolves, a form that has forgotten what it
    was doing. Restricted to the four care apps — force-stopping the system
    UI is a bigger hammer than any screen is worth, and this button is
    reachable from a phone in another state.
    """
    from apt_log import feed as feed_mod

    report("macro.step.closing")
    package = _front_package()
    if package not in feed_mod.CARE_APPS:
        raise RuntimeError("the phone is not showing one of the care apps")
    _force_stop(package)
    _forget_stitched(package)


def _restart_app(driver, report) -> None:
    """Close the app in front and open it again — the recipe the ANR watchdog
    uses, on demand rather than on a diagnosis.

    Relaunched through the launcher intent rather than the driver: after a
    force-stop the driver's handle on the app is stale, and asking it to
    activate the app it has just lost is how a recovery hangs.
    """
    from apt_log import feed as feed_mod

    report("macro.step.closing")
    package = _front_package()
    if package not in feed_mod.CARE_APPS:
        raise RuntimeError("the phone is not showing one of the care apps")
    _force_stop(package)
    _forget_stitched(package)
    time.sleep(1.5)
    report("macro.step.launching")
    wake_display()
    feed_mod._adb(["shell", "monkey", "-p", package,
                   "-c", "android.intent.category.LAUNCHER", "1"])
    if not wait_for(lambda: _front_package() == package, timeout=25.0):
        raise RuntimeError("the app did not come back")


# An app's own front page, by the atlas's names for it. Three rather than one
# because the apps disagree about what "the beginning" is: Mobile Caregiver+
# opens on its week of visits, HHAeXchange+ on a home screen or — with more
# than one agency on the account — on the picker, and a picker IS the
# beginning for an account that has to choose before it can do anything.
HOME_SCREENS = ("home", "today", "agency")

# How many times to press Back looking for one. Bounded because an app that
# never reports a page this recognises would otherwise be walked backwards out
# of itself, which is the exact fault this exists to fix.
BACKS_TO_HOME = 6

# How long a Back takes to land. Short: this is one keyevent and a redraw, and
# the loop below does up to six of them while somebody waits.
BACK_SETTLE = 0.9

# APPS WHOSE OWN FLOW LEAVES THEIR PACKAGE.
#
# HHAeXchange+ signs in through a Chrome Custom Tab, so for the length of that
# form the foreground package is Chrome and the app is still, in every sense
# that matters to a person holding the phone, the app. `_hhax_uma_login`
# already knows this — it checks for the web form by package for exactly this
# reason.
#
# Found the hard way: without this the walk read Chrome as "you have left",
# activated the app, which resumed onto the same pending tab, and reported
# success from inside a browser. Back inside a Custom Tab closes it and
# returns to the app, which is what the walk wants anyway.
WEB_FLOW_HOST = {"com.hhaexchange.uma": "com.android.chrome"}


def _app_home(driver, report) -> None:
    """Back to the app's own front page, without leaving the app.

    Reported as the thing that made a wrong tap feel like a dead end: "if I
    click on the wrong patient then what, I'm stuck? I need to open the app
    again?" Reopening works and costs a sign-in nobody needed — the session
    was alive the whole time, the app was simply three screens deep.

    Presses Back and looks, rather than knowing a route per app. The atlas
    already names each app's pages, and asking "am I home yet" after each
    press is both simpler than a per-app map and honest about apps whose
    pages nobody has walked: inMyTeam has no atlas entries at all, and this
    still gets it to wherever Back stops being able to go.

    THE ONE THING IT WILL NOT DO IS LEAVE. Back from an app's root pops the
    task stack and lands in whatever was under it — the launcher, or another
    care app. If that happens the app is brought straight back and the walk
    stops: being at the app's second screen is a worse answer than being at
    its first, and both are far better than being somewhere else entirely.
    """
    from apt_log import device as device_mod
    from apt_log import feed as feed_mod

    package = _front_package()
    if package not in feed_mod.CARE_APPS:
        raise RuntimeError("the phone is not showing one of the care apps")

    report("macro.step.checking")
    for _ in range(BACKS_TO_HOME):
        focus = feed_mod.current_focus() or ""
        front = focus.split("/")[0]
        if front != package and front != WEB_FLOW_HOST.get(package):
            # Back took us out. Undo it, and stop rather than press again —
            # a second press from here would leave a second time.
            driver.activate_app(package)
            wait_for(lambda: _front_package() == package, timeout=10.0)
            break
        if front == package and feed_mod.screen_for(focus,
                                                    _tree()) in HOME_SCREENS:
            break
        report("macro.step.navigating")
        device_mod.send_ui_action("back")
        time.sleep(BACK_SETTLE)
    else:
        # Ran out of presses without recognising a front page. Activating is
        # the last honest move: it returns the app's own task to the front
        # without restarting it, so nothing is lost and no session is spent.
        driver.activate_app(package)

    _forget_stitched(package)

    # AND THEN CHECK, rather than reporting done because the loop ended.
    #
    # The first version said "finished" from wherever it stopped, and the
    # live run caught it out immediately: HHAeXchange+ was resumed onto its
    # pending sign-in tab, the walk read Chrome as "left the app", activated
    # the app — which reopened the same tab — and reported success with the
    # phone sitting in a browser.
    #
    # Ending up somewhere else is the one outcome this macro exists to
    # prevent, so it is the one outcome it must not call success.
    if _front_package() != package:
        raise RuntimeError(
            "the app would not come back to the front; it may be asking to "
            "sign in")
    report("macro.step.finished")


# ------------------------------------------------------- switching agencies
# HHAeXchange+ carries more than one agency on one account, and the round
# crosses between them twice a day. Walked live, and the path is four taps
# deep with nothing on the way that names itself usefully:
#
#   Menú (bottom bar)  →  Agencias (menu_screen_connections)
#     →  Cambiar proveedor activo (agency_configuration_screen_change_...)
#       →  the provider picker (OnboardingActivity)
#
# Two of those have resource ids, which is what makes this worth automating at
# all: the bottom bar's Menú does not, so it is found by its words.
UMA_MENU_WORDS = ("Menú", "Menu")
UMA_AGENCIES_ID = "menu_screen_connections"
UMA_CHANGE_ID = "agency_configuration_screen_change_connection_button"

# The word beside whichever provider is currently in use. Read off the live
# screen — the Agencias page marks the active one, which is how this can skip
# the whole walk when she is already where she wants to be.
UMA_ACTIVE_WORDS = ("Activa", "Active")


def _uma_agency(driver, report) -> None:
    """Open HHAeXchange+'s provider picker.

    Stops AT the picker rather than choosing: which agency is wanted is a
    fact about the visit she pressed, and `uma_agency_for` below is the
    version that knows one. This one is the plain "let me switch" control.
    """
    _walk_to_agency_picker(driver, report)
    report("macro.step.finished")


def _uma_agency_for(driver, report, agency: str) -> None:
    """Open HHAeXchange+ on a NAMED provider.

    What a visit row presses. The row knows which agency its patient belongs
    to — the schedule on the device says so — and pressing "Caridad" and then
    being asked which agency she is with is a question the page could have
    answered itself.

    THE ARGUMENT CANNOT NAME A CONTROL THAT IS NOT ON SCREEN. It is matched
    against the provider rows the app itself is drawing, by the same
    smallest-clickable-containing-the-words rule the sign-in walk uses. An
    agency that is not on the account finds nothing and this fails, which is
    the right outcome: better a macro that says it could not than one that
    presses the other provider.

    It also does nothing at all when that provider is already the active one.
    Switching to where you are costs a reload of the whole schedule, which is
    the slowest thing this app does.
    """
    from apt_log import feed as feed_mod

    wanted = (agency or "").strip()
    if not wanted:
        raise RuntimeError("no agency was named")

    if _already_on(driver, wanted):
        report("macro.step.finished")
        return

    _walk_to_agency_picker(driver, report)

    report("macro.step.navigating")
    # The picker lists providers by their full registered name, which is
    # longer and punctuated differently from what a schedule file calls them
    # ("Fatima Home Care" against "Fatima Home Care, Inc. (Fatima Home Care,
    # Inc.)"). The first couple of words are what they have in common.
    row = _words(driver, wanted, *wanted.split()[:2])
    if row is None:
        raise RuntimeError(f"{wanted} is not one of the providers on screen")
    row.click()
    # Deliberately NOT waited on. Choosing a provider starts a reload that
    # takes the better part of a minute and the activity swaps in five
    # seconds — a wait long enough to be honest here would outlast the
    # caller, and the portal's own spinner is what she is looking at.
    report("macro.step.finished")


def _already_on(driver, agency: str) -> bool:
    """Whether that provider is the active one, read off the Agencias page.

    Only answerable when that page happens to be in front — everywhere else
    this returns False and the walk runs, which costs a few taps and is the
    safe way round.
    """
    first = (agency.split() or [""])[0]
    for word in UMA_ACTIVE_WORDS:
        try:
            found = driver.find_elements(
                "xpath",
                f'//*[contains(@text,"{first}") or contains(@content-desc,"{first}")]'
                f'/following::*[contains(@text,"{word}")][1]')
        except Exception:  # noqa: BLE001
            continue
        if found:
            return True
    return False


def _walk_to_agency_picker(driver, report) -> None:
    from apt_log import feed as feed_mod

    if _front_package() != "com.hhaexchange.uma":
        driver.activate_app("com.hhaexchange.uma")
        if not wait_for(lambda: _front_package() == "com.hhaexchange.uma",
                        timeout=15.0):
            raise RuntimeError("HHAeXchange+ did not come to the front")

    report("macro.step.navigating")
    # Already there? The picker IS this app's other front page, and walking a
    # four-tap route to arrive where we started would be four chances to end
    # up somewhere else.
    if feed_mod.screen_for(feed_mod.current_focus() or "", _tree()) == "agency":
        return

    def press(finder, what):
        target = finder()
        if target is None:
            raise RuntimeError(f"could not find {what}")
        target.click()

    press(lambda: _words(driver, *UMA_MENU_WORDS), "the menu")
    time.sleep(BACK_SETTLE)
    press(lambda: _by_id(driver, UMA_AGENCIES_ID), "Agencias")
    time.sleep(BACK_SETTLE)
    press(lambda: _by_id(driver, UMA_CHANGE_ID), "the change-provider button")
    if not wait_for(
            lambda: feed_mod.screen_for(feed_mod.current_focus() or "",
                                        _tree()) == "agency",
            timeout=20.0):
        raise RuntimeError("the provider picker did not open")


def _by_id(driver, resource_id: str):
    try:
        found = [e for e in driver.find_elements(
            "xpath", f'//*[@resource-id="{resource_id}"'
                     f' or contains(@resource-id,":id/{resource_id}")]')
            if e.is_displayed()]
    except Exception:  # noqa: BLE001
        return None
    return found[0] if found else None


def _words(driver, *words):
    """The smallest clickable containing any of these words.

    The same rule `by_words` uses inside the sign-in walk and for the Play
    Store's Update button, and for the same reason: these apps hang captions
    on non-clickable children, and the screen's own root contains every word
    on it. Smallest is what makes it the control rather than the page.
    """
    best = None
    for word in words:
        try:
            hits = [e for e in driver.find_elements(
                "xpath",
                f'//*[@clickable="true"][contains(@text,"{word}")'
                f' or contains(@content-desc,"{word}")'
                f' or .//*[contains(@text,"{word}")'
                f' or contains(@content-desc,"{word}")]]')
                if e.is_displayed()]
        except Exception:  # noqa: BLE001
            continue
        for hit in hits:
            if best is None or _area(hit) < _area(best):
                best = hit
    return best


def _tree() -> str:
    """The published hierarchy, read from disk rather than from the phone.

    The feed writes one every time it reads one. Asking the device directly
    from here would either spawn a second `uiautomator dump` — which wedged
    the instrumentation hard enough to need a restart, twice — or open a
    second Appium session, and UiAutomator2 allows exactly one.
    """
    from apt_log import feed as feed_mod
    from apt_log.ui import state as state_mod

    try:
        return (state_mod.STATE_DIR / feed_mod.HIERARCHY_NAME).read_text(
            encoding="utf-8")
    except OSError:
        return ""


def _rescan(driver, report) -> None:
    """Throw away what the portal thinks this page looks like, and read it
    again.

    The front end serves a stitched whole-page document from a cache, so a
    page that changed in a way the tap machinery did not notice can be
    rendered from a copy that is no longer true. This is the button for "what
    I am looking at is not what the phone is showing" — the one state where
    the honest fix is to stop trusting the cache.
    """
    report("macro.step.clearing")
    _forget_stitched(_front_package())
    report("macro.step.reading")
    _stitch_walk(driver)


def _clear_screen(driver, report) -> None:
    """Get whatever is sitting over the app off the screen.

    The escape hatch for the case that produced it: the notification shade
    came down over inMyTeam and stayed for twenty minutes, because the
    command that closes it reports success and does nothing on this phone,
    and because the watchdog had been told the app was in front. The
    automatic collapse runs on a cooldown and gives up quietly; this is the
    same thing with a person behind it, on the page where the person is
    actually looking.

    Deliberately gentle and deliberately not a restart. Nothing is
    force-stopped, nothing is committed, no visit is touched — it dismisses
    the panel and brings the care app back to the front, which is what a
    person standing over the phone would do with a thumb.
    """
    from apt_log import feed as feed_mod

    report("macro.step.clearing")
    feed_mod.collapse_shade()

    # The shade is the case that produced this, but it is not the only panel
    # that can sit over the app — the volume dialog is the other one she can
    # raise by brushing a side button. The swipe above does nothing to that
    # one, and Back does. Sent only if something is STILL covering the screen,
    # so this never spends a Back on the app itself.
    if feed_mod.screen_is_covered(feed_mod.current_focus()):
        feed_mod._adb(["shell", "input", "keyevent", "KEYCODE_BACK"])
        time.sleep(0.6)

    package = _last_care_package()
    if not package:
        return
    if _front_package() == package:
        return
    report("macro.step.launching")
    feed_mod._adb(["shell", "monkey", "-p", package,
                   "-c", "android.intent.category.LAUNCHER", "1"])
    wait_for(lambda: _front_package() == package, timeout=15.0)


def _last_care_package() -> str:
    """The care app to come back to: the one in front if it is one, else the
    last one the watchdog saw. Never a guess at a package that is not ours."""
    from apt_log import feed as feed_mod

    front = _front_package()
    if front in feed_mod.CARE_APPS:
        return front
    return feed_mod.last_care_app()


# A required field on inMyTeam's plan of care is marked with a star in the
# left margin — the app draws it as a bullet, the tree reports the character.
# The whole rule for this macro comes from it: "if there is a star next to a
# task we must check mark it".
REQUIRED_MARKS = ("*", "•", "•")

# A task's own tick sits in the left margin beside its name. The OTHER column
# — "Patient refused" — carries an identical CheckBox two thirds of the way
# across, and ticking that one says the opposite of what this macro is for.
# The split is by position because position is the only thing that
# distinguishes them: same class, same size, no id, no caption, no
# description. Measured on the live screen at 720 wide, the task ticks sit at
# x=24 and the refusals at x=591.
TASK_TICK_MAX_X = 0.25


def starred_tasks(elements: list[dict], statics: list[dict],
                  width: int) -> list[dict]:
    """Every task tick that a star says is required and that is not yet on.

    Pairs by BASELINE: the star and the tick share a line, which is what
    "next to" means on this screen and what survives a density change. A star
    with no tick on its line is not a task at all — the instruction sentence
    at the top carries one, and so do both signature captions — and it
    quietly matches nothing, which is the behaviour that keeps this macro
    from wandering off the task list.

    Takes a parsed page rather than a driver so the front end can ask the
    same question of the screen it has already been published — one reading,
    used both to offer the button and to run it.
    """
    if not width:
        return []
    stars = [s for s in statics
             if (s.get("txt") or "").strip() in REQUIRED_MARKS]
    ticks = [e for e in elements
             if e.get("cls") == "CheckBox"
             and e.get("enabled", True) is not False
             and e["b"][2] <= width * TASK_TICK_MAX_X]
    wanted: list[dict] = []
    for star in stars:
        middle = (star["b"][1] + star["b"][3]) / 2
        mate = next((t for t in ticks
                     if t["b"][1] <= middle <= t["b"][3]), None)
        if mate is None or mate.get("checked"):
            continue
        if mate not in wanted:
            wanted.append(mate)
    return wanted


def _starred_tasks(driver) -> list[dict]:
    """`starred_tasks`, read off the live page."""
    from apt_log import feed as feed_mod

    src = driver.page_source or ""
    return starred_tasks(feed_mod.elements(src), feed_mod.statics(src),
                         (driver.get_window_size() or {}).get("width") or 0)


# HHAeXchange+ writes the same page a different way. Every task carries a
# pair of its own — "Se realizó" and "No realizado" — and names them, which
# inMyTeam does not: `poc_task_item_status_completed_false` beside
# `poc_task_item_status_refused_false`. The id even carries the state in its
# last word, which is what makes this readable without a star to pair against.
POC_DONE_ID = "poc_task_item_status_completed"
POC_REFUSED_ID = "poc_task_item_status_refused"


def poc_tasks(elements: list[dict]) -> list[dict]:
    """Every HHAeXchange+ care-plan task not yet marked done.

    The refused column is never a candidate: nothing whose id says `refused`
    is even looked at, and saying "the patient refused this" on her behalf is
    not a thing this macro will ever do.

    A task is a candidate only when BOTH readings of its state say off — the
    id's last word, and the selection state the app writes into the
    description ("no seleccionado …"). They come from different places and
    are parsed by different code, so if they ever disagree the task is left
    alone and she decides. A tap here toggles, so a wrong read does not
    merely fail to help: it would take a tick BACK OFF.
    """
    wanted: list[dict] = []
    for element in elements:
        rid = element.get("rid") or ""
        if not rid.startswith(POC_DONE_ID):
            continue
        if element.get("enabled", True) is False:
            continue
        if element.get("checked") or rid.endswith("_true"):
            continue
        wanted.append(element)
    return wanted


def _poc_tasks(driver) -> list[dict]:
    """`poc_tasks`, read off the live page."""
    from apt_log import feed as feed_mod

    return poc_tasks(feed_mod.elements(driver.page_source or ""))


def pending_tasks(elements: list[dict], statics: list[dict], width: int,
                  package: str) -> list[dict]:
    """The unticked tasks on whichever plan of care this page is.

    One button for her, two readings underneath: the apps mark a required
    task in ways that have nothing in common — inMyTeam with a star in the
    margin and an anonymous CheckBox, HHAeXchange+ with a named pair — and
    the package is the only thing that decides which to use.

    Pure, so the front end can ask it of the screen document it already has
    and offer the button only where there is something to press. A button
    that is always there and usually does nothing is a button pressed at the
    wrong moment, which is the argument this file already makes about the
    sign-in macros.
    """
    if package == "com.hhaexchange.uma":
        return poc_tasks(elements)
    return starred_tasks(elements, statics, width)


def _pending_tasks(driver) -> list[dict]:
    """`pending_tasks`, read off the live page."""
    from apt_log import feed as feed_mod

    src = driver.page_source or ""
    return pending_tasks(feed_mod.elements(src), feed_mod.statics(src),
                         (driver.get_window_size() or {}).get("width") or 0,
                         _front_package())


def _check_tasks(driver, report) -> None:
    """Tick every starred task on the plan of care.

    Thirteen tasks, each needing a tap in a 32px box, driven from Miami
    through a phone in another state — that is the clunkiness this removes.

    Both apps' plans of care, one button: inMyTeam marks a required task
    with a star in the margin, HHAeXchange+ with a named Se realizó /
    No realizado pair. See `_pending_tasks`.

    Deliberately only ADDS ticks. A tap on a checkbox toggles it, so running
    this over a half-filled list would undo the caregiver's own work; already
    ticked boxes are read from the tree and left alone. It never touches the
    "Patient refused" column, never presses Save or Check out, and never
    signs anything: what it does is the tedious half, and every consequential
    decision on this page stays hers.

    It sees what the phone is showing and nothing else. Both plans fit on one
    screen at this density; a longer one would leave tasks below the fold
    untouched AND unreported, since the read-back cannot see them either.
    Worth knowing before this is pointed at a plan nobody has counted.
    """
    report("macro.step.reading")
    pending = _pending_tasks(driver)
    if not pending:
        report("macro.step.nothing_to_check")
        return
    report("macro.step.checking")
    for tick in pending:
        x = (tick["b"][0] + tick["b"][2]) // 2
        y = (tick["b"][1] + tick["b"][3]) // 2
        _tap_xy(x, y)
        time.sleep(0.35)
    # Read the page back rather than trusting the taps: a box that did not
    # take is the failure worth naming, because a plan of care submitted a
    # tick short is rejected by the agency and she finds out hours later.
    left = _pending_tasks(driver)
    if left:
        raise RuntimeError(
            f"{len(left)} of {len(pending)} tasks did not tick")


# ---------------------------------------------------------------- EVV entry
#
# THE ONE PLACE IN THIS PROJECT THAT WRITES A RECORD ABOUT A PERSON.
#
# Everything else here reads a screen or moves between them. These two press
# the control that tells an agency a caregiver arrived at a patient's home at
# a particular minute, and they run from a timer rather than from a finger.
# What makes that acceptable is REQ-5.9: somebody armed this block in advance
# and that arming is an attestation of presence, recorded with their name.
#
# The patient's name arrives as the macro's ARGUMENT and is never logged, never
# put in an exception message, and never written to a status file — `execute`
# already sends `type(exc).__name__` rather than the message for exactly this
# reason, and these keep that true by not putting a name in one.

# The word on the control that starts a visit, per app. Matched as a phrase,
# not a fragment: on Mobile Caregiver+ the immediate left-hand neighbour of
# "Comenzar Visita" is "Cancelar Visita", and cancelling somebody's visit by
# a loose match is the failure this whole file is written to avoid.
EVV_ENTRY_WORDS = {
    "com.tellus.evv.v2": ("Comenzar Visita", "Comenzar visita"),
    "com.inmyteam.inmyteam": ("Check in",),
    "com.hhaexchange.uma": ("Registro de entrada de EVV",),
}

# What the screen says once the entry has landed. Read back rather than
# trusting the tap (REQ-4 verify-after-acting), and this app makes it easy:
# the real start time appears and the controls go away.
EVV_STARTED_WORDS = {
    "com.tellus.evv.v2": ("Hora de inicio real", "Servicio Completada"),
    "com.inmyteam.inmyteam": ("Check out",),
    # The button flips from entrada to salida, and the banner names the
    # minute the call went in — either is proof the record was written.
    "com.hhaexchange.uma": ("Registro de salida de EVV",
                            "pendiente de aprobaci"),
}

# inMyTeam refuses any visit that is not today's, in as many words, and draws
# no control at all. Seeing this is not a failure of navigation — it is the
# app telling us the visit is not actionable.
NOT_TODAY_WORDS = ("not scheduled for today", "no está programada para hoy")

# WHAT THE APP SAYS WHEN THE MINUTE IS WRONG, and it is worth knowing that
# asking costs something: watched live, a check-in pressed outside the visit's
# window put "Failed  Check in 11:55 PM" on the patient's own record and
# raised "Warning! / Invalid time". So a premature press is not a harmless
# no-op — it writes a failure onto somebody's chart.
#
# The guards above are what stop that happening from a timer: the visit is
# only reached inside its own window, and the not-today check runs before
# anything is pressed. This pair is the backstop, so a refusal is REPORTED as
# a refusal rather than timing out as "the screen did not confirm".
INVALID_TIME_WORDS = ("invalid time", "hora inválida", "hora invalida")

EVV_SETTLE = 1.2


# WHAT A FIRE NOTICE MAY SAY, WHICH IS ALMOST NOTHING.
#
# These land on a lock screen and travel through a public relay, so they carry
# no patient, no visit, no time and no app — the same rule the code notice
# already follows. The sentence's whole job is to get somebody to open the
# portal, where the details are behind the tailnet and a login.
#
# Only failures are announced. A check-in that worked is the machine doing its
# job, and a phone that buzzes for every routine success is a phone somebody
# turns off before the one that matters.
FIRE_FAILED = "A scheduled check-in did not complete. Open the portal."
FIRE_MISSED = "A scheduled check-in was missed and needs doing by hand."


def _tell_somebody_about_the_fire(item, outcome: str) -> None:
    """Alert on a fire that did not land. Never raises — see notify."""
    if outcome == "done":
        return
    sentence = FIRE_MISSED if outcome == "missed" else FIRE_FAILED
    try:
        from apt_log import push

        push.send(PUSH_TITLE, sentence, url=PORTAL_URL, tag="evv")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not push the fire notice (%s)", exc)
    try:
        from apt_log import notify

        notify.send(sentence, url=PORTAL_URL)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not alert about the fire (%s)", exc)


# ------------------------------------------------------ the location prompt
#
# THE FIRST CHECK-IN LANDS ON ANDROID'S LOCATION DIALOG, NOT ON THE APP.
#
# Watched live on the first attempt: `Check in` opened
# "Allow Inmyteam to access this device's location?" with Precise/Approximate
# above and three buttons below. That is Android's own dialog, drawn over the
# app by `com.google.android.permissioncontroller` — the visit never checked
# in, and a fire that met this at 5am would have burned its slot on a prompt.
#
# TWO ANSWERS, AND THE FIRST ONE IS THE REAL FIX. The permission is granted
# ahead of time, so the dialog does not appear at all; `pm grant` does exactly
# what tapping the dialog does and needs no screen. The second answer is for
# the day a factory reset or an app update revokes it anyway: the macro
# recognises the dialog and answers it rather than timing out against a
# control that is not there.
PERMISSION_PKG = "com.google.android.permissioncontroller"

# EVV is a location claim, so these apps genuinely need the fix — this is not
# a permission being widened for convenience. Foreground and background both:
# the owner asked for "Always", and a check-in fired by a timer is not always
# a check-in with the app in front.
LOCATION_PERMS = ("android.permission.ACCESS_FINE_LOCATION",
                  "android.permission.ACCESS_COARSE_LOCATION",
                  "android.permission.ACCESS_BACKGROUND_LOCATION")

# What the dialog's buttons say. "While using the app" is what a foreground
# check-in needs and is the only affirmative this will press: `Only this time`
# would put the same dialog in front of tomorrow's fire, and `Don't allow`
# would poison the app's own EVV.
ALLOW_WORDS = ("While using the app", "Mientras se usa la app",
               "Mientras uso la app", "Allow only while using the app")
PRECISE_WORDS = ("Precise", "Precisa", "Precisión")


def _declared_permissions(package: str, serial: str | None = None) -> set:
    """What this package asks for in its manifest, or an empty set.

    Empty means the read failed, and an empty set must not be read as "it
    wants nothing" — the caller treats it as "no idea, try them all", which
    is the behaviour this replaced.
    """
    from apt_log import feed as feed_mod

    try:
        out = feed_mod._adb(["shell", "dumpsys", "package", package], serial,
                            timeout=20.0)
        if out.returncode != 0:
            return set()
        text = out.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(re.findall(r"android\.permission\.[A-Z_]+", text))


def grant_location(package: str, serial: str | None = None) -> dict:
    """Grant this app the location permissions, without any screen.

    Returns what each one ended up as, so a caller can say what happened
    rather than assuming. BACKGROUND MAY REFUSE and that is not a failure:
    on Android 11+ `ACCESS_BACKGROUND_LOCATION` is not always grantable this
    way, and foreground alone is enough for a check-in pressed while the app
    is in front — which is what the fire does.
    """
    from apt_log import feed as feed_mod

    # ONLY WHAT THE APP ACTUALLY ASKS FOR. Granting a permission a package
    # never declared throws a SecurityException the length of a stack trace,
    # and the first armed fire put one in the log for
    # ACCESS_BACKGROUND_LOCATION — which inMyTeam does not request. Harmless,
    # and it buried the line that mattered. What the package declares is a
    # cheap thing to read first.
    wanted = _declared_permissions(package, serial)
    out = {}
    for perm in LOCATION_PERMS:
        if wanted and perm not in wanted:
            out[perm.rsplit(".", 1)[-1]] = "not requested by the app"
            continue
        try:
            done = feed_mod._adb(["shell", "pm", "grant", package, perm],
                                 serial)
            out[perm.rsplit(".", 1)[-1]] = (
                "granted" if done.returncode == 0
                else (done.stderr or b"").decode("utf-8", "replace").strip()
                or "refused")
        except (OSError, subprocess.SubprocessError) as exc:
            out[perm.rsplit(".", 1)[-1]] = type(exc).__name__
    log.info("location for %s: %s", package,
             ", ".join(f"{k}={v}" for k, v in out.items()))
    return out


def _answer_the_permission_dialog(driver, report) -> bool:
    """If Android's permission dialog is up, allow and carry on.

    Returns whether it did anything. Only ever presses the
    while-using-the-app affirmative — see ALLOW_WORDS for why the other two
    are not options this may take on somebody's behalf.
    """
    if _front_package() != PERMISSION_PKG:
        return False
    report("macro.step.allowing_location")
    # Precise, where the dialog offers the choice: an EVV record built on a
    # coarse fix is a worse record, and this app is asking because it intends
    # to attach the position to a visit.
    precise = _words(driver, *PRECISE_WORDS)
    if precise is not None:
        try:
            precise.click()
            time.sleep(0.4)
        except Exception:  # noqa: BLE001 — the choice is optional
            log.debug("could not pick a precise fix", exc_info=True)
    allow = _words(driver, *ALLOW_WORDS)
    if allow is None:
        raise RuntimeError("the location prompt has no allow button on it")
    allow.click()
    time.sleep(EVV_SETTLE)
    if _front_package() == PERMISSION_PKG:
        # A second page of the same dialog, or it did not take. Either way
        # this is not a thing to keep tapping at.
        raise RuntimeError("the location prompt did not go away")
    return True


def _evv_arg(app: str, patient: str, at: str = "") -> str:
    """One string carrying the parts, because a macro takes one argument.

    JSON rather than a delimiter: a patient's name is arbitrary text and
    choosing a separator it cannot contain is a guess about somebody's name.

    `at` is the block's own start, "HH:MM", and it is not decoration: one
    patient can have TWO cards on the same evening, each with its own
    check-in button, and pressing the wrong one records the wrong half of
    the visit. Optional because two of the three apps show one card per
    patient per day and have never needed it.
    """
    doc = {"app": app, "patient": patient}
    if at:
        doc["at"] = at
    return json.dumps(doc)


def _evv_when(arg: str) -> str:
    """The block's start from a macro argument, "HH:MM", or "" when unsaid."""
    try:
        doc = json.loads((arg or "").strip())
    except (json.JSONDecodeError, AttributeError):
        return ""
    return str(doc.get("at") or "") if isinstance(doc, dict) else ""


def _evv_parts(arg: str) -> tuple[str, str]:
    """(package, patient) from a macro argument, or a refusal.

    Accepts a bare name too, for a press from the portal where the app in
    front is the app meant.
    """
    raw = (arg or "").strip()
    if not raw:
        raise RuntimeError("no patient was named")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return _front_package(), raw
    if not isinstance(doc, dict):
        raise RuntimeError("that is not a visit")
    patient = str(doc.get("patient") or "").strip()
    if not patient:
        raise RuntimeError("no patient was named")
    return str(doc.get("app") or "") or _front_package(), patient


def _bring_up(driver, package: str) -> None:
    """The app in front, or a refusal. Never presses anything on it."""
    if driver.current_package == package:
        return
    wake_display()
    driver.activate_app(package)
    if not wait_for(lambda: driver.current_package == package, timeout=15.0):
        raise RuntimeError("the app did not come to the front")
    time.sleep(EVV_SETTLE)


# HOW LONG A COLD START COUNTS AS FRESH.
#
# Long enough that a lead window at 04:45 spares the 05:00 fire a second
# restart, short enough that an app process alive since yesterday never
# qualifies. The number is a ceiling on how stale the list may be at the
# moment the entry is pressed, and forty-five minutes is the lead window
# plus room for it to have run late.
FRESH_FOR = 45 * 60.0

# When each package last came up from cold, by this process's clock. Process
# local on purpose: the Runner is one long-lived process, so the lead window
# and the fire share it, and a Runner that restarted in between has no
# business claiming a fetch it did not watch.
_freshened: dict[str, float] = {}


def _freshen(driver, report, package: str, max_age: float = 0.0) -> bool:
    """Make the app fetch today from its server, and say whether it had to.

    `_bring_up` returns the moment the right package is in front, and an app
    that has been in front since yesterday is still in front today. It keeps
    the list it fetched then — Android had no reason to kill it and the app
    had no reason to ask again — so the walk can arrive at a correctly-named
    screen showing the wrong day's visits, and every check after it is a
    check against stale data.

    A force-stop is the only lever this code has that an app cannot ignore.
    Pressing the app's own Refresh is the polite version and it is not
    enough: it re-renders whatever the app already holds, and on inMyTeam it
    was pressed twice against a visit whose record was genuinely absent and
    changed nothing either time. Killing the process removes the question.

    Relaunched through the launcher intent rather than the driver, for the
    reason `_restart_app` gives: after a force-stop the driver's handle on
    the app is stale, and asking it to activate the app it has just lost is
    how a recovery hangs.

    `max_age` of zero means always. Anything else means "only if the last
    cold start is older than this", which is what lets the fire skip the
    cost when the lead window has already paid it.
    """
    from apt_log import feed as feed_mod

    if max_age and (time.time() - _freshened.get(package, 0.0)) < max_age:
        return False
    report("macro.step.freshening")
    _force_stop(package)
    _forget_stitched(package)
    time.sleep(1.5)
    wake_display()
    feed_mod._adb(["shell", "monkey", "-p", package,
                   "-c", "android.intent.category.LAUNCHER", "1"])
    if not wait_for(lambda: _front_package() == package, timeout=25.0):
        raise RuntimeError("the app did not come back")
    time.sleep(EVV_SETTLE)
    _freshened[package] = time.time()
    return True


# Where today's visits actually live, per app.
#
# Mobile Caregiver+ lands on its week list, so the row is on the screen the
# app opens to. inMyTeam lands on a page of BUCKETS — Today, Tomorrow, Next,
# Past, each a count — and the rows are one tap further in.
EVV_TODAY_WORDS = {
    "com.inmyteam.inmyteam": ("Today", "Hoy"),
    "com.tellus.evv.v2": (),
}


def _find_visit_row(driver, report, package: str, patient: str):
    """The patient's row, walking to the list if we are not already on it.

    THE APP IS WHEREVER IT WAS LAST LEFT. Android keeps app state, and
    `_bring_up` returns the moment the right package is in front — it does
    not care which of its screens that is. So the first armed fire looked for
    Carmen's row on the plan-of-care sheet the app had been sitting on since
    the night before, found nothing, and failed with "that visit is not on
    this screen". Both the lead-window walk and the fire itself, for the same
    reason.

    Three tries, cheapest first: where we are (she may already be on the
    list), then the app's own home, then the bucket that holds today. Nothing
    here presses anything consequential — every step is navigation.
    """
    row = _row_for(driver, patient)
    if row is not None:
        return row
    return _row_from_the_list(driver, report, package, patient)


def _row_from_the_list(driver, report, package: str, patient: str):
    """The patient's row, reached by walking to the app's own list."""
    report("macro.step.to_the_list")
    _app_home(driver, report)
    time.sleep(EVV_SETTLE)
    row = _row_for(driver, patient)
    if row is not None:
        return row
    for word in EVV_TODAY_WORDS.get(package, ()):
        bucket = _words(driver, word)
        if bucket is None:
            continue
        bucket.click()
        time.sleep(EVV_SETTLE)
        break
    return _row_for(driver, patient)


def _says_not_today(driver) -> bool:
    """Whether the open visit is one the app says is not today's."""
    page = (driver.page_source or "").lower()
    return any(w in page for w in NOT_TODAY_WORDS)


def _open_todays_visit(driver, report, package: str, patient: str) -> None:
    """Open the patient's visit FOR TODAY, or refuse.

    THE NAME ON A VISIT DETAIL IS NOT A LIST ROW, and this is the trap that
    caught the first live test of the walk. inMyTeam restores the screen it
    was last on, so switching to it reopened a visit from the previous day —
    and the patient's name is printed on that page, so the row finder matched
    it there and reported success without ever reaching a list. It had opened
    the wrong occurrence, on the wrong day, and called it found.

    Nothing would have been written: the check-in step refuses on the app's
    own "not scheduled for today". But a walk that lands on the wrong day and
    says it succeeded is a walk that cannot be trusted to have landed on the
    right one either, so the day is now checked HERE, once, for both callers.

    A visit that is not today's sends it back to the list to look properly.
    """
    row = _find_visit_row(driver, report, package, patient)
    if row is None:
        raise RuntimeError("that visit is not on this screen")
    row.click()
    time.sleep(EVV_SETTLE)
    _answer_the_permission_dialog(driver, report)
    if not _says_not_today(driver):
        return
    # Wrong day. Go to the list properly and take the row from there.
    row = _row_from_the_list(driver, report, package, patient)
    if row is None:
        raise RuntimeError("that visit is not on this screen")
    row.click()
    time.sleep(EVV_SETTLE)
    _answer_the_permission_dialog(driver, report)
    if _says_not_today(driver):
        raise RuntimeError("the app says this visit is not today's")


def _row_for(driver, patient: str):
    """The visit row for this patient, as the smallest clickable holding it.

    Both apps put the patient's name inside the row rather than on it —
    Mobile Caregiver+ in the row's `content-desc`, inMyTeam in a child of the
    card — so this is the same smallest-enclosing-clickable rule the rest of
    the file uses. Located by NAME and never by index: REQ-4 forbids reaching
    a patient by position, and both apps' row ids are positions.
    """
    return _words(driver, patient)


# THE ONE SCREEN IN inMyTeam THAT KNOWS WHETHER A VISIT HAPPENED.
#
# Three of its screens answer that question and only this one is right. The
# Visit Detail's "Your activity on this patient" is a PER-DEVICE log: on
# 2026-08-21 it went on reading "No check in and check out data has been
# recorded" through the app's own Refresh and through a force-stop and cold
# relaunch, on a visit the caregiver had checked in and out from her own
# handset that morning — while `My Work` -> Checks, same app, same account,
# same minute, listed all four events for the day. A device that has never
# checked a patient in reports that nothing was ever recorded, however much
# was. The list card knows too, but draws it as two Compose check marks with
# no node, no id and no content-desc: pixels, unreadable.
#
# These lines are ordinary TextViews. That is the whole reason this walk
# exists rather than a screenshot classifier.
MY_WORK_WORDS = ("My Work", "Mi trabajo")
CHECKS_TAB_WORDS = ("Checks", "Registros")
SEARCH_WORDS = ("SEARCH", "Search", "BUSCAR", "Buscar")
CHECK_EVENT_WORDS = ("check in", "check out", "entrada", "salida")
CHECK_IN_WORDS_SEEN = ("check in", "entrada")

# Which apps can be asked this question at all. Empty for the rest, and an
# app that cannot be asked is NOT thereby cleared — see `_already_entered`.
CHECK_LOG_APPS = ("com.inmyteam.inmyteam",)


DRAWER_DESC = "Open navigation drawer"
BACKS_TO_DRAWER = 5

# Fragments every Jetpack app carries, which say nothing about where the
# person is.
PLUMBING_FRAGMENTS = ("ReportFragment", "NavHostFragment",
                      "SupportRequestManagerFragment")
_FRAGMENT = re.compile(r"#\d+: ([A-Za-z]\w*Fragment)\{")


def _where_in_app(package: str) -> str:
    """The app's current screen, by the name the app's own code gives it.

    THE ANSWER TO A QUESTION THE ATLAS CANNOT ANSWER. `feed.screen_for` keys
    on the activity, and inMyTeam has exactly one: MainActivity is the visits
    hub, every bucket list, the visit detail and the work log alike. So the
    atlas says "home" wherever it is standing, `_app_home` returns having
    pressed nothing, and every walk through this app has had to infer its
    position from whichever words happened to be on the glass.

    `dumpsys activity` publishes the fragment back stack by class name, and
    that name does change: the visits hub reports `VisitsFragment`, the work
    log reports `MyWorksFragment`. Read-only, no gesture, nothing to get
    wrong — and unlike a map of the app it cannot go stale, because it is the
    app answering rather than us remembering.

    "" when the phone will not say. Callers must treat that as "no idea",
    never as "not there".
    """
    from apt_log import feed as feed_mod

    try:
        out = feed_mod._adb(["shell", "dumpsys", "activity", package]
                            ).stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    seen = [n for n in _FRAGMENT.findall(out) if n not in PLUMBING_FRAGMENTS]
    return seen[0] if seen else ""


def _the_drawer(driver):
    """inMyTeam's nav-drawer button, or None — and the only honest "is this
    the app's front page" test it has.

    `_app_home` asks the atlas, and this app's atlas names ONE activity:
    MainActivity is the visits hub, every bucket list and the visit detail
    alike, so "home" is true wherever it is standing and the walk returns
    having pressed nothing. The corner control is what actually differs —
    the hub draws this drawer, an inner page draws Back in the same place.
    The first live run of the work-log walk failed on exactly that, from a
    visit detail, reporting the log unreachable when it was three Backs away.
    """
    try:
        found = [e for e in driver.find_elements(
            "xpath", f'//*[@content-desc="{DRAWER_DESC}"]')
            if e.is_displayed()]
    except Exception:  # noqa: BLE001
        return None
    return found[0] if found else None


def _open_my_work(driver, report) -> bool:
    """The `My Work` screen, reached from wherever the app is standing.

    Its route is the nav drawer, and the drawer is only on the app's front
    page — so this presses Back until the drawer appears, and never presses
    it again once the app is no longer in front.
    """
    package = driver.current_package
    drawer = _back_to_the_drawer(driver, report, package)
    if drawer is None:
        # LOST. Rather than press Back a sixth time into whatever is under
        # the app, start it from cold — which is the one position this code
        # can be certain of — and walk again from there.
        report("macro.step.freshening")
        _freshen(driver, report, package)
        drawer = _back_to_the_drawer(driver, report, package)
    if drawer is None:
        return False
    drawer.click()
    time.sleep(EVV_SETTLE)
    item = _words(driver, *MY_WORK_WORDS)
    if item is None:
        return False
    item.click()
    time.sleep(EVV_SETTLE)
    # ASK THE APP WHERE IT LANDED rather than reading the glass. A Search
    # button is on more than one of its screens; `MyWorksFragment` is on
    # exactly one. Only when the phone will not say at all does this fall
    # back to looking for the control.
    where = _where_in_app(package)
    if where:
        return where.lower().startswith("mywork")
    return bool(_words(driver, *SEARCH_WORDS))


def _back_to_the_drawer(driver, report, package: str):
    """Press Back until the app's front page is showing, and hand back its
    drawer button. None if it never got there."""
    from apt_log import device as device_mod

    drawer = _the_drawer(driver)
    for _ in range(BACKS_TO_DRAWER):
        if drawer is not None:
            return drawer
        report("macro.step.navigating")
        device_mod.send_ui_action("back")
        time.sleep(BACK_SETTLE)
        if _front_package() != package:
            # Back popped the task stack and left the app. Undo it and stop:
            # a second press from here would leave a second time.
            _bring_up(driver, package)
            return _the_drawer(driver)
        drawer = _the_drawer(driver)
    return drawer


def _todays_check_events(driver, report, patient: str) -> list[str]:
    """Every check event this account holds for `patient` today, in words.

    Returns the lines as the app writes them — "Check in 05:00 AM" — or an
    empty list when the day is genuinely clear.

    RAISES rather than returning empty when it cannot get a straight answer.
    "I could not look" and "there is nothing there" are the same shape and
    opposite meanings, and the caller is about to decide whether to touch a
    live agency record on the strength of it.
    """
    from apt_log import feed as feed_mod

    package = driver.current_package
    if not _open_my_work(driver, report):
        raise RuntimeError("the work log is not reachable from here")

    # THE RANGE HAS TO SAY TODAY, and it is read rather than trusted: a range
    # left over from another search answers a different question in exactly
    # the same words, and the answer decides whether a live agency record
    # gets touched.
    #
    # Nothing is typed into those two fields. They are picker-backed, and the
    # value this walk wants is the one the app puts there itself — so when
    # the range is wrong the fix is to start the app from cold, which is what
    # restores its defaults, and walk in again. Once. Still wrong after that
    # is a refusal, not a third attempt.
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    if today not in (driver.page_source or ""):
        report("macro.step.freshening")
        _freshen(driver, report, package)
        if not _open_my_work(driver, report):
            raise RuntimeError("the work log is not reachable from here")
    if today not in (driver.page_source or ""):
        raise RuntimeError("the work log is not showing today")

    # Checks BEFORE Search. The Visits tab returns nothing for a day whose
    # Checks tab has four events, so a search run on the wrong tab comes back
    # empty and looks exactly like a clear day.
    tab = _words(driver, *CHECKS_TAB_WORDS)
    if tab is None:
        raise RuntimeError("the work log has no checks tab")
    tab.click()
    time.sleep(EVV_SETTLE)
    go = _words(driver, *SEARCH_WORDS)
    if go is None:
        raise RuntimeError("the work log has no search")
    go.click()
    time.sleep(EVV_SETTLE * 2)

    rows = feed_mod.statics(driver.page_source or "")
    rows.sort(key=lambda s: (s["b"][1], s["b"][0]))
    words = [(s.get("txt") or "").strip() for s in rows]
    if not any(patient.lower() in w.lower() for w in words):
        return []
    # Everything under the patient's own line, up to the next patient. One
    # search can list several.
    start = next(i for i, w in enumerate(words)
                 if patient.lower() in w.lower())
    out = []
    for w in words[start + 1:]:
        low = w.lower()
        if any(v in low for v in CHECK_EVENT_WORDS):
            out.append(w)
        elif out:
            break
    return out


def _already_entered(driver, report, package: str, patient: str) -> str:
    """The check-in this patient already has for today, or "".

    An app whose log has not been walked answers "" — it has no way to know
    and says so by saying nothing. That is deliberate and it is the reason
    this returns a string rather than a bool: the caller must not read
    "no evidence of an entry" as "no entry", and the only app that can
    currently give evidence either way is named in `CHECK_LOG_APPS`.
    """
    if package not in CHECK_LOG_APPS:
        return ""
    report("macro.step.reading_the_log")
    seen = _todays_check_events(driver, report, patient)
    return next((w for w in seen
                 if any(v in w.lower() for v in CHECK_IN_WORDS_SEEN)), "")


def _evv_checks(driver, report, arg: str) -> None:
    """Show the day's check-in and check-out record for one patient.

    THE SCREEN NOBODY COULD FIND. Its route is four presses deep behind a nav
    drawer, a tab that is not the one that opens, and a Search that must be
    pressed after the tab rather than before — and getting any of that wrong
    returns an empty list that looks exactly like a day with no work on it.
    Reaching it by hand, reliably, while an entry is being watched, is not a
    reasonable thing to ask of anybody.

    It presses nothing consequential: a date search is reading.
    """
    package, patient = _evv_parts(arg) if arg else _the_visit_in_hand()
    if package not in CHECK_LOG_APPS:
        raise RuntimeError("this app's work log is not walked")
    report("macro.step.launching")
    _bring_up(driver, package)
    report("macro.step.reading_the_log")
    seen = _todays_check_events(driver, report, patient)
    report("macro.step.finished" if seen else "macro.step.nothing_recorded")


# How far down the day to look for a visit this button can actually answer
# for. Long enough to cross the visits on apps whose logs are not walked,
# short enough that the answer is still about today rather than next week.
VISITS_TO_LOOK_DOWN = 8


def _the_visit_in_hand() -> tuple[str, str]:
    """The visit this button is about when nobody said — the one running, or
    the soonest one whose record can actually be read.

    WHAT MAKES IT A BUTTON RATHER THAN A ROUTE. Watching an armed entry means
    watching ONE visit, the one whose minute is arriving, and asking the
    person watching to name it is asking them to know which patient the
    scheduler picked. The schedule already knows.

    IT SKIPS PAST WHAT IT CANNOT ANSWER FOR. Only inMyTeam has a walked work
    log, and the very next visit on the clock is often on another app — so
    taking "next" literally made the button refuse for most of the day, in
    words that sound like a fault. The screen it lands on names the patient
    and the date, so which record is on show is never in doubt.
    """
    from datetime import datetime
    from apt_log import schedule as schedule_mod

    try:
        plan = schedule_mod.load()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("there is no schedule to read") from exc
    now = datetime.now(plan.zone)
    running = plan.current(now)
    ahead = ([running] if running else []) + list(
        plan.upcoming(now, limit=VISITS_TO_LOOK_DOWN))
    if not ahead:
        raise RuntimeError("there is no visit to look up")
    visit = next((v for v in ahead if v.app in CHECK_LOG_APPS), ahead[0])
    return visit.app, visit.patient


# HHAeXchange+ PUTS ITS CHECK-IN ON THE LANDING SCREEN.
#
# Walked live on 2026-08-21 at 20:05, four minutes before a real visit, with
# the owner watching. No agency has to be chosen — confirmed by the owner for
# every patient in this app, "regardless of agency selected" — and no visit
# detail has to be opened. Programación lists today's visits already
# expanded, each with a full-width `Registro de entrada de EVV`. Pressing it
# opens `Verificación electrónica de visitas` with a GPS map and a single
# `Continuar`, and pressing that writes the record: the visit screen then
# says "Llamada EVV en H:MM p. m. pendiente de aprobación de la oficina" and
# the button becomes `Registro de salida de EVV`.
#
# TWO CARDS CAN CARRY THE SAME NAME. A patient whose evening is written as
# two entries has two cards, each with its own button, and pressing the wrong
# one records the wrong half. They are told apart by the hours printed on the
# card — which are the AGENCY's window, not ours: the cards say 8:00-9:00 and
# 9:00-10:00 where the schedule says 8:05 and 9:05. So the match is nearest,
# not equal, and a card further off than this is not the visit we meant.
UMA_CARD_TOLERANCE = timedelta(minutes=20)
UMA_CLOCK_IN_ID = "clock_in"
UMA_GPS_CONTINUE_ID = "gps_continue"
UMA_GPS_WORDS = ("Verificación electrónica", "Verificacion electronica")


def _uma_cards(driver, patient: str) -> list[dict]:
    """Every check-in button on the schedule that belongs to this patient,
    each with the window printed on its card.

    `[{"button": el, "at": time|None, "says": "8:00 p. m. - 9:00 p. m."}]`,
    in the order they sit on the screen.
    """
    from apt_log import feed as feed_mod

    source = driver.page_source or ""
    els = feed_mod.elements(source)
    rows = feed_mod.statics(source)
    buttons = [e for e in els if UMA_CLOCK_IN_ID in (e.get("rid") or "")
               and int(e.get("step") or 0) == 0]
    buttons.sort(key=lambda e: e["b"][1])

    named = [r for r in rows
             if patient.lower() in (r.get("txt") or "").lower()]
    out = []
    for button in buttons:
        top = button["b"][1]
        # The card's own name and hours are the nearest of each ABOVE the
        # button — the layout is name, hours, details, button.
        mine = [r for r in named if r["b"][3] <= top]
        if not mine:
            continue
        mine.sort(key=lambda r: top - r["b"][3])
        if top - mine[0]["b"][3] > 400:
            continue                    # a different card's name entirely
        hours = [r for r in rows
                 if r["b"][3] <= top and _uma_start(r.get("txt") or "")]
        hours.sort(key=lambda r: top - r["b"][3])
        says = hours[0]["txt"] if hours else ""
        out.append({"button": button, "at": _uma_start(says), "says": says})
    return out


_UMA_HOUR = re.compile(r"(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?", re.I)


def _uma_start(text: str):
    """The first clock reading in a card's hours line, as a `time`, or None."""
    found = _UMA_HOUR.search(text or "")
    if not found:
        return None
    hour, minute, half = int(found.group(1)), int(found.group(2)), found.group(3).lower()
    if half == "p" and hour != 12:
        hour += 12
    if half == "a" and hour == 12:
        hour = 0
    return dtime(hour % 24, minute)


def _uma_pick(cards: list[dict], want: str) -> dict:
    """The card for the block starting at `want` ("HH:MM"), or a refusal.

    Nearest rather than equal, because the card prints the agency's window
    and the schedule keeps the caregiver's — five minutes apart by design.
    Refuses on a tie or on nothing close enough: a check-in on the wrong
    half is worse than one not made.
    """
    if not cards:
        raise RuntimeError("that visit is not on this screen")
    if len(cards) == 1:
        return cards[0]
    if not want:
        raise RuntimeError("two visits for this patient and no time to tell "
                           "them apart")
    hh, _, mm = want.partition(":")
    target = dtime(int(hh), int(mm))
    def far(card):
        if card["at"] is None:
            return timedelta(days=1)
        a = timedelta(hours=target.hour, minutes=target.minute)
        b = timedelta(hours=card["at"].hour, minutes=card["at"].minute)
        return abs(a - b)
    ranked = sorted(cards, key=far)
    if far(ranked[0]) > UMA_CARD_TOLERANCE:
        raise RuntimeError("no card on this screen matches that visit's time")
    if len(ranked) > 1 and far(ranked[1]) == far(ranked[0]):
        raise RuntimeError("two cards match that time equally well")
    return ranked[0]


def _uma_entry(driver, report, patient: str, want: str) -> None:
    """Press this app's check-in for one visit, through the GPS screen.

    Presses exactly twice and verifies after both: the card's own button,
    then `Continuar` on the map. Anything it cannot identify is a refusal
    rather than a guess, because every press here writes to a live agency
    record.
    """
    report("macro.step.finding_patient")
    card = _uma_pick(_uma_cards(driver, patient), want)
    log.info("uma card chosen: %s", card["says"] or "(no hours printed)")

    report("macro.step.checking_in")
    _tap_element(driver, card["button"])
    if not wait_for(lambda: any(w in (driver.page_source or "")
                                for w in UMA_GPS_WORDS), timeout=20.0):
        raise RuntimeError("the verification screen did not open")
    _answer_the_permission_dialog(driver, report)

    # GPS, always — the owner's standing instruction. The other tab is a FOB
    # reader this project has no way to hold.
    from apt_log import feed as feed_mod
    here = [e for e in feed_mod.elements(driver.page_source or "")
            if UMA_GPS_CONTINUE_ID in (e.get("rid") or "")
            and int(e.get("step") or 0) == 0]
    if len(here) != 1:
        # A stitched page repeats a FIXED footer at every step it was
        # captured in, and this map does not scroll. Step 0 is the copy on
        # the glass; anything else and we do not know what we would press.
        raise RuntimeError("the continue button is not where it should be")
    _tap_element(driver, here[0])


def _tap_element(driver, element) -> None:
    """Touch the centre of an element read out of the tree."""
    x1, y1, x2, y2 = element["b"]
    driver.tap([((x1 + x2) // 2, (y1 + y2) // 2)])
    time.sleep(EVV_SETTLE)


def _evv_entry(driver, report, arg: str) -> None:
    """Press the app's own check-in for one patient, and verify it landed.

    `arg` is the patient's name as the schedule spells it.

    Refuses rather than guesses, at every step: an app that is not in front,
    a patient whose row is not on the screen, a visit the app says is not
    today's, a control that is not drawn, and a screen that does not confirm
    afterwards are all failures, and each leaves the visit for a person. The
    scheduler has already claimed the occurrence by the time this runs, so a
    failure here means the entry is made by hand — never retried into a record
    claiming a later arrival than the truth (REQ-5.5).
    """
    from apt_log import autoentry

    package, patient = _evv_parts(arg)
    why = autoentry.refusal(package, "entry")
    if why:
        # Named rather than attempted. HHAeXchange+'s control has only ever
        # been seen on a visit already under way; pressing an unknown button
        # on a live agency record to find out what it does is not a thing to
        # do.
        raise RuntimeError(f"this app's entry is not walked ({why})")

    # BEFORE THE APP IS EVEN OPENED. A dialog answered here is a dialog that
    # never interrupts the press, and this costs nothing when the permission
    # is already held.
    grant_location(package)

    report("macro.step.launching")
    # Cold-start only if nothing already has. When the lead window ran, this
    # costs nothing and the app is fifteen minutes fresh; when it did not —
    # it failed, or the entry was fired by hand from the portal — this is
    # what stops the press landing on yesterday's list.
    if not _freshen(driver, report, package, max_age=FRESH_FOR):
        _bring_up(driver, package)

    # BEFORE THE VISIT IS EVEN OPENED, because the visit's own page is the
    # one that lies. It reports this DEVICE's history, so a phone that has
    # never checked this patient in shows an empty record and a live
    # `Check in` for a visit the caregiver finished hours ago — and this app
    # accepts that press and answers "Success". One 05:00-06:00 visit
    # collected two extra check-ins that way, four hours after it ended.
    #
    # A refusal here spends the slot, which is correct: the entry exists, it
    # simply was not made by us, and REQ-5.5 forbids trying again later into
    # a record that would claim a later arrival than the truth.
    already = _already_entered(driver, report, package, patient)
    if already:
        raise RuntimeError(f"this visit is already checked in ({already})")

    if package == "com.hhaexchange.uma":
        # A shape of its own: the button is on the landing screen and the
        # press is followed by a GPS map with its own Continuar. See
        # `_uma_entry`, walked live with the owner watching.
        _uma_entry(driver, report, patient, _evv_when(arg))
    else:
        report("macro.step.finding_patient")
        # Opens TODAY's visit or refuses — see `_open_todays_visit`. inMyTeam
        # gates the action to the scheduled day and says so, and the app being
        # right about that is not the walk being wrong.
        _open_todays_visit(driver, report, package, patient)

        report("macro.step.checking_in")
        button = _words(driver, *EVV_ENTRY_WORDS[package])
        if button is None:
            raise RuntimeError("the check-in control is not on this screen")
        button.click()
        time.sleep(EVV_SETTLE)
    # AND AGAIN AFTER THE PRESS, because this is where it actually appeared:
    # `Check in` is what asks for the fix, so the dialog lands between the
    # press and the confirmation rather than before either.
    _answer_the_permission_dialog(driver, report)

    # The app's own refusal, named. Read before the confirmation wait so it
    # comes back as "the app refused the time" instead of twelve seconds of
    # silence and "the screen did not confirm the check-in".
    after = driver.page_source or ""
    if any(w in after.lower() for w in INVALID_TIME_WORDS):
        raise RuntimeError("the app refused the time")

    # VERIFY, because "the tap was accepted" is not "the visit started". The
    # cost of believing a tap that did not take is a shift with no check-in
    # on it, which is the exact failure this project exists to prevent — and
    # one week of live data had three of them.
    report("macro.step.confirming")
    if not wait_for(lambda: any(
            w.lower() in (driver.page_source or "").lower()
            for w in EVV_STARTED_WORDS[package]), timeout=12.0):
        raise RuntimeError("the screen did not confirm the check-in")


def _evv_prepare(driver, report, arg: str) -> None:
    """Open the patient's visit and stop, leaving the control on the screen.

    THE ANSWER TO inMyTeam NOT BEING ARMABLE THE NIGHT BEFORE. That app draws
    "Check in" only on the scheduled day — the evening before shows "This
    visit is not scheduled for today" and no control, so there is nothing to
    get ready against until the day arrives.

    Arming is not the walk, though. Arming is a standing decision about a
    recurring block; the walk belongs on the day, in the lead window. This is
    that walk. By the time the entry is due the app is open, signed in and on
    the patient's own detail, so the fire is one press rather than a cold
    start, a login and a search against the clock.

    IT PRESSES NOTHING CONSEQUENTIAL. Opening a visit's detail is reading.
    """
    package, patient = _evv_parts(arg)
    report("macro.step.launching")
    # FROM COLD, ALWAYS. This is the one moment in the day when a restart is
    # free — the lead window exists precisely to spend fifteen minutes on
    # getting ready — and it is the only way to be sure the list the fire
    # presses against was fetched today rather than yesterday.
    _freshen(driver, report, package)
    report("macro.step.finding_patient")
    # The same day-checked opener the fire uses. A walk that gets ready on
    # the WRONG day is worse than one that fails: it reports success and
    # leaves the fire fifteen minutes later to discover the truth.
    _open_todays_visit(driver, report, package, patient)
    report("macro.step.ready")


def _phone_settings(driver, report) -> None:
    """Open Android's own Settings.

    Nothing this portal does needs it; the person holding the portal
    sometimes does — wifi, sound, the phone's own display size. The
    containment watchdog is told to allow it (see feed.SETTINGS_APPS), or it
    would bounce the phone back to a care app five seconds after it opened.
    """
    from apt_log import feed as feed_mod

    report("macro.step.launching")
    wake_display()
    feed_mod._adb(["shell", "am", "start", "-a",
                   "android.settings.SETTINGS"])
    wait_for(lambda: "settings" in _front_package(), timeout=15.0)


def _restart_phone(driver, report) -> None:
    """Reboot the phone.

    The last resort, and the reason it is on this page: the alternative is a
    phone call to somebody in the room. It costs about a minute during which
    nothing works — adb goes away, the resident Appium session dies with it,
    and both come back on their own.

    Refused while a visit app is mid-flow is NOT attempted: this code cannot
    tell a half-finished check-in from a settled screen, and a button that
    sometimes refuses for reasons nobody can see is worse than one that
    always does what it says. It is spelled out on the page instead.
    """
    from apt_log import feed as feed_mod

    report("macro.step.restarting")
    feed_mod._adb(["reboot"])
    # Do not wait for it here. The reboot takes the driver with it, and a
    # macro that sits for a minute holding the session is a macro that looks
    # wedged while the phone is doing exactly what it was told.


MACROS: dict[str, Macro] = {
    m.name: m for m in (
        Macro("hhax_legacy_login", "macro.hhax_legacy_login", _hhax_legacy_login),
        Macro("hhax_uma_login", "macro.hhax_uma_login", _hhax_uma_login),
        Macro("mobile_caregiver_pin", "macro.mobile_caregiver_pin",
              _mobile_caregiver_pin),
        Macro("open_hhax_legacy", "macro.open_hhax_legacy",
              _open_app("com.hhaexchange.caregiver")),
        Macro("open_hhax_uma", "macro.open_hhax_uma",
              _open_app("com.hhaexchange.uma")),
        Macro("open_mobile_caregiver", "macro.open_mobile_caregiver",
              _open_app("com.tellus.evv.v2")),
        Macro("inmyteam_login", "macro.inmyteam_login", _inmyteam_login),
        Macro("inmyteam_resend_code", "macro.inmyteam_resend_code",
              _inmyteam_resend_code),
        Macro("open_inmyteam", "macro.open_inmyteam",
              _open_app("com.inmyteam.inmyteam")),
        Macro("read_page", "macro.read_page", _read_page),
        Macro("close_app", "macro.close_app", _close_app),
        Macro("restart_app", "macro.restart_app", _restart_app),
        Macro("rescan", "macro.rescan", _rescan),
        Macro("app_home", "macro.app_home", _app_home),
        Macro("uma_agency", "macro.uma_agency", _uma_agency),
        Macro("uma_agency_for", "macro.uma_agency", _uma_agency_for,
              takes_arg=True),
        # THE TWO THAT WRITE A RECORD ABOUT A PERSON. `evv_entry` is in
        # CONFIRM: a person pressing it by hand is asked first, because from
        # the portal it looks like any other button and it is not one. The
        # scheduler does not go through that prompt — its confirmation is the
        # arming switch, thrown in advance and recorded with a name.
        Macro("evv_entry", "macro.evv_entry", _evv_entry, takes_arg=True),
        Macro("evv_checks", "macro.evv_checks", _evv_checks,
              takes_arg=True),
        Macro("evv_prepare", "macro.evv_prepare", _evv_prepare,
              takes_arg=True),
        Macro("clear_screen", "macro.clear_screen", _clear_screen),
        Macro("check_tasks", "macro.check_tasks", _check_tasks),
        Macro("phone_settings", "macro.phone_settings", _phone_settings),
        Macro("restart_phone", "macro.restart_phone", _restart_phone),
        Macro("update_app", "macro.update_app", _update_app),
        # One per app, for the console's version panel. Named rather than
        # parameterised because /macro takes a name from this registry and
        # nothing else — a route that accepted a package from a browser
        # would be a route that installs whatever it is handed.
        Macro("update_hhax_uma", "macro.update_hhax_uma",
              _update_app_for("com.hhaexchange.uma")),
        Macro("update_mobile_caregiver", "macro.update_mobile_caregiver",
              _update_app_for("com.tellus.evv.v2")),
        Macro("update_inmyteam", "macro.update_inmyteam",
              _update_app_for("com.inmyteam.inmyteam")),
    )
}

# What the control centre offers, in the order somebody reaches for them:
# refresh what I am looking at, restart what is stuck, close it, the phone's
# own settings, and — last, and last resort — the phone itself.
#
# The sign-in macros are deliberately not here. They are battle-tested and
# they run themselves when a session expires; a button that duplicates what
# already happens automatically is a button whose only use is pressing it at
# the wrong moment.
OPERATIONS = ("rescan", "read_page", "evv_checks", "clear_screen",
              "restart_app", "close_app", "phone_settings", "restart_phone")

# The ones that cannot be undone by pressing it again. The page asks first.
#
# `update_app` belongs here for a reason unlike the other's: a reboot costs a
# minute, while an install replaces the software this whole project is written
# against and there is no going back to the old build from the phone. It is
# also deliberately absent from OPERATIONS — it appears only on the screen
# where an update is actually being demanded, because a button offering to
# replace an app is not a thing to have standing by.
CONFIRM = ("restart_phone", "update_app", "update_hhax_uma",
           "update_mobile_caregiver", "update_inmyteam",
           # Asks because it sends a real text message to a real phone and
           # steps toward a rate limit that would lock the account out of
           # the app. Cheap to press, not free.
           "inmyteam_resend_code",
           # Asks because it writes an EVV record asserting a caregiver was
           # at a patient's home. From the portal it looks like every other
           # button on the page and it is not one of them. The SCHEDULER does
           # not come through here — its confirmation is the arming switch,
           # thrown in advance and recorded with a name (REQ-5.9).
           "evv_entry")


# Session-expiry dialogs, per app. A dialog whose wording is recognised as
# "your session ended" is itself the request to sign back in: its only
# meaningful exit leads to the login screen, each app's auth macro already
# knows how to walk from there, and waiting instead leaves the phone parked
# on the dialog until a human taps — the owner's screenshot, every morning.
# Wordings only; an unrecognised dialog stays untouched, as everywhere.
#
# The legacy list is deliberately narrower than session.EXPIRED_WORDINGS:
# that list matches the text of a modal already known to be up, where
# "iniciar sesión" is safe. Here the match runs against a whole screen's
# words, and the login page itself says "iniciar sesión".
EXPIRY_MARKERS = {
    "com.hhaexchange.caregiver": (
        "sesión ha expirado", "sesion ha expirado", "session has expired",
        "inactividad", "inactivity",
        "cerrará la sesión", "cerrara la sesion",
    ),
    "com.hhaexchange.uma": (
        "se ha cerrado la sesión", "se ha cerrado la sesion",
        "regresar al inicio de sesión", "regresar al inicio de sesion",
        "logged out due to", "return to login", "back to login",
    ),
    # Mobile Caregiver+ shows this one AFTER the passcode has already been
    # accepted: the keypad unlocks the app on the phone, the dashboard opens,
    # and only then does it admit the server session is gone. Walked live.
    "com.tellus.evv.v2": (
        "sesión caducada", "sesion caducada",
        "sesión ha caducado", "sesion ha caducado",
        "session has expired",
    ),
}


def expiry_on_screen(doc: dict) -> bool:
    """Whether the screen document shows a recognised session-expiry dialog."""
    markers = EXPIRY_MARKERS.get(doc.get("app") or "")
    if not markers:
        return False
    words = " ".join(
        n.get("txt") or ""
        for n in (doc.get("statics") or []) + (doc.get("elements") or [])
    ).lower()
    return any(m in words for m in markers)


def auth_macro_for(app: str, provider=None, doc: dict | None = None) -> str | None:
    """The auth macro for a foreground package — if its secrets exist.

    An auth macro without its credentials fails on every auto-auth attempt,
    ninety seconds apart, for as long as someone watches — so an app only
    gets automatic sign-in once its secrets are actually on the device. The
    tile still offers the macro either way; a manual press failing once with
    a clear status is information, a background loop of failures is noise.

    `doc` is the screen document, consulted only for Chrome: HHAeXchange+'s
    session expires into a Chrome Custom Tab, and a login screen sitting in
    Chrome is invisible to a package-only rule — the session died overnight
    and nobody was signed back in until a human tapped. The document's own
    words say whose form it is.
    """
    from apt_log import secrets as secrets_mod

    provider = provider or secrets_mod.FileSecretProvider()

    def have(*keys) -> bool:
        try:
            for key in keys:
                provider.get(key)
            return True
        except (secrets_mod.SecretNotFound, PermissionError, OSError):
            return False

    def uma_credentialed() -> bool:
        # One account fronts both HHAeXchange apps: the legacy credentials
        # satisfy this one too, and UMA_* only exists to override them.
        return (have(secrets_mod.UMA_USERNAME, secrets_mod.UMA_PASSWORD)
                or have(secrets_mod.APP_USERNAME, secrets_mod.APP_PASSWORD))

    if app == "com.hhaexchange.caregiver":
        # RETIRED. Its one patient moved to HHAeXchange+, so a session on it
        # is a session nobody is going to use — and signing one in
        # unprompted is the churn the watcher's whole gate exists to avoid.
        # The macro stays registered: this refuses to run it BY ITSELF, not
        # to run it at all, because fetching an old record deliberately is
        # exactly the case retirement should still allow. See feed.retired.
        return None
    if app == "com.hhaexchange.uma":
        return "hhax_uma_login" if uma_credentialed() else None
    if app == "com.android.chrome" and doc:
        words = " ".join(
            n.get("txt") or ""
            for n in (doc.get("statics") or []) + (doc.get("elements") or []))
        if "hhaexchange" in words.lower():
            return "hhax_uma_login" if uma_credentialed() else None
        return None
    if app == "com.tellus.evv.v2":
        # Either secret makes the macro worth offering: the app locks two
        # different ways and the screen in front decides which one it is
        # answering. Offering it with only a PIN stored is still right — the
        # keypad is the lock seen most often — and it fails loudly rather
        # than silently on the form.
        return ("mobile_caregiver_pin"
                if have(secrets_mod.MC_PIN) or have(secrets_mod.MC_PASSWORD)
                else None)
    if app == "com.inmyteam.inmyteam":
        # It signs itself in like the other three now. It was held back
        # because pressing it SENDS A TEXT MESSAGE, and automatic meant a
        # phone idling on that splash texting somebody every ninety seconds.
        # Two things changed that: the walk is a no-op once the app is
        # already asking for a code, so the common repeat costs nothing; and
        # anything that sends a message gets its own long cooldown (see
        # SMS_AUTH_COOLDOWN) rather than the ordinary one.
        return "inmyteam_login" if have(secrets_mod.INMYTEAM_PHONE) else None
    return None


# The words inMyTeam's own sign-in screens use. This app needs them because
# it has ONE activity: splash, phone number, code and the signed-in app all
# live under `mainactivity`, so the atlas cannot tell them apart and the
# capture refusals never fire — there is no password field anywhere in the
# walk. Its own words are the only signal it gives.
_IMT_LOGIN_WORDS = (
    "get started",            # the marketing splash
    "sign in with your phone",
    "enter your cell phone",
    "iniciar sesión con su",  # the same screens with the phone in Spanish
    "número de teléfono",
)


def wants_to_sign_in(doc: dict | None) -> bool:
    """Whether inMyTeam is showing one of its own sign-in screens.

    The code screen is deliberately NOT here. Reaching it is the walk's
    destination, and treating it as "please sign in" would put the loop back
    at a screen it had already arrived at.
    """
    if not doc or doc.get("app") != "com.inmyteam.inmyteam":
        return False
    words = " ".join(
        (n.get("txt") or "")
        for n in (doc.get("statics") or []) + (doc.get("elements") or [])
    ).lower()
    if any(w in words for w in ("enter your code", "verify your account",
                                "introduce el código")):
        return False
    return any(w in words for w in _IMT_LOGIN_WORDS)


# -------------------------------------------------------------------- request
# How much of an argument is ever carried. Long enough for the longest
# provider name on the account and short enough that nothing interesting fits.
ARG_MAX = 120


def request(name: str, path: Path | None = None, arg: str = "") -> str:
    """Ask for a macro by name. Returns the request id.

    `arg` is passed only to macros that declared `takes_arg`, and is dropped
    for every other one — so adding an argument to a request cannot change
    what a macro that never wanted one does.
    """
    if name not in MACROS:
        raise KeyError(name)
    if not MACROS[name].takes_arg:
        arg = ""
    target = path or REQUEST_PATH
    rid = uuid.uuid4().hex[:12]
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"id": rid, "name": name, "at": time.time(),
                               "arg": str(arg)[:ARG_MAX]}),
                   encoding="utf-8")
    os.replace(tmp, target)
    log.info("macro requested: %s (%s)", name, rid)
    return rid


def someone_wants_the_phone(request_path: Path | None,
                            deep_path: Path | None,
                            poke_path: Path | None) -> bool:
    """True if a real action is waiting — a macro, a below-fold tap, a fresh
    finger. The warm sweep peeks with this (never consuming the request) so
    it yields the phone the instant she actually asks for something."""
    for p in (request_path or REQUEST_PATH, deep_path or DEEPTAP_REQUEST_PATH):
        try:
            if p.exists():
                return True
        except OSError:
            pass
    try:
        poke = (poke_path or SCREEN_PATH.parent / "hierarchy-poke")
        if time.time() - poke.stat().st_mtime < STITCH_TAP_QUIET:
            return True
    except OSError:
        pass
    return False


def take_request(path: Path | None = None) -> dict | None:
    """Claim a pending request, removing it. None when there is nothing to do."""
    target = path or REQUEST_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        target.unlink()
    except OSError:
        pass

    if time.time() - float(payload.get("at", 0)) > REQUEST_MAX_AGE:
        log.info("ignoring a stale macro request (%s)", payload.get("name"))
        return None
    if payload.get("name") not in MACROS:
        return None
    return payload


# --------------------------------------------------------------------- status
def write_status(status: Status, path: Path | None = None) -> None:
    target = path or STATUS_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(status.__dict__), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("cannot publish macro status (%s)", exc)


def read_status(path: Path | None = None) -> Status:
    target = path or STATUS_PATH
    try:
        return Status(**json.loads(target.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError):
        return Status()


# --------------------------------------------------------------------- runner
class Runner:
    """Polls for a request and runs it on the resident session."""

    def __init__(self, request_path: Path | None = None,
                 status_path: Path | None = None,
                 screen_path: Path | None = None,
                 viewers_path: Path | None = None,
                 secrets=None):
        self._request_path = request_path
        self._status_path = status_path
        self._screen_path = screen_path
        self._viewers_path = viewers_path
        self._secrets = secrets
        # None, not 0.0: the cooldown compares against time.monotonic(),
        # which starts near zero at boot — a zero sentinel made a machine
        # younger than the cooldown believe an auth had just fired, and
        # auto-auth lay dormant for the rest of the window. Found when a
        # freshly recycled container failed three tests a long-lived one had
        # been passing all day.
        self._auto_auth_at: float | None = None
        # Per macro, so one app's cooldown never gates another's.
        self._auto_auth_seen: dict[str, float] = {}
        # The frame a stitch walk failed on: not retried until the screen
        # changes, or a stubborn page would be walked forever.
        self._stitch_failed_for: str = ""
        # When the next walk attempt is allowed (monotonic). None, not 0.0,
        # for the same reason as the auth cooldown above. The floor binds
        # only re-walks of the page it was armed on — a page transition
        # scans immediately.
        self._stitch_next_at: float | None = None
        self._stitch_last_page: tuple | None = None
        # When the scheduler last looked for something to fire (monotonic).
        # The loop ticks once a second and a fire has a five-minute window,
        # so this keeps it from re-reading the schedule sixty times a minute.
        # None, not 0.0, for the same reason as the auth cooldown: "never
        # looked" is a different fact from "looked at the epoch".
        self._fire_checked: float | None = None
        # The same, for the lead-window walk, plus which occurrences have
        # already been walked. In memory rather than on disk: a restart
        # re-walking one visit is harmless (it opens a page), where a restart
        # re-FIRING one is not, which is why that ledger is on disk and this
        # set is not.
        self._prep_checked: float | None = None
        self._prepared: set = set()
        # Whether each app's last substantive screen was a foldable page
        # (a run of date headers): a CHANGE in this is a page transition
        # even when the activity name never changes (Compose keeps every
        # page in one activity). None until first observed.
        self._seen_folds: dict = {}
        # An app whose tabs are worth pre-scanning: set the moment a
        # sign-in finishes, cleared once its tabs are warmed (or the sweep
        # was pre-empted). None means nothing to warm.
        self._warm_app: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._recall_auto_auth()
        self._reconcile()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="aptlog-macros")
        self._thread.start()

    def _auth_seen_path(self) -> Path:
        """Beside the status file, wherever that is — so a test that
        redirects one redirects both."""
        if self._status_path is not None:
            return self._status_path.parent / AUTH_SEEN_PATH.name
        return AUTH_SEEN_PATH

    def _recall_auto_auth(self) -> None:
        """Load when each auto-auth last fired, from before the restart."""
        try:
            stored = json.loads(
                self._auth_seen_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return
        if not isinstance(stored, dict):
            return
        self._auto_auth_seen = {
            name: float(when) for name, when in stored.items()
            if isinstance(name, str) and isinstance(when, (int, float))}

    def _remember_auto_auth(self) -> None:
        """Write it down. Never fatal: failing to record a cooldown must not
        stop the sign-in it was recorded for."""
        try:
            target = self._auth_seen_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._auto_auth_seen), encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            log.warning("cannot record the auto-auth cooldown (%s)", exc)

    def _reconcile(self) -> None:
        """A macro cannot still be running if this process just started.

        WHAT THIS COSTS WHEN IT IS MISSING, caught live: auto sign-in fired
        for inMyTeam at 13:18:18 and wrote "running"; the deploy restarted
        this service at 13:18:19 and killed the thread mid-macro. Nothing
        ever wrote a terminal state, so the file said "running" for the next
        thirteen minutes — and it would have said so until some other macro
        happened to run.

        Two things read that flag and both went wrong. The containment
        watchdog stands down for a macro in flight, so the phone sat outside
        its care apps unwatched. And auto-auth refuses to stack onto a
        running macro, so the sign-in that was interrupted could never
        restart — the app stayed on its marketing splash, which is exactly
        where it was found.

        The status is a claim about THIS process. On start it is either
        finished or it was interrupted, and "interrupted" is the honest word
        for it: not a macro that failed at its task, one that never got to
        try.
        """
        was = read_status(self._status_path)
        if was.state != "running":
            return
        log.warning("macro %s was interrupted by a restart; clearing it",
                    was.name or "?")
        write_status(Status(id=was.id, name=was.name, state="failed",
                            step=was.step,
                            error="interrupted by a restart"),
                     self._status_path)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # Signature replays ride this loop too: both need the resident session,
        # UiAutomator2 allows exactly one, and one thread claiming work is what
        # keeps a macro and a replay from interleaving their gestures.
        from apt_log import sign

        while not self._stop.is_set():
            try:
                deep = take_deep_tap()
                if deep is not None:
                    self.execute_deep_tap(deep)
                pending = take_request(self._request_path)
                if pending is not None:
                    self.execute(pending["name"], pending["id"],
                                 pending.get("arg", ""))
                signature = sign.take_request()
                if signature is not None:
                    sign.execute(signature)
                action = sign.take_action()
                if action is not None:
                    sign.do_action(action)
                self.maybe_auto_auth()
                self.maybe_fire()
                self.maybe_prepare()
                self.maybe_warm()
                self.maybe_stitch()
            except Exception as exc:  # noqa: BLE001
                log.warning("macro runner: %s", exc)
            self._stop.wait(POLL_EVERY)

    def maybe_fire(self) -> bool:
        """Do the armed entries whose minute has come. The scheduler's hands.

        Runs on the same loop as everything else, so it holds the one resident
        session the same way a macro does and cannot interleave gestures with
        one. It yields to a running macro rather than queueing behind it: a
        fire that waits is a fire that lands late, and late is refused.

        THE ORDER HERE IS THE SAFETY PROPERTY. The occurrence is spent BEFORE
        the phone is touched, so a crash mid-press cannot leave the slot open
        for the next tick to press again — a double check-in is a corrupted
        record on somebody's timesheet, which is worse than a missed one. The
        outcome is amended after.
        """
        from apt_log import arming, autoentry, schedule as schedule_mod

        if read_status(self._status_path).state == "running":
            return False
        # The loop ticks once a SECOND, and a fire has a five-minute window —
        # so re-reading and re-parsing the schedule sixty times a minute buys
        # nothing at all. Cheapest question first: with nothing armed there is
        # no schedule to read, which is also the shipped state.
        if not arming.armed():
            return False
        tick = time.monotonic()
        if self._fire_checked is not None \
                and tick - self._fire_checked < FIRE_EVERY:
            return False
        self._fire_checked = tick
        now = datetime.now().astimezone()
        try:
            plan = schedule_mod.load()
        except Exception as exc:  # noqa: BLE001 — no schedule is not an error
            log.debug("no schedule to fire from (%s)", exc)
            return False
        items = autoentry.fireable(autoentry.due(plan, now))
        if not items:
            self._say_what_was_missed(plan, now)
            return False

        item = items[0]          # oldest first; the rest ride the next tick
        # The claim on the slot, taken first and unconditionally.
        autoentry.spend(item.occurrence, "running",
                        {"app": item.visit.app, "kind": item.kind,
                         # WHO SAID SHE WAS THERE. The record must never read
                         # as though a machine observed her (REQ-5.9).
                         "presence": "attested",
                         "attested_by": item.who,
                         "attested_at": item.attested_at,
                         "due": item.when.isoformat()})
        log.info("firing an armed %s for a %s visit attested by %s",
                 item.kind, item.visit.app, item.who or "unknown")
        rid = uuid.uuid4().hex[:12]
        try:
            status = self.execute("evv_entry", rid,
                                  _evv_arg(item.visit.app, item.visit.patient,
                                           item.visit.block.start.strftime("%H:%M")))
        except Exception as exc:  # noqa: BLE001
            autoentry.spend(item.occurrence, "failed",
                            {"error": type(exc).__name__})
            log.warning("the armed fire failed (%s)", type(exc).__name__)
            return False
        outcome = "done" if status.state == "done" else "failed"
        autoentry.spend(item.occurrence, outcome,
                        {"error": status.error or ""})
        _tell_somebody_about_the_fire(item, outcome)
        return outcome == "done"

    def maybe_prepare(self) -> bool:
        """Get the app onto the patient's visit before the entry is due.

        WITHOUT THIS THE FIRE IS A COLD START AGAINST THE CLOCK: launch, wait
        for a splash, maybe sign in, find the patient, open the visit, press —
        all inside a five-minute window, on an app that has been asleep since
        yesterday. The lead window exists so none of that happens at 5am.

        It runs ONCE per occurrence, not every tick, because it navigates the
        phone and doing that on a loop would fight the caregiver for it. The
        ledger records the walk under its own outcome, so a later fire can
        still spend the same slot for real.

        It presses nothing consequential: opening a visit's detail is reading.
        """
        from apt_log import arming, autoentry, schedule as schedule_mod

        if read_status(self._status_path).state == "running":
            return False
        if not arming.armed():
            return False
        tick = time.monotonic()
        if self._prep_checked is not None \
                and tick - self._prep_checked < PREPARE_EVERY:
            return False
        self._prep_checked = tick
        if someone_wants_the_phone(self._request_path, DEEPTAP_REQUEST_PATH,
                                   (self._screen_path or SCREEN_PATH).parent
                                   / "hierarchy-poke"):
            return False            # her hands are on it; the fire still works
        try:
            plan = schedule_mod.load()
        except Exception as exc:  # noqa: BLE001
            log.debug("no schedule to prepare from (%s)", exc)
            return False
        now = datetime.now().astimezone()
        for item in autoentry.preparing(plan, now):
            if autoentry.refusal(item.visit.app, item.kind,
                                 item.visit.block):
                continue
            if item.occurrence in self._prepared:
                continue
            self._prepared.add(item.occurrence)
            log.info("getting %s ready ahead of an armed entry",
                     item.visit.app)
            rid = uuid.uuid4().hex[:12]
            try:
                self.execute("evv_prepare", rid,
                             _evv_arg(item.visit.app, item.visit.patient,
                                           item.visit.block.start.strftime("%H:%M")))
            except Exception as exc:  # noqa: BLE001 — a failed walk is not a
                # failed fire. The entry still has its own attempt, from
                # wherever the phone happens to be, and that attempt is the
                # one that matters.
                log.warning("could not get ready (%s)", type(exc).__name__)
            return True
        return False

    def _say_what_was_missed(self, plan, now) -> None:
        """An armed entry whose window closed with nothing recorded.

        Said once — the ledger is marked as it is announced — because a person
        now has to make the entry by hand and the honest arrival minute is
        already in the past. Silence here would be the machine quietly not
        doing the one thing it was armed to do.
        """
        from apt_log import autoentry

        for item in autoentry.missed(plan, now):
            if autoentry.refusal(item.visit.app, item.kind,
                                 item.visit.block):
                continue
            autoentry.spend(item.occurrence, "missed",
                            {"app": item.visit.app, "kind": item.kind,
                             "due": item.when.isoformat()})
            log.warning("an armed %s went past its window unrecorded",
                        item.kind)
            _tell_somebody_about_the_fire(item, "missed")

    def maybe_warm(self) -> bool:
        """Pre-scan the just-signed-in app's other tabs, once.

        Warming is the only way a never-opened tab can be cached: its
        virtualized list has never been materialised, so scrolling it into
        being is unavoidable — the sweep just pays that cost before she
        does, while she is not yet interacting. Guarded like the scan
        (watching, idle, unblocked) and disarmed after one attempt whether
        it warmed anything or was pre-empted, so it never loops.
        """
        app = self._warm_app
        if not app:
            return False
        # The warm sweep scrolls too, so it is held off a replay for exactly
        # the same reason the walk is — see `maybe_stitch`.
        from apt_log import sign as sign_mod

        if sign_mod.in_flight():
            return False
        if not someone_is_watching(self._viewers_path):
            return False
        if read_status(self._status_path).state == "running":
            return False
        deep_path = DEEPTAP_REQUEST_PATH
        poke_path = (self._screen_path or SCREEN_PATH).parent / "hierarchy-poke"
        if someone_wants_the_phone(self._request_path, deep_path, poke_path):
            # She is already doing something; skip warming entirely rather
            # than fight her for the phone. Disarmed: her navigation will
            # scan each page she actually visits anyway.
            self._warm_app = None
            return False
        self._warm_app = None            # one attempt, whatever happens

        from apt_log import resident

        log.info("warming the tabs after sign-in")
        try:
            warmed = resident.run(
                lambda d: _warm_sweep(d, self._request_path, deep_path,
                                      poke_path))
            log.info("warmed %s tab(s)", warmed)
            # The sweep left the phone on a fresh page; let the normal scan
            # own it rather than treating the warmed landing as seen.
            self._stitch_last_page = None
            return bool(warmed)
        except Exception as exc:  # noqa: BLE001
            log.warning("tab warm failed: %s", exc)
            return False

    def maybe_stitch(self) -> bool:
        """Walk the current page into a whole-page document.

        The owner's spec, verbatim: "the front end should represent
        everything on the screen whether it's viewable or not" — nobody
        should ever scroll the phone to find out whether the portal missed
        something. So EVERY care-app page is scanned when it appears, not
        just ones advertising themselves scrollable (Compose screens lie
        about that), and a page that turns out not to scroll publishes as
        the whole page too — which is exactly what it is. The phone is
        remote-only, so it may scroll itself — but only while someone is
        watching, never over a running macro, and never within seconds of
        a tap: no scanning over her fingers. A failed walk is not retried
        for the same screen; the next screen change resets the ledger.
        """
        if not someone_is_watching(self._viewers_path):
            return False
        if read_status(self._status_path).state == "running":
            return False
        # A REPLAY IS NOT A MACRO, AND THE GUARD ABOVE CANNOT SEE ONE.
        #
        # The check above reads the macro status; a signature replay writes
        # its own file, so a walk could begin in the middle of one and scroll
        # straight down the canvas. This file's own comment records the
        # symptom from the first field test — "the scroll gesture drew a
        # straight line down the canvas she was about to sign" — and the
        # owner has now described the same thing again: "too straight of a
        # line, like something I didn't draw at all".
        #
        # It is also the reason the failure only ever happened while he was
        # watching. The walk needs a viewer (the line above), so every
        # signature the owner watched was eligible to be walked over, and
        # every replay requested from a script with nobody watching landed
        # whole. Four experiments could not reproduce it because none of them
        # was watched.
        from apt_log import sign as sign_mod

        if sign_mod.in_flight():
            return False
        target = self._screen_path or SCREEN_PATH
        try:
            doc = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if (doc.get("blocked") or doc.get("full") or not doc.get("app")
                or doc.get("screen") == "launcher"):
            return False
        # A canvas screen is never walked — a swipe there is ink on the
        # signature. The walk itself re-checks the live screen besides,
        # because this doc can be a page old by the time the swipe fires.
        if doc.get("canvas"):
            return False
        from apt_log import feed as feed_mod

        # Only the four apps the portal exists for. Without this, the walker
        # cheerfully walked the notification shade — swiping through system
        # UI nobody asked to read, on a phone nobody is holding.
        if doc.get("app") not in feed_mod.CARE_APPS:
            return False
        fid = doc.get("id") or ""
        if not fid or fid == self._stitch_failed_for:
            return False
        # Page identity by (app, activity) misses every transition inside a
        # Compose single-activity app: the schedule and the visit details
        # both live in HomeActivity, so coming BACK from the details read as
        # "same page" — no fresh walk, no re-expansion, and the owner tapped
        # a stale expanded card that refused ("clicked ver detalles and it
        # did not work"). The page's own shape breaks the tie: the schedule
        # shows a run of date headers and the details page does not, so a
        # change in foldability IS a page transition. Tracked on every look
        # (not only on walks — the cooldown used to eat the observation),
        # and a sparse mid-transition tree is no verdict either way.
        statics = doc.get("statics") or []
        app = doc.get("app")
        if len(statics) >= 5:
            folds_now = _page_folds(statics)
            was_folds = self._seen_folds.get(app)
            self._seen_folds[app] = folds_now
        else:
            folds_now = was_folds = self._seen_folds.get(app)
        try:
            poked = (target.parent / feed_mod.POKE_NAME).stat().st_mtime
            if time.time() - poked < STITCH_TAP_QUIET:
                return False
        except OSError:
            pass
        page = (doc.get("app"), doc.get("activity"))
        now = time.monotonic()
        fresh = (page != self._stitch_last_page
                 or (was_folds is not None and folds_now != was_folds))
        if (not fresh
                and self._stitch_next_at is not None
                and now < self._stitch_next_at):
            return False
        self._stitch_last_page = page
        self._stitch_next_at = now + STITCH_COOLDOWN

        from apt_log import resident

        log.info("walking the page for a whole-page document (frame %s)", fid)
        try:
            # A freshly-entered page is already at its top; only a re-scan
            # of the same page pays the scroll-to-top probe.
            ok = bool(resident.run(lambda d: _stitch_walk(d, assume_top=fresh)))
        except Exception as exc:  # noqa: BLE001
            # Transient — a session mid-rebuild, an adb hiccup. NOT latched:
            # the next loop may find the session back, and latching here
            # left a healthy screen unstitched until it changed.
            log.warning("stitch walk failed: %s", exc)
            return False
        if not ok:
            # The walk ran and produced nothing: this screen has nothing to
            # stitch, and retrying it forever would animate the phone for
            # no one. Latched until the screen changes.
            log.info("stitch walk yielded nothing for frame %s", fid)
            self._stitch_failed_for = fid
        return ok

    def execute_deep_tap(self, request: dict) -> None:
        """Tap below the fold: replay the scroll, re-verify, then touch.

        The same refuse-if-moved promise the tap machinery has always made,
        extended past the fold — the element must be found in a FRESH dump
        at its scroll step, and the tap lands on the found bounds, never
        the recorded ones.
        """
        from apt_log import resident
        from apt_log import stitch as stitch_mod
        from apt_log import feed as feed_mod

        aim = request["aim"]
        step = int(aim.get("step") or 0)

        tapped_app: list[str] = []

        def _do(driver):
            cx, y_top, y_bot = _swipe_geometry(driver)
            _scroll_to_top(driver, cx, y_top, y_bot)
            for _ in range(step):
                _swipe(driver, cx, y_bot, y_top)
                time.sleep(0.8)
            els = feed_mod.elements(driver.page_source or "")
            found = stitch_mod.locate(aim, els)
            if found is None:
                raise RuntimeError("no longer where it was — look again")
            tapped_app.append(driver.current_package or "")
            x1, y1, x2, y2 = found["b"]
            driver.tap([((x1 + x2) // 2, (y1 + y2) // 2)])

        ok, error = True, ""
        try:
            resident.run(_do)
        except RuntimeError as exc:
            ok, error = False, str(exc)
        except Exception as exc:  # noqa: BLE001
            ok, error = False, type(exc).__name__
        try:
            tmp = DEEPTAP_RESULT_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "id": request.get("id", ""), "ok": ok, "error": error,
                "at": time.time()}), encoding="utf-8")
            os.replace(tmp, DEEPTAP_RESULT_PATH)
        except OSError as exc:
            log.warning("cannot write the deep-tap result (%s)", exc)
        # The tap changed this app's page, so its cached scans are no longer
        # the truth. Only this app's: the other apps' pages were not touched,
        # and forgetting them meant every switch back re-scanned a page that
        # had not changed.
        if ok and tapped_app:
            _forget_stitched(tapped_app[0])

    def maybe_auto_auth(self) -> bool:
        """Sign in when the known app is sitting on its sign-in screen.

        True when an auth run was started. See the constants above for the
        whole argument; the conditions here are the guardrails in order —
        someone watching, right app, actually the login screen, seen fresh,
        and outside the cooldown that keeps a bad-credential day from
        becoming a loop.
        """
        if not someone_is_watching(self._viewers_path):
            return False

        # Never stack onto a run in flight or just done: a tile's own sign-in
        # walk leaves the login screen visible in the (slightly lagging)
        # screen document for a beat after it starts, and a second auth on
        # top of the first is the "why is it signing in twice" the owner
        # reported. The status file is the single record of runs either way.
        status = read_status(self._status_path)
        if status.state == "running":
            return False
        try:
            since = (datetime.now()
                     - datetime.fromisoformat(status.at)).total_seconds()
            if status.state in ("done", "failed") and since < 10.0:
                return False
        except (TypeError, ValueError):
            pass

        target = self._screen_path or SCREEN_PATH
        try:
            doc = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False

        # Per-app: each agency app whose secrets are on the device gets
        # automatic sign-in on its own credential screens.
        macro_name = auth_macro_for(doc.get("app") or "", self._secrets,
                                    doc=doc)
        if macro_name is None:
            return False
        # "Sign in when we see inputs for auth" — the owner's rule, taken
        # literally. The activity's name is not the signal: the flight
        # recorder's fourth-ever entry showed this app hosting its login form
        # under an activity classified "startup", and the capture refusals
        # (login-shaped activity, password field on screen) are the parts that
        # actually mean credentials are being asked for.
        from apt_log.feed import CREDENTIAL_REFUSALS

        if (doc.get("screen") != "login"
                and doc.get("blocked") not in CREDENTIAL_REFUSALS
                and not expiry_on_screen(doc)
                and not wants_to_sign_in(doc)):
            return False
        try:
            age = (datetime.now()
                   - datetime.fromisoformat(doc["at"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            return False
        if age > AUTO_AUTH_FRESH:
            return False
        # Two cooldowns, because two costs. The ordinary one keeps a
        # bad-credential day from becoming a loop. The long one is for a
        # macro whose every attempt SENDS A TEXT MESSAGE to a person: a
        # ninety-second retry there is a stream of codes she never asked for
        # and a walk into the app's rate limit. Kept per macro so a slow one
        # never gates a fast one.
        cooldown = (SMS_AUTH_COOLDOWN if macro_name in SENDS_A_MESSAGE
                    else AUTO_AUTH_COOLDOWN)
        # Per macro and ONLY per macro. Falling back to the shared timestamp
        # was the first version and it defeated the whole point: Mobile
        # Caregiver+ signing itself in put inMyTeam's fifteen minutes on the
        # clock, so inMyTeam sat on its splash with somebody watching and
        # nothing happened. Seen exactly that way on the live phone.
        last = self._auto_auth_seen.get(macro_name)
        now = time.time()
        if last is not None and 0 <= now - last < cooldown:
            # `0 <=` so a clock that jumped BACKWARDS cannot park a macro
            # forever: a timestamp in the future is not a cooldown that has
            # not elapsed, it is a reading that cannot be trusted.
            return False

        self._auto_auth_at = time.monotonic()
        self._auto_auth_seen[macro_name] = now
        self._remember_auto_auth()
        log.info("login screen is up — signing in without being asked")
        self.execute(macro_name, f"auto-{uuid.uuid4().hex[:8]}")
        return True

    def execute(self, name: str, rid: str, arg: str = "") -> Status:
        from apt_log import resident
        from apt_log.ui import mirror as mirror_mod

        macro = MACROS[name]
        status = Status(id=rid, name=name, state="running",
                        step="macro.step.starting")
        write_status(status, self._status_path)

        def report(step_key: str) -> None:
            status.step = step_key
            status.at = datetime.now().isoformat()
            write_status(status, self._status_path)
            # The mirror already says "stopped, waiting for your answer" and
            # friends; a macro is the one time it can honestly say "working".
            mirror_mod.publish(screen="unknown", step="working")

        try:
            if macro.takes_arg:
                resident.run(lambda driver: macro.run(driver, report, arg))
            else:
                resident.run(lambda driver: macro.run(driver, report))
        except Exception as exc:  # noqa: BLE001
            log.warning("macro %s failed: %s", name, exc)
            status.state = "failed"
            # The message is shown to her, so it must not carry a credential or
            # a name. Macro failures are structural -- a missing screen, a dialog
            # nobody recognised -- so the type is the useful part.
            status.error = type(exc).__name__
            status.at = datetime.now().isoformat()
            write_status(status, self._status_path)
            return status

        status.state = "done"
        status.step = "macro.step.finished"
        status.at = datetime.now().isoformat()
        write_status(status, self._status_path)
        log.info("macro %s finished", name)
        # A sign-in just landed on the app's home tab; its other tabs have
        # never been opened, so nothing about them is cached. Arm the warm
        # sweep to pre-scan them before she navigates.
        if WARM_ENABLED and "login" in name:
            self._warm_app = name
        return status
