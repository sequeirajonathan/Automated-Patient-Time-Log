"""Android runtime permission dialogs.

These are the platform's own UI, not the app's, so they render in the *system*
locale — English on the dev handheld, Spanish on the production phone. Text
matching would therefore pass here and fail in Florida, which is the same trap
the login screen set. Everything below keys on framework resource-ids, which are
language-independent.

Two choices are deliberate rather than incidental:

**Never "Only this time".** It re-prompts on the next launch, and an unattended
box has nobody to answer a dialog. The run would simply stall behind it.

**Prefer the widest grant offered.** Location is what REQ-5 corroborates with, and
a foreground-only grant is worth having over none — but which options appear is
the platform's choice, so this takes the best available rather than assuming.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

ID = "id"
XPATH = "xpath"

ACTIVITY = "GrantPermissionsActivity"

# The controller package was renamed in Android 10; support both so the same code
# works on an older handheld.
PACKAGES = (
    "com.android.permissioncontroller",
    "com.android.packageinstaller",
)


@dataclass
class PermissionSelectors:
    # Ordered by preference. "Allow always" avoids a re-prompt entirely;
    # foreground-only is the usual best offer for location.
    allow_preference: tuple[str, ...] = (
        "permission_allow_always_button",
        "permission_allow_foreground_only_button",
        "permission_allow_button",
    )
    # Present but never chosen — see the module docstring.
    never_press: tuple[str, ...] = (
        "permission_allow_one_time_button",
        "permission_deny_button",
        "permission_deny_and_dont_ask_again_button",
    )
    # Android 12+ splits location into precise/approximate. A geofence needs precise.
    precise_location: str = "permission_location_accuracy_radio_fine"
    message: str = "permission_message"
    extra_allow_text: list[str] = field(default_factory=list)


class PermissionsScreen:
    def __init__(self, driver, selectors: PermissionSelectors | None = None):
        self.driver = driver
        self.sel = selectors or PermissionSelectors()

    def _find(self, short_id: str):
        for pkg in PACKAGES:
            found = self.driver.find_elements(ID, f"{pkg}:id/{short_id}")
            if found:
                return found[0]
        return None

    def is_displayed(self) -> bool:
        try:
            if ACTIVITY in (self.driver.current_activity or ""):
                return True
        except Exception:  # noqa: BLE001
            pass
        return any(self._find(s) is not None for s in self.sel.allow_preference)

    def describe(self) -> str:
        """What this dialog is asking for, for the log.

        Which permissions were granted is material to REQ-5 — a location grant
        that is foreground-only changes what the gate can rely on — so it gets
        recorded rather than silently accepted.
        """
        el = self._find(self.sel.message)
        return (el.text or "").strip() if el is not None else "unknown permission"

    def _select_precise_location(self) -> bool:
        radio = self._find(self.sel.precise_location)
        if radio is None:
            return False
        try:
            radio.click()
            log.info("selected precise location")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not select precise location: %s", exc)
            return False

    def allow(self) -> str:
        """Grant this dialog, taking the widest option offered.

        Returns the id of the control pressed, so the caller can record which
        kind of grant was actually made.
        """
        self._select_precise_location()

        for short_id in self.sel.allow_preference:
            el = self._find(short_id)
            if el is None:
                continue
            el.click()
            log.info("granted %r via %s", self.describe(), short_id)
            return short_id

        raise RuntimeError(
            "a permission dialog is on screen but none of the expected allow "
            f"controls were found ({', '.join(self.sel.allow_preference)}) — "
            "refusing to press an unidentified button"
        )


def grant_pending_permissions(
    driver, max_dialogs: int = 10, settle: float = 0.8
) -> list[str]:
    """Clear consecutive permission dialogs, allowing each.

    Bounded: permission prompts arrive in a chain and a dialog that will not
    dismiss must be reported rather than looped on. Returns what was granted.
    """
    granted: list[str] = []
    for _ in range(max_dialogs):
        screen = PermissionsScreen(driver)
        if not screen.is_displayed():
            return granted
        what = screen.describe()
        control = screen.allow()
        granted.append(f"{what} [{control}]")
        time.sleep(settle)

    raise RuntimeError(
        f"still on a permission dialog after {max_dialogs} grants — "
        f"granted so far: {granted}"
    )
