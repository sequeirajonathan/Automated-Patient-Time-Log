"""Which build of each app is on the phone.

Every reflow rule and every macro in this project is an assumption about an
app version that Google Play can replace overnight. These tests are about
the one question that has to be answerable afterwards: did the app change?
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

from apt_log import versions


# The real thing, off the phone, trimmed of nothing that matters. Note the
# Play Store: an updated system app dumps TWICE — the installed build first,
# the factory one still on /system after it.
DUMP = """pkg=com.tellus.evv.v2
    versionCode=979 minSdk=28 targetSdk=35
    versionName=26.15.1
pkg=com.hhaexchange.uma
    versionCode=251368 minSdk=32 targetSdk=37
    versionName=26.07.01
pkg=com.inmyteam.inmyteam
    versionCode=148 minSdk=24 targetSdk=36
    versionName=4.3.5
pkg=com.android.vending
    versionCode=85273430 minSdk=32 targetSdk=37
    versionName=52.7.34-31 [0] [PR] 964418415
    versionCode=83883120 minSdk=29 targetSdk=34
    versionName=38.8.31-29 [0] [PR] 595838346
"""


class TestReadingWhatIsInstalled:
    def test_every_watched_app_is_found(self):
        found = versions.parse(DUMP)
        assert found["com.tellus.evv.v2"] == {"name": "26.15.1", "code": "979"}
        assert found["com.hhaexchange.uma"]["name"] == "26.07.01"
        assert found["com.inmyteam.inmyteam"]["code"] == "148"

    def test_an_updated_system_app_reports_the_build_that_is_running(self):
        """The Play Store dumps its factory version underneath its installed
        one. Taking the last would have published a build from 2023 and made
        every "did Play change?" answer wrong in the same direction."""
        found = versions.parse(DUMP)
        assert found[versions.PLAY_STORE]["name"] == "52.7.34-31"
        assert found[versions.PLAY_STORE]["code"] == "85273430"

    def test_an_app_that_is_not_installed_is_not_a_version(self):
        text = "pkg=com.gone\npkg=com.tellus.evv.v2\n    versionName=26.15.1\n"
        found = versions.parse(text)
        assert "com.gone" not in found
        assert found["com.tellus.evv.v2"]["name"] == "26.15.1"

    def test_nothing_at_all_parses_to_nothing(self):
        assert versions.parse("") == {}

    def test_the_retired_app_is_not_watched(self):
        """It still gets read when it is in front, but the phone never goes
        there, so an update to it changes nothing — and a line nobody needs
        is a line that teaches people to skim the ones they do."""
        assert "com.hhaexchange.caregiver" not in versions.watched()
        assert "com.tellus.evv.v2" in versions.watched()
        assert versions.PLAY_STORE in versions.watched()


class TestWhatCountsAsAChange:
    NOW = {"com.x": {"name": "2.0", "code": "20"}}

    def test_a_new_version_is_a_change(self):
        was = {"com.x": {"name": "1.9", "code": "19"}}
        moved = versions.changes(was, self.NOW)
        assert len(moved) == 1
        assert moved[0]["from"] == "1.9" and moved[0]["to"] == "2.0"

    def test_the_same_version_is_not(self):
        assert versions.changes(self.NOW, self.NOW) == []

    def test_a_rebuild_at_the_same_name_still_counts(self):
        """Vendors ship the same versionName twice. The code is what moved,
        and it is the code the app's own layout was built from."""
        was = {"com.x": {"name": "2.0", "code": "19"}}
        assert len(versions.changes(was, self.NOW)) == 1

    def test_an_app_seen_for_the_first_time_is_a_change(self):
        moved = versions.changes({}, self.NOW)
        assert moved[0]["from"] == ""

    def test_an_app_that_stopped_answering_is_not(self):
        """A sleeping phone, a half-attached cable and an app mid-install all
        read as "gone" for a moment. None of them is news."""
        assert versions.changes(self.NOW, {}) == []


