"""HHAeXchange+'s schedule after a visit is checked out renders, not 500s.

30 October: the visit card gained "Registros de salida de EVV 9:00 a. m."
under its check-in line. That line overlaps the top of the next visit's card
by a few pixels, so it was banded with that card, and the rule that makes
side-by-side rows into controls turned it into a button with no aim. The
template's `it.aim | tojson` then failed on the missing aim and every load of
/app answered 500 Internal Server Error.

The fixture is that screen, with names, address and phone replaced.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from apt_log.ui import screenview as sv

FIXTURE = Path(__file__).parent / "fixtures" / "uma_checked_out.json"


def _doc() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _items(model):
    for row in model.get("rows") or ():
        yield from row.get("items") or ()


class TestTheCheckOutLine:
    def test_no_row_without_an_aim_is_drawn_as_a_control(self):
        model = sv.build(_doc())
        bad = [it for it in _items(model)
               if it.get("kind") == "row" and not it.get("aim")
               and not it.get("info")]
        assert bad == []

    def test_the_check_out_record_is_still_shown(self):
        model = sv.build(_doc())
        said = json.dumps(model, ensure_ascii=False)
        assert "Registros de salida de EVV 9:00 a. m." in said

    def test_the_next_visit_is_still_a_control(self):
        model = sv.build(_doc())
        cards = [it for it in _items(model)
                 if "PATIENT TWO" in json.dumps(it.get("lines") or [])]
        assert cards and all(it.get("aim") for it in cards)


class TestThePageRenders:
    def test_the_app_page_answers_200_on_this_screen(self, tmp_path,
                                                    monkeypatch):
        from apt_log.ui import state as state_mod

        doc = _doc()
        (tmp_path / "screen.json").write_text(json.dumps(doc),
                                              encoding="utf-8")
        monkeypatch.setattr(state_mod, "STATE_DIR", tmp_path)
        from apt_log.ui.app import app

        r = TestClient(app).get("/app")
        assert r.status_code == 200

    def test_a_row_without_an_aim_renders_as_a_statement(self):
        """Belt and braces: whatever a future rule decides, the template
        never draws an aim-less row as a button."""
        tpl = Path("src/apt_log/ui/templates/_screen.html").read_text(
            encoding="utf-8")
        assert "{% elif it.kind == 'row' and (it.info or not it.aim) %}" in tpl


SECOND = Path(__file__).parent / "fixtures" / "uma_visit_in_progress.json"


class TestTheCheckInLineIsNotASectionSwitch:
    """5 November: the same kind of statement — "Registros de entrada de
    EVV 8:05 p. m." — tiled beside the disabled "La visita está en curso"
    and was folded into a two-part section switch, one part with no aim.
    The template's `tojson` raised and /app answered 500 again."""

    def _model(self):
        return sv.build(json.loads(SECOND.read_text(encoding="utf-8")))

    def test_no_segment_part_that_can_be_pressed_lacks_an_aim(self):
        def parts(o):
            if isinstance(o, dict):
                if o.get("kind") == "segment":
                    yield from o["parts"]
                for v in o.values():
                    yield from parts(v)
            elif isinstance(o, list):
                for v in o:
                    yield from parts(v)
        bad = [p for p in parts(self._model())
               if p.get("enabled") and not p.get("aim")]
        assert bad == []

    def test_the_check_in_record_is_still_shown(self):
        said = json.dumps(self._model(), ensure_ascii=False)
        assert "Registros de entrada de EVV 8:05 p. m." in said

    def test_the_app_page_answers_200_on_this_screen(self, tmp_path,
                                                    monkeypatch):
        from apt_log.ui import state as state_mod

        (tmp_path / "screen.json").write_text(SECOND.read_text(
            encoding="utf-8"), encoding="utf-8")
        monkeypatch.setattr(state_mod, "STATE_DIR", tmp_path)
        from apt_log.ui.app import app

        assert TestClient(app).get("/app").status_code == 200


class TestNoMissingAimCanTakeThePageDown:
    """Whatever a future rule gets wrong, a control without an aim is a dead
    button, never a 500 on the whole page."""

    def test_every_aim_in_the_screen_template_goes_through_aimjson(self):
        tpl = Path("src/apt_log/ui/templates/_screen.html").read_text(
            encoding="utf-8")
        assert "| tojson" not in tpl
        assert tpl.count("| aimjson") >= 19

    def test_a_missing_aim_renders_as_null(self):
        from apt_log.ui.app import templates

        env = templates.env
        out = env.from_string("{{ x.aim | aimjson }}").render(x={})
        assert out == "null"
        out = env.from_string("{{ x.aim | aimjson }}").render(
            x={"aim": {"rid": "a", "b": [1, 2, 3, 4]}})
        assert json.loads(out) == {"rid": "a", "b": [1, 2, 3, 4]}

    def test_the_page_refuses_to_send_a_null_aim(self):
        js = Path("src/apt_log/ui/static/phone.js").read_text(
            encoding="utf-8")
        body = js[js.index("try { aim = JSON.parse(el.dataset.aim); }"):]
        # Straight after the parse, before anything can send it.
        assert body.index("if (!aim) return;") < body.index("socket")
