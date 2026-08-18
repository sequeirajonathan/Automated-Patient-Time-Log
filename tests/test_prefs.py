"""Preferences: who is looking, and how they like to look.

Two properties carry real weight here and the rest is bookkeeping.

**The floor is a safety property.** 72 dpi crashed the phone during tuning and
needed somebody in the room to power-cycle it. The slider that sets density is
on a page reachable from a phone in another state, so the clamp cannot be a
convention — it has to be the only way in.

**Defaults are never overwritten.** An override is a layer above the code's own
table, so clearing one uncovers the value that was tuned against the real
screens. If setting and clearing went through the same field, a cleared
override would leave nothing behind and the phone would be laid out at whatever
the last person dragged to.
"""

from __future__ import annotations

import json

import pytest

from apt_log import prefs


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "prefs.json"
    monkeypatch.setattr(prefs, "PREFS_PATH", path)
    return path


class TestDensityIsClamped:
    def test_the_value_that_crashed_the_phone_cannot_be_reached(self, store):
        assert prefs.clamp(72) == prefs.DENSITY_FLOOR
        assert prefs.set_density("com.example.app", 40) == prefs.DENSITY_FLOOR

    def test_the_ceiling_holds_too(self, store):
        assert prefs.set_density("com.example.app", 10_000) == prefs.DENSITY_CEILING

    def test_nonsense_is_no_opinion_rather_than_a_number(self, store):
        for junk in (None, "", "big", object()):
            assert prefs.clamp(junk) is None

    def test_a_hand_edited_file_is_clamped_on_the_way_out(self, store):
        """The file is editable by whoever is on the Pi, so the read is where
        the guarantee has to hold — not only the write."""
        store.write_text(json.dumps({"density": {"com.example.app": 12}}),
                         encoding="utf-8")
        assert prefs.density_for("com.example.app") == prefs.DENSITY_FLOOR


class TestOverridesNeverLoseTheDefault:
    def test_clearing_falls_through_to_what_was_handed_in(self, store):
        prefs.set_density("com.inmyteam.inmyteam", 140)
        assert prefs.density_for("com.inmyteam.inmyteam", default=105) == 140
        prefs.set_density("com.inmyteam.inmyteam", None)
        assert prefs.density_for("com.inmyteam.inmyteam", default=105) == 105

    def test_a_page_beats_its_app(self, store):
        prefs.set_density("com.hhaexchange.caregiver", 96)
        prefs.set_density("com.hhaexchange.caregiver", 120, page=".SignatureActivity")
        assert prefs.density_for("com.hhaexchange.caregiver",
                                 page=".SignatureActivity") == 120
        assert prefs.density_for("com.hhaexchange.caregiver", page=".Other") == 96

    def test_clearing_a_page_uncovers_the_app_not_nothing(self, store):
        prefs.set_density("com.hhaexchange.caregiver", 96)
        prefs.set_density("com.hhaexchange.caregiver", 120, page=".SignatureActivity")
        prefs.set_density("com.hhaexchange.caregiver", None, page=".SignatureActivity")
        assert prefs.density_for("com.hhaexchange.caregiver",
                                 page=".SignatureActivity") == 96

    def test_the_global_setting_does_not_outrank_a_tuned_app(self, store):
        """It is for screens this system has no table for. Consulted in the
        per-app chain it would silently outrank every value tuned against a
        real screen."""
        prefs.set_global_density(150)
        assert prefs.density_for("com.inmyteam.inmyteam") is None
        assert prefs.global_density() == 150

    def test_nothing_set_is_nothing_returned(self, store):
        assert prefs.density_for("com.example.app") is None
        assert prefs.overrides() == {}


class TestDevices:
    def test_a_device_that_never_chose_has_no_language(self, store):
        prefs.seen("dev-1", where="/app")
        assert prefs.language_of("dev-1") == ""
        assert prefs.device("dev-1")["language"] == prefs.DEFAULT_LANGUAGE

    def test_one_devices_language_does_not_follow_another(self, store):
        """The whole reason preferences are per device: he reads English on his
        phone while she keeps reading Spanish on hers."""
        prefs.set_language("his", "en")
        prefs.set_language("hers", "es")
        assert prefs.language_of("his") == "en"
        assert prefs.language_of("hers") == "es"

    def test_an_unsupported_language_falls_back_rather_than_sticking(self, store):
        assert prefs.set_language("dev-1", "de") == prefs.DEFAULT_LANGUAGE

    def test_the_list_is_most_recent_first(self, store):
        prefs.seen("older", where="/app")
        prefs.seen("newer", where="/console")
        assert [d["id"] for d in prefs.devices()][0] == "newer"

    def test_a_name_makes_the_list_an_answer(self, store):
        prefs.seen("dev-1", where="/app")
        prefs.rename("dev-1", "Sadia — iPhone")
        assert prefs.device("dev-1")["name"] == "Sadia — iPhone"

    def test_a_device_nobody_has_used_in_a_month_is_forgotten(self, store):
        prefs.seen("ancient")
        doc = prefs.load()
        doc["devices"]["ancient"]["seen"] = 0
        prefs.save(doc)
        prefs.seen("current")
        assert "ancient" not in prefs.load()["devices"]


class TestItNeverTakesAPageDown:
    def test_a_malformed_file_reads_as_no_preferences(self, store):
        store.write_text("{ this is not json", encoding="utf-8")
        assert prefs.load() == {"devices": {}, "density": {}}

    def test_a_file_that_cannot_be_written_is_not_an_exception(self, tmp_path,
                                                               monkeypatch):
        """A preference that will not save must not fail the page it was set
        from — the page is what she is using; the preference is a nicety."""
        monkeypatch.setattr(prefs, "PREFS_PATH",
                            tmp_path / "no" / "such" / "dir" / "x" / "p.json")
        monkeypatch.setattr(prefs.Path, "mkdir",
                            lambda *a, **k: (_ for _ in ()).throw(OSError()))
        prefs.set_density("com.example.app", 100)      # must not raise
