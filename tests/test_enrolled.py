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
    """What an adoption has to say about itself, and what it no longer must.

    Condition 3 of REQ-10.6a once demanded a typed statement of who was
    present, refused if it was missing. The owner of the requirement removed
    it: the field asked, in front of a patient, a question whose answer was
    always the same two people, and a box filled in the same way every time is
    not a record of anything.

    So the demand is gone and the DATE is not — nor is the audit of every
    later use, which is the part that actually answers an auditor. These tests
    hold the new line rather than being deleted with the old one: a condition
    that was dropped on purpose should still have a test saying so.
    """

    def test_no_witness_is_no_longer_a_refusal(self, store):
        enrolled.enroll("Carmen Villalon", INK, witness="", path=store)
        assert enrolled.enrolled("Carmen Villalon", path=store)

    def test_it_can_be_left_out_entirely(self, store):
        """The client stopped sending the field at all when it came off the
        sheet, so absent and empty must behave alike."""
        enrolled.enroll("Carmen Villalon", INK, path=store)
        assert enrolled.roster(path=store)[0]["witness"] == ""

    def test_the_date_is_still_kept(self, store):
        """The condition that survived. An adoption with no date is one
        nobody can place in time, and that one does matter."""
        enrolled.enroll("Carmen Villalon", INK, path=store)
        assert enrolled.roster(path=store)[0]["at"]

    def test_an_older_adoption_keeps_what_it_said(self, store):
        """The field survives in the store and the roster still shows it, so
        adoptions made before the change do not quietly lose their sentence."""
        enrolled.enroll("Carmen Villalon", INK, witness=WITNESS, path=store)
        assert enrolled.roster(path=store)[0]["witness"] == WITNESS

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
        """THIS GUARD USED TO SEARCH FOR "0.1" IN THE RESPONSE TEXT, and it
        blocked a deploy at 19:00:20 by matching its own timestamp — the
        ".1" of `20.166373`. It had passed every run before that and would
        have failed one at random forever, which is the worst shape a guard
        can have: it looks strict, it is not checking what it claims, and it
        fires on the clock rather than on a leak.

        Checked by SHAPE instead. A stroke set is the only thing in this
        store that is a list of numbers, so no value in a roster row may be a
        list or an object at all — that catches the coordinates under any key
        anybody invents, which the substring never did.
        """
        monkeypatch.setattr(enrolled, "STORE_PATH", store)
        enrolled.enroll("Carmen Villalon", INK, witness=WITNESS, path=store)
        payload = client.get("/signature/roster").json()
        assert "strokes" not in json.dumps(payload)
        assert "points" not in json.dumps(payload)
        rows = payload["parties"]
        assert rows, "nothing was returned, so nothing was checked"
        for row in rows:
            for field, value in row.items():
                # Strings, or a list of them: a name, a date, a digest, the
                # apps a mark is for. A stroke set is numbers, nested, and
                # that is the shape being kept out — narrowed from "everything
                # must be a string", which forbade the apps list the moment
                # one person needed two signatures.
                if isinstance(value, str):
                    continue
                assert isinstance(value, list) and all(
                    isinstance(v, str) for v in value), (
                    "%s is %r — the only thing in this store shaped like "
                    "that is a signature" % (field, value))

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

    def test_enrolling_without_a_witness_is_accepted_now(self, client, store,
                                                         monkeypatch):
        """It used to be a 400. The field came off the sheet, so the client
        stopped sending it — and a route still refusing would mean every
        registration failing at the last press."""
        monkeypatch.setattr(enrolled, "STORE_PATH", store)
        r = client.post("/signature/enroll",
                        json={"name": "Carmen Villalon", "strokes": INK})
        assert r.status_code == 200
        assert enrolled.enrolled("Carmen Villalon", path=store)

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


