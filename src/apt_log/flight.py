"""A flight recorder for screens nobody was watching.

Every layout rule the renderer has — badges, icon glyphs, folded labels — came
from someone screenshotting an ugly screen and the rule being written down.
That loop needs the screenshot, which needs someone watching at the moment the
screen was up. Three apps have never been opened, and their screens will mostly
appear while nobody is.

So the feed keeps a rolling record of every distinct screen *structure* it
sees. Structure, never words: classes, ids, bounds, states, and the **length**
of each text — because layout is made of lengths, and a length is not a name.
An entry can be replayed through the renderer offline with synthesized text of
the right shape, and a screen that rendered badly at 2 PM can be fixed at
midnight without anyone having been there.

This file is frame.json-class data: loggable, textless, safe to paste into a
console. That is a design constraint, not a habit — the whole point is that it
gets read and shared freely during tuning.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

STATE_DIR = Path("/var/lib/aptlog")
FLIGHT_PATH = STATE_DIR / "flight.jsonl"

# Enough for weeks of distinct screens across four apps; small enough that the
# file stays a casual read.
MAX_ENTRIES = 200

# Structures recorded this process-lifetime. Restarts re-record at most one
# duplicate per structure, and the cap absorbs that.
_SEEN: set[str] = set()


def strip(doc: dict) -> dict:
    """A screen document reduced to structure. The only lossy step, on purpose.

    Text becomes its length. Everything else the renderer's rules key on —
    class, id, bounds, checked, focused — survives untouched.
    """
    def element(e: dict) -> dict:
        out = {"rid": e.get("rid", ""), "cls": e.get("cls", ""),
               "b": e.get("b"), "checked": bool(e.get("checked")),
               "focused": bool(e.get("focused")),
               "len": len(e.get("txt", "") or "")}
        return out

    return {
        "at": doc.get("at", ""),
        "app": doc.get("app", ""),
        # An activity class name is app code, not anything anyone typed —
        # and it is the key a future atlas row needs.
        "activity": doc.get("activity", ""),
        "screen": doc.get("screen", ""),
        "blocked": doc.get("blocked", ""),
        "id": doc.get("id", ""),
        "size": doc.get("size", [0, 0]),
        "elements": [element(e) for e in doc.get("elements") or []],
        "statics": [{"cls": s.get("cls", ""), "b": s.get("b"),
                     "len": len(s.get("txt", "") or "")}
                    for s in doc.get("statics") or []],
    }


def record(doc: dict, path: Path | None = None,
           seen: set[str] | None = None) -> bool:
    """Record a screen's structure if it has not been seen. True if written."""
    target = path or FLIGHT_PATH
    known = seen if seen is not None else _SEEN

    entry = strip(doc)
    if not entry["id"] or not entry["elements"] and not entry["statics"]:
        return False
    if entry["id"] in known:
        return False
    known.add(entry["id"])

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        _trim(target)
    except OSError as exc:
        log.warning("flight recorder cannot write (%s)", exc)
        return False
    return True


def _trim(target: Path) -> None:
    """Keep the newest MAX_ENTRIES. Oldest structures age out first — they are
    also the ones most likely to have been tuned already."""
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= MAX_ENTRIES:
        return
    tmp = target.with_suffix(".tmp")
    tmp.write_text("\n".join(lines[-MAX_ENTRIES:]) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def entries(path: Path | None = None) -> list[dict]:
    target = path or FLIGHT_PATH
    out = []
    try:
        for line in target.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


def replay(entry: dict) -> dict:
    """A recorded structure as a renderable screen document.

    Text is synthesized to the recorded lengths — layout is made of lengths,
    and 'xxxxx xxx' wraps where 'Horario para' wrapped. This is what lets a
    screen seen once at 2 PM be re-rendered and tuned at midnight.
    """
    def words(n: int) -> str:
        if n <= 0:
            return ""
        # Break the run into word-ish chunks so wrapping behaves like prose.
        out, remaining = [], n
        while remaining > 0:
            chunk = min(remaining, 7)
            out.append("x" * chunk)
            remaining -= chunk + 1
        return " ".join(out)[:n]

    return {
        "id": entry.get("id", ""),
        "at": entry.get("at", ""),
        "app": entry.get("app", ""),
        "activity": entry.get("activity", ""),
        "screen": entry.get("screen", ""),
        "blocked": entry.get("blocked", ""),
        "notice": "",
        "size": entry.get("size", [0, 0]),
        "elements": [{**e, "txt": words(e.get("len", 0)),
                      "has_text": bool(e.get("len"))}
                     for e in entry.get("elements") or []],
        "statics": [{**s, "txt": words(s.get("len", 0))}
                    for s in entry.get("statics") or []],
    }
