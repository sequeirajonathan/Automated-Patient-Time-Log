"""The control centre, and the two things it is for.

It exists because the phone view cannot be both. /app decides what is worth
showing somebody standing in a patient's kitchen — folds bands, sweeps
furniture, refuses pictures. That editing is the product, and it is also what
makes the front end impossible to teach: when a row does not appear, you cannot
tell from /app whether the app did not show it or the portal ate it.

So the two properties tested here are opposites of each other, and both are
deliberate:

  · the control centre hides nothing,
  · the density slider on it cannot reach the value that crashed the phone.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from apt_log import prefs
from apt_log.ui import machine as machine_mod
from apt_log.ui import state as state_mod
from apt_log.ui.app import app, queue

SCREEN = {
    "at": "2026-08-18T10:00:00",
    "app": "com.hhaexchange.caregiver",
    "activity": ".TodayActivity",
    "size": [1080, 2400],
    "blocked": "",
    "elements": [
        {"rid": "btnClockIn", "cls": "Button", "b": [40, 900, 1040, 1000],
         "enabled": True, "checked": False, "has_text": True,
         "txt": "Entrada"},
        {"rid": "btnClockOut", "cls": "Button", "b": [40, 1020, 1040, 1120],
         "enabled": False, "checked": False, "has_text": True,
         "txt": "Salida"},
    ],
    "statics": [
        {"cls": "TextView", "b": [40, 300, 1040, 360], "txt": "Horario para hoy"},
        {"cls": "TextView", "b": [40, 380, 1040, 430], "txt": "8:00 - 9:00"},
    ],
}


@pytest.fixture
def client(tmp_path):
    queue.cancel()
    (tmp_path / "screen.json").write_text(json.dumps(SCREEN), encoding="utf-8")
    with patch.object(state_mod, "STATE_DIR", tmp_path):
        yield TestClient(app)


class TestLanding:
    def test_the_front_door_is_the_phone(self, client):
        """Nobody opens this portal to read a status page. For a year the
        status page was what / served, and every session began by scrolling
        past it to reach the one link that did the job."""
        r = client.get("/", follow_redirects=False)
        assert r.status_code in (307, 303)
        assert r.headers["location"] == "/app"

    def test_the_two_pages_link_to_each_other(self, client):
        assert 'href="/console"' in client.get("/app").text
        assert 'href="/app"' in client.get("/console").text


class TestNothingIsHidden:
    """The rule this page is built on, asserted against the rows themselves."""

    def test_every_row_the_phone_reported_is_printed(self, client):
        body = client.get("/console").text
        for text in ("Horario para hoy", "8:00 - 9:00", "Entrada", "Salida"):
            assert text in body

    def test_the_untappable_text_is_there_too_not_only_the_controls(self, client):
        """The reflow folds labels into the control they belong to, which is
        right for her and wrong for someone working out why a label is
        missing."""
        body = client.get("/console").text
        assert "Horario para hoy" in body

    def test_resource_ids_are_shown_because_that_is_what_names_a_row(self, client):
        body = client.get("/console").text
        assert "btnClockIn" in body

    def test_a_greyed_control_is_marked_rather_than_dropped(self, client):
        body = client.get("/console").text
        assert "desactivado" in body

    def test_rows_come_out_in_the_order_they_sit_on_the_glass(self, client):
        from apt_log.ui.app import _raw_rows

        rows = _raw_rows(SCREEN)
        tops = [r["b"][1] for r in rows]
        assert tops == sorted(tops)
        assert rows[0]["txt"] == "Horario para hoy"


class TestDensityPanel:
    def test_the_slider_cannot_reach_the_value_that_crashed_the_phone(self, client):
        body = client.get("/console").text
        assert f'min="{prefs.DENSITY_FLOOR}"' in body
        assert f'max="{prefs.DENSITY_CEILING}"' in body

    def test_it_is_one_slider_and_two_buttons(self, client):
        """The first version had a three-way scope grid, which is the shape of
        the STORE, not of the decision. What somebody does here is drag it,
        look at the phone, and keep it or put it back."""
        body = client.get("/console").text
        assert body.count('type="range"') == 1
        assert "Aplicar" in body and "Volver al valor de siempre" in body

    def test_applying_writes_an_override_for_the_app_in_front(self, client):
        client.post("/settings/density",
                    data={"scope": "app", "value": "120",
                          "app_pkg": "com.hhaexchange.caregiver",
                          "next": "/console"})
        assert prefs.density_for("com.hhaexchange.caregiver") == 120

    def test_reset_is_an_empty_value_not_a_number(self, client):
        """The Reset button posts the same form with no value, which is what
        "clear this" means in the store. A number would be a different
        override, not the absence of one."""
        prefs.set_density("com.hhaexchange.caregiver", 120)
        client.post("/settings/density",
                    data={"scope": "app", "value": "",
                          "app_pkg": "com.hhaexchange.caregiver",
                          "next": "/console"})
        assert prefs.density_for("com.hhaexchange.caregiver") is None

    def test_a_page_override_from_before_is_still_honoured(self, client):
        """The UI stopped offering page scope; the store still reads it, so a
        value set when it did is not silently dropped."""
        prefs.set_density("com.hhaexchange.caregiver", 120,
                          page=".SignatureActivity")
        assert prefs.density_for("com.hhaexchange.caregiver",
                                 page=".SignatureActivity") == 120

    def test_an_out_of_range_post_is_clamped_not_refused(self, client):
        """Refusing would leave her with a control that appears to do nothing.
        Clamping does what she meant, safely, and shows the value it used."""
        client.post("/settings/density",
                    data={"scope": "app", "value": "12",
                          "app_pkg": "com.inmyteam.inmyteam", "next": "/console"})
        assert prefs.density_for("com.inmyteam.inmyteam") == prefs.DENSITY_FLOOR

    def test_clearing_an_override_is_offered_beside_it(self, client):
        client.post("/settings/density",
                    data={"scope": "app", "value": "120",
                          "app_pkg": "com.inmyteam.inmyteam", "next": "/console"})
        assert "com.inmyteam.inmyteam" in client.get("/console").text
        client.post("/settings/density/clear",
                    data={"key": "com.inmyteam.inmyteam", "next": "/console"})
        assert prefs.overrides() == {}

    def test_the_global_setting_is_for_everything_that_is_not_a_care_app(self, client):
        client.post("/settings/density",
                    data={"scope": "global", "value": "150", "next": "/console"})
        assert prefs.global_density() == 150
        assert prefs.density_for("com.inmyteam.inmyteam") is None


class TestDensityReachesThePhone:
    """A slider nothing reads is a slider that lies."""

    def test_an_override_wins_over_the_tuned_default(self, tmp_path, monkeypatch):
        from apt_log import feed

        monkeypatch.setattr(prefs, "PREFS_PATH", tmp_path / "prefs.json")
        assert feed._density_wanted("com.inmyteam.inmyteam/.Main") == 105
        prefs.set_density("com.inmyteam.inmyteam", 140)
        assert feed._density_wanted("com.inmyteam.inmyteam/.Main") == 140

    def test_clearing_it_puts_the_tuned_default_back(self, tmp_path, monkeypatch):
        from apt_log import feed

        monkeypatch.setattr(prefs, "PREFS_PATH", tmp_path / "prefs.json")
        prefs.set_density("com.inmyteam.inmyteam", 140)
        prefs.set_density("com.inmyteam.inmyteam", None)
        assert feed._density_wanted("com.inmyteam.inmyteam/.Main") == 105

    def test_a_page_override_beats_the_landscape_signature_rule(self, tmp_path,
                                                                monkeypatch):
        """Somebody who deliberately set a value for the signature page means
        it for the signature page."""
        from apt_log import feed

        monkeypatch.setattr(prefs, "PREFS_PATH", tmp_path / "prefs.json")
        landscape = '<node bounds="[0,0][1200,800]" />'
        focus = "com.hhaexchange.caregiver/.SignatureActivity"
        assert feed._density_wanted(focus, landscape) == feed.SIGNATURE_DENSITY
        prefs.set_density("com.hhaexchange.caregiver", 130,
                          page=".SignatureActivity")
        assert feed._density_wanted(focus, landscape) == 130

    def test_an_app_this_system_knows_nothing_about_is_left_alone(self, tmp_path,
                                                                  monkeypatch):
        """Until somebody asks for it. Forcing a density on the phone's own
        settings app would be this system reaching outside what it is for."""
        from apt_log import feed

        monkeypatch.setattr(prefs, "PREFS_PATH", tmp_path / "prefs.json")
        assert feed._density_wanted("com.android.settings/.Main") is None
        prefs.set_global_density(150)
        assert feed._density_wanted("com.android.settings/.Main") == 150


class TestOperations:
    """The shortcuts somebody actually reaches for from two thousand
    kilometres away, in place of the four sign-in walks that run themselves."""

    def test_the_page_offers_the_operational_ones(self, client):
        from apt_log import macros

        body = client.get("/console").text
        for name in macros.OPERATIONS:
            assert f'value="{name}"' in body

    def test_closing_refuses_when_the_phone_is_not_in_a_care_app(self, monkeypatch):
        """Force-stopping the system UI is a bigger hammer than any screen is
        worth, and this button is reachable from a phone in another state."""
        from apt_log import feed, macros

        monkeypatch.setattr(feed, "current_focus",
                            lambda *a, **k: "com.android.settings/.Settings")
        stopped = []
        monkeypatch.setattr(macros, "_force_stop", lambda pkg: stopped.append(pkg))
        with pytest.raises(RuntimeError):
            macros.MACROS["close_app"].run(None, lambda *a: None)
        assert stopped == []

    def test_closing_a_care_app_force_stops_it(self, monkeypatch):
        from apt_log import feed, macros

        monkeypatch.setattr(feed, "current_focus",
                            lambda *a, **k: "com.inmyteam.inmyteam/.Main")
        stopped = []
        monkeypatch.setattr(macros, "_force_stop", lambda pkg: stopped.append(pkg))
        monkeypatch.setattr(macros, "_forget_stitched", lambda app: None)
        macros.MACROS["close_app"].run(None, lambda *a: None)
        assert stopped == ["com.inmyteam.inmyteam"]

    def test_restarting_relaunches_through_the_launcher_not_the_driver(self,
                                                                      monkeypatch):
        """After a force-stop the driver's handle on the app is stale, and
        asking it to activate the app it has just lost is how a recovery
        hangs. The ANR watchdog learned this the same way."""
        from apt_log import feed, macros

        monkeypatch.setattr(feed, "current_focus",
                            lambda *a, **k: "com.inmyteam.inmyteam/.Main")
        sent = []
        monkeypatch.setattr(feed, "_adb", lambda args, *a, **k: sent.append(args))
        monkeypatch.setattr(macros, "_forget_stitched", lambda app: None)
        monkeypatch.setattr(macros, "wake_display", lambda: None)
        monkeypatch.setattr(macros.time, "sleep", lambda *_: None)
        monkeypatch.setattr(macros, "wait_for", lambda *a, **k: True)
        macros.MACROS["restart_app"].run(None, lambda *a: None)
        assert ["shell", "am", "force-stop", "com.inmyteam.inmyteam"] in sent
        assert any("monkey" in a for a in sent)

    def test_rescan_drops_the_cache_before_walking(self, monkeypatch):
        """The whole point: the page is being rendered from a stitched copy
        that is no longer true, so trusting the cache is the bug."""
        from apt_log import feed, macros

        order = []
        monkeypatch.setattr(feed, "current_focus",
                            lambda *a, **k: "com.inmyteam.inmyteam/.Main")
        monkeypatch.setattr(macros, "_forget_stitched",
                            lambda app: order.append(("forget", app)))
        monkeypatch.setattr(macros, "_stitch_walk",
                            lambda d, **k: order.append(("walk",)))
        macros.MACROS["rescan"].run(None, lambda *a: None)
        assert order == [("forget", "com.inmyteam.inmyteam"), ("walk",)]

    def test_the_reboot_does_not_hold_the_session_waiting(self, monkeypatch):
        """It takes the driver with it. A macro sitting for a minute holding a
        session that is already gone looks wedged while the phone is doing
        exactly what it was told."""
        from apt_log import feed, macros

        sent = []
        monkeypatch.setattr(feed, "_adb", lambda args, *a, **k: sent.append(args))
        monkeypatch.setattr(macros, "wait_for",
                            lambda *a, **k: pytest.fail("must not wait"))
        macros.MACROS["restart_phone"].run(None, lambda *a: None)
        assert sent == [["reboot"]]

    def test_the_reboot_is_the_only_one_that_asks_first(self):
        from apt_log import macros

        assert macros.CONFIRM == ("restart_phone",)


class TestWhoIsOn:
    def test_a_visit_is_recorded_with_the_page_it_was_on(self, client):
        client.get("/console")
        assert [d["where"] for d in prefs.devices()] == ["/console"]

    def test_naming_this_device_makes_the_list_readable(self, client):
        client.get("/console")
        client.post("/settings/name", data={"name": "Jonathan — laptop",
                                            "next": "/console"})
        assert "Jonathan — laptop" in client.get("/console").text

    def test_the_same_browser_stays_one_device(self, client):
        client.get("/console")
        client.get("/app")
        client.get("/console")
        assert len(prefs.devices()) == 1


class TestMetrics:
    def test_a_reading_never_raises_whatever_the_machine_says(self):
        doc = machine_mod.read(force=True)
        assert "services" in doc and "phone" in doc

    def test_a_missing_probe_is_unknown_rather_than_a_zero(self, monkeypatch):
        """Zero free memory and "the file was not there" are different news."""
        monkeypatch.setattr(machine_mod, "_meminfo", lambda: None)
        monkeypatch.setattr(machine_mod, "_temperature", lambda: None)
        doc = machine_mod.read(force=True)
        assert doc["mem"] is None
        assert doc["temp_c"] is None

    def test_a_unit_somebody_turned_off_is_not_a_fault(self):
        """The scheduler is disabled on purpose — the machine deciding when to
        record a visit is the thing that was abandoned. Shown in the same red
        as a crash it would be red every day, and a row that is red every day
        is a row people stop reading."""
        doc = {"services": [{"unit": "aptlog-agent", "state": "off"},
                            {"unit": "aptlog-ui", "state": "ok"}],
               "phone": {"attached": "ok", "battery": 80},
               "net": {"up": "ok"}}
        assert machine_mod.worst(doc) == "ok"

    def test_a_unit_that_stopped_while_still_enabled_is_a_fault(self, monkeypatch):
        calls = []

        class Done:
            stdout = "ActiveState=failed\nUnitFileState=enabled\n"

        monkeypatch.setattr(machine_mod.subprocess, "run",
                            lambda *a, **k: calls.append(a) or Done())
        assert machine_mod._unit("aptlog-ui") == "bad"

    def test_the_summary_is_bad_when_anything_is_nearly_full(self):
        doc = {"services": [{"unit": "x", "state": "ok"}],
               "phone": {"attached": "ok", "battery": 80},
               "net": {"up": "ok"},
               "mem": {"pct": 40}, "disk": {"pct": 97}}
        assert machine_mod.worst(doc) == "bad"

    def test_a_nearly_flat_phone_is_bad_news_before_it_is_a_dead_one(self):
        doc = {"services": [{"unit": "x", "state": "ok"}],
               "phone": {"attached": "ok", "battery": 4},
               "net": {"up": "ok"}}
        assert machine_mod.worst(doc) == "bad"

    def test_the_page_renders_when_no_probe_can_answer(self, client, monkeypatch):
        """A control centre that will not render because /proc changed shape is
        worse than one with blank lines in it."""
        monkeypatch.setattr(machine_mod, "read", lambda *a, **k: {
            "at": 0, "host": "", "mem": None, "disk": None, "state_disk": None,
            "temp_c": None, "uptime": None, "load": None, "cpus": 1,
            "services": [], "net": {"up": "unknown", "address": ""},
            "phone": {"attached": "unknown", "battery": None, "charging": None,
                      "temp_c": None, "density": None}})
        assert client.get("/console").status_code == 200


class TestItStaysMobile:
    """Opened from a bookmark on an iPhone at least as often as from a desk."""

    def test_it_owns_the_status_bar_region(self, client):
        body = client.get("/console").text
        assert "env(safe-area-inset-top)" in body
        assert "env(safe-area-inset-bottom)" in body

    def test_it_scales_to_the_device_width(self, client):
        assert "width=device-width" in client.get("/console").text
