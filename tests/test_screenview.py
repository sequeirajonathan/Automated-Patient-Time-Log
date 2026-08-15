"""Reflowing an Android screen into native rows.

The property these tests hold: reflow changes where a control is drawn, never
what tapping it means — every interactive item keeps its rid/class/bounds aim
— and the things she reads together stay together.
"""

from __future__ import annotations

from apt_log.ui import screenview


def el(rid, cls, b, txt="", checked=False):
    return {"rid": rid, "cls": cls, "b": b, "txt": txt, "checked": checked,
            "focused": False, "has_text": bool(txt)}


def st(b, txt, cls="TextView"):
    return {"cls": cls, "b": b, "txt": txt}


def doc(elements, statics, size=(720, 1600)):
    return {"id": "f1", "size": list(size), "elements": elements,
            "statics": statics, "blocked": "", "notice": ""}


class TestCarePlanRow:
    """The screen that motivated the reflow: '127 - Toilet Use  [✓][✗]'."""

    ROW_Y = (770, 840)

    def _model(self):
        return screenview.build(doc(
            elements=[
                el("chk_yes", "RadioButton", [640, 775, 690, 835], "✓"),
                el("chk_no", "RadioButton", [692, 775, 720, 835], "✗"),
            ],
            statics=[st([90, 780, 600, 830], "127 - Toilet Use")],
        ))

    def test_the_label_and_its_toggles_share_a_row(self):
        rows = self._model()["rows"]
        row = rows[0]["items"]
        kinds = [i["kind"] for i in row]
        assert "label" in kinds and "segment" in kinds

    def test_flush_small_buttons_become_one_segmented_control(self):
        row = self._model()["rows"][0]["items"]
        seg = next(i for i in row if i["kind"] == "segment")
        assert [p["aim"]["rid"] for p in seg["parts"]] == ["chk_yes", "chk_no"]

    def test_every_part_keeps_its_own_aim(self):
        row = self._model()["rows"][0]["items"]
        seg = next(i for i in row if i["kind"] == "segment")
        for part in seg["parts"]:
            assert part["aim"]["b"] and part["aim"]["cls"] == "RadioButton"


class TestContainment:
    def test_labels_inside_a_tappable_row_become_its_text(self):
        """An unlabelled rectangle above three orphaned words is a list cell
        that lost its label. Fold it back in."""
        m = screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 500, 720, 700])],
            statics=[st([20, 520, 700, 580], "Visita no programada"),
                     st([20, 590, 700, 660], "08:00 PM - 09:00 PM")],
        ))
        cell = m["rows"][0]["items"][0]
        assert cell["kind"] == "row"
        assert cell["lines"] == ["Visita no programada", "08:00 PM - 09:00 PM"]

    def test_a_folded_label_is_not_also_rendered_loose(self):
        m = screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 500, 720, 700])],
            statics=[st([20, 520, 700, 580], "Visita no programada")],
        ))
        all_items = [i for r in m["rows"] for i in r["items"]]
        assert len([i for i in all_items
                    if i["kind"] == "label"]) == 0


class TestNavBar:
    def test_the_apps_own_nav_is_recognised(self):
        m = screenview.build(doc(
            elements=[el("btn_back", "Button", [0, 20, 120, 120], "Atrás"),
                      el("btn_add", "Button", [640, 20, 720, 120], "+")],
            statics=[st([200, 30, 560, 110], "Detalle de Visita")],
        ))
        assert m["nav"] is not None
        assert m["nav"]["title"] == "Detalle de Visita"
        assert m["nav"]["back"]["aim"]["rid"] == "btn_back"
        assert [b["aim"]["rid"] for b in m["nav"]["trailing"]] == ["btn_add"]

    def test_a_screen_without_a_top_bar_has_no_nav(self):
        m = screenview.build(doc(
            elements=[el("btn_go", "Button", [100, 800, 620, 900], "Entrar")],
            statics=[],
        ))
        assert m["nav"] is None
        assert m["rows"][0]["items"][0]["kind"] == "button"


class TestOrderAndOrientation:
    def test_reading_order_is_top_to_bottom(self):
        m = screenview.build(doc(
            elements=[el("low", "Button", [0, 1000, 720, 1100], "Low"),
                      el("high", "Button", [0, 300, 720, 400], "High")],
            statics=[],
        ))
        rids = [i["aim"]["rid"] for r in m["rows"] for i in r["items"]]
        assert rids == ["high", "low"]

    def test_a_landscape_screen_is_understood_sideways(self):
        """The signature screen: bounds overflow the portrait width."""
        m = screenview.build(doc(
            elements=[el("btn_save", "Button", [1400, 600, 1580, 700], "Salvar")],
            statics=[], size=(720, 1600),
        ))
        assert m is not None
        assert m["rows"][0]["items"][0]["aim"]["rid"] == "btn_save"

    def test_no_size_is_no_model(self):
        assert screenview.build({"size": [0, 0]}) is None
