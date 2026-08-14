"""Screenshot capture with a password-field interlock (REQ-3).

The UI's phone-view panel shows the caregiver what the device sees, and failures
are easier to diagnose with a picture. Both are good reasons to capture screens,
and both would happily photograph a password field mid-entry.

The guard is *active* rather than advisory: `safe_screenshot` asks the device
whether a password field currently has focus and refuses on its own, instead of
trusting every future call site to wrap itself correctly. A call site that forgets
is the normal way this kind of rule decays.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from threading import local

log = logging.getLogger(__name__)

# XPath rather than a resource-id: password fields are marked by an accessibility
# attribute that every Android app sets, so this works without knowing the app.
_FOCUSED_PASSWORD = "//*[@password='true' and @focused='true']"
_ANY_PASSWORD = "//*[@password='true']"

_state = local()


def _suppressed() -> bool:
    return getattr(_state, "suppressed", False)


@contextmanager
def suppressed():
    """Explicitly forbid capture for the duration of a block.

    Belt to the interlock's braces: use it around a whole login flow, where a
    password field may be about to receive focus rather than already having it.
    """
    previous = _suppressed()
    _state.suppressed = True
    try:
        yield
    finally:
        _state.suppressed = previous


def password_field_focused(driver) -> bool:
    """True if a password input currently has focus.

    Errs toward True: if the hierarchy cannot be read, assume the unsafe case.
    A missed screenshot costs a diagnostic; a captured one costs a credential.
    """
    try:
        return bool(driver.find_elements("xpath", _FOCUSED_PASSWORD))
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot determine focus (%s); refusing capture", exc)
        return True


def password_field_present(driver) -> bool:
    try:
        return bool(driver.find_elements("xpath", _ANY_PASSWORD))
    except Exception:  # noqa: BLE001
        return True


def safe_screenshot(driver, *, strict: bool = False) -> str | None:
    """Base64 PNG, or None when capture would be unsafe.

    `strict` refuses whenever a password field exists on screen at all, not only
    when it holds focus — for anything persisted or shown to a third party.
    """
    if _suppressed():
        log.debug("screenshot suppressed by an active guard")
        return None
    if password_field_focused(driver):
        log.info("screenshot refused: a password field has focus (REQ-3)")
        return None
    if strict and password_field_present(driver):
        log.info("screenshot refused: a password field is on screen (strict)")
        return None
    try:
        return driver.get_screenshot_as_base64()
    except Exception as exc:  # noqa: BLE001
        # FLAG_SECURE apps refuse capture outright; that is a valid answer.
        log.info("screenshot unavailable: %s", exc)
        return None
