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
import unicodedata
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

# A SHARE OF THE SCREEN IS THE WRONG YARDSTICK INSIDE A DIALOG.
#
# Mobile Caregiver+ puts its signature pad in an AlertDialog — parentPanel,
# customPanel, custom — so the tree's whole extent is the DIALOG, 1080x1419,
# not the phone. Its canvas is `drawing_view_participant`, 994x198, which is
# 12.8% of that and so failed the 22% test. Nothing else qualified either,
# and a live check-out came back `no_canvas` with the caregiver standing at
# the pad.
#
# Measured on that dialog, the things the id hints also match:
#
#     drawing_view_participant       994x198   <- the canvas
#     button_participant_signature  1020x 56
#     buttonClearSignature           314x 50
#
# Width cannot separate them: the header button is WIDER than the canvas.
# Height does, by nearly four to one, and it is the honest measure of "big
# enough to sign on" — a signature strip is wide and short, a button is wide
# and thin. So a hinted node also counts when it is broad AND deep enough in
# its own right, whatever share of its container that happens to be.
#
# Checked against HHAeXchange+'s full-screen pad too, which this must not
# break: 1058x1246 on 1080x2340 passes both tests comfortably.
CANVAS_MIN_WIDTH_SHARE = 0.40
CANVAS_MIN_HEIGHT_SHARE = 0.08

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


# How long after a replay finishes the phone is still the replay's. The ink
# is painted asynchronously — see INK_PAINT — and a swipe landing in that
# window draws across a signature that has only just arrived.
IN_FLIGHT_TAIL = 3.0


def in_flight(status_path: Path | None = None,
              request_path: Path | None = None) -> bool:
    """Whether a signature replay is queued, running, or just finished.

    ON A SIGNATURE CANVAS A SWIPE IS INK, and nothing else that touches this
    phone could tell a replay was happening. The walker asks whether a MACRO
    is running, and a replay is not a macro — it writes its own status file —
    so a page walk could begin mid-signature and scroll straight down the
    canvas. The walk only runs when somebody is watching, which is exactly
    why every replay the owner watched lost a stroke and every one requested
    from a script, unwatched, landed whole.

    The tail matters as much as the running state: `perform()` returns before
    the phone has finished painting, so "not running any more" is not yet
    "safe to touch".
    """
    if (request_path or REQUEST_PATH).exists():
        return True
    status = read_status(status_path)
    if status.state == "running":
        return True
    # THE TAIL ONLY MEANS ANYTHING FOR A REPLAY THAT ACTUALLY HAPPENED.
    #
    # `read_status` answers a DEFAULT Status when no file exists, and its
    # `at` defaults to now — so a machine that has never replayed a signature
    # looked like one that finished a moment ago, and the walker was held off
    # for ever. Caught by the existing walk tests going red the instant this
    # was wired in, which is what they are for.
    if status.state not in ("done", "failed") or not status.at:
        return False
    try:
        ended = datetime.fromisoformat(status.at)
    except ValueError:
        return False
    if ended.tzinfo is not None:
        ended = ended.astimezone().replace(tzinfo=None)
    return (datetime.now() - ended).total_seconds() < IN_FLIGHT_TAIL


