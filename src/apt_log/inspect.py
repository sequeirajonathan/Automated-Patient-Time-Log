"""Screen inspection that cannot leak patient data by accident.

Building a page object needs the shape of a screen: which resource-ids exist, how
many of each, what is clickable, what nests inside what. It almost never needs
the text, and on this app the text is patient names and home addresses.

So text is **off by default** and has to be asked for per resource-id. Ids that
look like they carry identifying information are refused even when asked for,
because the failure mode of a redactor is silent and one-directional: nobody
notices a name that should not have been printed until it is already in a
transcript.

The output is meant to be safe to paste anywhere — a chat, an issue, a commit
message.
"""

from __future__ import annotations

import html
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Substrings that mark an id as carrying identifying data. Matched loosely on
# purpose: a new screen adding lbl_patient_phone should be caught without anyone
# remembering to update this.
PHI_ID_MARKERS = (
    "patient", "name", "address", "phone", "dob", "birth", "ssn", "email",
    "member", "client", "contact", "emergency", "caregiver", "note", "comment",
)

REDACTED = "<redacted>"


def looks_like_phi(resource_id: str) -> bool:
    lowered = resource_id.lower()
    return any(marker in lowered for marker in PHI_ID_MARKERS)


# Two or more consecutive all-caps words. This app renders patient names that way
# ("CARIDAD ROJAS") while its own chrome is title case ("Detalle de Visita",
# "Registrar Entrada"), so the shape separates data from label reasonably well.
_SHOUTED_NAME = re.compile(r"\b[^\W\d_]{2,}\b(?:\s+\b[^\W\d_]{2,}\b)+")


def _looks_like_a_name(text: str) -> bool:
    for candidate in _SHOUTED_NAME.findall(text):
        if candidate.isupper():
            return True
    return False


def scrub_text(text: str) -> tuple[str, bool]:
    """Reduce a revealed value to the part that is safe to show.

    Learned the hard way: `lbl_header` carries the agency name on one screen and
    "Detalle de Visita\\n<PATIENT>" on another. The id is identical, so an
    id-based rule cannot tell them apart — the content has to be looked at too.

    Two reductions, both conservative:

    - keep only the first line. Where this app combines a label with a datum it
      separates them with a newline, and the label is the part worth seeing.
    - redact entirely if what remains looks like a shouted proper name.

    Returns (text, was_reduced).
    """
    first, sep, _rest = text.partition("\n")
    reduced = bool(sep)
    first = first.strip()

    if _looks_like_a_name(first):
        return REDACTED, True
    return first, reduced


@dataclass
class Node:
    cls: str
    resource_id: str
    clickable: bool
    text: str | None          # None when withheld
    checked: str = ""
    bounds: str = ""


@dataclass
class ScreenReport:
    activity: str
    node_count: int
    nodes: list[Node] = field(default_factory=list)
    withheld: set[str] = field(default_factory=set)
    refused: set[str] = field(default_factory=set)
    reduced: set[str] = field(default_factory=set)

    def render(self) -> str:
        counts = Counter((n.cls, n.resource_id, n.clickable) for n in self.nodes)

        # Collapsing rows that share a key is what makes a long list readable,
        # but collapsing their *text* hid the second entry of a two-item chooser
        # — the tool reported "GPS" and silently dropped "token de seguridad".
        # Distinct values are kept and shown together.
        texts: dict[tuple, list[str]] = {}
        for n in self.nodes:
            key = (n.cls, n.resource_id, n.clickable)
            value = REDACTED if n.text is None else (n.text or "")
            bucket = texts.setdefault(key, [])
            if value and value not in bucket:
                bucket.append(value)

        lines = [
            f"activity : {self.activity}",
            f"nodes    : {self.node_count}",
            "",
            f"  {'CLASS':<22} {'RESOURCE-ID':<30} {'CLK':<5} {'N':<4} TEXT",
        ]
        seen: set[tuple] = set()
        for n in self.nodes:
            key = (n.cls, n.resource_id, n.clickable)
            if key in seen:
                continue
            seen.add(key)
            values = texts.get(key) or ["-"]
            shown = " | ".join(v[:34] for v in values[:4])
            if len(values) > 4:
                shown += f" | (+{len(values) - 4} more)"
            lines.append(
                f"  {n.cls:<22} {n.resource_id:<30} "
                f"{str(n.clickable):<5} {counts[key]:<4} {shown}"
            )
        if self.refused:
            lines += ["", "text refused for ids that look identifying:",
                      "  " + ", ".join(sorted(self.refused))]
        if self.reduced:
            lines += ["", "text reduced (multi-line or name-shaped):",
                      "  " + ", ".join(sorted(self.reduced))]
        if self.withheld:
            lines += ["", "text withheld (not requested):",
                      "  " + ", ".join(sorted(self.withheld))]
        return "\n".join(lines)


def _attr(node: str, name: str) -> str:
    """Attribute value with XML entities decoded.

    Appium returns page_source as XML, so a newline inside a label arrives as
    `&#10;` rather than a literal. Without decoding, the label/datum split in
    scrub_text never fires — it redacted the right thing for the wrong reason,
    and would have thrown away the useful half.
    """
    m = re.search(rf'{name}="([^"]*)"', node)
    return html.unescape(m.group(1)) if m else ""


def inspect_source(
    page_source: str,
    activity: str = "",
    text_for: tuple[str, ...] = (),
    allow_phi_text: bool = False,
) -> ScreenReport:
    """Summarise a page source, withholding text unless explicitly requested.

    `text_for` holds short resource-id suffixes, e.g. ("lbl_schedule_start_time",).
    """
    wanted = {t.split("/")[-1] for t in text_for}
    report = ScreenReport(activity=activity, node_count=0)

    for raw in re.findall(r"<[^>]+>", page_source):
        cls = _attr(raw, "class")
        if not cls:
            continue
        report.node_count += 1
        rid_full = _attr(raw, "resource-id")
        rid = rid_full.split("/")[-1]
        text = _attr(raw, "text")

        shown: str | None = None
        if text:
            if rid in wanted or "*" in wanted:
                if looks_like_phi(rid) and not allow_phi_text:
                    report.refused.add(rid or "<no id>")
                elif allow_phi_text:
                    shown = text
                else:
                    scrubbed, reduced = scrub_text(text)
                    shown = scrubbed
                    if reduced:
                        report.reduced.add(rid or "<no id>")
            else:
                report.withheld.add(rid or "<no id>")

        if not rid and not text and not _attr(raw, "clickable") == "true":
            continue

        report.nodes.append(Node(
            cls=cls.replace("android.widget.", "").replace("android.view.", ""),
            resource_id=rid,
            clickable=_attr(raw, "clickable") == "true",
            text=shown,
            checked=_attr(raw, "checked"),
            bounds=_attr(raw, "bounds"),
        ))
    return report


def inspect_driver(driver, **kwargs) -> ScreenReport:
    try:
        activity = driver.current_activity or ""
    except Exception:  # noqa: BLE001
        activity = "<unavailable>"
    return inspect_source(driver.page_source, activity=activity, **kwargs)
