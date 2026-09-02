"""The settings debugger, and the two properties it must keep.

The page maps the phone's own Settings onto the portal, and the whole safety
argument is the same one /macro and /device make: the browser posts an ID,
the id selects a row of a table in code, and only the row knows an intent
action. These tests pin that — a form field must never reach `am start` —
and pin the degradation the page promises: a Pi whose adb is gone renders
dashes, because the person opening a debug page is diagnosing exactly that.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from apt_log.ui import phonesettings
from apt_log.ui import state as state_mod
from apt_log.ui.app import app, queue
from apt_log.ui.i18n import catalog_keys


SCREEN = {
    "at": "2026-08-18T10:00:00",
    "app": "com.android.settings",
    "activity": ".Settings",
    "size": [1080, 2400],
    "blocked": "",
    "elements": [],
    "statics": [],
}


@pytest.fixture
def client(tmp_path):
    queue.cancel()
    (tmp_path / "screen.json").write_text(json.dumps(SCREEN), encoding="utf-8")
    with patch.object(state_mod, "STATE_DIR", tmp_path):
        yield TestClient(app)


@pytest.fixture
def no_adb(monkeypatch):
    """A Pi whose adb has gone away entirely."""
    def missing(*_a, **_k):
        raise OSError("No such file or directory: 'adb'")

    monkeypatch.setattr(phonesettings.subprocess, "run", missing)


def _fake_adb(transcript: bytes, recorded: list | None = None,
              returncode: int = 0, stderr: bytes = b""):
    """A subprocess.run double that answers every call with one transcript."""
    def run(cmd, **_kwargs):
        if recorded is not None:
            recorded.append(cmd)
        return SimpleNamespace(returncode=returncode, stdout=transcript,
                               stderr=stderr)

    return run


def _transcript(**overrides) -> bytes:
    """A full probe transcript in READINGS order, one value per probe."""
    values = {
        "locale": "es-US",
        "locale_default": "en-US",
        "timezone": "America/New_York",
        "android": "13",
        "model": "SM-A156U",
        "clock": "2026-09-02 15:30:12",
        "font_scale": "null",
        "brightness": "180",
        "screen_timeout": "120000",
        "wifi": "1",
        "bluetooth": "0",
        "airplane": "0",
        "auto_time": "1",
        "auto_zone": "null",
    }
    values.update(overrides)
    parts = [values[r["id"]] for r in phonesettings.READINGS]
    return ("\n~\n".join(parts) + "\n").encode()


class TestTheMapItself:
    def test_every_panel_is_wellformed_and_unique(self):
        ids = [p["id"] for p in phonesettings.PANELS]
        assert len(ids) == len(set(ids))
        for panel in phonesettings.PANELS:
            assert panel["action"].startswith("android."), panel
            assert panel["group"] in phonesettings.GROUPS, panel

    def test_every_panel_label_exists_in_both_catalogs(self):
        """A button whose caption is its raw key is a button nobody can
        read. The parity test guards en against es; this guards the table
        against both."""
        en, es = catalog_keys("en"), catalog_keys("es")
        for panel in phonesettings.PANELS:
            assert panel["label_key"] in en and panel["label_key"] in es

    def test_every_visible_reading_label_exists_in_both_catalogs(self):
        en, es = catalog_keys("en"), catalog_keys("es")
        for reading in phonesettings.READINGS:
            if reading["kind"] == "hidden":
                continue
            assert reading["label_key"] in en and reading["label_key"] in es

    def test_language_is_the_first_panel(self):
        """The reason the page exists comes first on it."""
        assert phonesettings.PANELS[0]["id"] == "language"
        assert phonesettings.PANELS[0]["action"] == \
            "android.settings.LOCALE_SETTINGS"

    def test_readings_are_read_only_by_construction(self):
        """No probe writes. The writes live in the clock section, behind
        their own allow-list — a `settings put` appearing HERE would be the
        readings growing a capability their docstring promises they lack."""
        for reading in phonesettings.READINGS:
            probe = reading["probe"]
            assert probe.startswith(("getprop ", "settings get ", "date ")), \
                probe
            assert "put" not in probe and "-s" not in probe


class TestOpenPanel:
    def test_an_unknown_id_never_reaches_adb(self, monkeypatch):
        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"", calls))
        with pytest.raises(KeyError):
            phonesettings.open_panel("android.intent.action.CALL")
        assert calls == []

    def test_the_intent_comes_from_the_table_not_the_caller(self, monkeypatch):
        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"Starting: Intent...", calls))
        monkeypatch.setattr("apt_log.device.send_ui_action", lambda *_: None)
        panel = phonesettings.open_panel("language")
        assert panel["id"] == "language"
        assert calls == [["adb", "shell", "am", "start", "-a",
                          "android.settings.LOCALE_SETTINGS"]]

    def test_am_reporting_an_error_on_stdout_is_a_refusal(self, monkeypatch):
        """`am start` says "Error: Activity not started" and exits 0, so the
        words have to be read — a phone without the screen must not report
        "opened"."""
        monkeypatch.setattr(
            phonesettings.subprocess, "run",
            _fake_adb(b"Error: Activity not started, unable to resolve"))
        monkeypatch.setattr("apt_log.device.send_ui_action", lambda *_: None)
        with pytest.raises(phonesettings.SettingsUnavailable):
            phonesettings.open_panel("language")

    def test_a_wake_that_fails_does_not_refuse_the_open(self, monkeypatch):
        def refuse(*_a):
            raise RuntimeError("screen is having a day")

        monkeypatch.setattr("apt_log.device.send_ui_action", refuse)
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"Starting: Intent..."))
        assert phonesettings.open_panel("wifi")["id"] == "wifi"


class TestReadings:
    def test_a_full_transcript_parses_onto_the_right_rows(self, monkeypatch):
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(_transcript()))
        doc = phonesettings.readings()
        assert doc["ok"]
        by_id = {r["id"]: r for r in doc["rows"]}
        assert by_id["locale"]["value"] == "es-US"
        assert by_id["timezone"]["value"] == "America/New_York"
        assert by_id["wifi"]["value"] == "1"
        assert by_id["bluetooth"]["value"] == "0"
        # 120000 ms is a screen timeout somebody can reason about in minutes.
        assert by_id["screen_timeout"]["value"] == "2 min"
        assert by_id["brightness"]["value"] == "180 / 255"
        # `settings get` says "null" for never-set; for font scale that IS
        # the default, and a dash there would read as "unknown".
        assert by_id["font_scale"]["value"] == "1.0"
        # never-set auto_time_zone has no default worth asserting.
        assert by_id["auto_zone"]["value"] is None

    def test_an_unset_locale_falls_back_to_the_factory_one(self, monkeypatch):
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(_transcript(locale="")))
        doc = phonesettings.readings()
        by_id = {r["id"]: r for r in doc["rows"]}
        assert by_id["locale"]["value"] == "en-US"
        # The fallback probe itself never renders as a row.
        assert "locale_default" not in by_id

    def test_no_adb_degrades_to_dashes_not_an_exception(self, no_adb):
        doc = phonesettings.readings()
        assert doc["ok"] is False
        assert doc["rows"], "the rows still exist to render"
        assert all(r["value"] is None for r in doc["rows"])

    def test_half_a_transcript_is_refused_whole(self, monkeypatch):
        """A phone mid-reboot answers with a truncated transcript; parsed
        by position it would put the wifi answer on the bluetooth row."""
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"es-US\n~\nen-US\n"))
        doc = phonesettings.readings()
        assert doc["ok"] is False
        assert all(r["value"] is None for r in doc["rows"])


def _clock_adb(calls: list, zone: str = "America/New_York"):
    """An adb double for the clock paths: answers the timezone probe with a
    real zone name and takes every write silently."""
    def run(cmd, **_kwargs):
        calls.append(cmd)
        if "persist.sys.timezone" in cmd:
            return SimpleNamespace(returncode=0,
                                   stdout=(zone + "\n").encode(), stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    return run


class TestTheClock:
    def test_an_unknown_switch_never_reaches_adb(self, monkeypatch):
        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"", calls))
        with pytest.raises(KeyError):
            phonesettings.set_time_switch("adb_enabled", False)
        assert calls == []

    def test_the_switch_key_comes_from_the_table(self, monkeypatch):
        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"", calls))
        phonesettings.set_time_switch("auto_zone", False)
        assert calls == [["adb", "shell", "settings", "put", "global",
                          "auto_time_zone", "0"]]

    def test_set_clock_reads_the_time_in_the_phones_zone(self, monkeypatch):
        """15:30 typed means the PHONE's 15:30 — the person typing may be two
        time zones from the handset, and a clock set in the browser's zone
        would be hours off while looking exactly right."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _clock_adb(calls))
        millis = phonesettings.set_clock("2026-09-02T15:30")
        wanted = datetime(2026, 9, 2, 15, 30,
                          tzinfo=ZoneInfo("America/New_York"))
        assert millis == int(wanted.timestamp() * 1000)
        assert ["adb", "shell", "cmd", "alarm", "set-time",
                str(millis)] in calls

    def test_set_clock_switches_automatic_time_off_first(self, monkeypatch):
        """With auto_time on, the network puts the time straight back within
        seconds — a control that visibly works and silently un-works."""
        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _clock_adb(calls))
        phonesettings.set_clock("2026-09-02T15:30")
        auto_off = calls.index(["adb", "shell", "settings", "put", "global",
                                "auto_time", "0"])
        set_time = next(i for i, c in enumerate(calls) if "set-time" in c)
        assert auto_off < set_time

    def test_a_time_that_will_not_parse_writes_nothing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _clock_adb(calls))
        with pytest.raises(ValueError):
            phonesettings.set_clock("half past nine")
        assert calls == []

    def test_an_offset_smuggled_into_the_time_is_refused(self, monkeypatch):
        """The page sends naive datetimes; an offset would quietly make "the
        phone's own zone" mean something else."""
        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _clock_adb(calls))
        with pytest.raises(ValueError):
            phonesettings.set_clock("2026-09-02T15:30+05:00")
        assert calls == []

    def test_reset_turns_both_switches_back_on(self, monkeypatch):
        """The undo for the section, and deliberately BOTH switches whatever
        a test flipped: "back on automatic" is one state, not a memory."""
        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"", calls))
        phonesettings.reset_clock()
        assert calls == [
            ["adb", "shell", "settings", "put", "global", "auto_time", "1"],
            ["adb", "shell", "settings", "put", "global",
             "auto_time_zone", "1"],
        ]

    def test_reset_never_sets_a_time_itself(self, monkeypatch):
        """The network is the one clock source that cannot be a stale guess;
        a reset that wrote its own idea of "now" would just be a second
        wrong clock."""
        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"", calls))
        phonesettings.reset_clock()
        assert all("set-time" not in c for c in calls)

    def test_an_unreadable_zone_refuses_rather_than_guessing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _clock_adb(calls, zone=""))
        with pytest.raises(phonesettings.SettingsUnavailable):
            phonesettings.set_clock("2026-09-02T15:30")
        # The timezone probe ran; no write did.
        assert all("put" not in c and "set-time" not in c for c in calls)


