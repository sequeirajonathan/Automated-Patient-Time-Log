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
