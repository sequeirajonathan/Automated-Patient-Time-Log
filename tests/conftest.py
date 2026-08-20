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

import importlib

import pytest

from apt_log import flight, macros, prefs, schedule, sms, versions
from apt_log.ui import mirror, state


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
    # The pause flag, for the third time and found a third way: CI on a clean
    # machine, where /var/lib/aptlog does not exist and cannot be created, so
    # the test that presses Resume died with PermissionError. On the Pi and on
    # a developer's box it had been quietly touching the real flag — which is
    # the machine's actual "is the scheduler paused" switch — and passing.
    #
    # A test that only passes because the machine it runs on happens to let it
    # write there is not testing what it claims to test. That is the whole
    # argument at the top of this file, and it keeps needing to be made about
    # one more path.
    monkeypatch.setattr(state, "PAUSED_FLAG", tmp_path / "paused")

    # ...and the rest of them, found by deleting /var/lib/aptlog and running
    # the suite to see what grew back. Three did:
    #
    #   flight.jsonl     the flight recorder — the suite has been appending
    #                    test fixtures to the real recording, on the Pi, on
    #                    every deploy, mixed in with actual visits.
    #   viewers.json     the live viewer count. `someone_is_watching` reads
    #                    it to decide whether auto sign-in may run unattended,
    #                    so a test writing it is a test reaching into a
    #                    safety interlock on the running machine.
    #   hierarchy-poke   the feed's "read the tree now" nudge.
    #
    # None of them FAILED anywhere — they are written inside try/except, so
    # on CI they silently did nothing and on the Pi they silently worked.
    # That is the worst shape a bug can have, and the only reason any of this
    # came to light is that PAUSED_FLAG happened to be written somewhere that
    # raises.
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(flight, "FLIGHT_PATH", tmp_path / "flight.jsonl")
    monkeypatch.setattr(macros, "VIEWERS_PATH", tmp_path / "viewers.json")
    # The web process keeps its OWN handle on the same file, resolved at
    # import, so redirecting STATE_DIR above does not move it — which is why
    # this one survived the first attempt at the list. Reached through
    # sys.modules because `from apt_log.ui import app` is the FastAPI
    # instance, not the module that holds the constant.
    monkeypatch.setattr(importlib.import_module("apt_log.ui.app"),
                        "VIEWERS_PATH", tmp_path / "viewers.json")
    # And the mirror, found by the same method a second time: delete
    # /var/lib/aptlog, run the suite, see what grows back. `write_frame`
    # publishes it, so any test that writes a frame wrote the machine's live
    # mirror — the document the portal renders from.
    monkeypatch.setattr(mirror, "MIRROR_PATH", tmp_path / "mirror.json")
    # The auto-auth cooldown and the "we already said so" mark. Both are new
    # and both would have leaked the same way — and the second one bit
    # immediately: one test marking the notice as sent silenced the next,
    # which is the same shape as a real machine going quiet for the wrong
    # reason.
    monkeypatch.setattr(macros, "AUTH_SEEN_PATH", tmp_path / "auto-auth.json")
    monkeypatch.setattr(macros, "TOLD_PATH", tmp_path / "code-notice.json")

    # AND NOTHING MAY READ THE PHONE'S MESSAGES.
    #
    # This one did not leak state — it reached across the tailnet and read a
    # caregiver's SMS inbox, from the deploy gate, on the live machine. The
    # sign-in walk now looks for a texted code, so every test that exercises
    # the walk called `content query --uri content://sms/inbox` for real.
    #
    # It hung the gate for twenty minutes: each poll spawns an adb subprocess
    # with a twenty-second timeout, the walk waits over a minute for a code,
    # and a phone that is busy answers slowly. Two managers ended up queued
    # behind a lock, and the deploy that looked like a slow gate was a test
    # suite interrogating a phone.
    #
    # Stubbed at `_adb` rather than at `subprocess.run`, because `subprocess`
    # is shared with every other module and patching it globally would
    # silence things that legitimately shell out. Tests that mean to exercise
    # `_adb` itself hold a reference taken at import, before this runs.
    monkeypatch.setattr(sms, "_adb", lambda *a, **k: "")
    # ...and the forwarder's throttle, which is module state: one test that
    # forwards a code silences the next one for twenty seconds, and which
    # tests those are depends on the order they run in.
    monkeypatch.setattr(sms, "_last_poll", [0.0])

    # AND NOTHING MAY READ THE REAL SCHEDULE.
    #
    # Not state that could leak — the opposite direction, and the same shape
    # as the SMS stub above: /etc/aptlog/schedule.json holds who is cared for,
    # at whose home, at what hour, and the deploy gate runs this suite on the
    # machine that has it. A test calling `load()` with no argument would read
    # six people's health information into a test process, on the Pi, and
    # pass either way — which is how a leak gets to be invisible.
    #
    # Every test here passes its own path. This is the belt for the next one
    # that forgets.
    monkeypatch.setattr(schedule, "SCHEDULE_PATH",
                        tmp_path / "schedule.json")
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
