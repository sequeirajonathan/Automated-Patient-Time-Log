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

    def render(self) -> str:
        counts = Counter((n.cls, n.resource_id, n.clickable) for n in self.nodes)
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
            shown = REDACTED if n.text is None else (n.text[:34] or "-")
            lines.append(
                f"  {n.cls:<22} {n.resource_id:<30} "
                f"{str(n.clickable):<5} {counts[key]:<4} {shown}"
            )
        if self.refused:
            lines += ["", "text refused for ids that look identifying:",
                      "  " + ", ".join(sorted(self.refused))]
        if self.withheld:
            lines += ["", "text withheld (not requested):",
                      "  " + ", ".join(sorted(self.withheld))]
        return "\n".join(lines)


def _attr(node: str, name: str) -> str:
    m = re.search(rf'{name}="([^"]*)"', node)
    return m.group(1) if m else ""


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
                else:
                    shown = text
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
