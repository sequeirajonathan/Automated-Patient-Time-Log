"""The mirror frame.

Two properties carry the file: nothing free-form gets in, and an old frame
admits to being old.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from apt_log.ui import mirror as mirror_mod
from apt_log.ui.mirror import Mirror, publish, read


class TestVocabulary:
    def test_a_known_screen_and_step_survive(self, tmp_path):
        frame = publish("verification", "waiting", "PT-0042",
                        path=tmp_path / "mirror.json")
        assert frame.screen == "verification"
        assert frame.step == "waiting"

    def test_an_unknown_screen_becomes_unknown_rather_than_being_carried(self, tmp_path):
        """The allow-list is the whole safety property.

        A screen name passed straight through is a channel for whatever text the
        caller happened to have, and on the visit screen that text includes a
        patient's name.
        """
        frame = publish("Detalle de Visita", "working", path=tmp_path / "m.json")
        assert frame.screen == "unknown"

    def test_an_unknown_step_becomes_unknown(self, tmp_path):
        assert publish("home", "thinking", path=tmp_path / "m.json").step == "unknown"


class TestPatientIdentifier:
    def test_an_identifier_is_kept(self, tmp_path):
        frame = publish("today", "working", "PT-0042", path=tmp_path / "m.json")
        assert frame.patient_id == "PT-0042"

    def test_a_name_is_refused_because_it_is_not_id_shaped(self, tmp_path):
        """REQUIREMENTS §5 — a name never reaches the record, and this file is
        part of the record's surface. The guard has to hold against a caller
        passing the wrong variable, not only a careless one."""
        frame = publish("today", "working", "CARIDAD ROJAS", path=tmp_path / "m.json")
        assert frame.patient_id == "?"
        assert "CARIDAD" not in (tmp_path / "m.json").read_text(encoding="utf-8")

    def test_the_reader_re_checks_what_is_on_disk(self, tmp_path):
        """The file lives between two processes; the reader should not trust it
        more than the writer did."""
        path = tmp_path / "m.json"
        path.write_text(json.dumps({
            "at": datetime.now().isoformat(),
            "screen": "el detalle de la visita",
            "step": "waiting",
            "patient_id": "Alice Example",
        }), encoding="utf-8")
        frame = read(path)
        assert frame.screen == "unknown"
        assert frame.patient_id == "?"


class TestStaleness:
    def test_a_fresh_frame_is_not_stale(self):
        assert Mirror(at=datetime.now()).stale is False

    def test_an_old_frame_says_so(self):
        old = datetime.now() - mirror_mod.STALE_AFTER - timedelta(seconds=1)
        assert Mirror(at=old).stale is True

    def test_a_frame_that_was_never_published_is_stale(self):
        """Not "the controller is on the startup screen" — not knowing is its
        own answer, and a confident wrong one is worse."""
        assert Mirror().stale is True
        assert Mirror().screen == "unknown"


class TestRoundTrip:
    def test_what_is_published_is_what_is_read(self, tmp_path):
        path = tmp_path / "m.json"
        scheduled = datetime(2026, 8, 14, 20, 0)
        publish("visit", "waiting", "PT-0042", scheduled, path=path)
        frame = read(path)
        assert (frame.screen, frame.step, frame.patient_id) == \
               ("visit", "waiting", "PT-0042")
        assert frame.scheduled == scheduled
        assert frame.waiting is True

    def test_a_missing_file_reads_as_unknown_rather_than_raising(self, tmp_path):
        frame = read(tmp_path / "nothing.json")
        assert frame.at is None and frame.screen == "unknown"

    def test_a_corrupt_file_reads_as_unknown_rather_than_raising(self, tmp_path):
        path = tmp_path / "m.json"
        path.write_text("{ this is not json", encoding="utf-8")
        assert read(path).screen == "unknown"

    def test_no_partial_frame_is_left_behind(self, tmp_path):
        """The page polls this on a timer, so it will eventually read during a
        write unless the write is atomic."""
        path = tmp_path / "m.json"
        publish("home", "working", path=path)
        assert json.loads(path.read_text(encoding="utf-8"))["screen"] == "home"
        assert not (tmp_path / "m.tmp").exists()
