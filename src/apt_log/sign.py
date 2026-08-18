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
            if not (0 <= x <= 1 and 0 <= y <= 1):
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

    if len(named) > 1:
        named = [b for b in named
                 if not any(_wraps(b, other) for other in named)]

    pool = named or shaped
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
            u, v = p[0], p[1]
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


def button_targets(xml: str, package: str = "") -> dict | None:
    """Where the app's own clear and save sit, or None off the moment.

    Only on a sideways signature moment, and only when the canvas leaves
    the strip the buttons live in — everywhere else there is nothing to
    press and the answer is a refusal, not a guess.
    """
    if not sideways(xml, package):
        return None
    bounds, _ = find_canvas(xml, dump=False)
    if bounds is None:
        return None
    x1, _y1, _x2, y2 = bounds
    if x1 < _MIN_STRIP:
        return None
    x = max(x1 // 2, 4)
    return {"clear": [x, y2 - _BUTTON_CLEAR_LIFT],
            "confirm": [x, y2 - _BUTTON_CONFIRM_LIFT]}


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
        targets = button_targets(driver.page_source or "", package)
        if not targets:
            status.state, status.reason = "failed", "no_canvas"
            return
        _perform(driver, [[tuple(targets[kind])]])
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
PEN_SETTLE = 0.06
STROKE_GAP = 0.30
# Per-move duration. The W3C default is 250ms per point, which turns a real
# signature (hundreds of points) into minutes of drawing; a dozen ms keeps a
# stroke fluid without outrunning injection.
MOVE_MS = 12


def _perform(driver, paths) -> None:
    """Drive the strokes through W3C pointer actions. Thin on purpose — the
    logic worth testing lives in build_paths and find_canvas."""
    from selenium.webdriver.common.actions import interaction
    from selenium.webdriver.common.actions.action_builder import ActionBuilder
    from selenium.webdriver.common.actions.pointer_input import PointerInput

    for i, path in enumerate(paths):
        if i:
            time.sleep(STROKE_GAP)
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


def execute(payload: dict, status_path: Path | None = None) -> Status:
    """Find the canvas, replay the strokes, publish the outcome."""
    from apt_log import resident

    strokes = payload["strokes"]
    status = Status(id=payload.get("id", ""), state="running",
                    digest=digest(strokes)[:16])
    write_status(status, status_path)

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
        _perform(driver, build_paths(strokes, bounds,
                                     payload.get("aspect", 1.0),
                                     rotate=sideways(xml, package)))
        status.state = "done"

    try:
        resident.run(work)
    except Exception as exc:  # noqa: BLE001
        log.warning("signature replay failed: %s", exc)
        status.state, status.reason = "failed", "replay_failed"

    status.at = datetime.now().isoformat()
    write_status(status, status_path)
    log.info("signature replay %s (%s)", status.state,
             status.reason or status.digest[:8])
    return status
