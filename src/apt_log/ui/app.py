"""The relay-and-mirror page.

Binds loopback only; `tailscale serve` publishes it to the tailnet
(ARCHITECTURE §2). The building LAN is shared with other residents and this page
shows visit state, so it must never reach an interface reachable from there.

The page does two jobs, and they are the same job from two directions.

**Mirror** — it shows what the controller is looking at: which screen, what it is
doing there, and a picture of the phone. She is on another floor and this is her
only view of a device she cannot walk over to.

**Relay** — when the controller reaches something only she can supply, it stops
and asks here. A signature (REQ-10), a security token read from the patient's
home, or one of the options the app itself is offering. She answers from her
phone, the controller types it in.

Everything else is read-only, and one control is deliberately absent: there is no
"record this visit anyway". Overriding the presence gate from a web page would
defeat the gate, and the gate is the thing that makes a recorded visit mean
anything.

The relay does not weaken that, but not because a token proves anything — her
token device travels with her and says nothing about where either of them is.
It is because none of the three answers is about presence at all. A choice is
limited to options the agent enumerated when it opened the request, and REQ-5
decides whether a visit may be recorded before any of this is reached.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from apt_log import __version__
from apt_log.ui import state as state_mod
from apt_log.ui.i18n import SUPPORTED, Translator, normalise
from apt_log.ui.relay import (
    KIND_CHOICE,
    KIND_SIGNATURE,
    KIND_TOKEN,
    RelayError,
    RelayExpired,
    RelayQueue,
)

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
LANGUAGE_COOKIE = "aptlog_lang"

app = FastAPI(title="APT Log", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
queue = RelayQueue()


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
    notice = request.query_params.get("relay")
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "t": t,
            "lang": t.language,
            "languages": SUPPORTED,
            "s": state_mod.collect(),
            "pending": queue.current(),
            "relay_notice": notice if notice in ("sent", "expired", "refused") else "",
            "Health": state_mod.Health,
            "KIND_SIGNATURE": KIND_SIGNATURE,
            "KIND_TOKEN": KIND_TOKEN,
            "KIND_CHOICE": KIND_CHOICE,
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


def _mirror_payload(s, t: Translator | None = None) -> dict:
    m = s.mirror
    payload = {
        "screen": m.screen,
        "step": m.step,
        "patient_id": m.patient_id,
        "stale": m.stale,
        "at": m.at.isoformat() if m.at else None,
        "screen_at": s.screenshot_at.timestamp() if s.screenshot_at else None,
    }
    if t is not None:
        # Translated here rather than in the browser. The script has no catalog
        # and should never acquire one: a page that renders in Spanish until it
        # updates itself into English is worse than one that does not update.
        payload["text_where"] = (
            t("mirror.unreported") if m.at is None
            else t(f"mirror.screen.{m.screen}")
        )
        payload["text_step"] = t(f"mirror.step.{m.step}")
    return payload


@app.get("/api/state")
def api_state():
    s = state_mod.collect()
    pending = queue.current()
    return JSONResponse({
        "overall": s.overall.value,
        "transport_mode": s.transport_mode,
        "paused": s.paused,
        "health": [{"key": x.key, "status": x.status.value} for x in s.health],
        "attention": len(s.attention),
        # Kept alongside the general "relay" key: a signature is the one request
        # that existed before the relay did, and other things read this.
        "signature_pending": bool(pending and pending["kind"] == KIND_SIGNATURE),
        "relay": None if not pending else {
            "kind": pending["kind"],
            "patient_id": pending["patient_id"],
        },
        "mirror": _mirror_payload(s),
        "generated_at": s.generated_at.isoformat(),
    })


@app.get("/events")
async def events(request: Request):
    """SSE, so a request reaches her without a refresh (REQ-10.2).

    She may be mid-visit on another floor; a page she has to remember to reload
    is a page she will not reload. The mirror rides the same stream — a still
    frame that only updates when she thinks to pull down is not a mirror, and a
    controller that has stopped and is waiting for her looks exactly like one
    that is working if the page never moves.
    """
    t = _translator(request)

    async def stream():
        last = None
        while True:
            if await request.is_disconnected():
                return
            s = state_mod.collect()
            pending = queue.current()
            payload = {
                "relay": None if not pending else {
                    "nonce": pending["nonce"], "kind": pending["kind"],
                },
                "signature_pending": bool(pending and pending["kind"] == KIND_SIGNATURE),
                "mirror": _mirror_payload(s, t),
                "paused": s.paused,
            }
            if payload != last:
                last = payload
                yield f"data: {json.dumps(payload)}\n\n"
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
        digest = queue.submit(nonce, KIND_SIGNATURE, strokes)
    except RelayExpired:
        return JSONResponse({"error": "expired"}, status_code=409)
    except RelayError as exc:
        log.warning("signature refused: %s", exc)
        return JSONResponse({"error": "refused"}, status_code=400)

    log.info("signature accepted for nonce %s (sha256 %s…)", nonce, digest[:8])
    return JSONResponse({"ok": True})


@app.post("/relay")
def submit_relay(nonce: str = Form(...), kind: str = Form(...),
                 value: str = Form(...)):
    """Answer a token or choice request.

    Form-encoded and served without script on purpose: the signature needs a
    canvas, but a code and a two-button choice do not, and this is the path that
    has to work on whatever browser is on her phone while she is standing in
    someone's kitchen.

    Nothing here decides anything. The kind and the permitted answers were fixed
    by the agent when it opened the request; this route can only carry a reply
    back or refuse it.
    """
    if kind not in (KIND_TOKEN, KIND_CHOICE):
        # Signatures go through /signature, and there is no third thing.
        return Response(status_code=400)

    try:
        digest = queue.submit(nonce, kind, value)
    except RelayExpired:
        return RedirectResponse(url="/?relay=expired", status_code=303)
    except RelayError as exc:
        # The reason is deliberately not echoed into the URL: for a token it
        # would be describing a credential.
        log.warning("relay refused a %s: %s", kind, exc)
        return RedirectResponse(url="/?relay=refused", status_code=303)

    log.info("relay accepted a %s (sha256 %s…)", kind, digest[:8])
    return RedirectResponse(url="/?relay=sent", status_code=303)


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
