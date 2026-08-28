"""Adopted signatures — and the four lines that make them defensible.

REQ-10.6 forbade storing a signature, and this module is the amendment to it.
An exception like that survives only as long as the reasoning behind it does,
and reasoning in a docstring is not enforcement. These are the enforcement:

  * no timer, macro or scheduler can apply one;
  * no route hands the strokes back;
  * an adoption records who was present;
  * an application leaves a trail.

Erode any one of them and this stops being "a patient signing with one press"
and becomes "the machine signing for a patient", which is the thing everybody
involved agreed not to build.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apt_log import enrolled
from apt_log.ui.app import app


SRC = Path(__file__).resolve().parents[1] / "src" / "apt_log"

# A signature-shaped payload: two strokes, normalised 0..1, as the pad emits.
INK = [[[0.1, 0.5], [0.3, 0.2], [0.5, 0.6]], [[0.6, 0.3], [0.9, 0.4]]]
WITNESS = "signed in front of Sadia at the kitchen table"


@pytest.fixture
def store(tmp_path):
    return tmp_path / "signatures.json"


@pytest.fixture
def client():
    return TestClient(app)


class TestAdoptingASignature:
    def test_it_comes_back_the_way_it_went_in(self, store):
        enrolled.enroll("Carmen Villalon", INK, aspect=2.2,
                        witness=WITNESS, path=store)
        strokes, aspect = enrolled.strokes_for("Carmen Villalon", path=store)
        assert strokes == INK
        assert aspect == pytest.approx(2.2)

    def test_an_unknown_party_is_a_refusal_not_a_crash(self, store):
        assert enrolled.strokes_for("Nobody At All", path=store) is None

    def test_withdrawing_it_takes_it_away(self, store):
        enrolled.enroll("Carmen Villalon", INK, witness=WITNESS, path=store)
        assert enrolled.forget("Carmen Villalon", path=store) is True
        assert enrolled.strokes_for("Carmen Villalon", path=store) is None
        assert enrolled.forget("Carmen Villalon", path=store) is False

    def test_one_person_is_one_adoption(self, store):
        """The card, the app and the portal spell a name three ways. None of
        them is a second person."""
        enrolled.enroll("Carmen Villalon", INK, witness=WITNESS, path=store)
        enrolled.enroll("  CARMEN   VILLALON ", INK, witness=WITNESS,
                        path=store)
        assert len(enrolled.roster(path=store)) == 1

    def test_the_display_name_is_kept_exactly_as_given(self, store):
        """Folding is for matching. It is not for writing down."""
        enrolled.enroll("Lucresia L Pupo", INK, witness=WITNESS, path=store)
        assert enrolled.roster(path=store)[0]["name"] == "Lucresia L Pupo"

    def test_a_store_edited_into_nonsense_refuses(self, store):
        enrolled.enroll("Carmen Villalon", INK, witness=WITNESS, path=store)
        doc = json.loads(store.read_text())
        doc[enrolled.key("Carmen Villalon")]["strokes"] = "not strokes"
        store.write_text(json.dumps(doc))
        assert enrolled.strokes_for("Carmen Villalon", path=store) is None


class TestAnAdoptionSaysWhoWasThere:
    def test_it_refuses_without_a_witness(self, store):
        with pytest.raises(ValueError):
            enrolled.enroll("Carmen Villalon", INK, witness="", path=store)

    def test_a_token_word_is_not_a_witness(self, store):
        with pytest.raises(ValueError):
            enrolled.enroll("Carmen Villalon", INK, witness="ok", path=store)

    def test_the_witness_is_kept_with_the_date(self, store):
        enrolled.enroll("Carmen Villalon", INK, witness=WITNESS, path=store)
        row = enrolled.roster(path=store)[0]
        assert row["witness"] == WITNESS
        assert row["at"]

    def test_only_a_signature_can_be_adopted(self, store):
        """The same shape check the live replay uses, so nothing can be
        enrolled that could never be drawn."""
        for junk in ([], "", [[]], [[[0, 0, 0, 0]]], None):
            with pytest.raises(ValueError):
                enrolled.enroll("Carmen Villalon", junk, witness=WITNESS,
                                path=store)

    def test_a_signature_belongs_to_somebody(self, store):
        with pytest.raises(ValueError):
            enrolled.enroll("   ", INK, witness=WITNESS, path=store)


class TestTheStrokesNeverLeaveTheMachine:
    def test_the_roster_carries_no_signature(self, store):
        enrolled.enroll("Carmen Villalon", INK, witness=WITNESS, path=store)
        assert "strokes" not in json.dumps(enrolled.roster(path=store))

    def test_the_route_carries_no_signature(self, client, store, monkeypatch):
        monkeypatch.setattr(enrolled, "STORE_PATH", store)
        enrolled.enroll("Carmen Villalon", INK, witness=WITNESS, path=store)
        body = client.get("/signature/roster").text
        assert "strokes" not in body
        # And not the numbers themselves under another name.
        assert "0.1" not in body and "0.5" not in body

    def test_no_route_reads_the_store_directly(self):
        """Every route goes through `roster`/`strokes_for`. A route that
        opened the file itself would be one nobody had thought about."""
        source = (SRC / "ui" / "app.py").read_text(encoding="utf-8")
        assert "STORE_PATH" not in source
        assert "signatures.json" not in source


class TestNothingScheduledCanApplyASignature:
    """THE LINE THAT MATTERS MOST.

    A signature applied by a timer is the falsified attestation this whole
    design exists to avoid. The defence is that the acting layer cannot see
    this module at all — not a convention, an import graph.
    """

    @pytest.mark.parametrize("module", ["autoentry.py", "macros.py",
                                        "schedule.py", "feed.py"])
    def test_the_acting_layer_cannot_see_adopted_signatures(self, module):
        source = (SRC / module).read_text(encoding="utf-8")
        assert "enrolled" not in source, (
            f"{module} must not reach adopted signatures — a signature "
            "applied without a press is not an attestation"
        )

    def test_applying_is_a_route_and_only_a_route(self):
        """`strokes_for` has exactly one caller in the tree, and it is the
        one that answers a button."""
        callers = [p for p in SRC.rglob("*.py")
                   if "strokes_for" in p.read_text(encoding="utf-8")
                   and p.name != "enrolled.py"]
        assert [p.name for p in callers] == ["app.py"]


class TestApplyingLeavesATrail:
    def test_a_press_is_recorded(self, tmp_path):
        trail = tmp_path / "signings.jsonl"
        enrolled.record_use("Carmen Villalon", "abc123", package="com.x",
                            path=trail)
        row = json.loads(trail.read_text().strip())
        assert row["name"] == "Carmen Villalon"
        assert row["package"] == "com.x"
        assert row["how"] == "pressed"

    def test_the_trail_carries_no_strokes(self, tmp_path):
        trail = tmp_path / "signings.jsonl"
        enrolled.record_use("Carmen Villalon", "abc123", path=trail)
        assert "strokes" not in trail.read_text()

    def test_it_appends_rather_than_replaces(self, tmp_path):
        trail = tmp_path / "signings.jsonl"
        enrolled.record_use("A Patient", "d1", path=trail)
        enrolled.record_use("A Patient", "d1", path=trail)
        assert len(trail.read_text().strip().splitlines()) == 2

    def test_a_trail_that_cannot_be_written_does_not_stop_the_signature(self):
        """Two people are standing in front of the screen. A logging problem
        is not a reason to fail their check-out."""
        enrolled.record_use("A Patient", "d1",
                            path=Path("/proc/nonexistent/signings.jsonl"))


class TestTheStoreIsOwnerOnly:
    def test_it_is_written_0600(self, store):
        enrolled.enroll("Carmen Villalon", INK, witness=WITNESS, path=store)
        assert os.stat(store).st_mode & 0o777 == 0o600

    def test_rewriting_it_does_not_widen_it(self, store):
        enrolled.enroll("Carmen Villalon", INK, witness=WITNESS, path=store)
        os.chmod(store, 0o644)
        enrolled.enroll("Another Party", INK, witness=WITNESS, path=store)
        assert os.stat(store).st_mode & 0o777 == 0o600


class TestTheImageCarriesNoSignatures:
    def test_the_sanitiser_removes_the_store(self):
        """An image is copied, handed over and archived. The schedule is
        stripped for naming people; this is stronger — it reproduces their
        signatures."""
        script = (SRC.parents[1] / "scripts" / "sanitize-for-image.sh"
                  ).read_text(encoding="utf-8")
        assert "signatures.json" in script


class TestTheRoutes:
    def test_applying_an_unknown_party_is_a_refusal(self, client, store,
                                                    monkeypatch):
        monkeypatch.setattr(enrolled, "STORE_PATH", store)
        r = client.post("/signature/apply", json={"name": "Nobody"})
        assert r.status_code == 404

    def test_applying_queues_the_same_replay_a_drawn_one_uses(
            self, client, store, monkeypatch):
        monkeypatch.setattr(enrolled, "STORE_PATH", store)
        enrolled.enroll("Carmen Villalon", INK, aspect=2.2, witness=WITNESS,
                        path=store)
        seen = {}

        def fake_request(strokes, aspect=1.0):
            seen["strokes"], seen["aspect"] = strokes, aspect
            return "rid123"

        monkeypatch.setattr("apt_log.sign.request", fake_request)
        r = client.post("/signature/apply",
                        json={"name": "Carmen Villalon", "package": "com.x"})
        assert r.status_code == 200
        assert seen["strokes"] == INK
        assert seen["aspect"] == pytest.approx(2.2)

    def test_enrolling_without_a_witness_is_a_four_hundred(self, client,
                                                           store, monkeypatch):
        monkeypatch.setattr(enrolled, "STORE_PATH", store)
        r = client.post("/signature/enroll",
                        json={"name": "Carmen Villalon", "strokes": INK})
        assert r.status_code == 400

    def test_enrolling_junk_is_a_four_hundred(self, client, store,
                                              monkeypatch):
        monkeypatch.setattr(enrolled, "STORE_PATH", store)
        r = client.post("/signature/enroll",
                        json={"name": "Carmen Villalon",
                              "strokes": "not a signature",
                              "witness": WITNESS})
        assert r.status_code == 400

    def test_a_party_can_withdraw(self, client, store, monkeypatch):
        monkeypatch.setattr(enrolled, "STORE_PATH", store)
        enrolled.enroll("Carmen Villalon", INK, witness=WITNESS, path=store)
        r = client.post("/signature/forget", json={"name": "Carmen Villalon"})
        assert r.json()["ok"] is True
        assert enrolled.strokes_for("Carmen Villalon", path=store) is None


class TestMatchingTheScreenToTheRoster:
    """The app's name and the adopted name are not the same string.

    The app renders a legal name off an agency record; the adoption is typed
    into a phone by somebody standing in a living room. Case, accents and a
    middle initial must not decide whether a person's own signature is offered
    to them — and nothing looser than that, because every loosening is a step
    toward one person's signature going under another person's name.
    """

    def test_a_middle_initial_does_not_break_it(self):
        assert enrolled.matches("MARIA X GARCIA", "Maria Garcia")

    def test_case_and_accents_do_not_break_it(self):
        assert enrolled.matches("ANA RUÍZ", "ana ruiz")

    def test_a_shared_surname_is_not_a_match(self):
        """Two people in one household is the ordinary case here, not an
        exotic one."""
        assert not enrolled.matches("JUAN PEREZ", "Ana Perez")

    def test_a_different_person_is_not_a_match(self):
        assert not enrolled.matches("MARIA GARCIA", "Beto Sosa")

    def test_either_side_may_carry_more_of_the_name(self):
        """Neither the app nor the adoption is authoritative about how much
        of somebody's name gets written down."""
        assert enrolled.matches("MARIA GARCIA", "Maria Garcia Lopez")
        assert enrolled.matches("Maria Garcia Lopez", "MARIA GARCIA")

    def test_an_empty_side_matches_nothing(self):
        assert not enrolled.matches("", "Maria Garcia")
        assert not enrolled.matches("Maria Garcia", "")

    def test_initials_alone_are_not_a_name(self):
        """"M X" against "Maria Garcia" would otherwise pass on an empty
        significant set."""
        assert not enrolled.matches("M X", "Maria Garcia")


