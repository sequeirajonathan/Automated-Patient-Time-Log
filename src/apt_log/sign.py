"""Replay a locally-drawn signature onto the app's own canvas.

The mirror made every step of a visit workable from another floor except one.
A signature is a continuous stroke, and no mirror is fast enough to draw one
through: each movement would cross the network twice before the ink appeared.
Observed on the first real clock-out — the signature screen was reached
remotely and then could not be signed.

So the drawing happens where there is no latency at all: on her own phone, on
a pad in the portal. What crosses the network is the finished strokes, once,
and the controller replays them onto the app's canvas in one motion through
the resident session.

**Replay draws. It never commits.** The app's save button is not pressed here
under any outcome — the strokes land on the canvas and the screen then shows
her signature, and pressing "Salvar" stays her tap on a screen she is looking
at. Same line the macros hold: fill, not commit.

**The strokes land on a canvas or nowhere.** Replay refuses unless it can find
exactly one signature-canvas-shaped element on the current screen, and every
replayed point is confined to that element's rectangle. There is no mode in
which this module taps a button: a stray point becomes a dot of ink, not a
press. Refusals are answers ("this screen has no signature box"), not errors.

**Nothing re-stampable is kept** (REQ-10.6). The request file is deleted the
moment it is claimed, before the replay is attempted, and the status file
carries a digest and an outcome — never the strokes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

STATE_DIR = Path("/var/lib/aptlog")
REQUEST_PATH = STATE_DIR / "sign-request.json"
STATUS_PATH = STATE_DIR / "sign-status.json"

# A signature drawn for a screen that has since moved on must not land on
# whatever replaced it. Tighter than the macro TTL because the strokes are
# re-stampable in a way a macro name is not.
REQUEST_MAX_AGE = 90.0

# Bounds on what a request may carry. Forty strokes is an elaborate signature;
# three thousand points is a slow, careful one. Anything past these is not a
# signature, whatever it is.
MAX_STROKES = 40
MAX_POINTS = 3000

# How far outside the pad a point may stray and still be part of a signature.
#
# The pad clamps at the source now, so nothing should arrive outside at all;
# this is for the browser that has the old script cached, and for the rounding
# a fractional device pixel can produce. It exists because the old rule —
# strictly 0..1, refuse the WHOLE payload otherwise — meant one finger sliding
# a little past the edge bounced an entire signature with a bare "failed" and
# left her looking at strokes that would not send.
#
# A quarter of the pad is generous for a slip and still nowhere near a payload
# that is not a signature, which is what this check is really for. Anything
# inside it is clamped onto the canvas by build_paths, which already does that.
OVERSHOOT = 0.25

# The apps whose canvases may be signed. Replay refuses anywhere else — the
# launcher, a settings page, another app entirely.
APP_PACKAGES = (
    "com.hhaexchange.caregiver",
    "com.hhaexchange.uma",
    "com.tellus.evv.v2",
    "com.inmyteam.inmyteam",
)

# How a signature canvas is recognised. By resource-id when the app names one;
# otherwise by shape: a custom drawing view surfaces in the hierarchy as a bare
# "View" (uiautomator reports the base class), with no text, covering a large
# share of the screen. Both rules are deliberately narrow — the finder would
# rather refuse than guess, because "no signature box here" costs a retry and a
# wrong rectangle costs ink on the wrong control.
CANVAS_ID_HINTS = ("signature", "firma", "sign_pad", "draw")
# Apps as often ship the canvas as a CUSTOM CLASS with no resource-id at
# all — SignatureView, SignaturePad, DrawingView — which the id hints
# never see and the bare-View shape rule rejects. The class's own name is
# as strong a signal as an id. The only live refusal so far (no_canvas,
# Aug 14, the one replay ever attempted against the real screen) is
# consistent with exactly this shape.
CANVAS_CLASS_HINTS = ("signature", "signpad", "drawing", "sketch")
CANVAS_CLASSES = ("View",)
CANVAS_MIN_SHARE = 0.22

# Ink stays off the canvas edge. The app's own border, a watermark line, an "X"
# baseline mark — the margin keeps the replay clear of all of it.
CANVAS_INSET = 0.06

_NODE = re.compile(r"<[A-Za-z][\w.$]*[^>]*>")
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass
class Status:
    id: str = ""
    state: str = "idle"       # idle | running | done | failed
    reason: str = ""          # no_canvas | ambiguous | wrong_app | replay_failed
    digest: str = ""          # sha256 of the strokes, for the audit trail
    kind: str = ""            # "" for a replay; "clear"/"confirm" for a press
    at: str = field(default_factory=lambda: datetime.now().isoformat())


def _attr(node: str, name: str) -> str:
    m = re.search(rf'{name}="([^"]*)"', node)
    return m.group(1) if m else ""


# ------------------------------------------------------------------- request
def validate(strokes) -> bool:
    """Whether a payload is signature-shaped. Everything else is refused.

    Accepts both shapes the pad has produced over its life: a stroke as a bare
    list of points, or as {"points": [...]} with extras like pen width.
    """
    if not isinstance(strokes, list) or not strokes:
        return False
    if len(strokes) > MAX_STROKES:
        return False
    total = 0
    for stroke in strokes:
        points = stroke.get("points") if isinstance(stroke, dict) else stroke
        if not isinstance(points, list) or not points:
            return False
        total += len(points)
        if total > MAX_POINTS:
            return False
        for p in points:
            if not (isinstance(p, list) and 2 <= len(p) <= 3):
                return False
            x, y = p[0], p[1]
            if not (isinstance(x, (int, float)) and isinstance(y, (int, float))):
                return False
            if not (-OVERSHOOT <= x <= 1 + OVERSHOOT
                    and -OVERSHOOT <= y <= 1 + OVERSHOOT):
                return False
    return True


def request(strokes, aspect: float = 1.0, path: Path | None = None) -> str:
    """Ask for a replay. Returns the request id."""
    if not validate(strokes):
        raise ValueError("that is not a signature")
    target = path or REQUEST_PATH
    rid = uuid.uuid4().hex[:12]
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"id": rid, "strokes": strokes,
                               "aspect": aspect, "at": time.time()}),
                   encoding="utf-8")
    os.replace(tmp, target)
    log.info("signature replay requested (%s)", rid)
    return rid


def take_request(path: Path | None = None) -> dict | None:
    """Claim a pending request. The file is deleted before anything is tried,
    so a crash mid-replay cannot leave strokes on disk (REQ-10.6)."""
    target = path or REQUEST_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        target.unlink()
    except OSError:
        pass
    if time.time() - float(payload.get("at", 0)) > REQUEST_MAX_AGE:
        log.info("ignoring a stale signature request")
        return None
    if not validate(payload.get("strokes")):
        return None
    return payload


# -------------------------------------------------------------------- status
def write_status(status: Status, path: Path | None = None) -> None:
    target = path or STATUS_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(status.__dict__), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("cannot publish signature status (%s)", exc)


def read_status(path: Path | None = None) -> Status:
    target = path or STATUS_PATH
    try:
        return Status(**json.loads(target.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError):
        return Status()


# -------------------------------------------------------------------- finder
def find_canvas(xml: str, dump: bool = True) -> tuple[list[int] | None, str]:
    """The signature canvas on this screen, or why there is none.

    Returns (bounds, "") or (None, reason). Exactly one candidate is the only
    acceptable answer: zero means this is not a signature screen, and two means
    the shape rule matched something it should not have — both are refusals,
    because a wrong rectangle puts ink on the wrong control.
    """
    named = []
    shaped = []

    # The screen's own extent, for the area share. Falls back to the largest
    # bounds seen, which also handles the rotated signature screen without
    # asking anyone what the orientation is.
    max_x = max_y = 0
    nodes = []
    for raw in _NODE.findall(xml or ""):
        m = _BOUNDS.search(_attr(raw, "bounds"))
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        nodes.append((raw, [x1, y1, x2, y2]))
        max_x, max_y = max(max_x, x2), max(max_y, y2)

    screen_area = max_x * max_y
    if not screen_area:
        return None, "no_canvas"

    for raw, b in nodes:
        rid = _attr(raw, "resource-id").split("/")[-1].lower()
        cls = (_attr(raw, "class") or "").rsplit(".", 1)[-1]
        if any(hint in rid for hint in CANVAS_ID_HINTS):
            named.append(b)
            continue
        if any(hint in cls.lower() for hint in CANVAS_CLASS_HINTS):
            named.append(b)
            continue
        if (cls in CANVAS_CLASSES
                and not _attr(raw, "text")
                and _attr(raw, "clickable") != "true"
                and (b[2] - b[0]) * (b[3] - b[1]) >= screen_area * CANVAS_MIN_SHARE):
            shaped.append(b)

    # Hint-matched candidates NEST: the live refusal was two full-page
    # "layout_tab_content_signature*" wrappers around the one real canvas
    # (gesturePatientSignature). A wrapper that wholly contains another
    # candidate is wrapping, not drawing — the innermost is the canvas.
    def _wraps(outer, inner):
        return (outer is not inner
                and outer[0] <= inner[0] and outer[1] <= inner[1]
                and outer[2] >= inner[2] and outer[3] >= inner[3])

    def _distinct(boxes):
        seen, out = set(), []
        for b in boxes:
            key = tuple(b)
            if key not in seen:
                seen.add(key)
                out.append(b)
        return out

    # TWO NAMES FOR ONE RECTANGLE ARE NOT TWO CANVASES. The live refusal on a
    # real clock-out was `layout_tab_content_signature_skip` and
    # `layout_tab_content_signature` at IDENTICAL bounds [0,120,720,1532]: the
    # de-nesting rule below asks whether one candidate wraps another, and on
    # equal rectangles each wraps the other, so BOTH were dropped and the
    # finder reported `no_canvas`. Downstream that is the toast reading "This
    # screen has no signature box" over a screen plainly showing one, a Save
    # that could never fire, and a caregiver pressing the app's own button by
    # hand at 10pm. De-duplicate first, keeping document order.
    #
    # BOTH POOLS NEED THIS, and for a while only `named` got it. inMyTeam's
    # signature sheet names nothing — no id, no telling class — so it takes
    # the SHAPED path, and Compose hands that path the bottom sheet's own
    # wrapper TWICE at identical bounds plus the real canvas inside it:
    #
    #   View [0,923,720,1561]   39.9% of the screen   the sheet
    #   View [0,923,720,1561]   39.9%                 the sheet again
    #   View [13,998,707,1469]  28.4%                 the canvas
    #
    # Three candidates, so the finder refused "ambiguous" and she could not
    # sign at all — caught on the live sheet, and intermittent because the
    # number of wrappers Compose emits varies. De-duplicated and de-nested
    # the same way, the wrappers collapse to one and then drop for containing
    # the canvas, leaving exactly the one thing that draws.
    def _narrow(boxes):
        if len(boxes) > 1:
            boxes = _distinct(boxes)
        if len(boxes) > 1:
            boxes = [b for b in boxes
                     if not any(_wraps(b, other) for other in boxes)]
        return boxes

    pool = _narrow(named) or _narrow(shaped)
    if not pool:
        if dump:
            _dump_refusal(nodes, "no_canvas")
        return None, "no_canvas"
    if len(pool) > 1:
        if dump:
            log.info("signature finder: %d candidates, refusing", len(pool))
            _dump_refusal(nodes, "ambiguous")
        return None, "ambiguous"
    return pool[0], ""


DEBUG_PATH = STATE_DIR / "sign-debug.json"


# Captions the refusal record may keep, and nothing else.
#
# The record deliberately holds no text at all, because a signature screen
# shows a patient's name and a caregiver's. But the one question it cannot
# currently answer is the one that matters — WHICH BUTTON IS WHICH — and that
# needs captions. So it keeps a caption only when the caption is one of these
# words: chrome, in either language, that no name can collide with.
#
# Written after an evening spent inferring button identity from screenshots
# and rotation arithmetic, and getting it wrong twice. The next real signature
# should simply say.
_CONTROL_WORDS = frozenset((
    "borrar", "salvar", "anular", "atrás", "atras", "cancelar", "guardar",
    "limpiar", "aceptar", "de acuerdo", "firmar",
    "clear", "save", "cancel", "back", "ok", "done", "sign", "submit",
))


def _control_word(raw: str) -> str:
    """The node's caption if it is known chrome, otherwise nothing."""
    txt = _attr(raw, "text").strip()
    return txt if txt.lower() in _CONTROL_WORDS else ""


