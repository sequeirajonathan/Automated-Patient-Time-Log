"""Reflow an Android screen into rows a phone-first page can render natively.

The first wireframe reproduced the screen's *geometry*: every node absolutely
positioned at the device's own coordinates, fonts scaled to fit their boxes.
It was faithful and it looked broken — overlapping headers, orphaned toggles,
text shrunk to fit rectangles that were never meant to be read at this width.
Geometry is the one thing about the source screen not worth keeping.

What is worth keeping is everything else: which controls exist, what they say,
what belongs beside what, and the order she reads them in. So this module
linearizes the screen — sorts the nodes into horizontal bands, folds labels
into the tappable rows that contain them, pairs adjacent small buttons into
segments — and hands the template semantic rows. The template renders them as
a native mobile list; the device's coordinates decide *order and grouping* and
are never used for placement again.

Tap identity is untouched. Every interactive item carries the same rid, class
and bounds the overlay always posted, and the server re-verifies them against
the published frame before anything reaches the phone. Reflowing changes
where a control is drawn on her page, never what tapping it means.
"""

from __future__ import annotations

# Widget classes with a meaning of their own. Anything else that is clickable
# is a container row — the shape Android lists are made of.
BUTTONS = ("Button", "ImageButton")
FIELDS = ("EditText", "AutoCompleteTextView", "SearchView")
TOGGLES = ("CheckBox", "Switch", "CompoundButton", "ToggleButton", "RadioButton")
IMAGES = ("ImageView",)
TEXTS = ("TextView",)

# Two nodes share a band when their vertical spans overlap by at least this
# share of the smaller one. Android rows are not perfectly aligned; half is
# enough to keep a label with its toggles without merging neighbouring rows.
BAND_OVERLAP = 0.5

# A pair of small same-sized buttons sitting flush against each other is a
# segmented choice (the care plan's ✓/✗ pairs). "Small" is relative to the
# screen, "flush" is a gap smaller than this share of the screen width.
SEGMENT_MAX_WIDTH = 0.18
SEGMENT_MAX_GAP = 0.02

# The top of the screen, where an app keeps its own navigation bar.
NAV_BAND_BOTTOM = 0.12


def _is_icon_text(txt: str) -> bool:
    """Text that is an icon font's private glyph, not a word.

    Android apps ship icon fonts whose characters live in Unicode's private
    use area; the dump hands them over as text and rendering them raw shows a
    tofu box or a stray hamburger where the app showed a drawn icon. Words are
    words; glyphs are decoration.
    """
    return bool(txt) and all(
        0xE000 <= ord(c) <= 0xF8FF or c.isspace() for c in txt)


def _fold_label(cell_b: list[int], s: dict) -> tuple[str, str]:
    """How a label folded into a cell should be carried: as a line, a badge,
    or not at all.

    A short number's meaning is its position, learned from the real agency
    home screen: on the left it decorates an icon (the day inside the calendar
    glyph — the date is already in the subtitle); on the right it is a count
    riding in a bubble. Words are lines wherever they sit.
    """
    txt = s.get("txt", "")
    if _is_icon_text(txt):
        return "drop", ""
    if len(txt) <= 3 and txt.strip().isdigit():
        cx = (s["b"][0] + s["b"][2]) / 2
        width = cell_b[2] - cell_b[0] or 1
        share = (cx - cell_b[0]) / width
        if share < 0.33:
            return "drop", ""
        if share > 0.6:
            return "badge", txt.strip()
    return "line", txt


def _kind(cls: str) -> str:
    if cls in BUTTONS:
        return "button"
    if cls in FIELDS:
        return "field"
    if cls in TOGGLES:
        return "toggle"
    if cls in IMAGES:
        return "image"
    if cls in TEXTS:
        return "text"
    return "row"


def _effective_size(doc: dict) -> tuple[int, int]:
    """The screen's real orientation, inferred from the bounds themselves.

    The app forces landscape on some screens while `wm size` keeps reporting
    portrait; the coordinates are what everything here sorts by, so they are
    also what decides which way up the screen is.
    """
    w, h = (doc.get("size") or [0, 0])[:2]
    everything = (doc.get("elements") or []) + (doc.get("statics") or [])
    max_x = max((e["b"][2] for e in everything if e.get("b")), default=0)
    if max_x > w and max_x <= max(w, h):
        w, h = h, w
    return w, h


def _contains(outer: list[int], inner: list[int]) -> bool:
    ox1, oy1, ox2, oy2 = outer
    ix1, iy1, ix2, iy2 = inner
    # Centre containment rather than full: Android labels routinely overhang
    # their row by a pixel or two of padding.
    cx, cy = (ix1 + ix2) / 2, (iy1 + iy2) / 2
    return ox1 <= cx <= ox2 and oy1 <= cy <= oy2


def _overlap(a: list[int], b: list[int]) -> float:
    top = max(a[1], b[1])
    bottom = min(a[3], b[3])
    if bottom <= top:
        return 0.0
    smaller = min(a[3] - a[1], b[3] - b[1]) or 1
    return (bottom - top) / smaller


