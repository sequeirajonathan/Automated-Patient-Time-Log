"""Reading the code off the phone that received it.

inMyTeam is the one app here that cannot be signed in without a human: it
texts six digits and waits. That is only true while the code lands somewhere
the controller cannot see — and `content query --uri content://sms/inbox`
reads it off a phone the controller is already driving.

Every test here is about the two ways that goes wrong: reading a message that
is none of this project's business, and typing a code that has expired.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from apt_log import sms
# Captured at import, BEFORE conftest stubs the module attribute — these two
# tests are about `_adb`'s own error handling, which a stub cannot exercise.
from apt_log.sms import _adb as REAL_ADB


def rows(*messages) -> str:
    """The provider's own output shape: `Row: N key=value, key=value`."""
    return "\n".join(
        "Row: %d date=%d, body=%s" % (i, int(when * 1000), body)
        for i, (when, body) in enumerate(messages))


REAL = "Here is your passcode from INMYTEAM: 604820"


class TestFindingTheCodeInAMessage:
    def test_the_real_message_this_was_written_from(self):
        assert sms.code_from(REAL) == "604820"

    def test_the_last_six_digit_run_wins(self):
        """A reference number can share the sentence; the code is where a
        person's eye ends up, after the colon."""
        assert sms.code_from("Ref 998877 passcode: 604820") == "604820"

    def test_a_longer_run_of_digits_is_not_a_code(self):
        """A phone number in the body must not be mistaken for one."""
        assert sms.code_from("Call 17864204664 for help") == ""

    def test_a_shorter_run_is_not_either(self):
        assert sms.code_from("Your code is 1234") == ""

    def test_nothing_to_read_is_not_a_code(self):
        assert sms.code_from("") == ""
        assert sms.code_from(None) == ""


class TestOnlyOneSenderIsEverRead:
    """A caregiver's phone carries messages from patients, from the agency,
    from her family. None of that is this project's business, so the filter
    is applied AT THE PROVIDER — the query cannot return somebody else's
    message even for an instant."""

    def _capture(self, out=""):
        seen = {}

        def fake(args, serial=None, timeout=20.0):
            seen["args"] = args
            return out
        return seen, fake

    def test_the_sender_is_filtered_in_the_query_itself(self):
        seen, fake = self._capture()
        with patch.object(sms, "_adb", fake):
            sms.latest_code("7864204664")
        where = seen["args"][seen["args"].index("--where") + 1]
        assert "7864204664" in where
        assert "address LIKE" in where

    def test_only_the_date_and_body_are_asked_for(self):
        """Not the whole row. There is no reason for this process to see a
        thread id or an address it already knows."""
        seen, fake = self._capture()
        with patch.object(sms, "_adb", fake):
            sms.latest_code("7864204664")
        projection = seen["args"][seen["args"].index("--projection") + 1]
        assert projection == "date:body"

    def test_a_sender_with_no_digits_reads_nothing_at_all(self):
        """Rather than querying for everything, which is what an empty
        pattern would do."""
        called = []
        with patch.object(sms, "_adb", lambda *a, **k: called.append(1) or ""):
            assert sms.latest_code("") == ""
        assert not called

    def test_the_last_ten_digits_are_what_match(self):
        """The provider stores whatever shape arrived — +1 prefixed, bare,
        sometimes spaced — and the last ten are common to all of them."""
        seen, fake = self._capture()
        with patch.object(sms, "_adb", fake):
            sms.latest_code("+1 (786) 420-4664")
        where = seen["args"][seen["args"].index("--where") + 1]
        assert "7864204664" in where


class TestOnlyAFreshCodeCounts:
    """An old code is not merely useless. inMyTeam REJECTS it — which clears
    the field, raises a warning dialog that is absent from the accessibility
    tree, and spends one of a limited number of attempts."""

    def test_a_code_from_a_moment_ago_is_taken(self):
        now = time.time()
        with patch.object(sms, "_adb", lambda *a, **k: rows((now - 5, REAL))):
            assert sms.latest_code(now=now) == "604820"

    def test_a_code_from_an_hour_ago_is_not(self):
        now = time.time()
        with patch.object(sms, "_adb",
                          lambda *a, **k: rows((now - 3600, REAL))):
            assert sms.latest_code(now=now) == ""

    def test_the_newest_wins_when_several_are_in_the_window(self):
        now = time.time()
        out = rows((now - 90, "passcode from INMYTEAM: 111111"),
                   (now - 10, "passcode from INMYTEAM: 222222"),
                   (now - 50, "passcode from INMYTEAM: 333333"))
        with patch.object(sms, "_adb", lambda *a, **k: out):
            assert sms.latest_code(now=now) == "222222"

    def test_a_message_dated_in_the_future_is_not_trusted(self):
        """A phone with a wrong clock produces these, and they sort first."""
        now = time.time()
        out = rows((now + 8000, "passcode from INMYTEAM: 999999"),
                   (now - 5, REAL))
        with patch.object(sms, "_adb", lambda *a, **k: out):
            assert sms.latest_code(now=now) == "604820"

    def test_a_row_with_an_unreadable_date_is_skipped_not_fatal(self):
        now = time.time()
        out = "Row: 0 date=nonsense, body=%s\n%s" % (REAL, rows((now - 5, REAL)))
        with patch.object(sms, "_adb", lambda *a, **k: out):
            assert sms.latest_code(now=now) == "604820"

    def test_a_matching_sender_with_no_code_in_it_is_nothing(self):
        now = time.time()
        with patch.object(sms, "_adb",
                          lambda *a, **k: rows((now - 5, "Welcome to inMyTeam"))):
            assert sms.latest_code(now=now) == ""