def _nodes_of(xml: str) -> list:
    """(raw, bounds) for every node with a real rectangle — what the refusal
    dump wants, without making its caller rebuild the finder's own loop."""
    out = []
    for raw in _NODE.findall(xml or ""):
        m = _BOUNDS.search(_attr(raw, "bounds"))
        if not m:
            continue
        b = [int(g) for g in m.groups()]
        if b[2] > b[0] and b[3] > b[1]:
            out.append((raw, b))
    return out


def _dump_refusal(nodes, reason: str) -> None:
    """The screen's structure, kept when the finder refuses.

    The only signature screen this system ever faced was seen once, at
    night, and its hierarchy was gone by morning — the flight recorder
    had rolled and the refusal reason alone said nothing. One failed
    attempt should hand over the fix: class, id, bounds and clickability
    for every node, and not one character of text (the flight recorder's
    own discipline — a signature screen shows patient and caregiver
    names).
    """
    try:
        doc = {"reason": reason, "at": datetime.now().isoformat(),
               "nodes": [{"cls": (_attr(raw, "class") or "").rsplit(".", 1)[-1],
                          "rid": _attr(raw, "resource-id").split("/")[-1],
                          "clickable": _attr(raw, "clickable"),
                          # Chrome captions only — see _CONTROL_WORDS.
                          "word": _control_word(raw),
                          "b": b}
                         for raw, b in nodes]}
        tmp = DEBUG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        tmp.replace(DEBUG_PATH)
        log.info("signature refusal recorded for repair (%s)", reason)
    except OSError as exc:
        log.warning("could not record the refusal (%s)", exc)


