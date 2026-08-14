"""REQ-2/REQ-3 smoke check — open the app, sign in, and say what happened.

`probe` answers whether this app *can* be driven at all (REQ-1). This answers a
narrower and more perishable question: does today's device, with today's
credentials, against today's build of the app, actually get as far as a
signed-in session. That is the thing that breaks quietly — a rotated password,
an app update that renames the submit control, a phone that came back from a
reboot locked — and none of it shows up until a scheduled run fails at 09:00.

**It signs in and stops.** No agency is chosen and nothing is checked off, so it
writes nothing to the agency's record. Agency selection is the first screen
where a wrong move has consequences (screens/agency.py), and a smoke check has
no business being there.

**Nothing here prints screen text.** Steps report activity names, gate names and
counts. Where a dialog has to be read, the check says so and points at
`apt-log inspect`, which owns the redaction rules — rather than growing a second,
weaker copy of them here. Output is meant to be safe to paste into a chat, which
is the only reason anyone will run it and send the result on.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from apt_log.device import DEFAULT_SERVER, DeviceSession, DeviceUnavailable
from apt_log.probe import Status, _adb, _app_running
from apt_log.screens import session as session_screen
from apt_log.screens.agency import AgencyScreen
from apt_log.screens.home import HomeScreen
from apt_log.screens.login import LoginError, LoginScreen, _redact
from apt_log.secrets import APP_PASSWORD, APP_USERNAME, SecretNotFound, SecretProvider
from apt_log.transport import TransportMode

log = logging.getLogger(__name__)

# How long to wait for the process to come back after a force-stop and relaunch.
LAUNCH_TIMEOUT = 20.0


@dataclass
class Step:
    number: int
    name: str
    status: Status
    detail: str = ""


@dataclass
class LoginCheckReport:
    package: str
    steps: list[Step] = field(default_factory=list)
    transport_mode: TransportMode | None = None

    def add(self, number: int, name: str, status: Status, detail: str = "") -> Step:
        step = Step(number, name, status, detail)
        self.steps.append(step)
        log.info("%s: %s %s", name, status.value, detail)
        return step

    @property
    def failures(self) -> list[Step]:
        return [s for s in self.steps if s.status is Status.FAIL]

    @property
    def unknowns(self) -> list[Step]:
        return [s for s in self.steps if s.status is Status.UNKNOWN]

    @property
    def signed_in(self) -> bool:
        """True only when the run positively observed a signed-in screen.

        Unlike the probe, UNKNOWN is not a neutral verdict here. The probe asks
        whether a project is possible and refuses to kill it on a maybe; this
        asks whether a sign-in happened, and a run that could not tell has not
        established that it did.
        """
        return not self.failures and not self.unknowns

    def render(self) -> str:
        width = max((len(s.name) for s in self.steps), default=10)
        lines = [f"REQ-3 login check — {self.package}", ""]
        for s in self.steps:
            lines.append(f"  {s.number}. {s.name:<{width}}  {s.status.value}")
            if s.detail:
                for chunk in _wrap(s.detail):
                    lines.append(f"       {chunk}")
        lines.append("")

        if self.transport_mode is TransportMode.DEV:
            lines.append(
                "Transport mode is 'dev' — the phone is attached over TCP, so this "
                "run says nothing about where it is (REQ-5.4.1)."
            )
        if self.failures:
            names = ", ".join(f"#{s.number}" for s in self.failures)
            lines.append(f"NOT SIGNED IN: step(s) failed: {names}")
        elif self.unknowns:
            lines.append(
                "No step failed, but the run could not confirm a signed-in screen. "
                "Read the details above before treating this as a pass."
            )
        else:
            lines.append("Signed in. Nothing was checked off and no agency was chosen.")
        return "\n".join(lines)


def _wrap(text: str, width: int = 72) -> list[str]:
    """Fold a detail line without pulling in textwrap's paragraph handling."""
    out: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            if line and len(line) + 1 + len(word) > width:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        out.append(line)
    return out


def _activity(driver) -> str:
    try:
        return driver.current_activity or ""
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------- the steps


