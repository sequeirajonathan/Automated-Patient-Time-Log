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
            elements=[el("btn_menu", "TextView", [575, 300, 648, 380],
                         "")],
            statics=[st([100, 300, 200, 380], "")],
        ))
        items = [i for r in m["rows"] for i in r["items"]]
        assert all(not i.get("txt") for i in items)
        assert all(i["kind"] != "label" for i in items)

    def test_a_known_button_glyph_keeps_its_meaning(self):
        """The agency picker's info button is drawn as  — stripped to
        an empty box it was unfindable; translated it reads as itself. A
        loose static with a mere decoration glyph stays dropped."""
        m = screenview.build(doc(
            elements=[el("btn_info", "TextView", [575, 800, 648, 880],
                         "")],
            statics=[st([100, 800, 160, 880], "")],
        ))
        items = [i for r in m["rows"] for i in r["items"]]
        assert any(i.get("txt") == "?" and i.get("aim") for i in items)
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
        # The app's tab bar is lifted out of the list to ride the control
        # bar (apptabs), never buried as a row in the body.
        assert not any(r.get("tabs") for r in m["rows"])
        assert len(m["apptabs"]) == 4

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
        assert [mk["sym"] for mk in cell["marks"]] == ["✓", "✓"]
        assert all(mk["tone"] == "ok" for mk in cell["marks"])

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
        assert [mk["sym"] for mk in cell["marks"]] == ["✓", "✓"]

    def test_an_anonymous_image_is_still_decoration(self):
        m = screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 200, 720, 640])],
            statics=[st([20, 240, 400, 300], "Detalles"),
                     {"cls": "ImageView", "b": [469, 304, 503, 338],
                      "txt": "", "rid": "ivDecorativeLogo"}],
        ))
        cell = m["rows"][0]["items"][0]
        assert cell["lines"] == ["Detalles"]


class TestProseVsHeadings:
    """The migration pitch rendered as a wall of shouting: whole paragraphs
    wore the uppercase section-header treatment, and orphaned list dots
    flexed into gap-rows of their own."""

    def test_a_short_loose_label_is_a_heading_and_prose_is_not(self):
        m = screenview.build(doc(
            elements=[],
            statics=[st([20, 100, 400, 160], "Esto es lo que debe hacer:"),
                     st([20, 200, 700, 400],
                        "La aplicación es nueva y más fácil de usar. La "
                        "configuración solo tarda unos minutos en total.")],
        ))
        labels = [i for r in m["rows"] for i in r["items"]
                  if i["kind"] == "label"]
        assert labels[0]["header"] is True
        assert labels[1]["header"] is False

    def test_orphaned_bullets_are_dropped(self):
        m = screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 500, 720, 640])],
            statics=[st([30, 520, 60, 570], "•"),
                     st([80, 520, 700, 570], "Toque Iniciar configuración"),
                     st([30, 600, 60, 630], "•")],
        ))
        all_txt = []
        for r in m["rows"]:
            for i in r["items"]:
                all_txt.extend(i.get("lines") or [])
                if i.get("txt"):
                    all_txt.append(i["txt"])
        assert "•" not in all_txt
        assert "Toque Iniciar configuración" in all_txt


class TestSectionHeaders:
    """Day dividers on a scrolled-and-stitched schedule ('agosto 18',
    '19', '20') landed a few pixels apart and banded into one boxed row,
    losing the header treatment. Each heading gets its own line — but a
    short label sitting WITH controls is not a lone heading."""

    def test_headers_that_band_are_split_to_their_own_rows(self):
        m = screenview.build(doc(
            elements=[],
            statics=[st([30, 600, 400, 650], "agosto 18, 2026"),
                     st([30, 640, 400, 690], "agosto 19, 2026"),
                     st([30, 680, 400, 730], "agosto 20, 2026")],
        ))
        header_rows = [r for r in m["rows"]
                       if len(r["items"]) == 1 and r["items"][0].get("header")]
        texts = [r["items"][0]["txt"] for r in header_rows]
        assert texts == ["agosto 18, 2026", "agosto 19, 2026",
                         "agosto 20, 2026"]

    def test_a_short_label_with_controls_stays_on_its_row(self):
        """The care-plan row's label is short (header by length) but belongs
        beside its ✓/✗ — an all-header split must not tear it away."""
        m = screenview.build(doc(
            elements=[el("chk_yes", "RadioButton", [640, 610, 690, 660], "✓"),
                      el("chk_no", "RadioButton", [692, 610, 720, 660], "✗")],
            statics=[st([30, 615, 600, 655], "127 - Toilet Use")],
        ))
        row = m["rows"][0]["items"]
        kinds = [i["kind"] for i in row]
        assert "label" in kinds and "segment" in kinds


