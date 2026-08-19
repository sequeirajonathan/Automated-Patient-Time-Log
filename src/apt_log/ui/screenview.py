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

import re

# Widget classes with a meaning of their own. Anything else that is clickable
# is a container row — the shape Android lists are made of.
BUTTONS = ("Button", "ImageButton")
FIELDS = ("EditText", "AutoCompleteTextView", "SearchView")
TOGGLES = ("CheckBox", "Switch", "CompoundButton", "ToggleButton", "RadioButton")

# ...but a CHECKBOX is not a SWITCH, and drawing one as the other was
# reported as "it's a check list design not an on off switch". A switch
# says a setting is on; a plan of care asks whether a task was done, and
# the answer to that is a tick in a box. They stay one KIND — the grouping
# rules and the segmented ✓/✗ pairs all key off "toggle" — and differ only
# in how they are drawn.
CHECKS = ("CheckBox", "RadioButton")
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

# ...except when Android has NAMED it as the way out. `touch_outside` is the
# framework's own id for the region around a dialog or a bottom sheet, and
# touching it dismisses: it is not scenery, it is the exit.
#
# This exists because a modal with no way out actually happened. inMyTeam's
# signature pad is a bottom sheet whose two exits are that scrim and a small
# ✕ — and the portal drew NEITHER. The scrim went as a curtain (right, for a
# scrim that means nothing) and the ✕ went as an accessibility annotation
# (right, for a caption of twelve characters in a sixteen-pixel box). Each
# rule was correct on its own and together they stranded her on the staff
# signature with Done, Clear, and no third option: KEYCODE_BACK does not
# close this sheet, verified on the phone, so the portal's own Back was inert
# too. Reported as "I need a way to exit out of the signature portion".
#
# The scrim is the signal, NOT the target. Tapping the middle of it was tried
# on the real sheet and did nothing — inMyTeam's is not cancelable on an
# outside touch — so aiming there would have shipped a Close button that does
# nothing, which is worse than no button. What the id tells us is only this:
# a screen carrying one is a MODAL, and a modal has a way out.
DISMISS_IDS = ("touch_outside",)

# ...and on a modal, the way out is the small wordless control above the
# sheet's own buttons. inMyTeam's is a 32px ✕ at the top-left of the sheet,
# carrying a content-description ("Cancel Visit") that describes something it
# does not do — pressed twice on the live phone, it closes the sheet and
# leaves the form, the ticks and the typed note exactly as they were.
#
# Held to the same discipline as the signature canvas finder: EXACTLY ONE
# candidate or nothing at all. Two small wordless controls above the buttons
# means the shape rule matched something it should not have, and a wrong
# "Close" is a button she presses to escape that does something else.
DISMISS_MAX_SIDE = 0.10

# A wordless little square in the left margin, level with a line of text that
# starts just past it, is that line's ICON — the clock beside a visit's hours,
# the pin beside its address. inMyTeam draws three of them on Visit Detail and
# gives none of them a caption or a description, so each one rendered as its
# own full-width button reading "···" with the sentence it belongs to stranded
# on the line below. Reported as "visit details looks really bad", and it was
# three grey blocks doing nothing above three homeless sentences.
#
# Sized relative to the screen: an icon is small in BOTH directions (a wide
# short box is a divider, a tall thin one is a rule), it ends before its text
# begins, and the gap between them is a margin rather than a layout column.
ICON_MAX_SIDE = 0.10
ICON_MAX_GAP = 0.10

# How much two bands must share vertically before a horizontal gap between
# them is read as one row drawn across two baselines rather than two rows.
# Below BAND_OVERLAP by design — the horizontal test is what separates a list
# from a split row, so this only has to be enough to mean "the same line".
# The plan of care's halves share 47%.
SPLIT_ROW_OVERLAP = 0.3

# How close a caption must sit above a wordless control to be its name. A
# label's own line height, near enough — further than that and it is a
# heading over a section rather than a caption on a box.
CAPTION_MAX_GAP = 0.02

# Orphaned list dots. The reflow keeps a list's items, not its typography.
BULLETS = {"•", "·", "◦", "‣", "∙", "*", "-"}

# A loose label this short reads as a section heading; longer is prose. The
# header treatment (uppercase, small, muted) applied to whole paragraphs
# turned the migration pitch into a wall of shouting.
HEADER_MAX_CHARS = 32

# A nav bar's title runs longer than a section heading: the visit detail
# titles itself with the page name AND the patient ("Detalle de Visita
# CARIDAD ROJAS BATISTA", 39 chars), and holding the title to the heading
# cap demoted the whole title bar to a boxed row (seen live, first field
# test). Still short of a sentence — the tablet-density schedule's hint
# line ("Encontrará las visitas anteriores con Visita Buscar", 51 chars)
# must keep failing, or the page renames itself to the hint.
NAV_TITLE_MAX_CHARS = 48


# A word in a box far too small to show it is not being shown: it is an
# ACCESSIBILITY ANNOTATION riding on the icon that occupies that box. The
# HHAeXchange+ schedule puts "Contraído"/"Expandido" — the chevron's state —
# in a five-pixel square beside each visit, and rendered as words it printed
# "Expandido" under every patient's name, which is what a screen reader says
# and not what the phone shows.
#
# Measured PER CHARACTER, and against the screen rather than in pixels: the
# phone's density changes underneath this, and box and text scale together.
# An absolute width was the first attempt and was not a signal — it left
# "Menú" in a 20px box sitting a whisker from the cut.
#
# The threshold is set between two populations of REAL measurements, not
# chosen. At 720 wide: the annotation runs 0.56 px per character, and the
# tightest genuine text anybody has seen is Mobile Caregiver+'s "Visitas"
# tab caption at 2.43 — a caption whose box is drawn tighter than its own
# text, which is how a tab bar is built and which the existing tab-bar tests
# already carried. 1.44 sits between them with room on both sides.
#
# Four characters minimum, so a mark or a badge is never touched: a check is
# one character in a box just as narrow, and "54" is two.
ANNOTATION_WIDTH_PER_CHAR = 0.002
ANNOTATION_MIN_CHARS = 4


def _is_annotation(txt: str, b: list[int], screen_w: int) -> bool:
    txt = txt.strip()
    if len(txt) < ANNOTATION_MIN_CHARS or not screen_w:
        return False
    return (b[2] - b[0]) / len(txt) < screen_w * ANNOTATION_WIDTH_PER_CHAR


