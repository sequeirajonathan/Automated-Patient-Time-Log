"""The language-selection gate that precedes login.

`com.hhaexchange.caregiver` opens on `.LanguageSelectionActivity`, a picker and an
Apply control, before it will show a login form. Discovered by inspecting the real
app: a cold start lands here, not on login, so anything that assumes otherwise
waits forever for a password field that is one screen away.

Unlike login.py this screen uses resource-ids. They are app-scoped and stable
across a session, and there is nothing structural to key on — a picker and a
clickable TextView look like any other view group. The ids are the app's own
public UI identifiers, not site data.

`txtApply` being a TextView rather than a Button is why login.py's submit search
falls back to clickable text; the same pattern shows up throughout this app.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

XPATH = "xpath"
ID = "id"

PACKAGE = "com.hhaexchange.caregiver"
ACTIVITY = ".LanguageSelectionActivity"


@dataclass
class LanguageSelectors:
    picker: str = f"{PACKAGE}:id/pickerView"
    apply: str = f"{PACKAGE}:id/txtApply"
    header: str = f"{PACKAGE}:id/lbl_header"


# The picker cannot be driven. Inspected on the device it is an android.view.View
# with no children, no text, no content-desc, clickable=false, scrollable=false
# and a11y-important=false — custom-drawn, so the language options are not in the
# accessibility tree at all. There is nothing to select by name and no way to read
# back what is currently selected.
#
# Blind swiping would "work" in the sense of changing something, but it could not
# be verified, and silently choosing the wrong language for the caregiver is
# exactly the class of guess this project refuses elsewhere.
#
# So the phone's language is set by a human when the device is provisioned
# (PI_SETUP.md §2) and the app follows it. What the automation *can* do is check
# that the screen is rendering in the language it expects before accepting the
# default, which is what header_language() is for.
# Two signals rather than one, because only the English side is confirmed. The
# header reads "Select Language" and the apply control reads "Next" on the device
# in front of us; the Spanish strings are the app's likely wording but have not
# been seen yet, so matching either field lets one carry the check if the other
# is phrased differently than guessed.
#
# When someone sets the phone to Spanish, read the two actual strings off the
# screen and correct these — a wrong guess here makes apply(expect_language="es")
# refuse a correctly-configured phone, which is a safe failure but a confusing one.
HEADER_BY_LANGUAGE = {
    "en": ("select language",),
    "es": ("seleccionar idioma", "seleccione idioma", "elegir idioma",
           "seleccionar lenguaje", "idioma"),
}

APPLY_BY_LANGUAGE = {
    "en": ("next",),
    "es": ("siguiente", "continuar", "aceptar"),
}


class LanguageMismatch(RuntimeError):
    """The app is not in the language this deployment expects."""


class LanguageScreen:
    def __init__(self, driver, selectors: LanguageSelectors | None = None):
        self.driver = driver
        self.sel = selectors or LanguageSelectors()

    def is_displayed(self) -> bool:
        """Prefer the activity name; fall back to the picker for a renamed activity."""
        try:
            if (self.driver.current_activity or "").endswith(ACTIVITY):
                return True
        except Exception:  # noqa: BLE001 — current_activity is not always available
            pass
        return bool(self.driver.find_elements(ID, self.sel.picker))

    def _text_of(self, selector: str) -> str:
        controls = self.driver.find_elements(ID, selector)
        return (controls[0].text or "").strip().lower() if controls else ""

    def header_language(self) -> str | None:
        """Which language this screen is rendering in, or None if unrecognised.

        Reads the header and the apply control. The picker is opaque, so these
        two labels are the only readable evidence of the app's language.
        """
        header = self._text_of(self.sel.header)
        apply_text = self._text_of(self.sel.apply)

        for code in ("es", "en"):  # check the specific case before the default
            if any(p in header for p in HEADER_BY_LANGUAGE.get(code, ())):
                return code
            if apply_text and apply_text in APPLY_BY_LANGUAGE.get(code, ()):
                return code
        return None

    def apply(self, expect_language: str | None = None) -> None:
        """Accept the picker's current selection and move on.

        Does not choose a language — it cannot, see the note above the selectors.
        `expect_language` asserts the screen is already rendering in the language
        the deployment expects, turning a silent wrong-language run into a loud
        failure at provisioning time rather than a surprise in Florida.
        """
        if expect_language is not None:
            actual = self.header_language()
            if actual != expect_language:
                raise LanguageMismatch(
                    f"expected the app in {expect_language!r} but the language "
                    f"screen reads {actual or 'an unrecognised language'}. The "
                    "picker cannot be driven programmatically — set the phone's "
                    "language in Android Settings (PI_SETUP.md §2)."
                )

        controls = self.driver.find_elements(ID, self.sel.apply)
        if not controls:
            raise RuntimeError(
                f"language screen present but {self.sel.apply} is missing — "
                "the app's layout has changed"
            )
        controls[0].click()
        log.info("dismissed the language-selection gate")


# Activities the app passes through on its way somewhere else. Checking for a
# gate while one of these is up finds nothing and wrongly concludes there is
# nothing to clear — observed on real hardware, where the language gate appears
# roughly three seconds after .AppLaunchActivity.
TRANSIENT_ACTIVITIES = (".AppLaunchActivity",)


def _activity(driver) -> str:
    try:
        return driver.current_activity or ""
    except Exception:  # noqa: BLE001
        return ""


def advance_past_startup_gates(
    driver, timeout: float = 45.0, poll: float = 1.0, max_clears: int = 3
) -> list[str]:
    """Wait out splash screens and clear pre-login gates until login is reachable.

    Polls rather than checking once. A cold start is not instantaneous, and the
    first thing on screen is a launch activity, not the gate — so a single
    immediate check reliably sees nothing to do and returns while the app is
    still on its way to the screen it was meant to clear.

    Stops as soon as a login form appears, or the app settles on something that
    is neither transient nor a known gate (already signed in). Bounded by both a
    deadline and a clear count, so a gate that refuses to advance is reported
    rather than looped on.
    """
    from apt_log.screens.login import LoginScreen

    deadline = time.monotonic() + timeout
    dismissed: list[str] = []
    seen_settled: str | None = None

    while time.monotonic() < deadline:
        if LoginScreen(driver).is_displayed():
            return dismissed

        gate = LanguageScreen(driver)
        if gate.is_displayed():
            if len(dismissed) >= max_clears:
                raise RuntimeError(
                    f"language gate still present after {max_clears} attempts — "
                    "not advancing blind"
                )
            gate.apply()
            dismissed.append("language")
            seen_settled = None
            time.sleep(poll)
            continue

        activity = _activity(driver)
        if activity.endswith(TRANSIENT_ACTIVITIES):
            seen_settled = None
            time.sleep(poll)
            continue

        # Anything else *might* mean already signed in — but each check above is a
        # separate round trip, so the app can move between them. Observed: the gate
        # appeared after is_displayed() read splash and before this line read the
        # activity, and the mismatch was misread as "settled". Require the same
        # activity twice before believing it, so a transition in flight is waited
        # out rather than mistaken for a destination.
        if seen_settled == activity:
            return dismissed
        seen_settled = activity
        time.sleep(poll)

    raise TimeoutError(
        f"app did not reach a login screen or a settled state within {timeout}s "
        f"(last activity: {_activity(driver) or 'unknown'})"
    )
