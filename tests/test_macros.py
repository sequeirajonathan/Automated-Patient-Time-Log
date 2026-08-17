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

    def test_the_expiry_dialog_is_clicked_through_to_the_web_form(self):
        """'Desconectado — se ha cerrado la sesión': the dialog's exit is
        log_out_button ("Regresar al inicio de sesión"). Clicking it hands
        off to the Chrome sign-in form, where the ordinary walk takes over.
        An earlier version terminated and relaunched the app here instead —
        which could hang inside the driver call and freeze the whole runner
        — so the button is pressed, and the app is never restarted."""
        import itertools

        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)

        driver = self._driver("com.hhaexchange.uma.HomeActivity")
        state = {"dismissed": False}
        exit_btn = MagicMock()
        exit_btn.click.side_effect = \
            lambda: state.__setitem__("dismissed", True)

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
        driver.terminate_app.assert_not_called()

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


class TestTheDialogThatDismissesItself:
    """The inactivity countdown dialog runs out and swaps the screen
    between the macro reading it and tapping it — seen live as a
    StaleElementReferenceException at the clearing step, which failed the
    whole sign-in. A dialog that left on its own needed no dismissing."""

    def test_a_stale_dialog_is_read_again_not_fatal(self):
        from selenium.common.exceptions import StaleElementReferenceException

        from apt_log.screens import login as login_mod
        from apt_log.screens import session as session_mod

        class Reached(Exception):
            pass

        driver = MagicMock()
        with patch.object(session_mod, "modal_message",
                          side_effect=["Se cerrará la sesión por inactividad",
                                       ""]), \
             patch.object(session_mod, "is_expired", return_value=True), \
             patch.object(session_mod, "dismiss",
                          side_effect=StaleElementReferenceException("gone")), \
             patch.object(login_mod, "authenticate_if_needed",
                          side_effect=Reached), \
             patch("apt_log.macros.wake_display"), \
             patch("apt_log.macros.time.sleep"):
            with pytest.raises(Reached):
                macros._hhax_legacy_login(driver, lambda _k: None)


class TestMigrationPitch:
    """After sign-in the legacy app parks on a webview pitching
    HHAeXchange+, and Back RETREATS to the login screen (walked live) —
    so the macro takes the page's own mid-shift recommendation,
    «Recordarme más tarde». The webview's mood decides the aim: some
    visits it surfaces the button by name once scrolled, some visits its
    content never reaches the tree and the bottom-anchored coordinate
    tap from the first discovery is all there is. The landing is
    verified either way."""

    def _driver(self, button_after_swipes=None):
        from unittest.mock import PropertyMock

        driver = MagicMock()
        driver.get_window_size.return_value = {"width": 720, "height": 1600}
        state = {"tapped": False, "swipes": 0}
        webview = MagicMock()
        webview.rect = {"x": 11, "y": 75, "width": 698, "height": 1406}
        button = MagicMock()
        button.rect = {"x": 38, "y": 1375, "width": 645, "height": 52}

        def find(_strategy, xpath):
            if "Recordarme" in xpath:
                if (button_after_swipes is not None
                        and state["swipes"] >= button_after_swipes):
                    return [button]
                return []
            return [webview]
        driver.find_elements.side_effect = find
        driver.swipe.side_effect = lambda *_a, **_k: state.__setitem__(
            "swipes", state["swipes"] + 1)

        def activity():
            return ("com.hhaexchange.caregiver.AgencySelectionActivity"
                    if state["tapped"]
                    else "com.hhaexchange.caregiver.MigrationWebViewActivity")
        type(driver).current_activity = PropertyMock(side_effect=activity)
        driver.tap.side_effect = lambda *_a, **_k: state.__setitem__(
            "tapped", True)
        return driver

    def test_the_named_button_is_preferred(self):
        """Seen live: after one swipe the webview exposed «Recordarme más
        tarde» as a real Button. Its own centre is the aim — no blind
        coordinates when the page offers a handle."""
        import itertools

        driver = self._driver(button_after_swipes=1)
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            macros._skip_migration_pitch(driver, lambda _k: None)
        driver.tap.assert_called_once_with([(360, 1401)])

    def test_a_mute_webview_gets_the_bottom_anchored_tap(self):
        """The first discovery's webview exposed nothing by name; the
        coordinate tap just above its bottom edge is the fallback."""
        import itertools

        driver = self._driver(button_after_swipes=None)
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            macros._skip_migration_pitch(driver, lambda _k: None)
        driver.tap.assert_called_once_with([(360, 1401)])
        assert driver.swipe.call_count == 3

    def test_a_screen_offering_nothing_fails_loudly(self):
        import itertools
        from unittest.mock import PropertyMock

        driver = self._driver()
        driver.find_elements.side_effect = lambda *_a: []
        type(driver).current_activity = PropertyMock(
            return_value="com.hhaexchange.caregiver.MigrationWebViewActivity")
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            with pytest.raises(RuntimeError, match="nothing to aim at"):
                macros._skip_migration_pitch(driver, lambda _k: None)

    def test_any_other_screen_is_left_alone(self):
        import itertools

        driver = MagicMock()
        driver.current_activity = "com.hhaexchange.caregiver.HomeActivity"
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            macros._skip_migration_pitch(driver, lambda _k: None)
        driver.tap.assert_not_called()

    def test_a_pitch_that_will_not_close_fails_loudly(self):
        import itertools
        from unittest.mock import PropertyMock

        driver = self._driver()
        driver.tap.side_effect = None            # the tap changes nothing
        type(driver).current_activity = PropertyMock(
            return_value="com.hhaexchange.caregiver.MigrationWebViewActivity")
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            with pytest.raises(RuntimeError, match="did not close"):
                macros._skip_migration_pitch(driver, lambda _k: None)


