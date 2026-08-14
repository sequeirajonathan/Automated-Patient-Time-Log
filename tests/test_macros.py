"""Named sequences, and the things they are not allowed to be.

Most of these hold one line: the page asks for a name from a list, and the list
lives in code. A macro that took steps from a browser would be arbitrary remote
scripting with a friendlier label.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from apt_log import macros


class TestRegistry:
    def test_a_macro_is_asked_for_by_name(self, tmp_path):
        rid = macros.request("hhax_legacy_login", tmp_path / "req.json")
        assert rid
        taken = macros.take_request(tmp_path / "req.json")
        assert taken["name"] == "hhax_legacy_login"

    def test_an_unknown_name_is_refused_at_the_source(self, tmp_path):
        """Not validated on the way out — refused on the way in, so nothing
        unknown is ever written to a file the runner will read."""
        with pytest.raises(KeyError):
            macros.request("rm -rf", tmp_path / "req.json")
        assert not (tmp_path / "req.json").exists()

    def test_a_request_naming_something_unknown_is_dropped(self, tmp_path):
        """Belt and braces: the file is on disk between two processes."""
        path = tmp_path / "req.json"
        path.write_text(json.dumps({"id": "x", "name": "nope", "at": time.time()}),
                        encoding="utf-8")
        assert macros.take_request(path) is None

    def test_every_macro_carries_a_translation_key_not_a_label(self):
        """The UI never receives English from this module."""
        for macro in macros.MACROS.values():
            assert macro.label_key.startswith("macro.")


class TestRequestLifetime:
    def test_a_request_is_claimed_once(self, tmp_path):
        """Two runners, or one restarting, must not both sign in."""
        path = tmp_path / "req.json"
        macros.request("hhax_legacy_login", path)
        assert macros.take_request(path) is not None
        assert macros.take_request(path) is None

    def test_a_stale_request_is_ignored(self, tmp_path):
        """The feed can be down when a button is pressed. A sign-in that fires
        when the process comes back minutes later is a surprise on a phone
        nobody is holding."""
        path = tmp_path / "req.json"
        path.write_text(json.dumps({
            "id": "x", "name": "hhax_legacy_login",
            "at": time.time() - macros.REQUEST_MAX_AGE - 10,
        }), encoding="utf-8")
        assert macros.take_request(path) is None

    def test_no_request_is_not_an_error(self, tmp_path):
        assert macros.take_request(tmp_path / "absent.json") is None


class TestExecution:
    def _runner(self, tmp_path):
        return macros.Runner(tmp_path / "req.json", tmp_path / "status.json")

    def test_progress_is_published_as_it_goes(self, tmp_path):
        seen = []

        def fake(_driver, report):
            report("macro.step.launching")
            seen.append(macros.read_status(tmp_path / "status.json").step)
            report("macro.step.signing_in")
            seen.append(macros.read_status(tmp_path / "status.json").step)

        with patch.dict(macros.MACROS,
                        {"t": macros.Macro("t", "macro.t", fake)}), \
             patch.object(macros, "read_status", macros.read_status), \
             patch("apt_log.resident.run", lambda work: work(MagicMock())), \
             patch("apt_log.ui.mirror.publish"):
            self._runner(tmp_path).execute("t", "r1")

        assert seen == ["macro.step.launching", "macro.step.signing_in"]

    def test_a_finished_macro_says_so(self, tmp_path):
        with patch.dict(macros.MACROS,
                        {"t": macros.Macro("t", "macro.t", lambda d, r: None)}), \
             patch("apt_log.resident.run", lambda work: work(MagicMock())), \
             patch("apt_log.ui.mirror.publish"):
            status = self._runner(tmp_path).execute("t", "r1")
        assert status.state == "done"
        assert macros.read_status(tmp_path / "status.json").state == "done"

    def test_a_failure_is_reported_without_leaking_its_message(self, tmp_path):
        """The status is shown to her. Macro failures are structural, so the
        exception type is the useful part and the message is where a credential
        or a patient name would ride along."""
        def boom(_driver, _report):
            raise RuntimeError("password Hunter2 rejected for CARIDAD ROJAS")

        with patch.dict(macros.MACROS,
                        {"t": macros.Macro("t", "macro.t", boom)}), \
             patch("apt_log.resident.run", lambda work: work(MagicMock())), \
             patch("apt_log.ui.mirror.publish"):
            status = self._runner(tmp_path).execute("t", "r1")

        assert status.state == "failed"
        assert status.error == "RuntimeError"
        blob = json.dumps(status.__dict__)
        for secret in ("Hunter2", "CARIDAD", "ROJAS"):
            assert secret not in blob

    def test_no_session_is_a_failure_not_a_crash(self, tmp_path):
        with patch.dict(macros.MACROS,
                        {"t": macros.Macro("t", "macro.t", lambda d, r: None)}), \
             patch("apt_log.resident.run",
                   side_effect=RuntimeError("no Appium session available")), \
             patch("apt_log.ui.mirror.publish"):
            status = self._runner(tmp_path).execute("t", "r1")
        assert status.state == "failed"


class TestTheLineMacrosDoNotCross:
    def test_no_macro_clocks_in(self):
        """Signing in is twelve taps with no judgement in them. Clocking in is
        one tap that produces a record and, if wrong, a call from the agency.
        The line is about which mistakes are recoverable, not which are hard.
        """
        import inspect
        from apt_log import macros as mod

        source = inspect.getsource(mod)
        for forbidden in ("btn_clock_in", "clock_in", "choose_verification",
                          "open_verification_chooser"):
            assert forbidden not in source

    def test_a_macro_refuses_an_unrecognised_dialog(self):
        """Same rule the session module holds. A macro is not an excuse to tap
        through something nothing has read."""
        from apt_log.screens import session as session_mod

        driver = MagicMock()
        with patch.object(session_mod, "modal_message", return_value="¿Borrar todo?"), \
             patch.object(session_mod, "is_expired", return_value=False):
            with pytest.raises(RuntimeError, match="unrecognised"):
                macros._hhax_legacy_login(driver, lambda _s: None)


class TestWaiting:
    """The macro is faster than a person reading output, which is the bug.

    The first live run submitted credentials, asked whether the agency screen was
    showing before the app had drawn it, skipped the selection, and failed the
    home check — with the phone sitting on the agency screen the whole time. The
    manual walkthrough that "proved" the steps had prints between them, and that
    delay was enough to hide it.
    """

    def test_it_waits_rather_than_asking_once(self):
        answers = [False, False, True]
        assert macros.wait_for(lambda: answers.pop(0), timeout=5, poll=0.01) is True

    def test_it_gives_up_rather_than_hanging(self):
        assert macros.wait_for(lambda: False, timeout=0.05, poll=0.01) is False

    def test_a_screen_mid_transition_reads_as_not_yet(self):
        """Reading a hierarchy while it is being rebuilt raises. That is "not
        ready", not "broken", and treating it as an error would abandon a macro
        that was about to succeed."""
        state = {"n": 0}

        def flaky():
            state["n"] += 1
            if state["n"] < 3:
                raise RuntimeError("stale element")
            return True

        assert macros.wait_for(flaky, timeout=5, poll=0.01) is True

    def test_either_landing_screen_is_accepted(self):
        """One agency goes straight to home; several stop to ask. Waiting for
        the agency picker specifically would hang on the single-agency case."""
        import inspect
        source = inspect.getsource(macros._hhax_legacy_login)
        assert "screen.is_displayed() or home.is_displayed()" in source


class TestAutofillDialog:
    """Android's own "Save password?" prompt.

    It appears over the app the instant credentials are submitted. Because it
    belongs to the system rather than the app, nothing in screens/session.py was
    looking for it — so the sign-in macro reached the agency step and then waited
    twenty seconds at a screen that was never going to change.
    """

    def test_it_is_declined_when_present(self):
        driver = MagicMock()
        button = MagicMock()
        driver.find_elements.return_value = [button]
        assert macros.dismiss_autofill(driver) is True
        button.click.assert_called_once()

    def test_nothing_happens_when_it_is_absent(self):
        driver = MagicMock()
        driver.find_elements.return_value = []
        assert macros.dismiss_autofill(driver) is False

    def test_it_declines_rather_than_accepts(self):
        """Storing the agency password in the phone's autofill is a credential
        decision, and taking it silently in the affirmative on someone else's
        work phone is not this code's call."""
        assert macros.AUTOFILL_DECLINE.endswith("autofill_save_no")
        import inspect
        assert "autofill_save_yes" not in inspect.getsource(macros)