class TestTheStoreLivesWhereTheServiceCanWrite:
    """The first live registration failed, and not on anything about signatures.

        PermissionError: [Errno 13] Permission denied:
        '/etc/aptlog/signatures.tmp'

    `/etc/aptlog` is root-owned and NOT writable by the service, deliberately:
    REQ-5.4.1 turns on the service being unable to create
    `/etc/aptlog/transport.conf` and switch off its own containment. An atomic
    write needs a temporary file beside the target, so the store could never
    have been written there — and loosening that directory would have traded a
    containment guarantee for a save button.
    """

    def _source(self) -> str:
        """Read from the file, not the module.

        conftest redirects both constants to a temporary path for every test
        — which is the isolation working, and which means the live module
        never says where the store really goes. The declaration does."""
        from pathlib import Path

        return Path("src/apt_log/enrolled.py").read_text(encoding="utf-8")

    def test_it_is_in_the_state_directory(self):
        src = self._source()
        assert 'STORE_PATH = Path("/var/lib/aptlog/signatures.json")' in src
        assert 'USE_PATH = Path("/var/lib/aptlog/signings.jsonl")' in src

    def test_it_is_not_in_the_config_directory(self):
        """Which the service cannot write to, and must not be able to."""
        assert 'STORE_PATH = Path("/etc/aptlog' not in self._source()

    def test_the_file_is_still_owner_only(self, store):
        """Moving the directory changed where it lives, not who may read a
        stroke set. The file's own mode is what decides that."""
        enrolled.enroll("Carmen Villalon", INK, path=store)
        assert oct(os.stat(store).st_mode & 0o777) == oct(enrolled.STORE_MODE)

    def test_the_temporary_file_lands_beside_it(self, tmp_path):
        """The whole reason this moved. An atomic replace cannot cross a
        filesystem, so the temporary file has to be writable in the same
        directory as the target."""
        store = tmp_path / "nested" / "signatures.json"
        enrolled.enroll("Carmen Villalon", INK, path=store)
        assert store.exists()
        assert not list(store.parent.glob("*.tmp"))

    def test_the_sanitiser_sweeps_both_paths(self):
        """A Pi imaged today may have been registered on before the move, and
        a signature left in the old place is still a signature."""
        from pathlib import Path

        script = Path("scripts/sanitize-for-image.sh").read_text(
            encoding="utf-8")
        assert "/var/lib/aptlog/signatures.json" in script
        assert "/etc/aptlog/signatures.json" in script

    def test_the_sanitiser_sweeps_the_trail_too(self):
        """It names patients and dates. Not reproducible ink, and not
        something to hand over either."""
        from pathlib import Path

        script = Path("scripts/sanitize-for-image.sh").read_text(
            encoding="utf-8")
        assert "/var/lib/aptlog/signings.jsonl" in script

    def test_the_sanitiser_sweeps_the_open_visit(self):
        """`ui.opened` writes down one patient's name and the minute her
        visit was opened, so the pad the check-out leads to knows whose it
        is. Small, and exactly the kind of small that must not travel with a
        disk image."""
        from pathlib import Path

        script = Path("scripts/sanitize-for-image.sh").read_text(
            encoding="utf-8")
        assert "/var/lib/aptlog/visit-open.json" in script


class TestAStoreThatCannotBeWritten:
    """It answered 500, and the page said "that didn't reach the phone".

    A sentence about a handset, over a fault that was entirely the Pi's own
    disk — pointing the caregiver at the one thing that was working.
    """

    def test_it_is_not_a_bad_request(self, client, monkeypatch, tmp_path):
        """400 would say she sent something wrong. She did not."""
        unwritable = tmp_path / "nope" / "signatures.json"
        monkeypatch.setattr(enrolled, "STORE_PATH", unwritable)
        monkeypatch.setattr(enrolled, "_write", _refuse_to_write)
        r = client.post("/signature/enroll",
                        json={"name": "Carmen Villalon", "strokes": INK})
        assert r.status_code != 400
        assert r.status_code != 500

    def test_it_names_the_store(self, client, monkeypatch, tmp_path):
        """So the page can say which machine is at fault instead of guessing
        at the phone."""
        monkeypatch.setattr(enrolled, "STORE_PATH",
                            tmp_path / "nope" / "signatures.json")
        monkeypatch.setattr(enrolled, "_write", _refuse_to_write)
        r = client.post("/signature/enroll",
                        json={"name": "Carmen Villalon", "strokes": INK})
        assert r.json()["error"] == "store_unwritable"

    def test_nothing_is_recorded(self, client, monkeypatch, tmp_path):
        store = tmp_path / "signatures.json"
        monkeypatch.setattr(enrolled, "STORE_PATH", store)
        monkeypatch.setattr(enrolled, "_write", _refuse_to_write)
        client.post("/signature/enroll",
                    json={"name": "Carmen Villalon", "strokes": INK})
        assert not enrolled.enrolled("Carmen Villalon", path=store)


def _refuse_to_write(*a, **k):
    raise PermissionError(13, "Permission denied")


