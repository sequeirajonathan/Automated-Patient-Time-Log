"""Test-wide safety: nothing here may touch the machine's real state.

The deploy gate runs this suite ON THE PI, against /var/lib/aptlog — the
directory holding the live preferences of the two people using the portal. A
test that renders a page records a device visit, and a test that switches
language writes a language: run without this, a deploy would quietly edit her
settings as a side effect of checking that it is safe to deploy.

So the preferences file is redirected to a temporary path for every test,
automatically, whether or not the test knows preferences exist.
"""

from __future__ import annotations

import pytest

from apt_log import prefs, versions


@pytest.fixture(autouse=True)
def _isolated_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "PREFS_PATH", tmp_path / "prefs.json")
    # The app-version record, for the same reason and caught the same way:
    # `feed.run` checks versions on every tick, so a test exercising the loop
    # on the Pi wrote the machine's real file during the deploy gate. Nothing
    # was lost — it wrote the truth — but it wrote it BEFORE the service ever
    # ran, which is how a baseline nobody meant to take gets taken.
    monkeypatch.setattr(versions, "VERSIONS_PATH",
                        tmp_path / "app-versions.json")
    # The timer is module state, so one test's reading silences the next.
    monkeypatch.setattr(versions, "_last_check", [0.0])
    yield


# --------------------------------------------------------------- real seconds
# THE SUITE MUST NOT SPEND REAL TIME WAITING FOR A MOCK.
#
# Measured on the Pi, where it matters, because the deploy gate runs this
# suite before every release: 1,575 tests in 67 seconds, of which roughly
# FORTY were spent asleep. Twenty tests accounted for it; the other fifteen
# hundred cost about nine seconds between them. "The suite is too big" was the
# obvious reading and the wrong one — it was never the count.
#
# Where each second went, and why the fix differs per case:
#
#   * `macros.wait_for` — 25s, in four tests. Fixed at those four call sites
#     rather than here, with `fast_clock` below. See its docstring: patching
#     `time.sleep` alone, which is what those four did, makes this WORSE than
#     leaving it alone.
#
#   * `sign` — 6s of stroke pacing. Silenced by standing its sleep down rather
#     than by shrinking STROKE_GAP, because that constant carries a REAL
#     property — a pad that receives strokes too fast draws one long line —
#     and test_sign asserts on its value. A fixture that quietly rewrote it
#     would turn that assertion into theatre.
#
#   * `agency` and `feed.type_into` — 11s of settle pauses, now named
#     constants, stood down here. No test in either file is testing the pause.
#
# What is deliberately NOT stood down: the image work in TestCompression. That
# is genuine computation on a real JPEG and it is measuring a real claim about
# size, so it costs what it costs.
@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch):
    from apt_log import feed, sign
    from apt_log.screens import agency

    monkeypatch.setattr(agency, "VERIFY_SETTLE", 0.0)
    monkeypatch.setattr(feed, "TYPE_SETTLE", 0.0)
    monkeypatch.setattr(sign.time, "sleep", lambda _s: None)
    yield


def fast_clock(step: float = 0.5):
    """Patch `macros.wait_for`'s clock so a wait costs no real seconds.

    The idiom this project already had, in TestMigrationPitch, made general.
    It is the RIGHT half of a pair whose other half is a trap: patching
    `time.sleep` alone leaves the deadline on real time, so a predicate that
    never comes true turns a polite poll into a hot spin that burns the whole
    timeout anyway. Four tests did exactly that and cost the deploy gate
    twenty-five seconds a run.

    Moving the clock instead keeps the loop honest — it still polls, still
    gives up when its deadline passes, still runs the same body — and the
    seconds are simply fictional. Use it WITH the sleep patch, never instead
    of it: the sleep is what would otherwise pace the fake clock in real time.
    """
    import itertools
    from unittest.mock import patch

    return patch("apt_log.macros.time.monotonic",
                 side_effect=itertools.count(step=step))


def strip_js_comments(source: str) -> str:
    """Code without its prose.

    A guard that cannot tell the two apart forbids describing the bug it
    exists to prevent — this project has now been bitten by that three times
    (form.action, the type bar's constant, and the word "caches" in the very
    comment explaining that nothing is cached).

    It lives here rather than in one test module because two of them now
    check JavaScript for things that must and must not be in it.
    """
    import re

    return re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