class TestTheClockReading:
    def setup_method(self):
        phonesettings.forget_clock()

    def test_the_phones_own_hour_in_the_launchers_spelling(self, monkeypatch):
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"2026-09-02 05:07 EST\n~\n1\n~\n1\n"))
        doc = phonesettings.clock_state(fresh=True)
        assert doc["ok"]
        assert doc["said"] == "5:07 AM EST"
        assert doc["auto"] is True and doc["auto_zone"] is True

    def test_a_never_set_switch_is_unknown_not_off(self, monkeypatch):
        """`settings get` says "null" for a switch nothing has touched; on
        this phone that is the default (automatic), and the page must not
        raise the amber pill over it."""
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"2026-09-02 05:07 EST\n~\nnull\n~\nnull\n"))
        doc = phonesettings.clock_state(fresh=True)
        assert doc["auto"] is None and doc["auto_zone"] is None

    def test_no_phone_is_not_ok_and_says_nothing(self, no_adb):
        doc = phonesettings.clock_state(fresh=True)
        assert doc["ok"] is False and doc["said"] == ""

    def test_the_reading_is_cached_and_a_write_busts_it(self, monkeypatch):
        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"2026-09-02 05:07 EST\n~\n1\n~\n1\n", calls))
        phonesettings.clock_state()
        phonesettings.clock_state()
        assert len(calls) == 1, "the socket asks every tick for every viewer"
        phonesettings.set_time_switch("auto_time", False)
        phonesettings.clock_state()
        # the switch write, then a fresh read
        assert len(calls) == 3


