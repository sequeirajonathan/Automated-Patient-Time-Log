"""System state for the dashboard.

Every reader here degrades to "unknown" rather than raising. The panel exists so
the caregiver can tell whether the system is working from four floors away; a
page that 500s because one probe failed tells her nothing, and she cannot fix it
either way.

"Unknown" is deliberately distinct from "bad". Not knowing whether the phone is
attached is a different situation from knowing it is not, and collapsing them
would make a network hiccup look like a hardware failure.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from apt_log.ui.mirror import Mirror
from apt_log.ui import mirror as mirror_mod

log = logging.getLogger(__name__)

STATE_DIR = Path("/var/lib/aptlog")
AUDIT_PATH = STATE_DIR / "audit.jsonl"
PAUSED_FLAG = STATE_DIR / "paused"
ACK_PATH = STATE_DIR / "acknowledged.json"
SCREENSHOT_PATH = STATE_DIR / "last-screen.jpg"
HEARTBEAT_PATH = STATE_DIR / "last-heartbeat"

UNITS = ("aptlog-agent", "aptlog-appium", "aptlog-ui")
HEARTBEAT_STALE_AFTER = timedelta(minutes=15)


class Health(StrEnum):
    OK = "ok"
    BAD = "bad"
    UNKNOWN = "unknown"


@dataclass
class Signal:
    key: str          # i18n key for the label
    status: Health
    detail: str = ""


@dataclass
class Visit:
    patient_id: str
    scheduled: datetime | None
    observed: datetime | None
    status: str                      # pending | done | skipped | failed
    gate_passed: bool | None = None
    reason: str = ""
    attempt_id: str = ""

    @property
    def divergence_minutes(self) -> int | None:
        if self.scheduled is None or self.observed is None:
            return None
        return round((self.observed - self.scheduled).total_seconds() / 60)


@dataclass
class DashboardState:
    health: list[Signal] = field(default_factory=list)
    gate: list[Signal] = field(default_factory=list)
    visits: list[Visit] = field(default_factory=list)
    attention: list[Visit] = field(default_factory=list)
    transport_mode: str = "unknown"
    paused: bool = False
    heartbeat: datetime | None = None
    screenshot_at: datetime | None = None
    mirror: Mirror = field(default_factory=Mirror)
    generated_at: datetime = field(default_factory=datetime.now)

    @property
    def overall(self) -> Health:
        if any(s.status is Health.BAD for s in self.health):
            return Health.BAD
        if any(s.status is Health.UNKNOWN for s in self.health):
            return Health.UNKNOWN
        return Health.OK


# ------------------------------------------------------------------- readers


def _unit_active(unit: str) -> Health:
    try:
        out = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return Health.UNKNOWN
    status = out.stdout.strip()
    if status == "active":
        return Health.OK
    if status in ("activating", "reloading"):
        return Health.UNKNOWN
    return Health.BAD


def _phone_attached() -> tuple[Health, str]:
    try:
        from apt_log.transport import attached_devices
        serials, mode = attached_devices()
    except Exception as exc:  # noqa: BLE001
        log.debug("cannot read devices: %s", exc)
        return Health.UNKNOWN, ""
    if serials:
        return Health.OK, mode.value
    return Health.BAD, mode.value


def _transport_mode() -> str:
    try:
        from apt_log.transport import resolve_transport_mode
        return resolve_transport_mode().value
    except Exception:  # noqa: BLE001
        return "unknown"


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def read_audit(path: Path = AUDIT_PATH, limit: int = 200) -> list[Visit]:
    """Most recent attempts, newest last. A malformed line is skipped, not fatal."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []

    visits: list[Visit] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            log.warning("skipping malformed audit line")
            continue

        def _dt(key):
            raw = rec.get(key)
            try:
                return datetime.fromisoformat(raw) if raw else None
            except (TypeError, ValueError):
                return None

        action = rec.get("action_taken", "")
        gate = rec.get("gate_result")
        if action == "checked_off":
            status = "done"
        elif gate is False:
            status = "skipped"
        elif rec.get("error"):
            status = "failed"
        else:
            status = "pending"

        visits.append(Visit(
            patient_id=rec.get("patient_id", "?"),
            scheduled=_dt("scheduled_time_utc"),
            observed=_dt("observed_time_utc"),
            status=status,
            gate_passed=gate,
            reason=rec.get("error") or "",
            attempt_id=rec.get("attempt_id", ""),
        ))
    return visits


def _acknowledged() -> set[str]:
    try:
        return set(json.loads(ACK_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()


def acknowledge(attempt_id: str) -> None:
    acked = _acknowledged()
    acked.add(attempt_id)
    ACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACK_PATH.write_text(json.dumps(sorted(acked)), encoding="utf-8")


def is_paused() -> bool:
    return PAUSED_FLAG.exists()


def set_paused(paused: bool) -> None:
    PAUSED_FLAG.parent.mkdir(parents=True, exist_ok=True)
    if paused:
        PAUSED_FLAG.touch()
    elif PAUSED_FLAG.exists():
        PAUSED_FLAG.unlink()


def collect() -> DashboardState:
    phone_health, mode = _phone_attached()
    heartbeat = _mtime(HEARTBEAT_PATH)

    if heartbeat is None:
        hb_health = Health.UNKNOWN
    elif datetime.now() - heartbeat > HEARTBEAT_STALE_AFTER:
        hb_health = Health.BAD
    else:
        hb_health = Health.OK

    # Two signals used to sit here and no longer do. "Scheduler" reported a
    # unit whose job — deciding on its own when to record a visit — was
    # abandoned; it is a service like any other now, and the control centre
    # lists it as one. "Last check-in" reported the heartbeat file, which
    # answers "is the controller alive" a second time and less directly than
    # the three below. The heartbeat is still written and still read by
    # heartbeat.sh; it is the PANEL that was noise.
    health = [
        Signal("health.controller", Health.OK),   # this page rendered, so it is up
        Signal("health.phone", phone_health),
        Signal("health.appium", _unit_active("aptlog-appium")),
    ]

    visits = read_audit()
    acked = _acknowledged()
    attention = [
        v for v in visits
        if v.status in ("failed", "skipped") and v.attempt_id not in acked
    ]

    latest = visits[-1] if visits else None
    gate: list[Signal] = []
    if latest is not None:
        gate = [
            Signal("gate.usb", Health.OK if latest.gate_passed else Health.BAD),
        ]

    return DashboardState(
        health=health,
        gate=gate,
        visits=visits,
        attention=attention,
        transport_mode=mode or _transport_mode(),
        paused=is_paused(),
        heartbeat=heartbeat,
        screenshot_at=_mtime(SCREENSHOT_PATH),
        mirror=mirror_mod.read(),
    )
