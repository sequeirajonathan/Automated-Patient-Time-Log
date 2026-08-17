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

# The top of the screen, where an app keeps its own navigation bar: the
# first band is a nav when it STARTS at the very top, ENDS above the
# content, and every tappable in it is a small utility button — a back
# chevron, an info dot, a badge — never a wide call to action. The band
# bottom alone was the old test, and each app's header missed it by its
# own margin: the agency picker's title block at 0.123, the legacy home's
# logo-and-agency block at 0.19, both rendered as boxed rows instead of
# title bars. Smallness is what actually separates chrome from content.
NAV_TOP_MAX = 0.12
NAV_BAND_BOTTOM = 0.20

# A tappable container this flat is a scrim edge or a divider — a 5px strip
# nobody could hit with a finger on the real phone — not a row. Rendering
# one produced a full-height empty cell with a chevron pointing at nothing.
SLIVER_MAX_HEIGHT = 0.02

# An anonymous container spanning at least this share of the screen's height
# is a curtain — a slide-over's scrim, a drawer edge — not a row.
CURTAIN_MIN_HEIGHT = 0.55

# Orphaned list dots. The reflow keeps a list's items, not its typography.
BULLETS = {"•", "·", "◦", "‣", "∙", "*", "-"}

# A loose label this short reads as a section heading; longer is prose. The
# header treatment (uppercase, small, muted) applied to whole paragraphs
# turned the migration pitch into a wall of shouting.
HEADER_MAX_CHARS = 32


def _is_icon_text(txt: str) -> bool:
    """Text that is an icon font's private glyph, not a word.

    Android apps ship icon fonts whose characters live in Unicode's private
    use area; the dump hands them over as text and rendering them raw shows a
    tofu box or a stray hamburger where the app showed a drawn icon. Words are
    words; glyphs are decoration.
    """
    return bool(txt) and all(
        0xE000 <= ord(c) <= 0xF8FF or c.isspace() for c in txt)


# Icon glyphs that are STATE, not decoration. The visits list marks each
# EVV check-in and check-out with a drawn check, shipped as an icon font's
# private character — dropping it with the chevrons and hamburgers erased
# the one thing the row exists to say. Known state glyphs translate to real
# symbols; everything else in the private use area stays decoration.
ICON_MARKS = {
    "\uf00c": "\u2713",   # check
    "\uf058": "\u2713",   # circled check
    "\uf00d": "\u2715",   # cross
    "\uf057": "\u2715",   # circled cross
    "\uf071": "\u26a0",   # warning triangle
}

# Glyphs that NAME A BUTTON rather than mark state. On a clickable these
# keep their meaning (the agency picker's info button rendered as an empty
# box until its glyph translated); as loose statics they stay decoration \u2014
# an \u2139 beside a paragraph says nothing the paragraph doesn't.
ICON_LABELS = {
    **ICON_MARKS,
    "\uf059": "?",        # circled question \u2014 the info button
    "\uf05a": "\u2139",   # circled info
}

# Some apps draw their state marks as ImageViews instead \u2014 no text at all,
# identity carried by the resource-id alone. The legacy visits list shows a
# drawable check per verified EVV record under exactly these ids; the image
# only exists on rows where the record is confirmed.
IMAGE_MARKS = {
    "imgstarttime": "\u2713",
    "imgendtime": "\u2713",
}


def _mark_for(s: dict) -> str:
    """The state symbol a static stands for, or ''. Text glyphs and named
    state images are the two shapes marks arrive in."""
    txt = (s.get("txt") or "").strip()
    if txt:
        return ICON_MARKS.get(txt, "")
    return IMAGE_MARKS.get((s.get("rid") or "").lower(), "")


def _fold_label(cell_b: list[int], s: dict) -> tuple[str, str]:
    """How a label folded into a cell should be carried: as a line, a badge,
    or not at all.

    A short number's meaning is its position, learned from the real agency
    home screen: on the left it decorates an icon (the day inside the calendar
    glyph — the date is already in the subtitle); on the right it is a count
    riding in a bubble. Words are lines wherever they sit.
    """
    txt = s.get("txt", "")
    mark = _mark_for(s)
    if mark:
        return "mark", mark
    if not txt:
        return "drop", ""      # an unrecognised image: decoration
    if txt.strip() in BULLETS:
        # A lone list dot: the list's structure does not survive the
        # reflow, and the orphaned glyph rendered as a row of its own
        # with a ten-character gap where flex stretched it.
        return "drop", ""
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
        # The aim carries the bounds AS CAPTURED at the element's scroll
        # step — tap truth — while node["b"] may be the virtual page
        # position — layout truth. A below-the-fold aim also carries its
        # step, so the tap machinery knows to replay the scroll first.
        out["aim"] = {"rid": node.get("rid", ""), "cls": node.get("cls", ""),
                      "b": node.get("aim_b") or node["b"]}
        if node.get("step"):
            out["aim"]["step"] = node["step"]
    return out


