"""REQ-7 — the audit record.

`transport_mode` and `location_source_type` are positional and have no defaults.
That is deliberate: REQ-7 says a record missing either is not a valid record, so
the type makes one impossible to construct rather than trusting every future call
site to remember. The distinction between an observed check-off and one produced
under a development affordance cannot be reconstructed after the fact — if it is
not written at the time, it is gone.

`scheduled_time_utc` and `observed_time_utc` are separate fields and are never
collapsed (§1.1). A scheduled job records when a visit was *supposed* to happen,
not when it did.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from apt_log.transport import TransportMode

AUDIT_PATH = Path("/var/lib/aptlog/audit.jsonl")


@dataclass(frozen=True)
class GateSignals:
    """REQ-5.6 — individual signal results, not just the verdict.

    A later reader needs to see *why* a check-off was allowed, which a single
    boolean cannot carry.
    """

    usb_transport: bool
    gateway_mac_match: bool | None = None
    bssid_match: bool | None = None
    wan_ip_match: bool | None = None
    fix: dict[str, Any] | None = None


@dataclass(frozen=True)
class SignatureMeta:
    """REQ-10.11 — proof a distinct signature happened, without a reusable copy.

    Never holds strokes or a bitmap. The hash is what distinguishes one signing
    from another; keeping the strokes would be keeping something re-stampable.
    """

    occurred: bool
    nonce: str | None = None
    stroke_count: int | None = None
    duration_ms: int | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class AuditRecord:
    attempt_id: str
    patient_id: str
    scheduled_time_utc: datetime
    observed_time_utc: datetime
    gate_result: bool
    # No defaults. REQ-7: a record missing either of these is not a record.
    transport_mode: TransportMode
    location_source_type: str
    signals: GateSignals
    action_taken: str
    ui_verification_result: str | None = None
    signature: SignatureMeta | None = None
    error: str | None = None
    app_version: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.location_source_type:
            raise ValueError(
                "location_source_type is mandatory (REQ-7) — a record that cannot "
                "distinguish an observed fix from a stub is not a valid record"
            )

    def to_json(self) -> str:
        payload = asdict(self)
        payload["transport_mode"] = self.transport_mode.value
        payload["scheduled_time_utc"] = self.scheduled_time_utc.isoformat()
        payload["observed_time_utc"] = self.observed_time_utc.isoformat()
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def append(record: AuditRecord, path: Path = AUDIT_PATH) -> None:
    """Append one record. The log is append-only; nothing here rewrites history."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record.to_json() + "\n")