# ----------------------------------------------------------------- sideways
# The legacy signature page draws its whole UI rotated a quarter turn while
# the activity stays portrait: the baseline runs up the device's left edge,
# the caption down its right, and the phone is meant to be turned sideways
# to sign. The hierarchy betrays it — single-line labels ("Firma del
# cuidador") occupy boxes taller than they are wide, which horizontal text
# never does. Two such labels and a portrait extent mean the presentation
# is rotated; a truly landscape screen (wide bounds) needs no help, because
# injected coordinates already follow the rotated display.
_ROT_LABEL_MIN_CHARS = 6
_ROT_LABEL_MAX_W = 40
_ROT_LABEL_MIN_H = 60


def presentation_rotated(xml: str) -> bool:
    """Whether this screen draws its content turned 90° inside a portrait
    activity — the shape strokes must be turned to match."""
    max_x = max_y = 0
    tall_labels = 0
    for raw in _NODE.findall(xml or ""):
        m = _BOUNDS.search(_attr(raw, "bounds"))
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        max_x, max_y = max(max_x, x2), max(max_y, y2)
        w, h = x2 - x1, y2 - y1
        if (len(_attr(raw, "text")) >= _ROT_LABEL_MIN_CHARS
                and w <= _ROT_LABEL_MAX_W
                and h >= max(_ROT_LABEL_MIN_H, 3 * w)):
            tall_labels += 1
    return max_x < max_y and tall_labels >= 2