class TestCallsToAction:
    """A call to action ('Continuar visitando') is filled and centred, not
    one more grey nav row with a chevron — the difference the owner missed
    where every action looked the same."""

    def test_an_action_row_is_flagged_cta(self):
        m = screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 500, 720, 600])],
            statics=[st([200, 520, 520, 580], "Continuar visitando")],
        ))
        cell = m["rows"][0]["items"][0]
        assert cell["cta"] is True

    def test_a_data_row_is_not_a_cta(self):
        m = screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 500, 720, 600])],
            statics=[st([20, 520, 520, 580], "Detalles del paciente")],
        ))
        cell = m["rows"][0]["items"][0]
        assert not cell.get("cta")

    def test_a_login_button_is_a_cta(self):
        m = screenview.build(doc(
            elements=[el("btn", "Button", [40, 800, 680, 890], "Iniciar sesión")],
            statics=[],
        ))
        assert m["rows"][0]["items"][0]["cta"] is True


class TestFieldPairs:
    """A caption stacked a pixel above its value is one field, not two
    floating headers with a wall of space between them (the patient
    detail's 'Identificación de admisión' / 'XOR-900196')."""

    def test_caption_and_value_pair_into_a_field(self):
        m = screenview.build(doc(
            elements=[],
            statics=[st([160, 115, 500, 131], "Identificación de admisión"),
                     st([160, 131, 500, 147], "XOR-900196")],
        ))
        items = [i for r in m["rows"] for i in r["items"]]
        kv = [i for i in items if i["kind"] == "kv"]
        assert kv and kv[0]["caption"] == "Identificación de admisión"
        assert kv[0]["value"] == "XOR-900196"

    def test_a_caption_over_a_card_stays_a_heading(self):
        """Its value is a tappable row, folded into a container — the
        caption introduces a section, it is not half of a field."""
        m = screenview.build(doc(
            elements=[el("", "RelativeLayout", [160, 226, 709, 290])],
            statics=[st([160, 207, 500, 223], "Números de teléfono"),
                     st([175, 240, 500, 270], "Teléfono principal")],
        ))
        items = [i for r in m["rows"] for i in r["items"]]
        assert any(i["kind"] == "label" and i.get("header")
                   and i["txt"] == "Números de teléfono" for i in items)
        assert not any(i["kind"] == "kv" for i in items)


class TestAppTabsToControlBar:
    """The app's own bottom tab bar is lifted out of the list to ride the
    portal's control bar — always reachable, the way the native app keeps
    it. The selected tab (no clickable container in Compose) survives as
    the lit 'current' slot; the rest carry a tap aim."""

    def test_a_compose_bar_keeps_the_selected_tab(self):
        m = screenview.build(doc(
            elements=[el("pac", "View", [246, 1508, 478, 1580]),
                      el("menu", "View", [483, 1508, 720, 1580])],
            statics=[st([40, 1520, 200, 1560], "Programación"),
                     st([300, 1520, 440, 1560], "Pacientes"),
                     st([560, 1520, 660, 1560], "Menú")],
        ))
        tabs = m["apptabs"]
        assert [t["txt"] for t in tabs] == ["Programación", "Pacientes", "Menú"]
        assert tabs[0]["current"] is True and tabs[0]["aim"] is None
        assert tabs[1]["aim"] and tabs[2]["aim"]
        # And it is not left duplicated in the body.
        assert not any(t["txt"] == "Programación"
                       for r in m["rows"] for t in r["items"])


