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
# Screens flash through login on their own during app startup; only a login
# screen the feed has seen *recently and still* is a real landing.
AUTO_AUTH_FRESH = 6.0
SCREEN_PATH = STATE_DIR / "screen.json"

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
    message = session_mod.modal_message(driver)
    if message:
        if not session_mod.is_expired(driver):
            # Refusing to dismiss what nothing has read is the same rule the
            # session module holds, and a macro is not an excuse to break it.
            raise RuntimeError("an unrecognised dialog is on screen")
        session_mod.dismiss(driver)

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
                exits[0].click()             # the dialog's way out is login
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
    # discriminator, and wrong-guess damage is a tap inside a login form.
    size = driver.get_window_size()
    password_bottom = field("password").rect["y"]
    candidates = [
        b for b in driver.find_elements("class name", "android.widget.Button")
        if b.rect["width"] > size["width"] * 0.5
        and b.rect["y"] > password_bottom
    ]
    if not candidates:
        raise RuntimeError("no submit-shaped button below the password field")
    candidates[0].click()

    wait_for(lambda: dismiss_autofill(driver), timeout=6.0, poll=0.5)

    report("macro.step.checking")
    # Done means Chrome handed control back to the app, signed in.
    if not wait_for(lambda: driver.current_package == "com.hhaexchange.uma",
                    timeout=30.0):
        raise RuntimeError("did not return to the app after signing in")


def _mobile_caregiver_pin(driver, report) -> None:
    """Mobile Caregiver+ — type the passcode, only if the keypad is up.

    Discovery: an existing session sits behind a PIN keypad ("Introduce un
    código de acceso", digits 0-9). The keypad's buttons carry their digits
    as text, so the PIN is typed by tapping them; the screen advances by
    itself when the last digit lands.
    """
    from apt_log.secrets import MC_PIN, FileSecretProvider

    report("macro.step.launching")
    wake_display()
    driver.activate_app("com.tellus.evv.v2")
    wait_for(lambda: bool(driver.current_activity), timeout=15.0)

    def on_pin_screen():
        return "pin" in (driver.current_activity or "").lower()

    if not wait_for(on_pin_screen, timeout=4.0, poll=0.5):
        report("macro.step.checking")
        return

    pin = FileSecretProvider().get(MC_PIN)   # raises before any tap if unset

    report("macro.step.signing_in")
    for digit in pin:
        keys = driver.find_elements(
            "xpath", f'//android.widget.Button[@text="{digit}"]')
        if not keys:
            raise RuntimeError("the keypad is not where discovery saw it")
        keys[0].click()

    report("macro.step.checking")
    if not wait_for(lambda: not on_pin_screen(), timeout=15.0):
        raise RuntimeError("still on the passcode screen after typing the PIN")


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
        Macro("open_inmyteam", "macro.open_inmyteam",
              _open_app("com.inmyteam.inmyteam")),
    )
}


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
        return "mobile_caregiver_pin" if have(secrets_mod.MC_PIN) else None
    return None


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
                pending = take_request(self._request_path)
                if pending is not None:
                    self.execute(pending["name"], pending["id"])
                signature = sign.take_request()
                if signature is not None:
                    sign.execute(signature)
                self.maybe_auto_auth()
            except Exception as exc:  # noqa: BLE001
                log.warning("macro runner: %s", exc)
            self._stop.wait(POLL_EVERY)

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
                and not expiry_on_screen(doc)):
            return False
        try:
            age = (datetime.now()
                   - datetime.fromisoformat(doc["at"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            return False
        if age > AUTO_AUTH_FRESH:
            return False
        if (self._auto_auth_at is not None
                and time.monotonic() - self._auto_auth_at < AUTO_AUTH_COOLDOWN):
            return False

        self._auto_auth_at = time.monotonic()
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
        return status
