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


class TestUtilityButtons:
    def test_a_narrow_button_is_marked_small(self):
        """A badge rendered at full width is promoted to a call to action it
        never was — seen on the real agency home screen, where a '2' badge
        became the biggest button on the page."""
        m = screenview.build(doc(
            elements=[el("btn_left", "Button", [0, 300, 72, 380], "2"),
                      el("btn_go", "Button", [60, 800, 660, 900], "Entrar")],
            statics=[],
        ))
        items = {i["aim"]["rid"]: i for r in m["rows"] for i in r["items"]}
        assert items["btn_left"]["small"] is True
        assert items["btn_go"]["small"] is False


class TestLayoutIntelligence:
    """Learned from the real agency home screen the owner screenshotted."""

    def test_icon_font_glyphs_are_not_words(self):
        """A private-use glyph rendered raw is a tofu box or a stray hamburger
        where the app drew an icon."""
        m = screenview.build(doc(
            elements=[el("btn_info", "TextView", [575, 300, 648, 380],
                         "")],
            statics=[st([100, 300, 200, 380], "")],
        ))
        items = [i for r in m["rows"] for i in r["items"]]
        assert all(not i.get("txt") for i in items)
        assert all(i["kind"] != "label" for i in items)

    def test_a_count_on_the_right_is_a_badge(self):
        """'Mensajes  54' — the count rides in a bubble, not a subtitle."""
        m = screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 500, 720, 700])],
            statics=[st([60, 520, 400, 580], "Mensajes"),
                     st([640, 530, 700, 590], "54")],
        ))
        cell = m["rows"][0]["items"][0]
        assert cell["badge"] == "54"
        assert "54" not in cell["lines"]

    def test_a_number_inside_a_left_icon_is_decoration(self):
        """The day inside the calendar glyph. The date is already in the
        subtitle; repeating '14' as a text line was noise."""
        m = screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 500, 720, 740])],
            statics=[st([23, 530, 131, 640], "14"),
                     st([154, 510, 552, 580], "Horario para hoy"),
                     st([154, 580, 700, 660], "Visitas para 08/14/2026"),
                     st([640, 515, 700, 570], "2")],
        ))
        cell = m["rows"][0]["items"][0]
        assert cell["lines"] == ["Horario para hoy", "Visitas para 08/14/2026"]
        assert cell["badge"] == "2"

    def test_words_are_lines_wherever_they_sit(self):
        m = screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 500, 720, 700])],
            statics=[st([600, 520, 710, 580], "Hoy")],
        ))
        assert m["rows"][0]["items"][0]["lines"] == ["Hoy"]


class TestBandShapes:
    """Row shapes learned from the first flight-recorder discovery session."""

    def test_a_band_of_small_buttons_is_a_keypad_row(self):
        """Mobile Caregiver+ asks for its PIN on a digit grid; left-aligned
        list rows made it usable but not a keypad."""
        m = screenview.build(doc(
            elements=[el("", "Button", [60, 800, 200, 900], "1"),
                      el("", "Button", [280, 800, 420, 900], "2"),
                      el("", "Button", [500, 800, 640, 900], "3")],
            statics=[],
        ))
        assert m["rows"][0].get("keys") is True

    def test_a_bottom_band_of_containers_is_a_tab_bar(self):
        """inMyTeam keeps its own tabs at the bottom; as list cells they
        crammed four labels and four chevrons into one row."""
        m = screenview.build(doc(
            elements=[el("assigned", "FrameLayout", [0, 1450, 180, 1580]),
                      el("open", "FrameLayout", [180, 1450, 360, 1580]),
                      el("chat", "FrameLayout", [360, 1450, 540, 1580]),
                      el("trips", "FrameLayout", [540, 1450, 720, 1580])],
            statics=[st([20, 1470, 160, 1520], "Visits")],
        ))
        tabs = [r for r in m["rows"] if r.get("tabs")]
        assert len(tabs) == 1
        assert len(tabs[0]["items"]) == 4

    def test_an_ordinary_pair_of_wide_buttons_is_neither(self):
        m = screenview.build(doc(
            elements=[el("ok", "Button", [40, 800, 340, 900], "Aceptar"),
                      el("no", "Button", [380, 800, 680, 900], "Cancelar")],
            statics=[],
        ))
        assert not m["rows"][0].get("keys")
        assert not m["rows"][0].get("tabs")

    def test_a_mid_screen_row_of_containers_is_not_a_tab_bar(self):
        m = screenview.build(doc(
            elements=[el("a", "FrameLayout", [0, 500, 240, 620]),
                      el("b", "FrameLayout", [240, 500, 480, 620]),
                      el("c", "FrameLayout", [480, 500, 720, 620])],
            statics=[],
        ))
        assert not any(r.get("tabs") for r in m["rows"])


