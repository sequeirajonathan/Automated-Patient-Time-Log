"""Named sequences, and the things they are not allowed to be.

Most of these hold one line: the page asks for a name from a list, and the list
lives in code. A macro that took steps from a browser would be arbitrary remote
scripting with a friendlier label.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from apt_log import macros
from apt_log.macros import AUTO_AUTH_COOLDOWN


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

    def test_one_apps_sign_in_does_not_silence_another(self, tmp_path):
        """Seen on the live phone: Mobile Caregiver+ signed itself in, and
        inMyTeam then sat on its splash with somebody watching and nothing
        happened — because the per-macro cooldown fell back to the shared
        timestamp the other app had just set. Different apps, different
        credentials, different clocks."""
        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     INMYTEAM_PHONE, MC_PIN,
                                     MemorySecretProvider)

        runner = macros.Runner(
            tmp_path / "req.json", tmp_path / "status.json",
            screen_path=tmp_path / "screen.json",
            viewers_path=self._watching(tmp_path),
            secrets=MemorySecretProvider(**{APP_USERNAME: "u",
                                            APP_PASSWORD: "p",
                                            MC_PIN: "2580",
                                            INMYTEAM_PHONE: "3055550123"}))
        ran = []
        with patch.object(runner, "execute", side_effect=lambda n, r: ran.append(n)):
            self._doc(tmp_path, app="com.tellus.evv.v2")
            assert runner.maybe_auto_auth() is True
            # Now the other app's sign-in screen, immediately after.
            path = tmp_path / "screen.json"
            import datetime as dt

            path.write_text(json.dumps({
                "app": "com.inmyteam.inmyteam", "screen": "unknown",
                "blocked": "", "at": dt.datetime.now().isoformat(),
                "statics": [{"txt": "Let’s Get Started"}], "elements": []}))
            assert runner.maybe_auto_auth() is True
        assert ran == ["mobile_caregiver_pin", "inmyteam_login"]

    def test_a_macro_that_texts_somebody_waits_much_longer(self, tmp_path):
        """Its own cooldown, because its retry costs a message on a real
        person's phone rather than a wasted second."""
        import datetime as dt

        from apt_log.secrets import INMYTEAM_PHONE, MemorySecretProvider

        runner = macros.Runner(
            tmp_path / "req.json", tmp_path / "status.json",
            screen_path=tmp_path / "screen.json",
            viewers_path=self._watching(tmp_path),
            secrets=MemorySecretProvider(**{INMYTEAM_PHONE: "3055550123"}))

        def splash():
            (tmp_path / "screen.json").write_text(json.dumps({
                "app": "com.inmyteam.inmyteam", "screen": "unknown",
                "blocked": "", "at": dt.datetime.now().isoformat(),
                "statics": [{"txt": "Let’s Get Started"}], "elements": []}))

        with patch.object(runner, "execute"):
            splash()
            assert runner.maybe_auto_auth() is True
            # Past the ordinary cooldown, nowhere near this one.
            runner._auto_auth_seen["inmyteam_login"] -= AUTO_AUTH_COOLDOWN + 5
            splash()
            assert runner.maybe_auto_auth() is False
            runner._auto_auth_seen["inmyteam_login"] -= macros.SMS_AUTH_COOLDOWN
            splash()
            assert runner.maybe_auto_auth() is True

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

    def test_the_expiry_dialog_is_restarted_over_adb_not_clicked(self):
        """'Desconectado — se ha cerrado la sesión': a FRESH STACK is the
        recovery. Clicking the dialog's exit walks into the stale activity
        stack, which swallows the sign-in redirect (watched live twice:
        Chrome submits, the dialog resurfaces). And the restart must not go
        through the driver — terminate_app against exactly this wedged app
        hung inside its HTTP call for an hour and froze the whole runner.
        Plain adb force-stops and relaunches; a cold start lands directly
        on the pending Chrome form, which the ordinary walk fills."""
        import itertools

        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)

        driver = self._driver("com.hhaexchange.uma.HomeActivity")
        state = {"dismissed": False}
        exit_btn = MagicMock()
        adb_calls = []

        def adb(cmd, **_kw):
            adb_calls.append(cmd)
            if cmd[2:4] == ["am", "force-stop"]:
                state["dismissed"] = True
            return MagicMock(returncode=0)

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
             patch("apt_log.macros.subprocess.run", side_effect=adb), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            try:
                macros.MACROS["hhax_uma_login"].run(driver, lambda _k: None)
            except RuntimeError:
                pass    # the mock form is shallow past the fields
        assert any(c[2:4] == ["am", "force-stop"] for c in adb_calls)
        assert any(c[2] == "monkey" for c in adb_calls)
        exit_btn.click.assert_not_called()
        driver.terminate_app.assert_not_called()

    def test_a_dialog_surviving_the_restart_is_clicked_as_a_last_resort(self):
        """A fresh stack has nothing stale to strand the redirect in, so if
        the dialog somehow comes back after the restart, its button is the
        one exit left — pressed once, never looped on."""
        import itertools

        from apt_log.secrets import (APP_PASSWORD, APP_USERNAME,
                                     MemorySecretProvider)

        driver = self._driver("com.hhaexchange.uma.HomeActivity")
        state = {"restarted": False, "dismissed": False}
        exit_btn = MagicMock()
        exit_btn.click.side_effect = \
            lambda: state.__setitem__("dismissed", True)

        def adb(cmd, **_kw):
            if cmd[2:4] == ["am", "force-stop"]:
                state["restarted"] = True
            return MagicMock(returncode=0)

        def find_elements(_by, selector):
            if "Regresar al inicio" in selector:
                return [] if state["dismissed"] else [exit_btn]
            if 'resource-id=' in selector and state["dismissed"]:
                return [MagicMock()]
            return []
        driver.find_elements.side_effect = find_elements

        provider = MemorySecretProvider(**{APP_USERNAME: "u",
                                           APP_PASSWORD: "p"})
        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider",
                   return_value=provider), \
             patch("apt_log.macros.subprocess.run", side_effect=adb), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            try:
                macros.MACROS["hhax_uma_login"].run(driver, lambda _k: None)
            except RuntimeError:
                pass    # the mock form is shallow past the fields
        assert state["restarted"] is True
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


