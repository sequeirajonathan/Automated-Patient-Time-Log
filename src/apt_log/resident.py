"""A long-lived Appium session, kept open because opening one costs 11 seconds.

`adb shell uiautomator dump` spawns a fresh instrumentation on every single call:
start the framework, wait for the window to be idle, dump, tear down. Measured on
the Pi that is **6.1 seconds**, and because it sometimes captures one window
layer instead of the composed screen it needed retrying, which took a screen read
to **12.7 seconds**. That is the number that made the portal feel broken — the
picture could not update faster than the hierarchy it was waiting on.

Appium's UiAutomator2 server stays resident, so the same read is **1.75 seconds**
and does not come back partial. The session costs 11.4 seconds to open, once.

Nothing competes for it any more. The autonomous agent that would have wanted
this session is not being built, so the portal can simply hold one.

Screenshots deliberately stay on `adb exec-out screencap` (865 ms versus 1070 ms
through Appium) and taps stay on `adb shell input tap` (92 ms versus 116 ms).
This session exists for the one operation where Appium is decisively better.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

DEFAULT_SERVER = "http://127.0.0.1:4723"

# Long enough that the session survives quiet stretches between her taps. The
# portal is used in bursts: nothing for twenty minutes, then a flurry.
COMMAND_TIMEOUT = 3600


class Resident:
    """One Appium session, created on demand and rebuilt when it dies.

    Thread-safe because the feed loop reads through it while a tap may be
    arriving on another thread.
    """

    def __init__(self, server: str = DEFAULT_SERVER):
        self._server = server
        self._driver = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle
    def _create(self):
        from appium import webdriver
        from appium.options.android import UiAutomator2Options

        options = UiAutomator2Options()
        options.platform_name = "Android"
        options.automation_name = "UiAutomator2"
        options.no_reset = True
        options.new_command_timeout = COMMAND_TIMEOUT
        options.set_capability("appium:skipDeviceInitialization", True)
        # The portal reads screens that are animating or have a live timer on
        # them. Waiting for idle on those blocks until the wait times out, which
        # is exactly the stall this class exists to remove.
        options.set_capability("appium:waitForIdleTimeout", 0)
        # No appPackage: the session attaches to whatever is in the foreground,
        # which is the point. She drives; this watches.
        log.info("opening a resident Appium session (about 11s)")
        return webdriver.Remote(self._server, options=options)

    def _discard(self) -> None:
        driver, self._driver = self._driver, None
        if driver is None:
            return
        try:
            driver.quit()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        with self._lock:
            self._discard()

    # ----------------------------------------------------------------- source
    def page_source(self) -> str | None:
        """The current hierarchy, or None if it cannot be read.

        Retries once through a fresh session. A session dies for ordinary
        reasons — the phone rebooted, adb dropped, the server restarted — and on
        an unattended box the difference between "recovers by itself" and "needs
        a person" is this one retry.
        """
        for attempt in (1, 2):
            with self._lock:
                if self._driver is None:
                    try:
                        self._driver = self._create()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("cannot open an Appium session: %s", exc)
                        return None
                try:
                    return self._driver.page_source
                except Exception as exc:  # noqa: BLE001
                    log.info("resident session failed (%s); attempt %d", exc, attempt)
                    self._discard()
        return None

    def alive(self) -> bool:
        with self._lock:
            return self._driver is not None


# Module-level because the session is a device-wide resource: two of them would
# fight over the same UiAutomator2 server.
_resident = Resident()


def page_source() -> str | None:
    return _resident.page_source()


def close() -> None:
    _resident.close()
