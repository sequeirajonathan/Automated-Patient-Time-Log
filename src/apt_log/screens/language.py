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


def advance_past_startup_gates(driver, max_steps: int = 3) -> list[str]:
    """Clear pre-login screens until a login form can be reached.

    Bounded rather than a while-loop: if a gate does not clear, this must fail
    and be reported, not spin. Returns the gates it dismissed.
    """
    dismissed: list[str] = []
    for _ in range(max_steps):
        screen = LanguageScreen(driver)
        if not screen.is_displayed():
            break
        screen.apply()
        dismissed.append("language")
    else:
        raise RuntimeError(
            f"still on a startup gate after {max_steps} attempts — not advancing blind"
        )
    return dismissed
