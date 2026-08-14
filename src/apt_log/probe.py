"""REQ-1 feasibility probe.

Answers whether this project is possible at all, before any feature code exists.

The probe's job is to be *right*, not decisive. A false "the app blocks automation"
kills a viable project; a false PASS spends weeks discovering the same wall later.
Where a check cannot be determined confidently it reports UNKNOWN with what it saw,
rather than guessing.

Checks 1, 2 and 4 are blocking: if any fails, no stack choice rescues it and the
vendor's sanctioned API becomes the only viable path (REQ-0).
"""

from __future__ import annotations

import base64
import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum

ADB = "adb"
DEFAULT_SERVER = "http://127.0.0.1:4723"

# A fully black PNG at phone resolution compresses to very little. Screenshots of
# real UI do not. Used only as a heuristic, and only to raise UNKNOWN.
SUSPICIOUSLY_SMALL_PNG = 6000


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    # Not used by the probe, whose six checks always run. The login check shares
    # this vocabulary and has steps that legitimately do not apply — a login form
    # that never appeared because the app was already signed in, say. That is a
    # different statement from UNKNOWN, which means the check ran and could not
    # tell.
    SKIP = "SKIP"


@dataclass
class CheckResult:
    number: int
    name: str
    status: Status
    detail: str
    blocking: bool = False


@dataclass
class ProbeReport:
    package: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def blocking_failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.blocking and r.status is Status.FAIL]

    @property
    def viable(self) -> bool:
        """False only on a confirmed blocking failure. UNKNOWN is not a verdict."""
        return not self.blocking_failures

    def render(self) -> str:
        width = max((len(r.name) for r in self.results), default=10)
        lines = [f"REQ-1 probe — {self.package}", ""]
        for r in self.results:
            flag = " (blocking)" if r.blocking else ""
            lines.append(f"  {r.number}. {r.name:<{width}}  {r.status.value:<7}{flag}")
            if r.detail:
                lines.append(f"       {r.detail}")
        lines.append("")
        if self.blocking_failures:
            names = ", ".join(f"#{r.number}" for r in self.blocking_failures)
            lines.append(f"STOP: blocking check(s) failed: {names}")
            lines.append("No stack choice rescues this. See REQ-0 for the vendor-API path.")
        elif any(r.status is Status.UNKNOWN for r in self.results):
            lines.append("No blocking failure, but some checks are UNKNOWN — read the")
            lines.append("details above before treating this as a go.")
        else:
            lines.append("No blocking failures.")
        return "\n".join(lines)


# --------------------------------------------------------------------------- adb


