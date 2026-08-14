"""REQ-7 — the fields that make a record a record."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from apt_log.audit import AuditRecord, GateSignals, SignatureMeta, append
from apt_log.transport import TransportMode


def _record(**overrides):
    base = dict(
        attempt_id="a1",
        patient_id="PT-0042",          # identifier, never a name (§5)
        scheduled_time_utc=datetime(2026, 8, 14, 14, 0, tzinfo=UTC),
        observed_time_utc=datetime(2026, 8, 14, 14, 7, tzinfo=UTC),
        gate_result=True,
        transport_mode=TransportMode.DEV,
        location_source_type="StubLocationSource",
        signals=GateSignals(usb_transport=False, bssid_match=True),
        action_taken="checked_off",
    )
    base.update(overrides)
    return AuditRecord(**base)


class TestMandatoryProvenanceFields:
    def test_transport_mode_cannot_be_omitted(self):
        with pytest.raises(TypeError):
            AuditRecord(  # type: ignore[call-arg]
                attempt_id="a1",
                patient_id="PT-0042",
                scheduled_time_utc=datetime.now(UTC),
                observed_time_utc=datetime.now(UTC),
                gate_result=True,
                location_source_type="StubLocationSource",
                signals=GateSignals(usb_transport=False),
                action_taken="checked_off",
            )

    def test_location_source_type_cannot_be_omitted(self):
        with pytest.raises(TypeError):
            AuditRecord(  # type: ignore[call-arg]
                attempt_id="a1",
                patient_id="PT-0042",
                scheduled_time_utc=datetime.now(UTC),
                observed_time_utc=datetime.now(UTC),
                gate_result=True,
                transport_mode=TransportMode.USB,
                signals=GateSignals(usb_transport=True),
                action_taken="checked_off",
            )

    def test_location_source_type_cannot_be_blank(self):
        with pytest.raises(ValueError, match="location_source_type"):
            _record(location_source_type="")

    def test_both_survive_serialisation(self):
        payload = json.loads(_record().to_json())
        assert payload["transport_mode"] == "dev"
        assert payload["location_source_type"] == "StubLocationSource"


class TestTimesStaySeparate:
    def test_scheduled_and_observed_are_distinct_fields(self):
        payload = json.loads(_record().to_json())
        assert payload["scheduled_time_utc"] != payload["observed_time_utc"]
        assert payload["scheduled_time_utc"].startswith("2026-08-14T14:00")
        assert payload["observed_time_utc"].startswith("2026-08-14T14:07")


class TestSignalsAndSignature:
    def test_individual_signals_are_recorded_not_just_the_verdict(self):
        payload = json.loads(_record().to_json())
        assert payload["signals"]["usb_transport"] is False
        assert payload["signals"]["bssid_match"] is True
        assert payload["gate_result"] is True

    def test_signature_carries_a_hash_and_no_strokes(self):
        rec = _record(
            signature=SignatureMeta(
                occurred=True, nonce="n1", stroke_count=3,
                duration_ms=1400, sha256="abc123",
            )
        )
        payload = json.loads(rec.to_json())
        assert payload["signature"]["sha256"] == "abc123"
        blob = rec.to_json().lower()
        assert "stroke" not in blob.replace("stroke_count", "")
        assert "bitmap" not in blob


class TestAppend:
    def test_appends_one_line_per_record(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        append(_record(attempt_id="a1"), path=path)
        append(_record(attempt_id="a2"), path=path)
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["attempt_id"] == "a1"
        assert json.loads(lines[1])["attempt_id"] == "a2"
