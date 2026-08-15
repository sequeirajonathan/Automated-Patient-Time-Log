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
def find_canvas(xml: str) -> tuple[list[int] | None, str]:
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
        if (cls in CANVAS_CLASSES
                and not _attr(raw, "text")
                and _attr(raw, "clickable") != "true"
                and (b[2] - b[0]) * (b[3] - b[1]) >= screen_area * CANVAS_MIN_SHARE):
            shaped.append(b)

    pool = named or shaped
    if not pool:
        return None, "no_canvas"
    if len(pool) > 1:
        log.info("signature finder: %d candidates, refusing", len(pool))
        return None, "ambiguous"
    return pool[0], ""


# --------------------------------------------------------------------- paths
def build_paths(strokes, bounds: list[int],
                aspect: float = 1.0) -> list[list[tuple[int, int]]]:
    """Device-pixel stroke paths, confined to the canvas rectangle.

    `aspect` is the pad's own width over height. The pad normalises each axis
    to 0..1, so without it a signature drawn on a wide pad would land squashed
    into a square. One uniform scale for both axes, centred — a signature
    stretched to fill a canvas of a different shape stops looking like the one
    she drew. Every point is clamped to the inset rectangle besides; the
    mapping should never produce an outside point, and the clamp is for the
    day something does.
    """
    aspect = min(max(float(aspect or 1.0), 0.2), 8.0)
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
            px = off_x + p[0] * draw_w
            py = off_y + p[1] * draw_h
            px = min(max(px, left), left + width)
            py = min(max(py, top), top + height)
            path.append((int(px), int(py)))
        if path:
            paths.append(path)
    return paths


def digest(strokes) -> str:
    return hashlib.sha256(
        json.dumps(strokes, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


# -------------------------------------------------------------------- replay
def _perform(driver, paths) -> None:
    """Drive the strokes through W3C pointer actions. Thin on purpose — the
    logic worth testing lives in build_paths and find_canvas."""
    from selenium.webdriver.common.actions import interaction
    from selenium.webdriver.common.actions.action_builder import ActionBuilder
    from selenium.webdriver.common.actions.pointer_input import PointerInput

    for path in paths:
        actions = ActionBuilder(driver,
                                mouse=PointerInput(interaction.POINTER_TOUCH,
                                                   "touch"))
        pen = actions.pointer_action
        pen.move_to_location(*path[0])
        pen.pointer_down()
        for x, y in path[1:]:
            pen.move_to_location(x, y)
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
        bounds, refusal = find_canvas(driver.page_source)
        if bounds is None:
            status.state, status.reason = "failed", refusal
            return
        _perform(driver, build_paths(strokes, bounds,
                                     payload.get("aspect", 1.0)))
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
