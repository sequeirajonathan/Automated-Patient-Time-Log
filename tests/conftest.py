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
