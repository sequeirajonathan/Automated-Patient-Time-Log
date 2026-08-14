"""The status and signature UI.

Binds loopback only; `tailscale serve` publishes it to the tailnet
(ARCHITECTURE §2). The building LAN is shared with other residents and this page
shows visit state, so it must never reach an interface reachable from there.

Mostly read-only by design. Three write paths exist and no more:

- **signature** — REQ-10, the reason this page is interactive at all
- **acknowledge** — clears an alert
- **pause / resume** — an emergency stop when something is visibly wrong

There is deliberately no "record this visit anyway" control. Overriding the
presence gate from a web page would defeat the gate, and the gate is the thing
that makes a recorded visit mean anything.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from apt_log import __version__
from apt_log.ui import state as state_mod
from apt_log.ui.i18n import SUPPORTED, Translator, normalise
from apt_log.ui.signature_queue import SignatureQueue

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
LANGUAGE_COOKIE = "aptlog_lang"

app = FastAPI(title="APT Log", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
queue = SignatureQueue()


def _translator(request: Request) -> Translator:
    """Cookie first, then the browser's own preference (REQ-11)."""
    chosen = request.cookies.get(LANGUAGE_COOKIE)
    if chosen not in SUPPORTED:
        chosen = normalise(request.headers.get("accept-language"))
    return Translator(chosen)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Not decorative: manager.sh gates every deploy on this, and heartbeat.sh
    will not ping without it."""
    return {"status": "ok", "version": __version__}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    t = _translator(request)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "t": t,
            "lang": t.language,
            "languages": SUPPORTED,
            "s": state_mod.collect(),
            "pending_signature": queue.current(),
            "Health": state_mod.Health,
        },
    )


@app.get("/screen.png")
def phone_screen():
    """The last capture of the phone screen.

    Written by the agent through capture.safe_screenshot, which refuses while a
    password field has focus (REQ-3). Nothing here re-checks that, because
    nothing here can — by the time a file exists the moment has passed. The
    interlock has to hold at capture time, which is why it is active rather than
    advisory.
    """
    path = state_mod.SCREENSHOT_PATH
    if not path.exists():
        return Response(status_code=404)
    return Response(
        path.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/language")
def set_language(request: Request, language: str = Form(...)):
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        LANGUAGE_COOKIE, normalise(language),
        max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax",
    )
    return response


@app.get("/api/state")
def api_state():
    s = state_mod.collect()
    return JSONResponse({
        "overall": s.overall.value,
        "transport_mode": s.transport_mode,
        "paused": s.paused,
        "health": [{"key": x.key, "status": x.status.value} for x in s.health],
        "attention": len(s.attention),
        "signature_pending": queue.current() is not None,
        "generated_at": s.generated_at.isoformat(),
    })


@app.get("/events")
async def events(request: Request):
    """SSE, so a signature request reaches her without a refresh (REQ-10.2).

    She may be mid-visit on another floor; a page she has to remember to reload
    is a page she will not reload.
    """
    async def stream():
        last = None
        while True:
            if await request.is_disconnected():
                return
            pending = queue.current()
            marker = pending["nonce"] if pending else None
            if marker != last:
                last = marker
                yield f"data: {json.dumps({'signature_pending': marker is not None})}\n\n"
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/signature")
async def submit_signature(request: Request):
    """Accept captured strokes for the outstanding request (REQ-10.5).

    Rejects a payload whose nonce is not the outstanding one, so a captured
    request body cannot be replayed. The strokes are handed to the agent and
    dropped here — REQ-10.6 forbids keeping anything re-stampable.
    """
    payload = await request.json()
    nonce = payload.get("nonce")
    strokes = payload.get("strokes")

    if not isinstance(strokes, list) or not strokes:
        return JSONResponse({"error": "empty"}, status_code=400)

    try:
        digest = queue.submit(nonce, strokes)
    except KeyError:
        return JSONResponse({"error": "expired"}, status_code=409)

    log.info("signature accepted for nonce %s (sha256 %s…)", nonce, digest[:8])
    return JSONResponse({"ok": True})


@app.post("/acknowledge")
def acknowledge(attempt_id: str = Form(...)):
    state_mod.acknowledge(attempt_id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/control")
def control(action: str = Form(...)):
    if action not in ("pause", "resume"):
        return Response(status_code=400)
    state_mod.set_paused(action == "pause")
    log.warning("scheduler %sd from the UI", action)
    return RedirectResponse(url="/", status_code=303)
