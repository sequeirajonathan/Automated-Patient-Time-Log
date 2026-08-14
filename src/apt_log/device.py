"""REQ-2 — device session management, and the OPERATIONS §1.1 recovery ladder.

Everything here assumes nobody is in the room. Two consequences shape the design:

- **The ladder terminates.** An automation that retries forever is indistinguishable
  from one that is working. Rung 7 alerts and stops, on purpose.
- **Escalation is ordered by cost**, cheapest first, and each rung is only tried
  when the one below it did not help.

Rung 6 reboots the Pi. It is deliberately near the bottom: rebooting the Pi is
cheap, but rebooting the *phone* is not, because Android needs the lock PIN to
decrypt user storage after a cold boot and that has to be scripted (§1.4).
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from apt_log.probe import _adb
from apt_log.secrets import PHONE_PIN, SecretNotFound, SecretProvider
from apt_log.transport import TransportMode, attached_devices

log = logging.getLogger(__name__)

DEFAULT_SERVER = "http://127.0.0.1:4723"

KEYCODE_WAKEUP = "224"
KEYCODE_BACK = "4"
KEYCODE_HOME = "3"
KEYCODE_RECENTS = "187"
KEYCODE_MENU = "82"   # dismisses the keyguard
KEYCODE_ENTER = "66"


# Actions the UI is allowed to send to the phone. An allow-list rather than a
# keyevent parameter, and it stays that way: the moment the page can post an
# arbitrary keycode it is a remote control for the device, and "the UI cannot
# record a visit" stops being a property of the routing and starts being a
# property of whatever someone types into a form field.
#
# KEYCODE_WAKEUP over KEYCODE_POWER because wake is idempotent. Power toggles,
# so a double-tap from an impatient thumb on a slow connection would put the
# screen back to sleep -- and the person pressing it is a thousand miles from
# the phone and pressing it *because* they cannot see what happened.
UI_ACTIONS = {
    "wake": KEYCODE_WAKEUP,
    # Navigation, because without it a screen with no on-screen way out is a
    # dead end. A keypad came up with its nav bar outside the tappable set and
    # there was no way back -- from Texas, with the phone in Florida. These are
    # 92ms each and cannot record anything; being stuck is the worse risk by a
    # wide margin.
    "back": KEYCODE_BACK,
    "home": KEYCODE_HOME,
    "recents": KEYCODE_RECENTS,
    "enter": KEYCODE_ENTER,
}


class DeviceUnavailable(RuntimeError):
    """The ladder ran to the end without recovering the device."""


def send_ui_action(action: str, serial: str | None = None) -> None:
    """Send one of the enumerated UI actions. Raises on anything else."""
    keycode = UI_ACTIONS.get(action)
    if keycode is None:
        raise DeviceUnavailable(
            f"{action!r} is not an action this page may send; "
            f"known: {sorted(UI_ACTIONS)}"
        )
    cmd = ["adb"] + (["-s", serial] if serial else []) + \
          ["shell", "input", "keyevent", keycode]
    result = subprocess.run(cmd, capture_output=True, timeout=20)
    if result.returncode != 0:
        raise DeviceUnavailable(
            f"adb refused {action}: {result.stderr.decode('utf-8', 'replace').strip()}"
        )


class Rung(StrEnum):
    ADB_RECONNECT = "adb reconnect"
    ADB_SERVER_RESTART = "adb server restart"
    USB_POWER_CYCLE = "uhubctl port power-cycle"
    HOST_REBOOT = "reboot the Pi"
    GIVE_UP = "alert and stop"


@dataclass(frozen=True)
class RecoveryOutcome:
    recovered: bool
    rungs_tried: list[Rung]
    serial: str | None = None
    detail: str = ""


def _device_present() -> tuple[bool, str | None, TransportMode]:
    serials, mode = attached_devices()
    return (bool(serials), serials[0] if serials else None, mode)


class DeviceRecovery:
    """The escalation ladder, with each action injectable so it can be tested.

    The real implementations shell out; tests substitute them and assert on the
    order rungs were tried and that the ladder stopped.
    """

    def __init__(
        self,
        *,
        allow_host_reboot: bool = True,
        uhubctl_location: str | None = None,
        alert: Callable[[str], None] | None = None,
        settle_seconds: float = 3.0,
    ):
        self.allow_host_reboot = allow_host_reboot
        self.uhubctl_location = uhubctl_location
        self.alert = alert or (lambda msg: log.error("ALERT: %s", msg))
        self.settle_seconds = settle_seconds

    # ---------------------------------------------------------------- actions
    def _adb_reconnect(self) -> None:
        _adb("reconnect")

    def _adb_server_restart(self) -> None:
        _adb("kill-server")
        _adb("start-server")

    def _usb_power_cycle(self) -> None:
        if not self.uhubctl_location:
            raise RuntimeError("no uhubctl location configured")
        subprocess.run(
            ["uhubctl", "-l", self.uhubctl_location, "-a", "cycle"],
            capture_output=True, text=True, timeout=60, check=False,
        )

    def _reboot_host(self) -> None:
        subprocess.run(["systemctl", "reboot"], capture_output=True, check=False)

    # ------------------------------------------------------------------ ladder
    def recover(self) -> RecoveryOutcome:
        tried: list[Rung] = []

        present, serial, mode = _device_present()
        if present:
            return RecoveryOutcome(True, tried, serial, "device was already present")

        for rung, action in (
            (Rung.ADB_RECONNECT, self._adb_reconnect),
            (Rung.ADB_SERVER_RESTART, self._adb_server_restart),
            (Rung.USB_POWER_CYCLE, self._usb_power_cycle),
            (Rung.HOST_REBOOT, self._reboot_host),
        ):
            if rung is Rung.USB_POWER_CYCLE and mode is TransportMode.DEV:
                # A TCP-attached phone is not on a hub port; cutting power to one
                # would do nothing. Skipping is honest, not a shortcut.
                log.info("skipping %s: transport is dev, phone is not on a hub port", rung)
                continue
            if rung is Rung.HOST_REBOOT and not self.allow_host_reboot:
                log.warning("would reboot the host, but that is disabled")
                continue

            tried.append(rung)
            log.warning("device missing — escalating to: %s", rung)
            try:
                action()
            except Exception as exc:  # noqa: BLE001 — a failed rung is not fatal
                log.warning("%s failed: %s", rung, exc)
                continue

            if rung is Rung.HOST_REBOOT:
                # Nothing after this executes; the machine is going down.
                return RecoveryOutcome(False, tried, None, "host reboot issued")

            time.sleep(self.settle_seconds)
            present, serial, _ = _device_present()
            if present:
                return RecoveryOutcome(True, tried, serial, f"recovered after {rung}")

        tried.append(Rung.GIVE_UP)
        detail = "device did not return after the full recovery ladder"
        self.alert(detail)
        return RecoveryOutcome(False, tried, None, detail)


class DeviceSession:
    """An Appium session against the attached phone, plus the REQ-2 lifecycle.

    Cold-starts the app on every run. A live session is never assumed: the agent
    fires hours apart, and whatever the phone was doing in between is not this
    run's starting state.
    """

    def __init__(
        self,
        package: str,
        secrets: SecretProvider,
        *,
        activity: str | None = None,
        server_url: str = DEFAULT_SERVER,
        recovery: DeviceRecovery | None = None,
    ):
        self.package = package
        self.secrets = secrets
        self.activity = activity
        self.server_url = server_url
        self.recovery = recovery or DeviceRecovery()
        self.driver = None
        self.serial: str | None = None
        self.transport_mode: TransportMode | None = None

    # ----------------------------------------------------------- REQ-2 pieces
    def ensure_device(self) -> str:
        """Confirm a usable device, running the recovery ladder if not."""
        present, serial, mode = _device_present()
        self.transport_mode = mode
        if present:
            self.serial = serial
            return serial  # type: ignore[return-value]

        outcome = self.recovery.recover()
        self.transport_mode = _device_present()[2]
        if not outcome.recovered:
            raise DeviceUnavailable(
                f"{outcome.detail}; rungs tried: "
                f"{', '.join(r.value for r in outcome.rungs_tried)}"
            )
        self.serial = outcome.serial
        return outcome.serial  # type: ignore[return-value]

    def wake_and_unlock(self) -> None:
        """Wake, dismiss the keyguard, and enter the PIN if one is configured.

        Must be a numeric PIN — `input text` cannot draw a pattern or present a
        fingerprint (OPERATIONS §1.4).
        """
        _adb("shell", "input", "keyevent", KEYCODE_WAKEUP)
        _adb("shell", "input", "keyevent", KEYCODE_MENU)
        try:
            pin = self.secrets.get(PHONE_PIN)
        except SecretNotFound:
            log.info("no %s configured; assuming a swipe-only lock", PHONE_PIN)
            return
        # Honest limitation: `input text` takes the PIN as an argv element, so it
        # is briefly visible in `ps` on this host and in the phone's own process
        # list. There is no stdin-fed equivalent. The exposure is bounded — the
        # only other account on the Pi is root, which can read the secrets file
        # anyway — but it is real, and this is not the same protection as the app
        # password, which travels over the Appium session and never hits argv.
        _adb("shell", "input", "text", pin)
        _adb("shell", "input", "keyevent", KEYCODE_ENTER)

    def cold_start(self) -> None:
        """Force-stop then launch. Every scheduled run starts from a known state."""
        _adb("shell", "am", "force-stop", self.package)
        time.sleep(1)
        _adb(
            "shell", "monkey", "-p", self.package,
            "-c", "android.intent.category.LAUNCHER", "1",
        )

    def is_alive(self) -> bool:
        """Watchdog: has the device gone away mid-run?"""
        present, serial, _ = _device_present()
        return present and serial == self.serial

    # ------------------------------------------------------------- lifecycle
    def open(self):
        from appium import webdriver
        from appium.options.android import UiAutomator2Options

        self.ensure_device()
        self.wake_and_unlock()

        options = UiAutomator2Options()
        options.platform_name = "Android"
        options.automation_name = "UiAutomator2"
        options.app_package = self.package
        if self.activity:
            options.app_activity = self.activity
        options.no_reset = True
        options.new_command_timeout = 120
        if self.serial:
            options.udid = self.serial

        self.driver = webdriver.Remote(self.server_url, options=options)
        self.ensure_authenticated()
        return self.driver

    def ensure_authenticated(self) -> bool:
        """REQ-3: check for the login screen on every run, re-auth if present.

        Called from open() rather than left to the caller. A session that expired
        overnight is the normal case here, not the exception, and a check that
        depends on someone remembering to make it is a check that eventually
        stops happening.
        """
        from apt_log.screens.login import authenticate_if_needed

        if self.driver is None:
            raise RuntimeError("no active session")
        return authenticate_if_needed(self.driver, self.secrets)

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception as exc:  # noqa: BLE001
                log.warning("driver.quit() failed: %s", exc)
            finally:
                self.driver = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()
