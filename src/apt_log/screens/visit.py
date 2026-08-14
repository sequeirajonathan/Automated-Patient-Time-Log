"""The visit detail screen, and the EVV method chooser behind clock-in.

Two findings here reshape what REQ-4 has to do.

**A visit is not clock-in/clock-out.** It carries a care task plan —
`expandable_list_tasks` holds thirteen duties for this visit — alongside the two
time controls and a signature tab. REQ-4 describes locating a patient and
verifying a tap; the actual workflow is larger, and the spec should say so before
anything is built against the smaller version.

**Clock-in offers a choice of verification method: GPS or a security token.**
That second option matters more than it looks. A token is read from a device in
the patient's home, so it evidences presence at the *patient's residence* — which
is what the agency actually needs — and it does not depend on a phone parked four
floors away getting a satellite fix through a concrete building. Nothing here
chooses between them; that is a decision about how the deployment works, not one
this module should make silently.

Nothing in this module completes a clock-in. `open_verification_chooser` stops at
the chooser and `cancel_verification` backs out of it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

ID = "id"
XPATH = "xpath"

PACKAGE = "com.hhaexchange.caregiver"
ACTIVITY = ".VisitDetailActivity"

# As rendered on the device. English wordings included for the same reason they
# are everywhere else: development runs Spanish, another device may not.
METHOD_GPS = "gps"
METHOD_TOKEN = "token"

METHOD_LABELS = {
    METHOD_GPS: ("gps",),
    METHOD_TOKEN: ("token de seguridad", "security token", "token"),
}


@dataclass
class VisitSelectors:
    clock_in: str = f"{PACKAGE}:id/btn_clock_in"
    clock_out: str = f"{PACKAGE}:id/btn_clock_out"
    start_time: str = f"{PACKAGE}:id/lbl_start_time"
    end_time: str = f"{PACKAGE}:id/lbl_end_time"
    confirmed_start: str = f"{PACKAGE}:id/lbl_confirmed_start_time"
    confirmed_end: str = f"{PACKAGE}:id/lbl_confirmed_end_time"
    tasks: str = f"{PACKAGE}:id/expandable_list_tasks"
    task_id: str = f"{PACKAGE}:id/lbl_task_id"
    signature_tab: str = f"{PACKAGE}:id/layout_tab_content_signature"
    signature_skip: str = f"{PACKAGE}:id/layout_tab_content_signature_skip"
    method_list: str = f"{PACKAGE}:id/list_verification_method"
    method_label: str = f"{PACKAGE}:id/label_title"
    method_cancel: str = f"{PACKAGE}:id/button_cancel"


class VisitError(RuntimeError):
    """The visit screen could not be driven confidently."""


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


class VisitDetailScreen:
    def __init__(self, driver, selectors: VisitSelectors | None = None):
        self.driver = driver
        self.sel = selectors or VisitSelectors()

    def is_displayed(self) -> bool:
        try:
            if (self.driver.current_activity or "").endswith(ACTIVITY):
                return True
        except Exception:  # noqa: BLE001
            pass
        return bool(self.driver.find_elements(ID, self.sel.clock_in))

    # --------------------------------------------------------------- reading
    def _text(self, selector: str) -> str:
        found = self.driver.find_elements(ID, selector)
        return (found[0].text or "").strip() if found else ""

    def scheduled_window(self) -> tuple[str, str]:
        """What the visit was scheduled for — never conflated with confirmed."""
        return self._text(self.sel.start_time), self._text(self.sel.end_time)

    def confirmed_window(self) -> tuple[str, str]:
        """What the app has actually recorded. Empty until clocked."""
        return self._text(self.sel.confirmed_start), self._text(self.sel.confirmed_end)

    def clocked_in(self) -> bool:
        return bool(self.confirmed_window()[0])

    def task_ids(self) -> list[str]:
        """Care plan duty codes.

        Codes only. The descriptions alongside them are care information about a
        specific patient, and nothing here needs them to operate.
        """
        ids = []
        for el in self.driver.find_elements(ID, self.sel.task_id):
            raw = (el.text or "").strip()
            code = raw.split("-")[0].strip()
            if code:
                ids.append(code)
        return ids

    def has_signature_tab(self) -> bool:
        return bool(self.driver.find_elements(ID, self.sel.signature_tab))

    # -------------------------------------------------------- clock-in flow
    def open_verification_chooser(self) -> list[str]:
        """Tap clock-in and return the verification methods offered.

        Stops at the chooser. Selecting a method is what starts a real clock-in,
        and that decision does not belong to this function.
        """
        buttons = self.driver.find_elements(ID, self.sel.clock_in)
        if not buttons:
            raise VisitError("no clock-in control on this screen")
        buttons[0].click()
        return self.verification_methods()

    def verification_methods(self) -> list[str]:
        return [(el.text or "").strip()
                for el in self.driver.find_elements(ID, self.sel.method_label)]

    def chooser_is_open(self) -> bool:
        return bool(self.driver.find_elements(ID, self.sel.method_list))

    def cancel_verification(self) -> None:
        found = self.driver.find_elements(ID, self.sel.method_cancel)
        if not found:
            raise VisitError("no cancel control on the verification chooser")
        found[0].click()
        log.info("cancelled the verification chooser; nothing was recorded")

    def choose_verification(self, method: str) -> str:
        """Select a verification method by name.

        Deliberately explicit — there is no default. GPS and token evidence
        different things, and picking one silently would decide how this
        deployment proves presence.
        """
        if method not in METHOD_LABELS:
            raise VisitError(
                f"unknown verification method {method!r}; known: {sorted(METHOD_LABELS)}"
            )
        wordings = METHOD_LABELS[method]
        rows = self.driver.find_elements(ID, self.sel.method_label)
        matches = [el for el in rows
                   if any(w in _normalise(el.text) for w in wordings)]

        if not matches:
            offered = [(el.text or "").strip() for el in rows]
            raise VisitError(
                f"no verification method matching {method!r}; offered: {offered!r}"
            )
        if len(matches) > 1:
            raise VisitError(f"{len(matches)} methods match {method!r}; refusing to guess")

        label = (matches[0].text or "").strip()
        xpath = (
            f"//*[@resource-id='{self.sel.method_label}' and @text={_xpath_literal(label)}]"
            "/ancestor::*[@clickable='true'][1]"
        )
        targets = self.driver.find_elements(XPATH, xpath)
        if not targets:
            raise VisitError(f"found {label!r} but nothing clickable above it")
        targets[0].click()
        log.info("selected verification method %s", label)
        return label


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    parts = value.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"