def _is_spelled_out(txt: str) -> bool:
    """Text a screen reader was meant to say letter by letter.

    Apps write alt text for the ear, not the eye: "H H AeXchange +" is the
    logo's description spelling the brand out, and rendered as words it is
    gibberish wearing a title's clothes.

    FOUR or more tokens, half of them a single character. Four rather than
    three because three is where real names live — "A B Smith" is two initials
    and a surname, which this test wrongly flagged when it asked for three,
    and a caught patient name is a worse outcome than a missed logo. Spelling
    is long by nature; a name with initials is short.
    """
    tokens = txt.split()
    if len(tokens) < 4:
        return False
    singles = sum(1 for t in tokens if len(t.strip(".·-")) <= 1)
    return singles * 2 >= len(tokens)


def _is_icon_text(txt: str) -> bool:
    """Text that is an icon font's private glyph, not a word.

    Android apps ship icon fonts whose characters live in Unicode's private
    use area; the dump hands them over as text and rendering them raw shows a
    tofu box or a stray hamburger where the app showed a drawn icon. Words are
    words; glyphs are decoration.
    """
    return bool(txt) and all(
        0xE000 <= ord(c) <= 0xF8FF or c.isspace() for c in txt)


def _is_honest_title(txt: str) -> bool:
    """Whether this is a name for the page, or something that landed there.

    The header is the one string on the screen somebody reads without looking
    for it, so what may sit in it is worth stating: not a spelled-out brand,
    not a paragraph, not a bare symbol, and not nothing. Anything rejected
    falls through to the app's own name, which is always true.

    A title must contain a LETTER OR DIGIT. That is what rules out the glyph
    case, and it rules it out at the right end: an icon that translates to a
    real mark (the EVV check) is meaningful in a row and still says nothing as
    the name of a page.
    """
    txt = (txt or "").strip()
    return bool(txt) and any(c.isalnum() for c in txt) and not (
        _is_spelled_out(txt)
        or len(txt) > NAV_TITLE_MAX_CHARS)


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
    "\uf060": "\u2190",   # left arrow \u2014 the visit-details back button
}

# What a state mark MEANS, so the template can colour it. A verified check
# is good news (green); a cross or a warning is not. The medical context
# makes this worth the colour: a caregiver scanning her day wants the
# confirmed check-ins to read as confirmed at a glance.
MARK_TONE = {
    "\u2713": "ok",       # check -> green
    "\u2715": "bad",      # cross -> red
    "\u26a0": "warn",     # warning -> amber
}

# A row whose only words are a GENERATED DESCRIPTION — the sentence a screen
# reader would say, which is all a custom-drawn list gives the tree. Mobile
# Caregiver+ builds its visits that way: "La visita está programada para
# ATANASIO MEDEROS TORRIEN en martes, 18 de agosto de 2026 de 9:05 AM a
# 11:05 AM y su estado es No Empezadas, Tarde". Rendered whole, three of
# those are a wall of prose where the phone shows three scannable rows.
#
# Shaped ONLY when the sentence can be accounted for completely — a name, a
# time range, and a status. Anything less and it is left exactly as written,
# because a rule that drops half a sentence it did not understand is worse
# than a long line. The one part not carried over is the date, which the
# page states once above the list as its own section header.
DESC_MIN_CHARS = 60
# Two or more capitalised words in a row: how these apps write a patient's
# name, and long enough that "AM"/"PM" beside a clock cannot pass for one.
_DESC_NAME = re.compile(
    r"\b([A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ.'\-]{1,}(?:\s+[A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ.'\-]{1,})+)\b")
_DESC_CLOCK = re.compile(r"\d{1,2}:\d{2}\s*(?:[AaPp]\.?\s?[Mm]\.?)?")
# Both locales, as everywhere else here: the apps ship Spanish and English
# and nothing else has ever appeared on this phone.
_DESC_STATUS = (" y su estado es ", " y el estado es ",
                " and its status is ", " and the status is ")


def _shape_description(text: str) -> list[str] | None:
    """A generated description as the lines of a list cell, or None."""
    if len(text) < DESC_MIN_CHARS:
        return None
    names = _DESC_NAME.findall(text)
    if not names:
        return None
    clocks = [c.strip() for c in _DESC_CLOCK.findall(text) if c.strip()]
    lowered = text.lower()
    status = ""
    for lead in _DESC_STATUS:
        cut = lowered.rfind(lead)
        if cut != -1:
            status = text[cut + len(lead):].strip(" .")
            break
    if len(clocks) < 2 or not status:
        return None
    # The longest run of capitals is the name; a shorter one is an initialism
    # somewhere else in the sentence.
    name = max(names, key=len).strip()
    return [name, f"{clocks[0]} – {clocks[1]}", status]

# Text the app itself presents as a PRIMARY BUTTON \u2014 the filled calls to
# action a screen is built around. Matched at the start of a label so
# "Continuar visitando", "Iniciar visita no programada", "Guardar
# cambios" all read as calls to action and earn the filled, coloured
# treatment instead of looking like one more grey row. Spanish first
# because that is the caregiver's app language; the English forms cover the
# apps that mix locales. "Ver detalles" is here although it navigates:
# the schedule's expanded card draws it as its one filled button, and the
# owner named it the card's only real call to action \u2014 the app's own
# visual weight is the standard, not the tap's side effect.
ACTION_WORDS = (
    "continuar", "iniciar sesi\u00f3n", "iniciar visita", "guardar",
    "confirmar", "enviar", "comenzar", "empezar", "aceptar",
    "registrar entrada", "registrar salida", "ver detalles",
    "clock in", "clock out", "sign in", "start visit", "save", "submit",
    "confirm", "continue", "view details", "see details",
    "let's get started", "get started",
)


# Actions the app itself plays DOWN, whatever their verb. "Iniciar
# visita no programada" starts the exception workflow — creating an
# UNSCHEDULED visit — and the app draws it as small plain text under
# today's cards while "Ver detalles" gets the filled pill (checked
# against the phone's own pixels). The portal must not out-shout the
# app on a record-creating path: these render as ordinary rows.
DEMOTED_HINTS = ("no programada", "unscheduled")


def _looks_like_cta(text: str) -> bool:
    # The curly apostrophe is the one apps actually ship ("Let\u2019s Get
    # Started"); the word list is written with the plain one.
    t = (text or "").strip().lower().replace("\u2019", "'")
    if any(h in t for h in DEMOTED_HINTS):
        return False
    return bool(t) and any(t.startswith(w) for w in ACTION_WORDS)

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
        # Absent means enabled: every element published before this field
        # existed was one, and a screen read by an older feed must not come
        # out greyed from end to end.
        "enabled": node.get("enabled", True) is not False,
        # A tick box rather than an on/off switch. See CHECKS.
        "check": node.get("cls") in CHECKS,
        "b": node["b"],
    }
    # A name the PORTAL supplies for a control the app left nameless, carried
    # as a key rather than a string because the model is language-free until
    # somebody asks for it in a language. Resolved by `label_keys` at render.
    if node.get("txt_key"):
        out["txt_key"] = node["txt_key"]
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


