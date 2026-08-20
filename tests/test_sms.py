"""Reading the code off the phone that received it.

inMyTeam is the one app here that cannot be signed in without a human: it
texts six digits and waits. That is only true while the code lands somewhere
the controller cannot see — and `content query --uri content://sms/inbox`
reads it off a phone the controller is already driving.

Every test here is about the two ways that goes wrong: reading a message that
is none of this project's business, and typing a code that has expired.
"""

from __future__ import annotations

import importlib
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

    def test_nor_does_sending_one(self):
        """The direction that would cost real money and reach a real
        handset. Same stub, and it has to cover this too."""
        seen = []
        with patch("subprocess.run", lambda *a, **k: seen.append(a)):
            assert sms.send("5551234567", "hello") is False
        assert not seen, "a test tried to send a text"


# ================================================================== SENDING


class TestSendingOneFromThisPhone:
    """`service call isms` rather than the messaging app, because the moment
    a code needs sending is the moment inMyTeam is holding its code screen
    open — and walking away to Messages abandons the sign-in this exists to
    help."""

    def _args(self, out=sms._SENT_OK, number="954-540-5276", body="hi"):
        seen = {}

        def fake(args, serial=None, timeout=20.0):
            seen["args"] = args
            return out

        with patch.object(sms, "_adb", fake):
            seen["ok"] = sms.send(number, body)
        return seen

    def test_it_calls_the_telephony_service_directly(self):
        args = self._args()["args"]
        assert args[:5] == ["shell", "service", "call", "isms", sms.SEND_TXN]

    def test_nothing_is_started_on_the_screen(self):
        """No activity, no intent, no app. A send that took focus would take
        it from the screen the walk is standing on."""
        args = " ".join(self._args()["args"])
        assert "am start" not in args and "activity" not in args

    def test_the_number_arrives_as_bare_digits(self):
        """Whatever shape the secrets file was written in."""
        args = self._args(number="+1 (954) 540-5276")["args"]
        assert "9545405276" in args
        assert "+1 (954) 540-5276" not in args

    def test_a_body_with_spaces_survives_the_devices_own_shell(self):
        import shlex

        args = self._args(body="inMyTeam sign-in code 120740")["args"]
        # `adb shell` joins these into a command line for sh ON THE PHONE.
        # What that sh will hand to `service` is what matters.
        assert shlex.split(" ".join(args)).count("inMyTeam sign-in code "
                                                 "120740") == 1

    def test_the_argument_list_matches_the_aidl(self):
        """Ten arguments in the order sendTextForSubscriber declares them.
        Getting this wrong sends somebody else's field as the message."""
        args = self._args(number="9545405276", body="hi")["args"]
        assert args[5:] == [
            "i32", sms.DEFAULT_SUB_ID,
            "s16", sms.SEND_PKG,
            "s16", "null",          # callingAttributionTag
            "s16", "9545405276",    # destAddr
            "s16", "null",          # scAddr
            "s16", "hi",            # text
            "s16", "null",          # sentIntent
            "s16", "null",          # deliveryIntent
            "i32", "0",             # persistMessageForNonDefaultSmsApp
            "i64", "0",             # messageId
        ]

    def test_a_clean_parcel_is_the_only_success(self):
        assert self._args(out="Result: Parcel(00000000    '....')")["ok"]

    def test_an_exception_parcel_is_not(self):
        """What a wrong transaction number looks like on a phone whose
        framework numbers these methods differently — the failure this will
        actually meet, when the new device arrives."""
        out = ("Result: Parcel(Error: 0xffffffff \"Unknown transaction\")")
        assert self._args(out=out)["ok"] is False

    def test_silence_is_not_success_either(self):
        assert self._args(out="")["ok"] is False

    def test_no_number_sends_nothing(self):
        called = []
        with patch.object(sms, "_adb", lambda *a, **k: called.append(1) or ""):
            assert sms.send("", "hi") is False
            assert sms.send("nonsense", "hi") is False
            assert sms.send("9545405276", "") is False
        assert not called

    def test_the_number_is_never_logged(self, caplog):
        """A log on this machine is read over somebody's shoulder. The
        failure is worth a line; who it was for is not."""
        with caplog.at_level("WARNING"), patch.object(sms, "_adb",
                                                      lambda *a, **k: "no"):
            sms.send("9545405276", "hi")
        assert "9545405276" not in caplog.text


