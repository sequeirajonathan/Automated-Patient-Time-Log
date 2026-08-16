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

    def _watching(self, tmp_path, n=1):
        path = tmp_path / "viewers.json"
        path.write_text(json.dumps({"n": n}))
        return path

    def _secrets(self):
        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)

        return MemorySecretProvider(**{APP_USERNAME: "u", APP_PASSWORD: "p"})

    def _runner(self, tmp_path):
        return macros.Runner(tmp_path / "req.json", tmp_path / "status.json",
                             screen_path=tmp_path / "screen.json",
                             viewers_path=self._watching(tmp_path),
                             secrets=self._secrets())

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
        ("com.inmyteam.inmyteam", "login"),     # no auth flow mapped
        ("com.hhaexchange.caregiver", "home"),  # not a login screen
    ])
    def test_only_the_proven_apps_login_screen(self, tmp_path, app, screen):
        self._doc(tmp_path, app=app, screen=screen)
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_auto_auth() is False
        execute.assert_not_called()

    def test_the_uma_login_screen_fires_its_own_macro(self, tmp_path):
        """The legacy credentials serve both HHAeXchange apps, so landing on
        HHAeXchange+'s auth screen signs in with nothing extra configured."""
        self._doc(tmp_path, app="com.hhaexchange.uma", screen="login")
        runner = self._runner(tmp_path)
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_auto_auth() is True
        assert execute.call_args.args[0] == "hhax_uma_login"

    def test_nobody_watching_means_nothing_fires(self, tmp_path):
        """Unwatched, the app's inactivity timer signs the session back out
        and the two loop all night. Signing in is a service to a viewer, not
        a state the phone owes the void."""
        self._doc(tmp_path)
        runner = macros.Runner(tmp_path / "req.json", tmp_path / "status.json",
                               screen_path=tmp_path / "screen.json",
                               viewers_path=self._watching(tmp_path, n=0),
                               secrets=self._secrets())
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_auto_auth() is False
        execute.assert_not_called()

    def test_a_dead_uis_stale_claim_counts_as_nobody(self, tmp_path):
        """The file says someone is watching, but its mtime is old — the UI
        that wrote it is gone. A crashed process must not hold the gate open."""
        import os

        self._doc(tmp_path)
        viewers = self._watching(tmp_path, n=2)
        old = __import__("time").time() - macros.VIEWERS_FRESH - 10
        os.utime(viewers, (old, old))
        runner = macros.Runner(tmp_path / "req.json", tmp_path / "status.json",
                               screen_path=tmp_path / "screen.json",
                               viewers_path=viewers, secrets=self._secrets())
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


class TestLaunchSteadiness:
    """What the owner reported as flicker, held at the source."""

    def test_the_display_is_woken_before_the_app_is_driven(self):
        """Appium happily drives an app with the display off — the sketch
        showed a homepage while the physical phone was black."""
        import inspect

        for name in ("hhax_legacy_login", "open_hhax_uma"):
            source = inspect.getsource(macros.MACROS[name].run)
            assert "wake_display()" in source

    def test_auto_auth_never_stacks_onto_a_running_macro(self, tmp_path):
        import datetime as dt

        (tmp_path / "screen.json").write_text(json.dumps({
            "app": "com.hhaexchange.caregiver", "screen": "login",
            "blocked": "", "at": dt.datetime.now().isoformat()}))
        (tmp_path / "viewers.json").write_text(json.dumps({"n": 1}))
        macros.write_status(macros.Status(state="running"),
                            tmp_path / "status.json")
        runner = macros.Runner(tmp_path / "req.json", tmp_path / "status.json",
                               screen_path=tmp_path / "screen.json",
                               viewers_path=tmp_path / "viewers.json",
                               secrets=TestAutoAuth()._secrets())
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_auto_auth() is False
        execute.assert_not_called()

    def test_nor_onto_one_that_just_finished(self, tmp_path):
        """The tile's own walk leaves the login screen visible in the lagging
        screen document for a beat after it finishes — a second auth on top
        was the "why is it signing in twice"."""
        import datetime as dt

        (tmp_path / "screen.json").write_text(json.dumps({
            "app": "com.hhaexchange.caregiver", "screen": "login",
            "blocked": "", "at": dt.datetime.now().isoformat()}))
        (tmp_path / "viewers.json").write_text(json.dumps({"n": 1}))
        macros.write_status(macros.Status(state="done"),
                            tmp_path / "status.json")
        runner = macros.Runner(tmp_path / "req.json", tmp_path / "status.json",
                               screen_path=tmp_path / "screen.json",
                               viewers_path=tmp_path / "viewers.json",
                               secrets=TestAutoAuth()._secrets())
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_auto_auth() is False
        execute.assert_not_called()


