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
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

log = logging.getLogger(__name__)

DEFAULT_SERVER = "http://127.0.0.1:4723"

# Long enough that the session survives quiet stretches between her taps. The
# portal is used in bursts: nothing for twenty minutes, then a flurry.
COMMAND_TIMEOUT = 3600

# Creating a session is flaky in a specific way: the first attempt after the
# device has been left alone tends to fail with "the instrumentation process
# cannot be initialized", and the next one succeeds. Measured: 50s of failed
# attempts, then 11s to a working session, and 664-796ms per read thereafter.
#
# So a failure must not be retried immediately. Without this cooldown the feed
# spent every cycle in a doomed 25-second connect, which is worse than having no
# hierarchy at all -- it had no hierarchy AND no time left for anything else.
RETRY_AFTER = 30.0

# Creating a session can also hang outright rather than failing -- observed on
# the Pi sitting past 90 seconds with no error and no session, which leaves the
# overlay dead forever rather than merely slow. Nothing in the Appium client
# bounds this, so it is bounded here.
CONNECT_BUDGET = 40.0


class Resident:
    """One Appium session, created on demand and rebuilt when it dies.

    Thread-safe because the feed loop reads through it while a tap may be
    arriving on another thread.
    """

    def __init__(self, server: str = DEFAULT_SERVER):
        self._server = server
        self._driver = None
        self._lock = threading.Lock()
        self._blocked_until = 0.0

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
        with ThreadPoolExecutor(max_workers=1) as pool:
            # The worker is abandoned on timeout rather than cancelled -- there
            # is no way to interrupt a blocked HTTP call. It is a daemon thread
            # in a daemon thread; it will die with the process. Leaking one of
            # those beats an overlay that never returns.
            future = pool.submit(webdriver.Remote, self._server, options=options)
            try:
                return future.result(timeout=CONNECT_BUDGET)
            except FuturesTimeout:
                pool.shutdown(wait=False, cancel_futures=True)
                raise TimeoutError(
                    f"Appium did not open a session within {CONNECT_BUDGET:.0f}s"
                ) from None

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
                    if time.monotonic() < self._blocked_until:
                        return None
                    try:
                        self._driver = self._create()
                    except Exception as exc:  # noqa: BLE001
                        self._blocked_until = time.monotonic() + RETRY_AFTER
                        log.warning("cannot open an Appium session (%s); "
                                    "not retrying for %.0fs", exc, RETRY_AFTER)
                        return None
                try:
                    source = self._driver.page_source
                    self._blocked_until = 0.0
                    return source
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