class TestInformationalRows:
    """The expanded visit card's status lines are statements, not doors.

    Compose marks '✓ Registros de entrada de EVV 6:00' clickable, and the
    reflow drew it as one more chevroned cell — which made the whole card
    look like every element kept expanding (reported live). A row that hugs
    its content and carries a single status line renders as an info line:
    the check keeps its colour, the chevron and the button dress go. The
    card's one real call to action, 'Ver detalles', is drawn by the app as
    its filled button and earns the same here."""

    def _model(self):
        return screenview.build(doc(
            elements=[
                el("", "View", [25, 583, 700, 615]),    # card header row
                el("", "View", [25, 626, 148, 658]),    # "Detalles del paciente"
                el("", "View", [25, 658, 261, 690]),    # EVV check row
                el("", "View", [28, 727, 699, 759]),    # "Ver detalles"
            ],
            statics=[
                st([25, 583, 126, 599], "LUCRESIA L PUPO"),
                st([25, 599, 125, 615], "6:00 a. m. - 9:00 a. m."),
                st([25, 634, 33, 646], ""),       # person icon
                st([36, 634, 148, 650], "Detalles del paciente"),
                st([33, 669, 42, 680], ""),       # drawn check
                st([45, 666, 253, 682], "Registros de entrada de EVV 6:00 a. m."),
                st([333, 736, 395, 750], "Ver detalles"),
            ],
        ))

    def _items(self):
        return [i for r in self._model()["rows"] for i in r["items"]]

    def test_a_status_line_is_informational_in_its_marks_tone(self):
        evv = next(i for i in self._items()
                   if "Registros de entrada" in " ".join(i.get("lines", [])))
        assert evv["info"] is True
        assert evv["tone"] == "ok"
        assert [m["sym"] for m in evv["marks"]] == ["✓"]

    def test_a_bare_caption_row_is_informational_without_a_tone(self):
        cap = next(i for i in self._items()
                   if "Detalles del paciente" in " ".join(i.get("lines", [])))
        assert cap["info"] is True and cap["tone"] == ""

    def test_the_full_width_card_header_stays_a_cell(self):
        head = next(i for i in self._items()
                    if "LUCRESIA L PUPO" in " ".join(i.get("lines", [])))
        assert not head.get("info")

    def test_ver_detalles_is_the_cards_call_to_action(self):
        cta = next(i for i in self._items()
                   if "Ver detalles" in " ".join(i.get("lines", [])))
        assert cta["cta"] is True and not cta.get("info")

    def test_a_mid_screen_utility_is_not_dressed_as_information(self):
        """The schedule's search ("Visita Buscar") is a narrow tappable
        floating mid-screen. Statements start at the left content margin;
        this is a control, and hiding its tap behind an info dress would
        cost a real feature."""
        m = screenview.build(doc(
            elements=[el("schedule_screen_visit_search", "View",
                         [419, 506, 484, 538])],
            statics=[st([419, 515, 484, 529], "Visita Buscar")],
        ))
        cell = next(i for r in m["rows"] for i in r["items"]
                    if i["kind"] == "row")
        assert not cell.get("info")

    def test_a_narrow_two_line_row_is_still_a_cell(self):
        """Two stacked lines are a data cell whatever its width — only a
        single status line reads as a statement."""
        m = screenview.build(doc(
            elements=[el("", "View", [25, 583, 400, 655])],
            statics=[st([25, 591, 126, 607], "LUCRESIA L PUPO"),
                     st([25, 607, 125, 623], "6:00 a. m. - 9:00 a. m.")],
        ))
        cell = next(i for r in m["rows"] for i in r["items"]
                    if i["kind"] == "row")
        assert not cell.get("info")


class TestTabBaseline:
    """The bar's captions share one baseline. At density 84 the schedule's
    last card runs to within a few pixels of the pinned bar, and its own
    short lines were consumed as extra tabs ('Detalles del paciente'
    riding next to 'Programación'). Only the bottom-most aligned run of
    two or more captions is the bar."""

    def test_content_hugging_the_bar_is_not_a_tab(self):
        m = screenview.build(doc(
            elements=[el("pac", "View", [246, 1508, 478, 1580]),
                      el("menu", "View", [483, 1508, 720, 1580])],
            statics=[st([36, 1521, 148, 1537], "Detalles del paciente"),
                     st([592, 1521, 697, 1537], "Detalles de la visita"),
                     st([40, 1542, 200, 1553], "Programación"),
                     st([300, 1542, 440, 1553], "Pacientes"),
                     st([560, 1542, 660, 1553], "Menú")],
        ))
        tabs = m["apptabs"]
        assert [t["txt"] for t in tabs] == ["Programación", "Pacientes", "Menú"]
        body = [t for r in m["rows"] for i in r["items"]
                for t in ([i.get("txt")] + (i.get("lines") or [])) if t]
        assert any("Detalles del paciente" in t for t in body)


class TestDemotedActions:
    """"Iniciar visita no programada" starts the exception workflow —
    creating an UNSCHEDULED visit — and the app draws it as small plain
    text while "Ver detalles" gets the filled pill (checked against the
    phone's own pixels). The portal must not out-shout the app on a
    record-creating path: it renders as an ordinary row, still tappable,
    never dressed as the page's primary action."""

    def test_the_unscheduled_visit_entry_is_not_a_cta(self):
        m = screenview.build(doc(
            elements=[el("schedule_screen_create_unscheduled_visit", "View",
                         [14, 890, 706, 935])],
            statics=[st([300, 900, 421, 925], "Iniciar visita no programada")],
        ))
        cell = next(i for r in m["rows"] for i in r["items"]
                    if i["kind"] == "row")
        assert not cell.get("cta")
        assert cell["aim"]["rid"] == "schedule_screen_create_unscheduled_visit"

    def test_a_plain_start_visit_button_keeps_its_weight(self):
        m = screenview.build(doc(
            elements=[el("btn_start", "Button", [60, 800, 660, 900],
                         "Iniciar visita")],
            statics=[],
        ))
        btn = next(i for r in m["rows"] for i in r["items"]
                   if i["kind"] == "button")
        assert btn["cta"] is True