class TestReadPage:
    """The owner's ask: the front end should have everything a page shows.
    The reading walks the page with mid-screen swipes — they move the page
    and can press nothing — and writes every text line in reading order.
    A reading, not a tappable surface: coordinates below the fold are not
    targets."""

    def test_the_page_is_walked_and_the_lines_stitched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(macros, "SCAN_PATH", tmp_path / "scan.json")
        driver = MagicMock()
        driver.get_window_size.return_value = {"width": 720, "height": 1600}
        driver.current_package = "com.hhaexchange.caregiver"
        pages = [
            # already at top: the up-phase sees the same page twice
            '<node class="android.widget.TextView" text="Uno" clickable="false" bounds="[10,100][700,150]"/>',
            '<node class="android.widget.TextView" text="Uno" clickable="false" bounds="[10,100][700,150]"/>',
            # down-phase: overlap ("Dos") dropped, repeats further kept
            '<node class="android.widget.TextView" text="Uno" clickable="false" bounds="[10,100][700,150]"/>'
            '<node class="android.widget.TextView" text="Dos" clickable="false" bounds="[10,200][700,250]"/>',
            '<node class="android.widget.TextView" text="Dos" clickable="false" bounds="[10,80][700,130]"/>'
            '<node class="android.widget.TextView" text="Tres" clickable="false" bounds="[10,200][700,250]"/>',
            '<node class="android.widget.TextView" text="Dos" clickable="false" bounds="[10,80][700,130]"/>'
            '<node class="android.widget.TextView" text="Tres" clickable="false" bounds="[10,200][700,250]"/>',
        ]
        sources = iter(pages)
        type(driver).page_source = property(
            lambda self: next(sources, pages[-1]))
        with patch("apt_log.macros.time.sleep"):
            macros._read_page(driver, lambda _k: None)
        doc = json.loads((tmp_path / "scan.json").read_text())
        assert doc["lines"] == ["Uno", "Dos", "Tres"]
        assert doc["app"] == "com.hhaexchange.caregiver"

    def test_the_macro_is_registered_with_a_translation_key(self):
        assert macros.MACROS["read_page"].label_key == "macro.read_page"