def _check_credentials(report: LoginCheckReport, secrets: SecretProvider) -> bool:
    """Read the credentials before touching the phone.

    Order matters. With `--clear-data` the alternative is to wipe the app's local
    state, walk the language wheel, arrive at the login form and only then
    discover the secrets file is missing a key — leaving the phone signed out
    with nothing able to sign it back in.
    """
    missing: list[str] = []
    for key in (APP_USERNAME, APP_PASSWORD):
        try:
            if not secrets.get(key):
                missing.append(key)
        except SecretNotFound:
            missing.append(key)
        except PermissionError as exc:
            report.add(1, "credentials", Status.FAIL, str(exc))
            return False

    if missing:
        report.add(
            1, "credentials", Status.FAIL,
            f"not configured: {', '.join(missing)}. Set them with "
            "scripts/set-credentials.sh; never pass a password on the command "
            "line (REQ-3).",
        )
        return False

    report.add(1, "credentials", Status.PASS,
               f"{APP_USERNAME} and {APP_PASSWORD} are readable")
    return True


def _start_app(
    report: LoginCheckReport,
    session: DeviceSession,
    *,
    cold_start: bool,
    clear_data: bool,
) -> bool:
    package = session.package

    # --clear-data implies a restart: pm clear kills the process, so leaving it
    # unlaunched would attach to nothing. Honouring one flag by quietly ignoring
    # the other is the failure mode worth avoiding here.
    if not cold_start and not clear_data:
        report.add(
            3, "app start", Status.SKIP,
            "--no-cold-start: the app was left in whatever state it was already in",
        )
        return True

    if clear_data:
        cleared = _adb("shell", "pm", "clear", package)
        if "Success" not in cleared.stdout:
            report.add(
                3, "app start", Status.FAIL,
                "pm clear did not report success: "
                f"{cleared.stdout.strip() or cleared.stderr.strip() or 'no output'}",
            )
            return False

    session.cold_start()

    deadline = time.monotonic() + LAUNCH_TIMEOUT
    while time.monotonic() < deadline:
        if _app_running(package):
            detail = "force-stopped and relaunched; process is up"
            if clear_data:
                detail = (
                    "local data cleared, then relaunched; process is up. The app "
                    "starts from language selection, so the login form is "
                    "guaranteed to be exercised."
                )
            report.add(3, "app start", Status.PASS, detail)
            return True
        time.sleep(1.0)

    report.add(
        3, "app start", Status.FAIL,
        f"process did not come up within {LAUNCH_TIMEOUT:.0f}s of launch. Run "
        "`apt-log probe` — a launch that dies under automation is REQ-1 check 4, "
        "not a login problem.",
    )
    return False


def _wait_for_landing(driver, *, timeout: float, poll: float) -> Step:
    """Where the app ended up. Polls, because the transition is not instant."""
    deadline = time.monotonic() + timeout
    while True:
        # Checked first: a dialog sits on top of whatever is behind it, so
        # finding the home menu underneath one is not a clean sign-in.
        if session_screen.interrupted(driver):
            if session_screen.is_expired(driver):
                return Step(
                    8, "signed in", Status.FAIL,
                    "the session-expiry dialog is on screen. The credentials may "
                    "be fine — the app dropped the session again — but this run "
                    "did not reach a signed-in screen.",
                )
            return Step(
                8, "signed in", Status.FAIL,
                "a dialog is blocking the screen. Its text is not printed here; "
                "read it with `apt-log inspect --text-for lbl_message`.",
            )
        if AgencyScreen(driver).is_displayed():
            return Step(
                8, "signed in", Status.PASS,
                "reached agency selection — the app accepted the credentials. "
                "Stopping here: choosing an employer belongs to the check-off "
                "flow, not to a smoke check.",
            )
        if HomeScreen(driver).is_displayed():
            return Step(8, "signed in", Status.PASS,
                        "reached the home menu")
        if time.monotonic() >= deadline:
            break
        time.sleep(poll)

    if LoginScreen(driver).is_displayed():
        return Step(
            8, "signed in", Status.FAIL,
            f"still on the login form {timeout:.0f}s after submitting. Either the "
            "app rejected the credentials, or the control that was tapped is not "
            "the one that submits. Check the secrets file first, then re-read the "
            "form with `apt-log inspect`.",
        )
    return Step(
        8, "signed in", Status.UNKNOWN,
        f"neither agency selection nor the home menu within {timeout:.0f}s "
        f"(activity: {_activity(driver) or 'unknown'}). The app may have gained a "
        "screen between login and agency selection — inspect it and teach this "
        "check about it rather than widening what counts as success.",
    )


