"""The flight recorder: structure, never words.

Its one hard property is the disclosure rule — this file is meant to be read,
pasted and shared freely during tuning, so a name must not be able to reach
it. Lengths survive because layout is made of lengths.
"""

from __future__ import annotations

import json

from apt_log import flight


def doc(sid="s1", app="com.x", statics=None, elements=None):
    return {
        "id": sid, "at": "2026-08-15T12:00:00", "app": app, "screen": "visit",
        "blocked": "", "size": [720, 1600],
        "elements": elements if elements is not None else [
            {"rid": "btn_go", "cls": "Button", "b": [0, 0, 100, 50],
             "txt": "Registrar Salida", "checked": False, "focused": False}],
        "statics": statics if statics is not None else [
            {"cls": "TextView", "b": [0, 60, 720, 120],
             "txt": "PACIENTE FICTICIA EJEMPLO"}],
    }


class TestDisclosure:
    def test_no_words_survive(self, tmp_path):
        target = tmp_path / "flight.jsonl"
        flight.record(doc(), target, seen=set())
        raw = target.read_text()
        for word in ("PACIENTE", "FICTICIA", "Registrar", "Salida"):
            assert word not in raw

    def test_lengths_survive_because_layout_is_made_of_them(self, tmp_path):
        target = tmp_path / "flight.jsonl"
        flight.record(doc(), target, seen=set())
        entry = flight.entries(target)[0]
        assert entry["statics"][0]["len"] == len("PACIENTE FICTICIA EJEMPLO")
        assert entry["elements"][0]["len"] == len("Registrar Salida")


class TestRecording:
    def test_each_structure_is_recorded_once(self, tmp_path):
        target = tmp_path / "flight.jsonl"
        seen = set()
        assert flight.record(doc("a"), target, seen) is True
        assert flight.record(doc("a"), target, seen) is False
        assert flight.record(doc("b"), target, seen) is True
        assert len(flight.entries(target)) == 2

    def test_an_empty_screen_is_not_worth_recording(self, tmp_path):
        target = tmp_path / "flight.jsonl"
        assert flight.record(doc("e", statics=[], elements=[]),
                             target, set()) is False

    def test_the_file_is_capped_newest_kept(self, tmp_path):
        target = tmp_path / "flight.jsonl"
        seen = set()
        for i in range(flight.MAX_ENTRIES + 25):
            flight.record(doc(f"s{i}"), target, seen)
        kept = flight.entries(target)
        assert len(kept) == flight.MAX_ENTRIES
        assert kept[-1]["id"] == f"s{flight.MAX_ENTRIES + 24}"
        assert kept[0]["id"] == "s25"


class TestReplay:
    def test_synthesized_text_has_the_recorded_shape(self, tmp_path):
        target = tmp_path / "flight.jsonl"
        flight.record(doc(), target, seen=set())
        entry = flight.entries(target)[0]
        replayed = flight.replay(entry)
        assert len(replayed["statics"][0]["txt"]) == len("PACIENTE FICTICIA EJEMPLO")
        assert " " in replayed["statics"][0]["txt"]   # wraps like prose
        assert set(replayed["statics"][0]["txt"]) <= {"x", " "}

    def test_a_replayed_entry_renders(self, tmp_path):
        from apt_log.ui import screenview

        target = tmp_path / "flight.jsonl"
        flight.record(doc(), target, seen=set())
        model = screenview.build(flight.replay(flight.entries(target)[0]))
        assert model is not None
        assert model["rows"]
