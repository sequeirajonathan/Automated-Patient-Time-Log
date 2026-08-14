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

from fastapi import FastAPI, Form, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from apt_log import __version__
from apt_log import video as video_mod
from apt_log.ui import state as state_mod
from apt_log.ui.i18n import SUPPORTED, Translator, normalise
from apt_log.ui.relay import (
    KIND_CHOICE,
    KIND_OTP,
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


def _credential_screen_showing() -> bool:
    """Whether the phone is somewhere a password can be typed.

    The video stream has no per-frame decision point, so this is what stands in
    for the JPEG path's per-frame refusal. Errs to True: if the focused window
    cannot be read, the stream stops. A frozen picture costs her a moment; a
    recording of her signing in cannot be taken back.
    """
    from apt_log.feed import current_focus, looks_like_a_login_screen

    focus = current_focus()
    if not focus:
        return True
    return looks_like_a_login_screen(focus)


_video = video_mod.Streamer(on_data=lambda _b: None,
                            unsafe=_credential_screen_showing)
_video_sinks: set = set()


def _fan_out(chunk: bytes) -> None:
    for sink in list(_video_sinks):
        sink(chunk)


_video._on_data = _fan_out


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
            "device_notice": (request.query_params.get("device")
                              if request.query_params.get("device") in ("sent", "failed")
                              else ""),
            "Health": state_mod.Health,
            "KIND_SIGNATURE": KIND_SIGNATURE,
            "KIND_TOKEN": KIND_TOKEN,
            "KIND_CHOICE": KIND_CHOICE,
            "KIND_OTP": KIND_OTP,
        },
    )


@app.get("/screen.jpg")
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
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/frame.json")
def frame_map():
    """The tappable structure of the current screen, for the overlay.

    Carries no text by construction (see feed.elements), so this is the one
    endpoint that can be logged, cached, or dumped into a console without
    thinking about who is on the screen.
    """
    path = state_mod.STATE_DIR / "frame.json"
    if not path.exists():
        return JSONResponse({"id": "", "size": [0, 0], "elements": []})
    return Response(path.read_text(encoding="utf-8"),
                    media_type="application/json",
                    headers={"Cache-Control": "no-store"})


@app.post("/tap")
async def tap_element(request: Request):
    """Tap something she aimed at on the mirrored screen.

    Coordinates are never accepted. The body names an element from a specific
    frame, and the server re-reads the screen and refuses if anything tappable
    moved — because a blind coordinate lands on whatever occupies that spot now,
    and on this app that can be a verification prompt that calls the agency.

    409 means "look again", not "failed". The page refreshes and she re-aims.
    """
    from apt_log.feed import NotOnScreen, StaleAim, tap

    payload = await request.json()
    frame = payload.get("frame")
    element = payload.get("element") or {}
    if not frame or not isinstance(element, dict):
        return JSONResponse({"error": "malformed"}, status_code=400)

    try:
        result = tap(frame, element)
    except StaleAim as exc:
        log.info("tap refused: %s", exc)
        return JSONResponse({"error": "stale"}, status_code=409)
    except NotOnScreen as exc:
        log.warning("tap refused: %s", exc)
        return JSONResponse({"error": "not_on_screen"}, status_code=409)

    return JSONResponse({"ok": True, **result})


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


def _read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


EMPTY_FRAME = {"id": "", "img": "", "size": [0, 0], "elements": []}
SLOW_EVERY = 10.0

# From the SPS the device actually emits: profile_idc 0x64 (High), level 0x29
# (4.1). Read off the stream rather than assumed, because a wrong codec string
# makes VideoDecoder refuse to configure with no useful explanation.
VIDEO_CODEC = "avc1.640029"

# Chunks held for a socket that is not keeping up. Small on purpose -- see
# queue_chunk.
VIDEO_BACKLOG = 64


