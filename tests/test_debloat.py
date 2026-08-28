"""The debloat script's protect list, checked against what the code needs.

The previous phone was stripped by hand and nothing was written down, so when
it was replaced the exercise started again from zero. `scripts/debloat.sh` is
the answer to that — but a list of package names in a shell script rots the
moment the code around it moves, and the failure mode is not a broken script.
It is HHAeXchange+ silently unable to sign in, three weeks later, on a phone
nobody remembers stripping.

So the constants are the source of truth and this file holds them to it: every
package the controller reaches for by name must appear in PROTECT, and nothing
in PROTECT may also be matched as bloat.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

from apt_log import autoentry, feed, macros, sign, sms

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "debloat.sh"


def _array(name: str) -> list[str]:
    """The entries of a bash array literal in the script."""
    body = SCRIPT.read_text(encoding="utf-8")
    m = re.search(rf"^{name}=\((.*?)^\)", body, re.S | re.M)
    assert m, f"{name} is not an array in debloat.sh"
    out = []
    for raw in m.group(1).splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.extend(line.split())
    return out


@pytest.fixture(scope="module")
def protect():
    return _array("PROTECT")


@pytest.fixture(scope="module")
def bloat():
    return _array("BLOAT")


class TestEverythingTheControllerNeedsIsProtected:
    def test_every_care_app(self, protect):
        for pkg in feed.CARE_APPS:
            assert pkg in protect, f"{pkg} is a care app and must not be removed"

    def test_every_app_that_can_be_armed(self, protect):
        for pkg in autoentry.SUPPORTED:
            assert pkg in protect

    def test_every_app_that_can_be_signed_on(self, protect):
        for pkg in sign.APP_PACKAGES:
            assert pkg in protect

    def test_the_browser_that_signs_in_for_hhaexchange(self, protect):
        """THE LEAST OBVIOUS ENTRY AND THE MOST EXPENSIVE TO GET WRONG.

        HHAeXchange+ authenticates through a Chrome Custom Tab. Strip Chrome
        as 'a browser nobody uses' and that app can never sign in again — and
        the symptom appears days later, on a phone whose debloat nobody
        remembers.
        """
        for host in macros.WEB_FLOW_HOST.values():
            assert host in protect, (
                f"{host} carries a sign-in web flow and must be protected")

    def test_the_package_the_code_is_texted_through(self, protect):
        assert sms.SEND_PKG in protect


class TestTheTwoListsCannotDisagree:
    def test_nothing_protected_is_also_matched_as_bloat(self, protect, bloat):
        """A package in both lists is a bug wearing a safety net: `apply`
        would refuse it, but `plan` would advertise a removal that never
        happens, and the next person to simplify the script removes the net."""
        for keep in protect:
            for pattern in bloat:
                assert not keep.startswith(pattern), (
                    f"{keep} is protected but matches bloat pattern {pattern}")

    def test_no_bloat_pattern_is_dangerously_broad(self, bloat):
        """`com.` or `com.samsung.` would take the phone with it."""
        for pattern in bloat:
            assert pattern.count(".") >= 1, f"{pattern} is too broad"
            assert len(pattern) >= 8, f"{pattern} is too broad"
            assert pattern not in ("com.android.", "com.google.",
                                   "com.samsung.", "com.sec."), pattern


class TestTheScriptItself:
    def test_removal_is_the_reversible_form(self):
        """`-k --user 0` leaves the APK on the system partition, so `restore`
        and a factory reset can both put it back. A bare `pm uninstall` on a
        system app cannot be undone without a reflash."""
        body = SCRIPT.read_text(encoding="utf-8")
        assert "pm uninstall -k --user 0" in body
        assert "cmd package install-existing" in body

    def test_it_refuses_to_guess_between_two_phones(self):
        """Two devices attached and no serial is how the wrong phone gets
        stripped."""
        body = SCRIPT.read_text(encoding="utf-8")
        assert "ANDROID_SERIAL" in body

    def test_it_records_what_it_removed(self):
        body = SCRIPT.read_text(encoding="utf-8")
        assert "RECORD" in body and "restore" in body

    def test_every_adb_call_in_a_read_loop_closes_stdin(self):
        """ADB EATS THE LOOP IT IS RUNNING INSIDE.

        `adb` reads stdin, so inside `while read -r p` it consumes the rest of
        the package list. The first live `apply` removed exactly one package
        of thirty-five and reported success — it fails in the direction of
        doing too little, which is the only reason it was survivable.

        Both loops that drive adb per package must close its stdin.
        """
        body = SCRIPT.read_text(encoding="utf-8")
        for call in ("pm uninstall -k --user 0", "cmd package install-existing"):
            line = next(ln for ln in body.splitlines() if call in ln
                        and ln.strip().startswith("out="))
            assert "</dev/null" in line, (
                f"{call!r} runs in a read loop and must close stdin")

    def test_it_is_executable(self):
        assert stat.S_IMODE(SCRIPT.stat().st_mode) & stat.S_IXUSR, (
            "chmod +x scripts/debloat.sh")