def build(doc: dict) -> dict | None:
    """The semantic model: a nav bar (maybe) and a list of rows."""
    w, h = _effective_size(doc)
    if not w or not h:
        return None

    # Stitched documents carry two coordinate systems: `vb` is the item's
    # place on the page as a whole (layout truth, what everything below
    # sorts and groups by) and `b` is where it sat on the device when
    # captured (tap truth, preserved in aim_b). Viewport documents have
    # only `b`, and the two roles coincide.
    elements = [dict(e, b=e.get("vb") or e["b"], aim_b=e["b"])
                for e in doc.get("elements") or [] if e.get("b")]
    statics = [dict(s, b=s.get("vb") or s["b"])
               for s in doc.get("statics") or [] if s.get("b")]

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
            # A drawn icon, not a word (see _is_icon_text) — but a KNOWN
            # glyph keeps its meaning: the agency picker's info button
            # rendered as an empty box until its glyph translated.
            item["txt"] = ICON_LABELS.get(item["txt"].strip(), "")
        if kind == "row":
            own = sorted(labels.get(id(e), []),
                         key=lambda s: (s["b"][1], s["b"][0]))
            lines, badge, marks = [], "", []
            for s in own:
                how, value = _fold_label(e["b"], s)
                # No line twice: the legacy home repeats a subtitle node in
                # its own hierarchy, and a cell reading the same sentence
                # two times looks broken even when the tree really does.
                if how == "line" and value and value not in lines:
                    lines.append(value)
                elif how == "badge":
                    badge = value
                elif how == "mark":
                    marks.append(value)
            if marks:
                # The row's state marks, gathered onto one line: the visits
                # list draws a check per verified EVV record, and "✓ ✓"
                # under the times is the row's whole point.
                lines.append(" ".join(marks))
            item["lines"] = lines
            item["badge"] = badge
        items.append(item)
    for i, s in enumerate(statics):
        if i in folded:
            continue
        mark = _mark_for(s)
        if mark:
            items.append(_item({**s, "txt": mark}, "label"))
        elif (s.get("txt") and not _is_icon_text(s["txt"])
                and s["txt"].strip() not in BULLETS):
            label = _item(s, "label")
            # Short reads as a heading; longer is prose and must never
            # wear the uppercase header treatment — a paragraph in small
            # caps is a wall of shouting, not a section title.
            label["header"] = len(s["txt"].strip()) <= HEADER_MAX_CHARS
            items.append(label)

    # A curtain — an anonymous, label-less container spanning most of the
    # screen's height (the sliver of dimmed screen beside a slide-over
    # panel, a drawer's edge) — is a surface, not a row. Banding goes by
    # vertical overlap, so one curtain overlaps every real row on the
    # screen and magnetizes them all into a single band — seen live as the
    # patient-details page rendered as sixteen strips of vertically
    # crushed letters. It says nothing, folds nothing, and is dropped.
    items = [n for n in items
             if not (n["kind"] == "row"
                     and not n.get("lines")
                     and not n.get("txt")
                     and ((n["b"][3] - n["b"][1]) >= h * CURTAIN_MIN_HEIGHT
                          or (n["b"][3] - n["b"][1]) <= h * SLIVER_MAX_HEIGHT))]

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
    if bands:
        first = bands[0]
        buttons = [n for n in first if n.get("aim")]
        titles = [n for n in first if not n.get("aim") and n.get("txt")]
        if (buttons
                and min(n["b"][1] for n in first) <= h * NAV_TOP_MAX
                and max(n["b"][3] for n in first) <= h * NAV_BAND_BOTTOM
                and all(n.get("small") for n in buttons)):
            title = max(titles, key=lambda n: len(n["txt"]), default=None)
            nav = {
                "back": buttons[0],
                "title": title["txt"] if title else "",
                "trailing": buttons[1:],
            }
            bands = bands[1:]

    rows = []
    for band in bands:
        shape = _band_shape(band, h)
        if shape.get("tabs"):
            band = _fold_tab_captions(band)
        rows.append({"items": band, **shape})
    return {"id": doc.get("id", ""), "nav": nav, "rows": rows,
            "notice": doc.get("notice", ""), "blocked": doc.get("blocked", ""),
            "webview": bool(doc.get("webview")),
            "scrollable": bool(doc.get("scrollable")),
            # Whether the rows above are the WHOLE page (a stitched walk)
            # or just the viewport — the footnote reads opposite ways.
            "full": bool(doc.get("full"))}


def _fold_tab_captions(band: list[dict]) -> list[dict]:
    """Give each tab the caption sitting over it, and drop the strays.

    A tab's label often escapes the centre-containment fold — it hangs a few
    pixels outside its container's box. In a recognised tab bar the x-overlap
    is authority enough.
    """
    tabs = [i for i in band if i.get("aim")]
    for loose in band:
        if loose.get("aim") or not loose.get("txt"):
            continue
        cx = (loose["b"][0] + loose["b"][2]) / 2
        for tab in tabs:
            if tab["b"][0] <= cx <= tab["b"][2] and not tab.get("txt"):
                tab["txt"] = loose["txt"]
                break
    return tabs


# The bottom strip of a screen, where an app keeps its own tab bar.
TAB_BAND_TOP = 0.88


def _band_shape(band: list[dict], height: int) -> dict:
    """Row-level shapes learned from the flight recorder's first session.

    A keypad (Mobile Caregiver+ asks for its PIN on one) is bands of small
    digit buttons — left-aligned list rows made it usable but not a keypad;
    centring is what makes it read as one. A tab bar (inMyTeam keeps one at
    the bottom) is a band of equal containers hugging the screen's bottom
    edge — as list cells it crammed four labels and four chevrons into one
    row.
    """
    interactive = [i for i in band if i.get("aim")]
    if not interactive:
        return {}
    if (len(interactive) == len(band) and len(band) >= 2
            and all(i.get("small") and i["kind"] in ("button", "toggle",
                                                     "image")
                    for i in band)):
        return {"keys": True}
    # A couple of loose captions ride along in a real tab bar — their labels
    # hang outside the containers they belong to. More than that is a list.
    if (len(interactive) >= 3
            and len(band) - len(interactive) <= 2
            and all(i["kind"] in ("row", "image") for i in interactive)
            and all(i["b"][3] >= height * TAB_BAND_TOP for i in interactive)):
        return {"tabs": True}
    return {}


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