def _adb(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ADB, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def usb_devices() -> list[str]:
    """Serials of authorised devices attached over USB.

    REQ-5.4: entries of the form <ip>:<port> are adb-over-TCP and are rejected
    outright. A network-attached phone could be anywhere, which breaks the chain
    of reasoning the presence gate depends on.
    """
    out = _adb("devices").stdout.splitlines()[1:]
    serials = []
    for line in out:
        parts = line.split()
        if len(parts) != 2 or parts[1] != "device":
            continue
        if re.match(r"^.+:\d+$", parts[0]):
            continue  # TCP transport — not permitted
        serials.append(parts[0])
    return serials


def _app_running(package: str) -> bool:
    return bool(_adb("shell", "pidof", package).stdout.strip())


# ------------------------------------------------------------------------ checks


def _check_device() -> CheckResult | None:
    """Not one of the six; a precondition. Returns a result only on failure."""
    serials = usb_devices()
    if not serials:
        tcp = _adb("devices").stdout
        detail = "no authorised USB device. `adb devices` said:\n       " + tcp.strip()
        if re.search(r"\d+\.\d+\.\d+\.\d+:\d+", tcp):
            detail += "\n       (a TCP entry is present — adb over TCP is rejected, REQ-5.4)"
        return CheckResult(0, "device attached", Status.FAIL, detail, blocking=True)
    if len(serials) > 1:
        return CheckResult(
            0,
            "device attached",
            Status.FAIL,
            f"{len(serials)} devices attached; probe requires exactly one: {serials}",
            blocking=True,
        )
    return None


def run_probe(
    package: str,
    activity: str | None = None,
    server_url: str = DEFAULT_SERVER,
    settle_seconds: int = 8,
) -> ProbeReport:
    report = ProbeReport(package=package)

    precondition = _check_device()
    if precondition:
        report.results.append(precondition)
        return report

    # Imported here so the module can be unit-tested without Appium installed.
    from appium import webdriver
    from appium.options.android import UiAutomator2Options

    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.automation_name = "UiAutomator2"
    options.app_package = package
    if activity:
        options.app_activity = activity
    options.no_reset = True
    options.new_command_timeout = 120

    driver = None

    # --- 1. session creation and survival -----------------------------------
    try:
        driver = webdriver.Remote(server_url, options=options)
        time.sleep(settle_seconds)
        if _app_running(package):
            report.results.append(
                CheckResult(1, "launches under Appium", Status.PASS,
                            f"process alive {settle_seconds}s after session start",
                            blocking=True)
            )
        else:
            # Died shortly after an automation session attached. Strong signal,
            # but a plain crash looks the same — do not overclaim.
            report.results.append(
                CheckResult(1, "launches under Appium", Status.FAIL,
                            f"process gone within {settle_seconds}s of session start; "
                            "check 4 distinguishes detection from an ordinary crash",
                            blocking=True)
            )
    except Exception as exc:  # noqa: BLE001 - the failure text is the finding
        report.results.append(
            CheckResult(1, "launches under Appium", Status.FAIL,
                        f"session creation failed: {type(exc).__name__}: {exc}",
                        blocking=True)
        )
        report.results.append(
            CheckResult(2, "view hierarchy dumpable", Status.UNKNOWN,
                        "not reached — no session", blocking=True)
        )
        _note_client_mismatch(report, exc)
        return report

    try:
        # --- 2. view hierarchy ---------------------------------------------
        try:
            source = driver.page_source or ""
            if len(source) > 200 and "<" in source:
                report.results.append(
                    CheckResult(2, "view hierarchy dumpable", Status.PASS,
                                f"{len(source)} chars of XML", blocking=True)
                )
            else:
                report.results.append(
                    CheckResult(2, "view hierarchy dumpable", Status.FAIL,
                                f"page_source returned {len(source)} chars — not a usable tree",
                                blocking=True)
                )
        except Exception as exc:  # noqa: BLE001
            report.results.append(
                CheckResult(2, "view hierarchy dumpable", Status.FAIL,
                            f"{type(exc).__name__}: {exc}", blocking=True)
            )

        # --- 3. FLAG_SECURE -------------------------------------------------
        report.results.append(_check_flag_secure(driver))

        # --- 4. automation detection ----------------------------------------
        report.results.append(_check_automation_detection(package))

        # --- 5. Play Integrity ----------------------------------------------
        report.results.append(_check_play_integrity(package))

        # --- 6. cold start to login ----------------------------------------
        report.results.append(_check_cold_start(driver, package))

    finally:
        try:
            driver.quit()
        except Exception:  # noqa: BLE001
            pass

    return report


def _note_client_mismatch(report: ProbeReport, exc: Exception) -> None:
    """Session failures from a client/server major mismatch mimic app rejection.

    Calling that out explicitly matters: mistaking it for "the app blocks
    automation" would abandon a viable project.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    hints = ("jsonwp", "protocol", "capabilit", "not implemented", "unsupported", "404")
    if any(h in text for h in hints):
        report.results.append(
            CheckResult(
                0,
                "client/server compatibility",
                Status.UNKNOWN,
                "this failure resembles an Appium-Python-Client / server major "
                "mismatch rather than app rejection. Server is pinned to 3.6.0; "
                "try stepping the client back through the 5.x line and re-run "
                "before concluding anything about the app.",
            )
        )


def _check_flag_secure(driver) -> CheckResult:
    try:
        shot = driver.get_screenshot_as_base64()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            3, "FLAG_SECURE", Status.FAIL,
            f"screenshot refused ({type(exc).__name__}) — FLAG_SECURE is set. "
            "The UI's phone-view panel will show a placeholder (ARCHITECTURE §3.1).",
        )
    size = len(base64.b64decode(shot))
    if size < SUSPICIOUSLY_SMALL_PNG:
        return CheckResult(
            3, "FLAG_SECURE", Status.UNKNOWN,
            f"screenshot succeeded but is only {size} bytes, consistent with an "
            "all-black capture. Open it by eye before trusting the phone-view panel.",
        )
    return CheckResult(3, "FLAG_SECURE", Status.PASS,
                       f"screenshot captured, {size} bytes — not set")


def _check_automation_detection(package: str) -> CheckResult:
    """Distinguish deliberate refusal from an ordinary crash.

    Looks for the app exiting while the UiAutomator service is attached, and for
    crash traces naming automation. Neither is conclusive alone.
    """
    if _app_running(package):
        return CheckResult(
            4, "automation not blocked", Status.PASS,
            "app still running with the UiAutomator service attached", blocking=True,
        )

    logcat = _adb("logcat", "-d", "-t", "300").stdout.lower()
    markers = ("uiautomator", "automation", "instrumentation", "debuggable", "tamper")
    hit = next((m for m in markers if m in logcat and package.lower() in logcat), None)
    if hit:
        return CheckResult(
            4, "automation not blocked", Status.FAIL,
            f"app exited and logcat mentions '{hit}' near {package} — consistent "
            "with deliberate refusal. Read the full logcat before concluding.",
            blocking=True,
        )
    return CheckResult(
        4, "automation not blocked", Status.UNKNOWN,
        "app is not running but logcat shows no automation-related marker. "
        "Could be an ordinary crash. Re-run; if it dies consistently only under "
        "Appium and not when launched by hand, treat that as detection.",
        blocking=True,
    )


def _check_play_integrity(package: str) -> CheckResult:
    if _app_running(package):
        return CheckResult(5, "Play Integrity", Status.PASS,
                           "app running on this device")
    return CheckResult(
        5, "Play Integrity", Status.UNKNOWN,
        "app not running; cannot distinguish an integrity refusal from check 4. "
        "Launch the app by hand — if it runs standalone but not under Appium, "
        "this is check 4, not integrity.",
    )


def _check_cold_start(driver, package: str) -> CheckResult:
    """`pm clear` then relaunch — what every scheduled run actually does (REQ-2)."""
    cleared = _adb("shell", "pm", "clear", package)
    if "Success" not in cleared.stdout:
        return CheckResult(
            6, "cold start reaches login", Status.UNKNOWN,
            f"pm clear did not report success: {cleared.stdout.strip() or cleared.stderr.strip()}",
        )
    _adb("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(6)
    if not _app_running(package):
        return CheckResult(6, "cold start reaches login", Status.FAIL,
                           "app did not come up after pm clear + launch")
    try:
        source = (driver.page_source or "").lower()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(6, "cold start reaches login", Status.UNKNOWN,
                           f"app running but page_source failed: {type(exc).__name__}: {exc}")
    if any(w in source for w in ("password", "sign in", "log in", "login", "username")):
        return CheckResult(6, "cold start reaches login", Status.PASS,
                           "login screen present after cold start")
    return CheckResult(
        6, "cold start reaches login", Status.UNKNOWN,
        "app is up but no login field matched. It may open elsewhere first — "
        "inspect page_source and adjust before REQ-3.",
    )