class TestWhoSignsResolvesToOneOrNobody:
    def _enrol(self, tmp_path, *names):
        store = tmp_path / "sig.json"
        for n in names:
            enrolled.enroll(n, INK, witness=WITNESS, path=store)
        return store

    def test_one_candidate_returns_the_rosters_own_spelling(self, tmp_path):
        """So the caller can point at a button by the name printed on it,
        not by the app's version of it."""
        store = self._enrol(tmp_path, "Maria Garcia")
        assert enrolled.who_signs("MARIA X GARCIA", path=store) == \
            "Maria Garcia"

    def test_two_candidates_return_nobody(self, tmp_path):
        """Ambiguity is the one thing not tolerated: showing no suggestion
        costs a tap, showing the wrong one costs the trail."""
        store = self._enrol(tmp_path, "Maria Garcia", "Maria Garcia Lopez")
        assert enrolled.who_signs("MARIA GARCIA", path=store) == ""

    def test_no_candidate_returns_nobody(self, tmp_path):
        store = self._enrol(tmp_path, "Maria Garcia")
        assert enrolled.who_signs("Beto Sosa", path=store) == ""

    def test_an_unnamed_screen_returns_nobody(self, tmp_path):
        store = self._enrol(tmp_path, "Maria Garcia")
        assert enrolled.who_signs("", path=store) == ""

    def test_an_empty_roster_returns_nobody(self, tmp_path):
        assert enrolled.who_signs("MARIA GARCIA",
                                  path=tmp_path / "none.json") == ""
