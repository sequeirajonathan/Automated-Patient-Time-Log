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

    def apply(self) -> None:
        """Accept the picker's current selection and move on.

        Deliberately does not choose a language. The app's own language affects
        what the caregiver sees on the phone, and REQ-11 governs the operator's
        UI rather than this one — changing it here would be a silent decision
        about someone else's screen. Whatever the device is already set to is
        the setting a human chose.
        """
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
            time.sleep(poll)
            continue

        if _activity(driver).endswith(TRANSIENT_ACTIVITIES):
            time.sleep(poll)
            continue

        # Settled on something that is neither a gate nor login: already signed in.
        return dismissed

    raise TimeoutError(
        f"app did not reach a login screen or a settled state within {timeout}s "
        f"(last activity: {_activity(driver) or 'unknown'})"
    )