class TestTheRecordOnDisk:
    def _run(self, tmp_path, dump, force=True):
        path = tmp_path / "app-versions.json"
        with patch.object(versions, "_shell", return_value=dump):
            moved = versions.check(force=force, path=path)
        return moved, path

    def test_the_first_reading_is_written_without_crying_update(self, tmp_path):
        """Everything is new the first time. Announcing four updates on the
        day this shipped would be four false alarms, which is how a warning
        stops being read."""
        moved, path = self._run(tmp_path, DUMP)
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["apps"]["com.tellus.evv.v2"]["name"] == "26.15.1"
        assert "changed" not in stored
        assert moved == []

    def test_a_second_identical_reading_changes_nothing(self, tmp_path):
        _, path = self._run(tmp_path, DUMP)
        with patch.object(versions, "_shell", return_value=DUMP):
            assert versions.check(force=True, path=path) == []

    def test_an_update_is_recorded_and_logged(self, tmp_path, caplog):
        _, path = self._run(tmp_path, DUMP)
        newer = DUMP.replace("versionName=26.15.1", "versionName=26.16.0")
        with caplog.at_level("WARNING"), \
                patch.object(versions, "_shell", return_value=newer):
            moved = versions.check(force=True, path=path)
        assert [c["package"] for c in moved] == ["com.tellus.evv.v2"]
        assert moved[0]["from"] == "26.15.1" and moved[0]["to"] == "26.16.0"
        assert "26.16.0" in caplog.text
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["changed"][0]["to"] == "26.16.0"
        assert stored["changed_at"]

    def test_the_last_change_survives_a_later_quiet_reading(self, tmp_path):
        """The panel is meant to still say "this one moved" on Monday when
        something that worked on Friday does not."""
        _, path = self._run(tmp_path, DUMP)
        newer = DUMP.replace("versionName=4.3.5", "versionName=4.4.0")
        with patch.object(versions, "_shell", return_value=newer):
            versions.check(force=True, path=path)
        with patch.object(versions, "_shell", return_value=newer):
            versions.check(force=True, path=path)
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["changed"][0]["package"] == "com.inmyteam.inmyteam"

    def test_a_phone_that_said_nothing_does_not_erase_the_record(self, tmp_path):
        """Otherwise unplugging the cable wipes the file, and plugging it back
        in reports every app as brand new."""
        _, path = self._run(tmp_path, DUMP)
        before = path.read_text(encoding="utf-8")
        with patch.object(versions, "_shell", return_value=""):
            assert versions.check(force=True, path=path) == []
        assert path.read_text(encoding="utf-8") == before

    def test_an_unreadable_file_is_not_fatal(self, tmp_path):
        path = tmp_path / "app-versions.json"
        path.write_text("{ not json", encoding="utf-8")
        assert versions.stored(path) == {}


class TestTheTimerIsInside:
    def test_a_second_call_straight_away_does_not_touch_the_phone(
            self, tmp_path):
        """`check` is called from the feed loop, which runs every few seconds.
        Holding the timer here is what makes that safe — a caller cannot get
        it wrong because a caller does not have one."""
        path = tmp_path / "app-versions.json"
        with patch.object(versions, "_shell", return_value=DUMP) as shell:
            versions.check(force=True, path=path)
            versions.check(path=path)
            versions.check(path=path)
        assert shell.call_count == 1

    def test_and_it_asks_again_once_the_interval_has_passed(self, tmp_path):
        path = tmp_path / "app-versions.json"
        with patch.object(versions, "_shell", return_value=DUMP) as shell:
            versions.check(force=True, path=path)
            versions._last_check[0] = time.time() - versions.CHECK_EVERY - 1
            versions.check(path=path)
        assert shell.call_count == 2

    def test_a_phone_that_will_not_answer_is_not_an_exception(self):
        with patch("subprocess.run", side_effect=OSError("no adb")):
            assert versions._shell(None) == ""
