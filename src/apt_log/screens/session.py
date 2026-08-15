"""Session expiry — an alert that can land on top of any screen.

The app drops its session aggressively and puts a modal over whatever activity
was showing: `lbl_message` reading "Debes de iniciar sesión." above a single
`btn_negative` reading "DE ACUERDO". Observed on `HomeActivity`, but nothing
about it is specific to that screen.

Two consequences shape this module.

**Logging in is not an exception path.** Scheduled visits are hours apart, so if
the session really expires in minutes then the agent finds this dialog *every*
time it wakes. Re-authentication is the ordinary preamble to a check-off, not a
recovery step, and treating it as an error would alert on the normal case.

**An unrecognised dialog is never dismissed.** `lbl_message` + `btn_negative` is
this app's generic alert shape — the same two ids carry whatever the app wants to
say. The message text is the only thing that distinguishes "your session ended"
from something that matters, and "DE ACUERDO" means *I agree*. Tapping it on an
unread message is agreeing to something nobody read, which is precisely what this
system must not do on the caregiver's behalf. So a modal whose wording is not
recognised raises instead, and the operator sees it.

The dangerous moment is expiry *between* a clock-in tap and its confirmation. The
caller must not assume either outcome: after re-login, re-read the visit's own
`lbl_visit_start_time` from today's list, which is the agency's record of what
actually happened (REQ-4, `screens/today.py`). Retrying on the assumption it
failed is how a visit gets recorded twice.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

ID = "id"

PACKAGE = "com.hhaexchange.caregiver"

# The app's generic alert shape. Neither id is unique to session expiry.
MESSAGE = f"{PACKAGE}:id/lbl_message"
DISMISS = f"{PACKAGE}:id/btn_negative"

# Wordings that mean "your session ended, log in again". Spanish is what the
# device runs; English is here for the same reason it is everywhere else in this
# package — the next device may not be configured the same way.
EXPIRED_WORDINGS = (
    "iniciar sesión",
    "iniciar sesion",
    "log in",
    "login",
    "sign in",
    "sesión ha expirado",
    "session has expired",
    # The inactivity countdown: "due to inactivity, you will be signed out in
    # 2 minutes". Same lifecycle family, and its DE ACUERDO acknowledges a
    # warning rather than agreeing to anything — dismissing it is safe and
    # leaving it up blocks every tap under it.
    "inactividad",
    "cerrará la sesión",
    "cerrara la sesion",
    "inactivity",
)


class UnknownDialog(RuntimeError):
    """A modal is on screen and its message is not one we recognise.

    Deliberately not recoverable in code. The button says "I agree" and nothing
    here has read what it would be agreeing to.
    """

    def __init__(self, message: str):
        self.message = message
        super().__init__(
            f"unrecognised modal: {message!r} — refusing to dismiss it, because "
            "the button agrees to something nothing here has read"
        )


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


def _text(driver, selector: str) -> str:
    found = driver.find_elements(ID, selector)
    return (found[0].text or "").strip() if found else ""


def modal_message(driver) -> str:
    """The text of the alert currently on screen, or empty if there is none."""
    return _text(driver, MESSAGE)


def is_expired(driver) -> bool:
    """True when the modal on screen is the session-expiry one.

    Requires both the alert and a recognised wording. The dialog ids alone would
    match any alert this app raises.
    """
    message = _normalise(modal_message(driver))
    if not message:
        return False
    return any(w in message for w in EXPIRED_WORDINGS)


def dismiss(driver) -> str:
    """Clear the session-expiry alert. Raises on any other modal.

    Returns the message that was dismissed, so the caller can record *which*
    interruption it handled rather than just that one occurred.
    """
    message = modal_message(driver)
    if not message:
        raise UnknownDialog("")

    if not is_expired(driver):
        raise UnknownDialog(message)

    buttons = driver.find_elements(ID, DISMISS)
    if not buttons:
        raise UnknownDialog(message)

    buttons[0].click()
    log.info("dismissed the session-expiry alert")
    return message


def interrupted(driver) -> bool:
    """True when *any* modal is blocking the screen.

    Separate from `is_expired` on purpose. A caller mid-flow needs to know it has
    been interrupted before it needs to know why — and the two answers differ,
    because an unrecognised interruption is a stop rather than a retry.
    """
    return bool(modal_message(driver))
