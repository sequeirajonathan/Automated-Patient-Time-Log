"""The language-selection gate that precedes login.

`com.hhaexchange.caregiver` opens on `.LanguageSelectionActivity`, a picker and an
Apply control, before it will show a login form. Discovered by inspecting the real
app: a cold start lands here, not on login, so anything that assumes otherwise
waits forever for a password field that is one screen away.

**The wheel is readable after all.** The picker view itself exposes nothing — no
children, no text, no content-desc — which led to an early conclusion that the
selection could not be verified. That was wrong. The app re-renders its own chrome
live as the wheel moves, so `lbl_header` and `txtApply` always describe the
*currently highlighted* language, before it is applied. Reading them turns
selection from a calibrated guess into a closed loop.

The labels below were read off the device rather than guessed. An earlier guess at
the Spanish wording ("seleccionar idioma" / "siguiente") was wrong on both counts,
which would have made expect_language="es" refuse a correctly configured phone.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

XPATH = "xpath"
ID = "id"

PACKAGE = "com.hhaexchange.caregiver"
ACTIVITY = ".LanguageSelectionActivity"


@dataclass
class LanguageSelectors:
    picker: str = f"{PACKAGE}:id/pickerView"
    apply: str = f"{PACKAGE}:id/txtApply"
    header: str = f"{PACKAGE}:id/lbl_header"


# Header text per language, observed on the device by stepping the wheel one item
# at a time. The header is the primary signal because it is unique per language;
# the apply control is not — Kreyol Ayisyen also reads "Next", so it cannot
# distinguish itself from English.
HEADER_TEXT = {
    "en": "select language",
    "es": "seleccione el idioma",
    "fr": "choisir la langue",
    "zh": "選擇語言",
    "ru": "выбрать язык",
    "ht": "chwazi lang",
    "ko": "언어 선택",
}

# Wheel order, observed. Used only to pick a direction to drag; the loop verifies
# every step, so an incomplete or slightly wrong list costs an extra iteration
# rather than a wrong result. The list continues past Korean (Armenian, Bengali,
# and more) — unlisted languages simply cannot be targeted.
LANGUAGE_ORDER = ("en", "es", "fr", "zh", "ru", "ht", "ko")

LANGUAGE_NAMES = {
    "en": "English", "es": "Espanol", "fr": "Francais", "zh": "Chinese",
    "ru": "Russian", "ht": "Kreyol Ayisyen", "ko": "Korean",
}

# One item per drag, measured: a 400px drag moved exactly three items, and the
# label spacing at the selection band is ~131px. Only needs to be accurate enough
# to move one detent — the loop re-reads and corrects, so it is not load-bearing.
ITEM_PITCH_PX = 133


class LanguageMismatch(RuntimeError):
    """The app is not in the language this deployment expects."""


class LanguageScreen:
    def __init__(self, driver, selectors: LanguageSelectors | None = None):
        self.driver = driver
        self.sel = selectors or LanguageSelectors()

    def is_displayed(self) -> bool:
        """Prefer the activity name; fall back to the picker for a renamed activity."""
        try:
            if (self.driver.current_activity or "").endswith(ACTIVITY):
                return True
        except Exception:  # noqa: BLE001 — current_activity is not always available
            pass
        return bool(self.driver.find_elements(ID, self.sel.picker))

    def _text_of(self, selector: str) -> str:
        controls = self.driver.find_elements(ID, selector)
        return (controls[0].text or "").strip().lower() if controls else ""

    def header_language(self) -> str | None:
        """The language currently highlighted on the wheel, or None if unknown.

        Live: the app re-renders this text as the wheel moves, so it reflects the
        highlighted item rather than the applied setting.
        """
        header = self._text_of(self.sel.header)
        if not header:
            return None
        for code, expected in HEADER_TEXT.items():
            if header == expected:
                return code
        log.info("unrecognised language header %r", header)
        return None

    def select_language(self, target: str, *, max_steps: int = 12) -> None:
        """Move the wheel to `target`, verifying after every step.

        A closed loop rather than a calculated drag. Each iteration reads what is
        actually highlighted and moves one detent toward the goal, so an imprecise
        pitch or a missed detent costs an extra iteration instead of silently
        landing on the wrong language — which is how an earlier version left the
        app on Francais.
        """
        if target not in LANGUAGE_ORDER:
            raise ValueError(
                f"cannot target {target!r}; known languages are {LANGUAGE_ORDER}"
            )

        from apt_log.gestures import drag

        pickers = self.driver.find_elements(ID, self.sel.picker)
        if not pickers:
            raise RuntimeError("language picker not on screen")
        rect = pickers[0].rect
        x = rect["x"] + rect["width"] // 2
        centre = rect["y"] + rect["height"] // 2
        half = ITEM_PITCH_PX // 2

        for _ in range(max_steps):
            current = self.header_language()
            if current == target:
                log.info("wheel on %s", LANGUAGE_NAMES[target])
                return
            if current is None:
                raise RuntimeError(
                    "cannot read the highlighted language — refusing to navigate "
                    "the wheel blind"
                )

            delta = LANGUAGE_ORDER.index(target) - LANGUAGE_ORDER.index(current)
            # Dragging up advances down the list, confirmed on the device.
            if delta > 0:
                drag(self.driver, x, centre + half, centre - half)
            else:
                drag(self.driver, x, centre - half, centre + half)
            time.sleep(0.4)

        raise RuntimeError(
            f"wheel did not reach {LANGUAGE_NAMES[target]} in {max_steps} steps; "
            f"last read {self.header_language()!r}"
        )

    def apply(self, expect_language: str | None = None) -> None:
        """Commit the highlighted selection.

        `expect_language` re-reads the live header immediately before tapping, so
        a wrong-language run fails while someone can still fix it rather than
        surfacing in Florida.
        """
        if expect_language is not None:
            actual = self.header_language()
            if actual != expect_language:
                raise LanguageMismatch(
                    f"expected {expect_language!r} but the wheel is on "
                    f"{actual or 'an unrecognised language'} — refusing to apply"
                )

        controls = self.driver.find_elements(ID, self.sel.apply)
        if not controls:
            raise RuntimeError(
                f"language screen present but {self.sel.apply} is missing — "
                "the app's layout has changed"
            )
        controls[0].click()
        log.info("applied the language selection")


# Activities the app passes through on its way somewhere else. Checking for a
# gate while one of these is up finds nothing and wrongly concludes there is
# nothing to clear — observed on real hardware, where the language gate appears
# roughly three seconds after .AppLaunchActivity.
TRANSIENT_ACTIVITIES = (".AppLaunchActivity",)


def _activity(driver) -> str:
    try:
        return driver.current_activity or ""
    except Exception:  # noqa: BLE001
        return ""


def advance_past_startup_gates(
    driver,
    timeout: float = 45.0,
    poll: float = 1.0,
    max_clears: int = 3,
    language: str | None = None,
) -> list[str]:
    """Wait out splash screens and clear pre-login gates until login is reachable.

    Polls rather than checking once. A cold start is not instantaneous, and the
    first thing on screen is a launch activity, not the gate — so a single
    immediate check reliably sees nothing to do and returns while the app is
    still on its way to the screen it was meant to clear.

    When `language` is given, the wheel is moved to it before applying; otherwise
    whatever is highlighted is accepted.

    Stops as soon as a login form appears, or the app settles on something that
    is neither transient nor a known gate (already signed in). Bounded by both a
    deadline and a clear count, so a gate that refuses to advance is reported
    rather than looped on.
    """
    from apt_log.screens.login import LoginScreen

    deadline = time.monotonic() + timeout
    dismissed: list[str] = []
    seen_settled: str | None = None

    while time.monotonic() < deadline:
        if LoginScreen(driver).is_displayed():
            return dismissed

        gate = LanguageScreen(driver)
        if gate.is_displayed():
            if len(dismissed) >= max_clears:
                raise RuntimeError(
                    f"language gate still present after {max_clears} attempts — "
                    "not advancing blind"
                )
            if language:
                gate.select_language(language)
            gate.apply(expect_language=language)
            dismissed.append("language")
            seen_settled = None
            time.sleep(poll)
            continue

        activity = _activity(driver)
        if activity.endswith(TRANSIENT_ACTIVITIES):
            seen_settled = None
            time.sleep(poll)
            continue

        # Anything else *might* mean already signed in — but each check above is a
        # separate round trip, so the app can move between them. Observed: the gate
        # appeared after is_displayed() read splash and before this line read the
        # activity, and the mismatch was misread as "settled". Require the same
        # activity twice before believing it, so a transition in flight is waited
        # out rather than mistaken for a destination.
        if seen_settled == activity:
            return dismissed
        seen_settled = activity
        time.sleep(poll)

    raise TimeoutError(
        f"app did not reach a login screen or a settled state within {timeout}s "
        f"(last activity: {_activity(driver) or 'unknown'})"
    )
