#!/usr/bin/env python3
"""Screenshot every state the portal can be in — without a phone attached.

The owner cannot send a video of "it looks wrong here", and nobody can stand
over the phone in Florida waiting for a screen to come up. So this drives the
real UI process against fabricated state files and photographs it, the way a
Cypress suite would: every page-level state (picker, live reflow, asleep,
launcher card, blocked, loading, offline, stale, dark), and then — the useful
half — every screen structure the flight recorder has ever seen, replayed
through the renderer with synthesized text. A screen that rendered badly at
2 PM in Florida gets its screenshot taken here at midnight and fixed.

    python scripts/portal-gallery.py [--flight flight.jsonl] [--out gallery/]

Needs Playwright with Chromium (dev machine tooling — never runs on the Pi).
The output directory is disposable and never committed.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

VIEW = {"width": 393, "height": 852}          # an iPhone-ish portal viewport
PORT = 8899


def fresh(doc: dict) -> dict:
    """Stamp a doc so the page calls it live rather than stale."""
    doc.setdefault("id", "gallery")
    doc["at"] = datetime.now().isoformat()
    doc["h_at"] = time.time()
    doc.setdefault("size", [720, 1600])
    doc.setdefault("blocked", "")
    doc.setdefault("notice", "")
    doc.setdefault("elements", [])
    doc.setdefault("statics", [])
    return doc


def el(rid, cls, b, txt="", checked=False):
    return {"rid": rid, "cls": cls, "b": b, "txt": txt, "checked": checked,
            "focused": False, "has_text": bool(txt)}


def st(b, txt, cls="TextView"):
    return {"cls": cls, "b": b, "txt": txt}


def visit_screen() -> dict:
    """A hand-built care-plan screen: every reflow feature on one page.

    All names fictional — this file is committed and the repo is public.
    """
    return fresh({
        "app": "com.hhaexchange.caregiver", "screen": "visit",
        "activity": "visitdetailactivity",
        "elements": [
            el("btn_back", "ImageButton", [10, 60, 90, 140]),
            el("", "RelativeLayout", [0, 260, 720, 420]),
            el("chk_yes_1", "RadioButton", [560, 470, 630, 530], "✓"),
            el("chk_no_1", "RadioButton", [636, 470, 706, 530], "✗"),
            el("chk_yes_2", "RadioButton", [560, 560, 630, 620], "✓"),
            el("chk_no_2", "RadioButton", [636, 560, 706, 620], "✗"),
            el("btn_save", "Button", [40, 1380, 680, 1470], "Guardar"),
        ],
        "statics": [
            st([100, 70, 620, 130], "Plan de cuidado"),
            st([20, 280, 700, 340], "Visita no programada"),
            st([20, 350, 700, 410], "08:00 PM - 09:00 PM"),
            st([30, 480, 540, 525], "100 - Baño de esponja"),
            st([30, 570, 540, 615], "127 - Uso del inodoro"),
        ],
    })


SCENES = [
    # name, screen.json doc, JS run after load (the page's own classes — the
    # same ones its socket client sets — forced the way Cypress forces state).
    ("01-picker", visit_screen(), ""),
    ("02-visit-reflow", visit_screen(), "toScreen()"),
    ("03-signin-blocked", fresh({
        "app": "com.hhaexchange.caregiver", "screen": "login",
        "activity": "signinactivity", "blocked": "login_activity",
        "elements": [el("btn_login", "Button", [40, 900, 680, 990],
                        "INICIAR SESIÓN")],
        "statics": [st([40, 700, 680, 760], "Bienvenido")],
    }), "toScreen()"),
    ("04-asleep", fresh({
        "app": "", "screen": "unknown", "blocked": "no_focus",
        "elements": [], "statics": [],
    }), "toScreen(); cls('asleep')"),
    ("05-launcher-card", fresh({
        "app": "com.android.launcher3", "screen": "launcher",
        "activity": "quicksteplauncher",
        "statics": [st([40, 200, 300, 260], "x" * 8)],
    }), "toScreen(); cls('offapp')"),
    ("06-opening", visit_screen(), "toScreen(); busy('Opening HHAeXchange…')"),
    ("07-offline", visit_screen(), "toScreen(); cls('offline'); label('Disconnected')"),
    ("08-stale", visit_screen(), "toScreen(); cls('stale'); label('Syncing…')"),
]


def serve(state_dir: Path) -> None:
    import importlib

    import apt_log.ui.state as state_mod
    state_mod.STATE_DIR = state_dir
    # Not `import apt_log.ui.app`: the package re-exports its FastAPI object
    # under the same name, and the attribute wins the binding.
    ui_app = importlib.import_module("apt_log.ui.app")
    import uvicorn

    config = uvicorn.Config(ui_app.app, host="127.0.0.1", port=PORT,
                            log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("the UI process did not come up")


JS_HELPERS = """
window.toScreen = () => { document.body.dataset.view = 'screen'; };
window.cls = (c) => document.body.classList.add(c);
window.busy = (text) => {
  document.body.classList.add('busy');
  const t = document.getElementById('busy-text');
  if (t) t.textContent = text;
};
window.label = (text) => {
  const l = document.getElementById('live-label');
  if (l) l.textContent = text;
};
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flight", type=Path, default=None,
                    help="a flight.jsonl to replay, one screenshot per entry")
    ap.add_argument("--out", type=Path, default=Path("gallery"))
    ap.add_argument("--dark", action="store_true",
                    help="photograph the scenes in dark mode too")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    state_dir = Path(tempfile.mkdtemp(prefix="aptlog-gallery-"))
    (state_dir / "frame.json").write_text(json.dumps(
        {"id": "gallery", "img": "", "at": datetime.now().isoformat(),
         "size": [720, 1600]}))
    serve(state_dir)

    from playwright.sync_api import sync_playwright

    def shoot(page, name: str, doc: dict, script: str) -> None:
        # Re-stamped at shoot time: docs built at import age past the
        # staleness threshold while earlier scenes are being photographed,
        # and every scene after the first few turns amber by accident.
        doc["at"] = datetime.now().isoformat()
        doc["h_at"] = time.time()
        (state_dir / "screen.json").write_text(json.dumps(doc))
        page.goto(f"http://127.0.0.1:{PORT}/app", wait_until="networkidle")
        page.evaluate(JS_HELPERS)
        if script:
            page.evaluate("() => { %s }" % script)
        page.wait_for_timeout(700)          # entrance animation settles
        page.screenshot(path=str(args.out / f"{name}.png"))
        print(f"  {name}.png")

    # A pre-provisioned Chromium (CI images, cloud sandboxes) may not match
    # the installed Playwright's pinned build; point at it rather than
    # downloading another browser.
    import os
    exe = os.environ.get("APTLOG_CHROMIUM")
    if not exe:
        for candidate in ("/opt/pw-browsers/chromium",):
            if Path(candidate).exists():
                exe = candidate
                break

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=exe) if exe \
            else pw.chromium.launch()
        for scheme in (["light", "dark"] if args.dark else ["light"]):
            ctx = browser.new_context(viewport=VIEW, device_scale_factor=2,
                                      is_mobile=True, has_touch=True,
                                      color_scheme=scheme)
            page = ctx.new_page()
            suffix = "" if scheme == "light" else "-dark"
            print(f"scenes ({scheme}):")
            for name, doc, script in SCENES:
                shoot(page, name + suffix, doc, script)

            if args.flight and scheme == "light":
                from apt_log import flight
                print("flight replays:")
                for i, entry in enumerate(flight.entries(args.flight)):
                    doc = fresh(flight.replay(entry))
                    app = (entry.get("app", "") or "none").rsplit(".", 1)[-1]
                    scr = entry.get("screen", "") or "unknown"
                    if doc["blocked"]:
                        # A blocked entry has no elements to draw; skip the
                        # empty frame rather than photograph nothing.
                        continue
                    shoot(page, f"flight-{i:03d}-{app}-{scr}", doc, "toScreen()")
            ctx.close()
        browser.close()
    print(f"\nwrote {len(list(args.out.glob('*.png')))} screenshots to {args.out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
