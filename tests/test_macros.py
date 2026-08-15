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
            raise RuntimeError("password Hunter2 rejected for PACIENTE FICTICIA")

        with patch.dict(macros.MACROS,
                        {"t": macros.Macro("t", "macro.t", boom)}), \
             patch("apt_log.resident.run", lambda work: work(MagicMock())), \
             patch("apt_log.ui.mirror.publish"):
            status = self._runner(tmp_path).execute("t", "r1")

        assert status.state == "failed"
        assert status.error == "RuntimeError"
        blob = json.dumps(status.__dict__)
        for secret in ("Hunter2", "PACIENTE", "FICTICIA"):
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

    def test_the_landing_screen_is_not_the_macros_business(self):
        """It used to wait for agency-or-home and then choose the agency. The
        owner's correction: the button handles auth and nothing else — where
        the app lands after sign-in, and which agency to enter, are hers."""
        import inspect
        source = inspect.getsource(macros._hhax_legacy_login)
        assert "AgencyScreen" not in source
        assert "AGENCY_NAME" not in source


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


class TestOpenMacros:
    """One per app, and they only bring the app to the front.

    For an app she is already signed into, Android's own state keeping makes
    this the whole of switching to it. For the three apps whose sign-in flows
    have never been walked, it is also the most a button may honestly do:
    opening a screen nobody has mapped is safe exactly because it does nothing
    on it.
    """

    def test_all_four_apps_have_one(self):
        from apt_log.macros import MACROS
        for name in ("open_hhax_legacy", "open_hhax_uma",
                     "open_mobile_caregiver", "open_inmyteam"):
            assert name in MACROS

    def test_it_activates_and_verifies_the_foreground_package(self):
        from unittest.mock import MagicMock

        from apt_log.macros import MACROS

        driver = MagicMock()
        driver.current_activity = ".Something"
        driver.current_package = "com.hhaexchange.uma"
        MACROS["open_hhax_uma"].run(driver, lambda _k: None)
        driver.activate_app.assert_called_once_with("com.hhaexchange.uma")

    def test_it_fails_rather_than_pretending(self):
        """The wrong app in front must be a failure she can see, not a quiet
        wireframe of something she did not ask for."""
        from unittest.mock import MagicMock, patch as patch_mod

        import pytest as pytest_mod

        from apt_log import macros as macros_mod
        from apt_log.macros import MACROS

        driver = MagicMock()
        driver.current_activity = ".Something"
        driver.current_package = "com.wrong.app"
        with patch_mod.object(macros_mod.time, "sleep"), \
             patch_mod.object(macros_mod.time, "monotonic",
                              side_effect=[i * 0.5 for i in range(200)]):
            with pytest_mod.raises(RuntimeError):
                MACROS["open_hhax_uma"].run(driver, lambda _k: None)