# -------------------------------------------------------------------- finder
def find_canvas(xml: str, dump: bool = True) -> tuple[list[int] | None, str]:
    """The signature canvas on this screen, or why there is none.

    Returns (bounds, "") or (None, reason). Exactly one candidate is the only
    acceptable answer: zero means this is not a signature screen, and two means
    the shape rule matched something it should not have — both are refusals,
    because a wrong rectangle puts ink on the wrong control.
    """
    named: list[list[int]] = []

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

    # A NAME ABOUT SIGNATURES IS NOT THE SAME AS A SURFACE TO DRAW ON.
    #
    # HHAeXchange+'s patient signature screen names its two BUTTONS
    # `signature_screen_clear_button` and `signature_screen_submit_button` —
    # 0.07% and 0.13% of the screen — and leaves the canvas, half the screen
    # wide, with no id at all. Both buttons matched the id hint, neither
    # wrapped the other, and the finder refused "ambiguous" without ever
    # looking at the thing that draws. Caught on the live pad with the
    # caregiver standing on step 2 of 3 of a check-out.
    #
    # So a hinted node has to be big enough to sign on before it is a
    # candidate. The small ones are not thrown away: they are the app SAYING
    # this is a signature screen, which is worth more than the id.
    hinted: list[list[int]] = []
    surfaces: list[tuple[list[int], bool]] = []
    for raw, b in nodes:
        rid = _attr(raw, "resource-id").split("/")[-1].lower()
        cls = (_attr(raw, "class") or "").rsplit(".", 1)[-1]
        big = (b[2] - b[0]) * (b[3] - b[1]) >= screen_area * CANVAS_MIN_SHARE
        # Or big enough to sign on in its OWN right — see the constants.
        # A pad inside a dialog is a small share of a small tree, and the
        # share test alone threw the real canvas away.
        signable = ((b[2] - b[0]) >= max_x * CANVAS_MIN_WIDTH_SHARE
                    and (b[3] - b[1]) >= max_y * CANVAS_MIN_HEIGHT_SHARE)
        if (any(hint in rid for hint in CANVAS_ID_HINTS)
                or any(hint in cls.lower() for hint in CANVAS_CLASS_HINTS)):
            (named if (big or signable) else hinted).append(b)
            continue
        if cls in CANVAS_CLASSES and not _attr(raw, "text") and big:
            surfaces.append((b, _attr(raw, "clickable") == "true"))

    # A drawing surface is usually inert in the tree, and that quietness is
    # what tells it from a big clickable container. HHAeXchange+'s is not:
    # its canvas reports clickable, so requiring quiet left the screen with
    # no candidate at all. A clickable surface counts only where the app has
    # already said, somewhere on this screen, that signing is what happens
    # here — which is what the small hinted controls are evidence of.
    quiet = [b for b, clickable in surfaces if not clickable]
    shaped = quiet or ([b for b, _ in surfaces] if (hinted or named) else [])

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
    # ENVIAR WAS MISSING, AND IT IS THE ONE THAT MATTERS.
    #
    # This dump is the only artifact built to hand over a signature repair,
    # and the word HHAeXchange+ writes on its submit button was not on the
    # list — so the live capture of that screen came back with "Borrar"
    # beside the clear button and an EMPTY string beside the submit one,
    # which reads as "the app does not name that button" when in fact the
    # app names it perfectly well. An hour of this repair went into the
    # wrong half of the screen because of it. `_SAVE_WORDS` has held
    # "enviar" all along; these two lists were simply never reconciled.
    "enviar", "send",
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
#
# The two box tests are SHARES of the window's width, not pixels. They were
# measured at 40 and 60 on a 720-wide screen, which is what these fractions
# reproduce there — but a denser panel draws the same label bigger in every
# dimension, and a fixed 40px ceiling on "narrow" would stop matching the
# very labels it was written for.
_ROT_LABEL_MIN_CHARS = 6
_ROT_LABEL_MAX_W_SHARE = 40 / 720
_ROT_LABEL_MIN_H_SHARE = 60 / 720


def presentation_rotated(xml: str) -> bool:
    """Whether this screen draws its content turned 90° inside a portrait
    activity — the shape strokes must be turned to match."""
    boxes = []
    max_x = max_y = 0
    for raw in _NODE.findall(xml or ""):
        m = _BOUNDS.search(_attr(raw, "bounds"))
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        max_x, max_y = max(max_x, x2), max(max_y, y2)
        boxes.append((x2 - x1, y2 - y1, len(_attr(raw, "text"))))

    if not max_x or max_x >= max_y:
        # A truly landscape screen needs no help: injected coordinates
        # already follow the rotated display.
        return False

    # Measured against the window this tree actually describes, so the same
    # labels match at any resolution.
    narrow = max_x * _ROT_LABEL_MAX_W_SHARE
    tall = max_x * _ROT_LABEL_MIN_H_SHARE
    tall_labels = sum(
        1 for w, h, chars in boxes
        if chars >= _ROT_LABEL_MIN_CHARS and w <= narrow and h >= max(tall, 3 * w))
    return tall_labels >= 2


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


def signing_moment(xml: str) -> bool:
    """Whether a pad is LIVE on this screen, rather than remembered by it.

    Split out of `sideways`, which was answering two questions at once and
    getting the second one wrong on a screen that did not exist when it was
    written. "Is the ink turned a quarter turn" and "is there a pad here to
    press buttons on" are different questions, and HHAeXchange+'s check-out
    pages are the case that separates them: a real signature pad, drawn
    genuinely landscape (1600x720), needing no turn at all. Asking `sideways`
    whether to offer the app's own Borrar and Enviar answered no, and the
    caregiver got step two of the portal with nothing in it.

    A moment is: something that actually draws (or the pad's own captioned
    save), exactly one canvas by the finder's reckoning, and that canvas
    DOMINATING the screen. The last part is what keeps a completed visit
    page — which keeps the saved signatures' wrappers in its tree — from
    reading as a live pad.
    """
    # A DRAWING SURFACE, or the pad's own captioned save. Either is evidence;
    # the tab WRAPPER alone is not.
    if not (_has_drawing_surface(xml) or _app_buttons(xml).get("confirm")):
        return False
    max_x, max_y = _extent(xml)
    if not max_x:
        return False
    bounds, _ = find_canvas(xml, dump=False)
    if bounds is None:
        return False
    return ((bounds[2] - bounds[0]) >= CANVAS_DOMINANT_W * max_x
            and (bounds[3] - bounds[1]) >= CANVAS_DOMINANT_H * max_y)