class TestAuthMacroFor:
    """An app gets automatic sign-in only once its secrets are on the device.

    An auth macro without its credentials fails on every auto-auth attempt,
    ninety seconds apart, for as long as someone watches. Gating on secret
    presence keeps an uncredentialed app's tile honest — the macro is still
    offered for a manual press, but the background loop never starts.
    """

    def _full_provider(self):
        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME, MC_PIN,
                                     UMA_PASSWORD, UMA_USERNAME,
                                     MemorySecretProvider)

        return MemorySecretProvider(**{
            APP_USERNAME: "u", APP_PASSWORD: "p",
            UMA_USERNAME: "e", UMA_PASSWORD: "w", MC_PIN: "1234"})

    def test_each_credentialed_app_maps_to_its_own_macro(self):
        provider = self._full_provider()
        assert (macros.auth_macro_for("com.hhaexchange.caregiver", provider)
                == "hhax_legacy_login")
        assert (macros.auth_macro_for("com.hhaexchange.uma", provider)
                == "hhax_uma_login")
        assert (macros.auth_macro_for("com.tellus.evv.v2", provider)
                == "mobile_caregiver_pin")

    def test_a_missing_secret_withholds_the_macro(self):
        """Only the legacy credentials exist: the PIN app stays manual."""
        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)

        provider = MemorySecretProvider(**{APP_USERNAME: "u",
                                           APP_PASSWORD: "p"})
        assert (macros.auth_macro_for("com.hhaexchange.caregiver", provider)
                == "hhax_legacy_login")
        assert macros.auth_macro_for("com.tellus.evv.v2", provider) is None

    def test_the_legacy_credentials_serve_both_hhaexchange_apps(self):
        """One account fronts both apps — the owner's word. The legacy
        credentials already on the device credential HHAeXchange+ too, so
        it auto-signs-in with nothing more configured."""
        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)

        provider = MemorySecretProvider(**{APP_USERNAME: "u",
                                           APP_PASSWORD: "p"})
        assert (macros.auth_macro_for("com.hhaexchange.uma", provider)
                == "hhax_uma_login")

    def test_half_a_credential_pair_is_not_enough(self):
        from apt_log.secrets import UMA_USERNAME, MemorySecretProvider

        provider = MemorySecretProvider(**{UMA_USERNAME: "e"})
        assert macros.auth_macro_for("com.hhaexchange.uma", provider) is None

    def test_an_app_without_an_auth_flow_gets_none(self):
        """inMyTeam keeps its session alive on its own; nothing is mapped."""
        provider = self._full_provider()
        assert macros.auth_macro_for("com.inmyteam.inmyteam", provider) is None
        assert macros.auth_macro_for("", provider) is None