# The legacy app draws its signature pages turned a quarter turn inside a
# portrait activity, and the tree shows NONE of it: the sideways captions
# are laid out as ordinary wide boxes and rotated only in the drawing pass
# uiautomator cannot see. The label heuristic above found nothing tall and
# a real signature replayed unturned (seen live, first field test, on the
# night's second attempt). For these apps the page itself is the evidence:
# a signature canvas on a portrait screen is a sideways canvas, always —
# the app's truly-landscape variant announces itself with wide bounds and
# needs no turn.
ROTATED_CANVAS_APPS = ("com.hhaexchange.caregiver",)


def _extent(xml: str) -> tuple[int, int]:
    max_x = max_y = 0
    for raw in _NODE.findall(xml or ""):
        m = _BOUNDS.search(_attr(raw, "bounds"))
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if x2 > x1 and y2 > y1:
            max_x, max_y = max(max_x, x2), max(max_y, y2)
    return max_x, max_y


# The share of the screen a canvas must claim before the page counts as a
# signature MOMENT rather than a page that merely remembers one. The
# completed visit detail keeps the saved signatures' wrappers in its tree,
# and mere canvas marks held the portal's peek sideways on an upright page
# (seen live, first field test). Both live signature screens clear these
# comfortably: the full page at ~94% of the height, the tab variant at 41%.
CANVAS_DOMINANT_W = 0.6
CANVAS_DOMINANT_H = 0.35


def sideways(xml: str, package: str = "") -> bool:
    """Whether ink replayed onto this screen must turn a quarter turn.

    True only for a signature MOMENT: the replay's own finder accepts the
    screen (exactly one canvas) and that canvas dominates it. The peek and
    the ink turn by this same answer — they must never disagree.
    """
    if presentation_rotated(xml):
        return True
    if package not in ROTATED_CANVAS_APPS:
        return False
    # A DRAWING SURFACE, or the pad's own captioned save. Either is evidence;
    # the tab WRAPPER alone is not.
    #
    # `layout_tab_content_signature` sits on this app's visit page whether or
    # not the pad is showing. Until now the finder happened to reject that page
    # anyway — the two identical wrappers annihilated each other — so the
    # wrapper never got the chance to be mistaken for a moment. Fixing that
    # annihilation takes the accidental protection away, and without something
    # in its place the peek would turn sideways over an ordinary visit page and
    # offer a Save with nothing to save.
    #
    # A real canvas (`gestureSignature`, `gesturePatientSignature` — seen on
    # the first field signature) is a drawing surface and settles it by itself.
    # Where the app publishes only wrappers, as it did on the screen that
    # failed tonight, the captioned Salvar is what appears and disappears with
    # the pad.
    if not (_has_drawing_surface(xml) or _app_buttons(xml).get("confirm")):
        return False
    max_x, max_y = _extent(xml)
    if not max_x or max_x >= max_y:
        return False
    bounds, _ = find_canvas(xml, dump=False)
    if bounds is None:
        return False
    return ((bounds[2] - bounds[0]) >= CANVAS_DOMINANT_W * max_x
            and (bounds[3] - bounds[1]) >= CANVAS_DOMINANT_H * max_y)


# --------------------------------------------------------------------- paths
def build_paths(strokes, bounds: list[int],
                aspect: float = 1.0,
                rotate: bool = False) -> list[list[tuple[int, int]]]:
    """Device-pixel stroke paths, confined to the canvas rectangle.

    `aspect` is the pad's own width over height. The pad normalises each axis
    to 0..1, so without it a signature drawn on a wide pad would land squashed
    into a square. One uniform scale for both axes, centred — a signature
    stretched to fill a canvas of a different shape stops looking like the one
    she drew. Every point is clamped to the inset rectangle besides; the
    mapping should never produce an outside point, and the clamp is for the
    day something does.

    `rotate` turns the signature a quarter turn for the sideways-drawn
    page: pad top goes to the device's right edge, pad left to its top —
    (u, v) becomes (1-v, u) — so the ink reads correctly when the content
    is viewed the way the app draws it. The pad's aspect inverts with it.
    """
    aspect = min(max(float(aspect or 1.0), 0.2), 8.0)
    if rotate:
        aspect = 1.0 / aspect
    x1, y1, x2, y2 = bounds
    inset_x = (x2 - x1) * CANVAS_INSET
    inset_y = (y2 - y1) * CANVAS_INSET
    left, top = x1 + inset_x, y1 + inset_y
    width, height = (x2 - x1) - 2 * inset_x, (y2 - y1) - 2 * inset_y

    # Fit the pad's rectangle uniformly inside the canvas, centred.
    k = min(width / aspect, height)
    draw_w, draw_h = k * aspect, k
    off_x = left + (width - draw_w) / 2
    off_y = top + (height - draw_h) / 2

    paths = []
    for stroke in strokes:
        points = stroke.get("points") if isinstance(stroke, dict) else stroke
        path = []
        for p in points:
            # Onto the pad first. A point that strayed past the edge (see
            # OVERSHOOT) rides the edge; mapping it raw would push the ink
            # outside the centred draw box and only the canvas clamp below
            # would catch it, landing the stroke on the wrong margin.
            u = min(max(float(p[0]), 0.0), 1.0)
            v = min(max(float(p[1]), 0.0), 1.0)
            if rotate:
                u, v = 1.0 - v, u
            px = off_x + u * draw_w
            py = off_y + v * draw_h
            px = min(max(px, left), left + width)
            py = min(max(py, top), top + height)
            path.append((int(px), int(py)))
        if path:
            paths.append(path)
    return paths