class TestTheRoutes:
    def test_the_page_renders_with_no_phone_at_all(self, client, no_adb):
        """The person opening a debug page is diagnosing why adb went away.
        An exception page here is the tool failing at its one moment."""
        body = client.get("/debug").text
        assert "debug.title" not in body  # every key resolved
        for panel in phonesettings.PANELS:
            assert f'value="{panel["id"]}"' in body

    def test_an_unknown_panel_is_refused_not_forwarded(self, client,
                                                       monkeypatch):
        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"", calls))
        r = client.post("/debug/open",
                        data={"panel": "android.settings.SETTINGS"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/debug?opened=unknown"
        assert calls == []

    def test_opening_language_lands_back_with_the_notice(self, client,
                                                         monkeypatch):
        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"Starting: Intent...", calls))
        monkeypatch.setattr("apt_log.device.send_ui_action", lambda *_: None)
        r = client.post("/debug/open", data={"panel": "language"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/debug?opened=language"
        assert ["adb", "shell", "am", "start", "-a",
                "android.settings.LOCALE_SETTINGS"] in calls

    def test_a_phone_that_refuses_says_failed(self, client, monkeypatch):
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"Error: unable to resolve"))
        monkeypatch.setattr("apt_log.device.send_ui_action", lambda *_: None)
        r = client.post("/debug/open", data={"panel": "language"},
                        follow_redirects=False)
        assert r.headers["location"] == "/debug?opened=failed"

    def test_the_api_translates_the_switches(self, client, monkeypatch):
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(_transcript()))
        doc = client.get("/api/phone-settings",
                         headers={"accept-language": "en"}).json()
        by_id = {r["id"]: r for r in doc["rows"]}
        assert by_id["wifi"]["said"] == "On"
        assert by_id["bluetooth"]["said"] == "Off"
        assert by_id["auto_zone"]["said"] == "—"

    def test_the_switch_route_answers_with_the_notice(self, client,
                                                      monkeypatch):
        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"", calls))
        r = client.post("/debug/time/switch",
                        data={"switch": "auto_time", "on": "0"},
                        follow_redirects=False)
        assert r.headers["location"] == "/debug?saved=switch"
        assert ["adb", "shell", "settings", "put", "global",
                "auto_time", "0"] in calls

    def test_a_bad_time_reports_itself_and_writes_nothing(self, client,
                                                          monkeypatch):
        calls = []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"", calls))
        r = client.post("/debug/time/set", data={"when": "half past nine"},
                        follow_redirects=False)
        assert r.headers["location"] == "/debug?saved=bad_time"
        assert calls == []

    def test_the_set_route_asks_the_feed_process_not_adb(self, client,
                                                         monkeypatch):
        """A change of clock owes the phone an app restart and a cleared
        Settings screen, and only the feed process can do those — so the
        page asks for the `clock_set` macro rather than writing the clock
        itself. Nothing touches adb from this process."""
        from apt_log import macros as macros_mod

        calls, asked = [], []
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"", calls))
        monkeypatch.setattr(macros_mod, "request",
                            lambda name, arg="", **_k: asked.append((name, arg)))
        r = client.post("/debug/time/set", data={"when": "2026-09-02T15:30"},
                        follow_redirects=False)
        assert r.headers["location"] == "/debug?saved=clock"
        assert asked == [("clock_set", "2026-09-02T15:30")]
        assert calls == []

    def test_an_offset_in_the_time_is_a_bad_time_here_too(self, client,
                                                          monkeypatch):
        from apt_log import macros as macros_mod

        asked = []
        monkeypatch.setattr(macros_mod, "request",
                            lambda name, arg="", **_k: asked.append(name))
        r = client.post("/debug/time/set",
                        data={"when": "2026-09-02T15:30+05:00"},
                        follow_redirects=False)
        assert r.headers["location"] == "/debug?saved=bad_time"
        assert asked == []

    def test_the_reset_route_asks_the_feed_process(self, client, monkeypatch):
        from apt_log import macros as macros_mod

        asked = []
        monkeypatch.setattr(macros_mod, "request",
                            lambda name, arg="", **_k: asked.append(name))
        r = client.post("/debug/time/reset", follow_redirects=False)
        assert r.headers["location"] == "/debug?saved=reset"
        assert asked == ["clock_reset"]

    def test_the_clock_api_reads_fresh(self, client, monkeypatch):
        monkeypatch.setattr(phonesettings.subprocess, "run",
                            _fake_adb(b"2026-09-02 17:38 EDT\n~\n0\n~\n1\n"))
        phonesettings.forget_clock()
        doc = client.get("/api/clock").json()
        assert doc["ok"] and doc["said"] == "5:38 PM EDT"
        assert doc["auto"] is False and doc["auto_zone"] is True
        assert doc["date"] == "2026-09-02" and doc["time"] == "17:38"

    def test_the_console_offers_the_way_in(self, client, no_adb):
        """The tab strip, on both pages, each pointing at the other."""
        assert 'href="/debug"' in client.get("/console").text
        assert 'href="/console"' in client.get("/debug").text