class TestSwipeFallback:
    """UiAutomator2 refuses W3C action chains now and then while a plain
    `input swipe` on the same device works. The walk falls back instead of
    dying — observed live as half the whole-page walks failing with
    "Unable to perform W3C actions"."""

    def test_a_working_driver_swipes_without_adb(self):
        driver = MagicMock()
        with patch("apt_log.macros.subprocess.run") as run:
            macros._swipe(driver, 360, 900, 500)
        driver.swipe.assert_called_once_with(360, 900, 360, 500, 260)
        run.assert_not_called()

    def test_a_w3c_refusal_falls_back_to_adb(self):
        driver = MagicMock()
        driver.swipe.side_effect = Exception("Unable to perform W3C actions")
        with patch("apt_log.macros.subprocess.run") as run:
            macros._swipe(driver, 360, 900, 500)
        run.assert_called_once()
        assert run.call_args.args[0] == [
            "adb", "shell", "input", "swipe", "360", "900", "360", "500", "260"]


class TestMaybeStitch:
    """The walk fires itself, so its gates carry the safety argument: only a
    watched, unblocked, scrollable care-app page, never twice in quick
    succession, and never system UI."""

    def _runner(self, tmp_path, app="com.hhaexchange.caregiver",
                screen="home", fid="abc123", activity="homeactivity",
                scrollable=True):
        viewers = tmp_path / "viewers.json"
        viewers.write_text(json.dumps({"n": 1}), encoding="utf-8")
        screen_doc = tmp_path / "screen.json"
        screen_doc.write_text(json.dumps({
            "id": fid, "app": app, "screen": screen, "blocked": "",
            "activity": activity, "scrollable": scrollable,
            "full": False}), encoding="utf-8")
        return macros.Runner(status_path=tmp_path / "status.json",
                             screen_path=screen_doc, viewers_path=viewers)

    def _repoint(self, runner, tmp_path, **kw):
        doc = json.loads(runner._screen_path.read_text(encoding="utf-8"))
        doc.update(kw)
        runner._screen_path.write_text(json.dumps(doc), encoding="utf-8")

    def test_a_care_app_page_is_walked(self, tmp_path):
        runner = self._runner(tmp_path)
        with patch("apt_log.resident.run", return_value=True) as walk:
            assert runner.maybe_stitch() is True
        walk.assert_called_once()

    def test_a_page_not_calling_itself_scrollable_is_still_scanned(self, tmp_path):
        """Compose screens lie about scrollability, and the owner's spec is
        that no page leaves anyone wondering: the walk itself discovers
        whether there is more, and a one-screen page publishes as the
        whole page — which is exactly what it is."""
        runner = self._runner(tmp_path, scrollable=False)
        with patch("apt_log.resident.run", return_value=True) as walk:
            assert runner.maybe_stitch() is True
        walk.assert_called_once()

    def test_a_page_transition_pays_no_cooldown(self, tmp_path):
        """Waiting out a floor on a FRESH page read as 'the front end is
        missing things'. The floor binds only the page it was armed on."""
        runner = self._runner(tmp_path)
        with patch("apt_log.resident.run", return_value=True) as walk:
            assert runner.maybe_stitch() is True
            self._repoint(runner, tmp_path, activity="visitdetailactivity",
                          id="def456")
            assert runner.maybe_stitch() is True
        assert walk.call_count == 2

    def test_a_fresh_tap_defers_the_scan(self, tmp_path):
        """No scanning over her fingers: a tap seconds ago means someone is
        driving the phone, and the walk waits its turn."""
        from apt_log import feed as feed_mod

        runner = self._runner(tmp_path)
        (tmp_path / feed_mod.POKE_NAME).write_text("now", encoding="utf-8")
        with patch("apt_log.resident.run") as walk:
            assert runner.maybe_stitch() is False
        walk.assert_not_called()

    def test_system_ui_is_never_walked(self, tmp_path):
        """Seen live: the walker swiping through the notification shade —
        system UI nobody asked to read, on a phone nobody is holding."""
        runner = self._runner(tmp_path, app="com.android.systemui")
        with patch("apt_log.resident.run") as walk:
            assert runner.maybe_stitch() is False
        walk.assert_not_called()

    def test_the_cooldown_stops_back_to_back_walks(self, tmp_path):
        """A page whose text ticks re-hashes every frame; without a floor
        between attempts the phone scrolls itself over and over."""
        runner = self._runner(tmp_path)
        with patch("apt_log.resident.run", return_value=True) as walk:
            assert runner.maybe_stitch() is True
            assert runner.maybe_stitch() is False
        walk.assert_called_once()

    def test_a_transient_failure_is_also_held_to_the_cooldown(self, tmp_path):
        """Transient failures are not latched to the frame — but they must
        not retry every poll tick either, or a dying session animates the
        phone once a second."""
        runner = self._runner(tmp_path)
        with patch("apt_log.resident.run",
                   side_effect=RuntimeError("no Appium session")) as walk:
            assert runner.maybe_stitch() is False
            assert runner.maybe_stitch() is False
        walk.assert_called_once()


