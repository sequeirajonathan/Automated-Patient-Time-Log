"""Named sequences she can start with one button.

This is the automation that survived. What got abandoned was a machine deciding
*when* to record a visit; what is left is a machine doing the tedious parts *when
she asks*, on a screen she is watching. Nothing here fires on a timer and nothing
here decides anything.

**Macros navigate and fill. They do not commit.**

Signing in is twelve taps and a password through a mirrored screen, with no
judgement anywhere in it — exactly what a button is for. Clocking in is one tap
that produces a record and, if it is wrong, a phone call from the agency to her.
That one stays hers, on the real screen, with her looking at it. The line is not
about difficulty; it is about which mistakes are recoverable.

**The page can only ask for a name.** Macros live here, in code, and the UI is
handed a list. It cannot post steps. A macro that accepted a sequence from a
browser would be arbitrary remote scripting with a friendlier label, and the
argument that the portal cannot do anything she did not ask for would collapse
into "the client is well-behaved".

They run in the feed process because that is where the resident Appium session
lives. The UI writes a request file and reads a status file; a second session in
the web process is not available at any price, as fourteen seconds a tap
demonstrated.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

STATE_DIR = Path("/var/lib/aptlog")
REQUEST_PATH = STATE_DIR / "macro-request.json"
STATUS_PATH = STATE_DIR / "macro-status.json"

# A request older than this is ignored rather than run. The feed can be down when
# a button is pressed, and a sign-in that fires when the process comes back
# minutes later is a surprise on a phone nobody is holding.
REQUEST_MAX_AGE = 60.0

POLL_EVERY = 1.0


@dataclass
class Macro:
    name: str
    label_key: str          # i18n key; the UI never sees English from here
    run: object             # callable(driver, report) -> None


@dataclass
class Status:
    id: str = ""
    name: str = ""
    state: str = "idle"      # idle | running | done | failed
    step: str = ""           # i18n key for what it is doing
    error: str = ""
    at: str = field(default_factory=lambda: datetime.now().isoformat())


# --------------------------------------------------------------------- macros
def _hhax_legacy_login(driver, report) -> None:
    """Cold app to the agency's home screen.

    Every step here was proven end to end against the real account before it was
    ever a button: startup gates, credentials, agency selection, home.
    """
    from apt_log import config
    from apt_log.screens import agency as agency_mod
    from apt_log.screens import home as home_mod
    from apt_log.screens import login as login_mod
    from apt_log.screens import session as session_mod
    from apt_log.screens.language import advance_past_startup_gates
    from apt_log.secrets import FileSecretProvider

    report("macro.step.launching")
    driver.activate_app("com.hhaexchange.caregiver")

    report("macro.step.clearing")
    message = session_mod.modal_message(driver)
    if message:
        if not session_mod.is_expired(driver):
            # Refusing to dismiss what nothing has read is the same rule the
            # session module holds, and a macro is not an excuse to break it.
            raise RuntimeError("an unrecognised dialog is on screen")
        session_mod.dismiss(driver)
    for gate in advance_past_startup_gates(driver, language="es"):
        log.info("macro cleared startup gate: %s", gate)

    report("macro.step.signing_in")
    login_mod.authenticate_if_needed(driver, FileSecretProvider())

    report("macro.step.agency")
    screen = agency_mod.AgencyScreen(driver)
    if screen.is_displayed():
        screen.select(config.get("AGENCY_NAME"))

    report("macro.step.checking")
    if not home_mod.HomeScreen(driver).is_displayed():
        raise RuntimeError("did not reach the home screen")


MACROS: dict[str, Macro] = {
    m.name: m for m in (
        Macro("hhax_legacy_login", "macro.hhax_legacy_login", _hhax_legacy_login),
    )
}


# -------------------------------------------------------------------- request
def request(name: str, path: Path | None = None) -> str:
    """Ask for a macro by name. Returns the request id."""
    if name not in MACROS:
        raise KeyError(name)
    target = path or REQUEST_PATH
    rid = uuid.uuid4().hex[:12]
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"id": rid, "name": name, "at": time.time()}),
                   encoding="utf-8")
    os.replace(tmp, target)
    log.info("macro requested: %s (%s)", name, rid)
    return rid


def take_request(path: Path | None = None) -> dict | None:
    """Claim a pending request, removing it. None when there is nothing to do."""
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
        log.info("ignoring a stale macro request (%s)", payload.get("name"))
        return None
    if payload.get("name") not in MACROS:
        return None
    return payload


# --------------------------------------------------------------------- status
def write_status(status: Status, path: Path | None = None) -> None:
    target = path or STATUS_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(status.__dict__), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("cannot publish macro status (%s)", exc)


def read_status(path: Path | None = None) -> Status:
    target = path or STATUS_PATH
    try:
        return Status(**json.loads(target.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError):
        return Status()


# --------------------------------------------------------------------- runner
class Runner:
    """Polls for a request and runs it on the resident session."""

    def __init__(self, request_path: Path | None = None,
                 status_path: Path | None = None):
        self._request_path = request_path
        self._status_path = status_path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="aptlog-macros")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                pending = take_request(self._request_path)
                if pending is not None:
                    self.execute(pending["name"], pending["id"])
            except Exception as exc:  # noqa: BLE001
                log.warning("macro runner: %s", exc)
            self._stop.wait(POLL_EVERY)

    def execute(self, name: str, rid: str) -> Status:
        from apt_log import resident
        from apt_log.ui import mirror as mirror_mod

        macro = MACROS[name]
        status = Status(id=rid, name=name, state="running",
                        step="macro.step.starting")
        write_status(status, self._status_path)

        def report(step_key: str) -> None:
            status.step = step_key
            status.at = datetime.now().isoformat()
            write_status(status, self._status_path)
            # The mirror already says "stopped, waiting for your answer" and
            # friends; a macro is the one time it can honestly say "working".
            mirror_mod.publish(screen="unknown", step="working")

        try:
            resident.run(lambda driver: macro.run(driver, report))
        except Exception as exc:  # noqa: BLE001
            log.warning("macro %s failed: %s", name, exc)
            status.state = "failed"
            # The message is shown to her, so it must not carry a credential or
            # a name. Macro failures are structural -- a missing screen, a dialog
            # nobody recognised -- so the type is the useful part.
            status.error = type(exc).__name__
            status.at = datetime.now().isoformat()
            write_status(status, self._status_path)
            return status

        status.state = "done"
        status.step = "macro.step.finished"
        status.at = datetime.now().isoformat()
        write_status(status, self._status_path)
        log.info("macro %s finished", name)
        return status
