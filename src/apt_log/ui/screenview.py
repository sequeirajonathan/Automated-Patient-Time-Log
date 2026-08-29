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

# A TAB BAR WHOSE CURRENT TAB IS NOT A CONTROL. HHAeXchange+ marks the EVV
# screen's tab by taking its click handler away: "Dispositivo o FOB" is an
# element with bounds, and "GPS" — the tab she is actually on — is nothing
# but a word centred in the other half. What says "tab bar" is the geometry:
# equal, screen-width-dividing slots with a caption on each slot's centre
# line. A tab is a big target, never a chip, and an ordinary row's caption
# sits at its left margin rather than on a centre line — which is what keeps
# this from firing on lists.
TAB_SLOT_MIN_WIDTH = 0.25
TAB_SLOT_TOLERANCE = 0.06

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


# AN ICON'S BOX IS SQUARE; A WORD'S BOX IS NOT.
#
# The width-per-character rule above catches a description hung on an icon
# only when the word is long enough to be obviously too big for its box, so
# it splits a set of identical icons by how long their captions happen to
# be. Mobile Caregiver+'s completed visit draws three in a column — a pin, a
# handset, a tick — each captioned for a screen reader:
#
#     "Dirección"            38px wide,  9 chars -> 4.2 per char, KEPT
#     "Número de teléfono"   38px wide, 18 chars -> 2.1 per char, dropped
#     "Servicio Completada"  30px wide, 19 chars -> 1.6 per char, dropped
#
# So the address row wore "Dirección" as a heading — a word the phone draws
# nowhere — while the phone row beside it, built identically, had none.
# Two identical rows, rendered two different ways.
#
# But this cannot be decided where that rule lives, and trying cost four
# tests: the SAME kind of description is what NAMES a control. HHAeXchange+'s
# back arrow is an empty button captioned "Atrás" in a 25px square, and that
# caption is the only thing identifying it as the up-arrow — drop it and the
# app's own Back rides into the title bar beside the portal's.
#
# What separates them is not the box, it is what else is in the row. A
# square description beside real text is decoration on an icon; a square
# description that is ALL a control has is that control's name. So the test
# is applied where containment is known, and only ever to take a caption
# away from a row that has something better to say.
ICON_CAPTION_MAX_ASPECT = 1.4


