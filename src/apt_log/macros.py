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
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

STATE_DIR = Path("/var/lib/aptlog")
REQUEST_PATH = STATE_DIR / "macro-request.json"
STATUS_PATH = STATE_DIR / "macro-status.json"

# A request older than this is ignored rather than run. The feed can be down when
# a button is pressed, and a sign-in that fires when the process comes back
# minutes later is a surprise on a phone nobody is holding.
REQUEST_MAX_AGE = 60.0

POLL_EVERY = 1.0

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


def someone_is_watching(path: Path | None = None) -> bool:
    target = path or VIEWERS_PATH
    try:
        if time.time() - target.stat().st_mtime > VIEWERS_FRESH:
            return False
        return int(json.loads(target.read_text(encoding="utf-8"))
                   .get("n", 0)) > 0
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


@dataclass
class Macro:
    name: str
    label_key: str          # i18n key; the UI never sees English from here
    run: object             # callable(driver, report) -> None


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
        driver.tap([(rect["x"] + rect["width"] // 2,
                     rect["y"] + rect["height"] - 80)])
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
    if wait_for(on_pin_screen, timeout=4.0, poll=0.5):
        # Read only once the keypad is actually up, and raised rather
        # than returned: an unlocked app must not even open the file.
        pin = FileSecretProvider().get(MC_PIN)
        report("macro.step.signing_in")
        for digit in pin:
            keys = driver.find_elements(
                "xpath", f'//android.widget.Button[@text="{digit}"]')
            if not keys:
                raise RuntimeError("the keypad is not where discovery saw it")
            keys[0].click()
        report("macro.step.checking")
        if not wait_for(lambda: not on_pin_screen(), timeout=15.0):
            raise RuntimeError(
                "still on the passcode screen after typing the PIN")

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
            slots.append({"point": (cx, s["b"][1] - 40), "selected": True})
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
    """inMyTeam — get as far as the code, and stop there.

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
        report("macro.step.awaiting_code")
        return

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
    submit.click()

    # ------------------------------------------------------------ the code
    # Done means the number was accepted and the app is asking for the code.
    # Not "the screen changed": a rejected number also changes the screen,
    # and reporting success over an error message is how a macro teaches
    # somebody to distrust it.
    report("macro.step.awaiting_code")
    if not wait_for(lambda: _asks_for_a_code(driver), timeout=25.0):
        raise RuntimeError("the app did not ask for a code")

    # The text has been sent, so somebody has to be told. This is the one
    # step in the whole system that cannot be waited out or refused — the
    # code exists only on her phone — and a portal that sits there silently
    # asking is a portal nobody discovers is asking.
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
    pushed = 0
    try:
        from apt_log import push

        pushed = push.send(PUSH_TITLE, CODE_WAITING, url=CODE_DEEP_LINK,
                           tag="otp")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not push the code notice (%s)", exc)

    from apt_log import notify

    notify.send(CODE_WAITING, url=PORTAL_URL)
    log.info("code notice: pushed to %d subscriber(s)", pushed)


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
        Macro("open_inmyteam", "macro.open_inmyteam",
              _open_app("com.inmyteam.inmyteam")),
        Macro("read_page", "macro.read_page", _read_page),
        Macro("close_app", "macro.close_app", _close_app),
        Macro("restart_app", "macro.restart_app", _restart_app),
        Macro("rescan", "macro.rescan", _rescan),
        Macro("clear_screen", "macro.clear_screen", _clear_screen),
        Macro("phone_settings", "macro.phone_settings", _phone_settings),
        Macro("restart_phone", "macro.restart_phone", _restart_phone),
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
OPERATIONS = ("rescan", "read_page", "clear_screen", "restart_app",
              "close_app", "phone_settings", "restart_phone")

# The one that cannot be undone by pressing it again. The page asks first.
CONFIRM = ("restart_phone",)


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
        return AUTO_AUTH_MACRO if have(secrets_mod.APP_USERNAME,
                                       secrets_mod.APP_PASSWORD) else None
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
def request(name: str, path: Path | None = None) -> str:
    """Ask for a macro by name. Returns the request id."""
    if name not in MACROS:
        raise KeyError(name)
    target = path or REQUEST_PATH
    rid = uuid.uuid4().hex[:12]
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"id": rid, "name": name, "at": time.time()}),
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
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="aptlog-macros")
        self._thread.start()

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
                    self.execute(pending["name"], pending["id"])
                signature = sign.take_request()
                if signature is not None:
                    sign.execute(signature)
                action = sign.take_action()
                if action is not None:
                    sign.do_action(action)
                self.maybe_auto_auth()
                self.maybe_warm()
                self.maybe_stitch()
            except Exception as exc:  # noqa: BLE001
                log.warning("macro runner: %s", exc)
            self._stop.wait(POLL_EVERY)

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
        if last is not None and time.monotonic() - last < cooldown:
            return False

        self._auto_auth_at = time.monotonic()
        self._auto_auth_seen[macro_name] = self._auto_auth_at
        log.info("login screen is up — signing in without being asked")
        self.execute(macro_name, f"auto-{uuid.uuid4().hex[:8]}")
        return True

    def execute(self, name: str, rid: str) -> Status:
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