class TestInMyTeamWalksToTheCode:
    """Reported from the field: "it should have entered a phone number
    waiting for the OTP but it's not getting me to that step on app launch".

    It never was. inMyTeam had no sign-in macro, so its tile ran the
    open-only one: the app came to the front, landed on its marketing splash
    — "THE FUTURE of home care agencies", one "Let's Get Started" button —
    and waited for a tap nobody was going to give it.

    The walk stops at the code screen on purpose. A macro cannot invent a
    texted code, and the portal's type bar was built for this moment.
    """

    SPLASH = '<node text="Let’s Get Started"/>'
    NUMBER = '<node text="Enter your cell phone number"/><node text="Sign in"/>'
    # The app's own words, off the live screen.
    CODE = ('<node text="Verify Your Account"/>'
            '<node text="A text with a code was sent to"/>'
            '<node text="Enter your code"/><node text="Verify"/>')

    def _driver(self, state):
        """A phone that advances a screen each time something is clicked."""
        driver = MagicMock()
        driver.current_activity = "com.inmyteam.inmyteam.MainActivity"
        driver.current_package = "com.inmyteam.inmyteam"

        def page():
            return {"splash": self.SPLASH, "number": self.NUMBER,
                    "code": self.CODE}[state["at"]]

        def find_elements(_by, selector):
            if "EditText" in selector:
                return [self._box(state)] if state["at"] in ("number",
                                                             "code") else []
            for word in ("Get Started", "Comenzar", "Empezar"):
                if word in selector and state["at"] == "splash":
                    return [self._tap(state, "number")]
            for word in ("Sign in", "Iniciar"):
                if word in selector and state["at"] == "number":
                    # Document order, exactly as the phone reports it: the
                    # full-screen container first, the button after.
                    return [self._root(state), self._tap(state, "code")]
            return []

        driver.find_elements.side_effect = find_elements
        type(driver).page_source = PropertyMock(side_effect=page)
        return driver

    def _tap(self, state, goes_to, size=(600, 60)):
        control = MagicMock()
        control.is_displayed.return_value = True
        control.rect = {"x": 0, "y": 0, "width": size[0], "height": size[1]}
        control.click.side_effect = lambda: state.update(at=goes_to)
        return control

    def _root(self, state):
        """The screen's own root: clickable, full-screen, and it contains
        every word on the page — including "Sign in with your phone number".
        Pressing it does nothing at all."""
        control = MagicMock()
        control.is_displayed.return_value = True
        control.rect = {"x": 0, "y": 0, "width": 720, "height": 1515}
        control.click.side_effect = lambda: state.setdefault("dead", 0)
        return control

    def _box(self, state):
        box = MagicMock()
        box.is_displayed.return_value = True
        state.setdefault("typed", [])
        box.send_keys.side_effect = state["typed"].append
        state.setdefault("boxes", []).append(box)
        return box

    def _run(self, state):
        import itertools

        from apt_log.secrets import INMYTEAM_PHONE, MemorySecretProvider

        driver = self._driver(state)
        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider",
                   return_value=MemorySecretProvider(
                       **{INMYTEAM_PHONE: "3055550123"})), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            macros.MACROS["inmyteam_login"].run(driver, lambda _k: None)
        return driver

    def test_it_presses_through_the_splash_and_types_the_number(self):
        state = {"at": "splash"}
        self._run(state)
        assert state["at"] == "code"
        assert state["typed"] == ["3055550123"]

    def test_the_box_is_cleared_before_the_number_goes_in(self):
        """The app remembers the last number, and typing into a prefilled
        box appends to it — the trap the HHAeXchange+ web form sprang once."""
        state = {"at": "number"}
        self._run(state)
        assert state["typed"] == ["3055550123"]

    def test_it_stops_at_the_code_rather_than_reporting_signed_in(self):
        """Success here means "now she types the code", and the macro has to
        actually see the app ask for one."""
        steps = []
        state = {"at": "splash"}
        import itertools

        from apt_log.secrets import INMYTEAM_PHONE, MemorySecretProvider

        driver = self._driver(state)
        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider",
                   return_value=MemorySecretProvider(
                       **{INMYTEAM_PHONE: "3055550123"})), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            macros.MACROS["inmyteam_login"].run(driver, steps.append)
        assert steps[-1] == "macro.step.awaiting_code"

    def _elsewhere(self, tappables):
        """An app showing something this walk does not recognise."""
        driver = MagicMock()
        driver.current_activity = "com.inmyteam.inmyteam.MainActivity"
        driver.current_package = "com.inmyteam.inmyteam"

        def find_elements(_by, selector):
            if selector == '//*[@clickable="true"]':
                return [MagicMock() for _ in range(tappables)]
            return []

        driver.find_elements.side_effect = find_elements
        type(driver).page_source = PropertyMock(return_value="<node/>")
        return driver

    def _bare_run(self, driver):
        import itertools

        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider") as provider_cls, \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            macros.MACROS["inmyteam_login"].run(driver, lambda _k: None)
        return provider_cls

    def test_a_signed_in_app_is_left_alone(self):
        provider_cls = self._bare_run(self._elsewhere(tappables=6))
        provider_cls.assert_not_called()

    def test_an_unreadable_tree_is_not_mistaken_for_signed_in(self):
        """Live: the macro read the tree of the app the phone was LEAVING,
        found no field in it, and reported done over an inMyTeam splash that
        had not drawn yet. An empty tree is "not yet", not "nothing to do" —
        the trap the HHAeXchange+ macro wrote down and this one sprang."""
        import itertools

        driver = self._elsewhere(tappables=0)
        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider"), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            with pytest.raises(RuntimeError, match="not showing anything"):
                macros.MACROS["inmyteam_login"].run(driver, lambda _k: None)

    def test_it_waits_for_this_app_and_not_just_any_activity(self):
        """`current_activity` answers the moment the driver knows anything,
        including the app the phone is leaving."""
        import itertools

        driver = self._elsewhere(tappables=6)
        driver.current_package = "com.tellus.evv.v2"
        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider"), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            with pytest.raises(RuntimeError, match="did not come to the front"):
                macros.MACROS["inmyteam_login"].run(driver, lambda _k: None)

    def test_a_number_the_app_will_not_take_is_a_failure_not_a_success(self):
        """A rejected number changes the screen too. Reporting success over
        an error message is how a macro teaches somebody to distrust it."""
        import itertools

        from apt_log.secrets import INMYTEAM_PHONE, MemorySecretProvider

        state = {"at": "number"}
        driver = self._driver(state)
        # The app bounces back to the number screen with an error instead of
        # asking for a code.
        self.CODE = '<node text="That number is not registered"/>'
        try:
            with patch("apt_log.macros.wake_display"), \
                 patch("apt_log.secrets.FileSecretProvider",
                       return_value=MemorySecretProvider(
                           **{INMYTEAM_PHONE: "3055550123"})), \
                 patch("apt_log.macros.time.sleep"), \
                 patch("apt_log.macros.time.monotonic",
                       side_effect=itertools.count(step=0.5)):
                with pytest.raises(RuntimeError, match="did not ask for a code"):
                    macros.MACROS["inmyteam_login"].run(driver, lambda _k: None)
        finally:
            del self.CODE

    def test_the_button_is_pressed_and_not_the_screen_it_sits_on(self):
        """Live: the number went in, "Sign in" matched the full-screen root
        because the heading reads "Sign in with your phone number", and
        nothing happened. The button is the tightest node with the words."""
        state = {"at": "number"}
        self._run(state)
        assert state["at"] == "code"
        assert "dead" not in state

    def test_a_code_screen_already_up_is_arrival_not_a_reason_to_start_over(self):
        """Both screens are one EditText and a button. Without this check the
        walk reads the CODE box as the number box, clears whatever she is
        part-way through typing, puts a phone number in its place, and
        presses submit. Found by leaving the phone on the code screen and
        asking what the macro would do next."""
        state = {"at": "code"}
        self._run(state)
        assert state["at"] == "code"
        assert state.get("typed", []) == []
        # And the box she may be part-way through typing into is untouched.
        assert all(not box.clear.called for box in state.get("boxes", []))

    def test_it_signs_itself_in_like_the_other_three(self):
        """It was held back because pressing it texts a real person. What
        changed: the walk is a no-op once the app is already asking for a
        code, so the common repeat costs nothing."""
        from apt_log.secrets import INMYTEAM_PHONE, MemorySecretProvider

        provider = MemorySecretProvider(**{INMYTEAM_PHONE: "3055550123"})
        assert (macros.auth_macro_for("com.inmyteam.inmyteam", provider)
                == "inmyteam_login")

    def test_no_number_stored_means_no_automatic_anything(self):
        from apt_log.secrets import MemorySecretProvider

        assert macros.auth_macro_for("com.inmyteam.inmyteam",
                                     MemorySecretProvider()) is None

    def test_a_macro_that_texts_somebody_gets_the_long_cooldown(self):
        """Ninety seconds is right for a wasted retry and wrong for one that
        puts another message on a real person's phone."""
        assert "inmyteam_login" in macros.SENDS_A_MESSAGE
        assert macros.SMS_AUTH_COOLDOWN >= 10 * 60

    def test_the_tile_is_what_runs_it(self):
        from apt_log.ui.app import PHONE_APPS

        tile = next(a for a in PHONE_APPS if a["id"] == "inmyteam")
        assert tile["macro"] == "inmyteam_login"
        assert tile["open"] == "open_inmyteam"