def _is_icon_caption(s: dict) -> bool:
    """Whether this label is a description hung on an icon, judged by the
    shape of the box it claims to be drawn in."""
    b = s.get("b") or []
    txt = (s.get("txt") or "").strip()
    if len(b) != 4 or len(txt) < ANNOTATION_MIN_CHARS:
        return False
    width, height = b[2] - b[0], b[3] - b[1]
    return height > 0 and width / height <= ICON_CAPTION_MAX_ASPECT


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

    A DIGIT IS NOT A LETTER BEING SPELLED. "Paso 2 de 3" is four tokens, two
    of them one character, and it was read as a spelled-out brand — so the
    signature screens' step counter was thrown away, and the caregiver could
    not tell the patient's signature from her own until after she had
    collected one. Nobody spells a brand out in numbers.
    """
    tokens = txt.split()
    if len(tokens) < 4:
        return False
    singles = sum(1 for t in tokens
                  if len((stripped := t.strip(".·-"))) <= 1
                  and not stripped.isdigit())
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


# The words that join the name to the date, and the date to the first clock.
# Stripped off the slice between them so what is left is the date alone.
_WHEN_EDGE = ("en", "on", "de", "del", "from", "a", "to", "para")


def _when_between(text: str, name: str) -> str:
    """The date phrase sitting between the patient's name and the first
    clock — "sábado, 29 de agosto de 2026".

    Taken as the slice between two things already located rather than
    matched by a date pattern, because the pattern would have to know every
    month in two languages to earn its keep, and a slice that comes out
    wrong comes out as prose rather than as a wrong date. Empty when
    anything is out of place, and an empty date is simply not drawn.
    """
    cut = text.find(name)
    clock = _DESC_CLOCK.search(text, cut + len(name) if cut != -1 else 0)
    if cut == -1 or clock is None:
        return ""
    middle = text[cut + len(name):clock.start()]
    # The app brackets the name on the detail page and not on the list, so
    # the bracket is stripped here rather than assumed away.
    middle = middle.strip().lstrip("]").strip()
    words = middle.replace(",", " ,").split()
    while words and words[0].strip(",").lower() in _WHEN_EDGE:
        words.pop(0)
    while words and words[-1].strip(",").lower() in _WHEN_EDGE:
        words.pop()
    return " ".join(words).replace(" ,", ",").strip(" ,")


def _describe(text: str) -> dict | None:
    """A generated description broken into its parts, or None.

    Shaped ONLY when the sentence accounts for itself — a name, a time
    range and a status. Anything less and the caller leaves it exactly as
    written, because a rule that drops half a sentence it did not
    understand is worse than a long line.
    """
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
    return {"name": name,
            "when": _when_between(text, name),
            "window": f"{clocks[0]} – {clocks[1]}",
            "status": status}


def _shape_description(text: str) -> list[str] | None:
    """A generated description as the lines of a list cell, or None.

    The date is dropped here and only here: in a LIST the page states it
    once above as a section header, so repeating it on every row is noise.
    A detail page has no such header, which is why `_describe` keeps it and
    this does not.
    """
    parts = _describe(text)
    if parts is None:
        return None
    return [parts["name"], parts["window"], parts["status"]]


# WHAT A VISIT'S STATUS MEANS, as a colour.
#
# Mobile Caregiver+ ends its description sentence with the visit's state, and
# the five it uses were all seen in one week of real data:
#
#     Sin empezar            not started, and not yet late
#     Completada             done
#     Completadas, Tarde     done, outside the window
#     No Empezadas, Tarde    never started, window passed
#     Perdida                missed
#
# Three of those are the outcomes this whole project exists to prevent, and
# the reflow was drawing all five as one more muted line under the times —
# the same grey as the clock, easy to read straight past. A status is not a
# detail about a visit; on this screen it is the point of the row.
_STATUS_TONES = (
    ("perdida", "bad"),
    ("missed", "bad"),
    ("tarde", "warn"),
    ("late", "warn"),
    ("completad", "ok"),
    ("complete", "ok"),
)


def status_tone(words: str) -> str:
    """"ok", "warn", "bad", or "" for a status with no colour of its own.

    Ordered worst-first: "Completadas, Tarde" is a visit that happened
    outside its window, and the thing worth seeing about it is the Tarde.
    """
    low = (words or "").strip().lower()
    if not low:
        return ""
    for mark, tone in _STATUS_TONES:
        if mark in low:
            return tone
    return ""

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
    # The submit under a search form. "Visita Buscar" is the MENU ROW that
    # opens that form and starts with its own word, so it is untouched.
    "buscar", "search",
    "clock in", "clock out", "sign in", "start visit", "save", "submit",
    "confirm", "continue", "view details", "see details",
    "let's get started", "get started",
    # The button under the texted code. Reported from the field as blending
    # in — and it was, because this list decides which control gets the
    # filled pill and "verify" was not on it. It is the only thing to do on
    # that screen, at the one moment she is holding a code that expires.
    "verify", "verificar", "verificación", "verificacion",
)

# NOT here on purpose: "resend" / "reenviar". It is a real control and it now
# has a macro behind it, but it is the SECOND thing to reach for on that
# screen — offering it in the same filled pill as Verify would have the page
# shouting two different instructions at somebody under time pressure.


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

# TWO TICKS THAT MEAN DIFFERENT THINGS.
#
# Both of the above are the same green check, and on a visits list they sit
# side by side: one says the check-IN was recorded, one says the check-OUT
# was. Drawn as two identical ticks they are indistinguishable — and worse,
# a visit checked in but not out shows ONE tick, exactly like a visit
# checked out but not in. That distinction is the entire content of an EVV
# record, and it is what the list is read for.
#
# So each named mark carries the portal's own word for what it marks. The
# app supplies no caption at all — the tick is a drawable and its id is the
# only thing that says which one it is — so this is the portal naming what
# it can see, the same way the agency filter is named.
IMAGE_MARK_KEYS = {
    "imgstarttime": "papp.mark.entry",
    "imgendtime": "papp.mark.exit",
}


def _mark_for(s: dict) -> str:
    """The state symbol a static stands for, or ''. Text glyphs and named
    state images are the two shapes marks arrive in."""
    txt = (s.get("txt") or "").strip()
    if txt:
        return ICON_MARKS.get(txt, "")
    return IMAGE_MARKS.get((s.get("rid") or "").lower(), "")


def _mark_key(s: dict) -> str:
    """What this mark is called, for the marks that stand for a moment in a
    visit rather than for a state in general. "" for the rest — a tick with
    no name renders exactly as it always did."""
    if (s.get("txt") or "").strip():
        return ""
    return IMAGE_MARK_KEYS.get((s.get("rid") or "").lower(), "")


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


def _kind(cls: str, role: str = "") -> str:
    """What to draw this as: its widget class, or the role the feed gave it.

    A role only ever arrives on a control the app declared in words rather
    than in attributes — HHAeXchange+'s care-plan ticks are plain Views whose
    description says "no seleccionado … Toca dos veces para alternar". The
    class stays what the phone published, because the class is half of what a
    tap verifies itself against; the role is only about the drawing.
    """
    if role in ("toggle", "button", "field", "image", "text"):
        return role
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
        # The widget class the app used, carried through because some
        # readings need to know a caption from a spent control: HHAeXchange's
        # visit detail publishes a used-up "Registrar entrada" as a
        # non-clickable BUTTON, and HHAeXchange+ publishes the tab you are on
        # as a non-clickable TEXTVIEW. Both arrive as statics; only one of
        # them is a tab. See `_slot_tabs`.
        "cls": node.get("cls", ""),
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


# What an app calls its own help, in both languages. Same whole-caption
# rule: "Ayuda con la firma" would be a real control and is not this.
_HELP_WORDS = frozenset((
    "ayuda", "help", "información", "informacion", "info",
    "más información", "mas informacion", "more information",
))


def _captions(item: dict) -> list[str]:
    """Every word this control offers as its own name.

    A nav button usually has NO text and one folded line — the description
    the app hangs on it for a screen reader. HHAeXchange+'s back arrow is
    exactly that: `menu_top_bar_back_button`, no text, described "Atrás".
    Reading only `txt` meant the word list below never saw a single one of
    them, so every app's back arrow and every app's help icon came through
    as an unlabelled "···" bubble in the corner. Reported as: "these
    bubbles on the top right with … are confusing".
    """
    words = [(item.get("txt") or "").strip()]
    lines = item.get("lines") or []
    if len(lines) == 1:
        words.append((lines[0] or "").strip())
    return [w.lower() for w in words if w]


# What TalkBack is told to say, which is not what the screen says. An app
# hangs these on a control so a screen reader can explain how to work it;
# the portal is not a screen reader, and the visit search's date range came
# through reading "Ingrese el formato de datos como mes, fecha, año, doble
# toque para activar" — a sentence about double-tapping, on a page where
# nothing is double-tapped.
_READER_INSTRUCTIONS = (
    "doble toque", "doble pulsación", "doble pulsacion", "toque dos veces",
    "pulse dos veces", "double tap", "double-tap", "swipe up or down",
    "deslice hacia arriba o hacia abajo",
)


def _is_screenreader_instruction(text: str) -> bool:
    """Whether this line is spoken instructions rather than content."""
    return any(m in (text or "").lower() for m in _READER_INSTRUCTIONS)


def _is_up_affordance(item: dict) -> bool:
    """Whether this control is the app's own version of Back."""
    return any(w in _UP_WORDS for w in _captions(item))