def sideways(xml: str, package: str = "") -> bool:
    """Whether ink replayed onto this screen must turn a quarter turn.

    True only for a signature MOMENT drawn rotated inside a PORTRAIT activity.
    A genuinely landscape pad needs no turn — injected coordinates already
    follow the rotated display — which is why the moment test now lives in
    `signing_moment` and this function is only about the turn.
    """
    if presentation_rotated(xml):
        return True
    if package not in ROTATED_CANVAS_APPS:
        return False
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
    if not signing_moment(xml):
        return False
    # PORTRAIT is what is left to check. A truly landscape page needs no turn,
    # and that is the whole distinction this function now carries.
    max_x, max_y = _extent(xml)
    return bool(max_x) and max_x < max_y


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
_SAVE_IDS = ("button_save", "btn_save", "save_button",
             # HHAeXchange+ calls it submit, and its caption is "Enviar" —
             # the word for sending the signature on, not for saving it.
             "submit_button", "button_submit",
             # AND MOBILE CAREGIVER+ WRITES ITS IDS IN camelCase. Every
             # pattern above has an underscore in it, so `buttonComplete`
             # and `buttonClearSignature` matched nothing at all and this
             # app's pad reached the drawer with no controls on it —
             # leaving the caregiver to switch to the phone view to finish
             # a signature, which is the switching the drawer exists to
             # remove. Read off the live dialog on 1 Sep.
             "buttoncomplete")
_CLEAR_IDS = ("button_clear", "btn_clear", "clear_button", "button_erase",
              "buttonclearsignature")