class TestUmaLogin:
    """HHAeXchange+ signs in through a web form — and only when asked to."""

    def _driver(self, activity):
        driver = MagicMock()
        driver.current_activity = activity
        driver.current_package = "com.hhaexchange.uma"
        return driver

    def test_an_alive_session_is_left_alone(self):
        """A substantive screen with no auth ask on it means signed in:
        nothing is ever tapped. (The credentials are read up front — before
        any tap, which is the doctrine — but reading a file is not an
        action on the phone.)"""
        import itertools

        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)

        driver = self._driver("com.hhaexchange.uma.MainActivity")
        driver.find_elements.return_value = []      # no expiry dialog either
        driver.page_source = '<a clickable="true"/>' * 5   # a real screen
        provider = MemorySecretProvider(**{APP_USERNAME: "u",
                                           APP_PASSWORD: "p"})
        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider",
                   return_value=provider), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            macros.MACROS["hhax_uma_login"].run(driver, lambda _k: None)
        assert not any(
            c for c in driver.method_calls if c[0].endswith(".click"))

    def test_the_expiry_dialog_is_walked_through_to_the_form(self):
        """'Desconectado — se ha cerrado la sesión': the dialog's only exit
        leads to login, so the macro takes it and carries on to the form."""
        import itertools

        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)

        driver = self._driver("com.hhaexchange.uma.HomeActivity")
        state = {"dismissed": False}
        exit_btn = MagicMock()

        def dismiss():
            state["dismissed"] = True
        exit_btn.click.side_effect = dismiss

        def find_elements(_by, selector):
            if "Regresar al inicio" in selector:
                return [] if state["dismissed"] else [exit_btn]
            if 'resource-id=' in selector and state["dismissed"]:
                return [MagicMock()]
            if "Iniciar sesi" in selector and state["dismissed"]:
                return [MagicMock()]
            return []
        driver.find_elements.side_effect = find_elements

        provider = MemorySecretProvider(**{APP_USERNAME: "u",
                                           APP_PASSWORD: "p"})
        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider",
                   return_value=provider), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            try:
                macros.MACROS["hhax_uma_login"].run(driver, lambda _k: None)
            except RuntimeError:
                pass    # the mock form is shallow past the fields
        exit_btn.click.assert_called_once()

    def test_missing_credentials_stop_the_macro_before_any_tap(self):
        """The secrets are read before the first tap, so an uncredentialed
        run fails cleanly on a screen it has not touched."""
        import itertools

        from apt_log.secrets import MemorySecretProvider, SecretNotFound

        driver = self._driver("com.hhaexchange.uma.AuthActivity")
        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider",
                   return_value=MemorySecretProvider()), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            with pytest.raises(SecretNotFound):
                macros.MACROS["hhax_uma_login"].run(driver, lambda _k: None)
        driver.find_elements.assert_not_called()

    def test_the_legacy_credentials_are_the_fallback(self):
        """One account fronts both HHAeXchange apps: with only the legacy
        credentials on the device, the walk still begins — it does not stop
        at a missing-secret error."""
        import itertools

        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)

        driver = self._driver("com.hhaexchange.uma.AuthActivity")
        provider = MemorySecretProvider(**{APP_USERNAME: "u",
                                           APP_PASSWORD: "p"})
        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider",
                   return_value=provider), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            try:
                macros.MACROS["hhax_uma_login"].run(driver, lambda _k: None)
            except RuntimeError:
                pass    # the mock driver's form is shallow; that is fine —
                        # what matters is the secrets were satisfied
        driver.find_elements.assert_called()


class TestMobileCaregiverPin:
    """Mobile Caregiver+ unlocks by tapping its own keypad."""

    def test_the_pin_is_typed_digit_by_digit(self):
        import itertools
        from unittest.mock import PropertyMock

        from apt_log.secrets import MC_PIN, MemorySecretProvider

        state = {"activity": "com.tellus.evv.v2.PinActivity"}
        pressed = []
        driver = MagicMock()
        type(driver).current_activity = PropertyMock(
            side_effect=lambda: state["activity"])

        def find_elements(_by, selector):
            digit = selector.split('"')[1]
            button = MagicMock()

            def click(d=digit):
                pressed.append(d)
                if len(pressed) == 4:      # the screen advances by itself
                    state["activity"] = "com.tellus.evv.v2.HomeActivity"
            button.click.side_effect = click
            return [button]

        driver.find_elements.side_effect = find_elements
        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider",
                   return_value=MemorySecretProvider(**{MC_PIN: "2580"})), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            macros.MACROS["mobile_caregiver_pin"].run(driver, lambda _k: None)
        assert pressed == ["2", "5", "8", "0"]

    def test_an_unlocked_app_is_left_alone(self):
        import itertools

        driver = MagicMock()
        driver.current_activity = "com.tellus.evv.v2.HomeActivity"
        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider") as provider_cls, \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            macros.MACROS["mobile_caregiver_pin"].run(driver, lambda _k: None)
        provider_cls.assert_not_called()
        driver.find_elements.assert_not_called()


class TestUmaWebFormIsTheAsk:
    """Reported live: the tile 'didn't even try'. The macro had run, seen an
    activity that was not the app's 'auth' one, and reported done — while
    the phone sat on the sign-in form, which lives in a Chrome Custom Tab
    under Chrome's own activity name. The form in front IS the ask."""

    def test_a_form_already_in_chrome_is_filled_not_skipped(self):
        import itertools

        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)

        driver = MagicMock()
        driver.current_activity = "org.chromium.chrome.CustomTabActivity"
        driver.current_package = "com.android.chrome"
        provider = MemorySecretProvider(**{APP_USERNAME: "u",
                                           APP_PASSWORD: "p"})
        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider",
                   return_value=provider), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            try:
                macros.MACROS["hhax_uma_login"].run(driver, lambda _k: None)
            except RuntimeError:
                pass    # the mock form is shallow past the fields

        selectors = [c.args[1] for c in driver.find_elements.call_args_list]
        # It went for the form's fields directly — and never hunted for the
        # app's sign-in control inside Chrome, which is not there to find.
        assert any('resource-id="email"' in s for s in selectors)
        assert not any("Iniciar sesi" in s for s in selectors)