def _is_help_affordance(item: dict) -> bool:
    """Whether this control is the app's help icon.

    Dropped from the title bar, on the owner's instruction: "the question
    marks also sometimes confuse me, not a useful thing on our end honestly
    — I don't see me or my sister ever pressing that". It opens a
    documentation website, which is the one place the phone is not allowed
    to go, and unlabelled in the corner it has been mistaken for Back more
    than once. The help ROW inside a menu page is untouched — that is a
    destination she chose to walk to, not a bubble in the chrome.
    """
    if (item.get("aim") or {}).get("rid", "").lower() in (
            "help_button", "btn_info", "btn_help"):
        return True
    return any(w in _HELP_WORDS for w in _captions(item))


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
            # A row's state marks are named the same way. The EVV pair are
            # two identical ticks until they say which is which, and which
            # is which is the whole content of the record.
            walk(it.get("marks"))
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


# ------------------------------------------------- Android's own permission
# dialog
#
# WATCHED LIVE, AND IT RENDERED AS A MESS. The first automated check-in raised
# "Allow Inmyteam to access this device's location?" and the general reflow
# banded the whole dialog into ONE row: the question squeezed into a column
# two characters wide ("A l.."), the Precise/Approximate radios reduced to two
# bare chevrons and two unlabelled checkboxes, and the three stacked buttons
# laid out side by side as though they were a segmented control.
#
# The banding is not wrong in general — it is guessing at an app's layout from
# geometry, which is what it is for. But this screen never needs guessing at:
# it is Android's own, its resource-ids are fixed, and every app on the phone
# raises the identical dialog. So it is recognised and drawn as what it is.
#
# It matters more than an ordinary ugly screen. This is the dialog standing
# between a scheduled check-in and the record it exists to write, and the
# person answering it is being asked to make a permission decision from a
# phone in another state.
PERMISSION_DIALOG_ID = "grant_dialog"
PERMISSION_MESSAGE_ID = "permission_message"
PERMISSION_ACCURACY_ID = "permission_location_accuracy_radio_container"
# Which button means what, so the page can emphasise the affirmative and mark
# the refusal instead of showing three identical pills.
PERMISSION_BUTTONS = (
    ("permission_allow_foreground_only_button", "allow"),
    ("permission_allow_button", "allow"),
    ("permission_allow_always_button", "allow"),
    ("permission_allow_one_time_button", "once"),
    ("permission_deny_button", "deny"),
    ("permission_deny_and_dont_ask_again_button", "deny"),
)


def _short_id(value: str) -> str:
    return (value or "").rsplit("/", 1)[-1]


def _permission_dialog(doc: dict) -> dict | None:
    """The model for a permission prompt, or None if this is not one.

    Reads the published elements and statics like every other screen, so the
    aims are the ordinary verified ones and nothing here is a new capability.
    """
    elements = [e for e in doc.get("elements") or [] if e.get("b")]
    statics = [s for s in doc.get("statics") or [] if s.get("b")]
    ids = {_short_id(e.get("rid", "")) for e in elements}
    ids |= {_short_id(s.get("rid", "")) for s in statics}
    if PERMISSION_DIALOG_ID not in ids:
        return None

    def _caption(node):
        """Its own words, or the ones drawn over it."""
        word = (node.get("txt") or "").strip()
        if word:
            return word
        for s in statics:
            if s is node:
                continue
            text = (s.get("txt") or "").strip()
            if text and _contains(node["b"], s["b"]):
                return text
        return ""

    choices, actions = [], []
    for element in elements:
        rid = _short_id(element.get("rid", ""))
        if element.get("checkable") or rid.endswith(("_radio_fine",
                                                     "_radio_coarse")):
            word = _caption(element)
            if word:
                choices.append({"txt": word,
                                "on": bool(element.get("checked")),
                                "aim": _aim(element)})
            continue
        for known, tone in PERMISSION_BUTTONS:
            if rid == known:
                actions.append({"txt": _caption(element) or known,
                                "tone": tone, "aim": _aim(element)})
                break
    if not actions:
        return None

    # THE QUESTION IS WHATEVER TEXT IS LEFT OVER INSIDE THE DIALOG.
    #
    # Not matched on `permission_message`: the publisher does not carry that
    # node's resource-id, so an id rule found nothing on the very screen this
    # was written from — the question came back empty while sitting plainly
    # in the statics. Every control above has already claimed its own caption
    # from its own element, so any static text still inside the dialog and
    # not inside one of those controls is the sentence being asked.
    box = next((e["b"] for e in elements
                if _short_id(e.get("rid", "")) == PERMISSION_DIALOG_ID), None)
    spoken = {c["txt"] for c in choices} | {a["txt"] for a in actions}
    said = []
    for s in statics:
        text = (s.get("txt") or "").strip()
        if not text or text in spoken:
            continue
        if box is not None and not _contains(box, s["b"]):
            continue
        said.append(text)
    return {"message": " ".join(said), "choices": choices,
            "actions": actions, "box": box}