@app.websocket("/ws")
async def live(ws: WebSocket):
    """One connection carrying frames down and taps up.

    Replaces a 2-second poll for the screen and a full page reload for
    everything else. The reload mattered more than it sounds: every form POST
    used to answer with a redirect, so acknowledging a message or sending a code
    threw away her scroll position and the screen she was looking at, mid-visit,
    on a phone.

    **Two clocks, because the costs are nothing alike.** The screen changes
    constantly and reading it is two small file reads. System health barely
    changes and reading it shells out to systemctl three times and to adb once.
    The first version ran both every second on the event loop, which stalled the
    server hard enough that the socket never sent its first message -- the whole
    UI process blocked on subprocesses, for every connected client. The slow half
    now runs on a worker thread every ten seconds.

    The HTTP routes all still work. A socket is an enhancement, not a
    dependency -- she may be on whatever browser her phone has, standing in
    someone's kitchen.
    """
    await ws.accept()
    chosen = ws.cookies.get(LANGUAGE_COOKIE)
    if chosen not in SUPPORTED:
        chosen = normalise(ws.headers.get("accept-language"))
    t = Translator(chosen)

    frame_path = state_mod.STATE_DIR / "frame.json"
    last: dict = {}
    slow_at = 0.0
    watching_video = False
    sinks: list = []
    pending: list[bytes] = []

    def queue_chunk(chunk: bytes) -> None:
        # Dropped rather than queued without bound: on a slow link the right
        # thing for live video is to fall behind by losing frames, not by
        # growing a backlog she will watch play out minutes late.
        if len(pending) < VIDEO_BACKLOG:
            pending.append(chunk)

    def _release() -> None:
        for sink in sinks:
            _video_sinks.discard(sink)
        sinks.clear()
        pending.clear()

    try:
        while True:
            payload: dict = {}

            frame = _read_json(frame_path, EMPTY_FRAME)
            if frame != last.get("frame"):
                payload["frame"] = last["frame"] = frame

            pending = queue.current()
            nonce = pending["nonce"] if pending else ""
            if nonce != last.get("nonce"):
                last["nonce"] = nonce
                payload["relay_nonce"] = nonce
                # Re-rendered only when the request changes, not every tick: she
                # may be typing into this panel.
                payload["relay_html"] = templates.get_template("_relay.html").render(
                    t=t, pending=pending,
                    KIND_SIGNATURE=KIND_SIGNATURE, KIND_TOKEN=KIND_TOKEN,
                    KIND_CHOICE=KIND_CHOICE, KIND_OTP=KIND_OTP,
                )

            now = asyncio.get_event_loop().time()
            if now - slow_at >= SLOW_EVERY:
                slow_at = now
                s = await asyncio.to_thread(state_mod.collect)
                mirror = _mirror_payload(s, t)
                if mirror != last.get("mirror"):
                    payload["mirror"] = last["mirror"] = mirror
                if s.paused != last.get("paused"):
                    payload["paused"] = last["paused"] = s.paused

            if payload:
                await ws.send_json({"type": "state", **payload})

            while pending:
                await ws.send_bytes(pending.pop(0))

            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except (ValueError, TypeError):
                continue

            if msg.get("type") == "tap":
                await ws.send_json(await _do_tap(msg))
            elif msg.get("type") == "video":
                want = bool(msg.get("on"))
                if want and not watching_video:
                    watching_video = True
                    loop = asyncio.get_running_loop()

                    def sink(chunk: bytes, _loop=loop) -> None:
                        # Hops to the event loop thread: the encoder pumps on a
                        # plain thread and websockets are not thread-safe.
                        _loop.call_soon_threadsafe(queue_chunk, chunk)

                    sinks.append(sink)
                    _video_sinks.add(sink)
                    await asyncio.to_thread(_video.attach)
                    await ws.send_json({"type": "video", "on": True,
                                        "codec": VIDEO_CODEC})
                elif not want and watching_video:
                    watching_video = False
                    _release()
                    await ws.send_json({"type": "video", "on": False})
    except WebSocketDisconnect:
        return
    except RuntimeError:
        # Socket closed underneath us mid-send; nothing to clean up.
        return
    finally:
        if watching_video:
            _release()
            await asyncio.to_thread(_video.detach)


async def _do_tap(msg: dict) -> dict:
    from apt_log.feed import NotOnScreen, StaleAim, tap

    element = msg.get("element") or {}
    frame = msg.get("frame") or ""
    if not frame or not isinstance(element, dict):
        return {"type": "tap_result", "ok": False, "reason": "malformed"}
    try:
        # A tap dumps the hierarchy up to five times; on the event loop that
        # would freeze every other viewer's frames while she taps.
        await asyncio.to_thread(tap, frame, element)
    except StaleAim as exc:
        log.info("tap refused: %s", exc)
        return {"type": "tap_result", "ok": False, "reason": "stale"}
    except NotOnScreen as exc:
        log.warning("tap refused: %s", exc)
        return {"type": "tap_result", "ok": False, "reason": "stale"}
    return {"type": "tap_result", "ok": True}


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
    if kind not in (KIND_TOKEN, KIND_CHOICE, KIND_OTP):
        # Signatures go through /signature, because strokes are not a form field.
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


@app.post("/device")
def device_action(action: str = Form(...)):
    """Send one of a fixed set of harmless actions to the phone.

    Added because the screen goes to sleep and the phone-view panel then shows a
    black rectangle, which is indistinguishable from the feed being broken.

    The action names are an allow-list in `device.UI_ACTIONS`, not a keycode
    parameter. That distinction is the whole safety argument: waking a screen
    cannot record a visit or answer a verification prompt, but a route that
    forwards arbitrary keycodes could do both by driving the app directly. The
    "no override from the UI" property has to live in the routing rather than in
    what someone types into a form.
    """
    from apt_log.device import DeviceUnavailable, send_ui_action

    try:
        send_ui_action(action)
    except DeviceUnavailable as exc:
        log.warning("device action refused: %s", exc)
        return RedirectResponse(url="/?device=failed", status_code=303)
    log.info("device action %s sent from the UI", action)
    return RedirectResponse(url="/?device=sent", status_code=303)


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
