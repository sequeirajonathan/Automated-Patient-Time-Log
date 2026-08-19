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

from apt_log import prefs


@pytest.fixture(autouse=True)
def _isolated_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "PREFS_PATH", tmp_path / "prefs.json")
    yield


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