# ------------------------------------------------------------ app buttons
# The signature pages draw their own Borrar and Salvar in the same rotated
# pass that hides the captions: the tree holds NO trace of either button —
# the first field test's recordings show the full underlying visit page
# and not one signature control. So the portal cannot aim at them as
# elements; their place is derived from the one thing the finder does
# see, the canvas. On the observed full-page layout the pair sits in the
# strip LEFT of the canvas at its foot (the page's bottom-right corner,
# read sideways): Borrar centred ~62px above the canvas foot, Salvar
# ~22px, both at the strip's midline.
ACTION_PATH = STATE_DIR / "sign-action.json"
# A button press answers a screen someone is looking at right now.
ACTION_MAX_AGE = 20.0
ACTIONS = ("clear", "confirm")

_BUTTON_CLEAR_LIFT = 62
_BUTTON_CONFIRM_LIFT = 22
_MIN_STRIP = 12


# What the app calls its own save and clear.
#
# THE CAPTION IS THE SIGNAL, AND THE RESOURCE-ID IS NOT. Both screens of this
# activity publish a `button_save`, and they are not the same button:
#
#   signature tab        btn_left='Anular'   button_save='Salvar'   ← the save
#   plain visit detail   btn_left='Atrás'    button_save=''         ← the ✎ icon
#
# Same id, no caption, a different job, sitting on a real visit record. A rule
# that trusted the id would have pressed the pencil, so the caption is
# required and the id is only corroboration. A captioned button that this
# misses costs a refusal, which is now recorded; a wrong press costs a record.
#
# ANULAR IS NEITHER of these — it is cancel, it throws the signature away, and
# a rule loose enough to catch it would eventually press it.
_SAVE_IDS = ("button_save", "btn_save", "save_button")
_CLEAR_IDS = ("button_clear", "btn_clear", "clear_button", "button_erase")
_SAVE_WORDS = ("salvar", "save", "guardar")
_CLEAR_WORDS = ("borrar", "clear", "limpiar")


# Ids that name a LAYOUT holding the pad rather than the pad itself. A
# wrapper is present whether or not the thing it wraps is showing.
_WRAPPER_ID_MARKS = ("layout_", "tab_content", "_skip")


def _has_drawing_surface(xml: str) -> bool:
    """Whether a canvas-named node is a surface rather than a container."""
    for raw in _NODE.findall(xml or ""):
        rid = _attr(raw, "resource-id").split("/")[-1].lower()
        if not rid or not any(h in rid for h in CANVAS_ID_HINTS):
            continue
        if not any(mark in rid for mark in _WRAPPER_ID_MARKS):
            return True
    return False