class TestInMyTeamIsRecognisedByItsWords:
    """This app has ONE activity. Splash, phone number, code and the
    signed-in app all live under `mainactivity`, so the atlas cannot tell
    them apart, and there is no password field anywhere in the walk for the
    capture refusals to fire on. Its own words are the only signal it gives —
    the same shape of answer the Chrome rule in auth_macro_for needed."""

    def _doc(self, *words, app="com.inmyteam.inmyteam"):
        return {"app": app, "statics": [{"txt": w} for w in words],
                "elements": []}

    def test_the_splash_is_asking_to_be_signed_in(self):
        assert macros.wants_to_sign_in(
            self._doc("THE FUTURE of home care agencies",
                      "Let’s Get Started")) is True

    def test_the_number_screen_is_asking_too(self):
        assert macros.wants_to_sign_in(
            self._doc("Sign in with your phone number",
                      "Enter your cell phone number")) is True

    def test_the_code_screen_is_arrival_not_a_request(self):
        """Treating it as "please sign in" would put the loop back at a
        screen it had already reached — and each lap sends a text."""
        assert macros.wants_to_sign_in(
            self._doc("Verify Your Account",
                      "A text with a code was sent to",
                      "Enter your code", "Verify")) is False

    def test_the_signed_in_app_is_not_asking(self):
        assert macros.wants_to_sign_in(
            self._doc("Visitas", "Beneficiarios", "Mensajes")) is False

    def test_another_apps_screen_is_never_matched(self):
        """The words are loose on purpose; the package is what keeps them
        from reaching across apps."""
        assert macros.wants_to_sign_in(
            self._doc("Sign in with your phone number",
                      app="com.hhaexchange.uma")) is False

    def test_nothing_published_yet_is_not_asking(self):
        assert macros.wants_to_sign_in(None) is False
        assert macros.wants_to_sign_in({}) is False