class TestWhoTheCodeGoesTo:
    """Three people's personal mobile numbers. They live in the 0600 secrets
    file and NOT in this repository, because a git history is the one place a
    phone number cannot be taken back out of."""

    def _people(self, value):
        from apt_log.secrets import CODE_RECIPIENTS, MemorySecretProvider

        return sms.recipients(MemorySecretProvider(**{CODE_RECIPIENTS: value}))

    def test_labelled_entries_read_as_their_numbers(self):
        """The names are for whoever edits the file. Nothing uses them."""
        assert self._people("A:9545405276,B:7863568328") == [
            "9545405276", "7863568328"]

    def test_bare_numbers_work_too(self):
        assert self._people("9545405276, 7863568328") == [
            "9545405276", "7863568328"]

    def test_punctuation_people_actually_type_is_tolerated(self):
        assert self._people("Jon:(954) 540-5276") == ["9545405276"]

    def test_the_same_person_twice_is_one_text(self):
        assert self._people("A:9545405276,B:954-540-5276") == ["9545405276"]

    def test_and_with_a_country_code_is_still_the_same_person(self):
        """The shape people actually paste. A list where these are two is a
        list that texts one person twice and reports two recipients."""
        assert self._people("A:+1 954-540-5276,B:9545405276") == [
            "9545405276"]

    def test_a_number_that_is_not_this_country_is_left_alone(self):
        """Eleven digits beginning with 1 is the only shape collapsed.
        Guessing at other country codes would break a number to tidy it."""
        assert self._people("A:+44 7700 900123") == ["447700900123"]

    def test_something_too_short_to_be_a_number_is_dropped(self):
        """Rather than texting whatever the typo dialled."""
        assert self._people("A:9545405276,B:1234") == ["9545405276"]

    def test_no_list_configured_is_an_empty_list_not_a_crash(self):
        """The shipped state. Absent, nothing is forwarded and the sign-in
        behaves exactly as it did before this existed."""
        from apt_log.secrets import MemorySecretProvider

        assert sms.recipients(MemorySecretProvider()) == []

    def test_an_unreadable_secrets_file_forwards_to_nobody(self):
        class Angry:
            def get(self, _key):
                raise PermissionError("0644")

        assert sms.recipients(Angry()) == []


class TestTheMessageTheyReceive:
    def test_it_carries_the_code(self):
        assert "120740" in sms.FORWARD_TEXT.format(code="120740")

    def test_it_fits_in_one_text(self):
        """`sendTextForSubscriber` sends ONE message and does not split one,
        so a body over the limit arrives truncated — with the code at the
        end, which is the half that would be lost."""
        assert len(sms.FORWARD_TEXT.format(code="120740")) <= sms.SMS_ONE_PART

    def test_it_is_plain_ascii(self):
        """Not a style rule. One non-GSM character — an em dash, a curly
        quote, an accent — switches the whole message to UCS-2 and drops the
        limit from 160 characters to 70. Every other line in that module is
        written with em dashes; this one cannot be."""
        sms.FORWARD_TEXT.format(code="120740").encode("ascii")

    def test_it_names_the_app_so_two_codes_can_be_told_apart(self):
        assert "inMyTeam" in sms.FORWARD_TEXT

    def test_it_says_the_code_is_short_lived(self):
        """The failure this feature exists to prevent is somebody typing a
        code that expired while they went looking for it."""
        assert "expires" in sms.FORWARD_TEXT

    def test_it_says_nothing_about_a_patient_or_a_visit(self):
        """It lands on three lock screens. The argument that keeps all of
        that out of a push notification keeps it out of a text."""
        words = sms.FORWARD_TEXT.lower()
        for leak in ("patient", "visit", "client", "check", "address"):
            assert leak not in words