# ------------------------------------------------------------------- the check


def run_login_check(
    package: str,
    secrets: SecretProvider,
    *,
    activity: str | None = None,
    server_url: str = DEFAULT_SERVER,
    language: str | None = None,
    cold_start: bool = True,
    clear_data: bool = False,
    gate_timeout: float = 45.0,
    settle_timeout: float = 30.0,
    poll: float = 1.0,
    session: DeviceSession | None = None,
) -> LoginCheckReport:
    """Drive launch → startup gates → sign-in, reporting each step.

    `session` is injectable so the flow can be tested without a phone; nothing
    else here is, because everything else *is* the thing under test.
    """
    from apt_log.screens.language import advance_past_startup_gates

    report = LoginCheckReport(package=package)
    session = session or DeviceSession(
        package, secrets, activity=activity, server_url=server_url
    )

    if not _check_credentials(report, secrets):
        return report

    # --- 2. device --------------------------------------------------------
    try:
        serial = session.ensure_device()
        session.wake_and_unlock()
    except DeviceUnavailable as exc:
        report.add(2, "device", Status.FAIL, str(exc))
        return report
    except Exception as exc:  # noqa: BLE001 — adb itself failing, most likely
        report.add(2, "device", Status.FAIL, f"{type(exc).__name__}: {exc}")
        return report
    report.transport_mode = session.transport_mode
    mode = report.transport_mode.value if report.transport_mode else "unknown"
    report.add(2, "device", Status.PASS,
               f"{serial} attached over {mode}; woken and unlocked")

    # --- 3. app start -----------------------------------------------------
    if not _start_app(report, session, cold_start=cold_start, clear_data=clear_data):
        return report

    # --- 4. Appium session ------------------------------------------------
    try:
        driver = session.attach()
    except Exception as exc:  # noqa: BLE001 — the failure text is the finding
        report.add(
            4, "appium session", Status.FAIL,
            f"{type(exc).__name__}: {exc}. `apt-log probe` distinguishes a "
            "client/server mismatch from the app refusing automation.",
        )
        return report
    report.add(4, "appium session", Status.PASS, f"connected to {session.server_url}")

    try:
        # --- 5. startup gates ---------------------------------------------
        try:
            gates = advance_past_startup_gates(
                driver, timeout=gate_timeout, poll=poll, language=language
            )
        except Exception as exc:  # noqa: BLE001
            report.add(5, "startup gates", Status.FAIL,
                       f"{type(exc).__name__}: {exc}")
            return report
        report.add(5, "startup gates", Status.PASS,
                   ", ".join(gates) if gates else "none were in the way")

        # --- 6/7. the login form ------------------------------------------
        screen = LoginScreen(driver)
        if screen.is_displayed():
            report.add(6, "login form", Status.PASS, "a password field is on screen")
            try:
                screen.login(secrets)
            except LoginError as exc:
                report.add(
                    7, "credentials submitted", Status.FAIL,
                    f"{exc}. This is the form refusing to be driven blind, not a "
                    "wrong password.",
                )
                return report
            except Exception as exc:  # noqa: BLE001 — a dead session, most likely
                report.add(7, "credentials submitted", Status.FAIL,
                           f"{type(exc).__name__}: {exc}")
                return report
            report.add(
                7, "credentials submitted", Status.PASS,
                f"filled and submitted as {_redact(secrets.get(APP_USERNAME))}",
            )
        else:
            report.add(
                6, "login form", Status.SKIP,
                "no password field — the app is still signed in. A force-stop "
                "keeps the session; pass --clear-data to wipe it and exercise the "
                "login path for real.",
            )
            report.add(7, "credentials submitted", Status.SKIP, "nothing to sign in to")

        # --- 8. where it landed -------------------------------------------
        try:
            landed = _wait_for_landing(driver, timeout=settle_timeout, poll=poll)
        except Exception as exc:  # noqa: BLE001
            landed = Step(
                8, "signed in", Status.FAIL,
                f"could not read the screen after submitting: "
                f"{type(exc).__name__}: {exc}",
            )
        report.add(landed.number, landed.name, landed.status, landed.detail)
    finally:
        session.close()

    return report