class TestSkipPreamble:
    """A freshly-entered page is already at its top; only a re-scan pays
    the scroll-to-top probe. The probe swipe plus its settle was pure
    latency on the common case (a tab tapped, an activity opened)."""

    def _driver(self):
        driver = MagicMock()
        driver.get_window_size.return_value = {"width": 720, "height": 1600}
        driver.current_package = "com.hhaexchange.uma"
        src = ('<node class="android.widget.TextView" text="Uno" '
               'clickable="false" bounds="[10,100][700,150]"/>')
        type(driver).page_source = property(lambda self: src)
        return driver

    def test_a_fresh_page_skips_the_scroll_to_top(self, tmp_path, monkeypatch):
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        driver = self._driver()
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros._scroll_to_top") as to_top:
            macros._stitch_walk(driver, assume_top=True)
        to_top.assert_not_called()

    def test_a_rescan_still_scrolls_to_top(self, tmp_path, monkeypatch):
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        driver = self._driver()
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros._scroll_to_top") as to_top:
            macros._stitch_walk(driver, assume_top=False)
        to_top.assert_called_once()


class TestWarmSweep:
    """After a sign-in, the app's other tabs have never been opened, so
    their scans cannot exist. The sweep opens each once — non-committing —
    and yields the phone the instant a real action is waiting."""

    def _driver(self, tabs=3):
        driver = MagicMock()
        driver.get_window_size.return_value = {"width": 720, "height": 1600}
        driver.current_package = "com.hhaexchange.uma"
        labels = ["Programación", "Pacientes", "Menú", "Más"][:tabs]
        nodes = "".join(
            f'<node class="android.widget.TextView" text="{lbl}" '
            f'clickable="false" bounds="[{i*240},1427][{i*240+200},1475]"/>'
            for i, lbl in enumerate(labels))
        type(driver).page_source = property(lambda self: nodes)
        return driver

    def test_each_sibling_tab_is_opened_once(self, tmp_path, monkeypatch):
        """Slot 0 is the landing tab — already in front, already scanned;
        the sweep warms the others and comes home."""
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        driver = self._driver(tabs=3)
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros._stitch_walk", return_value=True) as walk:
            warmed = macros._warm_sweep(driver, tmp_path / "req.json",
                                        tmp_path / "deep.json",
                                        tmp_path / "poke")
        assert warmed == 2
        assert walk.call_count == 2

    def test_a_screen_without_tabs_is_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        driver = self._driver(tabs=1)
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros._stitch_walk", return_value=True) as walk:
            assert macros._warm_sweep(driver, tmp_path / "req.json",
                                      tmp_path / "deep.json",
                                      tmp_path / "poke") == 0
        walk.assert_not_called()

    def test_a_waiting_request_stops_the_sweep(self, tmp_path, monkeypatch):
        """She tapped something: the sweep yields the phone at once."""
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        req = tmp_path / "req.json"
        req.write_text("{}", encoding="utf-8")
        driver = self._driver(tabs=3)
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros._stitch_walk", return_value=True) as walk:
            macros._warm_sweep(driver, req, tmp_path / "deep.json",
                               tmp_path / "poke")
        walk.assert_not_called()

    def test_warming_is_disabled_so_a_login_does_not_arm_it(self, tmp_path):
        """Warming is off (WARM_ENABLED False): it stranded the owner on a
        settings sub-page. A login must not arm the autonomous sweep."""
        assert macros.WARM_ENABLED is False
        runner = macros.Runner(status_path=tmp_path / "s.json")
        with patch("apt_log.resident.run"), \
             patch("apt_log.ui.mirror.publish"):
            runner.execute("hhax_uma_login", "rid1")
        assert runner._warm_app is None

    def test_the_arming_still_gates_on_a_login_name(self, tmp_path):
        """The gate itself is intact for when warming is re-enabled."""
        runner = macros.Runner(status_path=tmp_path / "s.json")
        with patch("apt_log.resident.run"), \
             patch("apt_log.ui.mirror.publish"), \
             patch("apt_log.macros.WARM_ENABLED", True):
            runner.execute("hhax_uma_login", "rid1")
        assert runner._warm_app == "hhax_uma_login"