def _without(rows: list, box) -> list:
    """The rows with everything inside `box` taken out, and empties dropped.

    Used to stop the reflow repeating a dialog the page has already drawn
    properly. Anything OUTSIDE the box stays: a prompt can sit over a screen
    that still has content of its own around it.
    """
    if box is None:
        return rows
    kept = []
    for row in rows:
        items = [it for it in (row.get("items") or [])
                 if not _contains(box, it.get("b") or box)]
        if not items and row.get("items"):
            continue
        kept.append(dict(row, items=items) if row.get("items") else row)
    return kept


def _aim(element: dict) -> dict:
    return {"rid": element.get("rid", ""), "cls": element.get("cls", ""),
            "b": element.get("aim_b") or element["b"]}


# --------------------------------------------------------------- app alerts
#
# A MODAL THE APP PUT UP, AND THE REASON THE PAGE UNDER IT VANISHED.
#
# inMyTeam answers a check-in outside the visit's window with a small centred
# box — "Warning! / Invalid time / Accept" — and the portal rendered a page
# with almost nothing on it: a "Navigate up" button, a title, and three empty
# squares. Not the warning, not the patient, not the time, not the address,
# not the "Failed  Check in 11:55 PM" the app had just written.
#
# The cause is that a dialog is its own WINDOW, and the dump lists it BEFORE
# the activity behind it. The overlay rule reads document order as z-order —
# right for a surface an app slides over its own page, wrong for two windows —
# so the activity looked like a full-screen curtain that had arrived over the
# dialog, and everything "beneath" it was thrown away. The dialog was on top
# the whole time.
#
# Lifting the alert out before that pass fixes both halves at once: the alert
# is drawn as an alert, and with nothing left for the "curtain" to be covering
# the page beneath renders normally.
ALERT_ACTION_WORDS = (
    "accept", "aceptar", "ok", "okay", "entendido", "got it", "close",
    "cerrar", "dismiss", "descartar", "continue", "continuar", "aceptar",
)
# A dialog's button is small and its message sits right above it. Both are
# required: without the message this would claim any small OK-ish button on
# an ordinary form.
ALERT_ACTION_MAX_AREA = 0.06        # of the screen
ALERT_MESSAGE_REACH = 220           # px above the button the message may sit
ALERT_COLUMN = 0.42                 # how far off the button's centre it may sit


