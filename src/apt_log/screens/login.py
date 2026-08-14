"""REQ-3 — the login screen.

Selectors are structural rather than app-specific on purpose. `@password='true'`
is an accessibility attribute every Android text field sets correctly, because
the platform needs it for masking, so it survives a redesign that renames every
resource-id. Where a real resource-id turns out to be more stable, override it
through `LoginSelectors` rather than editing the strategy.

Nothing here guesses. If the screen is ambiguous — two password fields, no
identifiable submit control — it raises rather than picking one and hoping, for
the same reason REQ-4 forbids guessing between two patients with similar names.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from apt_log import capture
from apt_log.secrets import APP_PASSWORD, APP_USERNAME, SecretProvider

log = logging.getLogger(__name__)

XPATH = "xpath"

SUBMIT_WORDS = re.compile(r"\b(sign ?in|log ?in|login|submit|continue|next)\b", re.I)


class LoginError(RuntimeError):
    """The login screen could not be driven confidently."""


@dataclass
class LoginSelectors:
    password: str = "//*[@password='true']"
    # Any editable field that is not the password one. Ordering in the hierarchy
    # puts the username first on every login form the author has seen, but the
    # code verifies rather than assuming.
    editable: str = "//android.widget.EditText"
    buttons: str = "//android.widget.Button"
    # Some apps use a clickable TextView instead of a real Button.
    clickable_text: str = "//*[@clickable='true' and string-length(@text)>0]"
    extra_login_markers: list[str] = field(default_factory=list)


class LoginScreen:
    def __init__(self, driver, selectors: LoginSelectors | None = None):
        self.driver = driver
        self.sel = selectors or LoginSelectors()

    # ------------------------------------------------------------- detection
    def is_displayed(self) -> bool:
        """A password field on screen is the reliable signal.

        Text matching ("sign in", "iniciar sesión", …) is not: it is
        language-dependent, and this deployment runs a bilingual operator.
        """
        if self.driver.find_elements(XPATH, self.sel.password):
            return True
        for marker in self.sel.extra_login_markers:
            if self.driver.find_elements(XPATH, marker):
                return True
        return False

    # ---------------------------------------------------------------- fields
    def _password_field(self):
        found = self.driver.find_elements(XPATH, self.sel.password)
        if not found:
            raise LoginError("no password field on screen")
        if len(found) > 1:
            # A "confirm password" form is not a login form; driving it blind
            # could change the account's password.
            raise LoginError(
                f"{len(found)} password fields on screen — refusing to guess "
                "which one to fill"
            )
        return found[0]

    def _username_field(self):
        editable = self.driver.find_elements(XPATH, self.sel.editable)
        password = self._password_field()
        candidates = [e for e in editable if e.id != password.id]
        if not candidates:
            raise LoginError("no username field distinguishable from the password field")
        if len(candidates) > 1:
            raise LoginError(
                f"{len(candidates)} candidate username fields — refusing to guess"
            )
        return candidates[0]

    def _submit_control(self):
        buttons = self.driver.find_elements(XPATH, self.sel.buttons)
        named = [b for b in buttons if SUBMIT_WORDS.search(b.text or "")]
        if len(named) == 1:
            return named[0]
        if len(buttons) == 1:
            return buttons[0]

        clickable = self.driver.find_elements(XPATH, self.sel.clickable_text)
        named = [c for c in clickable if SUBMIT_WORDS.search(c.text or "")]
        if len(named) == 1:
            return named[0]

        raise LoginError(
            f"cannot identify a submit control ({len(buttons)} buttons, "
            f"{len(named)} text matches) — refusing to tap something arbitrary"
        )

    # ----------------------------------------------------------------- action
    def login(self, secrets: SecretProvider) -> None:
        """Fill and submit. Credentials travel over the Appium session only.

        `send_keys` puts the password in an HTTP request body to a loopback-bound
        server — never in argv, never in shell history, never in a log line.
        """
        username = secrets.get(APP_USERNAME)
        password = secrets.get(APP_PASSWORD)

        # Guard the whole flow, not just the moment focus lands: a screenshot
        # taken between the tap and the keystrokes would still catch the field.
        with capture.suppressed():
            user_field = self._username_field()
            user_field.clear()
            user_field.send_keys(username)

            pw_field = self._password_field()
            pw_field.clear()
            pw_field.send_keys(password)

            self._submit_control().click()

        log.info("submitted credentials for %s", _redact(username))


def authenticate_if_needed(driver, secrets: SecretProvider,
                           selectors: LoginSelectors | None = None) -> bool:
    """REQ-3: check for the login screen at the start of every run.

    Returns True if a login was performed. A live session is never assumed — the
    agent fires hours apart and the app may have expired its own.
    """
    screen = LoginScreen(driver, selectors)
    if not screen.is_displayed():
        return False
    log.info("login screen present — re-authenticating")
    screen.login(secrets)
    return True


def _redact(value: str) -> str:
    """Enough to correlate a log line, not enough to reuse."""
    if len(value) <= 2:
        return "*" * len(value)
    return f"{value[0]}{'*' * (len(value) - 2)}{value[-1]}"