class TestAuthOnlyMacro:
    """The button handles auth and nothing else.

    The owner's correction, verbatim: sign in only when auth inputs are on
    screen, never pick the agency — which agency to enter is her tap. And a
    signed-in app pressed again must cost nothing but the app coming forward.
    """

    def _run(self, driver, steps, auth_result, still_login_after=False):
        from unittest.mock import patch as patch_mod

        from apt_log.macros import MACROS

        with patch_mod("apt_log.screens.session.modal_message",
                       return_value=""), \
             patch_mod("apt_log.screens.login.authenticate_if_needed",
                       return_value=auth_result) as auth, \
             patch_mod("apt_log.screens.login.LoginScreen") as login_cls, \
             patch_mod("apt_log.macros.dismiss_autofill", return_value=True), \
             patch_mod("apt_log.macros.time.sleep"):
            login_cls.return_value.is_displayed.return_value = still_login_after
            MACROS["hhax_legacy_login"].run(driver, steps.append)
        return auth

    def test_a_signed_in_app_costs_nothing_but_coming_forward(self):
        from unittest.mock import MagicMock

        driver = MagicMock()
        steps = []
        self._run(driver, steps, auth_result=False)
        assert steps == ["macro.step.launching", "macro.step.clearing",
                         "macro.step.signing_in"]

    def test_auth_runs_when_the_password_field_is_there(self):
        from unittest.mock import MagicMock

        driver = MagicMock()
        steps = []
        auth = self._run(driver, steps, auth_result=True)
        auth.assert_called_once()
        assert steps[-1] == "macro.step.checking"

    def test_the_agency_is_never_chosen_for_her(self):
        """Which agency to enter is a decision, and decisions are her taps.
        The macro's steps must not contain the agency step at all."""
        from unittest.mock import MagicMock

        driver = MagicMock()
        steps = []
        self._run(driver, steps, auth_result=True)
        assert "macro.step.agency" not in steps

    def test_credentials_that_do_not_take_are_a_failure_not_a_shrug(self):
        import itertools
        from unittest.mock import MagicMock, patch as patch_mod

        import pytest as pytest_mod

        from apt_log.macros import MACROS

        driver = MagicMock()
        with patch_mod("apt_log.screens.session.modal_message",
                       return_value=""), \
             patch_mod("apt_log.screens.login.authenticate_if_needed",
                       return_value=True), \
             patch_mod("apt_log.screens.login.LoginScreen") as login_cls, \
             patch_mod("apt_log.macros.dismiss_autofill", return_value=True), \
             patch_mod("apt_log.macros.time.sleep"), \
             patch_mod("apt_log.macros.time.monotonic",
                       side_effect=itertools.count(step=0.5)):
            login_cls.return_value.is_displayed.return_value = True
            with pytest_mod.raises(RuntimeError, match="still on the sign-in"):
                MACROS["hhax_legacy_login"].run(driver, lambda _k: None)

    def test_home_under_a_modal_is_not_signed_in(self):
        """The expiry alert lands on top of HomeActivity; an unrecognised
        dialog is a stop, not something the macro may click through."""
        from unittest.mock import MagicMock, patch as patch_mod

        import pytest as pytest_mod

        from apt_log.macros import MACROS

        driver = MagicMock()
        with patch_mod("apt_log.screens.session.modal_message",
                       return_value="¿Seguro que desea salir?"), \
             patch_mod("apt_log.screens.session.is_expired",
                       return_value=False), \
             patch_mod("apt_log.macros.time.sleep"):
            with pytest_mod.raises(RuntimeError, match="unrecognised"):
                MACROS["hhax_legacy_login"].run(driver, lambda _k: None)


class TestAutoAuth:
    """Landing on the sign-in screen IS the request to sign in.

    The app expires its session mid-use; the alert's only button lands on a
    screen the portal will not photograph and nobody may type into. The
    runner signs in without being asked — for the one app whose sequence is
    proven, only on a fresh sighting, and behind a cooldown that keeps a
    bad-credential day from becoming a loop.
    """

    def _doc(self, tmp_path, app="com.hhaexchange.caregiver", screen="login",
             age=0.0, blocked=""):
        import datetime as dt

        path = tmp_path / "screen.json"
        at = (dt.datetime.now() - dt.timedelta(seconds=age)).isoformat()
        path.write_text(json.dumps({"app": app, "screen": screen,
                                    "blocked": blocked, "at": at}))
        return path

    def _runner(self, tmp_path):
        return macros.Runner(tmp_path / "req.json", tmp_path / "status.json",
                             screen_path=tmp_path / "screen.json")

    def test_a_fresh_login_screen_triggers_sign_in(self, tmp_path):
        self._doc(tmp_path)
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_auto_auth() is True
        assert execute.call_args.args[0] == "hhax_legacy_login"

    def test_the_cooldown_prevents_a_retry_storm(self, tmp_path):
        self._doc(tmp_path)
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute"):
            assert runner.maybe_auto_auth() is True
            assert runner.maybe_auto_auth() is False

    def test_a_stale_sighting_is_not_a_landing(self, tmp_path):
        """Screens flash through login during startup, and an old document
        may describe a screen long gone."""
        self._doc(tmp_path, age=macros.AUTO_AUTH_FRESH + 5)
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_auto_auth() is False
        execute.assert_not_called()

    @pytest.mark.parametrize("app,screen", [
        ("com.hhaexchange.uma", "login"),      # no proven sequence
        ("com.hhaexchange.caregiver", "home"),  # not a login screen
    ])
    def test_only_the_proven_apps_login_screen(self, tmp_path, app, screen):
        self._doc(tmp_path, app=app, screen=screen)
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_auto_auth() is False
        execute.assert_not_called()

    def test_auth_inputs_are_the_signal_not_the_activity_name(self, tmp_path):
        """The flight recorder's fourth-ever entry: this app hosts its login
        form under an activity classified "startup". The capture refusal says
        credentials are on screen; that is the owner's rule taken literally."""
        self._doc(tmp_path, screen="startup", blocked="password_field")
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_auto_auth() is True
        execute.assert_called_once()
