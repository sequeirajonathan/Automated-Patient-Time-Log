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
from apt_log import macros as macros_mod
from apt_log import prefs
from apt_log import schedule as schedule_mod
from apt_log import video as video_mod
from apt_log.ui import machine as machine_mod
from apt_log.ui import screenview as screenview_mod
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
# Which browser this is. Not a login and not a secret — it is what makes
# "who is on" answerable and what keeps his English off her screen.
DEVICE_COOKIE = "aptlog_device"

app = FastAPI(title="APT Log", docs_url=None, redoc_url=None)

# Identity of this running process, baked into every page shell and carried on
# every socket message. A deploy restarts the server; a page whose shell came
# from the previous one is a stale skin rendering fresh fragments — seen live
# as the new markup arriving into a cached page with none of the new styles,
# which reads as a broken design rather than a cache. When the client notices
# the mismatch it reloads itself once, so every open page follows a deploy
# instead of quietly rotting.
import uuid as _uuid

BOOT_ID = _uuid.uuid4().hex[:12]

# How many sockets are watching, published for the feed process. Auto sign-in
# reads it: a phone signing itself in at 3 AM with nobody watching is not a
# service, it is churn — the app's inactivity timer signs it back out and the
# two of them loop until morning. The file's mtime doubles as a liveness
# signal, refreshed on the slow tick, so a crashed UI cannot leave a stale
# "someone is watching" on disk.
VIEWERS_PATH = state_mod.STATE_DIR / "viewers.json"
_viewers = 0


def _publish_viewers() -> None:
    """The socket count, and when anybody last held one.

    The stamp is what survives a browser backgrounding. A phone drops its
    socket whenever the screen locks or she switches app, and read as "nobody
    is watching" that turned auto sign-in off at the exact moment a session
    expired mid-visit. `macros.someone_is_watching` reads both.

    It survives a restart of this process too, which the count cannot: a
    deploy resets the counter to zero, and every deploy tonight took auto
    sign-in offline until a browser happened to reconnect.
    """
    import time as _time

    seen = 0.0
    try:
        seen = float(json.loads(VIEWERS_PATH.read_text(encoding="utf-8"))
                     .get("seen") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        seen = 0.0
    if _viewers > 0:
        seen = _time.time()
    try:
        VIEWERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = VIEWERS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"n": _viewers, "seen": seen}),
                       encoding="utf-8")
        tmp.replace(VIEWERS_PATH)
    except OSError as exc:
        log.warning("cannot publish viewer count (%s)", exc)


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
    """This browser's language: what this device chose, then the cookie it
    chose it with, then the browser's own preference (REQ-11).

    The stored preference comes first because it is the one that survives a
    cleared cookie jar, and because two people share this portal: he switches
    to English on his phone and she keeps reading Spanish on hers, which only
    works if the choice belongs to a device rather than to the installation.
    """
    stored = prefs.language_of(request.cookies.get(DEVICE_COOKIE) or "")
    if stored in SUPPORTED:
        return Translator(stored)
    chosen = request.cookies.get(LANGUAGE_COOKIE)
    if chosen not in SUPPORTED:
        chosen = normalise(request.headers.get("accept-language"))
    return Translator(chosen)


def _device_id(request: Request) -> str:
    """Who is looking. An opaque id in a cookie — not a login, because there
    is nothing here to log into and never will be: the tailnet is the fence,
    and a password on top of it would only be one more thing she cannot get
    past standing in somebody's kitchen.

    A cookie that has gone missing does not make a new person. The store is
    asked to recognise the browser first, so the same phone comes back as
    itself instead of adding another row to "who is on" every time iOS
    forgets a jar.
    """
    return prefs.resolve(request.cookies.get(DEVICE_COOKIE) or "",
                         request.headers.get("user-agent", ""))


def _remember(response: Response, request: Request, device_id: str,
              where: str) -> Response:
    """Stamp the device cookie and record the visit, on the way out."""
    response.set_cookie(DEVICE_COOKIE, device_id, max_age=prefs.DEVICE_TTL,
                        httponly=True, samesite="lax")
    try:
        prefs.seen(device_id, agent=request.headers.get("user-agent", ""),
                   where=where)
    except Exception as exc:  # noqa: BLE001
        log.debug("cannot record the device (%s)", exc)
    return response


