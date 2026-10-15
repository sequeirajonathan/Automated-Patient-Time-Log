"""A recorder for the signing moment, because the log could not explain it.

Mobile Caregiver+ checkouts failed for the caregiver and worked for her
brother on the same evening, with the same stored signatures, the same
density, the same order of presses and the same timing — as far as the log
could see. And the log could not see much: the portal's own taps are logged
at INFO in a process whose journal keeps only warnings, so the press that
mattered (Confirmar firma) left no trace at all, and what the app showed in
the seconds after it was never kept.

So while a signing moment is on screen, and for TAIL seconds after it ends,
every frame the feed captures is kept: the picture as a JPEG, the tree with
typed text blanked, and one line per frame in `events.jsonl`. Every press made
from the portal in that window — a tap, Back, Home, the pad's own save or
clear, an adopted signature — goes into the same file from whichever process
made it. One directory per signing moment, pruned after KEEP_DAYS.

RECORD ONLY. Nothing here taps, waits, or decides; every entry point swallows
its own failures, because a recorder that can break a checkout is worse than
no recorder.

The pictures hold patient names and signatures. They stay on the controller,
next to the stitched pages that already do, and are never published.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

STATE_DIR = Path("/var/lib/aptlog")
RECORD_DIR = STATE_DIR / "signing-record"
# Which session is open, and when it was last fed. Read by the UI process to
# decide whether a press belongs in a recording.
CURRENT_NAME = "current.json"

# How long after the last sight of the signing moment frames are still kept.
# Long enough to see what Confirmar firma led to, and the page after it.
TAIL = 30.0
# A session with nothing new for this long is closed; the next sighting opens
# a fresh one.
GAP = 120.0
# Frames kept per session, so a page left open all evening cannot fill a disk.
MAX_FRAMES = 400
KEEP_DAYS = 14
JPEG_QUALITY = 70

# The app's own marks for its signing moment. Duplicated from feed's
# SIGNATURE_MARKS rather than imported: the check here must stay pure, and
# `feed._signature_page` keeps hysteresis state that a second caller would
# corrupt.
MARKS = ("compose_view_visit_sign_off", "buttonClearSignature")

# Feed-process state.
_session: dict = {"dir": None, "last_seen": 0.0, "last_fed": 0.0,
                  "frames": 0, "img": ""}


def signing(hierarchy: str | None, canvas: bool = False) -> bool:
    """Whether this tree is the signing moment — the page or the pad."""
    return canvas or any(m in (hierarchy or "") for m in MARKS)


def _prune(root: Path, now: float) -> None:
    try:
        for d in root.iterdir():
            if d.is_dir() and now - d.stat().st_mtime > KEEP_DAYS * 86400:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def _open(root: Path, now: float) -> Path:
    _prune(root, now)
    d = root / datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    _session.update(dir=d, frames=0, img="")
    log.warning("signing recorder: recording to %s", d)
    return d


def _append(d: Path, entry: dict) -> None:
    with (d / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _mark_current(root: Path, d: Path, now: float) -> None:
    tmp = root / (CURRENT_NAME + ".tmp")
    tmp.write_text(json.dumps({"dir": d.name, "at": now}), encoding="utf-8")
    tmp.replace(root / CURRENT_NAME)


def _jpeg(png: bytes) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


def _spawn(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def _write_jpeg(target: Path, png: bytes) -> None:
    try:
        target.write_bytes(_jpeg(png))
    except Exception as exc:  # noqa: BLE001
        log.warning("signing recorder: %s", exc)


def note_frame(png: bytes | None, hierarchy: str | None, focus: str,
               canvas: bool = False, density: int = 0,
               root: Path | None = None, now: float | None = None) -> None:
    """Called by the feed once per captured frame. Never raises."""
    try:
        root = root or RECORD_DIR
        now = time.time() if now is None else now
        here = signing(hierarchy, canvas)
        if here:
            _session["last_seen"] = now
        elif now - _session["last_seen"] > TAIL:
            return
        d = _session["dir"]
        if d is None or now - _session["last_fed"] > GAP or not d.exists():
            d = _open(root, now)
        _session["last_fed"] = now
        _mark_current(root, d, now)

        n = _session["frames"]
        if n >= MAX_FRAMES:
            return
        _session["frames"] = n + 1
        stamp = datetime.fromtimestamp(now).strftime("%H%M%S.%f")[:-3]
        img = hashlib.sha256(png).hexdigest()[:12] if png else ""
        entry = {"at": datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
                 "n": n, "focus": focus, "signing": here, "canvas": canvas,
                 "density": density, "img": img}
        # The same picture twice is kept once; the line still says it repeated.
        if png and img != _session["img"]:
            # Off the feed's thread: a full-size JPEG is a few hundred
            # milliseconds on the Pi, and the frame she is watching while she
            # signs must not wait for the copy of it.
            _spawn(_write_jpeg, d / f"{n:04d}-{stamp}.jpg", png)
            entry["file"] = f"{n:04d}-{stamp}.jpg"
            _session["img"] = img
        if hierarchy:
            from apt_log.feed import hush_typed

            (d / f"{n:04d}-{stamp}.xml").write_text(hush_typed(hierarchy),
                                                    encoding="utf-8")
        _append(d, entry)
    except Exception as exc:  # noqa: BLE001 — a recorder never breaks a frame
        log.warning("signing recorder: %s", exc)


def note_press(kind: str, root: Path | None = None,
               now: float | None = None, **detail) -> None:
    """A press made from the portal. Recorded only inside an open session.

    Called from the UI process, which shares nothing with the feed but the
    disk — so the open session is read from `current.json`. Never raises.
    """
    try:
        root = root or RECORD_DIR
        now = time.time() if now is None else now
        cur = json.loads((root / CURRENT_NAME).read_text(encoding="utf-8"))
        if now - float(cur.get("at", 0)) > TAIL + 15:
            return
        d = root / str(cur["dir"])
        if not d.is_dir():
            return
        # The time and the kind are written last, so no detail can clobber
        # them.
        _append(d, {**detail, "at": datetime.fromtimestamp(now).isoformat(
            timespec="milliseconds"), "press": kind})
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001
        log.warning("signing recorder: %s", exc)