class TestOnePersonTwoSignatures:
    """The caregiver signs inMyTeam one way and the other two another.

    She told us: inMyTeam takes her full "S Amselem" and the other apps take
    her initials. A store keyed by name alone holds one of those, so whichever
    was registered last would have been drawn on all three — her full mark
    onto a form that wants initials, or initials onto the one that does not.
    """

    OTHER = [[[0.2, 0.4], [0.4, 0.3], [0.6, 0.7]], [[0.7, 0.2], [0.8, 0.5]]]
    IMT = "com.inmyteam.inmyteam"
    UMA = "com.hhaexchange.uma"
    MC = "com.tellus.evv.v2"

    def _both(self, store):
        enrolled.enroll("Sadia Amselem", INK, apps=[self.IMT], path=store)
        enrolled.enroll("Sadia Amselem", self.OTHER, apps=[self.UMA, self.MC],
                        path=store)

    def test_each_app_gets_its_own_mark(self, store):
        self._both(store)
        assert enrolled.strokes_for("Sadia Amselem", path=store,
                                    package=self.IMT)[0] == INK
        for pkg in (self.UMA, self.MC):
            assert enrolled.strokes_for("Sadia Amselem", path=store,
                                        package=pkg)[0] == self.OTHER

    def test_an_app_she_has_not_registered_for_gets_nothing(self, store):
        """Rather than the other one. Drawing her initials where the form
        wants a signature is the failure this exists to prevent, and a
        near-miss is not better than an honest refusal."""
        self._both(store)
        assert enrolled.strokes_for("Sadia Amselem", path=store,
                                    package="com.example.other") is None

    def test_an_unscoped_adoption_still_covers_everything(self, store):
        """Every adoption made before today is unscoped, and a patient who
        appears in one app never needs more."""
        enrolled.enroll("Carmen Villalon", INK, path=store)
        for pkg in (self.IMT, self.UMA, ""):
            assert enrolled.strokes_for("Carmen Villalon", path=store,
                                        package=pkg)[0] == INK

    def test_a_scoped_adoption_beats_the_fallback(self, store):
        """Otherwise the general mark would win by being first in the file."""
        enrolled.enroll("Sadia Amselem", self.OTHER, path=store)
        enrolled.enroll("Sadia Amselem", INK, apps=[self.IMT], path=store)
        assert enrolled.strokes_for("Sadia Amselem", path=store,
                                    package=self.IMT)[0] == INK
        assert enrolled.strokes_for("Sadia Amselem", path=store,
                                    package=self.UMA)[0] == self.OTHER

    def test_the_trail_records_the_mark_that_was_drawn(self, store):
        """Not whichever came first in the file. An audit that names the
        wrong signature is worse than one that names none."""
        self._both(store)
        imt = enrolled.digest_for("Sadia Amselem", path=store,
                                  package=self.IMT)
        uma = enrolled.digest_for("Sadia Amselem", path=store,
                                  package=self.UMA)
        assert imt and uma and imt != uma

    def test_withdrawing_one_leaves_the_other(self, store):
        self._both(store)
        assert enrolled.forget("Sadia Amselem", path=store, apps=[self.IMT])
        assert enrolled.strokes_for("Sadia Amselem", path=store,
                                    package=self.IMT) is None
        assert enrolled.strokes_for("Sadia Amselem", path=store,
                                    package=self.UMA) is not None

    def test_withdrawing_without_saying_which_takes_them_all(self, store):
        """What "withdraw my signature" meant before one person could have
        two, and still the right answer when nobody has said which."""
        self._both(store)
        assert enrolled.forget("Sadia Amselem", path=store)
        assert enrolled.roster(path=store) == []

    def test_the_roster_says_what_each_is_for(self, store):
        self._both(store)
        scopes = sorted(tuple(sorted(r["apps"]))
                        for r in enrolled.roster(path=store))
        assert scopes == sorted([(self.IMT,),
                                 tuple(sorted((self.UMA, self.MC)))])

    def test_who_signs_answers_per_app(self, store):
        enrolled.enroll("Sadia Amselem", INK, apps=[self.IMT], path=store)
        assert enrolled.who_signs("S Amselem", path=store,
                                  package=self.IMT) == "Sadia Amselem"
        assert enrolled.who_signs("S Amselem", path=store,
                                  package=self.UMA) == ""

    def test_two_marks_for_one_person_are_not_an_ambiguity(self, store):
        """`who_signs` returns nothing when two PEOPLE could be meant. Two
        adoptions belonging to the same person, only one of which covers the
        app in front, is not that."""
        self._both(store)
        assert enrolled.who_signs("Sadia Amselem", path=store,
                                  package=self.IMT) == "Sadia Amselem"