class TestWarmReturnHome:
    """Home is 'the leftmost tab is selected', detected by the selected
    tab having no clickable container — never a remembered frame (the app
    changes those) and never Back (that walked out of the app to the
    launcher, seen live landing on Google)."""

    def _bar(self, selected):
        """A three-tab bar with `selected` (0-based) the tab in front."""
        labels = ["Programación", "Pacientes", "Menú"]
        parts = []
        for i, lbl in enumerate(labels):
            x = i * 240
            parts.append(f'<node class="android.widget.TextView" text="{lbl}" '
                         f'clickable="false" bounds="[{x},1427][{x+200},1475]"/>')
            if i != selected:            # the selected tab has no container
                parts.append('<node class="android.view.View" clickable="true" '
                             f'bounds="[{x},1312][{x+200},1492]"/>')
        return "".join(parts)

    def test_it_taps_until_the_leftmost_tab_is_selected(self):
        driver = MagicMock()
        driver.get_window_size.return_value = {"width": 720, "height": 1600}
        state = {"src": self._bar(selected=2)}   # parked on Menú
        type(driver).page_source = property(lambda self: state["src"])
        driver.tap.side_effect = lambda *_a: state.__setitem__(
            "src", self._bar(selected=0))        # a tap returns to schedule
        with patch("apt_log.macros.time.sleep"):
            macros._return_to_landing(driver)
        driver.tap.assert_called_once()          # one tap, then home

    def test_a_screen_without_a_tab_bar_is_never_backed_out_of(self):
        """No Back: a sub-page with no visible tab bar is left as-is rather
        than escaped into the launcher."""
        driver = MagicMock()
        driver.get_window_size.return_value = {"width": 720, "height": 1600}
        type(driver).page_source = property(
            lambda self: '<node class="android.widget.Button" clickable="true" '
                         'bounds="[0,300][720,400]"/>')
        with patch("apt_log.macros.time.sleep"):
            macros._return_to_landing(driver)
        driver.tap.assert_not_called()
        driver.back.assert_not_called()


class TestTabAim:
    """Tapping a Compose tab's caption does nothing (verified live); the
    clickable container up in the icon zone is where the tap takes. So the
    labels enumerate the slots but the container is the aim."""

    def test_the_clickable_container_is_the_tap_point_not_the_label(self):
        driver = MagicMock()
        driver.get_window_size.return_value = {"width": 720, "height": 1600}
        # A HHAeXchange+ tab bar: Programación selected (no container),
        # Pacientes and Menú clickable containers higher than their labels.
        src = (
            '<node class="android.widget.TextView" text="Programación" '
            'clickable="false" bounds="[0,1427][228,1475]"/>'
            '<node class="android.widget.TextView" text="Pacientes" '
            'clickable="false" bounds="[268,1427][452,1475]"/>'
            '<node class="android.widget.TextView" text="Menú" '
            'clickable="false" bounds="[552,1427][659,1475]"/>'
            '<node class="android.view.View" clickable="true" '
            'bounds="[246,1312][474,1492]"/>'          # Pacientes container
            '<node class="android.view.View" clickable="true" '
            'bounds="[492,1312][720,1492]"/>')         # Menú container
        type(driver).page_source = property(lambda self: src)
        slots = macros._tab_slots(driver)
        assert len(slots) == 3
        # Programación selected: no container, aim just above its label.
        assert slots[0] == {"point": (114, 1387), "selected": True}
        # Pacientes and Menú: the container centre, up in the icon zone.
        assert slots[1] == {"point": (360, 1402), "selected": False}
        assert slots[2] == {"point": (606, 1402), "selected": False}