# What an app calls its own up-arrow, in both languages and in the words
# Android's own description uses. Matched on the whole caption, never on a
# substring: "Volver a la lista" is a link somewhere and not this.
_UP_WORDS = frozenset((
    "navigate up", "up", "back", "go back", "navigate back",
    "atrás", "atras", "volver", "regresar", "arriba", "navegar hacia arriba",
))


def _is_up_affordance(item: dict) -> bool:
    """Whether this control is the app's own version of Back."""
    return (item.get("txt") or "").strip().lower() in _UP_WORDS


def _is_dismiss(node: dict) -> bool:
    """Whether this element is the framework's dismiss region. See DISMISS_IDS."""
    return (node.get("rid") or "").rsplit("/", 1)[-1].lower() in DISMISS_IDS


def _name_the_unnamed(elements: list[dict], statics: list[dict], doc: dict,
                      w: int, h: int) -> None:
    """Carry the feed's own naming onto the items it named. In place.

    The identification itself moved to `apt_log.controls`, where the raw text
    is: the same table that decides a box is the agency filter also decides
    that box's contents may be shown, and those two answers must come from one
    place or they drift. Here it is only copied across — and only where the
    control has no words of its own, because the SELECTED agency is a better
    label than the word "filter" and now arrives as ordinary text.
    """
    from apt_log import controls as controls_mod

    for e in elements:
        key = e.get("name_key")
        if key and (controls_mod.replaces(key) or not e.get("txt")):
            e["txt_key"] = key


