"""The home menu, reached after agency selection.

Every row on this screen shares the same resource-ids — `lbl_name`, `lbl_count`,
`lbl_description` repeat for each card — so rows can only be distinguished by
their label text. That is the one discriminator available, and it is
language-dependent, which is why each entry carries both Spanish and English
wording. Development runs Spanish here; matching only one language would work on
this device and fail on another.

The labels are also not the tappable element. Nothing with `lbl_name` reports
clickable=true; the touch target is an ancestor container, so opening a row means
walking up to the nearest clickable ancestor rather than tapping the text.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

ID = "id"
XPATH = "xpath"

PACKAGE = "com.hhaexchange.caregiver"
ACTIVITY = ".HomeActivity"


@dataclass
class HomeSelectors:
    scroll: str = f"{PACKAGE}:id/scroll_list_home"
    name: str = f"{PACKAGE}:id/lbl_name"
    description: str = f"{PACKAGE}:id/lbl_description"
    count: str = f"{PACKAGE}:id/lbl_count"
    header: str = f"{PACKAGE}:id/lbl_header"


# Semantic key -> the wordings that identify it. Spanish is what this deployment
# renders; English is included so the same code works on an English device.
MENU_LABELS: dict[str, tuple[str, ...]] = {
    "today_schedule": ("horario para hoy", "today's schedule", "schedule for today"),
    "unscheduled_visit": ("visita no programada", "unscheduled visit"),
    "visits": ("visitas", "visits"),
    "patients": ("pacientes", "patients"),
    "messages": ("mensajes", "messages"),
    "availability": ("mi disponibilidad", "my availability"),
}


class HomeError(RuntimeError):
    """The requested menu row could not be identified unambiguously."""


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


@dataclass
class MenuRow:
    key: str | None
    label: str
    description: str
    count: int | None


class HomeScreen:
    def __init__(self, driver, selectors: HomeSelectors | None = None):
        self.driver = driver
        self.sel = selectors or HomeSelectors()

    def is_displayed(self) -> bool:
        try:
            if (self.driver.current_activity or "").endswith(ACTIVITY):
                return True
        except Exception:  # noqa: BLE001
            pass
        return bool(self.driver.find_elements(ID, self.sel.scroll))

    def agency_label(self) -> str:
        """The header echoes the selected agency — a cheap check that the right
        employer is in context before anything is recorded against it."""
        found = self.driver.find_elements(ID, self.sel.header)
        return (found[0].text or "").strip() if found else ""

    # ------------------------------------------------------------------ rows
    def _label_elements(self):
        return self.driver.find_elements(ID, self.sel.name)

    def _key_for(self, label: str) -> str | None:
        norm = _normalise(label)
        for key, wordings in MENU_LABELS.items():
            if norm in wordings:
                return key
        return None

    def rows(self) -> list[MenuRow]:
        """Everything on the menu, for logging and for a useful error message."""
        names = [(el.text or "").strip() for el in self._label_elements()]
        descs = [(el.text or "").strip()
                 for el in self.driver.find_elements(ID, self.sel.description)]
        out: list[MenuRow] = []
        for i, label in enumerate(names):
            out.append(MenuRow(
                key=self._key_for(label),
                label=label,
                description=descs[i] if i < len(descs) else "",
                count=None,
            ))
        return out

    def today_visit_count(self) -> int | None:
        """The badge on the today's-schedule row.

        Worth reading before opening it: a count of zero means there is nothing
        to do, and knowing that without navigating avoids touching a list at all.
        """
        found = self.driver.find_elements(ID, self.sel.count)
        for el in found:
            raw = (el.text or "").strip()
            if raw.isdigit():
                return int(raw)
        return None

    def open(self, key: str) -> str:
        """Tap the row identified by `key`, via its nearest clickable ancestor."""
        if key not in MENU_LABELS:
            raise HomeError(f"unknown menu key {key!r}; known: {sorted(MENU_LABELS)}")

        matches = [el for el in self._label_elements()
                   if self._key_for(el.text or "") == key]

        if not matches:
            raise HomeError(
                f"no menu row matching {key!r}. Present: "
                f"{[r.label for r in self.rows()]!r}. Refusing to guess — the "
                "labels are language-dependent and may need a new wording."
            )
        if len(matches) > 1:
            raise HomeError(f"{len(matches)} rows match {key!r}; refusing to guess")

        label = (matches[0].text or "").strip()
        target = self._clickable_ancestor(label)
        target.click()
        log.info("opened menu row %s (%s)", key, label)
        return label

    def _clickable_ancestor(self, label: str):
        """The labels are inert; the card around them takes the tap."""
        xpath = (
            f"//*[@resource-id='{self.sel.name}' and @text={_xpath_literal(label)}]"
            "/ancestor::*[@clickable='true'][1]"
        )
        found = self.driver.find_elements(XPATH, xpath)
        if not found:
            raise HomeError(
                f"found the row {label!r} but nothing clickable above it — "
                "the layout has changed"
            )
        return found[0]


def _xpath_literal(value: str) -> str:
    """Quote safely: agency and menu labels contain apostrophes."""
    if "'" not in value:
        return f"'{value}'"
    parts = value.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"
