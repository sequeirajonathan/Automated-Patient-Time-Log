"""Controlled touch gestures.

`driver.swipe()` is a fling: it releases while still moving, so the target keeps
travelling under its own momentum. Distance requested is not distance travelled —
a 240px swipe on the language picker moved two items, not one.

These build W3C pointer sequences instead, stepping to the destination and
**pausing before release** so velocity is zero at pointer-up. That makes the move
deterministic, which is what lets a caller reason about how far something went.

The same machinery is what REQ-10 needs to replay a signature, where preserving
the shape of the motion is the whole point.
"""

from __future__ import annotations

import logging

from selenium.webdriver.common.actions import interaction
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput

log = logging.getLogger(__name__)

# Long enough for the widget to treat the gesture as a drag that ended at rest
# rather than a fling. Empirical, not magic — tune against the device if a
# picker still coasts.
SETTLE_SECONDS = 0.35


def _builder(driver) -> ActionBuilder:
    finger = PointerInput(interaction.POINTER_TOUCH, "finger")
    return ActionBuilder(driver, mouse=finger)


def drag(
    driver,
    x: int,
    y_from: int,
    y_to: int,
    *,
    steps: int = 12,
    step_pause: float = 0.02,
    settle: float = SETTLE_SECONDS,
) -> None:
    """A vertical drag that ends stationary.

    Stepping rather than jumping keeps the widget's own scroll tracking in sync;
    a single large move can be interpreted as a fling regardless of timing.
    """
    actions = _builder(driver)
    p = actions.pointer_action
    p.move_to_location(x, y_from)
    p.pointer_down()
    p.pause(step_pause)

    span = y_to - y_from
    for i in range(1, steps + 1):
        p.move_to_location(x, y_from + round(span * i / steps))
        p.pause(step_pause)

    # Velocity must be zero before release, or the widget coasts.
    p.pause(settle)
    p.pointer_up()
    actions.perform()
    log.debug("drag x=%d %d->%d (%+d px)", x, y_from, y_to, span)


def stroke(driver, points: list[tuple[int, int, float]]) -> None:
    """Replay an absolute path with its original timing (REQ-10.8).

    `points` is [(x, y, dt_seconds), ...] where dt is the delay *before* moving
    to that point. Timing is preserved because some widgets apply velocity-based
    smoothing, and a uniform-speed replay of a signature looks visibly wrong.
    """
    if not points:
        return
    actions = _builder(driver)
    p = actions.pointer_action

    x0, y0, _ = points[0]
    p.move_to_location(x0, y0)
    p.pointer_down()
    for x, y, dt in points[1:]:
        if dt > 0:
            p.pause(dt)
        p.move_to_location(x, y)
    p.pointer_up()
    actions.perform()
