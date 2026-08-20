"""Agency selection, which follows sign-in.

The account carries more than one agency — a former employer is still registered
on it — so this screen is not a formality. Choosing wrong would record a visit
against the wrong employer, which is worse than recording nothing.

Selection is therefore by **exact configured name**, never by position. The
target happens to be first in the list today; an index would work now and switch
employers silently the day an agency is added or removed. Exactly one match is
required — zero raises, and so does more than one.

The agency name is site-specific and lives in /etc/aptlog/site.conf, not here.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from apt_log import config

log = logging.getLogger(__name__)

# How long the tap is given to land before the row is read back. Named rather
# than a bare 1.0 so the suite can stand it down — eight tests take this pause
# and none of them is testing the pause.
VERIFY_SETTLE = 1.0

ID = "id"

PACKAGE = "com.hhaexchange.caregiver"
ACTIVITY = ".AgencySelectionActivity"


@dataclass
class AgencySelectors:
    list_view: str = f"{PACKAGE}:id/list_agencies"
    label: str = f"{PACKAGE}:id/label_agency_name"
    header: str = f"{PACKAGE}:id/lbl_header"


class AgencyError(RuntimeError):
    """The configured agency could not be selected unambiguously."""


def _normalise(text: str) -> str:
    """Collapse whitespace and case so a label wraps without breaking a match."""
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


class AgencyScreen:
    def __init__(self, driver, selectors: AgencySelectors | None = None):
        self.driver = driver
        self.sel = selectors or AgencySelectors()

    def is_displayed(self) -> bool:
        try:
            if (self.driver.current_activity or "").endswith(ACTIVITY):
                return True
        except Exception:  # noqa: BLE001
            pass
        return bool(self.driver.find_elements(ID, self.sel.list_view))

    def _rows(self):
        return self.driver.find_elements(ID, self.sel.label)

    def available(self) -> list[str]:
        """Labels as shown. Used for the error message when nothing matches."""
        return [(el.text or "").strip() for el in self._rows()]

    def select(self, agency_name: str | None = None, *, verify: bool = True) -> str:
        """Choose the configured agency.

        The app renders each entry as "Name (Name)", so the configured value is
        matched as a substring of the label rather than for equality — but still
        has to match exactly one row.
        """
        target = agency_name or config.require(config.AGENCY_NAME)
        wanted = _normalise(target)

        rows = self._rows()
        if not rows:
            raise AgencyError("agency list is empty — the screen may not have loaded")

        matches = [el for el in rows if wanted in _normalise(el.text)]

        if not matches:
            raise AgencyError(
                f"no agency matching {target!r}. Offered: {self.available()!r}. "
                "Refusing to pick one — the account carries more than one employer."
            )
        if len(matches) > 1:
            raise AgencyError(
                f"{len(matches)} agencies match {target!r}; the configured name "
                "must identify exactly one. Refusing to guess."
            )

        chosen = (matches[0].text or "").strip()
        matches[0].click()
        log.info("selected agency %s", chosen)

        if verify:
            time.sleep(VERIFY_SETTLE)
            self._verify_left_or_checked(wanted)
        return chosen

    def _verify_left_or_checked(self, wanted: str) -> None:
        """Confirm the tap registered — either the screen advanced, or the row is checked.

        A tap that appeared to land is not evidence it did (REQ-4).
        """
        try:
            if not (self.driver.current_activity or "").endswith(ACTIVITY):
                return  # moved on, which is the success case
        except Exception:  # noqa: BLE001
            return

        for el in self._rows():
            if wanted in _normalise(el.text):
                if (el.get_attribute("checked") or "").lower() == "true":
                    return
                raise AgencyError(
                    "tapped the agency but it is not selected and the screen did "
                    "not advance"
                )