class TestWhenTheProviderWillNotAnswer:
    def test_no_output_is_no_code_rather_than_a_crash(self):
        with patch.object(sms, "_adb", lambda *a, **k: ""):
            assert sms.latest_code() == ""

    def test_adb_failing_is_not_an_exception(self):
        with patch("subprocess.run", side_effect=OSError("no adb")):
            assert REAL_ADB(["shell", "true"]) == ""

    def test_a_nonzero_exit_reads_as_nothing(self):
        class Done:
            returncode = 1
            stdout = b"Error while accessing provider:sms"
        with patch("subprocess.run", return_value=Done()):
            assert REAL_ADB(["shell", "true"]) == ""


class TestWaitingForOneThatHasNotArrivedYet:
    """`after` is what makes this safe to call twice. Without it, a second run
    finds the FIRST run's code still in the inbox, types a code the app has
    already rejected, and reports success."""

    def test_it_returns_the_code_once_it_lands(self):
        codes = ["", "", "604820"]
        with patch.object(sms, "latest_code",
                          side_effect=lambda *a, **k: codes.pop(0)), \
                patch.object(sms.time, "sleep", lambda _s: None):
            assert sms.wait_for_code(timeout=30.0) == "604820"

    def test_it_gives_up_rather_than_hanging(self):
        clock = iter([0.0] + [float(i) for i in range(1, 200)])
        with patch.object(sms, "latest_code", lambda *a, **k: ""), \
                patch.object(sms.time, "sleep", lambda _s: None), \
                patch.object(sms.time, "monotonic", lambda: next(clock)):
            assert sms.wait_for_code(timeout=5.0) == ""

    def test_the_window_starts_when_the_caller_says(self):
        """So a code that predates Sign in cannot be picked up."""
        seen = {}

        def fake(sender=sms.INMYTEAM_SENDER, within=0.0, serial=None,
                 now=None):
            seen["within"] = within
            return "604820"

        with patch.object(sms, "latest_code", fake):
            sms.wait_for_code(after=time.time() - 10)
        # The window is how long ago the caller started, not the default.
        assert 9 <= seen["within"] <= 12


class TestQuotedForTheDevicesOwnShell:
    """`adb shell` does not take an argv. It joins what it is given into a
    command line and hands it to sh ON THE PHONE, so an unquoted WHERE clause
    arrives as five separate words.

    Caught live and worth the test: the query came back 2,765 bytes of
    `content`'s usage text, the row parser read nought rows out of it, and
    the result was indistinguishable from an empty inbox. `feed.type_into`
    learned the same lesson about a patient's name with a space in it.
    """

    def _where(self, sender="7864204664"):
        seen = {}

        def fake(args, serial=None, timeout=20.0):
            seen["args"] = args
            return ""

        with patch.object(sms, "_adb", fake):
            sms.latest_code(sender)
        return seen["args"][seen["args"].index("--where") + 1]

    def test_the_clause_survives_as_one_word(self):
        import shlex

        where = self._where()
        # What the phone's sh will actually pass to `content`.
        assert shlex.split(where) == ["address LIKE '%7864204664%'"]

    def test_the_sql_quotes_are_still_there_underneath(self):
        import shlex

        assert shlex.split(self._where())[0].endswith("'%7864204664%'")

    def test_usage_text_is_not_mistaken_for_rows(self):
        """What the unquoted version actually returned."""
        usage = "usage: adb shell content [subcommand] [options]\n" * 40
        assert sms._rows(usage) == []
        with patch.object(sms, "_adb", lambda *a, **k: usage):
            assert sms.latest_code() == ""


class TestNoTestCanReachThePhone:
    """This did not leak state — it read a caregiver's SMS inbox, from the
    deploy gate, on the live machine.

    The sign-in walk now looks for a texted code, so every test exercising
    the walk ran `content query --uri content://sms/inbox` for real. It hung
    the gate for twenty minutes: each poll spawns an adb subprocess with a
    twenty-second timeout, the walk waits over a minute, and a busy phone
    answers slowly. Two managers queued behind a lock and the deploy that
    looked like a slow gate was a test suite interrogating a phone.
    """

    def test_the_device_read_is_stubbed_for_every_test(self):
        """Not a stub this test installs — one conftest installed, which is
        what makes it true of tests that never think about messages."""
        assert sms._adb(["shell", "content", "query"]) == ""

    def test_so_reading_a_code_never_shells_out(self):
        seen = []
        with patch("subprocess.run", lambda *a, **k: seen.append(a)):
            assert sms.latest_code() == ""
        assert not seen, "a test reached for the phone's message store"

    def test_and_waiting_for_one_does_not_either(self):
        seen = []
        with patch("subprocess.run", lambda *a, **k: seen.append(a)), \
                patch.object(sms.time, "sleep", lambda _s: None), \
                patch.object(sms.time, "monotonic",
                             side_effect=[0.0, 1.0, 99.0]):
            assert sms.wait_for_code(timeout=5.0) == ""
        assert not seen
