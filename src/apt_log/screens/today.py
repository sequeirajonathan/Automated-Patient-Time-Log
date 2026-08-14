"""Today's schedule — the visit list.

The app models a visit exactly the way REQ-7 does: `lbl_schedule_start_time` is
when the visit was *supposed* to happen, `lbl_visit_start_time` is when it
actually got recorded. They are separate fields here for the same reason they are
separate fields in the audit log, and neither may be read as the other.

`lbl_visit_start_time` being empty means the visit has not been clocked in. That
is an idempotency signal from the agency's own system, and a better one than the
local database: it survives losing `/var/lib/aptlog`, and it reflects what the
agency actually has rather than what this device believes it sent.

**Patient names are PHI.** They are read here because a row cannot be found
without them, held only long enough to match, and never logged or stored — the
audit trail carries an identifier (REQUIREMENTS §5). `redact()` exists so a name
that must appear in a message appears as initials.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

ID = "id"
XPATH = "xpath"

PACKAGE = "com.hhaexchange.caregiver"
ACTIVITY = ".TodayScheduleActivity"


@dataclass
class TodaySelectors:
    list_view: str = f"{PACKAGE}:id/list_today_schedule"
    patient_name: str = f"{PACKAGE}:id/lbl_patient_name"
    date: str = f"{PACKAGE}:id/lbl_date"
    address: str = f"{PACKAGE}:id/lbl_address"
    schedule_start: str = f"{PACKAGE}:id/lbl_schedule_start_time"
    schedule_end: str = f"{PACKAGE}:id/lbl_schedule_end_time"
    visit_start: str = f"{PACKAGE}:id/lbl_visit_start_time"
    visit_end: str = f"{PACKAGE}:id/lbl_visit_end_time"
    header: str = f"{PACKAGE}:id/lbl_header"


class TodayScheduleError(RuntimeError):
    """A visit row could not be identified unambiguously."""


def redact(name: str) -> str:
    """Initials only. Enough to correlate a log line, not enough to identify."""
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    return "".join(p[0].upper() for p in parts) or "?"


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


@dataclass
class VisitRow:
    index: int
    patient_name: str            # transient; never persisted
    scheduled_start: str
    scheduled_end: str
    visit_start: str             # empty until clocked in
    visit_end: str               # empty until clocked out

    @property
    def clocked_in(self) -> bool:
        return bool(self.visit_start.strip())

    @property
    def clocked_out(self) -> bool:
        return bool(self.visit_end.strip())

    def __repr__(self) -> str:  # keeps names out of tracebacks and logs
        return (f"VisitRow(index={self.index}, patient={redact(self.patient_name)}, "
                f"scheduled={self.scheduled_start!r}, in={self.clocked_in}, "
                f"out={self.clocked_out})")


class TodayScheduleScreen:
    def __init__(self, driver, selectors: TodaySelectors | None = None):
        self.driver = driver
        self.sel = selectors or TodaySelectors()

    def is_displayed(self) -> bool:
        try:
            if (self.driver.current_activity or "").endswith(ACTIVITY):
                return True
        except Exception:  # noqa: BLE001
            pass
        return bool(self.driver.find_elements(ID, self.sel.list_view))

    def _texts(self, selector: str) -> list[str]:
        return [(el.text or "") for el in self.driver.find_elements(ID, selector)]

    def visits(self) -> list[VisitRow]:
        """Every row on screen.

        The per-field lists are positionally aligned by the app's own layout. If
        they ever disagree in length the rows cannot be reassembled reliably, so
        that raises rather than zipping to the shortest and silently dropping a
        visit.
        """
        names = self._texts(self.sel.patient_name)
        starts = self._texts(self.sel.schedule_start)
        ends = self._texts(self.sel.schedule_end)
        v_starts = self._texts(self.sel.visit_start)
        v_ends = self._texts(self.sel.visit_end)

        lengths = {len(names), len(starts), len(ends), len(v_starts), len(v_ends)}
        if len(lengths) > 1:
            raise TodayScheduleError(
                f"visit fields disagree in count {sorted(lengths)} — refusing to "
                "pair them up, a mismatched row would attribute one patient's "
                "time to another"
            )

        return [
            VisitRow(i, names[i], starts[i], ends[i], v_starts[i], v_ends[i])
            for i in range(len(names))
        ]

    def find(self, patient_name: str,
             scheduled_start: str | None = None) -> VisitRow:
        """Locate a visit by patient name and scheduled slot (REQ-4).

        Never by position: list order follows the agency's scheduling and can
        change between runs, so an index points at a different visit on a
        different day.

        **A patient can appear more than once.** Today's real schedule holds two
        back-to-back visits for the same person, which is ordinary in home care.
        Name alone is therefore not a unique key — the key is (patient, slot),
        matching the UNIQUE constraint REQ-4 puts on the local store. When a name
        matches several rows and no slot is given, this refuses rather than
        picking one, because clocking into the wrong hour of the right patient is
        still the wrong record.
        """
        wanted = _normalise(patient_name)
        rows = self.visits()

        matches = [r for r in rows if wanted == _normalise(r.patient_name)]
        if not matches:
            matches = [r for r in rows if wanted in _normalise(r.patient_name)]

        if not matches:
            raise TodayScheduleError(
                f"no visit for {redact(patient_name)} among {len(rows)} rows "
                "— refusing to guess"
            )

        if scheduled_start is not None:
            slot = _normalise(scheduled_start)
            slotted = [r for r in matches if _normalise(r.scheduled_start) == slot]
            if not slotted:
                offered = [r.scheduled_start for r in matches]
                raise TodayScheduleError(
                    f"{redact(patient_name)} has no visit scheduled at "
                    f"{scheduled_start!r}; scheduled slots today: {offered!r}"
                )
            if len(slotted) > 1:
                raise TodayScheduleError(
                    f"{len(slotted)} visits for {redact(patient_name)} share the "
                    f"slot {scheduled_start!r}; refusing to guess"
                )
            return slotted[0]

        if len(matches) > 1:
            offered = [r.scheduled_start for r in matches]
            raise TodayScheduleError(
                f"{redact(patient_name)} has {len(matches)} visits today "
                f"({offered!r}) — a scheduled start time is required to tell them "
                "apart. Clocking into the wrong hour of the right patient is "
                "still the wrong record."
            )
        return matches[0]

    def open(self, patient_name: str,
             scheduled_start: str | None = None) -> VisitRow:
        """Tap a visit row via its nearest clickable ancestor.

        The row's own fields are inert, as on the home menu.
        """
        row = self.find(patient_name, scheduled_start)

        # Match the card that contains BOTH the name and the slot. Keying on the
        # name alone would return two elements when a patient has back-to-back
        # visits, and taking the first would tap whichever the layout happened to
        # render first.
        predicates = [
            f".//*[@resource-id='{self.sel.patient_name}' "
            f"and @text={_xpath_literal(row.patient_name)}]",
            f".//*[@resource-id='{self.sel.schedule_start}' "
            f"and @text={_xpath_literal(row.scheduled_start)}]",
        ]
        xpath = "//*[@clickable='true'][" + " and ".join(predicates) + "]"

        found = self.driver.find_elements(XPATH, xpath)
        if not found:
            raise TodayScheduleError(
                f"found the row for {redact(patient_name)} at "
                f"{row.scheduled_start} but nothing clickable contains it — "
                "the layout has changed"
            )
        if len(found) > 1:
            raise TodayScheduleError(
                f"{len(found)} clickable cards contain {redact(patient_name)} at "
                f"{row.scheduled_start}; refusing to guess which to tap"
            )
        found[0].click()
        log.info("opened visit for %s scheduled %s",
                 redact(row.patient_name), row.scheduled_start)
        return row


def _xpath_literal(value: str) -> str:
    """Quote safely; names contain apostrophes (O'Brien)."""
    if "'" not in value:
        return f"'{value}'"
    parts = value.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"