def _item(node: dict, kind: str) -> dict:
    out = {
        "kind": kind,
        "txt": node.get("txt", ""),
        "checked": bool(node.get("checked")),
        "focused": bool(node.get("focused")),
        "b": node["b"],
    }
    if "rid" in node:
        out["aim"] = {"rid": node.get("rid", ""), "cls": node.get("cls", ""),
                      "b": node["b"]}
    return out


def build(doc: dict) -> dict | None:
    """The semantic model: a nav bar (maybe) and a list of rows."""
    w, h = _effective_size(doc)
    if not w or not h:
        return None

    elements = [e for e in doc.get("elements") or [] if e.get("b")]
    statics = [s for s in doc.get("statics") or [] if s.get("b")]

    # Labels inside a tappable container belong to it: they are the row's own
    # text, not free-floating captions. This is what turns "an unlabelled
    # rectangle above three orphaned words" back into a list cell.
    containers = [e for e in elements if _kind(e.get("cls", "")) == "row"]
    folded: set[int] = set()
    labels: dict[int, list[dict]] = {}
    for i, s in enumerate(statics):
        for c in containers:
            if _contains(c["b"], s["b"]):
                labels.setdefault(id(c), []).append(s)
                folded.add(i)
                break

    items: list[dict] = []
    for e in elements:
        kind = _kind(e.get("cls", ""))
        item = _item(e, kind)
        # A narrow button is a utility — a badge, a hamburger, an info dot —
        # and rendering it at full width promotes it to a call to action it
        # never was. Width relative to the screen is the only signal needed.
        item["small"] = (e["b"][2] - e["b"][0]) <= w * 0.22
        if _is_icon_text(item["txt"]):
            item["txt"] = ""       # a drawn icon, not a word; see _is_icon_text
        if kind == "row":
            own = sorted(labels.get(id(e), []),
                         key=lambda s: (s["b"][1], s["b"][0]))
            lines, badge = [], ""
            for s in own:
                how, value = _fold_label(e["b"], s)
                if how == "line" and value:
                    lines.append(value)
                elif how == "badge":
                    badge = value
            item["lines"] = lines
            item["badge"] = badge
        items.append(item)
    for i, s in enumerate(statics):
        if i not in folded and s.get("txt") and not _is_icon_text(s["txt"]):
            items.append(_item(s, "label"))

    items.sort(key=lambda n: (n["b"][1], n["b"][0]))

    # ------------------------------------------------------------------ bands
    bands: list[list[dict]] = []
    for item in items:
        if bands and any(_overlap(item["b"], other["b"]) >= BAND_OVERLAP
                         for other in bands[-1]):
            bands[-1].append(item)
        else:
            bands.append([item])
    for band in bands:
        band.sort(key=lambda n: n["b"][0])

    # --------------------------------------------------------------- segments
    for band in bands:
        _pair_segments(band, w)

    # -------------------------------------------------------------------- nav
    nav = None
    if bands and all(n["b"][3] <= h * NAV_BAND_BOTTOM for n in bands[0]):
        first = bands[0]
        buttons = [n for n in first if n.get("aim")]
        titles = [n for n in first if not n.get("aim") and n.get("txt")]
        if buttons:
            title = max(titles, key=lambda n: len(n["txt"]), default=None)
            nav = {
                "back": buttons[0],
                "title": title["txt"] if title else "",
                "trailing": buttons[1:],
            }
            bands = bands[1:]

    rows = [{"items": band} for band in bands]
    return {"id": doc.get("id", ""), "nav": nav, "rows": rows,
            "notice": doc.get("notice", ""), "blocked": doc.get("blocked", "")}


def _pair_segments(band: list[dict], width: int) -> None:
    """Fuse runs of small flush buttons into one segmented control, in place."""
    out: list[dict] = []
    run: list[dict] = []

    def flush() -> None:
        if len(run) >= 2:
            out.append({"kind": "segment", "b": [run[0]["b"][0], run[0]["b"][1],
                                                 run[-1]["b"][2], run[-1]["b"][3]],
                        "parts": list(run)})
        else:
            out.extend(run)
        run.clear()

    for item in band:
        small = (item["b"][2] - item["b"][0]) <= width * SEGMENT_MAX_WIDTH
        pairable = item.get("aim") and small and item["kind"] in ("button",
                                                                  "toggle")
        if pairable and run:
            gap = item["b"][0] - run[-1]["b"][2]
            same_size = abs((item["b"][2] - item["b"][0])
                            - (run[-1]["b"][2] - run[-1]["b"][0])) \
                <= width * 0.04
            if gap <= width * SEGMENT_MAX_GAP and same_size:
                run.append(item)
                continue
            flush()
        if pairable:
            run.append(item)
        else:
            flush()
            out.append(item)
    flush()
    band[:] = out