def _app_buttons(xml: str) -> dict:
    """The app's own save/clear as REAL elements, by their centre.

    Read off the live signature screen, the legacy app publishes these
    perfectly well:

        Button  btn_left     'Anular'  clickable  [10,75][100,109]
        Button  button_save  'Salvar'  clickable  [624,75][714,109]

    which means the save this whole path exists to press was never a pixel
    that had to be guessed at. Pressing the element is better than pressing
    an offset from the canvas in every way that matters: it survives a
    density change (the caregiver had to change the density by hand to reach
    the button one night), it survives a redesign that moves the strip, and
    it cannot land on the drawing.
    """
    found: dict[str, dict] = {}
    for raw in _NODE.findall(xml or ""):
        if _attr(raw, "clickable") != "true":
            continue
        m = _BOUNDS.search(_attr(raw, "bounds"))
        if not m:
            continue
        b = [int(g) for g in m.groups()]
        if b[2] <= b[0] or b[3] <= b[1]:
            continue
        full = _attr(raw, "resource-id")
        rid = full.split("/")[-1].lower()
        txt = _attr(raw, "text").strip()
        low = txt.lower()
        for kind, ids, words in (("confirm", _SAVE_IDS, _SAVE_WORDS),
                                 ("clear", _CLEAR_IDS, _CLEAR_WORDS)):
            if kind in found:
                continue                      # first match wins, in tree order
            if low in words or (low and any(i in rid for i in ids)):
                found[kind] = {"rid": full, "txt": txt,
                               "b": b,
                               "xy": [(b[0] + b[2]) // 2, (b[1] + b[3]) // 2]}
    return found


def button_targets(xml: str, package: str = "") -> dict | None:
    """Where the app's own clear and save sit, or None off the moment.

    Only on a sideways signature moment. The app's own elements are used
    where it publishes them; the canvas-relative offsets below are the
    fallback for a screen that draws its buttons as pixels and nothing else,
    which is what the first signature page seen here appeared to do.

    Each kind is answered independently, because a screen that names its
    save and draws its clear should still be able to save.
    """
    if not sideways(xml, package):
        return None
    targets: dict[str, dict] = {}
    for kind, el in _app_buttons(xml).items():
        # PRESSED AS AN ELEMENT, NOT AS A POINT. This app rotates its
        # signature page in the drawing pass, and the tree describes the
        # UNROTATED layout — so a coordinate taken from these bounds is not
        # reliably where the button is drawn. Letting Android dispatch to the
        # element sidesteps the question entirely, which on a screen where a
        # wrong press saves the wrong thing is the only acceptable answer.
        targets[kind] = {"element": el}

    # The pixel fallback, for whatever the app did not name. It needs the
    # canvas to leave a strip down the left for the buttons to live in;
    # where the canvas runs the full width there is no strip and nothing
    # here is better than a guess, so it declines rather than inventing one.
    if len(targets) < len(ACTIONS):
        bounds, _ = find_canvas(xml, dump=False)
        if bounds is not None:
            x1, _y1, _x2, y2 = bounds
            if x1 >= _MIN_STRIP:
                x = max(x1 // 2, 4)
                targets.setdefault("clear",
                                   {"point": [x, y2 - _BUTTON_CLEAR_LIFT]})
                targets.setdefault("confirm",
                                   {"point": [x, y2 - _BUTTON_CONFIRM_LIFT]})
    return targets or None


def request_action(kind: str, path: Path | None = None) -> str:
    """Ask for one press of the app's own clear or save. Returns the id."""
    if kind not in ACTIONS:
        raise ValueError("that is not a signature action")
    target = path or ACTION_PATH
    rid = uuid.uuid4().hex[:12]
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"id": rid, "kind": kind, "at": time.time()}),
                   encoding="utf-8")
    os.replace(tmp, target)
    log.info("signature %s requested (%s)", kind, rid)
    return rid


def take_action(path: Path | None = None) -> dict | None:
    """Claim a pending action; deleted before anything is tried."""
    target = path or ACTION_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        target.unlink()
    except OSError:
        pass
    if time.time() - float(payload.get("at", 0)) > ACTION_MAX_AGE:
        log.info("ignoring a stale signature action")
        return None
    if payload.get("kind") not in ACTIONS:
        return None
    return payload


def do_action(payload: dict, status_path: Path | None = None) -> Status:
    """Press the app's own clear or save, on her explicit ask.

    The replay itself still never commits — this is her tap on a screen
    she is looking at, relayed. Same finder, same refusals: off the
    signature moment there is nothing to press.
    """
    from apt_log import resident

    kind = payload["kind"]
    status = Status(id=payload.get("id", ""), state="running", kind=kind)
    write_status(status, status_path)

    def work(driver) -> None:
        package = driver.current_package
        if package not in APP_PACKAGES:
            status.state, status.reason = "failed", "wrong_app"
            return
        source = driver.page_source or ""
        targets = button_targets(source, package)
        if not targets or kind not in targets:
            status.state, status.reason = "failed", "no_canvas"
            # RECORD IT. This is the one artifact built to hand over the fix,
            # and it was written with dumping switched off on every path that
            # refuses — so the night this failed for real it held a
            # two-node file from the day before, and the repair had to be
            # reverse-engineered from screenshots. A refusal here is rare (it
            # takes a person pressing the button) and always worth keeping.
            _dump_refusal(_nodes_of(source), "no_canvas")
            return
        # Kept on the way IN, whether this succeeds or not: the record is only
        # useful if it describes the screen the press was aimed at, and after a
        # successful press that screen is gone.
        _dump_refusal(_nodes_of(source), f"pressing:{kind}")
        target = targets[kind]
        if "element" in target:
            if not _click_element(driver, target["element"]):
                status.state, status.reason = "failed", "not_pressable"
                return
        else:
            _perform(driver, [[tuple(target["point"])]])
        status.state = "done"

    try:
        resident.run(work)
    except Exception as exc:  # noqa: BLE001
        log.warning("signature %s failed: %s", kind, exc)
        status.state, status.reason = "failed", "replay_failed"

    status.at = datetime.now().isoformat()
    write_status(status, status_path)
    log.info("signature %s %s (%s)", kind, status.state, status.reason or "ok")
    return status