_SAVE_WORDS = ("salvar", "save", "guardar", "enviar", "submit", "send")
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
    # THE CAPTION IS OFTEN NOT ON THE BUTTON, and on this app it never is.
    #
    # inMyTeam's signature sheet publishes its Borrar as a clickable View with
    # no text and no resource-id, and the word sits in a TextView beside it:
    #
    #   View   clickable=true  [686,2258][788,2295]   (no text, no id)
    #   TextView  "Borrar"     [719,2265][770,2288]
    #
    # So the caption rule below could not see it, `_wipe_the_canvas` pressed
    # nothing, and a second replay landed on top of the first. Watched live:
    # the log line for the failed signature carries no `cleared=` at all.
    #
    # The tree here is a flat regex scan with no parent links, so containment
    # is done by geometry: a caption belongs to the SMALLEST clickable node
    # whose bounds enclose it. Smallest is what stops the screen's own root —
    # which encloses every word on it — claiming to be the clear button. It is
    # the same rule `_words` uses on the other side of this project, for the
    # same reason.
    captions: list[tuple[list[int], str]] = []
    for raw in _NODE.findall(xml or ""):
        txt = _attr(raw, "text").strip()
        if not txt:
            continue
        m = _BOUNDS.search(_attr(raw, "bounds"))
        if m:
            captions.append(([int(g) for g in m.groups()], txt.lower()))

    def _encloses(box, inner) -> bool:
        return (box[0] <= inner[0] and box[1] <= inner[1]
                and box[2] >= inner[2] and box[3] >= inner[3])

    found: dict[str, dict] = {}
    areas: dict[str, int] = {}
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
            # A CAPTION, OR AN ID THAT NAMES THE SIGNATURE SCREEN ITSELF.
            #
            # HHAeXchange+'s buttons carry no text — "Borrar" and "Enviar"
            # are separate TextViews laid over them — so a rule that needed
            # the caption could not see either one, on the very screen the
            # pad exists for.
            #
            # But the caption was earning its keep, and the tests said so
            # before this shipped: the legacy app's ORDINARY VISIT PAGE
            # carries `button_save` with no text, and that is a different
            # save entirely. Its pad's Salvar appears and disappears WITH the
            # pad, which is what makes the caption the evidence there.
            #
            # So an id stands alone only when it names the signature screen
            # as well as the action. `signature_screen_submit_button` does;
            # a bare `button_save` does not, and still needs its word.
            names_the_screen = any(h in rid for h in CANVAS_ID_HINTS)
            # The node's own caption, or one drawn over it. `held` carries the
            # word actually matched so the log and the pad say what the app
            # says, rather than an empty string for a textless button.
            held = low if (low and low in words) else ""
            if not held:
                for cbox, ctxt in captions:
                    if ctxt in words and _encloses(b, cbox):
                        held = ctxt
                        break
            if ((held)
                    or (any(i in rid for i in ids)
                        and (low or names_the_screen))):
                # SMALLEST WINS. Every ancestor of a caption encloses it, and
                # the page root encloses all of them; taking the first in tree
                # order would press a container the size of the screen.
                area = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
                if kind in found and area >= areas.get(kind, 0):
                    continue
                found[kind] = {"rid": full, "txt": txt or held,
                               "b": b,
                               "xy": [(b[0] + b[2]) // 2, (b[1] + b[3]) // 2]}
                areas[kind] = area
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
    # A SIGNATURE SCREEN, WHICH IS NOT THE SAME AS A SIDEWAYS ONE.
    #
    # This gate used to be `sideways()`, and that was right while the only
    # signature page here was the legacy app's, drawn a quarter turn inside a
    # portrait activity. HHAeXchange+'s check-out pages are not that: they are
    # GENUINELY landscape — action_bar_root [0,0][1600,720], seen on the real
    # check-out — so `sideways` answers False, correctly, and the whole row of
    # app-side buttons vanished on the one screen it was built for. The
    # caregiver was left pressing Enviar on the phone by hand while the portal
    # showed her step two and nothing in it.
    #
    # The canvas is the right gate. `find_canvas` accepting this screen is what
    # makes it a signature moment; the turn is a separate question about how the
    # ink is mapped, and only the pixel fallback below actually depends on it.
    #
    # It has to be SOME gate, not none: `_app_buttons` matches a caption in
    # _SAVE_WORDS, and the plan-of-care duties screen carries a `Guardar`
    # ("poc_task_save_button") that would otherwise be offered as a signature
    # submit on a screen with no signature on it.
    if not signing_moment(xml):
        return None
    bounds, _refusal = find_canvas(xml, dump=False)
    if bounds is None:
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
    #
    # STILL GATED ON THE TURN, and now that is the only thing that is. These
    # offsets were measured on the legacy app's rotated page and mean nothing
    # anywhere else — a derived coordinate on a screen whose buttons the tree
    # publishes perfectly well would be a guess replacing a fact.
    if sideways(xml, package) and len(targets) < len(ACTIONS):
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

# ---------------------------------------------------- claiming the gesture
# THE BOTTOM SHEET WAS EATING THE STROKES, and this is why a signature came
# out with letters missing.
#
# inMyTeam draws its pad inside a `design_bottom_sheet` — a
# BottomSheetDialog — and that container watches every touch for a DOWNWARD
# DRAG, because a downward drag is how a person dismisses it. A touch whose
# first movement is vertical is claimed by the sheet before the canvas ever
# sees it. A signature is mostly vertical movement, so most of its strokes
# were being taken.
#
# Measured on the live pad, on the caregiver's own phone, with the ink counted
# off a screenshot after each stroke:
#
#   pure downward diagonal, no prime .......... 0 ink,  LOST
#   the same stroke, primed horizontally ... 1318 ink,  drawn
#   sweep of nine horizontal strokes ....... all nine drawn
#   16 primed strokes, steep and shallow ... 16 of 16 drawn
#   the same, unprimed ...................... 0 of 16 drawn
#
# So each stroke now moves a few pixels SIDEWAYS first and comes back. That is
# enough to make the canvas the owner of the gesture — a horizontal drag is
# not the sheet's gesture, so it does not intercept — and everything after it
# goes to the pad.
#
# THE PRICE IS A SHORT HORIZONTAL LEAD-IN at the start of each stroke, and it
# is now four pixels rather than sixteen, because a second round of trials
# found the MECHANISM rather than a threshold. The sheet decides on the
# DIRECTION of the first movement, not on how far it travels:
#
#     prime  0px .... 0 of 5 landed
#     prime  2px .... 5 of 5
#     prime  4px .... 5 of 5      8px .... 5 of 5      16px .... 5 of 5
#
# Nothing between 2 and 16 behaves differently, so sixteen was buying no
# margin at all — only a visible tick at the start of every stroke, which is
# what the caregiver's eye went to. Four is twice the smallest that works and
# a quarter of what it was: 0.4% of the pad's width, narrower than the ink.
#
# Re-checked at 4px across four stroke shapes on the live pad — straight down,
# up and to the left, down and right, and a tight loop — 20 of 20.
#
# It also explains the report this project could not reproduce for weeks: "one
# continuous swipe gets the full signature, lifting the finger breaks it".
# Every lift starts a new gesture, and every new gesture is another chance for
# the sheet to take it.
CLAIM_NUDGE = 4

# A DOT IS NOT A DRAG, AND MUST NOT BE GIVEN ONE.
#
# The lead-in above is bought against gesture interception, and interception
# only happens to a DRAG. A single-point stroke — the tittle of an i, the dot
# of a j, a full stop — has no drag to steal, so the nudge buys nothing there
# and costs everything: it turns a dot into a 16px horizontal dash.
#
# Seen on the caregiver's own signature and reported as "two straight lines
# that look like they were done programmatically", which is exactly what they
# were. The log had already said so and nobody had read it that way:
#
#   points=34+27+72+101+1+1
#   boxes=…;456,1735-456,1735;448,1727-448,1727
#   strokes_ink=…,+83/1,+80/1
#
# Two strokes of ONE point each, at zero-size boxes, each leaving a line's
# worth of ink.
#
# A dot still needs SOME movement, because a pad that draws on move events
# renders a bare down-up as nothing at all — and a lost tittle is a wrong
# signature too. Three pixels is under any touch slop, so it cannot read as a
# drag, and it marks about as much as a fingertip would.
DOT_MARK = 3


# The locator strategy's wire name. Spelled out rather than imported from
# appium, which is not installed in the cloud container where this suite also
# runs — and importing it here would skip these tests exactly where the logic
# is cheapest to check.
_XPATH = "xpath"


def _click_element(driver, el: dict) -> bool:
    """Press the app's own button, re-matched on the SAME evidence that found it.

    The caption is required at press time for the same reason it is required
    at match time: this activity publishes one `button_save` on the signature
    page reading "Salvar" and another on the ordinary visit page with no
    caption at all, and the second one is the ✎ on a real record.
    Re-checking here closes the gap between reading the tree and acting on it,
    where the screen may have changed underneath.

    Where the ID names the signature screen, the id is the evidence and the
    id is what gets re-checked — see the block below. Either way the press
    happens only against exactly one match.
    """
    rid, txt = el.get("rid") or "", el.get("txt") or ""
    if not rid:
        return False
    if not txt:
        # A CAPTIONLESS BUTTON WHOSE ID NAMES THE SIGNATURE SCREEN.
        #
        # `_app_buttons` decided, deliberately and with the reasoning above
        # it, that `signature_screen_submit_button` needs no caption: the id
        # names the screen as well as the action, which is exactly the
        # evidence the bare `button_save` on the visit page lacks. This
        # function then re-checked with the OPPOSITE rule and refused
        # everything it was handed, so on HHAeXchange+ — where both Borrar
        # and Enviar are captionless Views with the words drawn over them as
        # separate TextViews — the app-side press could never succeed.
        #
        # The two rules have to agree. A caption is still required where the
        # caption was the evidence; where the id was, the id is what gets
        # re-checked. The single-match guard below is unchanged and still
        # does the real work of refusing an ambiguous screen.
        if not any(hint in rid.split("/")[-1].lower()
                   for hint in CANVAS_ID_HINTS):
            return False
        xpath = f"//*[@resource-id='{rid}']"
    else:
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

# How long the app gets to PAINT a stroke before the canvas is measured. The
# whole retry mechanism depends on this: measure too soon and a stroke that
# landed perfectly reads as missing, and gets drawn again. Set against the
# same stalled-frame evidence as INK_SETTLE — this app's own log shows frames
# taking seconds under an accessibility dump — and deliberately generous,
# because the cost of waiting is a third of a second per stroke and the cost
# of not waiting is a signature that looks like it lost a letter.
INK_PAINT = 0.4
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
    x0, y0 = path[0]
    pen.move_to_location(x0, y0)
    pen.pause(PEN_SETTLE)
    pen.pointer_down()
    pen.pause(PEN_SETTLE)
    # SIDEWAYS FIRST — see CLAIM_NUDGE. Without this the bottom sheet holding
    # the pad claims the touch as a drag-to-dismiss and the stroke is never
    # drawn at all.
    #
    # Toward whichever way the stroke is already travelling, so the lead-in
    # lies along the mark rather than across it; rightward for a stroke with
    # no horizontal travel at all. `build_paths` keeps every point inside the
    # canvas by a margin wider than this, so neither direction leaves the pad.
    #
    # NOT FOR A DOT — see DOT_MARK. There is no drag to be stolen, and the
    # nudge is what turned two tittles into two straight lines.
    way = 1 if path[-1][0] >= x0 else -1
    # One step out and one back. It used to walk out in five-pixel steps
    # because it had sixteen pixels to cover; at four there is nothing to
    # walk, and the loop was emitting its last point twice.
    lead = CLAIM_NUDGE if len(path) > 1 else DOT_MARK
    pen.move_to_location(x0 + way * lead, y0)
    # Back to where the stroke actually starts, so the signature itself is
    # drawn from its own first point.
    pen.move_to_location(x0, y0)
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
    first = _canvas_ink(bounds, serial) if bounds else None
    before = first
    # WHAT EACH STROKE ACTUALLY ADDED, AND HOW MANY GOES IT TOOK.
    #
    # Four experiments failed to reproduce the one failure that matters. A
    # steep vertical stroke inks; a stroke that reverses direction inks; the
    # exact shape, position, order and point count of the signature that lost
    # its arch all ink when replayed synthetically; so do points clumped the
    # way a slowing finger clumps them. Every synthetic lands. Only the real
    # signatures lose a stroke, and they have done it twice.
    #
    # So the trigger is in something a hand-drawn stroke carries that a
    # generated one does not, and guessing at it has now cost two wrong
    # fixes. This is the instrument instead: per stroke, what the canvas
    # gained and how many attempts it took. The next real signature answers
    # it from the record rather than from another theory.
    #
    # Deltas and counts only — a pixel total cannot reconstruct a signature,
    # which is the same line REQ-10.6 draws around everything else here.
    tally: list[str] = []
    for i, path in enumerate(paths):
        if i:
            time.sleep(STROKE_GAP)
        _draw(driver, path)
        if before is None:
            continue
        after = before
        tries = 1
        for _ in range(STROKE_RETRIES):
            # LOOK ONLY AFTER THE APP HAS HAD TIME TO PAINT.
            #
            # There was no pause here at all, and `perform()` returns when
            # the DRIVER accepted the chain — not when the phone finished
            # injecting it, which is the same fact `execute` already leans on
            # when it waits before dumping the hierarchy. So the canvas was
            # being measured while the stroke was still on its way, read as
            # "left no ink", and drawn again.
            #
            # Watched on inMyTeam: "stroke 2 of 3 left no ink; drawing it
            # again" twice in one replay, on the arch of an A whose crossbar
            # landed either side of it. The owner's read was that the phone
            # had the whole signature and the machine was looking too soon,
            # and the code agrees with him — there was nothing here to make
            # it wait.
            time.sleep(INK_PAINT)
            after = _canvas_ink(bounds, serial)
            if after is None or after - before >= INK_LANDED:
                break
            log.info("stroke %d of %d left no ink; drawing it again",
                     i + 1, len(paths))
            time.sleep(STROKE_GAP)
            _draw(driver, path)
            tries += 1
        if after is not None:
            tally.append("%+d/%d" % (after - before, tries))
        before = after if after is not None else before
    # And once more at the end, so "done" is not announced over a canvas the
    # last stroke has not reached yet. The peek she is looking at is the
    # frame the feed publishes next; declaring success before the ink is
    # painted is what makes a finished signature look like it lost a stroke.
    if bounds:
        time.sleep(INK_PAINT)
    return (first, _canvas_ink(bounds, serial) if bounds else None,
            ",".join(tally))


# How long the app gets to repaint an emptied canvas before ink goes onto it.
# The same argument as INK_PAINT and measured against the same evidence: this
# surface has been seen taking seconds to produce a frame, and a stroke drawn
# into a canvas that is still clearing is a stroke that gets wiped by the
# clear it arrived behind.
CLEAR_SETTLE = 0.6


def _wipe_the_canvas(driver, xml: str, placed) -> bool:
    """Press the app's own clear, if this screen has one. Never the save.

    Returns whether it pressed, and a False is not a failure: HHAeXchange+'s
    pad publishes both buttons, inMyTeam's publishes both, and a screen that
    publishes neither is a screen where there was nothing to wipe. The replay
    goes on either way — it did before this existed, and a signature drawn
    over an empty canvas is the ordinary case.

    THE SAVE IS DELIBERATELY UNREACHABLE FROM HERE. `_app_buttons` returns it
    in the same dictionary and this function must never look at that key: the
    press that commits a signature belongs to the party signing it, which is
    the whole of REQ-10.6a and the reason the replay has never touched it.
    """
    target = (_app_buttons(xml) or {}).get("clear")
    if not target:
        return False
    x, y = target["xy"]
    try:
        driver.tap([(x, y)])
    except Exception as exc:  # noqa: BLE001
        # Worth saying and not worth stopping for. The old behaviour was to
        # draw over whatever was there; that is still what happens, and the
        # log is what tells us it happened.
        log.warning("could not clear the canvas first (%s)", exc)
        placed[0] += " clear=err"
        return False
    time.sleep(CLEAR_SETTLE)
    return True


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
        # AND WIPE WHAT WAS THERE, so the drawer needs one button and not two.
        #
        # Step two used to show the app's own Borrar and Hecho side by side,
        # equally weighted, because a second replay lands ON TOP of the first
        # — the pad sends her WHOLE signature every time, so anything already
        # on the canvas has to go or the two overlap into a scribble. Redoing
        # meant Borrar, redraw, send, Hecho: four presses across two screens
        # for one signature, and it was reported as exactly that back and
        # forth.
        #
        # The clear belongs to the send, so it happens here. What this does
        # NOT do is press the affirmative — Hecho stays hers, REQ-10.6a rests
        # on it, and nothing in this file may reach for it.
        #
        # Only on her press. This runs inside a replay she asked for, which is
        # already a request to put a specific signature on that canvas; it is
        # never on a timer and never on arrival at the screen.
        wiped = _wipe_the_canvas(driver, xml, placed)
        paths = build_paths(strokes, bounds, payload.get("aspect", 1.0),
                            rotate=sideways(xml, package))
        # ASSIGNED, NOT APPENDED, and the wipe writes to the same buffer three
        # lines above — so its `cleared=` marker was being thrown away before
        # anybody read it. The one line that says whether the canvas was
        # emptied first, and it was the line that could not say it.
        placed[0] = (" cleared=%d" % int(bool(wiped))) + " canvas=%s boxes=%s" % (
            bounds,
            ";".join("%d,%d-%d,%d" % (min(x for x, _ in pth),
                                      min(y for _, y in pth),
                                      max(x for x, _ in pth),
                                      max(y for _, y in pth))
                     for pth in paths if pth))
        # WHAT THE CANVAS ACTUALLY HELD, before and after. The one number
        # that settles "did the ink land or did we look too soon" — the
        # question this replay has now raised twice, both times answerable
        # only by guessing at a screenshot afterwards. Two integers in the
        # log, and nothing reconstructable from them.
        was, now_ink, per_stroke = _perform(driver, paths, bounds=bounds)
        if was is not None and now_ink is not None:
            placed[0] += " ink=%d->%d" % (was, now_ink)
        if per_stroke:
            placed[0] += " strokes_ink=%s" % per_stroke
        # WHETHER ANYBODY WAS WATCHING. The leading unproven theory is that a
        # viewer's live peek competes with the replay for the phone — this
        # app's own log has shown frames taking three seconds under exactly
        # that load. Every synthetic replay so far ran with nobody watching
        # and every one landed; both real failures had the portal open. One
        # boolean in the log is what turns that from a coincidence into
        # evidence, either way.
        try:
            from apt_log.macros import someone_is_watching

            placed[0] += " watched=%d" % int(bool(someone_is_watching()))
        except Exception:  # noqa: BLE001 — diagnostics never break a replay
            pass
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


# ---------------------------------------------------------------- who signs
# THE APP SAYS WHOSE SIGNATURE IT IS ASKING FOR, and until now nobody read it.
#
# inMyTeam's exit is three steps, and two of them are signature pads back to
# back: `Paso 2 de 3` for the patient, `Paso 3 de 3` for the caregiver. They
# are the same screen, the same canvas and the same buttons — the ONLY thing
# distinguishing them is the title bar, which carries the signer's name.
#
# Today there is one inMyTeam patient, so "the patient" and "her" are the two
# possibilities and either could be guessed at. That stops being true the
# moment a second patient is onboarded, and the failure it produces is the
# worst one this feature has: an adopted signature applied under the wrong
# person's name, on a record an auditor reads.
#
# So the name is taken from the screen rather than inferred from the step, and
# the step counter is not used at all. A caption is a fact; "step 2 is always
# the patient" is a convention the app can change in an update.
#
# THE WORD ORDER IS THE APP'S OWN and it is broken: it renders *Firma de
# «name»* as `«NAME» Firma de`, with the name as the PREFIX. Both orders are
# accepted because the correct one is one bug-fix release away, and a build
# that fixed it would otherwise silently stop naming anybody.
# LONGEST FIRST. "firma de" is a prefix of "firma del", so a shorter marker
# tried first eats the article and hands back the rest of the word: the live
# sheet titled "Firma del Paciente" was read as a person called "l Paciente".
_SIGNS_FOR = ("firma de la", "firma del", "firma de", "signature of the",
              "signature of")

# A ROLE IS NOT A NAME, and these screens are titled with roles far more often
# than with people. "Firma del Paciente" says WHICH signature the sheet wants,
# not whose; putting it where a name goes produced a pad announcing it was
# asking for "l Paciente's signature", on a real check-out.
#
# Rejected rather than tidied, because there is nothing here to recover: a
# title that names a role names nobody, and "" is the answer that makes the
# pad say nothing instead of something wrong.
_ROLE_WORDS = frozenset((
    "paciente", "patient", "cliente", "client",
    "cuidador", "cuidadora", "caregiver", "personal", "staff",
    "empleado", "empleada", "employee", "trabajador", "trabajadora",
    "testigo", "witness", "representante", "representative",
))


# WHICH SIGNATURE THE SHEET IS ASKING FOR, when it names a role and not a
# person — which is what these screens do nearly always.
#
# `signer_named` rejects a role and returns "", correctly: a role names
# nobody. But the role is not nothing, and throwing it away cost the pad the
# one fact that keeps a signature off the wrong record. inMyTeam's check-out
# asks twice, back to back — "Firma del Paciente" then "Firma del personal" —
# and with neither answered the pad offered both parties as identical filled
# buttons. Reported from the room: "if it says firma del paciente the pencil
# drawer should know that it's not Sadia Amselem".
#
# A witness or a representative maps to neither party this project knows
# about, so they are absent here on purpose and read as "" — the same
# conservative answer the rest of this file gives.
SIGNER_ROLES = {
    "patient": ("paciente", "patient", "cliente", "client"),
    "staff": ("cuidador", "cuidadora", "caregiver", "personal", "staff",
              "empleado", "empleada", "employee",
              "trabajador", "trabajadora"),
}


def signer_role(doc: dict) -> str:
    """"patient", "staff", or "" — whose signature this sheet wants.

    "" wherever there is no signature heading, where the heading names a
    role this does not map, or where BOTH roles appear on one screen: two
    answers is the same as none, and guessing between them is exactly the
    mistake this exists to prevent.
    """
    found = set()
    for entry in (doc.get("statics") or []):
        text = (entry.get("txt") or "").strip()
        if not text or len(text) > 120:
            continue
        low = _fold(text)
        if not any(marker in low for marker in _SIGNS_FOR):
            continue
        words = set(low.split())
        for role, names in SIGNER_ROLES.items():
            if words & set(names):
                found.add(role)
    return found.pop() if len(found) == 1 else ""


def signer_named(doc: dict) -> str:
    """Whose signature this screen is asking for, or "".

    "" is a real answer and the common one: every screen that is not a
    signature pad, any pad titled with a ROLE rather than a person, and any
    pad whose title this does not recognise. The caller treats it as "say
    nothing", never as "assume the patient" — a wrong name here is worse than
    no name, because the whole point of reading it is to stop somebody's
    signature going on under another person's heading.
    """
    for entry in (doc.get("statics") or []):
        text = (entry.get("txt") or "").strip()
        if not text or len(text) > 120:
            continue
        low = _fold(text)
        # THE LONGEST MATCHING MARKER, AND THEN THE LINE IS DECIDED.
        #
        # Trying markers in turn and moving to the next one when a match came
        # back unusable is how "Firma del Paciente" was read as a person: the
        # role was correctly rejected under "firma del", and then "firma de"
        # got its own turn on the same line and produced "l Paciente".
        best = ""
        for marker in _SIGNS_FOR:
            if marker in low and len(marker) > len(best):
                best = marker
        if not best:
            continue
        at = low.find(best)
        name = (text[:at] or text[at + len(best):]).strip(" \t:·-–—,")
        # Role words shed from either end: "Firma del Paciente Ana Ruiz" is
        # asking Ana Ruiz, and the word before her name is a label.
        words = name.split()
        while words and _fold(words[0]) in _ROLE_WORDS:
            words.pop(0)
        while words and _fold(words[-1]) in _ROLE_WORDS:
            words.pop()
        name = " ".join(words)
        # A title that is ONLY the phrase, or only roles, names nobody; and a
        # remainder with no letter in it is punctuation, not a person.
        if name and any(c.isalpha() for c in name):
            return name
    return ""


def _fold(text: str) -> str:
    """Lowercase and unaccented, for comparing a word against a fixed list."""
    stripped = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())