class TestWarmWaitsForTabBar:
    """The sign-in's activate_app can leave the app mid-reload when the
    sweep starts — seen live warming 0 because the tab bar was not yet
    drawn. The sweep waits for it rather than giving up."""

    def test_a_late_tab_bar_is_waited_for(self, tmp_path, monkeypatch):
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        bar = "".join(
            f'<node class="android.widget.TextView" text="T{i}" '
            f'clickable="false" bounds="[{i*240},1427][{i*240+200},1475]"/>'
            for i in range(3))
        seq = ["", "", bar]              # blank while reloading, then the bar
        driver = MagicMock()
        driver.get_window_size.return_value = {"width": 720, "height": 1600}
        state = {"i": 0}

        def src(_self):
            i = min(state["i"], len(seq) - 1)
            state["i"] += 1
            return seq[i]
        type(driver).page_source = property(src)

        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=[0, 1, 2, 3, 4, 5, 6, 7]), \
             patch("apt_log.macros._stitch_walk", return_value=True) as walk, \
             patch("apt_log.macros._return_to_landing"):
            warmed = macros._warm_sweep(driver, tmp_path / "r", tmp_path / "d",
                                        tmp_path / "p")
        assert warmed == 2
        assert walk.call_count == 2


class TestScanOwnsTheSession:
    """A scan flags SCAN_ACTIVE so the hierarchy watcher yields the one
    Appium session — the interleaved dumps were what made the scan crawl
    and the live view lurch through the scroll."""

    def test_the_flag_is_set_during_the_walk_and_cleared_after(self, tmp_path, monkeypatch):
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        driver = MagicMock()
        driver.get_window_size.return_value = {"width": 720, "height": 1600}
        driver.current_package = "com.hhaexchange.uma"
        seen = {}
        src = ('<node class="android.widget.TextView" text="A" '
               'clickable="false" bounds="[10,100][700,150]"/>')
        type(driver).page_source = property(lambda self: src)

        real_swipe = macros._swipe

        def spy_swipe(*a, **k):
            seen["during"] = macros.SCAN_ACTIVE.is_set()
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros._swipe", spy_swipe):
            macros.SCAN_ACTIVE.clear()
            macros._stitch_walk(driver, assume_top=True)
        assert seen.get("during") is True          # set while walking
        assert macros.SCAN_ACTIVE.is_set() is False  # cleared after

    def test_the_flag_is_cleared_even_when_the_walk_raises(self, monkeypatch):
        driver = MagicMock()
        driver.get_window_size.side_effect = RuntimeError("session gone")
        macros.SCAN_ACTIVE.clear()
        with pytest.raises(RuntimeError):
            macros._stitch_walk(driver, assume_top=True)
        assert macros.SCAN_ACTIVE.is_set() is False