class TestCurtains:
    """The patient-details page, as reported: a slide-over panel leaves a
    tall sliver of the underlying screen tappable at the left edge. Banding
    goes by vertical overlap, so that one curtain overlapped every real row
    and magnetized the whole page into a single band — rendered as sixteen
    strips of vertically crushed letters. Structure taken from the live
    capture; the text here is fictional."""

    def _model(self):
        return screenview.build(doc(
            elements=[
                el("", "View", [0, 64, 144, 1492]),        # the curtain
                el("", "View", [587, 98, 695, 206]),       # close button
                el("", "View", [198, 848, 684, 986]),      # phone row
                el("", "View", [198, 1196, 684, 1334]),    # address row
            ],
            statics=[
                st([203, 115, 573, 189], "PACIENTE FICTICIA"),
                st([198, 240, 720, 378], "Identificación de admisión"),
                st([198, 378, 476, 447], "XOR-000000"),
                st([198, 476, 371, 545], "Oficina"),
                st([198, 545, 720, 683], "Agencia Ficticia LLC"),
                st([198, 743, 705, 812], "Números de teléfono"),
                st([258, 848, 684, 986], "Teléfono principal"),
                st([198, 1082, 476, 1151], "Direcciones"),
                st([258, 1196, 684, 1334], "Dirección principal"),
            ],
        ))

    def test_a_curtain_does_not_magnetize_the_page_into_one_band(self):
        rows = self._model()["rows"]
        assert len(rows) >= 6

    def test_the_curtain_itself_is_not_drawn(self):
        all_items = [i for r in self._model()["rows"] for i in r["items"]]
        assert not any(i["b"] == [0, 64, 144, 1492] for i in all_items)

    def test_the_real_rows_survive_with_their_labels(self):
        all_items = [i for r in self._model()["rows"] for i in r["items"]]
        texts = []
        for i in all_items:
            texts.extend(i.get("lines") or [])
            if i.get("txt"):
                texts.append(i["txt"])
        assert "Teléfono principal" in texts
        assert "Identificación de admisión" in texts

    def test_a_content_card_with_its_labels_is_not_a_curtain(self):
        """A tall container that actually holds the page's text is content;
        dropping it would drop the page."""
        m = screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 100, 720, 1500])],
            statics=[st([40, 200, 680, 260], "Visita no programada"),
                     st([40, 300, 680, 360], "08:00 PM - 09:00 PM")],
        ))
        all_items = [i for r in m["rows"] for i in r["items"]]
        assert any(i.get("lines") for i in all_items)


class TestStateMarks:
    """The visits list marks each verified EVV record with a drawn check,
    shipped as an icon font's private glyph. Dropping it with the chevrons
    erased the one thing the row exists to say — reported live: 'the front
    end is not capturing the check mark icons'."""

    def test_checks_inside_a_row_become_a_marks_line(self):
        m = screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 500, 720, 900])],
            statics=[st([20, 520, 400, 580], "PACIENTE FICTICIA"),
                     st([420, 530, 700, 580], "08/10/2026"),
                     st([560, 820, 620, 870], ""),
                     st([640, 820, 700, 870], "")],
        ))
        cell = m["rows"][0]["items"][0]
        assert "✓ ✓" in cell["lines"]

    def test_a_loose_check_still_shows(self):
        m = screenview.build(doc(
            elements=[],
            statics=[st([20, 520, 400, 580], "Registro de entrada"),
                     st([560, 520, 620, 580], "")],
        ))
        all_items = [i for r in m["rows"] for i in r["items"]]
        assert any(i.get("txt") == "✓" for i in all_items)

    def test_decoration_glyphs_are_still_dropped(self):
        """Chevrons, hamburgers, user icons: still decoration, still gone."""
        m = screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 500, 720, 700])],
            statics=[st([20, 520, 400, 580], "Detalles"),
                     st([650, 530, 700, 580], "")],   # chevron
        ))
        cell = m["rows"][0]["items"][0]
        assert cell["lines"] == ["Detalles"]


class TestImageMarks:
    """The legacy visits list draws its EVV checks as ImageViews — no text
    at all, identity in the resource-id (imgStartTime/imgEndTime), present
    only on rows whose record is confirmed. Verified against the live
    hierarchy after the glyph fix alone changed nothing on this screen."""

    def test_named_check_images_become_marks(self):
        m = screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 200, 720, 640])],
            statics=[st([20, 240, 400, 300], "PACIENTE FICTICIA"),
                     {"cls": "ImageView", "b": [469, 304, 503, 338],
                      "txt": "", "rid": "imgStartTime"},
                     {"cls": "ImageView", "b": [639, 304, 673, 338],
                      "txt": "", "rid": "imgEndTime"}],
        ))
        cell = m["rows"][0]["items"][0]
        assert "✓ ✓" in cell["lines"]

    def test_an_anonymous_image_is_still_decoration(self):
        m = screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 200, 720, 640])],
            statics=[st([20, 240, 400, 300], "Detalles"),
                     {"cls": "ImageView", "b": [469, 304, 503, 338],
                      "txt": "", "rid": "ivDecorativeLogo"}],
        ))
        cell = m["rows"][0]["items"][0]
        assert cell["lines"] == ["Detalles"]