def label_keys(model: dict, t) -> dict:
    """Resolve every `txt_key` through the caller's translator.

    Done here rather than in the template because a dozen render sites read
    `it.txt`, and a portal-supplied name has to arrive as ordinary text or it
    would need saying at every one of them. Done here rather than in `build`
    because the model is language-free until somebody asks for it in a
    language — which is what lets the same document render Spanish for her
    and English for whoever is helping her, at the same moment.
    """
    def walk(items):
        for it in items or ():
            key = it.get("txt_key")
            if key:
                it["txt"] = t(key)
            walk(it.get("parts"))
    for row in model.get("rows") or ():
        walk(row.get("items"))
    if model.get("nav"):
        # `back` as well as `trailing`: the drawer handle is the first button
        # in the bar and so lands in `back`, and walking only the trailing
        # ones left it saying "Open navigation drawer" in a Spanish page.
        nav = model["nav"]
        walk(([nav["back"]] if nav.get("back") else []) + (nav.get("trailing") or []))
    # And the modal's exit, which is named by the portal and by nothing else.
    if model.get("dismiss"):
        walk([model["dismiss"]])
    return model


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
    statics = [dict(s, b=s.get("vb") or s["b"], dev_b=s["b"])
               for s in doc.get("statics") or [] if s.get("b")
               and not _is_annotation(s.get("txt", ""), s["b"], w)]

    # ------------------------------------------------------------- overlays
    # An app can slide a full-screen surface OVER the page without removing
    # the page: the GPS confirm draws its map across everything, and the
    # tree still reports the visit page beneath — tabs, buttons, the whole
    # care plan — so the portal rendered both pages at once (seen live,
    # first field test). Document order is z-order: an ANONYMOUS container
    # spanning most of the screen that arrives AFTER interactive content it
    # covers is an overlay, where a stitched page's wrapper (an ancestor)
    # arrives BEFORE its children. Everything beneath an overlay is
    # invisible on the phone and drops; whatever rides on it or sits
    # outside it (its own controls, the confirm strip below) stays. A NAMED
    # container is a control in its own right — the signature canvas —
    # never an overlay.
    covers = [i for i, e in enumerate(elements)
              if not (e.get("rid") or "")
              and _kind(e.get("cls", "")) == "row"
              and (e["b"][3] - e["b"][1]) >= h * CURTAIN_MIN_HEIGHT
              and (e["b"][2] - e["b"][0]) >= w * 0.9
              and any(j < i and _contains(e["b"], o["b"])
                      for j, o in enumerate(elements))]
    if covers:
        c = covers[-1]
        cov = elements[c]["b"]
        elements = [e for j, e in enumerate(elements)
                    if j > c or (j != c and not _contains(cov, e["b"]))]
        statics = [s for s in statics if not _contains(cov, s["b"])]

    _name_the_unnamed(elements, statics, doc, w, h)

    # The way out of a modal, lifted before anything else looks at it — the
    # curtain rule below would drop it, and it is the one control on the
    # screen she cannot do without. Named in her own language, because the
    # app's word for it is an id.
    dismiss = None
    if any(_is_dismiss(e) for e in elements):
        elements = [e for e in elements if not _is_dismiss(e)]
        sheet = [e for e in elements
                 if not _is_up_affordance({"txt": e.get("txt", "")})]
        # Anything with words of its own is a control she can already read —
        # Done, Clear. The exit is the one that has none.
        wordless = [e for e in sheet if not (e.get("txt") or "").strip()]
        captioned = [e for e in sheet if (e.get("txt") or "").strip()]
        floor = min((e["b"][1] for e in captioned), default=h)
        exits = [e for e in wordless
                 if (e["b"][2] - e["b"][0]) <= w * DISMISS_MAX_SIDE
                 and (e["b"][3] - e["b"][1]) <= w * DISMISS_MAX_SIDE
                 and e["b"][3] <= floor
                 and not any(_contains(e["b"], o["b"])
                             for o in sheet if o is not e)]
        if len(exits) == 1:
            dismiss = _item(exits[0], "button")
            dismiss["txt_key"] = "papp.close_sheet"
            dismiss["small"] = True
            elements = [e for e in elements if e is not exits[0]]

    # The app's own bottom tab bar comes out first, before anything is
    # folded or banded: its captions and the containers under them are
    # chrome, lifted to the control bar, not content for the list. Detected
    # from the captions (every tab has one) so the selected tab — which
    # Compose leaves without a clickable container — is not lost.
    apptabs, tab_ids = _app_tabs(elements, statics, w, h)
    elements = [e for e in elements if id(e) not in tab_ids]
    statics = [s for s in statics if id(s) not in tab_ids]

    # Labels inside a tappable container belong to it: they are the row's own
    # text, not free-floating captions. This is what turns "an unlabelled
    # rectangle above three orphaned words" back into a list cell.
    #
    # Containment is judged WITHIN a capture, never across the stitch. The
    # virtual bounds carry the scroll offset's accumulated error, so on a
    # long page a day divider from one capture drifted inside a visit card
    # from another and folded in as a bogus subtitle (seen live: "agosto
    # 19/20/21" stuck under a visit row). Only a label captured at the same
    # scroll step as the container was really inside it.
    # A tall container with other interactive elements INSIDE it is a
    # WRAPPER, not a row: the inMyTeam sign-in hangs one clickable View
    # over the whole page, and letting it fold labels swallowed the
    # title, the field's caption and the submit's text into one giant
    # "button" — while the real Sign in control rendered empty. A tall
    # card that holds nothing but the page's text is still content and
    # keeps its labels; scaffolding folds nothing.
    def _wraps_others(e: dict) -> bool:
        return any(o is not e and _contains(e["b"], o["b"])
                   for o in elements)

    containers = [e for e in elements
                  if _kind(e.get("cls", "")) == "row"
                  and not ((e["b"][3] - e["b"][1]) >= h * CURTAIN_MIN_HEIGHT
                           and _wraps_others(e))]
    folded: set[int] = set()
    labels: dict[int, list[dict]] = {}
    for i, s in enumerate(statics):
        for c in containers:
            if s.get("step", 0) == c.get("step", 0) and _contains(c["b"], s["b"]):
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
            # An avatar chip's initial folded in among the row's first
            # lines ("H" beside the agency's name on the visits list): a
            # stub of capitals among real lines is the app's decoration,
            # the folded twin of the loose "LP" beside a patient's name.
            # It sorts first or second depending on which baseline sits a
            # pixel higher, so both leading slots are checked.
            if len(lines) >= 3:
                for k in (0, 1):
                    if (len(lines[k].strip()) <= 2
                            and lines[k].strip().isupper()):
                        lines.pop(k)
                        break
            # A cell whose whole content is one generated sentence becomes
            # the list row that sentence describes. Only when the sentence
            # accounts for itself — see _shape_description.
            if len(lines) == 1:
                shaped = _shape_description(lines[0])
                if shaped:
                    lines = shaped
            item["lines"] = lines
            item["badge"] = badge
            # A row whose only content is a KNOWN glyph is named by it —
            # the visit-details back button carries nothing but the drawn
            # arrow, and stripping that as decoration left an empty
            # sliver the row filter then swallowed: no back control.
            if not lines and not item["txt"]:
                item["txt"] = next(
                    (ICON_LABELS[t] for s in own
                     if (t := (s.get("txt") or "").strip()) in ICON_LABELS),
                    "")
            # State marks stay their own field — a verified EVV check-in is
            # a green tick the caregiver scans for, not a "✓" buried in a
            # subtitle line. The template draws them as coloured status pills.
            item["marks"] = [{"sym": m, "tone": MARK_TONE.get(m, "")}
                             for m in marks]
            item["cta"] = _looks_like_cta(lines[0] if lines else "")
            # A content-hugging row carrying one status line is the app
            # STATING something, not offering a door: the expanded visit
            # card's "✓ Registros de entrada de EVV" hugs its text at a
            # third of the screen while every genuinely navigable cell
            # spans it. Compose marks these clickable anyway, and drawing
            # them as chevroned cells made the whole card look like it
            # kept expanding ("makes it look like the individual elements
            # can continue to expand"). They render as status lines — the
            # check keeps its colour, the chevron and button dress go.
            # Anchored at the left content margin, where statements start:
            # a narrow tappable floating mid-screen ("Visita Buscar", the
            # schedule's search) is a utility button, and dressing it as
            # information would hide a real control. And LINE-shaped: the
            # visit-details "Funciones" tile is narrow, left-anchored and
            # single-captioned too, but it is a tall icon tile — a real
            # button, and dressing it as information hid its tap.
            narrow = (e["b"][2] - e["b"][0]) <= w * 0.6
            left_anchored = e["b"][0] <= w * 0.08
            line_shaped = (e["b"][3] - e["b"][1]) <= h * 0.03
            # A row the app itself marks disabled is a statement whatever
            # its shape — HHAeXchange+ marks its EVV record lines that way
            # ("Registros de entrada de EVV 6:00 a. m."), which is the app
            # saying outright what the shape rules above infer. Dimming
            # those to a greyed control would have hidden the very fact
            # they exist to show; they read as information instead.
            item["info"] = (
                (not item["enabled"] and bool(lines or marks))
                or (narrow and left_anchored and line_shaped
                    and not item["cta"] and not badge
                    and len(lines) <= 1 and bool(lines or marks)))
            item["tone"] = (item["marks"][0]["tone"]
                            if item["info"] and item["marks"] else "")
        elif kind == "button":
            item["cta"] = _looks_like_cta(item["txt"])
        items.append(item)
    # The loose text of the screen — everything not folded into a row. Marks
    # peel off first; the rest are captions, values, headings and prose.
    loose: list[dict] = []
    loose_marks: list[dict] = []
    for i, s in enumerate(statics):
        if i in folded:
            continue
        if _mark_for(s):
            loose_marks.append(s)
        elif (s.get("txt") and not _is_icon_text(s["txt"])
                and s["txt"].strip() not in BULLETS):
            loose.append(s)

    # A loose mark with a sentence right beside it is one statement, not
    # two strays: the visit-details screen draws its EVV chips with no
    # container at all, and "✓" floating a line above "Registros de
    # entrada…" read as debris. Pair them into the same status line the
    # schedule's chips get; a mark with no neighbour stays a lone label.
    paired: set[int] = set()
    for s in loose_marks:
        mark = _mark_for(s)
        mate = next(
            (t for t in loose
             if id(t) not in paired
             and 0 <= t["b"][0] - s["b"][2] <= 24
             and _overlap(t["b"], s["b"]) >= 0.5), None)
        if mate is None:
            items.append(_item({**s, "txt": mark}, "label"))
            continue
        paired.add(id(mate))
        chip = _item({k: v for k, v in s.items() if k != "rid"}, "row")
        chip["b"] = [s["b"][0], min(s["b"][1], mate["b"][1]),
                     mate["b"][2], max(s["b"][3], mate["b"][3])]
        chip.update({"txt": "", "lines": [mate["txt"].strip()], "badge": "",
                     "marks": [{"sym": mark,
                                "tone": MARK_TONE.get(mark, "")}],
                     "cta": False, "info": True,
                     "tone": MARK_TONE.get(mark, "")})
        items.append(chip)
    loose = [t for t in loose if id(t) not in paired]

    # Initials drawn as an avatar chip: a stub of capitals hugging the
    # left of a real name ("LP" beside "LUCRESIA L PUPO") is the app's
    # decoration, and rendering it as a section heading shouted noise.
    # The chip is taller than the name line it decorates (the name's
    # subtitle rides beside it too), so the overlap bar sits low.
    def _beside(t, s):
        return (0 <= t["b"][0] - s["b"][2] <= 24
                and _overlap(t["b"], s["b"]) >= 0.25)

    def _beneath(t, s):
        return (0 <= t["b"][1] - s["b"][3] <= 30
                and t["b"][0] < s["b"][2] and t["b"][2] > s["b"][0])

    loose = [s for s in loose
             if not (len(s["txt"].strip()) <= 3
                     and s["txt"].strip().isupper()
                     and any(t is not s
                             and len(t["txt"].strip()) > 3
                             and (_beside(t, s) or _beneath(t, s))
                             for t in loose))]

    # A field is a caption stacked tight above its value: the patient detail
    # renders "Identificación de admisión" and "XOR-900196" a pixel apart,
    # and rendering both as muted section headings made a value read as
    # another label with a wall of space around it. Two short loose labels,
    # left-aligned and nearly touching, are one field — caption over value.
    # A caption over a CARD (its value being a tappable row) is not here:
    # that value folded into a container, so the caption stays a heading.
    loose.sort(key=lambda s: (s["b"][1], s["b"][0]))

    # A RUN of loose labels — three or more single lines sharing a left
    # edge and a steady rhythm — is the app's list, not a stack of
    # headings: the visit detail's care plan is fourteen task lines, and
    # every one of them short enough to be "caption shaped", so the whole
    # plan rendered as a wall of muted small-caps section titles (seen
    # live, first field test). List members read as body lines.
    run_ids: set[int] = set()
    k = 0
    while k < len(loose):
        m = k + 1
        while m < len(loose):
            prev, cur = loose[m - 1], loose[m]
            line_h = max(cur["b"][3] - cur["b"][1], 1)
            if (abs(cur["b"][0] - loose[k]["b"][0]) <= max(8, 0.02 * w)
                    and 0 <= cur["b"][1] - prev["b"][3] <= 2.5 * line_h):
                m += 1
            else:
                break
        if m - k >= 3:
            run_ids.update(id(loose[t]) for t in range(k, m))
        k = m

    j = 0
    while j < len(loose):
        s = loose[j]
        nxt = loose[j + 1] if j + 1 < len(loose) else None
        height = s["b"][3] - s["b"][1]
        caption_shaped = (len(s["txt"].strip()) <= HEADER_MAX_CHARS
                          and id(s) not in run_ids)
        # A caption sits a hair above its value (a field), not a heading a
        # line above a paragraph. Tight gap, left-aligned, and the value is
        # a datum — a phone number, an ID, an office — never prose.
        # A caption and its value share a LEFT EDGE — that is what makes them
        # one datum rather than two. The old allowance was 6% of the screen,
        # 43px at this density, and the plan of care's section header
        # "Personal Care (1 H)" at x=19 swallowed its first task "Ambulation
        # Assist" at x=61 with a pixel to spare: the task's name vanished into
        # a field, its tick was left captioned by nothing, and the caregiver
        # lost the first line of the list she has to complete. 42px is not a
        # shared edge, it is the next column in.
        if (nxt is not None and caption_shaped
                and 0 <= nxt["b"][1] - s["b"][3] <= 0.5 * height
                and abs(nxt["b"][0] - s["b"][0]) <= max(8, 0.02 * w)
                and len((nxt["txt"] or "").strip()) <= 80):
            field = _item(s, "kv")
            field["caption"] = s["txt"]
            field["value"] = nxt["txt"]
            items.append(field)
            j += 2
            continue
        label = _item(s, "label")
        # Short reads as a heading; longer is prose and must never wear the
        # uppercase header treatment — a paragraph in small caps is a wall
        # of shouting, not a section title.
        label["header"] = caption_shaped
        items.append(label)
        j += 1

    # A curtain — an anonymous, label-less container spanning most of the
    # screen's height (the sliver of dimmed screen beside a slide-over
    # panel, a drawer's edge) — is a surface, not a row. Banding goes by
    # vertical overlap, so one curtain overlaps every real row on the
    # screen and magnetizes them all into a single band — seen live as the
    # patient-details page rendered as sixteen strips of vertically
    # crushed letters. It says nothing, folds nothing, and is dropped.
    # A row with a RESOURCE ID is a real control whatever its size: the
    # visit-details back button is a 25px-tall strip at tablet density,
    # and the sliver rule swallowed it — no way back. Anonymity is part
    # of what makes a scrim a scrim. But a name does not make a 1px strip
    # tappable: the agency picker's "viewtop" divider is id'd and one
    # pixel tall, and it rendered as an empty tappable row. Below a
    # finger's reach, the id no longer saves it.
    def _sliver(n: dict) -> bool:
        height = n["b"][3] - n["b"][1]
        if height >= h * CURTAIN_MIN_HEIGHT:
            return True
        if height <= h * 0.005:
            return True
        return (height <= h * SLIVER_MAX_HEIGHT
                and not (n.get("aim") or {}).get("rid"))

    items = [n for n in items
             if not (n["kind"] == "row"
                     and not n.get("lines")
                     and not n.get("txt")
                     and _sliver(n))]

    items.sort(key=lambda n: (n["b"][1], n["b"][0]))

    # ------------------------------------------------------------------ bands
    bands: list[list[dict]] = []
    for item in items:
        if bands and any(_overlap(item["b"], other["b"]) >= BAND_OVERLAP
                         for other in bands[-1]):
            bands[-1].append(item)
        else:
            bands.append([item])

    # A row the app drew across two baselines is still one row. inMyTeam's
    # plan of care puts a task's own tick and name on one line and its
    # "Patient refused" tick and caption seventeen pixels lower and two thirds
    # of the way across — one choice about one task, which banding split into
    # two cells because the halves overlap by 47% and the bar is 50%. Thirteen
    # tasks became twenty-six rows, and the caregiver scrolls twice as far to
    # answer half as many questions.
    #
    # Merged only when the two bands cannot be a list: a list's rows share
    # horizontal space and differ in y, so a candidate that overlaps the band
    # vertically AND is clear of every item in it horizontally is the same row
    # continued. That is a weaker bar than BAND_OVERLAP on purpose, and it is
    # safe precisely because the horizontal test does the separating.
    merged: list[list[dict]] = []
    for band in bands:
        if merged:
            prev = merged[-1]
            span = [min(n["b"][0] for n in prev), min(n["b"][1] for n in prev),
                    max(n["b"][2] for n in prev), max(n["b"][3] for n in prev)]
            here = [min(n["b"][0] for n in band), min(n["b"][1] for n in band),
                    max(n["b"][2] for n in band), max(n["b"][3] for n in band)]
            if (_overlap(span, here) >= SPLIT_ROW_OVERLAP
                    and _tiled(prev + band)):
                prev.extend(band)
                continue
        merged.append(band)
    bands = merged
    for band in bands:
        band.sort(key=lambda n: n["b"][0])

    # An icon in the margin belongs to the line it decorates, not to a row of
    # its own. See ICON_MAX_SIDE. The icon's own aim survives the fold when
    # the app made it tappable (the address pin opens maps), so the sentence
    # becomes the target and the tap still lands where the app put it — the
    # reflow's standing trade: it moves where a control is DRAWN, never what
    # pressing it means. A disabled icon is decoration and takes nothing with
    # it; it is simply not drawn, because the tree says no more about it than
    # that it is a square, and "···" was the portal inventing a control.
    for band in bands:
        _fold_gutter_icons(band, w)
    bands = [band for band in bands if band]

    # A caption sitting directly ON TOP of a wordless control is that
    # control's name. Android puts a label inside the box it names often
    # enough that folding by containment gets most of them; a signature field
    # is the case it does not. inMyTeam draws "Patient Signature" and then a
    # tall empty box beneath it, and the box came through nameless — two
    # blank cells with chevrons, one above the other, where the two things
    # she has to collect should be.
    #
    # Only a control with NOTHING to say adopts a caption, so a list row that
    # already carries its own words can never be renamed by the heading above
    # it — the day divider over a visit card stays a day divider.
    adopted: set[int] = set()
    for i, band in enumerate(bands):
        if i == 0 or len(band) != 1 or len(bands[i - 1]) != 1:
            continue
        it, cap = band[0], bands[i - 1][0]
        if (it["kind"] != "row" or it.get("lines") or it.get("txt")
                or not it.get("aim")):
            continue
        if cap["kind"] != "label" or not (cap.get("txt") or "").strip():
            continue
        if not (0 <= it["b"][1] - cap["b"][3] <= CAPTION_MAX_GAP * h):
            continue
        if abs(it["b"][0] - cap["b"][0]) > w * 0.03:
            continue
        it["lines"] = [cap["txt"]]
        adopted.add(id(cap))
    if adopted:
        bands = [b for b in bands if not (len(b) == 1 and id(b[0]) in adopted)]

    # A WIDE button with no label sitting beside labelled buttons is a
    # dead tab slot, not a control: the visit detail's tab strip keeps an
    # empty third slot, and rendering it as a full-width '···' button put
    # a nameless call to action in the middle of the tab bar (seen live,
    # first field test). A small empty button is still an icon — kept.
    for band in bands:
        texted = [n for n in band
                  if n["kind"] == "button" and (n["txt"] or "").strip()]
        if len(texted) >= 2:
            band[:] = [n for n in band
                       if not (n["kind"] == "button"
                               and not (n["txt"] or "").strip()
                               and not n.get("small"))]
    bands = [band for band in bands if band]

    # A band that is nothing but section headings is the scroll stitch's
    # day dividers ("agosto 18", "19", "20") landing a few pixels apart and
    # magnetizing into one row — where they lose the header treatment and
    # render as boxed cells. Split them back to one heading per line. A
    # short label sitting WITH controls (a care-plan row's "127 - Toilet
    # Use" beside its ✓/✗, a nav title beside its buttons) is not an
    # all-header band, so it is left whole.
    def _is_header(it: dict) -> bool:
        return it["kind"] == "label" and it.get("header")

    # Only the STACKED ones, though. Two headings side by side and never
    # touching are the app's own columns, not strays that drifted together —
    # Visit Detail puts the patient on the left and the visit's date on the
    # right, on one line, and splitting them stacked two section headings with
    # a heading's margin under each: the pair of enormous voids in the middle
    # of the page. The stitch's day dividers overlap in x and differ in y,
    # which is exactly the case this split was written for and the one _tiled
    # goes on catching.
    split: list[list[dict]] = []
    for band in bands:
        if (len(band) > 1 and all(_is_header(it) for it in band)
                and not _tiled(band)):
            for it in sorted(band, key=lambda n: (n["b"][1], n["b"][0])):
                split.append([it])
        else:
            split.append(band)
    bands = split
    for band in bands:
        band.sort(key=lambda n: n["b"][0])

    # Side-by-side rows are CONTROLS, not statements: the visit detail's
    # "Visits | Plan of care" section switch is two half-width rows in one
    # band, and the left one matched the info-row shape. A statement
    # stands alone on its line.
    for band in bands:
        rows_here = [it for it in band if it["kind"] == "row"]
        if len(rows_here) >= 2:
            for it in rows_here:
                it["info"] = False

    # ...and when exactly one of them is DISABLED, that one is the section
    # she is already looking at. The app says so itself — inMyTeam disables
    # "Visits" while the Visits pane is showing and enables "Plan of care" —
    # and rendering the disabled half as a greyed-out cell said "unavailable"
    # about the very thing on screen. As a segmented control it says "you are
    # here", which is what the app meant and what the owner asked for.
    #
    # Reuses the segment the care plan's ✓/✗ pairs already draw, so this is a
    # new READING of the tree rather than a new widget.
    for band in bands:
        rows_here = [it for it in band if it["kind"] == "row"]
        if len(rows_here) != len(band) or len(rows_here) < 2:
            continue
        if not _tiled(rows_here):
            continue
        if not all(it.get("lines") or it.get("txt") for it in rows_here):
            continue
        off = [it for it in rows_here if not it["enabled"]]
        if len(off) != 1:
            continue
        ordered = sorted(rows_here, key=lambda n: n["b"][0])
        for it in ordered:
            it["checked"] = it is off[0]
            # A segment part shows one caption. The row's folded line IS that
            # caption; the part renders `txt`, so move it across.
            if not it.get("txt") and it.get("lines"):
                it["txt"] = it["lines"][0]
        band[:] = [{"kind": "segment",
                    "b": [ordered[0]["b"][0], ordered[0]["b"][1],
                          ordered[-1]["b"][2], ordered[-1]["b"][3]],
                    "parts": ordered}]

    # --------------------------------------------------------------- segments
    for band in bands:
        _pair_segments(band, w)

    # -------------------------------------------------------------------- nav
    nav = None
    if bands:
        first = bands[0]
        buttons = [n for n in first if n.get("aim")]
        titles = [n for n in first if not n.get("aim") and n.get("txt")]
        # A title bar's title is a title, never a paragraph. A page whose top
        # band is a hint sentence with an inline link ("Encontrará las visitas
        # anteriores con Visita Buscar", seen on the tablet-density schedule)
        # is not a nav bar, and letting its hint become the title renamed the
        # whole page from "HHAeXchange+" to the hint.
        if (buttons
                and min(n["b"][1] for n in first) <= h * NAV_TOP_MAX
                and max(n["b"][3] for n in first) <= h * NAV_BAND_BOTTOM
                and all(n.get("small") for n in buttons)
                and all(len((t["txt"] or "").strip()) <= NAV_TITLE_MAX_CHARS
                        for t in titles)):
            # Longest of the HONEST candidates. A title bar often carries a
            # logo beside its title, and the logo's description was winning on
            # length: the schedule renamed itself "Logotipo de H H AeXchange +"
            # — the brand spelled out for a screen reader, in the one slot
            # somebody reads without looking for it. What is rejected here
            # leaves the title empty, and an empty title falls through to the
            # app's own name, which is always true.
            honest = [n for n in titles if _is_honest_title(n["txt"])]
            title = max(honest, key=lambda n: len(n["txt"]), default=None)
            # The app's own up-arrow does not ride along. The pill already has
            # a Back and this would be a second control meaning the same
            # thing, three inches away, in different words — "Navigate up"
            # beside "Back", which is a question the caregiver has to stop and
            # answer mid-visit. Reported from the field exactly that way.
            #
            # Suppressed rather than re-pointed: the pill's Back sends the
            # phone's own Back, which is what this arrow does, so there is
            # nothing to chain it to that is not already there. Only a control
            # that SAYS it goes up is dropped — the top-left button is
            # otherwise whatever the app put there, and on HHAeXchange+ that
            # was the help button (and on the signature page, Anular).
            controls = [b for b in buttons if not _is_up_affordance(b)]
            nav = {
                "back": controls[0] if controls else None,
                "title": title["txt"] if title else "",
                "trailing": controls[1:],
            }
            bands = bands[1:]

    rows = []
    for band in bands:
        shape = _band_shape(band, h)
        rows.append({"items": band, **shape})
    return {"id": doc.get("id", ""), "nav": nav, "rows": rows,
            "apptabs": apptabs,
            # The way out of a modal, when the screen is one. See DISMISS_IDS.
            "dismiss": dismiss,
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
# And the bar must actually HUG the bottom edge: its nodes reach into the
# screen's last twentieth. The schedule's list runs to within 8% of the
# bottom, and on a stitched page the tail's own rows (a page's virtual
# bottom runs on past the pinned bar) matched the loose band test —
# watched live: 'agosto 21', patient names and their times consumed as a
# sixteen-tab bar, and the page's last two days vanished from the portal.
TAB_REACH = 0.95


def _dev(item: dict) -> list[int]:
    """Where the node sat on the DEVICE when captured.

    Stitched pages give every item a virtual position too, and chrome
    must be judged where it was drawn: the tab bar is pinned to the
    screen's bottom while the page's virtual bottom runs on past it.
    """
    return item.get("dev_b") or item.get("aim_b") or item["b"]


def _app_tabs(elements: list[dict], statics: list[dict],
              w: int, h: int) -> tuple[list[dict], set[int]]:
    """The app's own bottom tab bar as {txt, aim, current}, and the ids of
    every node it consumed.

    Detected from the CAPTIONS, not the clickable containers: Compose does
    not mark the selected tab clickable, so a container-only detector loses
    a slot (the schedule's 'Programación' when it is the tab in front).
    Every tab has a caption; the caption's container, when there is one, is
    the tap point, and the caption with none is the tab already selected —
    shown lit, not tappable, since tapping the current tab does nothing.
    """
    band = h * TAB_BAND_TOP
    reach = h * TAB_REACH
    narrow = w * 0.5
    holders = [e for e in elements
               if _dev(e)[3] > band and _dev(e)[3] >= reach
               and (e["b"][2] - e["b"][0]) < narrow
               and _kind(e.get("cls", "")) == "row"]
    caps = [s for s in statics
            if _dev(s)[1] > band and _dev(s)[3] >= reach
            and (s.get("txt") or "").strip()
            and not _is_icon_text(s["txt"])
            and (s["b"][2] - s["b"][0]) < narrow
            and len(s["txt"].strip()) <= HEADER_MAX_CHARS]
    # The bar's captions share ONE baseline. At a dense-enough layout the
    # page's last card runs to within a few pixels of the pinned bar, and
    # its own short lines passed every test above — consumed live as a
    # five-tab bar ('Detalles del paciente' riding next to 'Programación').
    # Content sits on its own lines; the bar's captions align. Keep only
    # the bottom-most aligned run of two or more.
    caps.sort(key=lambda s: _dev(s)[1])
    groups: list[list[dict]] = []
    for s in caps:
        if groups and abs(_dev(s)[1] - _dev(groups[-1][0])[1]) <= 6:
            groups[-1].append(s)
        else:
            groups.append([s])
    caps = next((g for g in reversed(groups) if len(g) >= 2), [])
    caps.sort(key=lambda s: s["b"][0])

    def _aim(holder):
        aim = {"rid": holder.get("rid", ""), "cls": holder.get("cls", ""),
               "b": holder.get("aim_b") or holder["b"]}
        if holder.get("step"):
            aim["step"] = holder["step"]
        return aim

    consumed: set[int] = set()
    tabs: list[dict] = []

    def _sweep(consumed: set[int], chosen: list[dict]) -> set[int]:
        """The bar's own furniture, beside the captions it is named by.

        Consuming only the captions left the rest of the strip to fall into
        the page: Mobile Caregiver+ labels each tab cell with a description
        AS WELL as a caption, and hangs an unread count on one — so the
        patients list ended its body with a stray "Beneficiarios" and a bare
        "370".

        Narrow on purpose. Anything standing in the bottom band was tried
        first and swept the page's own last row with it: at a dense layout
        the content runs to within a few pixels of the pinned bar, and
        "Detalles del paciente" is content, not navigation. Only two things
        are furniture — a word the bar has already said, and a bare count —
        and neither can be mistaken for a row of the page.
        """
        said = {(t.get("txt") or "").strip().lower() for t in chosen}
        for node in statics:
            if id(node) in consumed:
                continue
            txt = (node.get("txt") or "").strip()
            if not txt or _dev(node)[3] < reach:
                continue
            if txt.lower() in said or txt.isdigit():
                consumed.add(id(node))
        return consumed


    # Compose-shaped bar: every tab has a caption, and the selected one has
    # no clickable container. Detect from captions so that slot survives.
    # THREE or more: a two-item bottom bar is a page's action pair — the
    # visit detail's "Check in" / "Note & Check out" rode the control bar
    # dressed as navigation, which a commit action must never do.
    if len(caps) >= 3:
        for s in caps:
            cx = (s["b"][0] + s["b"][2]) / 2
            holder = next((e for e in holders
                           if e["b"][0] <= cx <= e["b"][2]), None)
            if holder is not None:
                consumed.add(id(holder))
            tabs.append({"txt": s["txt"].strip(),
                         "aim": _aim(holder) if holder else None,
                         "current": holder is None})
            consumed.add(id(s))
        return tabs, _sweep(consumed, tabs)

    # Icon-tab bar (inMyTeam): three or more equal containers hugging the
    # bottom, captions optional. Each container is a tab; a stray caption
    # folds into the one above it.
    holders.sort(key=lambda e: e["b"][0])
    if len(holders) >= 3:
        for e in holders:
            cx = (e["b"][0] + e["b"][2]) / 2
            cap = next((s for s in caps
                        if s["b"][0] <= cx <= s["b"][2]), None)
            txt = cap["txt"].strip() if cap else ""
            if cap is not None:
                consumed.add(id(cap))
            tabs.append({"txt": txt, "aim": _aim(e), "current": False})
            consumed.add(id(e))
        return tabs, _sweep(consumed, tabs)

    return [], set()


def _fold_gutter_icons(band: list[dict], width: int) -> None:
    """Fold margin icons into the line they decorate, in place.

    Conservative in the direction that costs least: a miss leaves the "···"
    block that was there before, while a false positive would silently eat a
    real control. So the icon must be small in both directions, must END
    before its text starts, must sit within a margin's distance of it, and
    must share the line — and the text must be a plain label with no aim of
    its own, or there would be two controls competing for one row.
    """
    icons = [n for n in band
             if n["kind"] in ("button", "image")
             and not (n.get("txt") or "").strip()
             and not n.get("txt_key")
             and not n.get("lines")
             and (n["b"][2] - n["b"][0]) <= width * ICON_MAX_SIDE
             and (n["b"][3] - n["b"][1]) <= width * ICON_MAX_SIDE]
    if not icons:
        return
    folded: list[dict] = []
    for icon in icons:
        mate = next(
            (t for t in band
             if t is not icon and t["kind"] == "label"
             and (t.get("txt") or "").strip()
             and not t.get("aim")
             and 0 <= t["b"][0] - icon["b"][2] <= width * ICON_MAX_GAP
             and _overlap(t["b"], icon["b"]) >= 0.5), None)
        if mate is None:
            continue
        folded.append(icon)
        # A tappable icon hands its aim to the sentence; a disabled one hands
        # over nothing, which is the whole of what the tree knows about it.
        if icon.get("aim") and icon["enabled"]:
            mate["kind"] = "row"
            mate["lines"] = [mate["txt"]]
            mate["txt"] = ""
            mate["aim"] = icon["aim"]
            mate["marks"] = []
            mate["badge"] = ""
            mate["cta"] = False
            mate["info"] = False
            mate["small"] = False
    if folded:
        band[:] = [n for n in band if n not in folded]


def _tiled(band: list[dict]) -> bool:
    """Whether these items sit side by side rather than stacked.

    Two boxes that share a line and never overlap horizontally are the app's
    own columns; two that overlap horizontally are strays that drifted onto
    one line. The difference decides whether a band is a layout or an
    accident, and both callers below turn on it.
    """
    ordered = sorted(band, key=lambda n: n["b"][0])
    return all(ordered[i]["b"][2] <= ordered[i + 1]["b"][0]
               for i in range(len(ordered) - 1))


def _band_shape(band: list[dict], height: int) -> dict:
    """Row-level shapes learned from the flight recorder's first session.

    A keypad (Mobile Caregiver+ asks for its PIN on one) is bands of small
    digit buttons — left-aligned list rows made it usable but not a keypad;
    centring is what makes it read as one. A tab bar (inMyTeam keeps one at
    the bottom) is a band of equal containers hugging the screen's bottom
    edge — as list cells it crammed four labels and four chevrons into one
    row.
    """
    # A band of nothing but words is a heading line, not a cell. A lone one
    # already had that treatment; a PAIR did not, because every rule for it
    # was written with :only-child — so the moment Visit Detail's patient and
    # date stopped being split into two rows they landed in a card together,
    # at body size, with a list cell's padding around them. Reported as
    # blocky, too much space, and a grey block, and that is exactly what it
    # was. The band says what it is and the stylesheet does the rest.
    interactive = [i for i in band if i.get("aim")]
    if not interactive:
        if len(band) > 1 and all(i["kind"] == "label" for i in band):
            return {"heads": True}
        return {}
    # A CHECKLIST line: tick boxes and the words they answer for, and nothing
    # else. One line per question, which is what a checklist is — the default
    # cell layout wraps and centres, and thirteen of those turned a page she
    # scans in seconds into a page she scrolls for a minute.
    if (all(i["kind"] in ("toggle", "label") for i in band)
            and any(i["kind"] == "toggle" and i.get("check") for i in band)
            and any(i["kind"] == "label" for i in band)):
        return {"checks": True}
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
            and all((i["aim"]["b"][3] >= height * TAB_BAND_TOP
                     and i["aim"]["b"][3] >= height * TAB_REACH)
                    for i in interactive)):
        return {"tabs": True}
    # TWO captioned controls hugging the bottom edge are the page's actions,
    # not a tab bar and not two more list cells. On Visit Detail they are
    # "Check in" and "Note & Check out" — the only things on the page that
    # change a record — and they rendered as a pair of narrow grey rows with
    # chevrons, indistinguishable from the navigation above them. Drawn as
    # what they are: side by side, full width, the second one filled.
    #
    # They are no easier to hit by accident than before — the aim is
    # unchanged and still verified against the published frame. They are
    # merely no longer disguised as somewhere to browse.
    if (len(interactive) == 2 and len(band) == 2
            and all(i["kind"] == "row" and (i.get("txt") or i.get("lines"))
                    for i in interactive)
            and _tiled(interactive)
            and all(i["aim"]["b"][3] >= height * TAB_REACH
                    for i in interactive)):
        return {"actions": True}
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