def _app_alert(doc: dict, w: int, h: int) -> dict | None:
    """A modal the app raised, or None.

    Returns the message lines, the button, and the box the whole thing
    occupies — the caller uses that box to take the alert out of the page.
    """
    elements = [e for e in doc.get("elements") or [] if e.get("b")]
    statics = [s for s in doc.get("statics") or [] if s.get("b")
               and (s.get("txt") or "").strip()]
    if not (w and h):
        return None

    for element in elements:
        box = element["b"]
        area = (box[2] - box[0]) * (box[3] - box[1])
        if area > w * h * ALERT_ACTION_MAX_AREA or area <= 0:
            continue
        caption = next((s for s in statics if _contains(box, s["b"])), None)
        if caption is None:
            continue
        if (caption.get("txt") or "").strip().lower() not in ALERT_ACTION_WORDS:
            continue
        # The message: text ABOVE the button, in the button's own column.
        middle = (box[0] + box[2]) / 2
        said = [s for s in statics
                if s is not caption
                and 0 <= box[1] - s["b"][3] <= ALERT_MESSAGE_REACH
                and abs((s["b"][0] + s["b"][2]) / 2 - middle) <= w * ALERT_COLUMN]
        if not said:
            continue
        said.sort(key=lambda s: s["b"][1])
        lines = [(s.get("txt") or "").strip() for s in said]
        whole = [min([box[0]] + [s["b"][0] for s in said]),
                 min([box[1]] + [s["b"][1] for s in said]),
                 max([box[2]] + [s["b"][2] for s in said]),
                 max([box[3]] + [s["b"][3] for s in said])]
        return {"title": lines[0], "lines": lines[1:],
                "action": {"txt": (caption.get("txt") or "").strip(),
                           "aim": _aim(element)},
                "box": whole}
    return None


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

    # A MODAL THE APP RAISED, LIFTED OUT BEFORE ANYTHING ELSE LOOKS AT THE
    # PAGE. See `_app_alert`: a dialog is its own window and the dump lists it
    # BEFORE the activity behind it, so leaving it in makes the activity look
    # like a curtain that arrived over it — and the overlay pass below throws
    # away the dialog AND the page. Taking it out first is what stops that.
    alert = _app_alert(doc, w, h)
    if alert is not None:
        elements = [e for e in elements
                    if not _contains(alert["box"], e["b"])]
        statics = [s for s in statics
                   if not _contains(alert["box"], s["b"])]

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
              and _kind(e.get("cls", ""), e.get("role", "")) == "row"
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
                  if _kind(e.get("cls", ""), e.get("role", "")) == "row"
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
        kind = _kind(e.get("cls", ""), e.get("role", ""))
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
        if kind == "button" and not item["txt"]:
            # A BUTTON WHOSE CAPTION IS DRAWN ON TOP OF IT.
            #
            # Buttons only, and that restriction was earned: a text field's
            # placeholder also sits inside its own box, and folding it in
            # here took "Buscar por nombre" off the search box and wrote it
            # on the field as a label. A field's hint has its own machinery
            # further down and this must not race it.
            #
            # All three care apps do this — the button carries no text of its
            # own and the word sits in a separate node laid over it. Rendered
            # as found, the button comes out blank and the word comes out
            # beside it as loose prose, which is what "those two icons next to
            # Borrar and Enviar are broken, they don't show anything" is: not
            # a broken icon, an empty button standing next to its own caption.
            #
            # STRICT CONTAINMENT, the same rule `_canvas_actions` uses. A
            # label inside the box belongs to the box; a label that merely
            # overlaps is a neighbour, and guessing there is how "Enviar"
            # ends up written on the Clear button.
            for i, s in enumerate(statics):
                if i in folded or s.get("step", 0) != e.get("step", 0):
                    continue
                word = (s.get("txt") or "").strip()
                if not word or _is_icon_text(word):
                    continue
                if _contains(e["b"], s["b"]):
                    item["txt"] = word
                    folded.add(i)
                    break
        if kind == "row":
            own = sorted(labels.get(id(e), []),
                         key=lambda s: (s["b"][1], s["b"][0]))
            lines, badge, marks = [], "", []
            for s in own:
                how, value = _fold_label(e["b"], s)
                # No line twice: the legacy home repeats a subtitle node in
                # its own hierarchy, and a cell reading the same sentence
                # two times looks broken even when the tree really does.
                if how == "line" and value and value not in lines \
                        and not _is_screenreader_instruction(value):
                    lines.append(value)
                elif how == "badge":
                    badge = value
                elif how == "mark":
                    marks.append((value, _mark_key(s)))
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
            # An icon's caption, once the row has real words of its own —
            # see `_is_icon_caption`. Taken away only when something is left
            # to say: the same kind of caption standing ALONE is what names
            # an unlabelled control, and the app's back arrow has nothing
            # else. Order is kept; only the decoration goes.
            captions = {(s.get("txt") or "").strip() for s in own
                        if _is_icon_caption(s)}
            if captions and any(ln not in captions for ln in lines):
                lines = [ln for ln in lines if ln not in captions]
            # A cell whose whole content is one generated sentence becomes
            # the list row that sentence describes. Only when the sentence
            # accounts for itself — see _shape_description.
            if len(lines) == 1:
                shaped = _shape_description(lines[0])
                if shaped:
                    # The sentence accounted for itself, so its last part is
                    # the visit's STATE — lifted out of the stack of muted
                    # lines and onto the row as a chip. Only from a shaped
                    # description: a third line anywhere else is just a
                    # third line, and eating it would lose content.
                    lines = shaped[:-1]
                    item["status"] = shaped[-1]
                    item["status_tone"] = status_tone(shaped[-1])
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
            item["marks"] = [
                {"sym": m, "tone": MARK_TONE.get(m, ""),
                 **({"txt_key": key} if key else {})}
                for m, key in marks]
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
        # THE SENTENCE A DETAIL PAGE OPENS WITH IS THAT PAGE'S HEADING.
        #
        # A list row's generated description already becomes a shaped cell;
        # the same sentence standing alone did not, because that shaping
        # only ever ran inside a tappable row. So Mobile Caregiver+'s visit
        # detail opened with three lines of grey prose — the patient's name
        # in square brackets mid-clause, the window buried after it, and the
        # visit's state, the one word the page exists to report, last in the
        # run and in the same grey as everything else.
        #
        # Broken into what it says: who, when, and a state that carries its
        # own colour. The date stays here (a detail page has no section
        # header stating it) and the status becomes a chip, for the reason
        # the list rows already give — three of this app's five states are
        # the outcomes this project exists to prevent, and they must not
        # read as one more muted line.
        summary = _describe(s["txt"])
        if summary:
            card = _item(s, "summary")
            card.update(summary)
            card["status_tone"] = status_tone(summary["status"])
            items.append(card)
            j += 1
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

    # A SEARCH BOX IS ONE CONTROL, not three things that happen to overlap.
    #
    # The patients tab publishes its box as an EditText spanning the width,
    # a separate caption "Buscar por nombre" sitting inside it, and a
    # magnifier button inside its right edge. Banded as they arrive, the
    # portal drew an empty rectangle, a loose phrase floating beside it, and
    # a "···" bubble — three items for one box, and nothing that looked like
    # somewhere to type a name.
    #
    # Anything INSIDE the field's own bounds belongs to the field: a word
    # becomes its placeholder, a button becomes the button it already is on
    # the phone, in the corner of the box where the app drew it.
    for band in bands:
        for field in [it for it in band if it["kind"] == "field"]:
            fx1, fy1, fx2, fy2 = field["b"]

            def _inside(it: dict, _f=(fx1, fy1, fx2, fy2)) -> bool:
                return (it is not field and _f[0] <= it["b"][0]
                        and it["b"][2] <= _f[2] and _f[1] <= it["b"][1]
                        and it["b"][3] <= _f[3])

            for it in [n for n in band if _inside(n)]:
                caption = (it.get("txt") or "").strip() or (
                    (it.get("lines") or [""])[0] or "").strip()
                if it["kind"] == "label" and caption and not field.get("hint"):
                    field["hint"] = caption
                elif it.get("aim") and not field.get("submit"):
                    field["submit"] = it["aim"]
                else:
                    continue
                band.remove(it)

    # ...and where the app writes the caption ABOVE the box instead of inside
    # it — the visit search's "Nombre del paciente" — that caption is the
    # box's own name just the same. Folded in as the placeholder so the field
    # says what it wants, rather than standing over an empty rectangle. It
    # has to be directly above, left-aligned with it, and alone on its line;
    # a heading with a field somewhere under it is not this.
    kept: list[list[dict]] = []
    for band in bands:
        fields = [it for it in band if it["kind"] == "field"]
        if (len(band) == 1 and len(fields) == 1 and not fields[0].get("hint")
                and kept and len(kept[-1]) == 1
                and kept[-1][0]["kind"] == "label"
                and (kept[-1][0].get("txt") or "").strip()):
            cap, field = kept[-1][0], fields[0]
            gap = field["b"][1] - cap["b"][3]
            if (0 <= gap <= (field["b"][3] - field["b"][1])
                    and abs(cap["b"][0] - field["b"][0]) <= max(8, 0.02 * w)):
                field["hint"] = cap["txt"].strip()
                kept.pop()
        kept.append(band)
    bands = kept

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

    # ...and HHAeXchange+ says the same thing a third way: by publishing the
    # tab she is on as no control at all. On the EVV screen only "Dispositivo
    # o FOB" is an element; "GPS", the tab that is open, is a bare centred
    # word. Rendered as they arrive, the portal showed a loose caption beside
    # one live button — the only thing that looked current was the tab she
    # was NOT on. Reported from the field during a real check-in: "the GPS
    # tab wasn't selected, it doesn't say which option is selected".
    for band in bands:
        parts = _slot_tabs(band, w)
        if parts:
            band[:] = [{"kind": "segment",
                        "b": [parts[0]["b"][0],
                              min(p["b"][1] for p in parts),
                              parts[-1]["b"][2],
                              max(p["b"][3] for p in parts)],
                        "parts": parts}]

    # THE THING SHE SIGNS ON IS NOT A ROW.
    #
    # HHAeXchange+'s canvas is a bare View half the screen wide with no words
    # in it, and every reading the reflow has drew it as something else: as a
    # blank list cell, and — once a screen reader's "double tap" was taken as
    # evidence of a control — as an on/off SWITCH, on the page where a
    # caregiver is trying to collect a patient's signature.
    #
    # It gets its own kind, and the page says what it is: an empty rectangle
    # is the one thing a signature screen must never look like.
    if doc.get("canvas"):
        for band in bands:
            for item in band:
                if _is_canvas_item(item, w, h):
                    item["kind"] = "canvas"
                    break
            else:
                continue
            break

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
            # WHERE SHE IS IN A WALK OF SEVERAL. A title bar carries one
            # title, so the shorter line beside it was simply dropped — and
            # on HHAeXchange+'s signature screen that line is "Paso 2 de 3",
            # which is the difference between the patient's signature and
            # the caregiver's. She has to know which one she is collecting
            # before she collects it, not after.
            rest = [n for n in honest if n is not title]
            step = min(rest, key=lambda n: n["b"][1], default=None)
            # The app's own up-arrow does not ride along. The pill already has
            # a Back and this would be a second control meaning the same
            # thing, three inches away, in different words — "Navigate up"
            # beside "Back", which is a question the caregiver has to stop and
            # answer mid-visit. Reported from the field exactly that way.
            #
            # Suppressed rather than re-pointed: the pill's Back sends the
            # phone's own Back, which is what this arrow does, so there is
            # nothing to chain it to that is not already there.
            #
            # Help goes with it, on the owner's instruction — see
            # `_is_help_affordance`. What is left is whatever the app put in
            # its title bar that is neither of those, and it rides as a
            # trailing control under its own name.
            controls = [b for b in buttons
                        if not _is_up_affordance(b)
                        and not _is_help_affordance(b)]
            nav = {
                "back": controls[0] if controls else None,
                "title": title["txt"] if title else "",
                "step": step["txt"] if step else "",
                "trailing": controls[1:],
            }
            bands = bands[1:]

    # THE APP'S OWN BACK, WHEREVER IT LANDED.
    #
    # Suppressing it in the title bar was only ever half the rule: it is a
    # duplicate of the pill's Back whether or not a nav bar was recognised,
    # and on a page where none was it fell into the LIST — a full-width row
    # reading "Navigate up", three inches from a Back that does the same
    # thing. Reported on inMyTeam's visit detail, and the reason a nav bar is
    # not recognised there is worth knowing: that page arrives STITCHED, so
    # the bar's page coordinate is wherever the scroll was (y592 on the
    # capture this was written from) and the "is it at the top" test says no.
    #
    # Fixing the stitched title bar properly is a separate job — this is the
    # part that matters to somebody using it, and it holds on every page.
    for band in bands:
        band[:] = [n for n in band
                   if not (n.get("aim") and _is_up_affordance(n))]
    bands = [band for band in bands if band]

    rows = []
    for band in bands:
        shape = _band_shape(band, h)
        rows.append({"items": band, **shape})
    # Android's own permission prompt, drawn as itself rather than banded
    # into a row of look-alike pills. None on every other screen.
    permission = _permission_dialog(doc)
    if permission is not None:
        # AND THE ROWS MUST NOT SAY IT AGAIN. Caught by photographing the
        # fix rather than reading the CSS: the new dialog rendered correctly
        # and the old mangled band was still sitting underneath it, so the
        # page offered the same three buttons twice — once properly and once
        # as the garble this was written to remove. Two "Don't allow"s on a
        # screen is worse than one badly drawn one.
        rows = _without(rows, permission["box"])
    return {"id": doc.get("id", ""), "nav": nav, "rows": rows,
            "apptabs": apptabs,
            # THE TRAIL THE APP SAYS IT WALKED — see `feed.nav_state`. Named
            # apart from `nav` above, which is the app's own top bar: this is
            # the fragment stack underneath it, and the two disagree often
            # enough that sharing a word would be a bug waiting to happen.
            # Empty for any app that does not publish one, and an empty trail
            # renders as nothing at all rather than as "you are nowhere".
            "crumbs": _crumbs(doc, (nav or {}).get("title", "")),
            "permission": permission,
            # A modal the app raised, drawn as an alert above the page it is
            # blocking rather than scattered through it — or dropped.
            "alert": alert,
            # The way out of a modal, when the screen is one. See DISMISS_IDS.
            "dismiss": dismiss,
            "notice": doc.get("notice", ""), "blocked": doc.get("blocked", ""),
            "webview": bool(doc.get("webview")),
            "scrollable": bool(doc.get("scrollable")),
            # Whether the rows above are the WHOLE page (a stitched walk)
            # or just the viewport — the footnote reads opposite ways.
            "full": bool(doc.get("full"))}