class TestChromeIsTheUmaFormSometimes:
    """HHAeXchange+'s session expires into a Chrome Custom Tab. A login
    screen sitting in Chrome is invisible to a package-only rule — the
    session died overnight and nobody was signed back in until a human
    tapped. The screen document's own words say whose form it is."""

    def _provider(self):
        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)
        return MemorySecretProvider(**{APP_USERNAME: "u", APP_PASSWORD: "p"})

    def test_chrome_on_the_hhaexchange_form_arms_the_uma_macro(self):
        doc = {"statics": [{"txt": "secure.hhaexchange.com"}],
               "elements": []}
        assert macros.auth_macro_for("com.android.chrome", self._provider(),
                                     doc=doc) == "hhax_uma_login"

    def test_chrome_anywhere_else_is_nobody(self):
        doc = {"statics": [{"txt": "accounts.example.com"}], "elements": []}
        assert macros.auth_macro_for("com.android.chrome", self._provider(),
                                     doc=doc) is None

    def test_chrome_without_a_document_is_nobody(self):
        assert macros.auth_macro_for("com.android.chrome",
                                     self._provider()) is None

    def test_the_expired_session_in_chrome_fires_auto_auth(self, tmp_path):
        import datetime as dt

        (tmp_path / "screen.json").write_text(json.dumps({
            "app": "com.android.chrome", "screen": "unknown",
            "blocked": "password_field",
            "at": dt.datetime.now().isoformat(),
            "statics": [{"txt": "secure.hhaexchange.com"}],
            "elements": []}))
        (tmp_path / "viewers.json").write_text(json.dumps({"n": 1}))
        runner = macros.Runner(tmp_path / "req.json", tmp_path / "status.json",
                               screen_path=tmp_path / "screen.json",
                               viewers_path=tmp_path / "viewers.json",
                               secrets=self._provider())
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_auto_auth() is True
        assert execute.call_args.args[0] == "hhax_uma_login"


class TestExpiryDialogsAreTheAsk:
    """The phone parks on 'your session ended' until a human taps — the
    owner's screenshot, every morning. A dialog whose wording is recognised
    as expiry is itself the request to sign back in; its only meaningful
    exit leads to the login screen, and the app's auth macro knows the way
    from there. Unrecognised dialogs stay untouched, as everywhere."""

    def _provider(self):
        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)
        return MemorySecretProvider(**{APP_USERNAME: "u", APP_PASSWORD: "p"})

    def _runner(self, tmp_path, doc):
        import datetime as dt

        doc.setdefault("at", dt.datetime.now().isoformat())
        (tmp_path / "screen.json").write_text(json.dumps(doc))
        (tmp_path / "viewers.json").write_text(json.dumps({"n": 1}))
        return macros.Runner(tmp_path / "req.json", tmp_path / "status.json",
                             screen_path=tmp_path / "screen.json",
                             viewers_path=tmp_path / "viewers.json",
                             secrets=self._provider())

    def test_the_uma_expiry_dialog_fires_its_auth_macro(self, tmp_path):
        runner = self._runner(tmp_path, {
            "app": "com.hhaexchange.uma", "screen": "home", "blocked": "",
            "statics": [{"txt": "Desconectado"},
                        {"txt": "Se ha cerrado la sesión debido a 15 minutos"},
                        {"txt": "Regresar al inicio de sesión"}],
            "elements": []})
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_auto_auth() is True
        assert execute.call_args.args[0] == "hhax_uma_login"

    def test_the_legacy_expiry_alert_fires_its_auth_macro(self, tmp_path):
        runner = self._runner(tmp_path, {
            "app": "com.hhaexchange.caregiver", "screen": "home",
            "blocked": "",
            "statics": [{"txt": "Su sesión ha expirado"}], "elements": []})
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_auto_auth() is True
        assert execute.call_args.args[0] == "hhax_legacy_login"

    def test_an_unrecognised_dialog_fires_nothing(self, tmp_path):
        runner = self._runner(tmp_path, {
            "app": "com.hhaexchange.uma", "screen": "home", "blocked": "",
            "statics": [{"txt": "¿Seguro que desea borrar todo?"}],
            "elements": []})
        with patch.object(runner, "execute") as execute:
            assert runner.maybe_auto_auth() is False
        execute.assert_not_called()

    def test_the_login_page_itself_is_not_an_expiry_dialog(self):
        """'Iniciar sesión' appears on the login screen; the legacy marker
        list must not treat a whole login page as a dialog."""
        doc = {"app": "com.hhaexchange.caregiver",
               "statics": [{"txt": "Iniciar sesión"}], "elements": []}
        assert macros.expiry_on_screen(doc) is False