def _back_to(request: Request, wanted: str = "",
             fallback: str = "/app") -> str:
    """Where a settings form should return to.

    The language switch used to always redirect to `/`, which meant pressing
    it from the phone view threw her out of the phone view. A control that
    moves you somewhere else when all you asked for was a different language
    is a control people learn not to touch.

    Only a path on this portal is honoured, never a whole URL: this page can
    move the phone, and an open redirect from it is a way to get somebody to
    press a control on a page they think is somewhere else.
    """
    target = wanted or (request.headers.get("referer") or "").split("?")[0]
    if target[:4] == "http":
        from urllib.parse import urlparse

        parsed = urlparse(target)
        target = parsed.path if parsed.netloc == request.url.netloc else ""
    if not target.startswith("/") or target.startswith("//"):
        return fallback
    return target


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Not decorative: manager.sh gates every deploy on this, and heartbeat.sh
    will not ping without it."""
    return {"status": "ok", "version": __version__}


@app.get("/")
def landing():
    """The front door is the phone.

    For a year this was the status dashboard, because the status dashboard was
    written first. But nobody opens this portal to read a status dashboard —
    they open it to use the phone, and every session began by scrolling past
    seven panels of reference material to reach the one link that did the job.
    The reference material still exists, at /console, one tap away and no
    longer in the road.
    """
    return RedirectResponse(url="/app", status_code=307)


@app.get("/console", response_class=HTMLResponse)
def console(request: Request):
    """The control centre: everything about the two machines, unabridged.

    This page and /app are deliberately opposite. /app hides whatever would
    not help somebody standing in a patient's kitchen. This one hides nothing
    — the whole screen document, every override, every metric — because its
    reader is the person teaching her to use /app, and you cannot teach
    somebody through an interface that is editing what you can both see.
    """
    device_id = _device_id(request)
    t = _translator(request)
    doc = _read_json(state_mod.STATE_DIR / "screen.json", None) or {}
    focus = doc.get("app") or ""
    page = doc.get("activity") or ""
    response = templates.TemplateResponse(
        request=request,
        name="console.html",
        # no-store: a cached shell rendering a newer server's fragments is a
        # broken-looking page nobody can explain from their armchair.
        headers={"Cache-Control": "no-store"},
        context={
            "t": t,
            "boot": BOOT_ID,
            "lang": t.language,
            "languages": SUPPORTED,
            "s": state_mod.collect(),
            "Health": state_mod.Health,
            # The operational ones, in the order somebody reaches for them —
            # not every macro that exists. The sign-in walks are not offered
            # here: they run themselves when a session expires, and a button
            # duplicating that is only useful for pressing at a bad moment.
            "operations": [macros_mod.MACROS[name]
                           for name in macros_mod.OPERATIONS
                           if name in macros_mod.MACROS],
            "confirm": macros_mod.CONFIRM,
            "macro_status": macros_mod.read_status(),
            "m": machine_mod.read(),
            "machine_state": machine_mod.worst(),
            "device_id": device_id,
            "devices": prefs.devices(),
            "apps": PHONE_APPS,
            "focus_app": focus,
            "focus_page": page,
            "screen_doc": doc,
            "rows": _raw_rows(doc),
            "density": _density_model(focus, page),
            "notice": (request.query_params.get("saved") or "")[:24],
        },
    )
    return _remember(response, request, device_id, "/console")


@app.get("/app", response_class=HTMLResponse)
def phone_app(request: Request):
    """The full-screen phone view — the one she bookmarks to her home screen.

    A launcher of four tiles, then a live wireframe of whatever the phone
    shows: the screen's real structure, rendered as components instead of
    photographed. It rides the same socket, the same tap verification and the
    same macro allow-list as the control centre; this route adds a skin, not a
    capability. It is also where / lands: this is what the portal is for, and
    everything else lives at /console, one tap away.
    """
    device_id = _device_id(request)
    t = _translator(request)
    screen_doc = _read_json(state_mod.STATE_DIR / "screen.json", None)
    model = _screen_model(screen_doc) if screen_doc else None
    response = templates.TemplateResponse(
        request=request,
        name="phone.html",
        headers={"Cache-Control": "no-store"},
        context={
            "t": t,
            "boot": BOOT_ID,
            "lang": t.language,
            "languages": SUPPORTED,
            "apps": PHONE_APPS,
            "m": model,
            # The schedule's zone, for the launcher clock — see phone.html.
            # The building's, never the reader's.
            "zone": _schedule_zone(),
            "plan": _schedule_model(t),
            "arming": _arming_model(t),
            "screen_doc": screen_doc or {},
            "pending": queue.current(),
            "KIND_SIGNATURE": KIND_SIGNATURE,
            "KIND_TOKEN": KIND_TOKEN,
            "KIND_CHOICE": KIND_CHOICE,
            "KIND_OTP": KIND_OTP,
        },
    )
    return _remember(response, request, device_id, "/app")


@app.get("/sw.js")
def service_worker():
    """The push worker, served from the ROOT.

    A service worker's scope is its own directory and below, so the copy
    under /static could only ever control /static — never /app, which is the
    page a tapped notification is supposed to open. Same file, served from
    where it can do its job.
    """
    path = Path(__file__).parent / "static" / "sw.js"
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return Response(status_code=404)
    return Response(body, media_type="application/javascript",
                    headers={"Cache-Control": "no-store",
                             "Service-Worker-Allowed": "/"})


@app.get("/api/push/key")
def push_key():
    """What a browser needs to subscribe. Empty means push is unavailable on
    this machine, and the page hides its own control rather than offering one
    that cannot work."""
    from apt_log import push

    return JSONResponse({"key": push.public_key()},
                        headers={"Cache-Control": "no-store"})


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    """Remember a phone that wants to be told about the login code.

    The subscription is the browser's own object, stored as given. It is not
    a credential for anything here — it lets this server push to that phone
    and nothing else — but it is kept 0600 beside the other state all the
    same."""
    from apt_log import push

    payload = await request.json()
    ok = push.subscribe(payload.get("subscription") or {},
                        device_id=_device_id(request))
    if not ok:
        return JSONResponse({"error": "malformed"}, status_code=400)
    return JSONResponse({"ok": True, "subscribers": push.count()})


@app.delete("/api/push/subscribe")
async def push_unsubscribe(request: Request):
    from apt_log import push

    payload = await request.json()
    endpoint = (payload or {}).get("endpoint") or ""
    if endpoint:
        push.unsubscribe(endpoint)
    return JSONResponse({"ok": True, "subscribers": push.count()})


@app.get("/scan", response_class=HTMLResponse)
def page_reading(request: Request):
    """The last full-page reading, as a rendered fragment for the sheet.

    Text lines only — the read_page macro walked the page and wrote them.
    Same exposure class as screen.json: the page's own words, served over
    the tailnet to the same viewer.
    """
    t = _translator(request)
    doc = _read_json(state_mod.STATE_DIR / "scan.json", None) or {}
    lines = [str(line) for line in (doc.get("lines") or []) if str(line).strip()]
    if not lines:
        return HTMLResponse(
            f'<p class="scan-empty">{t("scan.empty")}</p>',
            headers={"Cache-Control": "no-store"})
    from markupsafe import escape

    body = "".join(f'<p class="scan-line">{escape(line)}</p>'
                   for line in lines)
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


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
def set_language(request: Request, language: str = Form(...),
                 next: str = Form("")):
    """Switch this device's language and stay where you were.

    Stored twice on purpose: in the preferences file, which is what makes it
    per-device and survives a new browser session, and in the cookie, which is
    what answers the very first request after a restart before anything has
    been looked up.
    """
    chosen = normalise(language)
    device_id = _device_id(request)
    prefs.set_language(device_id, chosen)
    response = RedirectResponse(url=_back_to(request, next), status_code=303)
    response.set_cookie(
        LANGUAGE_COOKIE, chosen,
        max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax",
    )
    return _remember(response, request, device_id, _back_to(request, next))


@app.post("/settings/name")
def set_device_name(request: Request, name: str = Form(""),
                    device: str = Form(""), next: str = Form("")):
    """Name a device. A list of opaque ids answers nobody's question about
    who is on; 'Sadia — iPhone' answers it at a glance."""
    target = device or _device_id(request)
    prefs.rename(target, name)
    response = RedirectResponse(
        url=_back_to(request, next, "/console") + "?saved=name", status_code=303)
    return _remember(response, request, _device_id(request), "/console")


@app.post("/settings/density")
def set_density(request: Request, scope: str = Form("app"),
                value: str = Form(""), app_pkg: str = Form(""),
                page: str = Form(""), next: str = Form("")):
    """Set or clear a density override.

    An empty value is not an error and not a zero: it is "clear this", which
    uncovers whatever the code's own table says. That asymmetry is the reason
    overrides live in their own store rather than being written over the
    defaults — the defaults were tuned against real screens on a phone that
    has actually been crashed by a bad value, and nothing a slider does may
    lose them.
    """
    if scope == "global":
        prefs.set_global_density(value)
    elif scope == "page" and app_pkg and page:
        prefs.set_density(app_pkg, value, page=page)
    elif app_pkg:
        prefs.set_density(app_pkg, value)
    response = RedirectResponse(
        url=_back_to(request, next, "/console") + "?saved=density",
        status_code=303)
    return _remember(response, request, _device_id(request), "/console")


@app.post("/settings/density/clear")
def clear_density(request: Request, key: str = Form(...), next: str = Form("")):
    """Remove one override by its stored key, from the list of what is in
    force. Same call as setting an empty value; separate route because a list
    of things to undo is a different control from a slider."""
    app_pkg, _, page = key.partition("::")
    prefs.set_density(app_pkg, None, page=page)
    response = RedirectResponse(
        url=_back_to(request, next, "/console") + "?saved=cleared",
        status_code=303)
    return _remember(response, request, _device_id(request), "/console")


@app.get("/api/machine")
def api_machine():
    """The metrics, for a page that is left open. Cached in the module, so a
    tab polling this every few seconds costs one reading, not one per tab."""
    doc = machine_mod.read()
    return JSONResponse({**doc, "state": machine_mod.worst(doc)},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/who")
def api_who():
    """Who has this portal open, and where. Names and languages only — there
    is nothing else stored about a device to leak."""
    return JSONResponse({"devices": prefs.devices()},
                        headers={"Cache-Control": "no-store"})


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
        payload["taken_text"] = (
            t("phone.stale", time=t.time(s.screenshot_at))
            if s.screenshot_at else t("phone.none")
        )
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


# --------------------------------------------------------------- the schedule
# Which app a visit belongs to, in the words she uses for it. The schedule file
# names packages, because that is what the machine will have to open; a tile
# says "Exchange+".
def _app_entry(package: str) -> dict:
    for entry in PHONE_APPS:
        if entry.get("package") == package:
            return entry
    return {}


def _app_label(package: str) -> str:
    return _app_entry(package).get("name") or package


# Monday-first, matching `date.weekday()` and `schedule.DAYS`. Keys rather
# than words: `strftime("%A")` answers in the C locale, which is English, and
# a Spanish page reading "Thursday" is the kind of miss that survives for
# months because the rest of the sentence around it is translated.
DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _a_visit(visit, now, t) -> dict:
    """One visit as the front end needs it.

    Times are pre-formatted HERE rather than in the browser, and that is
    deliberate: the phone showing this page may be in another timezone —
    Texas has been used for exactly this project's testing — and a browser
    formatting an instant would quietly render Eastern visits in whatever
    zone the person holding it is standing in. The times a caregiver's round
    runs on are the agency's, wherever she reads them.
    """
    # The app the visit belongs to, carried whole. A visit on the home screen
    # is a way INTO the app that holds it — she reads "Lucresia at 6:05" and
    # the next thing she wants is Exchange+, not a second tap on a tile she has
    # to match up by memory. That needs the same three attributes a launcher
    # tile carries, so the existing launch path can be reused rather than
    # re-implemented beside it.
    entry = _app_entry(visit.app)
    return {
        "patient": visit.patient,
        "app": _app_label(visit.app),
        "package": visit.app,
        "macro": entry.get("macro", ""),
        "open": entry.get("open", ""),
        "mark": entry.get("mark", ""),
        "accent": entry.get("accent", "#666"),
        "agency": visit.agency,
        "starts": _clock(visit.starts),
        "ends": _clock(visit.ends),
        "fires": _clock(visit.fires),
        "day": t("day.long.%s" % DAY_KEYS[visit.fires.weekday()]),
        "date": visit.fires.date().isoformat(),
        # The buffer is worth showing, not hiding: it is the one time on the
        # page that is NOT what the app says, and a caregiver who spots the
        # difference should find it explained rather than be left to wonder
        # which of the two is wrong.
        "buffered": visit.buffered,
        "part": visit.block.part,
        "of": visit.block.of,
        "running": visit.running(now),
        # The agency's rule for a split visit: enter on the first half, leave
        # on the last, nothing at the seam. See schedule.Visit.
        "does_entry": visit.does_entry,
        "does_exit": visit.does_exit,
        "entry_at": _clock(visit.entry_at) if visit.entry_at else "",
        "exit_at": _clock(visit.exit_at) if visit.exit_at else "",
        # Seconds, for a client that wants to count down without re-reading
        # the clock's timezone. Negative once it has fired.
        "in_seconds": int((visit.fires - now).total_seconds()),
    }


# EVERY TIME THIS PAGE SHOWS, WITH THE ZONE IT IS IN.
#
# The building is in Eastern and so is the schedule, so every hour this
# project reasons about is Eastern — but the people reading the page are not
# all in it. Reported by the owner from Central: "I'm getting confused with
# my CDT local time, would like to avoid any confusion on my end." An hour
# with no zone on it is an hour the reader has to guess at, and the guess is
# wrong for anyone outside Florida.
#
# Taken from the datetime itself rather than written down, so it says EDT in
# August and EST in January without anybody remembering to change it.
def _clock(value, zone: str = "") -> str:
    """`5:00 am EDT`. The zone is never omitted, however obvious it looks."""
    said = value.strftime("%-I:%M %p").lower()
    mark = value.strftime("%Z") if value.tzinfo else zone
    return f"{said} {mark}".strip()


def _zone_says(zone) -> str:
    """What the schedule's zone calls itself today — EDT or EST.

    For the times the schedule keeps as bare clock readings, which carry no
    date and so cannot answer this for themselves.
    """
    from datetime import datetime as _dt

    try:
        return _dt.now(zone).strftime("%Z")
    except Exception:  # noqa: BLE001
        return ""


def _schedule_zone() -> str:
    """The zone the schedule keeps its hours in, by name, or "".

    Handed to the page so a clock rendered in somebody's browser can show
    the phone's hour rather than theirs. "" when there is no schedule to
    ask, and the page falls back to the reader's own clock — an honest
    wrong answer beats a blank face.
    """
    try:
        return str(schedule_mod.load().zone)
    except Exception:  # noqa: BLE001
        return ""


def _schedule_model(t) -> dict:
    """What the home screen shows, and what /api/schedule answers with.

    A schedule that will not parse is reported rather than swallowed. The
    portal still renders — she has a phone to drive and this module is not
    what she opened it for — but the module says it cannot read the file
    instead of showing an empty week, which looks exactly like a day off.
    """
    from datetime import datetime

    try:
        plan = schedule_mod.load()
    except schedule_mod.BadSchedule as exc:
        log.warning("the schedule will not load: %s", exc)
        return {"ok": False, "error": str(exc), "current": None,
                "next": None, "queue": [], "week": []}
    now = datetime.now(plan.zone)
    current = plan.current(now)
    upcoming = plan.upcoming(now, limit=6)
    week = plan.week(now.date())
    return {
        "ok": True,
        "error": "",
        "configured": bool(len(plan)),
        "current": _a_visit(current, now, t) if current else None,
        "next": _a_visit(upcoming[0], now, t) if upcoming else None,
        # Everything after the next one — what the reveal cycles through and
        # what a caller counting ahead reads.
        "queue": [_a_visit(v, now, t) for v in upcoming[1:]],
        "week": [_a_visit(v, now, t) for v in week],
    }


def _first_half(plan, block) -> str:
    """When this split visit's check-in actually happens, in words.

    "" when there is no earlier half to point at, which makes the sentence
    read a little short rather than wrong.
    """
    says = _zone_says(plan.zone)
    mine = [b for b in plan.blocks
            if b.patient == block.patient and b.app == block.app
            and b.of == block.of and b.days == block.days]
    first = next((b for b in mine if b.part == 1), None)
    return _clock(first.start, says) if first else ""


def _arming_model(t) -> dict:
    """Every recurring block, with a switch each.

    One row per BLOCK, not per occurrence: "arm this patient's Monday
    morning" is a standing decision about a recurring thing. The week view
    lists the same block once per day it falls on and would ask the same
    question five times.
    """
    from apt_log import arming, autoentry

    try:
        plan = schedule_mod.load()
    except schedule_mod.BadSchedule as exc:
        return {"ok": False, "error": str(exc), "blocks": [], "armed": 0}
    on = arming.armed()
    says = _zone_says(plan.zone)
    claims = arming.attestations()
    rows = []
    for block in plan.blocks:
        key = arming.key_for(block)
        # WHETHER ARMING THIS ONE WOULD ACTUALLY DO ANYTHING. A switch that
        # looks identical to a working one and cannot fire is the worst thing
        # this page could show: it would read as "the check-in is handled"
        # for a visit nobody is going to check in. HHAeXchange+'s control has
        # never been walked, so its rows say so on their face.
        # The block, not just the app: a later half of a split visit has no
        # check-in to make however walked its app is.
        why = autoentry.refusal(block.app, "entry", block)
        claim = claims.get(key) or {}
        rows.append({
            "key": key,
            "armed": key in on,
            "fires": not why,
            "why": why,
            # The refusal for a later half names WHERE the check-in actually
            # is, because "not here" without "there" leaves somebody hunting
            # for a switch that does exist, one row up.
            "why_says": (t("arm.why.%s" % why, other=_first_half(plan, block))
                         if why else ""),
            # Who made the presence claim, so the page shows an attested
            # switch as attested rather than as a setting.
            "who": str(claim.get("who") or "") if key in on else "",
            "patient": block.patient,
            "app": _app_label(block.app),
            "mark": _app_entry(block.app).get("mark", ""),
            "accent": _app_entry(block.app).get("accent", "#666"),
            "agency": block.agency,
            "days": [t("day.%s" % DAY_KEYS[d]) for d in sorted(block.days)],
            # Bare clock readings with no date on them, so the zone has to
            # be supplied rather than read off the value — see `_zone_says`.
            "start": _clock(block.start, says),
            "end": _clock(block.end, says),
            "part": block.part,
            "of": block.of,
        })
    rows.sort(key=lambda r: (r["patient"], r["start"], r["part"]))
    return {"ok": True, "error": "", "blocks": rows,
            "armed": sum(1 for r in rows if r["armed"]),
            # Armed AND able to act. The two numbers differ exactly when
            # somebody has armed a visit whose app cannot be pressed, and
            # that difference is the thing worth showing.
            "firing": sum(1 for r in rows if r["armed"] and r["fires"])}


@app.post("/schedule/arm")
def schedule_arm(request: Request, key: str = Form(...),
                 on: str = Form("")):
    """Switch one block on or off.

    A POST because it changes what the machine is allowed to do, and it
    answers with the state it actually reached rather than the state it was
    asked for — a switch that looks thrown and is not is worse than one that
    refuses.
    """
    from apt_log import arming, prefs as prefs_mod

    # WHO IS MAKING THE PRESENCE CLAIM (REQ-5.9). Arming is an attestation,
    # not a preference: it says the caregiver will be at that patient's home,
    # and the machine writes an EVV record on the strength of it. So the
    # device's own name is recorded beside the switch and travels into the
    # audit entry. Unnamed devices attest as "unknown" rather than not at all
    # — a missing name must never quietly mean "not armed".
    who = ""
    try:
        who = prefs_mod.device(_device_id(request)).get("name") or ""
    except Exception:  # noqa: BLE001 — an unnamed device still arms
        log.debug("could not name the device arming this", exc_info=True)
    now = arming.set_armed(key[:64], on == "1", who=who)
    return JSONResponse({"key": key[:64], "armed": now,
                         "total": len(arming.armed())})


@app.get("/api/schedule")
def api_schedule(request: Request):
    """The round, refreshed without a reload.

    The home screen is server-rendered, which is right for the first paint
    and wrong ten minutes later: the next visit becomes the current one while
    the page sits open on a kitchen counter. The client re-reads this rather
    than reloading, because a reload would take her out of whatever else she
    was doing on the page.
    """
    return JSONResponse(_schedule_model(_translator(request)))


def _read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _sans_at(doc: dict) -> dict:
    """The document minus its timestamp, for change comparison."""
    return {k: v for k, v in doc.items() if k != "at"}


def _pending_task_count(doc: dict) -> int:
    """How many required tasks this page is still missing.

    Zero everywhere that is not a plan of care, which is what makes it safe
    to hang a button on. The count comes from `macros.pending_tasks` — the
    same reading the macro itself runs — so the number she is shown is the
    number that will be ticked, and the button cannot appear over a page the
    macro would then decline to act on.

    Never raises: this is one field of a payload that carries the whole
    screen, and a page shaped unexpectedly must cost the button, not the
    frame.
    """
    from apt_log import macros as macros_mod

    try:
        size = doc.get("size") or [0, 0]
        return len(macros_mod.pending_tasks(
            doc.get("elements") or [], doc.get("statics") or [],
            size[0] if len(size) == 2 else 0, doc.get("app") or ""))
    except Exception:
        log.exception("counting the page's unticked tasks")
        return 0


def _update_wall(doc: dict) -> bool:
    """Whether the app in front is refusing to work until it is updated.

    False on every ordinary screen, which is what makes it safe to hang a
    card and a button on — and the card matters here more than most, because
    the wall's own button leads to the Play Store and the containment
    watchdog bounces her back out of it within five seconds. Without this she
    is looking at a page with one control that cannot work.

    Never raises, for the same reason as the task count: a page shaped
    unexpectedly must cost the card, not the frame.
    """
    from apt_log import macros as macros_mod

    try:
        return macros_mod.update_wall_on_screen(doc)
    except Exception:
        log.exception("looking for an update wall")
        return False


def _code_screen(doc: dict) -> bool:
    """Whether inMyTeam is asking for the code it just texted.

    Gated on the CODE SCREEN rather than on the app's expired-code wording,
    and that is a deliberate choice about what can be relied on. The words an
    expired code produces have not been read off the live phone; the code
    screen has — a field plus the app's own word for a code — and it is the
    same test the sign-in macro stops at.

    So the way to a new code is offered for as long as she is on the screen
    where a code is what is wanted, rather than appearing only once the
    portal recognises a sentence nobody has verified. Wrong-but-unexpired is
    also a reason to want another one.

    Never raises: one field of a payload carrying the whole screen.
    """
    from apt_log import macros as macros_mod

    try:
        if (doc.get("app") or "") != "com.inmyteam.inmyteam":
            return False
        items = (doc.get("elements") or []) + (doc.get("statics") or [])
        if not any((e.get("cls") or "").endswith("EditText") for e in
                   (doc.get("elements") or [])):
            return False
        words = " ".join((e.get("txt") or "") for e in items).lower()
        return any(w in words for w in macros_mod._CODE_WORDS)
    except Exception:
        log.exception("looking for the code screen")
        return False


def _sheet_actions(doc: dict, model: dict | None) -> list[dict]:
    """The app's own buttons on a signature sheet, as aims.

    Gated on the screen being a MODAL — the reflow found a way out of it, so
    the app has a sheet in front — and on the row the reflow already
    recognised as that sheet's actions. Both are needed: Visit Detail has an
    actions row too, and it is "Check in" and "Note & Check out", which must
    never be shipped into the signature pad.

    Deliberately NOT gated on `canvas`. That flag comes from a mark in the
    hierarchy and it flickers: caught live reading False while the signature
    sheet was plainly open with Done and Clear on it, which would have made
    the pad's buttons come and go under her hand. A sheet is a sheet whether
    or not the canvas node happened to be in that dump.

    The captions are the app's own words, because they name the app's
    buttons and not the portal's.
    """
    # A SIGNATURE SCREEN IS NOT ALWAYS A SHEET. inMyTeam puts its pad in a
    # bottom sheet, which is where the `dismiss` gate comes from; HHAeXchange+
    # gives the pad a whole screen of its own, so the gate said no and the pad
    # fell back to the LEGACY pair — two presses at coordinates derived for a
    # different app's rotated page, aimed at nothing in particular.
    #
    # Where the app names its own Borrar and Enviar, they are real elements
    # with real bounds and they ride as ordinary verified taps. The pad shows
    # what the phone actually has, on either shape of signature screen.
    # NOT GATED ON `canvas` EITHER, for the reason stated two paragraphs up:
    # that flag flickers. Gating the NAMED buttons on it meant a flicker took
    # the pad's only working step-two controls away and dropped it onto the
    # legacy coordinate pair, which on this app cannot press anything at all
    # (see `sign.ROTATED_CANVAS_APPS` — the pair is for the LEGACY app).
    # `_canvas_actions` is safe ungated: it demands a resource-id naming both
    # the signature screen and the action, which no ordinary page carries.
    named = _canvas_actions(doc)
    if named:
        return named
    if not model or not model.get("dismiss"):
        return []
    for row in model.get("rows") or ():
        if not row.get("actions"):
            continue
        return _pad_order([{"txt": (it.get("txt")
                                    or (it.get("lines") or [""])[0] or ""),
                            "aim": it["aim"]}
                           for it in row["items"] if it.get("aim")])
    return []


# THE PAD READS THE SAME WHICHEVER APP IS BEHIND IT.
#
# The two apps put their buttons in opposite orders — HHAeXchange+ draws
# Borrar then Enviar, inMyTeam draws Done then Clear — and the pad was
# showing each app's own order, so the affirmative sat on the right for one
# and on the left for the other. Muscle memory built on one becomes a wrong
# press on the other, and the wrong press here wipes a signature.
#
# `_canvas_actions` has always ordered its pair deliberately (clear first,
# submit last). This is the same rule applied to the apps whose buttons carry
# no ids, so the pair is uniform everywhere rather than uniform in one place.
_ERASE_WORDS = ("borrar", "clear", "limpiar", "erase", "anular")


def _pad_order(actions: list[dict]) -> list[dict]:
    """Destructive first, affirmative last. Anything unrecognised keeps its
    place in the middle rather than being guessed at."""
    def rank(action: dict) -> int:
        word = (action.get("txt") or "").strip().lower()
        return 0 if word in _ERASE_WORDS else 1

    return sorted(actions, key=rank)


def _legacy_pad(package: str) -> bool:
    """Whether the legacy coordinate pair can press anything on this app.

    `sign.button_targets` answers None off `ROTATED_CANVAS_APPS`, so on every
    other app that pair is two buttons wired to a guaranteed refusal.
    """
    from apt_log import sign as sign_mod

    return bool(package) and package in sign_mod.ROTATED_CANVAS_APPS


# What to call a signature button whose overlaid label did not arrive. The
# app's own words on the app whose ids these are, so the pad still reads as
# the phone's screen and not as the portal's own invention.
_FALLBACK_WORD = {"clear": "Borrar", "confirm": "Enviar"}


def _canvas_actions(doc: dict) -> list[dict]:
    """The signature screen's own clear and submit, as aims.

    Read off the published elements rather than the raw tree, so the aims are
    the same ones every other tap on this page uses and are verified the same
    way. The captions are the app's own words, taken from the labels it lays
    over its buttons — a signature screen shows a patient's name, so the text
    does not travel, and these two come from the screen's own chrome.

    Clear first, submit last: the pad emphasises the last one, and the last
    one has to be the affirmative.

    A BUTTON IS NOT DROPPED FOR WANT OF ITS LABEL. The caption is drawn as a
    separate TextView laid over the button, and a dump that arrives without
    it — mid-repaint, or with the label folded elsewhere — used to remove the
    button from this list entirely. An empty list is not "no buttons": it
    sends the pad to the legacy coordinate pair, which on this app presses
    nothing. The resource-id has already named both the screen and the
    action by the time we get here, so where the label is absent the action's
    own word stands in and the button stays pressable.
    """
    from apt_log import sign as sign_mod

    order = {"clear": 0, "confirm": 1}
    found: list[tuple[int, dict]] = []
    for element in doc.get("elements") or []:
        rid = (element.get("rid") or "").lower()
        for kind, ids in (("clear", sign_mod._CLEAR_IDS),
                          ("confirm", sign_mod._SAVE_IDS)):
            if not any(i in rid for i in ids):
                continue
            if not any(h in rid for h in sign_mod.CANVAS_ID_HINTS):
                continue          # see sign._app_buttons: a generic id is not
            caption = _caption_over(doc, element["b"]) or _FALLBACK_WORD[kind]
            found.append((order[kind],
                          {"txt": caption,
                           "aim": {"rid": element.get("rid", ""),
                                   "cls": element.get("cls", ""),
                                   "b": element["b"]}}))
            break
    return [action for _, action in sorted(found, key=lambda p: p[0])]


def _caption_over(doc: dict, bounds: list[int]) -> str:
    """The word the app laid over a button, or "".

    HHAeXchange+'s buttons carry no text of their own: "Borrar" and "Enviar"
    are separate labels drawn on top of them. Overlapping is the whole
    relationship — the label sits inside the button's box.
    """
    x1, y1, x2, y2 = bounds
    for static in doc.get("statics") or []:
        b = static.get("b") or []
        text = (static.get("txt") or "").strip()
        if len(b) == 4 and text and x1 <= b[0] and b[2] <= x2 \
                and y1 <= b[1] and b[3] <= y2:
            return text
    return ""


def _render_key(doc: dict) -> dict:
    """What the rendered wireframe actually depends on — nothing that churns.

    `h_at` moves on every hierarchy read and `img` on any repainted pixel
    (the phone's own clock is enough), so comparing the raw document re-sent
    the HTML and swapped the DOM every second over an unchanged screen —
    watched live as a steady shimmer, and on a scrolled page as the scroll
    snapping back to the top. Staleness rides its own flag; the photograph
    rides the frame payload; neither belongs in this comparison.
    """
    return {k: v for k, v in doc.items() if k not in ("at", "h_at", "img")}


# The feed writes every 1-2 seconds when alive; a document this old means
# nobody is writing, whatever it says.
SCREEN_STALE_AFTER = 8.0

# The document can be stamped fresh every second while the hierarchy inside it
# is a kept copy from minutes ago — seen live as a dismissed modal rendered
# under a green "Live" while the resident session was down. The sketch's age
# is the hierarchy's age, not the file's.
HIERARCHY_STALE_AFTER = 25.0


def _screen_age(doc: dict | None) -> float:
    from datetime import datetime as _dt

    if not doc:
        return float("inf")
    try:
        return (_dt.now() - _dt.fromisoformat(doc["at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return float("inf")


def _screen_is_stale(doc: dict | None) -> bool:
    import time as _time

    if _screen_age(doc) > SCREEN_STALE_AFTER:
        return True
    h_at = (doc or {}).get("h_at") or 0
    # Docs from before the field existed carry 0; treat that as unknown
    # rather than ancient, or every deploy would open on amber.
    if h_at and _time.time() - h_at > HIERARCHY_STALE_AFTER:
        return True
    # The sketch belongs to a different app than the one in front: an app
    # switch is in progress and the elements on the page are the *old*
    # screen's. Amber, dimmed, untappable — never the launcher's rows under
    # the new app's name with a green Live beside them.
    app = (doc or {}).get("app") or ""
    h_app = (doc or {}).get("h_app") or ""
    if app and h_app and app != h_app:
        return True
    return False


EMPTY_FRAME = {"id": "", "img": "", "size": [0, 0], "elements": [],
               "blocked": "", "notice": ""}
SLOW_EVERY = 10.0

# The apps her patients are spread across, verified against `pm list packages`
# on the device. Names are the vendors' own brands, which is why they are not
# in the catalog: a brand does not translate.
#
# `macro` is what the tile runs — for most, the open-only macro, which brings
# the app to the front and touches nothing.
#
# THREE, not four. The legacy HHAeXchange app had one patient and that patient
# was migrated to HHAeXchange+, so it is off the picker: a tile is an
# invitation, and there is nothing on the other side of that one any more.
# What it WAS is recorded in RETIRED_TILE below rather than deleted, because
# the tile is the only place this project ever wrote down that app's identity
# — its package, its brand, its colour — and that is worth keeping legible
# next to the three that are live. See feed.RETIRED_APPS for the whole
# argument and for the list of things that deliberately did not move.
RETIRED_TILE = {"id": "hhax_legacy", "name": "HHAeXchange", "mark": "HX",
                "package": "com.hhaexchange.caregiver",
                "macro": "hhax_legacy_login", "open": "open_hhax_legacy",
                "accent": "#1b6ed6"}

PHONE_APPS = (
    # `open` is the app's open-only macro — activate and wait, touch
    # nothing. The client uses it to bounce an app back when a Back press
    # turns out to have exited it to the launcher.
    {"id": "hhax_uma", "name": "HHAeXchange+", "mark": "HX+",
     "package": "com.hhaexchange.uma",
     "macro": "hhax_uma_login", "open": "open_hhax_uma",
     "accent": "#7a3fd1"},
    {"id": "mobile_caregiver", "name": "Mobile Caregiver+", "mark": "MC",
     "package": "com.tellus.evv.v2",
     "macro": "mobile_caregiver_pin", "open": "open_mobile_caregiver",
     "accent": "#0c8f5a"},
    {"id": "inmyteam", "name": "inMyTeam", "mark": "iMT",
     "package": "com.inmyteam.inmyteam",
     # The tile signs in; `open` only brings it forward. inMyTeam is the one
     # app whose sign-in ends in a text message, so a deliberate press is
     # exactly the right trigger for it — and the press is what was missing:
     # the tile ran the open-only macro and parked on the marketing splash.
     "macro": "inmyteam_login", "open": "open_inmyteam",
     "accent": "#c2452e"},
)

def _raw_rows(doc: dict) -> list[dict]:
    """Every node of the current screen, in the order it sits on the glass.

    /app renders a *reflow* of this: bands folded, furniture swept, labels
    attached to the control they belong to. That reflow is the product, and
    it is also the thing that makes the front end impossible to teach — when
    a row does not appear she cannot tell whether the app did not show it or
    the portal ate it. This is the other half of the answer: the tree as it
    came off the device, tappable and untappable together, with the resource
    ids that name each one.

    Nothing is withheld here that is published anywhere else. Typed field
    contents never reach this document in the first place (see
    feed.write_screen) — that is a rule about credentials, not about
    redaction, and it is enforced a layer below anything a page can undo.
    """
    rows: list[dict] = []
    for el in doc.get("elements") or []:
        bounds = el.get("b") or [0, 0, 0, 0]
        rows.append({
            "tap": True,
            "txt": el.get("txt", ""),
            "rid": el.get("rid", ""),
            "cls": el.get("cls", ""),
            "b": bounds,
            "enabled": el.get("enabled", True),
            "checked": el.get("checked", False),
            "selected": el.get("selected", False),
            "focused": el.get("focused", False),
            "has_text": el.get("has_text", False),
        })
    for st in doc.get("statics") or []:
        bounds = st.get("b") or [0, 0, 0, 0]
        rows.append({
            "tap": False,
            "txt": st.get("txt", ""),
            "rid": st.get("rid", ""),
            "cls": st.get("cls", ""),
            "b": bounds,
            "enabled": True,
            "checked": False,
            "selected": False,
            "focused": False,
            "has_text": bool(st.get("txt")),
        })
    rows.sort(key=lambda r: (r["b"][1], r["b"][0]))
    return rows


def _density_model(app_pkg: str, page: str) -> dict:
    """What the density panel needs: what is in force, what set it, and what
    is stored — kept apart, because the whole point is that clearing an
    override cannot lose the value underneath it."""
    from apt_log import feed as feed_mod

    page_key = f"{app_pkg}::{page}" if app_pkg and page else ""
    store = prefs.overrides()
    built_in = None
    if app_pkg:
        built_in = feed_mod.APP_DENSITY.get(app_pkg)
        if built_in is None and app_pkg in feed_mod.CARE_APPS:
            built_in = feed_mod.DEFAULT_DENSITY
    return {
        "app": app_pkg,
        "page": page,
        "page_key": page_key,
        "floor": prefs.DENSITY_FLOOR,
        "ceiling": prefs.DENSITY_CEILING,
        "step": prefs.DENSITY_STEP,
        # The code's own value for this app — what a cleared override falls
        # back to, shown so nobody has to guess what clearing will do.
        "built_in": built_in,
        "app_override": store.get(app_pkg),
        "page_override": store.get(page_key) if page_key else None,
        "global_override": store.get(prefs.GLOBAL),
        "overrides": sorted(store.items()),
    }


def _screen_model(doc: dict) -> dict | None:
    """Semantic render model for _screen.html — see ui/screenview.py.

    The first version reproduced the screen's geometry: absolute boxes at
    device coordinates, fonts scaled to fit rectangles. Faithful, and it
    looked broken. The reflow keeps what matters — controls, words, order,
    grouping, tap identity — and hands layout to a design system built for
    the width it is actually read at.
    """
    from apt_log.ui import screenview

    return screenview.build(doc)

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
    global _viewers
    _viewers += 1
    _publish_viewers()
    # Same order as _translator: this device's stored choice outranks the
    # cookie, so a socket opened before the cookie caught up still pushes
    # fragments in the language the page is rendered in. Half a page in each
    # language is how a portal stops being trusted.
    chosen = prefs.language_of(ws.cookies.get(DEVICE_COOKIE) or "")
    if chosen not in SUPPORTED:
        chosen = ws.cookies.get(LANGUAGE_COOKIE)
    if chosen not in SUPPORTED:
        chosen = normalise(ws.headers.get("accept-language"))
    t = Translator(chosen)

    # Local, like every other heavy import in this file: `sign` pulls in the
    # replay machinery and `enrolled` opens the adopted-signature store, and
    # neither belongs in the import cost of serving a page.
    from apt_log import enrolled, sign

    frame_path = state_mod.STATE_DIR / "frame.json"
    screen_path = state_mod.STATE_DIR / "screen.json"
    last: dict = {}
    slow_at = 0.0
    watching_video = False
    sinks: list = []
    # NOT `pending`: that name is already the outstanding relay request in this
    # function, and reusing it meant every loop reassigned the video backlog to a
    # dict-or-None. The send loop then saw a falsy value and shipped nothing,
    # while the cleanup path called .clear() on None. Video looked switched off
    # rather than broken, which is the expensive kind of quiet.
    outbox: list[bytes] = []

    def queue_chunk(chunk: bytes) -> None:
        # Dropped rather than queued without bound: on a slow link the right
        # thing for live video is to fall behind by losing frames, not by
        # growing a backlog she will watch play out minutes late.
        if len(outbox) < VIDEO_BACKLOG:
            outbox.append(chunk)

    def _release() -> None:
        for sink in sinks:
            _video_sinks.discard(sink)
        sinks.clear()
        outbox.clear()

    try:
        while True:
            payload: dict = {}

            # Compared without the timestamp, which moves on every write and
            # would turn "did the screen change" into "has a second passed".
            frame = _read_json(frame_path, EMPTY_FRAME)
            if _sans_at(frame) != last.get("frame"):
                last["frame"] = _sans_at(frame)
                payload["frame"] = frame

            # The wireframe rides the same tick as the frame, rendered
            # server-side like every other sentence this socket sends. Compared
            # whole rather than by id: a checkbox flipping changes no target
            # and no id, and the wireframe still has to redraw it.
            screen_doc = _read_json(screen_path, None)
            if screen_doc is not None and _render_key(screen_doc) != last.get("screen_doc"):
                last["screen_doc"] = _render_key(screen_doc)
                model = _screen_model(screen_doc)
                # Read once: it walks the statics, and the roster lookup below
                # reads a file. Both are cheap and neither is free, and this
                # runs on every screen change.
                _signer = sign.signer_named(screen_doc)
                payload["screen"] = {
                    "id": screen_doc.get("id", ""),
                    "name": screen_doc.get("screen", "unknown"),
                    "app": screen_doc.get("app", ""),
                    # Whose screen the rendered sketch is — the launch
                    # overlay must hold until the *content* is the asked-for
                    # app's, not merely the focus.
                    "h_app": screen_doc.get("h_app", ""),
                    "blocked": screen_doc.get("blocked", ""),
                    "notice": screen_doc.get("notice", ""),
                    "landscape": bool(screen_doc.get("landscape")),
                    # Whether the PHOTOGRAPH is the wrong way up. Not the
                    # same as the screen being wide — see feed's note.
                    "turn": bool(screen_doc.get("turn")),
                    # A signature moment: canvas in front AND drawn
                    # sideways. The sign sheet shows its app-side Borrar
                    # and Salvar only here — they press real pixels, and
                    # off this screen there is nothing to press.
                    "canvas": bool(screen_doc.get("canvas")
                                   and screen_doc.get("landscape")),
                    # A DRAWING SURFACE, WHATEVER WAY UP IT IS.
                    #
                    # `canvas` above means canvas AND SIDEWAYS, which is the
                    # legacy app's rotated pad and not a general fact. The
                    # pencil was gated on it and inMyTeam's "Firma del
                    # Paciente" sheet is PORTRAIT — so on the one screen that
                    # needed the pad, the pencil was hidden and an adopted
                    # signature could not be applied at all. Reported live,
                    # with the sheet open and nothing to press.
                    #
                    # This is the raw fact and the caller unions it with the
                    # app-side buttons, because this flag FLICKERS — see
                    # `_sheet_actions`, which refuses to gate on it for that
                    # reason and was sitting three lines away while the pencil
                    # was gated on it anyway.
                    "pad": bool(screen_doc.get("canvas")),
                    # WHOSE SIGNATURE THIS SCREEN IS ASKING FOR.
                    #
                    # inMyTeam's exit puts two identical pads back to back —
                    # the patient's, then the caregiver's — and the only thing
                    # telling them apart is the title bar, which names the
                    # signer. With one inMyTeam patient that could be guessed
                    # at; with two it could not, and the failure is somebody's
                    # signature recorded under another person's name.
                    #
                    # Two values, because they answer two different questions.
                    # `signer` is what the APP says, shown as a heading so the
                    # person about to press knows whose pad this is. `adopted`
                    # is which entry in the roster that resolves to, computed
                    # here rather than in the browser: the tolerance lives in
                    # one tested place, and it returns nothing at all when
                    # more than one party could be meant.
                    "signer": _signer,
                    "signer_adopted": (enrolled.who_signs(_signer)
                                       if _signer else ""),
                    # WHETHER THE LEGACY COORDINATE PAIR CAN PRESS ANYTHING.
                    #
                    # It presses a point derived from the canvas on a page the
                    # app draws rotated, and `sign.button_targets` answers None
                    # for every package outside `ROTATED_CANVAS_APPS` — which
                    # holds the LEGACY app alone. The pad showed that pair
                    # whenever the named buttons came back empty, on any app,
                    # so on HHAeXchange+ it offered a Borrar and a Salvar that
                    # could only ever answer "no_canvas". Reported as the pad's
                    # step-two button doing nothing, and finished from the
                    # phone view instead — which worked, because that path taps
                    # the element.
                    "legacy_pad": _legacy_pad(screen_doc.get("app", "")),
                    # A system panel is over the app. Nothing she taps will
                    # reach the app underneath while this is true, so the page
                    # stops pretending otherwise and offers the way out.
                    "covered": bool(screen_doc.get("covered")),
                    # How many required tasks this plan of care is still
                    # missing — 0 on every other screen in both apps. The
                    # button that ticks them appears on the strength of this
                    # and nowhere else: a control that is always there and
                    # usually does nothing is a control pressed at the wrong
                    # moment. Counted from the SAME reading the macro runs,
                    # so the number on the button is the number it will tick.
                    "tasks": _pending_task_count(screen_doc),
                    # The app is refusing to be used until it is updated.
                    # Rendering that screen as an ordinary page is what it
                    # did before: one button, which opens the Play Store,
                    # where the containment watchdog bounces her straight
                    # back — a loop with no way out from this side.
                    "walled": _update_wall(screen_doc),
                    # She is on the code screen. The one thing the app gives
                    # no way out of: an expired or mistyped code has no
                    # "send another" on it, and the only path anybody had
                    # found was to force-stop the app by hand.
                    "code_screen": _code_screen(screen_doc),
                    # The signature sheet's OWN buttons — inMyTeam's Done and
                    # Clear — carried so the pad can show them beside her own
                    # controls. Without this she draws in the portal, switches
                    # to the phone view to find the app's save, and switches
                    # back: "would also like the signature controls embedded
                    # into the pad instead of switching between phone peek and
                    # front end". They are ordinary aims and press through the
                    # ordinary verified tap; nothing here is a new capability.
                    "sheet_actions": _sheet_actions(screen_doc, model),
                    # The app's own tab bar, lifted out of the list to ride
                    # the control bar beside Back and Home. Empty on screens
                    # without one.
                    "apptabs": (model or {}).get("apptabs") or [],
                    # Whether THIS app can be asked what it has recorded
                    # today. Only inMyTeam publishes a readable work log, so
                    # the button that opens it is offered on that app and
                    # nowhere else rather than standing by everywhere and
                    # refusing when pressed.
                    "checks_app": (screen_doc.get("app") or "")
                    in macros_mod.CHECK_LOG_APPS,
                }
                payload["screen_html"] = (
                    "" if model is None
                    else templates.get_template("_screen.html").render(
                        m=screenview_mod.label_keys(model, t), t=t))

            # "Live" is a claim, and the page must not keep making it over a
            # document nobody is refreshing. The feed restarting, the resident
            # session rebuilding, the phone unplugged — all leave the last
            # screen on disk looking current. Seen on the owner's phone as a
            # sign-in screen labelled Live while the photograph showed home.
            payload_stale = _screen_is_stale(screen_doc)
            if payload_stale != last.get("screen_stale"):
                last["screen_stale"] = payload_stale
                payload["screen_stale"] = payload_stale

            pending = queue.current()
            macro = macros_mod.read_status()
            macro_state = {"state": macro.state, "step": macro.step,
                           "name": macro.name, "error": macro.error,
                           # Sentences rendered here, as everywhere on this
                           # socket — the loading screen shows these verbatim.
                           "text": t(macro.step) if macro.step else "",
                           "state_text": t(f"macro.state.{macro.state}")}

            from apt_log import sign as sign_mod

            sig = sign_mod.read_status()
            # An app-button press ("clear"/"confirm") finishing must not
            # borrow the replay's "now press the app's save" sentence —
            # the press WAS the save. Kind-specific done-sentences win
            # when the status carries a kind.
            sig_kind = getattr(sig, "kind", "")
            sig_state = {"id": sig.id, "state": sig.state,
                         "text": (t(f"sign.{sig.reason}") if sig.reason
                                  else t(f"sign.state.{sig_kind}.done")
                                  if sig_kind and sig.state == "done"
                                  else t(f"sign.state.{sig.state}"))}
            if sig_state != last.get("sign"):
                payload["sign"] = last["sign"] = sig_state
            if macro_state != last.get("macro"):
                payload["macro"] = last["macro"] = macro_state

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
                # Refreshes the mtime, which is the liveness half of the
                # viewer signal the feed reads.
                _publish_viewers()
                # HOW MANY OF US ARE ON. Two people share this portal from two
                # states, and every control on it drives one phone — so
                # "somebody else is here" is a fact worth having before you
                # reach for a button. The count is sockets, which is as close
                # to "people looking" as anything on hand.
                if _viewers != last.get("viewers"):
                    payload["viewers"] = last["viewers"] = _viewers
                s = await asyncio.to_thread(state_mod.collect)
                mirror = _mirror_payload(s, t)
                if mirror != last.get("mirror"):
                    payload["mirror"] = last["mirror"] = mirror
                if s.paused != last.get("paused"):
                    payload["paused"] = last["paused"] = s.paused

            if payload:
                # `boot` rides every message so a shell from a previous server
                # can notice it is stale and reload itself.
                await ws.send_json({"type": "state", "boot": BOOT_ID, **payload})

            while outbox:
                await ws.send_bytes(outbox.pop(0))

            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except (ValueError, TypeError):
                continue

            if msg.get("type") == "tap":
                await ws.send_json(await _do_tap(msg))
            elif msg.get("type") == "text":
                await ws.send_json(await _do_text(msg))
            elif msg.get("type") == "device":
                await ws.send_json(await _do_device(msg))
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
        _viewers = max(0, _viewers - 1)
        _publish_viewers()
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


async def _do_text(msg: dict) -> dict:
    """Type a short code into a field she aimed at.

    Exists for exactly one shape of moment: a verification code lands on a
    family member's phone and someone types it into the app's field from
    the portal. Same aim verification as a tap (refuse-if-moved), fields
    only, letters and digits only, capped short — a token channel, not a
    message pipe. The value never appears in a log on either end.
    """
    from apt_log.feed import NotOnScreen, StaleAim, type_into

    element = msg.get("element") or {}
    frame = msg.get("frame") or ""
    value = msg.get("value")
    if not frame or not isinstance(element, dict) or not isinstance(value, str):
        return {"type": "text_result", "ok": False, "reason": "malformed"}
    try:
        await asyncio.to_thread(type_into, frame, element, value)
    except ValueError:
        return {"type": "text_result", "ok": False, "reason": "malformed"}
    except (StaleAim, NotOnScreen) as exc:
        log.info("text refused: %s", exc)
        return {"type": "text_result", "ok": False, "reason": "stale"}
    return {"type": "text_result", "ok": True}


def _back_would_leave() -> bool:
    """Whether a Back press right now would take the phone out of the app.

    Only ever True on an app that ANSWERS. A package with no fragment stack
    to read — anything not built on Jetpack navigation, which is two of the
    three care apps — returns nothing from `nav_state`, and nothing must mean
    "press it and see", exactly as before, never "refuse".
    """
    from apt_log import feed as feed_mod

    try:
        focus = feed_mod.current_focus() or ""
        package = focus.split("/")[0]
        if package not in feed_mod.CARE_APPS:
            return False
        return bool(feed_mod.nav_state(package).get("rooted"))
    except Exception:  # noqa: BLE001
        return False


async def _do_device(msg: dict) -> dict:
    """Back, Home, Recents, Wake — over the socket rather than a form post.

    Same allow-list as the POST route, checked in the same place: `send_ui_action`
    refuses anything that is not a name in `device.UI_ACTIONS`. This is a second
    door onto that function, not a second policy, which is the only way to add a
    door to something whose safety argument lives in the routing.
    """
    from apt_log.device import DeviceUnavailable, send_ui_action

    action = msg.get("action")
    if not isinstance(action, str) or not action:
        return {"type": "device_result", "ok": False}
    if action == "back" and await asyncio.to_thread(_back_would_leave):
        # NOT PRESSED, rather than pressed and undone. Back from an app's
        # first page pops Android's task stack into whatever was beneath —
        # the launcher, or another care app — and the portal's answer until
        # now was to notice afterwards and put her back, which works and
        # flickers a foreign screen at her on the way. The app publishes its
        # own fragment back stack; an empty one says this press would leave
        # before it is sent. Leaving an app is Home's job.
        log.info("back refused: the app is on its own first page")
        return {"type": "device_result", "ok": False, "rooted": True}
    try:
        # adb round trip. On the event loop it would stall every viewer's
        # frames, the same reason a tap does not run there.
        await asyncio.to_thread(send_ui_action, action)
    except DeviceUnavailable as exc:
        log.warning("device action refused: %s", exc)
        return {"type": "device_result", "ok": False}
    log.info("device action %s sent over the socket", action)

    # Back and Home change the screen; wake the hierarchy watcher so the
    # wireframe follows without waiting out its interval.
    try:
        import time as _time

        from apt_log.feed import POKE_NAME

        (state_mod.STATE_DIR / POKE_NAME).write_text(str(_time.time()),
                                                     encoding="utf-8")
    except OSError:
        pass
    return {"type": "device_result", "ok": True}


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


@app.post("/sign")
async def sign_current_screen(request: Request):
    """Accept strokes drawn on the portal's pad, for replay onto the app.

    The other half of the signature story. /signature answers a relay request
    the controller opened; this one is hers to start — she has driven the app
    to its own signature screen through the portal and needs ink to land on a
    canvas she cannot physically touch. Drawing through the mirror was tried
    on a real clock-out and is not drawable: every stroke crosses the network
    twice before the ink appears.

    The strokes are validated for shape here and handed to the feed process,
    which replays them onto exactly one signature-canvas element or refuses
    (see sign.py for the whole argument). Nothing here presses the app's save
    button — that stays her tap.
    """
    from apt_log import sign as sign_mod

    payload = await request.json()
    strokes = payload.get("strokes")
    if not sign_mod.validate(strokes):
        return JSONResponse({"error": "empty"}, status_code=400)
    try:
        aspect = float(payload.get("aspect", 1.0))
    except (TypeError, ValueError):
        aspect = 1.0
    rid = sign_mod.request(strokes, aspect=aspect)
    log.info("signature replay queued (%s)", rid)
    return JSONResponse({"ok": True, "id": rid})


@app.post("/sign-action")
async def sign_action(request: Request):
    """Press the app's own clear or save on the signature screen.

    The signature pages draw Borrar and Salvar in the same rotated pass
    that hides their captions — the tree holds no trace of either, so the
    portal cannot aim at them as elements. This is her tap on a screen she
    is looking at, relayed: the runner presses at a spot derived from the
    one thing the finder does see, the canvas, and refuses anywhere that
    is not a signature moment. The replay itself still never commits.
    """
    from apt_log import sign as sign_mod

    payload = await request.json()
    kind = payload.get("kind")
    if kind not in sign_mod.ACTIONS:
        return JSONResponse({"error": "unknown"}, status_code=400)
    rid = sign_mod.request_action(kind)
    log.info("signature %s queued (%s)", kind, rid)
    return JSONResponse({"ok": True, "id": rid})


@app.post("/code/broadcast")
def broadcast_code():
    """Text the newest sign-in code to everyone on the list, on her ask.

    The OTP lands on the phone holding the SIM. She signs in on her own
    phone, which never sees it, and the standing forwarder cannot help her
    once it has already marked that message passed on. This is the button for
    the moment she is actually in: at a login screen, code somewhere, no text.

    **The digits are not in the answer.** They go to the phones and nowhere
    else — a response carrying the code would put an OTP in a browser tab, in
    this process's log, and in the next screenshot of the portal. What comes
    back is how many were sent, of how many, and how old the code was.
    """
    from apt_log import sms as sms_mod

    try:
        out = sms_mod.broadcast_latest()
    except Exception as exc:  # noqa: BLE001 — a phone that will not answer
        log.warning("could not broadcast the code (%s)", exc)
        return JSONResponse({"error": "unreachable"}, status_code=502)
    return JSONResponse(out)


@app.get("/code/latest")
def latest_code_on_the_page():
    """The newest sign-in code, its age, and the minute it arrived.

    THE FAIL-SAFE FOR A TEXT THAT NEVER COMES. The broadcast is the first
    answer and it is not a complete one: a text can be filtered as smishing
    (a one-time code from an ordinary mobile number is the textbook shape),
    blocked, or simply not delivered to a phone with no service. Seen live —
    the handset logged `SEND_SMS status = 1 (Ok)` for all three recipients
    and one of them never received a thing.

    So the code is on the page too, where somebody on the tailnet can read
    it. That reverses the line `/code/broadcast` holds, deliberately: the
    reasoning there was that a code in a response is a code in a log, and the
    log half is the real risk — so `latest_for_display` writes the age to the
    journal and never the digits. The page itself is behind the same door as
    the patients' names and the ability to record a visit; a number that dies
    in fifteen minutes is not what makes it worth protecting.

    ALWAYS WITH ITS AGE. A code shown bare is a code somebody types at nine
    minutes old and blames the machine for. The minute is rendered in the
    schedule's own zone — Eastern — because the people reading this page are
    not all in it.
    """
    from apt_log import sms as sms_mod

    try:
        out = sms_mod.latest_for_display()
    except Exception as exc:  # noqa: BLE001 — a phone that will not answer
        log.warning("could not read the code for the page (%s)", exc)
        return JSONResponse({"error": "unreachable"}, status_code=502)

    if out.get("found"):
        zone = _schedule_zone()
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            when = datetime.fromtimestamp(out.pop("at"),
                                          ZoneInfo(zone) if zone else None)
            out["said"] = _clock(when, _zone_says(ZoneInfo(zone)) if zone else "")
        except Exception:  # noqa: BLE001 — a clock is not worth a 500
            out.pop("at", None)
            out["said"] = ""
    return JSONResponse(out)


@app.get("/signature/roster")
def signature_roster():
    """Who has adopted a signature. NEVER the signatures themselves.

    `enrolled.roster` is built to answer exactly this and to hold nothing
    re-stampable; this route must not reach past it into the store. That is
    the whole reason the two functions are separate.
    """
    from apt_log import enrolled as enrolled_mod

    return JSONResponse({"parties": enrolled_mod.roster()})


def _app_called(app_ref: str) -> str:
    """What the tiles call this app. The reference itself if nothing does.

    The schedule names an app however its author found convenient — a package
    on one line, a tile id on another — and neither is a thing to show a
    caregiver. Falling back to the raw reference rather than to "" keeps a
    schedule entry for an app this build does not know legible instead of
    blank.
    """
    for tile in PHONE_APPS + (RETIRED_TILE,):
        if app_ref in (tile["package"], tile["id"]):
            return tile["name"]
    return app_ref


@app.get("/signature/map")
def signature_map():
    """Everybody who will be asked to sign, and whether they have adopted one.

    THE ROSTER ALONE CANNOT ANSWER THIS. It lists who HAS adopted a signature,
    which is the wrong half of the question: the useful view is who is going
    to be asked — the patients on the schedule — and which of them still have
    nothing on file. A list that only shows the ones already done is a list
    that can never tell you what is left.

    So the schedule supplies the names and the store supplies the state, and
    a party in the store that is not on the schedule still appears: an
    adoption is a record, and one that stopped matching a schedule entry must
    not quietly vanish from the only screen that shows it.

    NO STROKES, by the same rule the roster follows — `enrolled.roster` is
    built to hold nothing re-stampable and this route must not reach past it.
    """
    from apt_log import enrolled as enrolled_mod
    from apt_log import schedule as sched

    roster = enrolled_mod.roster()
    by_name = {e["name"]: e for e in roster}

    people: list[dict] = []
    seen: set = set()
    try:
        plan = sched.load()
        blocks = plan.blocks
    except Exception:  # noqa: BLE001 — an unreadable schedule is not fatal
        # The store still has something to show, and saying nothing at all
        # here would read as "nobody has adopted a signature".
        blocks = []

    for name in [b.patient for b in blocks]:
        if name in seen:
            continue
        seen.add(name)
        # Which apps will put this person in front of a pad. Named rather
        # than counted: "she signs in two apps" is the fact that decides
        # whether one adoption is enough — and named the way the tiles name
        # them, because "com.hhaexchange.uma" on a caregiver's screen is not
        # an app name, it is a package identifier she has no use for.
        apps = sorted({_app_called(b.app) for b in blocks
                       if b.patient == name})
        # The store's own spelling wins for the button, because that is what
        # `who_signs` will hand the pad at the moment it matters.
        match = enrolled_mod.who_signs(name)
        entry = by_name.get(match, {})
        people.append({"name": name, "apps": apps,
                       "adopted": bool(match), "adopted_as": match,
                       "witness": entry.get("witness", ""),
                       "at": entry.get("at", ""),
                       "digest": entry.get("digest", ""),
                       "on_schedule": True})

    # Adoptions that match nobody on the schedule. Last, and marked, because
    # they are the exception rather than the working list.
    claimed = {p["adopted_as"] for p in people if p["adopted_as"]}
    for entry in roster:
        if entry["name"] in claimed:
            continue
        people.append({"name": entry["name"], "apps": [],
                       "adopted": True, "adopted_as": entry["name"],
                       "witness": entry.get("witness", ""),
                       "at": entry.get("at", ""),
                       "digest": entry.get("digest", ""),
                       "on_schedule": False})

    return JSONResponse({"people": people})


@app.post("/signature/enroll")
async def signature_enroll(request: Request):
    """Adopt a signature for one party, in person.

    Drawn once by its owner on the portal's own pad. It is the moment the
    agency's approval actually rests on, and it happens with both people
    sitting down, not against a clock.

    `witness` is accepted and no longer required — the field asking for it
    came off the sheet, and the store keeps whatever it is given so adoptions
    made before that keep saying what they said. See REQ-10.6a.
    """
    from apt_log import enrolled as enrolled_mod

    payload = await request.json()
    try:
        digest = enrolled_mod.enroll(payload.get("name", ""),
                                     payload.get("strokes"),
                                     aspect=float(payload.get("aspect") or 1.0),
                                     witness=payload.get("witness", ""))
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except OSError as exc:
        # A STORE THAT CANNOT BE WRITTEN IS NOT A BAD REQUEST AND IT IS NOT
        # THE PHONE.
        #
        # This went uncaught, so the route answered 500 and the page toasted
        # "That didn't reach the phone" — a sentence about a handset, over a
        # fault that was entirely the Pi's own disk. Watched live: the first
        # real registration died on `PermissionError:
        # /etc/aptlog/signatures.tmp` and the caregiver was told to look at
        # the phone.
        #
        # Its own status and its own sentence, and the reason in the log where
        # somebody can act on it.
        log.warning("could not write the signature store (%s)", exc)
        return JSONResponse({"error": "store_unwritable"}, status_code=507)
    return JSONResponse({"ok": True, "digest": digest[:12]})


@app.post("/signature/forget")
async def signature_forget(request: Request):
    """Withdraw an adoption. Hers to withdraw at any time, and the reason the
    roster shows the date: an adoption nobody remembers making is one to drop."""
    from apt_log import enrolled as enrolled_mod

    payload = await request.json()
    return JSONResponse({"ok": enrolled_mod.forget(payload.get("name", ""))})


@app.post("/signature/apply")
async def signature_apply(request: Request):
    """Draw an adopted signature onto the canvas in front of the phone.

    THE PRESS IS THE POINT. This route exists to be called by a person who has
    just touched a button, standing next to the phone, at the moment they are
    attesting to a visit. It is not reachable from the scheduler and it never
    will be — `autoentry` cannot import `enrolled` and a test enforces it.

    What crosses the wire is a NAME. The strokes are looked up here and handed
    straight to the same replay every hand-drawn signature uses, so this route
    can be used to put somebody's signature on the screen in front of them and
    cannot be used to obtain it.

    And as everywhere else on this path: it draws, it does not commit. The
    app's own Enviar stays a separate press.
    """
    from apt_log import enrolled as enrolled_mod
    from apt_log import sign as sign_mod

    payload = await request.json()
    name = payload.get("name", "")
    found = enrolled_mod.strokes_for(name)
    if found is None:
        return JSONResponse({"error": "not_enrolled"}, status_code=404)
    strokes, aspect = found
    rid = sign_mod.request(strokes, aspect=aspect)
    enrolled_mod.record_use(name, enrolled_mod.digest_for(name),
                            package=str(payload.get("package") or ""))
    log.info("adopted signature queued for replay (%s)", rid)
    return JSONResponse({"ok": True, "id": rid})


@app.post("/relay")
def submit_relay(request: Request,
                 nonce: str = Form(...), kind: str = Form(...),
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
        return RedirectResponse(url=_back_to(request) + "?relay=expired",
                                status_code=303)
    except RelayError as exc:
        # The reason is deliberately not echoed into the URL: for a token it
        # would be describing a credential.
        log.warning("relay refused a %s: %s", kind, exc)
        return RedirectResponse(url=_back_to(request) + "?relay=refused",
                                status_code=303)

    log.info("relay accepted a %s (sha256 %s…)", kind, digest[:8])
    return RedirectResponse(url=_back_to(request) + "?relay=sent",
                            status_code=303)


@app.post("/device")
def device_action(request: Request, action: str = Form(...)):
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
        return RedirectResponse(url=_back_to(request) + "?device=failed",
                                status_code=303)
    log.info("device action %s sent from the UI", action)
    return RedirectResponse(url=_back_to(request) + "?device=sent",
                            status_code=303)


@app.post("/macro")
def start_macro(request: Request, name: str = Form(...),
                arg: str = Form("")):
    """Ask the feed process to run a named sequence.

    A name from a list, never steps. The list lives in apt_log.macros and the
    page is handed it; a route that accepted a sequence from a browser would be
    arbitrary remote scripting with a friendlier label, and "the portal cannot
    do anything she did not ask for" would become "the client is well-behaved".

    `arg` does not widen that. It reaches only macros that declared they take
    one — `macros.request` drops it for every other — and the single macro
    that does uses it to choose between provider rows the app is already
    drawing. It cannot name a control that is not on screen.
    """
    from apt_log import macros

    try:
        macros.request(name, arg=arg)
    except KeyError:
        log.warning("unknown macro requested: %r", name)
        return RedirectResponse(url=_back_to(request) + "?macro=unknown",
                                status_code=303)
    return RedirectResponse(url=_back_to(request) + "?macro=started",
                            status_code=303)


@app.post("/acknowledge")
def acknowledge(request: Request, attempt_id: str = Form(...)):
    state_mod.acknowledge(attempt_id)
    return RedirectResponse(url=_back_to(request), status_code=303)


@app.post("/control")
def control(request: Request, action: str = Form(...)):
    if action not in ("pause", "resume"):
        return Response(status_code=400)
    state_mod.set_paused(action == "pause")
    log.warning("scheduler %sd from the UI", action)
    return RedirectResponse(url=_back_to(request, fallback="/console"),
                            status_code=303)