class TestForwardingHappensOncePerCode:
    """The notification storm, and the shape of its fix. That guard lived in
    a process's memory and every deploy restarted the process; this one is a
    message's own timestamp, written to disk."""

    def _send_to(self, code="120740", when=1000.0, people=("5551110000",
                                                           "5552220000")):
        sent = []
        with patch.object(sms, "recipients", lambda *a, **k: list(people)), \
                patch.object(sms, "send",
                             lambda n, b, s=None: sent.append((n, b)) or True):
            count = sms.forward_code(code, when)
        return count, sent

    def test_everyone_on_the_list_gets_it(self):
        count, sent = self._send_to()
        assert count == 2
        assert [n for n, _ in sent] == ["5551110000", "5552220000"]
        assert all("120740" in b for _, b in sent)

    def test_the_same_message_is_never_forwarded_twice(self):
        self._send_to(when=1000.0)
        count, sent = self._send_to(when=1000.0)
        assert count == 0 and sent == []

    def test_a_new_code_after_it_does_go(self):
        self._send_to(code="111111", when=1000.0)
        count, _ = self._send_to(code="222222", when=1100.0)
        assert count == 2

    def test_a_code_older_than_the_last_one_is_not_resent(self):
        """Which is what a second read of an inbox that has moved on looks
        like."""
        self._send_to(when=1100.0)
        count, _ = self._send_to(when=1000.0)
        assert count == 0

    def test_no_recipients_sends_nothing_and_marks_nothing(self):
        """So configuring the list later still forwards the next code."""
        with patch.object(sms, "recipients", lambda *a, **k: []):
            assert sms.forward_code("120740", 1000.0) == 0
        assert not sms.already_forwarded(1000.0)

    def test_the_mark_is_written_before_the_texts_go(self):
        """If this process dies halfway through three texts, somebody misses
        one code. If the mark waited until after, the next tick would start
        the list again — which is how one code becomes six texts, and the
        storm is the worse failure of the two."""
        with patch.object(sms, "recipients", lambda *a, **k: ["5551110000"]), \
                patch.object(sms, "send",
                             side_effect=RuntimeError("carrier")):
            with pytest.raises(RuntimeError):
                sms.forward_code("120740", 1000.0)
        assert sms.already_forwarded(1000.0)

    def test_an_empty_code_is_not_a_message(self):
        with patch.object(sms, "recipients", lambda *a, **k: ["5551110000"]):
            assert sms.forward_code("", 1000.0) == 0

    def test_a_mark_from_a_previous_boot_still_counts(self):
        """The whole point of it being on disk. This is the notification
        storm's exact fix, stated as a test."""
        sms._mark_forwarded(1000.0)
        importlib.reload(sms)
        try:
            assert sms.already_forwarded(1000.0)
        finally:
            importlib.reload(sms)


class TestTheTickBehindTheWalk:
    """What makes forwarding true of sign-ins the portal never started:
    somebody logging in from their own phone makes inMyTeam text THIS one,
    and nothing else in this system would notice."""

    def _tick(self, code="120740", when=1000.0, **kwargs):
        with patch.object(sms, "_newest", lambda **k: (when, code)), \
                patch.object(sms, "recipients", lambda *a, **k: ["5551110000"]), \
                patch.object(sms, "send", lambda *a, **k: True):
            return sms.forward_any_new(**kwargs)

    def test_a_code_sitting_in_the_inbox_goes_out(self):
        assert self._tick(now=100.0) == 1

    def test_the_phone_is_not_asked_every_tick(self):
        """Every read is an adb subprocess against a phone that is busy
        driving an app."""
        asked = []
        with patch.object(sms, "_newest",
                          lambda **k: asked.append(1) or (0.0, "")):
            sms.forward_any_new(now=100.0)
            sms.forward_any_new(now=101.0)
            sms.forward_any_new(now=100.0 + sms.FORWARD_POLL + 1)
        assert len(asked) == 2

    def test_the_walk_may_skip_the_wait(self):
        """It has just spent a minute waiting for this code and should not
        then wait out a poll interval to pass it on."""
        self._tick(now=100.0)
        assert self._tick(code="222222", when=1100.0, now=101.0,
                          force=True) == 1

    def test_but_forcing_does_not_skip_the_dedup(self):
        self._tick(when=1000.0, now=100.0)
        assert self._tick(when=1000.0, now=101.0, force=True) == 0

    def test_an_empty_inbox_is_not_an_error(self):
        assert self._tick(code="", when=0.0, now=100.0) == 0
