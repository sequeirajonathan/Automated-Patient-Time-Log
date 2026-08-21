"""Which visits the machine is allowed to act on.

Every name here is invented, as everywhere else that touches the round.

The property this file exists to hold is the default: **off**. A scheduler
whose switches default to on is a scheduler that starts doing things the first
time somebody edits a file, and the things it would do are EVV records
asserting that a caregiver was at a patient's home.
"""

from __future__ import annotations

import json

from apt_log import arming, schedule as sched


def block(patient="Ada", app="app.one", start="06:00", end="09:00",
          days=("mon",), agency="", part=1, of=1):
    return sched.parse({"visits": [{
        "patient": patient, "app": app, "start": start, "end": end,
        "days": list(days), "agency": agency, "part": part, "of": of}]
    }).blocks[0]


class TestNothingIsArmedUntilSomebodySaysSo:
    def test_a_machine_that_has_never_been_asked_arms_nothing(self):
        assert arming.armed() == set()
        assert arming.is_armed(block()) is False

    def test_a_file_that_will_not_parse_arms_nothing_either(self, tmp_path):
        """The failure has to fall the safe way. An unreadable file meaning
        "everything is on" is the one reading that could not be recovered
        from."""
        from apt_log.ui import state as state_mod

        (state_mod.STATE_DIR / arming.ARMED_NAME).write_text(
            "{ not json", encoding="utf-8")
        assert arming.armed() == set()

    def test_and_neither_does_a_file_of_the_wrong_shape(self):
        from apt_log.ui import state as state_mod

        (state_mod.STATE_DIR / arming.ARMED_NAME).write_text(
            json.dumps({"armed": "all"}), encoding="utf-8")
        assert arming.armed() == set()


class TestThrowingASwitch:
    def test_on_then_off(self):
        key = arming.key_for(block())
        assert arming.set_armed(key, True) is True
        assert arming.armed() == {key}
        assert arming.set_armed(key, False) is False
        assert arming.armed() == set()

    def test_it_survives_a_restart(self):
        """On disk, like every other decision this machine remembers. The
        alternative is a scheduler that forgets what it was allowed to do
        every time a deploy restarts it — which this project has already
        been bitten by once, with notifications."""
        import importlib

        key = arming.key_for(block())
        arming.set_armed(key, True)
        importlib.reload(arming)
        try:
            assert arming.armed() == {key}
        finally:
            importlib.reload(arming)

    def test_arming_one_leaves_the_others_alone(self):
        one, two = arming.key_for(block("Ada")), arming.key_for(block("Bea"))
        arming.set_armed(one, True)
        arming.set_armed(two, True)
        arming.set_armed(one, False)
        assert arming.armed() == {two}

    def test_disarm_all_is_the_way_back(self):
        arming.set_armed(arming.key_for(block("Ada")), True)
        arming.set_armed(arming.key_for(block("Bea")), True)
        arming.disarm_all()
        assert arming.armed() == set()

    def test_a_write_that_fails_reports_what_is_true(self, monkeypatch):
        """A switch that looks thrown and is not is worse than one that
        refuses to move."""
        from pathlib import Path

        def angry(*_a, **_k):
            raise OSError("read-only")

        monkeypatch.setattr(Path, "write_text", angry)
        assert arming.set_armed(arming.key_for(block()), True) is False


class TestTheKeyIsAboutTheBlockAndNotTheDay:
    """"Arm this patient's Monday morning" is a standing decision about a
    recurring thing, not about the fourteenth of March."""

    def test_the_same_block_keys_the_same_every_time(self):
        assert arming.key_for(block()) == arming.key_for(block())

    def test_a_different_patient_is_a_different_switch(self):
        assert arming.key_for(block("Ada")) != arming.key_for(block("Bea"))

    def test_so_is_a_different_time(self):
        assert arming.key_for(block(start="06:00")) != \
            arming.key_for(block(start="07:00"))

    def test_so_are_the_two_halves_of_a_split_visit(self):
        """They are two entries and either may be armed without the other."""
        assert arming.key_for(block(part=1, of=2)) != \
            arming.key_for(block(part=2, of=2))

    def test_a_different_app_is_a_different_switch(self):
        assert arming.key_for(block(app="app.one")) != \
            arming.key_for(block(app="app.two"))

    def test_no_patients_name_is_in_the_key(self):
        """It ends up in form values, page markup and server logs. A hash
        does the same job and spreads nothing."""
        key = arming.key_for(block("Ada"))
        assert "Ada" not in key
        assert "ada" not in key.lower()
        assert len(key) == 12 and all(c in "0123456789abcdef" for c in key)


class TestItIsKeptWhereTheMachineKeepsItsOwnState:
    def test_beside_the_other_things_it_remembers(self):
        """Not /etc: this is state a person changes from the portal, not
        configuration somebody edits by hand."""
        from apt_log.ui import state as state_mod

        arming.set_armed(arming.key_for(block()), True)
        assert (state_mod.STATE_DIR / arming.ARMED_NAME).exists()

    def test_the_path_is_resolved_on_the_call(self):
        """So a test can move it — the lesson from every other state file
        here, learned by deleting /var/lib/aptlog and watching what grew
        back."""
        import inspect

        source = inspect.getsource(arming._path)
        assert "STATE_DIR" in source
        assert "import" in source