# WHAT EACH SCREEN CALLS ITSELF, learned by standing on it.
#
# The trail arrives as FRAGMENT CLASS NAMES, and prettifying one gives
# "Visits Route" — English, inside a Spanish app, in a word the app never
# uses and nobody but its developers has ever seen. Reported from the phone
# as a breadcrumb with no direction, and that is exactly right: it is not
# the app's language and it is not even the app's vocabulary.
#
# The app does say what a screen is called. It writes it in the title bar of
# that screen, in her language, and the portal already reads it. So every
# time a screen is rendered, the title is remembered against the fragment
# standing on it, and the trail is spelled with those words afterwards.
#
# Learned rather than mapped, for the same reason the trail itself is: a
# table of fragment names would be one more thing to maintain against apps
# that rename their internals, and it would be wrong the first time one did.
# Fragment names are per-package — two apps can each have a `HomeFragment`
# meaning quite different things — so the memory is keyed by both.
_screen_names: dict[str, dict[str, str]] = {}
# Bounded, because this grows with every screen ever visited and nothing
# else prunes it. Generous next to any real app's page count.
NAMES_REMEMBERED = 200


def remember_screen_name(package: str, fragment: str, title: str) -> None:
    """Note that this fragment calls itself `title`, in the app's own words."""
    title = (title or "").strip()
    if not package or not fragment or not title:
        return
    known = _screen_names.setdefault(package, {})
    if known.get(fragment) == title:
        return
    if len(known) >= NAMES_REMEMBERED and fragment not in known:
        known.pop(next(iter(known)), None)
    known[fragment] = title