class TestAliveSessionSurvivesTheColdResume:
    """HHAeXchange+ passes THROUGH its auth activity on every cold resume
    while it checks the stored session. The first version read that flash
    as 'asked to sign in' and opened the web form over a live session —
    the owner's report: 'I just left the app, it should still have an
    active session.'"""

    def test_settling_on_home_ends_the_macro_without_a_tap(self):
        import itertools

        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)

        driver = MagicMock()
        driver.current_package = "com.hhaexchange.uma"
        # The resume arc: auth flashes past, then home settles.
        acts = itertools.chain(
            ["com.hhaexchange.uma.AuthenticationActivity"] * 4,
            itertools.repeat("com.hhaexchange.uma.HomeActivity"))
        from unittest.mock import PropertyMock
        type(driver).current_activity = PropertyMock(
            side_effect=lambda: next(acts))
        driver.find_elements.return_value = []   # no dialog, no form, no button
        driver.page_source = '<a clickable="true"/>' * 5   # home has content
        provider = MemorySecretProvider(**{APP_USERNAME: "u",
                                           APP_PASSWORD: "p"})
        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider",
                   return_value=provider), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            macros.MACROS["hhax_uma_login"].run(driver, lambda _k: None)
        # Nothing was ever clicked: the walk ended at "already signed in".
        for call in driver.find_elements.call_args_list:
            pass
        assert not any(
            c for c in driver.method_calls if c[0].endswith(".click"))

    def test_a_single_flash_of_the_sign_in_control_is_not_believed(self):
        """One sighting is a transition frame; two is an offer."""
        import itertools

        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)

        driver = MagicMock()
        driver.current_package = "com.hhaexchange.uma"
        acts = itertools.chain(
            ["com.hhaexchange.uma.AuthenticationActivity"] * 6,
            itertools.repeat("com.hhaexchange.uma.HomeActivity"))
        from unittest.mock import PropertyMock
        type(driver).current_activity = PropertyMock(
            side_effect=lambda: next(acts))

        flash = MagicMock()
        shown = {"n": 0}

        def find_elements(_by, selector):
            if "Iniciar sesi" in selector:
                shown["n"] += 1
                return [flash] if shown["n"] == 1 else []   # one frame only
            return []
        driver.find_elements.side_effect = find_elements
        driver.page_source = '<a clickable="true"/>' * 5
        provider = MemorySecretProvider(**{APP_USERNAME: "u",
                                           APP_PASSWORD: "p"})
        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider",
                   return_value=provider), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            macros.MACROS["hhax_uma_login"].run(driver, lambda _k: None)
        flash.click.assert_not_called()

    def test_an_empty_tree_is_never_a_signed_in_verdict(self):
        """A freshly woken Compose UI exposes nothing for seconds; the macro
        once read that silence as 'signed in' and reported done over a
        still-open expiry dialog. Nothing on screen is not a verdict — the
        macro must keep watching and fail loudly, never quietly succeed."""
        import itertools

        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)

        driver = MagicMock()
        driver.current_package = "com.hhaexchange.uma"
        driver.current_activity = "com.hhaexchange.uma.HomeActivity"
        driver.find_elements.return_value = []
        driver.page_source = "<hierarchy/>"          # the unready tree
        provider = MemorySecretProvider(**{APP_USERNAME: "u",
                                           APP_PASSWORD: "p"})
        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider",
                   return_value=provider), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            with pytest.raises(RuntimeError):
                macros.MACROS["hhax_uma_login"].run(driver, lambda _k: None)