class TestTheCodeIsAnnounced:
    """The one step in this system nobody can wait out: the code exists only
    on her phone. A portal that sits there silently asking is a portal nobody
    discovers is asking."""

    def test_reaching_the_code_screen_sends_a_notification(self):
        sent = []
        with patch("apt_log.notify.send",
                   side_effect=lambda m, url="": sent.append((m, url))):
            macros._say_the_code_is_waiting()
        assert len(sent) == 1
        message, url = sent[0]
        assert "code" in message.lower()
        # Tappable, and landing on the screen the code is for. This line read
        # `endswith("/app")` while its own comment said "straight to the
        # field", and the comment was the part that was wrong: /app is the app
        # picker. Reported from the field as the notification opening the page
        # with all the tiles on it.
        assert url.endswith(macros.CODE_DEEP_LINK)
        assert "view=screen" in url

    def test_the_link_is_a_name_a_phone_can_actually_open(self):
        """It was a bare host first, and `tailscale serve` holds a
        certificate for the node's WHOLE MagicDNS name — so the bare one
        fails validation instead of resolving to something friendlier. A tap
        that goes nowhere, on the one notification whose entire job is to be
        tapped."""
        from urllib.parse import urlparse

        parsed = urlparse(macros.PORTAL_URL)
        assert parsed.scheme == "https"
        assert parsed.hostname and parsed.hostname.count(".") >= 2, (
            f"{parsed.hostname!r} is not fully qualified")
        assert parsed.path == "/app"

    def test_it_carries_no_patient_and_no_code(self):
        """It goes to a public relay and lands on a lock screen. Neither is a
        place for a name, and there is no code to carry — that is the point
        of asking."""
        message = macros.CODE_WAITING
        assert not any(ch.isdigit() for ch in message)
        for leak in ("patient", "paciente", "visit", "visita"):
            assert leak not in message.lower()

    def test_a_notification_that_cannot_be_sent_never_fails_the_sign_in(self):
        with patch("apt_log.notify.send", side_effect=OSError("no network")):
            with pytest.raises(OSError):
                macros._say_the_code_is_waiting()
        # ...which is why the real helper swallows it. Proven against the
        # helper itself rather than assumed of the caller:
        from apt_log import notify

        with patch("apt_log.notify.subprocess.run",
                   side_effect=OSError("no curl")):
            assert notify.send("anything") is False


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

    def _folds_statics(self, n=3, today=True):
        mark = " (Hoy)" if today else ""
        out = [{"cls": "TextView", "b": [9, 347 + i*300, 98, 363 + i*300],
                "txt": f"agosto {17+i}, 2026" + (mark if i == 0 else "")}
               for i in range(n)]
        out += [{"cls": "TextView", "b": [25, 400 + i*40, 142, 416 + i*40],
                 "txt": f"linea {i}"} for i in range(4)]
        return out

    def test_returning_to_the_schedule_is_a_fresh_page(self, tmp_path):
        """Compose keeps every page in one activity, so (app, activity)
        cannot see schedule -> details -> Back. The page's own shape can:
        the schedule shows a run of date headers, the details page does
        not, and a change in foldability IS a page transition — the walk
        runs immediately, expansion allowed (assume_top on)."""
        runner = self._runner(tmp_path, app="com.hhaexchange.uma",
                              fid="sched1")
        self._repoint(runner, tmp_path, statics=self._folds_statics())
        walks = []
        with patch("apt_log.resident.run",
                   side_effect=lambda work: walks.append(True) or True):
            assert runner.maybe_stitch() is True           # schedule
            self._repoint(runner, tmp_path, fid="details1",
                          statics=[{"cls": "TextView",
                                    "b": [25, 400 + i*40, 142, 416 + i*40],
                                    "txt": f"detalle {i}"} for i in range(6)])
            assert runner.maybe_stitch() is True           # details, no wait
            self._repoint(runner, tmp_path, fid="sched2",
                          statics=self._folds_statics())
            assert runner.maybe_stitch() is True           # back: no wait
        assert len(walks) == 3

    def test_closing_a_card_stays_within_the_cooldown(self, tmp_path):
        """Folding a card changes the frame but not the page's shape: not
        a transition, so the floor holds and nothing re-expands over her
        hand."""
        runner = self._runner(tmp_path, app="com.hhaexchange.uma",
                              fid="sched1")
        self._repoint(runner, tmp_path, statics=self._folds_statics())
        with patch("apt_log.resident.run", return_value=True) as walk:
            assert runner.maybe_stitch() is True
            self._repoint(runner, tmp_path, fid="sched-closed",
                          statics=self._folds_statics())
            assert runner.maybe_stitch() is False
        walk.assert_called_once()

    def test_a_sparse_transition_tree_is_no_verdict(self, tmp_path):
        """A mid-transition frame publishes almost nothing; reading it as
        "not foldable" would make the settled schedule look like a fresh
        page and re-expand what she closed."""
        runner = self._runner(tmp_path, app="com.hhaexchange.uma",
                              fid="sched1")
        self._repoint(runner, tmp_path, statics=self._folds_statics())
        with patch("apt_log.resident.run", return_value=True) as walk:
            assert runner.maybe_stitch() is True
            self._repoint(runner, tmp_path, fid="blank",
                          statics=[{"cls": "TextView", "b": [9, 40, 98, 56],
                                    "txt": "cargando"}])
            assert runner.maybe_stitch() is False   # sparse: cooldown holds
            self._repoint(runner, tmp_path, fid="sched-settled",
                          statics=self._folds_statics())
            assert runner.maybe_stitch() is False   # same shape: still held
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
        # The first header is today's, marked the way the app marks it.
        return "".join(
            f'<node class="android.widget.TextView" '
            f'text="agosto {17+i}, 2026{" (Hoy)" if i == 0 else ""}" '
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

    def test_only_the_first_viewports_folds_are_opened(
            self, tmp_path, monkeypatch):
        """Two designs failed live before this one: opening cards as the
        walk reached them moved content between captures and the stitch
        published duplicated day headers; and a second clean sweep
        corrupted identically, because the app forgets an opened card
        once it scrolls out of view — a fully-open page does not exist to
        be rescanned. Only taps that land before anything is captured are
        stable, so a folded card that appears after the first swipe is
        left as its tappable header row."""
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        pages = {
            "top": self._dates() + self._card(401, "NIEVES C MASTRAPA"),
            "top_open": self._dates() + self._card(
                401, "NIEVES C MASTRAPA", folded=False, details=True),
            "below": self._dates() + self._card(401, "LUCRESIA L PUPO"),
        }
        state = {"page": "top"}
        driver = self._driver(state, pages)

        def tap(x, y):
            state["page"] = "top_open"

        def swipe(*_a, **_kw):
            state["page"] = "below"      # the next viewport scrolls in

        driver.swipe.side_effect = swipe
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros._tap_xy", side_effect=tap) as tapped:
            assert macros._stitch_walk(driver, assume_top=True) is True
        tapped.assert_called_once()      # the below-the-fold card: never

    def test_a_rescan_never_reopens_what_she_closed(self, tmp_path, monkeypatch):
        """Unfolding is part of arriving at a page, never part of watching
        one: a re-scan (assume_top=False) means someone changed the page —
        and if what changed is that she CLOSED a card, a scan that reopens
        it is the phone fighting her hand."""
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        pages = {"collapsed": self._dates() + self._card(401, "PACIENTE")}
        driver = self._driver({"page": "collapsed"}, pages)
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros._tap_xy") as tapped:
            macros._stitch_walk(driver, assume_top=False)
        tapped.assert_not_called()

    def test_only_todays_cards_are_opened_and_all_of_them(
            self, tmp_path, monkeypatch):
        """The owner's scoping: "we just care about today". Every card in
        today's section opens — two patients, three, however many share
        the day — and no card under any other date header is touched."""
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        def page(a_open, b_open):
            return (self._dates()
                    + self._card(401, "LUCRESIA L PUPO",
                                 folded=not a_open, details=a_open)
                    + self._card(501, "NIEVES C MASTRAPA",
                                 folded=not b_open, details=b_open)
                    + self._card(701, "PACIENTE FUTURO"))
        state = {"taps": []}
        pages = {0: page(False, False), 1: page(True, False),
                 2: page(True, True)}
        driver = MagicMock()
        driver.get_window_size.return_value = {"width": self.W, "height": self.H}
        driver.current_package = "com.hhaexchange.uma"
        type(driver).page_source = property(
            lambda _self: pages[len(state["taps"])])

        def tap(x, y):
            state["taps"].append((x, y))

        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros._tap_xy", side_effect=tap):
            assert macros._stitch_walk(driver, assume_top=True) is True
        assert state["taps"] == [(362, 417), (362, 517)]   # today only

    def test_no_today_header_on_screen_means_no_taps(
            self, tmp_path, monkeypatch):
        """A schedule scrolled past today (or filtered to another week)
        offers nothing this scan should touch."""
        monkeypatch.setattr(macros, "STITCH_DIR", tmp_path / "stitched")
        dates = "".join(
            f'<node class="android.widget.TextView" text="agosto {20+i}, 2026" '
            f'clickable="false" bounds="[9,{347+i*300}][98,{363+i*300}]"/>'
            for i in range(3))
        pages = {"collapsed": dates + self._card(401, "PACIENTE")}
        driver = self._driver({"page": "collapsed"}, pages)
        with patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros._tap_xy") as tapped:
            macros._stitch_walk(driver, assume_top=True)
        tapped.assert_not_called()


class TestCanvasNeverWalked:
    """A swipe on a signature canvas is ink. The walk decision skips
    canvas screens, and the walk itself re-checks the screen in front at
    swipe time — the checklist's Salvar landed on the signature screen
    while a walk was already due, and the scroll gesture drew a straight
    line down the canvas she was about to sign (seen live, first field
    test)."""

    def test_the_walk_aborts_on_a_live_canvas(self):
        from unittest.mock import MagicMock
        driver = MagicMock()
        driver.page_source = ('<node resource-id="app:id/gestureSignature" '
                              'class="android.widget.FrameLayout" '
                              'bounds="[28,90][700,1560]"/>')
        assert macros._stitch_walk(driver) is False
        driver.swipe.assert_not_called()
        driver.execute_script.assert_not_called()

    def test_the_decision_skips_a_canvas_doc(self, tmp_path):
        import json as _json
        doc = {"id": "f1", "app": "com.hhaexchange.caregiver",
               "activity": "visitdetailactivity", "screen": "visit",
               "blocked": "", "full": False, "canvas": True,
               "statics": [], "elements": []}
        (tmp_path / "screen.json").write_text(_json.dumps(doc),
                                              encoding="utf-8")
        (tmp_path / "viewers.json").write_text(_json.dumps({"n": 1}),
                                               encoding="utf-8")
        runner = macros.Runner(tmp_path / "req.json",
                               tmp_path / "status.json",
                               screen_path=tmp_path / "screen.json",
                               viewers_path=tmp_path / "viewers.json")
        assert runner.maybe_stitch() is False


class TestMobileCaregiverExpiredSession:
    """The second lock, found by walking it live.

    The passcode only unlocks the app on this phone. When the SERVER session
    lapses the dashboard opens, says "Sesión caducada", and drops to a
    username-and-password form no number of keypad taps can answer — which is
    how an auth macro spins forever without signing anything in.
    """

    FORM = "com.tellus.evv.activities.LoginActivity"
    EXPIRED = ("<node text='Sesión caducada'/>"
               "<node text='Su sesión ha caducado, por favor vuelva a "
               "iniciar sesión.'/>")

    def _driver(self, activity, source=""):
        from unittest.mock import PropertyMock
        state = {"activity": activity}
        driver = MagicMock()
        type(driver).current_activity = PropertyMock(
            side_effect=lambda: state["activity"])
        type(driver).page_source = PropertyMock(side_effect=lambda: source)
        return driver, state

    def _run(self, driver, **secrets):
        import itertools

        from apt_log.secrets import MemorySecretProvider

        with patch("apt_log.macros.wake_display"), \
             patch("apt_log.secrets.FileSecretProvider",
                   return_value=MemorySecretProvider(**secrets)), \
             patch("apt_log.macros.time.sleep"), \
             patch("apt_log.macros.time.monotonic",
                   side_effect=itertools.count(step=0.5)):
            macros.MACROS["mobile_caregiver_pin"].run(driver, lambda _k: None)

    def test_the_password_is_typed_into_the_form(self):
        from apt_log.secrets import MC_PASSWORD

        driver, state = self._driver(self.FORM)
        typed, boxes = [], {}

        def find_elements(by, selector):
            if "login_password_input" in selector:
                box = MagicMock()
                box.send_keys.side_effect = lambda v: typed.append(v)
                boxes["pw"] = box
                return [box]
            if "login_username_input" in selector:
                box = MagicMock()
                box.text = "SSequeira"          # remembered between sessions
                box.send_keys.side_effect = lambda v: typed.append(v)
                return [box]
            if "login_button" in selector:
                button = MagicMock()
                button.click.side_effect = lambda: state.update(
                    activity="com.tellus.evv.activities.DashboardActivity")
                return [button]
            return []

        driver.find_elements.side_effect = find_elements
        self._run(driver, **{MC_PASSWORD: "hunter2"})
        assert typed == ["hunter2"], "a remembered username is not retyped"

    def test_a_forgotten_username_is_filled_too(self):
        from apt_log.secrets import MC_PASSWORD, MC_USERNAME

        driver, state = self._driver(self.FORM)
        typed = []

        def find_elements(by, selector):
            if ("login_password_input" in selector
                    or "login_username_input" in selector):
                box = MagicMock()
                box.text = ""
                box.send_keys.side_effect = lambda v: typed.append(v)
                return [box]
            if "login_button" in selector:
                button = MagicMock()
                button.click.side_effect = lambda: state.update(
                    activity="com.tellus.evv.activities.DashboardActivity")
                return [button]
            return []

        driver.find_elements.side_effect = find_elements
        self._run(driver, **{MC_USERNAME: "SSequeira", MC_PASSWORD: "hunter2"})
        assert typed == ["SSequeira", "hunter2"]

    def test_without_a_password_nothing_is_typed_at_all(self):
        driver, _state = self._driver(self.FORM)
        driver.find_elements.side_effect = lambda *_a: []
        with pytest.raises(RuntimeError, match="none is stored"):
            self._run(driver)

    def test_only_the_expiry_dialog_is_dismissed(self):
        """Pressing whatever sits at android:id/button1 would press the
        positive button of any alert the app happened to be showing."""
        driver, _state = self._driver(
            "com.tellus.evv.activities.DashboardActivity",
            source="<node text='¿Borrar esta visita?'/>")
        clicked = []

        def find_elements(by, selector):
            if "button1" in selector:
                button = MagicMock()
                button.click.side_effect = lambda: clicked.append(1)
                return [button]
            return []

        driver.find_elements.side_effect = find_elements
        self._run(driver)
        assert clicked == [], "an unrecognised dialog stays untouched"

    def test_the_expiry_dialog_is_dismissed_to_reach_the_form(self):
        from apt_log.secrets import MC_PASSWORD

        driver, state = self._driver(
            "com.tellus.evv.activities.DashboardActivity",
            source=self.EXPIRED)
        clicked = []

        def find_elements(by, selector):
            if "button1" in selector:
                button = MagicMock()

                def click():
                    clicked.append(1)
                    state["activity"] = self.FORM
                button.click.side_effect = click
                return [button]
            if ("login_password_input" in selector
                    or "login_username_input" in selector):
                box = MagicMock()
                box.text = "SSequeira"
                return [box]
            if "login_button" in selector:
                button = MagicMock()
                button.click.side_effect = lambda: state.update(
                    activity="com.tellus.evv.activities.DashboardActivity")
                return [button]
            return []

        driver.find_elements.side_effect = find_elements
        self._run(driver, **{MC_PASSWORD: "hunter2"})
        assert clicked == [1]


    def test_the_form_is_found_under_a_foreign_id_namespace(self):
        """The live failure: UiAutomator2 prefixes a bare id with the app's
        package, and this app's package is not the namespace its ids live
        in — application com.tellus.evv.v2, resources com.tellus.evv. Every
        bare-id lookup came back empty on a form that was plainly there."""
        from apt_log.secrets import MC_PASSWORD

        driver, state = self._driver(self.FORM)
        typed = []
        # Only ids under the FOREIGN namespace exist here. A locator that
        # assumes the application package finds nothing.
        tree = {"com.tellus.evv:id/login_username_input": "SSequeira",
                "com.tellus.evv:id/login_password_input": "",
                "com.tellus.evv:id/login_button": None}

        def find_elements(by, selector):
            if by != "xpath":
                return []
            found = []
            for rid, text in tree.items():
                needle = selector.split('":id/')[-1].split('"')[0] \
                    if '":id/' in selector else None
                if needle and rid.endswith(":id/" + needle):
                    box = MagicMock()
                    box.text = text
                    box.send_keys.side_effect = lambda v: typed.append(v)
                    box.click.side_effect = lambda: state.update(
                        activity="com.tellus.evv.activities.DashboardActivity")
                    found.append(box)
            return found

        driver.find_elements.side_effect = find_elements
        self._run(driver, **{MC_PASSWORD: "hunter2"})
        assert typed == ["hunter2"]

    def test_the_sign_in_button_does_not_catch_its_neighbours(self):
        """login_passwordhelp_button and login_forgotusername_button sit
        beside it; anchoring on ":id/" keeps the match to a whole segment."""
        from apt_log import macros as m
        for rid in ("com.tellus.evv:id/login_passwordhelp_button",
                    "com.tellus.evv:id/login_forgotusername_button"):
            assert ':id/login_button' not in rid
        assert ':id/login_button' in "com.tellus.evv:id/login_button"
        assert ':id/login_button"' in m._MC_SIGN_IN

    def test_the_macro_is_offered_on_a_password_alone(self):
        """The app locks two ways; either secret makes it worth offering."""
        from apt_log.secrets import MC_PASSWORD, MC_PIN, MemorySecretProvider

        only_password = MemorySecretProvider(**{MC_PASSWORD: "hunter2"})
        only_pin = MemorySecretProvider(**{MC_PIN: "2580"})
        neither = MemorySecretProvider()
        for provider, expected in ((only_password, "mobile_caregiver_pin"),
                                   (only_pin, "mobile_caregiver_pin"),
                                   (neither, None)):
            assert macros.auth_macro_for(
                "com.tellus.evv.v2", provider) == expected


class TestClearScreen:
    """The hand-operated way out from under a system panel.

    Written because the automatic one could not do the job and there was
    nothing for a person to press: the shade sat over inMyTeam for twenty
    minutes, the collapse command reported success every time, and the portal
    went on rendering the app underneath as though it were in front.
    """

    def _run(self, focus_seq, front_seq, last_care="com.inmyteam.inmyteam"):
        from apt_log import feed as feed_mod

        calls = []
        collapsed = []
        focus = list(focus_seq)
        front = list(front_seq)

        def fake_adb(args, serial=None, **kw):
            calls.append(args)
            return type("R", (), {"stdout": b"", "returncode": 0})()

        with patch.object(feed_mod, "collapse_shade",
                          side_effect=lambda *a, **k: collapsed.append(1)), \
             patch.object(feed_mod, "current_focus",
                          side_effect=lambda *a, **k: focus.pop(0)
                          if focus else ""), \
             patch.object(feed_mod, "_adb", side_effect=fake_adb), \
             patch.object(feed_mod, "last_care_app", return_value=last_care), \
             patch.object(macros, "_front_package",
                          side_effect=lambda *a, **k: front.pop(0)
                          if len(front) > 1 else (front[0] if front else "")), \
             patch.object(macros, "wait_for", return_value=True), \
             patch.object(macros.time, "sleep"):
            macros._clear_screen(MagicMock(), lambda step: None)
        return calls, collapsed

    def test_the_shade_is_collapsed_first(self):
        calls, collapsed = self._run(
            ["com.inmyteam.inmyteam/.Main"], ["com.inmyteam.inmyteam"])
        assert collapsed, "the swipe that actually works must be tried"

    def test_a_panel_the_swipe_cannot_reach_gets_a_back(self):
        """The volume dialog is the other one she can raise by accident, and
        a shade swipe does nothing to it."""
        calls, _ = self._run(["VolumeDialog"], ["com.inmyteam.inmyteam"])
        assert ["shell", "input", "keyevent", "KEYCODE_BACK"] in calls

    def test_back_is_never_spent_on_the_app_itself(self):
        """A Back into a care app is a navigation she did not ask for — and
        mid-visit it is a step backwards out of a form."""
        calls, _ = self._run(["com.inmyteam.inmyteam/.Main"],
                             ["com.inmyteam.inmyteam"])
        assert not [c for c in calls if "keyevent" in c]

    def test_the_care_app_is_brought_back(self):
        calls, _ = self._run(["com.inmyteam.inmyteam/.Main"],
                             ["com.android.settings"])
        launches = [c for c in calls if "monkey" in c]
        assert launches and "com.inmyteam.inmyteam" in launches[0]

    def test_an_app_already_in_front_is_not_relaunched(self):
        """Gentle is the whole promise: relaunching inMyTeam mid-visit to fix
        a shade would cost more than the shade did."""
        calls, _ = self._run(["com.inmyteam.inmyteam/.Main"],
                             ["com.inmyteam.inmyteam"])
        assert not [c for c in calls if "monkey" in c]

    def test_nothing_is_force_stopped(self):
        calls, _ = self._run(["VolumeDialog"], ["com.android.settings"])
        assert not [c for c in calls if "force-stop" in c]
        assert not [c for c in calls if "am" in c]

    def test_no_care_app_seen_means_no_guess(self):
        """Never launch a package the watchdog has not actually watched."""
        calls, _ = self._run(["com.android.settings/.Settings"],
                             ["com.android.settings"], last_care="")
        assert not [c for c in calls if "monkey" in c]

    def test_it_is_offered_as_an_operation(self):
        assert "clear_screen" in macros.OPERATIONS
        assert "clear_screen" in macros.MACROS

    def test_it_asks_for_no_confirmation(self):
        """Restarting the phone does; dismissing a panel does not. A
        confirmation on the one button that unsticks the screen is a second
        thing to get past while stuck."""
        assert "clear_screen" not in macros.CONFIRM


class TestCheckStarredTasks:
    """Ticking the plan of care's required tasks.

    "if there is a star next to a task we must check mark it" — thirteen
    boxes of 32px, driven from another state, before every check-out.

    Bounds below are the live ones off inMyTeam's check-out page at 720x1600.
    Two CheckBox columns, identical in class, size, id (none) and caption
    (none): the task's own tick at x=24, and "Patient refused" at x=591.
    Position is the only thing that tells them apart, which is why it is the
    rule — and why the wrong one is a test rather than a comment.
    """

    W, H = 720, 1600

    def _page(self, checked=(), refused_checked=()):
        rows = []
        for i in range(13):
            top = 307 + i * 42
            rows.append(
                f'<node class="android.widget.CheckBox" resource-id=""'
                f' bounds="[24,{top}][56,{top + 32}]" enabled="true"'
                f' clickable="true"'
                f' checked="{"true" if i in checked else "false"}" />')
            rows.append(
                f'<node class="android.widget.CheckBox" resource-id=""'
                f' bounds="[591,{top + 17}][623,{top + 49}]" enabled="true"'
                f' clickable="true"'
                f' checked="{"true" if i in refused_checked else "false"}" />')
            rows.append(
                f'<node class="android.widget.TextView" text="*"'
                f' bounds="[16,{top + 9}][24,{top + 25}]" />')
        # The instruction sentence and both signature captions carry a star
        # too, and none of them is a task.
        rows.append('<node class="android.widget.TextView" text="*"'
                    ' bounds="[16,261][24,277]" />')
        rows.append('<node class="android.widget.TextView" text="*"'
                    ' bounds="[16,1007][24,1023]" />')
        rows.append('<node class="android.widget.TextView" text="*"'
                    ' bounds="[16,1132][24,1148]" />')
        return "<hierarchy>" + "".join(rows) + "</hierarchy>"

    def _driver(self, xml):
        driver = MagicMock()
        driver.page_source = xml
        driver.get_window_size.return_value = {"width": self.W,
                                               "height": self.H}
        return driver

    def test_every_starred_task_is_found(self):
        found = macros._starred_tasks(self._driver(self._page()))
        assert len(found) == 13

    def test_the_refused_column_is_never_one_of_them(self):
        """Ticking it says the opposite of what this macro is for."""
        found = macros._starred_tasks(self._driver(self._page()))
        assert all(t["b"][0] < self.W * 0.25 for t in found)
        assert not [t for t in found if t["b"][0] == 591]

    def test_a_star_with_no_tick_on_its_line_is_not_a_task(self):
        """The instruction sentence and the two signature captions each carry
        one. Matching them would be the macro wandering off the task list."""
        found = macros._starred_tasks(self._driver(self._page()))
        for stray in (261, 1007, 1132):
            assert not [t for t in found
                        if t["b"][1] <= stray + 8 <= t["b"][3]]

    def test_already_ticked_tasks_are_left_alone(self):
        """A tap TOGGLES. Running this over a half-filled list would undo the
        caregiver's own work."""
        found = macros._starred_tasks(
            self._driver(self._page(checked=(0, 3, 7))))
        assert len(found) == 10

    def test_a_fully_ticked_page_asks_for_nothing(self):
        found = macros._starred_tasks(
            self._driver(self._page(checked=tuple(range(13)))))
        assert found == []

    def test_a_refused_task_is_still_offered(self):
        """Refusing is a different statement from doing, and the star still
        stands. Whether the two may both be on is the app's business."""
        found = macros._starred_tasks(
            self._driver(self._page(refused_checked=(2,))))
        assert len(found) == 13

    def test_nothing_happens_without_a_screen_size(self):
        driver = self._driver(self._page())
        driver.get_window_size.return_value = {}
        assert macros._starred_tasks(driver) == []

    def test_it_taps_each_pending_box_once(self):
        driver = self._driver(self._page())
        taps = []
        with patch.object(macros, "_tap_xy",
                          side_effect=lambda x, y: taps.append((x, y))), \
             patch.object(macros.time, "sleep"), \
             patch.object(macros, "_pending_tasks",
                          side_effect=[macros._starred_tasks(driver), []]):
            macros._check_tasks(driver, lambda step: None)
        assert len(taps) == 13
        assert all(x < self.W * 0.25 for x, _ in taps)

    def test_a_box_that_did_not_take_is_named(self):
        """A plan of care submitted a tick short is rejected by the agency,
        and she finds out hours later."""
        driver = self._driver(self._page())
        pending = macros._starred_tasks(driver)
        with patch.object(macros, "_tap_xy"), \
             patch.object(macros.time, "sleep"), \
             patch.object(macros, "_pending_tasks",
                          side_effect=[pending, pending[:2]]):
            with pytest.raises(RuntimeError, match="did not tick"):
                macros._check_tasks(driver, lambda step: None)

    def test_it_is_registered_with_a_name_in_both_languages(self):
        import json
        from pathlib import Path

        assert "check_tasks" in macros.MACROS
        base = Path(__file__).resolve().parents[1] / "src/apt_log/ui/locales"
        for name in ("en.json", "es.json"):
            words = json.loads((base / name).read_text(encoding="utf-8"))
            for key in ("macro.check_tasks", "macro.step.checking",
                        "macro.step.nothing_to_check"):
                assert words.get(key), f"{name} is missing {key}"

    def test_it_only_ever_taps_and_reads(self):
        """The tedious half is what this automates. Every consequential
        decision on the page stays hers — so the body presses nothing but the
        boxes it found, and reaches for no other control."""
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(macros._check_tasks)))
        called = {n.func.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert called <= {"_pending_tasks", "_tap_xy", "report", "len",
                          "RuntimeError"}, f"unexpected calls: {called}"


class TestCheckingHHAeXchangePlusTasks:
    """The same button, on the other app's plan of care.

    HHAeXchange+ writes this page nothing like inMyTeam does. Every task
    carries a named pair of its own — `poc_task_item_status_completed_false`
    beside `poc_task_item_status_refused_false` — and neither is marked
    clickable: the app declares them in a screen reader's sentence instead
    ("no seleccionado  Se realizó  %1$s  Toca dos veces para activar").

    Bounds and ids below are the live ones off the Funciones page at 720x1600,
    recorded with the caregiver standing in the patient's home.
    """

    W, H = 720, 1600

    def _page(self, done=(), refused=(), count=14):
        def tick(kind, i, on, top):
            state = "true" if on else "false"
            word = "seleccionado" if on else "no seleccionado"
            caption = "Se realizó" if kind == "completed" else "No realizado"
            left = 636 if kind == "completed" else 667
            return (f'<node class="android.view.View"'
                    f' resource-id="poc_task_item_status_{kind}_{state}"'
                    f' clickable="false" text=""'
                    f' content-desc="{word} {caption} %1$s Toca dos veces'
                    f' para activar"'
                    f' bounds="[{left},{top}][{left + 31},{top + 29}]"'
                    f' package="com.hhaexchange.uma" />')

        rows = []
        for i in range(count):
            top = 150 + i * 51
            rows.append(f'<node class="android.widget.TextView"'
                        f' text="Tarea {i}" bounds="[37,{top}][110,{top+10}]"'
                        f' package="com.hhaexchange.uma" />')
            rows.append(tick("completed", i, i in done, top))
            rows.append(tick("refused", i, i in refused, top))
        rows.append('<node class="android.view.View" clickable="true"'
                    ' resource-id="poc_task_save_button" text=""'
                    ' content-desc="Salvar" bounds="[11,1500][709,1525]"'
                    ' package="com.hhaexchange.uma" />')
        return "<hierarchy>" + "".join(rows) + "</hierarchy>"

    def _driver(self, xml):
        driver = MagicMock()
        driver.page_source = xml
        driver.get_window_size.return_value = {"width": self.W,
                                               "height": self.H}
        return driver

    def test_every_task_is_found(self):
        assert len(macros._poc_tasks(self._driver(self._page()))) == 14

    def test_the_refused_column_is_never_one_of_them(self):
        """Saying "the patient refused this" on her behalf is not a thing
        this macro will ever do. Nothing named `refused` is even looked at."""
        found = macros._poc_tasks(self._driver(self._page()))
        assert all(POC in t["rid"] for t in found
                   for POC in [macros.POC_DONE_ID])
        assert not [t for t in found if "refused" in t["rid"]]

    def test_a_task_already_done_is_left_alone(self):
        """A tap TOGGLES — this would take her tick back off."""
        found = macros._poc_tasks(self._driver(self._page(done=(0, 3, 7))))
        assert len(found) == 11

    def test_a_fully_ticked_page_asks_for_nothing(self):
        assert macros._poc_tasks(
            self._driver(self._page(done=tuple(range(14))))) == []

    def test_a_refused_task_is_still_offered(self):
        """Refusing is a different statement from doing; whether both may be
        on is the app's business, not this macro's."""
        assert len(macros._poc_tasks(
            self._driver(self._page(refused=(2,))))) == 14

    def test_both_readings_have_to_agree_before_anything_is_pressed(self):
        """The id's last word and the state written into the description come
        from different places and are parsed by different code. If they ever
        disagree the task is left alone and she decides — because a wrong
        read here does not merely fail to help, it takes a tick BACK OFF."""
        xml = self._page().replace(
            'resource-id="poc_task_item_status_completed_false"'
            ' clickable="false" text=""'
            ' content-desc="no seleccionado Se realizó %1$s Toca dos veces'
            ' para activar" bounds="[636,150][667,179]"',
            'resource-id="poc_task_item_status_completed_true"'
            ' clickable="false" text=""'
            ' content-desc="no seleccionado Se realizó %1$s Toca dos veces'
            ' para activar" bounds="[636,150][667,179]"', 1)
        assert len(macros._poc_tasks(self._driver(xml))) == 13

    def test_the_save_button_is_not_a_task(self):
        found = macros._poc_tasks(self._driver(self._page()))
        assert not [t for t in found if "save" in t["rid"]]

    # ------------------------------------------------------- one button, two
    def test_the_app_in_front_decides_which_reading(self):
        driver = self._driver(self._page())
        with patch.object(macros, "_front_package",
                          return_value="com.hhaexchange.uma"):
            assert len(macros._pending_tasks(driver)) == 14
        with patch.object(macros, "_front_package",
                          return_value="com.inmyteam.inmyteam"):
            # inMyTeam's reading finds no stars on this page, and says so
            # rather than pressing anything it half-recognises.
            assert macros._pending_tasks(driver) == []

    def test_it_taps_each_pending_pair_once_on_the_done_side(self):
        driver = self._driver(self._page())
        taps = []
        with patch.object(macros, "_tap_xy",
                          side_effect=lambda x, y: taps.append((x, y))), \
             patch.object(macros.time, "sleep"), \
             patch.object(macros, "_pending_tasks",
                          side_effect=[macros._poc_tasks(driver), []]):
            macros._check_tasks(driver, lambda step: None)
        assert len(taps) == 14
        # 636..667 is Se realizó; 667..698 is No realizado.
        assert all(636 <= x <= 667 for x, _ in taps)

    def test_a_tick_that_did_not_take_is_named(self):
        driver = self._driver(self._page())
        pending = macros._poc_tasks(driver)
        with patch.object(macros, "_tap_xy"), \
             patch.object(macros.time, "sleep"), \
             patch.object(macros, "_pending_tasks",
                          side_effect=[pending, pending[:3]]):
            with pytest.raises(RuntimeError, match="did not tick"):
                macros._check_tasks(driver, lambda step: None)

    def test_it_still_only_taps_and_reads(self):
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(macros._poc_tasks)))
        called = {n.func.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert not called & {"_tap_xy", "_press", "_swipe"}


class TestSomeoneIsWatching:
    """"Instead of an auth trigger we were stranded on the signin page so I
    had to leave and click on the app for her to trigger an auth."

    Auto sign-in is gated on somebody being on the portal, and rightly: an
    unwatched phone loops sign-in against its own inactivity timer all night,
    which is what the gate was built after.

    But the count is LIVE SOCKETS, and a phone's browser drops its socket for
    every ordinary reason — the screen locks, she switches to the camera, iOS
    suspends the tab. Read strictly that is "nobody is watching", so the
    app's fifteen-minute timer signed the session out mid-visit and the one
    thing that exists to fix that refused to run.
    """

    def _write(self, tmp_path, payload, age=0.0):
        import os

        target = tmp_path / "viewers.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        if age:
            when = time.time() - age
            os.utime(target, (when, when))
        return target

    def test_somebody_on_a_socket_counts(self, tmp_path):
        assert macros.someone_is_watching(
            self._write(tmp_path, {"n": 1, "seen": time.time()}))

    def test_nobody_ever_does_not(self, tmp_path):
        assert not macros.someone_is_watching(
            self._write(tmp_path, {"n": 0, "seen": 0}))

    def test_a_browser_that_backgrounded_still_counts(self, tmp_path):
        """The socket is gone; she is still standing in the kitchen."""
        assert macros.someone_is_watching(
            self._write(tmp_path, {"n": 0, "seen": time.time() - 120}))

    def test_but_not_hours_later(self, tmp_path):
        """Three in the morning, which is the case the gate exists for."""
        assert not macros.someone_is_watching(
            self._write(tmp_path, {"n": 0, "seen": time.time() - 6 * 3600}))

    def test_the_window_outlasts_the_apps_own_timeout(self, tmp_path):
        """HHAeXchange+ signs out after fifteen minutes idle. A window
        shorter than that would close before the thing it is waiting for."""
        assert macros.ATTENDED_WINDOW > 15 * 60

    def test_a_stale_file_cannot_claim_a_live_socket(self, tmp_path):
        """The mtime is the liveness half: a crashed UI must read as nobody
        watching, not as somebody."""
        assert not macros.someone_is_watching(
            self._write(tmp_path, {"n": 3, "seen": 0}, age=600))

    def test_a_stale_file_can_still_carry_its_stamp(self, tmp_path):
        """The stamp carries its own age, so it does not need the mtime —
        and a UI that died two minutes ago was watched two minutes ago."""
        assert macros.someone_is_watching(
            self._write(tmp_path, {"n": 3, "seen": time.time() - 120},
                        age=600))

    def test_a_stamp_from_the_future_is_not_trusted(self, tmp_path):
        assert not macros.someone_is_watching(
            self._write(tmp_path, {"n": 0, "seen": time.time() + 9999}))

    def test_nonsense_is_refused_rather_than_guessed(self, tmp_path):
        for payload in ({"n": "x"}, {"seen": "soon"}, {}, []):
            assert not macros.someone_is_watching(
                self._write(tmp_path, payload))

    def test_a_missing_file_is_nobody(self, tmp_path):
        assert not macros.someone_is_watching(tmp_path / "nope.json")