def screen_name(package: str, fragment: str) -> str:
    """The app's own word for this screen, or "" if it has never been seen."""
    return (_screen_names.get(package) or {}).get(fragment, "")


# SCREENS THAT ARE BEHIND HER, NOT AHEAD OF HER.
#
# inMyTeam's back stack, read on the live phone, was TWENTY-TWO entries deep
# and began with the whole sign-in: Visits, Intro, Intro, Intro, …, Login,
# Verify Code, Schedule, Schedule Visit Detail. That is not a parsing
# mistake — the app really does push every step and never pops one — so the
# trail was factually right and completely useless, and it offered to walk
# her back into the passcode screen and the one where the texted code goes.
#
# A breadcrumb is how to step back, not everything since launch. Anything at
# or before the last sign-in screen is history she cannot return to and must
# not be invited to.
AUTH_FRAGMENT_MARKS = ("login", "signin", "verifycode", "verify", "otp",
                       "passcode", "intro", "splash", "onboard", "welcome")
# And a trail is short by nature. Four is the deepest any of these apps
# actually nests; beyond that it stops being a path and becomes a log.
CRUMB_MAX = 4


def _after_the_sign_in(trail: list[str]) -> list[str]:
    """The trail from just after the last sign-in screen, or all of it."""
    cut = -1
    for i, name in enumerate(trail[:-1]):     # never the screen she is on
        low = name.lower()
        if any(mark in low for mark in AUTH_FRAGMENT_MARKS):
            cut = i
    return trail[cut + 1:]


def _crumbs(doc: dict, title: str = "") -> list[dict]:
    """The app's own trail through itself, as a row of steps.

    Each step carries `back`: how many pops separate it from where the phone
    is standing. Zero is here. THAT NUMBER IS POPS, NOT PRESSES, and the
    difference is not pedantry — watched live, two Back presses on the work
    log were swallowed undoing its tab selection and popped nothing, while
    the screen's own Back arrow popped cleanly. So a step is something to
    walk towards, checking after each press, never a count to fire blindly.

    Spelled in the app's own words wherever the portal has stood on that
    screen and read its title — see `remember_screen_name`. A step it has
    never visited keeps the prettified class name, which is ugly but honest;
    it turns into the app's word the first time she goes there.
    """
    nav = doc.get("nav") or {}
    whole = nav.get("trail") or []
    says_all = nav.get("says") or []
    package = (doc.get("app") or "")
    if whole and title:
        remember_screen_name(package, whole[-1], title)
    # The pops are counted from the WHOLE stack, always: what is shown is a
    # question of usefulness, what a step costs to walk back to is a fact
    # about the phone, and confusing the two would send Back the wrong
    # number of times.
    depth_of = {name: i for i, name in enumerate(whole)}
    last_of_all = len(whole) - 1
    trail = _after_the_sign_in(whole)[-CRUMB_MAX:]
    says = [says_all[depth_of[n]] if depth_of.get(n, -1) < len(says_all)
            else n for n in trail]
    if len(trail) < 2:
        # One step is not a trail, it is just where you are. The nav bar
        # already says that, and a breadcrumb of length one is furniture.
        return []
    last = len(trail) - 1
    out = []
    for i, name in enumerate(trail):
        # The screen she is standing on is titled above; use that same word
        # rather than a second, different name for one place.
        known = title if i == last and title else screen_name(package, name)
        if not known and i != last:
            # A STEP NOBODY HAS EVER STOOD ON. The stack carries container
            # fragments that are not screens — a "route" holding the pages
            # inside it — and they have no title because they were never
            # drawn as a place. They are what made this trail read "Visits
            # Route › Visits": a developer's word for a thing that is not a
            # destination. Left out rather than printed under its class
            # name; dropping it changes no other step's `back`, which is
            # counted from the stack, not from what is shown.
            continue
        out.append({"at": name,
                    "says": known or (says[i] if i < len(says) else name),
                    "back": last_of_all - depth_of.get(name, last_of_all),
                    "here": i == last})
    if len(out) < 2:
        # What is left is only "you are here", which the title already says.
        return []
    return out


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