class TestAccordionScan:
    """The scan opens the schedule's folded visit cards as it walks.

    A collapsed card hides its EVV records and details button; the owner
    asked for the opposite ("open accordion elements so we get an accurate
    front end"). The chevron glyph is the state signal, verified live:
    collapsed it stands taller than wide at the row's trailing edge,
    expanded the same glyph is drawn rotated. Taps are budgeted, gated to
    the proven app and to a page showing a run of date headers, and backed
    out of the moment a tap navigates instead of unfolding."""

    W, H = 720, 1600
    CHEV = "\uf054"

    def _dates(self, n=3):
        return "".join(
            f'<node class="android.widget.TextView" text="agosto {17+i}, 2026" '
            f'clickable="false" bounds="[9,{347+i*300}][98,{363+i*300}]"/>'
            for i in range(n))

    def _card(self, y, name, folded=True, details=False):
        chev = (f'<node class="android.widget.TextView" text="{self.CHEV}" '
                f'clickable="false" bounds="[694,{y+3}][700,{y+14}]"/>'
                if folded else
                f'<node class="android.widget.TextView" text="{self.CHEV}" '
                f'clickable="false" bounds="[692,{y+6}][703,{y+12}]"/>')
        extra = ('<node class="android.widget.TextView" '
                 f'text="Registros de entrada de EVV 6:00 a. m." '
                 f'clickable="false" bounds="[45,{y+83}][253,{y+99}]"/>'
                 if details else "")
        return (f'<node class="android.view.View" clickable="true" '
                f'bounds="[25,{y}][700,{y+32}]"/>'
                f'<node class="android.widget.TextView" text="{name}" '
                f'clickable="false" bounds="[25,{y}][142,{y+16}]"/>'
                + chev + extra)

    def _driver(self, state, pages, package="com.hhaexchange.uma"):
        driver = MagicMock()
        driver.get_window_size.return_value = {"width": self.W, "height": self.H}
        driver.current_package = package
        type(driver).page_source = property(
            lambda _self: pages[state["page"]])
        return driver

    def test_a_folded_card_is_opened_and_its_contents_scanned(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        pages = {
            "collapsed": self._dates() + self._card(401, "NIEVES C MASTRAPA"),
            "expanded": self._dates() + self._card(
                401, "NIEVES C MASTRAPA", folded=False, details=True),
        }
        state = {"page": "collapsed"}
        driver = self._driver(state, pages)

        def tap(x, y):
            assert (x, y) == (362, 417)     # the folded row's centre
            state["page"] = "expanded"

        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros._tap_xy", side_effect=tap) as tapped:
            assert macros._stitch_walk(driver, assume_top=True) is True
        tapped.assert_called_once()
        written = "".join(p.read_text() for p in (tmp_path / "stitched").glob("*.json"))
        assert "Registros de entrada de EVV" in written

    def test_a_tap_that_navigates_is_backed_out_of(self, tmp_path, monkeypatch):
        """The guard: an unfolded card keeps the page's dates and chevrons;
        a page missing them is wherever the tap went instead. One Back
        returns, and the walk stops trusting taps."""
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        schedule = self._dates() + "".join(
            self._card(401 + i * 60, f"PACIENTE {i}") for i in range(4))
        pages = {
            "collapsed": schedule,
            "elsewhere": ('<node class="android.widget.TextView" text="Detalle" '
                          'clickable="false" bounds="[25,401][142,417]"/>'),
        }
        state = {"page": "collapsed"}
        driver = self._driver(state, pages)

        def tap(x, y):
            state["page"] = "elsewhere"

        def back(code):
            assert code == 4
            state["page"] = "collapsed"

        driver.press_keycode.side_effect = back
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros._tap_xy", side_effect=tap) as tapped:
            assert macros._stitch_walk(driver, assume_top=True) is True
        tapped.assert_called_once()          # never a second gamble
        driver.press_keycode.assert_called_once_with(4)

    def test_only_the_proven_app_is_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        pages = {"collapsed": self._dates() + self._card(401, "PACIENTE")}
        driver = self._driver({"page": "collapsed"}, pages,
                              package="com.tellus.evv.v2")
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros._tap_xy") as tapped:
            macros._stitch_walk(driver, assume_top=True)
        tapped.assert_not_called()

    def test_a_page_without_a_run_of_dates_is_left_alone(
            self, tmp_path, monkeypatch):
        """One or two dates on screen is any details page — its trailing
        chevrons navigate, and the scan must never walk away from the
        screen it was asked to read."""
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        pages = {"collapsed": self._dates(2) + self._card(401, "PACIENTE")}
        driver = self._driver({"page": "collapsed"}, pages)
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros._tap_xy") as tapped:
            macros._stitch_walk(driver, assume_top=True)
        tapped.assert_not_called()

    def test_an_open_cards_rotated_chevron_is_not_tapped(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        pages = {"collapsed": self._dates() + self._card(
            401, "PACIENTE", folded=False, details=True)}
        driver = self._driver({"page": "collapsed"}, pages)
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros._tap_xy") as tapped:
            macros._stitch_walk(driver, assume_top=True)
        tapped.assert_not_called()