def digest(strokes) -> str:
    return hashlib.sha256(
        json.dumps(strokes, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


# -------------------------------------------------------------------- replay
# A touch pointer that is up cannot hover, so the move that positions the pen
# before pointer_down is not guaranteed to land before the down does — the
# first live replay drew a connector line from the previous stroke's end into
# the next stroke's start. The pauses pin the ordering: position, settle,
# touch, settle, draw. The gap between strokes keeps the canvas from reading
# two quick gestures as one.
# How long the app gets to recover from the hierarchy dump before ink goes
# in. Set against a measured 3-second stalled frame rather than guessed; a
# second and a half is most of the way there and is invisible next to the
# round trip she is already waiting through.
INK_SETTLE = 1.5

PEN_SETTLE = 0.10
STROKE_GAP = 0.45
# Per-move duration. The W3C default is 250ms per point, which turns a real
# signature (hundreds of points) into minutes of drawing; a dozen ms keeps a
# stroke fluid without outrunning injection.
MOVE_MS = 12


# The locator strategy's wire name. Spelled out rather than imported from
# appium, which is not installed in the cloud container where this suite also
# runs — and importing it here would skip these tests exactly where the logic
# is cheapest to check.
_XPATH = "xpath"


def _click_element(driver, el: dict) -> bool:
    """Press the app's own button, matched on BOTH its id and its caption.

    Both are required at press time for the same reason the caption is
    required at match time: this activity publishes one `button_save` on the
    signature page reading "Salvar" and another on the ordinary visit page
    with no caption at all, and the second one is the ✎ on a real record.
    Re-checking here closes the gap between reading the tree and acting on it,
    where the screen may have changed underneath.
    """
    rid, txt = el.get("rid") or "", el.get("txt") or ""
    if not rid or not txt:
        return False
    quoted = txt.replace("'", "&apos;")
    xpath = f"//*[@resource-id='{rid}'][@text='{quoted}']"
    try:
        matches = driver.find_elements(_XPATH, xpath)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not look for %s (%s)", rid, exc)
        return False
    if len(matches) != 1:
        # Zero means the screen moved between the read and the press; more
        # than one means the same id and caption twice, and neither is a
        # thing to guess at with a signature on the screen.
        log.info("signature button %s matched %d elements — refusing",
                 rid, len(matches))
        return False
    matches[0].click()
    return True


# How many times a stroke that did not land is drawn again, and how much of
# the canvas has to change for it to count as landed.
#
# Three attempts at making the seam between strokes reliable did not hold —
# releasing the pointer source, widening the gap, letting the app recover from
# the hierarchy dump — and the field report after each was the same shape: the
# FIRST stroke lands and a later one does not. At that point the honest move
# is to stop trusting the injection and check.
#
# So each stroke is looked at. A screenshot of the canvas before and after
# says whether ink appeared; if it did not, that one stroke is drawn again.
# Only the missing stroke is redrawn, never the whole signature, so a retry
# cannot double an existing stroke — and the canvas is never cleared, so a
# stroke that DID land is never at risk.
STROKE_RETRIES = 2
# A stroke is dozens of dark pixels at minimum. Twenty is comfortably below
# the smallest real mark and comfortably above the noise of a repaint.
INK_LANDED = 20


def _canvas_ink(bounds, serial=None) -> int | None:
    """Dark pixels inside the canvas, or None if the screen cannot be read.

    None is not zero: a screenshot that fails must never be read as "the
    stroke did not land", or a working replay would redraw everything.
    """
    from apt_log import feed as feed_mod

    try:
        shot = feed_mod._adb(["exec-out", "screencap", "-p"], serial,
                             timeout=20.0)
        if shot.returncode != 0 or not shot.stdout:
            return None
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(shot.stdout)) as im:
            grey = im.convert("L").crop(tuple(bounds))
            return sum(1 for p in grey.getdata() if p < 100)
    except Exception as exc:  # noqa: BLE001
        log.info("cannot measure the canvas (%s)", exc)
        return None


def _draw(driver, path) -> None:
    """One stroke, one action chain."""
    from selenium.webdriver.common.actions import interaction
    from selenium.webdriver.common.actions.action_builder import ActionBuilder
    from selenium.webdriver.common.actions.pointer_input import PointerInput

    actions = ActionBuilder(driver,
                            mouse=PointerInput(interaction.POINTER_TOUCH,
                                               "touch"),
                            duration=MOVE_MS)
    pen = actions.pointer_action
    pen.move_to_location(*path[0])
    pen.pause(PEN_SETTLE)
    pen.pointer_down()
    pen.pause(PEN_SETTLE)
    for x, y in path[1:]:
        pen.move_to_location(x, y)
    pen.pause(PEN_SETTLE)
    pen.pointer_up()
    actions.perform()
    # Release the pointer source. Without this the next chain inherits a
    # half-alive input of the same id, which is the state a dropped pen-down
    # comes out of.
    try:
        actions.clear_actions()
    except Exception:  # noqa: BLE001
        pass


def _perform(driver, paths, bounds=None, serial=None) -> None:
    """Draw every stroke, and CHECK EACH ONE ARRIVED.

    One chain for the whole signature was tried and the driver refuses it:
    UiAutomator2 answers a chain holding more than one touch cycle with
    "Unable to perform W3C actions ... make sure your input actions chain is
    valid", every time. So a stroke is a chain, and the seam between chains
    is unreliable in a way three separate fixes did not settle.

    `perform()` returns when the driver has ACCEPTED the chain, not when the
    phone has finished injecting it, and there is nothing in the response
    that says the ink was drawn. The only thing that does is the screen. So
    the canvas is measured before and after each stroke, and a stroke that
    left no mark is drawn again.

    Only the missing stroke is redrawn — never the whole signature, and the
    canvas is never cleared — so a retry cannot double a stroke that landed
    or endanger one that did. Without bounds to measure, this degrades to
    exactly what it did before: draw each stroke once and hope.
    """
    # Measured ONCE per stroke, not twice: the reading after one stroke is
    # the reading before the next. A screenshot is a synchronous
    # SurfaceFlinger round trip and this replay already has a good reason to
    # believe that leaning on the app mid-signature is not free.
    before = _canvas_ink(bounds, serial) if bounds else None
    for i, path in enumerate(paths):
        if i:
            time.sleep(STROKE_GAP)
        _draw(driver, path)
        if before is None:
            continue
        after = before
        for _ in range(STROKE_RETRIES):
            after = _canvas_ink(bounds, serial)
            if after is None or after - before >= INK_LANDED:
                break
            log.info("stroke %d of %d left no ink; drawing it again",
                     i + 1, len(paths))
            time.sleep(STROKE_GAP)
            _draw(driver, path)
        before = after if after is not None else before


def execute(payload: dict, status_path: Path | None = None) -> Status:
    """Find the canvas, replay the strokes, publish the outcome."""
    from apt_log import resident

    strokes = payload["strokes"]
    status = Status(id=payload.get("id", ""), state="running",
                    digest=digest(strokes)[:16])
    write_status(status, status_path)
    # WHERE each stroke ended up, once mapped onto the canvas. Extents only —
    # a bounding box cannot reconstruct a signature, so nothing re-stampable
    # is written (REQ-10.6) — and it answers the question the point counts
    # cannot: a stroke that lands on top of another, or collapsed onto an
    # edge by a bad fit, is present in every count and invisible on the
    # phone. Reported as "only one of the initials made it", which is what
    # two initials drawn in the same place would look like.
    placed = [""]

    def work(driver) -> None:
        package = driver.current_package
        if package not in APP_PACKAGES:
            status.state, status.reason = "failed", "wrong_app"
            return
        xml = driver.page_source
        bounds, refusal = find_canvas(xml)
        if bounds is None:
            status.state, status.reason = "failed", refusal
            return
        # LET THE APP GET UP OFF THE FLOOR FIRST.
        #
        # Reading the hierarchy is not free on a Compose surface, and this
        # phone's own log says how expensive: during a replay the app's
        # frame time was
        #
        #   BufferQueueProducer: fps=1.27 dur=3149.73 max=3083.11
        #
        # — one frame over three seconds, with an accessibility dump either
        # side of it. Ink injected into a UI thread in that state arrives and
        # is not all drawn, which is a signature landing a stroke short.
        #
        # The report that found this was hers, and it named the variable
        # exactly: "if I press send too fast the same happens, but if I wait
        # about 5-10 seconds the whole signature makes it". Waiting let the
        # dumps subside. The replay does its own dump right here — for the
        # canvas bounds it cannot draw without — so it now waits too.
        time.sleep(INK_SETTLE)
        paths = build_paths(strokes, bounds, payload.get("aspect", 1.0),
                            rotate=sideways(xml, package))
        placed[0] = " canvas=%s boxes=%s" % (
            bounds,
            ";".join("%d,%d-%d,%d" % (min(x for x, _ in pth),
                                      min(y for _, y in pth),
                                      max(x for x, _ in pth),
                                      max(y for _, y in pth))
                     for pth in paths if pth))
        _perform(driver, paths, bounds=bounds)
        status.state = "done"

    try:
        resident.run(work)
    except Exception as exc:  # noqa: BLE001
        log.warning("signature replay failed: %s", exc)
        status.state, status.reason = "failed", "replay_failed"

    status.at = datetime.now().isoformat()
    write_status(status, status_path)
    # The stroke shape goes in the log, textless — counts only, no
    # coordinates. A signature reported as arriving with a stroke missing
    # could not be reproduced from generated strokes, and this is the datum
    # that would settle it: what she drew versus what the replay was handed.
    shape = "+".join(
        str(len(st.get("points") if isinstance(st, dict) else st))
        for st in (strokes or []))
    log.info("signature replay %s (%s) strokes=%d points=%s aspect=%.2f%s",
             status.state, status.reason or status.digest[:8],
             len(strokes or []), shape or "-",
             float(payload.get("aspect") or 0), placed[0])
    return status