# An ACTION bar's own reach, and looser on purpose. A tab bar is pinned to
# the very edge; a page's action pair sits above whatever inset the app keeps
# for the system gesture bar, so demanding it touch the edge asks for
# something it never does. HHAeXchange+'s signature screen missed by three
# pixels — its Borrar and Enviar end at 681 of 720, 94.6% — and Borrar came
# out as a list row with a chevron, pointing at somewhere to browse, next to
# the button that submits a patient's signature.
ACTION_REACH = 0.92


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
               and _kind(e.get("cls", ""), e.get("role", "")) == "row"]
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


# A canvas is the biggest wordless thing on a screen the feed has already
# marked as holding one. Deliberately not a re-implementation of the replay's
# own finder: that one has to be certain enough to put ink somewhere, this one
# only has to be certain enough to draw a panel, and the two answering
# slightly differently costs a panel in the wrong place rather than a
# signature in the wrong place.
CANVAS_ITEM_MIN_SHARE = 0.15


def _is_canvas_item(item: dict, width: int, height: int) -> bool:
    """Whether this item is the surface she signs on."""
    if item["kind"] not in ("row", "toggle", "button", "image"):
        return False
    if (item.get("txt") or "").strip() or item.get("lines"):
        return False
    b = item["b"]
    return ((b[2] - b[0]) * (b[3] - b[1])
            >= width * height * CANVAS_ITEM_MIN_SHARE)


def _slot_tabs(band: list[dict], width: int) -> list[dict] | None:
    """A tab bar whose current tab was published as a caption, not a control.

    Returns the parts of the segment it should become, in screen order, or
    None when this band is not that. The test is entirely geometric, because
    the tree gives nothing else to go on: the open tab has no click handler,
    no state flag, and no name of its own — it is a word, and the only thing
    that makes it a tab is where the word sits.

    So: every tappable in the band is the same big width, that width divides
    the screen into a whole number of slots, every item sits on its slot's
    centre line, and no slot is either empty or doubly occupied. A list row
    fails on the centre line, a chip row fails on the width, and a partial
    match fails outright rather than guessing — a tab bar drawn with the
    wrong tab lit is worse than one drawn plainly.
    """
    tabs = [i for i in band if i["kind"] != "label" and i.get("aim")]
    # A caption, not a spent control. Both arrive as statics and both sit in
    # their slot, so the class is the only thing that tells them apart: the
    # legacy visit detail's used-up "Registrar entrada" is a non-clickable
    # BUTTON beside a live Clock Out, and reading that pair as a tab bar
    # would have the portal announce "you are on the check-in tab" about two
    # things that are not tabs and not a choice.
    captions = [i for i in band
                if i["kind"] == "label" and i.get("cls") in TEXTS]
    if not tabs or not captions or len(tabs) + len(captions) != len(band):
        return None
    if not _tiled(band):
        return None
    span = max(t["b"][2] - t["b"][0] for t in tabs)
    if span < width * TAB_SLOT_MIN_WIDTH:
        return None
    if any(abs((t["b"][2] - t["b"][0]) - span) > 0.1 * span for t in tabs):
        return None
    slots = round(width / span)
    if slots < 2 or abs(slots * span - width) > 0.1 * width:
        return None

    seat_width = width / slots
    tolerance = width * TAB_SLOT_TOLERANCE
    seats: dict[int, dict] = {}
    for item in band:
        centre = (item["b"][0] + item["b"][2]) / 2
        seat = int(centre // seat_width)
        if abs(centre - (seat + 0.5) * seat_width) > tolerance or seat in seats:
            return None
        seats[seat] = item
    if len(seats) != slots:
        return None

    parts = []
    for seat in sorted(seats):
        item = seats[seat]
        part = dict(item)
        # The caption IS the open tab: it is the one the app took the click
        # handler off. Nothing to press, and the template draws a checked
        # part as "you are here" rather than as a disabled control.
        part["checked"] = item["kind"] == "label"
        if part["checked"]:
            part["aim"] = None
        # A segment part shows one caption, and a folded row keeps its own in
        # `lines` — the same move the disabled pair above makes.
        if not (part.get("txt") or "").strip() and part.get("lines"):
            part["txt"] = part["lines"][0]
        if not (part.get("txt") or "").strip():
            return None
        parts.append(part)
    return parts


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
            and all(i["aim"]["b"][3] >= height * ACTION_REACH
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
