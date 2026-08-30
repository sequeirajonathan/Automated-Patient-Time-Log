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
        """The title and the app's real controls come through — but its own
        BACK does not, and that is deliberate.

        The pill already has a Back that sends the phone's own Back, which is
        what "Atrás" does. Publishing both put two controls with one meaning
        three inches apart in different words, and it was reported from the
        field as exactly that confusion. `btn_add` still rides."""
        m = screenview.build(doc(
            elements=[el("btn_back", "Button", [0, 20, 120, 120], "Atrás"),
                      el("btn_add", "Button", [640, 20, 720, 120], "+")],
            statics=[st([200, 30, 560, 110], "Detalle de Visita")],
        ))
        assert m["nav"] is not None
        assert m["nav"]["title"] == "Detalle de Visita"
        kept = ([m["nav"]["back"]] if m["nav"]["back"] else []) \
            + m["nav"]["trailing"]
        assert [b["aim"]["rid"] for b in kept] == ["btn_add"]

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


class TestVisitDetailsScreen:
    """The Ver detalles page, reflowed from its live capture: a nav bar
    with the app's own back control, EVV chips with no containers, an
    avatar chip of initials, and tall icon tiles that are buttons."""

    def test_the_back_button_survives_and_names_the_nav(self):
        """The back control is a 25px sliver whose only content is the
        drawn arrow: the glyph names it, the resource id spares it from
        the sliver rule, and the band reads as a nav bar."""
        m = screenview.build(doc(
            elements=[el("menu_top_bar_back_button", "View", [6, 68, 31, 93]),
                      el("help_button", "View", [691, 68, 718, 94])],
            statics=[st([12, 73, 25, 89], "\uf060"),
                     st([321, 73, 399, 88], "agosto 17, 2026"),
                     st([697, 73, 712, 89], "\uf059")],
        ))
        assert m["nav"] is not None
        assert m["nav"]["title"] == "agosto 17, 2026"
        assert m["nav"]["back"]["aim"]["rid"] == "menu_top_bar_back_button"
        assert m["nav"]["back"]["txt"] == "\u2190"

    def test_containerless_marks_pair_with_their_sentence(self):
        """The details page draws its EVV chips with no container at all;
        a loose check a line above loose prose read as debris. Side by
        side they are one status line."""
        m = screenview.build(doc(
            elements=[],
            statics=[st([276, 595, 283, 604], "\uf00c"),
                     st([285, 593, 445, 606], "Registros de entrada de EVV")],
        ))
        chip = next(i for r in m["rows"] for i in r["items"]
                    if i["kind"] == "row")
        assert chip["info"] is True and chip["tone"] == "ok"
        assert chip["lines"] == ["Registros de entrada de EVV"]

    def test_avatar_initials_are_decoration(self):
        m = screenview.build(doc(
            elements=[],
            statics=[st([16, 557, 43, 586], "LP"),
                     st([59, 549, 143, 562], "LUCRESIA L PUPO")],
        ))
        texts = [i.get("txt") for r in m["rows"] for i in r["items"]]
        assert "LUCRESIA L PUPO" in texts and "LP" not in texts

    def test_a_tall_icon_tile_is_a_button_not_information(self):
        """"Funciones" is narrow, left-anchored and single-captioned like
        a status line — but it is an 80px icon tile, a real button, and
        an info dress would hide its tap."""
        m = screenview.build(doc(
            elements=[el("", "View", [4, 718, 320, 798])],
            statics=[st([120, 770, 200, 786], "Funciones")],
        ))
        tile = next(i for r in m["rows"] for i in r["items"]
                    if i["kind"] == "row")
        assert not tile.get("info")
        assert tile["aim"]


class TestWrapperContainers:
    """The inMyTeam sign-in hangs one clickable View over the whole page.
    Letting it fold labels swallowed the title, the field's caption and
    the submit's text into one giant "button" whose first line started
    with "Sign in" — a full-page CTA — while the real control rendered
    empty. A tall container WITH other interactive elements inside it is
    scaffolding and folds nothing."""

    def test_a_full_page_wrapper_folds_nothing(self):
        m = screenview.build(doc(
            elements=[el("", "View", [0, 64, 720, 1579]),
                      el("", "EditText", [11, 992, 709, 1026]),
                      el("", "View", [11, 1279, 709, 1321])],
            statics=[st([0, 317, 720, 381], "Sign in with your phone number"),
                     st([38, 1004, 150, 1017], "Enter your cell phone number"),
                     st([334, 1294, 362, 1306], "Sign in")],
        ))
        items = [i for r in m["rows"] for i in r["items"]]
        ctas = [i for i in items if i.get("cta")]
        assert len(ctas) == 1
        assert ctas[0]["lines"] == ["Sign in"]
        texts = [i.get("txt") for i in items if i["kind"] == "label"]
        assert "Sign in with your phone number" in texts

    def test_the_welcome_pages_get_started_is_the_call_to_action(self):
        """inMyTeam's initial page offers exactly one action, shipped with
        the curly apostrophe the word list is not written in."""
        m = screenview.build(doc(
            elements=[el("", "View", [30, 1531, 690, 1573])],
            statics=[st([314, 1546, 382, 1558], "Let\u2019s Get Started")],
        ))
        cell = next(i for r in m["rows"] for i in r["items"]
                    if i["kind"] == "row")
        assert cell["cta"] is True

    def test_a_folded_avatar_stub_is_not_the_first_line(self):
        """The visits list folds the agency chip's initial in as the row's
        first line ("H" ahead of the agency name) — decoration, dropped."""
        m = screenview.build(doc(
            elements=[el("", "View", [11, 194, 709, 257])],
            statics=[st([30, 205, 40, 222], "H"),
                     st([56, 200, 300, 216], "HOME CARE ON CALL, LLC"),
                     st([56, 218, 200, 232], "05:00 AM | 06:00 AM"),
                     st([56, 236, 200, 252], "Carmen Villalon")],
        ))
        card = next(i for r in m["rows"] for i in r["items"]
                    if i["kind"] == "row")
        assert card["lines"][0] == "HOME CARE ON CALL, LLC"
        assert "H" not in card["lines"]


class TestVisitDetailActions:
    """inMyTeam's visit detail carries its commit actions in a two-item
    bottom bar. Two aligned captions are a page's action pair, not a tab
    bar — "Check in" must never ride the control bar dressed as
    navigation. And its section switch ("Visits | Plan of care") is two
    side-by-side rows: controls, not statements."""

    def _model(self):
        return screenview.build(doc(
            elements=[el("", "View", [15, 483, 360, 508]),
                      el("", "View", [360, 483, 705, 508]),
                      el("", "View", [186, 1527, 267, 1559]),
                      el("", "View", [453, 1527, 534, 1559])],
            statics=[st([100, 489, 160, 504], "Visits"),
                     st([400, 489, 500, 504], "Plan of care"),
                     st([207, 1537, 246, 1549], "Check in"),
                     st([472, 1533, 516, 1553], "Note & Check out")],
        ))

    def test_two_bottom_captions_are_not_a_tab_bar(self):
        m = self._model()
        assert m["apptabs"] == []
        texts = [t for r in m["rows"] for i in r["items"]
                 for t in ([i.get("txt")] + (i.get("lines") or [])) if t]
        assert "Check in" in texts and "Note & Check out" in texts

    def test_side_by_side_rows_are_never_information(self):
        m = self._model()
        rows = [i for r in m["rows"] for i in r["items"] if i["kind"] == "row"]
        assert rows and all(not i.get("info") for i in rows)

    def test_a_centred_avatar_above_the_name_is_decoration(self):
        m = screenview.build(doc(
            elements=[],
            statics=[st([356, 508, 366, 525], "H"),
                     st([291, 537, 430, 552], "HOME CARE ON CALL, LLC")],
        ))
        texts = [i.get("txt") for r in m["rows"] for i in r["items"]]
        assert "HOME CARE ON CALL, LLC" in texts and "H" not in texts


class TestCarePlanListRuns:
    """The visit detail's care plan, as the first field test rendered it:
    fourteen short task lines, every one 'caption shaped', all shouting in
    small-caps header dress. Three or more loose labels sharing a left
    edge and a rhythm are the app's list — body lines, not headings."""

    TASKS = ["106 - Hair Care-Shampoo", "127 - Toilet Use", "134 - Bathing",
             "138 - Laundry", "141 - Incontinence Care"]

    def _labels(self, m):
        return [i for r in m["rows"] for i in r["items"]
                if i["kind"] == "label"]

    def test_a_run_of_aligned_lines_is_a_list_not_headings(self):
        m = screenview.build(doc(
            elements=[],
            statics=[st([5, 224 + 21 * k, 710, 234 + 21 * k], t)
                     for k, t in enumerate(self.TASKS)],
        ))
        labels = self._labels(m)
        assert [i["txt"] for i in labels] == self.TASKS
        assert not any(i.get("header") for i in labels)

    def test_a_lone_short_label_is_still_a_heading(self):
        m = screenview.build(doc(
            elements=[],
            statics=[st([5, 300, 200, 316], "Horario")],
        ))
        (label,) = self._labels(m)
        assert label["header"] is True

    def test_two_spread_labels_are_not_a_run(self):
        m = screenview.build(doc(
            elements=[],
            statics=[st([5, 300, 200, 316], "Horario"),
                     st([5, 800, 200, 816], "Mensajes")],
        ))
        assert all(i["header"] for i in self._labels(m))


class TestDeadTabSlot:
    """The visit detail's tab strip keeps a wide empty third slot, and it
    rendered as a nameless full-width '···' button in the tab bar. A wide
    empty button beside labelled ones is dead space; a small empty button
    is still an icon and stays."""

    def test_the_wide_empty_slot_is_dropped(self):
        m = screenview.build(doc(
            elements=[el("", "Button", [0, 590, 240, 622]),
                      el("", "Button", [240, 590, 480, 622],
                         "Registrar Entrada/Salida"),
                      el("", "Button", [480, 590, 720, 622], "Direcciones")],
            statics=[],
        ))
        buttons = [i for r in m["rows"] for i in r["items"]
                   if i["kind"] == "button"]
        assert [b["txt"] for b in buttons] == ["Registrar Entrada/Salida",
                                               "Direcciones"]

    def test_a_small_icon_button_survives_beside_labelled_ones(self):
        m = screenview.build(doc(
            elements=[el("btn_x", "Button", [660, 590, 700, 622]),
                      el("", "Button", [10, 590, 300, 622], "Guardar"),
                      el("", "Button", [310, 590, 600, 622], "Cancelar")],
            statics=[],
        ))
        buttons = [i for r in m["rows"] for i in r["items"]
                   if i["kind"] == "button"]
        assert len(buttons) == 3


class TestNavTitleLength:
    """'Detalle de Visita CARIDAD ROJAS BATISTA' is 39 characters, and the
    heading cap demoted the whole title bar to a boxed row. A nav title
    runs longer than a section heading — but a hint sentence still must
    not rename the page."""

    def test_a_page_plus_patient_title_is_still_a_nav(self):
        m = screenview.build(doc(
            elements=[el("btn_left", "Button", [0, 67, 42, 87], "Atrás")],
            statics=[st([42, 65, 684, 89],
                        "Detalle de Visita CARIDAD ROJAS BATISTA")],
        ))
        assert m["nav"] is not None
        assert m["nav"]["title"].startswith("Detalle de Visita")

    def test_a_hint_sentence_does_not_become_the_title(self):
        m = screenview.build(doc(
            elements=[el("btn_left", "Button", [0, 67, 42, 87], "Atrás")],
            statics=[st([42, 65, 684, 89],
                        "Encontrará las visitas anteriores con Visita "
                        "Buscar")],
        ))
        assert m["nav"] is None


class TestTheTitleIsAName:
    """What may sit in the header, which is the one string somebody reads
    without looking for it.

    The schedule renamed itself **"Logotipo de H H AeXchange +"** — the logo
    ImageView's alt text, the brand spelled out letter by letter for a screen
    reader — because it was the longest thing in the title bar. Two guards
    now: a picture's description never becomes words at all (see
    feed.statics), and a title that reads like spelling is rejected here.
    Rejected leaves the title empty, and the client falls through to the app's
    own name, which is always true.
    """

    def test_a_spelled_out_brand_is_not_a_title(self):
        m = screenview.build(doc(
            elements=[el("btn_left", "Button", [0, 67, 42, 87], "Atrás")],
            statics=[st([42, 65, 684, 89], "Logotipo de H H AeXchange +")],
        ))
        assert m["nav"] is not None            # it is still a nav bar
        assert m["nav"]["title"] == ""         # with nothing worth saying

    def test_the_real_title_wins_over_the_logo_beside_it(self):
        """A title bar usually carries both, and the logo was winning on
        length — which is exactly how the page got renamed."""
        m = screenview.build(doc(
            elements=[el("btn_left", "Button", [0, 67, 42, 87], "Atrás")],
            statics=[st([42, 65, 300, 89], "Logotipo de H H AeXchange +"),
                     st([310, 65, 600, 89], "Horario")],
        ))
        assert m["nav"]["title"] == "Horario"

    def test_an_icon_glyph_is_not_a_title(self):
        m = screenview.build(doc(
            elements=[el("btn_left", "Button", [0, 67, 42, 87], "Atrás")],
            statics=[st([42, 65, 684, 89], "")],
        ))
        assert m["nav"]["title"] == ""

    def test_initials_a_person_actually_wrote_are_left_alone(self):
        """The guard is for spelling, not for short words. A patient's name
        on a visit-detail title is the whole point of that title."""
        assert screenview._is_spelled_out("Detalle de Visita") is False
        assert screenview._is_spelled_out("CARIDAD ROJAS BATISTA") is False
        assert screenview._is_spelled_out("A B Smith") is False
        assert screenview._is_honest_title("A B Smith") is True


class TestAccessibilityAnnotationsAreNotWords:
    """"Expandido" under every patient's name.

    The HHAeXchange+ schedule puts the chevron's state — "Contraído" /
    "Expandido" — in a five-pixel square beside each visit. It is what a
    screen reader says about the arrow, not anything the phone shows, and
    rendered as text it printed a mystery word under each patient.

    The signal is that the box cannot possibly be showing the word. Numbers
    below are the real ones, read off the live schedule at density 84.
    """

    W = 720

    def test_a_word_in_a_five_pixel_box_is_not_being_shown(self):
        assert screenview._is_annotation("Contraído", [700, 160, 705, 169],
                                         self.W) is True

    def test_every_real_string_on_that_screen_survives(self):
        for txt, b in [("Visita Buscar", [407, 105, 459, 116]),
                       ("LUCRESIA L PUPO", [20, 158, 104, 171]),
                       ("agosto 18, 2026 (Hoy)", [7, 130, 108, 143]),
                       ("Ayuda", [691, 69, 718, 93]),
                       ("Programación", [94, 1553, 143, 1562]),
                       ("Pacientes", [343, 1553, 378, 1562]),
                       ("Menú", [591, 1553, 611, 1562])]:
            assert screenview._is_annotation(txt, b, self.W) is False, txt

    def test_the_tightest_real_caption_anybody_has_seen_survives(self):
        """Mobile Caregiver+ draws its tab captions in boxes tighter than
        their own text — "Visitas" at 2.43px per character against the
        annotation's 0.56. The first threshold sat between the wrong pair of
        numbers and swallowed it; the tab-bar tests caught that."""
        assert screenview._is_annotation("Visitas", [262, 1556, 279, 1565],
                                         self.W) is False

    def test_a_mark_in_an_equally_narrow_box_is_untouched(self):
        """The EVV check's own box is this narrow. A mark is one character;
        an annotation is a word, which is what the minimum length is for."""
        assert screenview._is_annotation("", [686, 160, 693, 169],
                                         self.W) is False
        assert screenview._is_annotation("54", [700, 160, 705, 169],
                                         self.W) is False

    def test_the_visit_row_reads_as_the_phone_shows_it(self):
        m = screenview.build(doc(
            elements=[el("", "View", [20, 158, 705, 184])],
            statics=[st([20, 158, 104, 171], "LUCRESIA L PUPO"),
                     st([20, 171, 100, 184], "6:00 a. m. - 9:00 a. m."),
                     st([700, 160, 705, 169], "Contraído")],
        ))
        cell = m["rows"][0]["items"][0]
        assert cell["lines"] == ["LUCRESIA L PUPO", "6:00 a. m. - 9:00 a. m."]


class TestMapOverlay:
    """The GPS confirm slides a full-screen map OVER the visit page and
    the tree still reports the page beneath — the portal rendered both at
    once. Document order is z-order: an anonymous full-screen container
    arriving AFTER content it covers is an overlay, and what it covers is
    invisible on the phone. A page's own wrapper arrives BEFORE its
    children and is no overlay."""

    def _model(self):
        return screenview.build(doc(
            elements=[
                el("btn_clock_out", "Button", [365, 148, 715, 174],
                   "Registrar Salida"),
                el("", "FrameLayout", [0, 128, 720, 1501]),
                el("btn_confirm", "Button", [312, 1529, 407, 1555],
                   "Confirmar"),
            ],
            statics=[st([5, 224, 710, 234], "106 - Hair Care-Shampoo"),
                     st([620, 1492, 720, 1501], "©2026 Google"),
                     st([0, 1506, 720, 1519],
                        "Se encuentra a 47 metros del paciente")],
        ))

    def test_covered_content_disappears(self):
        texts = [(i.get("txt") or "") for r in self._model()["rows"]
                 for i in r["items"]]
        assert "Registrar Salida" not in texts
        assert "106 - Hair Care-Shampoo" not in texts
        assert "©2026 Google" not in texts

    def test_the_overlays_own_strip_survives(self):
        texts = [(i.get("txt") or "") for r in self._model()["rows"]
                 for i in r["items"]]
        assert "Confirmar" in texts
        assert any("47 metros" in t for t in texts)

    def test_a_pages_own_wrapper_is_not_an_overlay(self):
        m = screenview.build(doc(
            elements=[
                el("", "FrameLayout", [0, 90, 720, 1568]),
                el("go", "Button", [10, 600, 700, 660], "Entrar"),
            ],
            statics=[st([10, 300, 700, 340], "Bienvenida")],
        ))
        texts = [(i.get("txt") or "") for r in m["rows"] for i in r["items"]]
        assert "Entrar" in texts
        assert "Bienvenida" in texts

    def test_a_named_full_screen_container_is_a_control_not_an_overlay(self):
        m = screenview.build(doc(
            elements=[
                el("btn_borrar", "Button", [4, 1480, 22, 1515], "Borrar"),
                el("gestureSignature", "FrameLayout", [28, 90, 700, 1560]),
            ],
            statics=[],
        ))
        texts = [(i.get("txt") or "") for r in m["rows"] for i in r["items"]]
        assert "Borrar" in texts


class TestGeneratedDescriptions:
    """Mobile Caregiver+'s visits list is custom-drawn: its only words are
    the sentence a screen reader would say. Rendered whole, three of those
    are a wall of prose where the phone shows three scannable rows."""

    SENTENCE = ("La visita está programada para ATANASIO MEDEROS TORRIEN en "
                "martes, 18 de agosto de 2026 de 9:05 AM a 11:05 AM y su "
                "estado es No Empezadas, Tarde")

    def test_the_sentence_becomes_a_list_cell(self):
        assert screenview._shape_description(self.SENTENCE) == [
            "ATANASIO MEDEROS TORRIEN", "9:05 AM – 11:05 AM",
            "No Empezadas, Tarde"]

    def test_english_reads_the_same_way(self):
        english = ("The visit is scheduled for MARINA ZALDIVAR MARTI on "
                   "Tuesday, August 18 2026 from 3:20 PM to 5:20 PM and its "
                   "status is Not started")
        assert screenview._shape_description(english) == [
            "MARINA ZALDIVAR MARTI", "3:20 PM – 5:20 PM", "Not started"]

    def test_a_sentence_it_cannot_account_for_is_left_alone(self):
        """A rule that drops half a sentence it did not understand is worse
        than a long line."""
        for text in (
            "La visita está programada para ATANASIO MEDEROS TORRIEN en "
            "martes, 18 de agosto de 2026",                    # no status
            "La visita de las 9:05 AM a 11:05 AM y su estado es Sin empezar",
            "Su sesión ha caducado, por favor vuelva a iniciar sesión.",
        ):
            assert screenview._shape_description(text) is None

    def test_a_short_label_is_never_reshaped(self):
        assert screenview._shape_description("Sin empezar") is None

    def test_the_list_form_still_drops_the_date(self):
        """A list states the day once as a section header; repeating it on
        every row is noise. The parts underneath keep it — see
        TestTheVisitDetailIsNotAParagraph."""
        assert screenview._describe(self.SENTENCE)["when"] == \
            "martes, 18 de agosto de 2026"
        assert "agosto" not in " ".join(
            screenview._shape_description(self.SENTENCE))


    def test_the_row_renders_as_title_and_details(self):
        m = screenview.build(doc(
            elements=[el("", "View", [0, 146, 720, 185])],
            statics=[st([0, 146, 720, 185], self.SENTENCE)],
        ))
        (cell,) = [i for r in m["rows"] for i in r["items"]
                   if i["kind"] == "row"]
        assert cell["lines"][0] == "ATANASIO MEDEROS TORRIEN"
        assert cell["lines"][1] == "9:05 AM – 11:05 AM"


class TestTheVisitDetailIsNotAParagraph:
    """Mobile Caregiver+'s completed visit, read off the phone 2026-08-29.
    Bounds verbatim; the patient's name replaced.

    Reported as the layout being "really bad", and it was: the page opened
    with three lines of grey prose — the patient's name in square brackets
    mid-clause, the window buried after it, and the visit's state, the one
    word the page exists to report, last in the run and in the same grey as
    the rest. The shaping that fixes exactly this had existed for a while
    and only ever ran inside a tappable row.
    """

    SENTENCE = ("La visita está programada para [ROSA EJEMPLO PRUEBA] en "
                "sábado, 29 de agosto de 2026 de 9:10 AM a 12:10 PM y su "
                "estado es Completada")

    def _model(self):
        return screenview.build(doc(
            size=(1080, 2340),
            elements=[
                el("", "android.view.View", [30, 605, 1050, 665]),
                el("", "android.view.View", [30, 653, 288, 713]),
            ],
            statics=[
                st([30, 211, 1050, 359], self.SENTENCE),
                st([30, 380, 468, 418], "Hora de inicio real - 9:10:13 AM"),
                st([30, 526, 60, 556], "Servicio Completada"),
                st([60, 522, 1050, 560], "T1019 (Personal care ser per 15 min)"),
                st([30, 616, 68, 653], "Dirección"),
                st([73, 616, 877, 654],
                   "350 NE 141ST ST APT 202, NORTH MIAMI, FL 331612852"),
                st([30, 664, 68, 702], "Número de teléfono"),
                st([73, 664, 288, 702], "(786) 352-1207"),
            ],
        ))

    def _only(self, kind):
        return [i for r in self._model()["rows"] for i in r["items"]
                if i["kind"] == kind]

    def test_the_opening_sentence_becomes_a_heading(self):
        (card,) = self._only("summary")
        assert card["name"] == "ROSA EJEMPLO PRUEBA"
        assert card["window"] == "9:10 AM – 12:10 PM"

    def test_the_brackets_the_app_puts_round_the_name_do_not_survive(self):
        (card,) = self._only("summary")
        assert "[" not in card["name"] and "]" not in card["name"]

    def test_the_date_is_kept_because_this_page_states_it_nowhere_else(self):
        (card,) = self._only("summary")
        assert card["when"] == "sábado, 29 de agosto de 2026"

    def test_the_state_is_a_chip_and_carries_its_colour(self):
        (card,) = self._only("summary")
        assert card["status"] == "Completada"
        assert card["status_tone"] == "ok"

    def test_the_sentence_is_not_also_left_lying_about_as_prose(self):
        """Shaped INSTEAD OF drawn, not as well as."""
        assert not [i for i in self._only("label")
                    if i["txt"].startswith("La visita está programada")]

    def test_the_address_and_the_phone_read_the_same_way(self):
        """Three icons in a column — a pin, a handset, a tick — each
        captioned for a screen reader in a box far too small to draw it.
        The per-character rule caught two and let "Dirección" through on
        the strength of being a shorter word, so the address row wore it as
        a heading and the phone row beside it, built identically, had none.
        """
        rows = self._only("row")
        assert [r["lines"] for r in rows] == [
            ["350 NE 141ST ST APT 202, NORTH MIAMI, FL 331612852"],
            ["(786) 352-1207"]]

    def test_a_caption_that_is_all_a_control_has_still_names_it(self):
        """The other half of the same rule, and the reason it cannot live
        where the annotation test lives: this shape is ALSO what NAMES an
        unlabelled control. An icon button carries nothing but the word hung
        on it for a screen reader, in a box the size of the icon; take that
        away and the row renders as an anonymous bubble.

        Taken away only from a row with something better to say — which is
        what the two rows above have and this one does not.
        """
        m = screenview.build(doc(
            elements=[el("filter_button", "View", [620, 68, 645, 93])],
            statics=[st([620, 68, 645, 93], "Filtro")],
        ))
        assert self._named(m, "Filtro"), \
            "an icon caption standing alone is the control's name"

    @staticmethod
    def _named(m, word):
        """Every item the model draws, wherever it put it — a control at the
        top of a screen is lifted into the nav bar rather than listed."""
        found = [i for r in m["rows"] for i in r["items"]]
        if m.get("nav"):
            found += ([m["nav"]["back"]] if m["nav"]["back"] else []) \
                + (m["nav"]["trailing"] or [])
        return [i for i in found if word in ((i or {}).get("lines") or [])]

    def test_the_apps_own_back_arrow_is_still_recognised_by_its_caption(self):
        """The same shape, on the control this most nearly broke. "Atrás" in
        a 25px square is the only thing identifying HHAeXchange+'s back
        arrow, and it is recognised in order to be SUPPRESSED — the pill
        already has a Back, and two of them three inches apart in different
        words is a question the caregiver has to stop and answer. Losing the
        caption put the app's arrow back in the title bar."""
        m = screenview.build(doc(
            elements=[el("menu_top_bar_back_button", "View", [6, 68, 31, 93]),
                      el("title_bar", "View", [40, 60, 700, 100], "Visitas")],
            statics=[st([6, 68, 31, 93], "Atrás")],
        ))
        assert not self._named(m, "Atrás")

class TestARefusalIsNotTheSameAnswerAsDone:
    """inMyTeam's check-out sheet gives every task TWO checkboxes: the left
    one records that the treatment was given, the right one records that the
    patient REFUSED it.

    Reported from a live check-out, mid-visit: "one check is for the actual
    treatment, the other is for denying that same treatment". The app
    staggers the pair — done box beside the name, refusal box lower and to
    the right — so the reflow banded them separately and drew two identical
    unlabelled switches on two rows with nothing on either saying which
    answer it gave. Whichever way that is misread, the record is wrong about
    whether a person was cared for.

    Geometry verbatim from the phone, 2026-08-29: done boxes at x 26-58,
    refusal boxes at x 868-900, each pair 27px apart vertically.
    """

    TASKS = ["Asistente de ambulación", "Ayudar a vestirse",
             "Baño en silla de baño"]

    def _model(self, refusals=True):
        els = []
        sts = [st([26, 300, 700, 330], "Seleccione las tareas completadas"),
               st([26, 335, 400, 362], "Personal Care (3 H)")]
        for k, name in enumerate(self.TASKS):
            y = 369 + 62 * k
            els.append(el("", "CheckBox", [26, y, 58, y + 32]))
            sts.append(st([10, y + 3, 22, y + 29], "*"))
            sts.append(st([70, y + 1, 500, y + 31], name))
            if refusals:
                els.append(el("", "CheckBox", [868, y + 27, 900, y + 59]))
                sts.append(st([640, y + 28, 860, y + 58],
                              "El paciente se niega"))
        return screenview.build(doc(elements=els, statics=sts,
                                    size=(1080, 2340)))

    def _task_rows(self, m):
        return [r for r in m["rows"]
                if any(i["kind"] == "toggle" for i in r["items"])]

    def test_every_task_carries_both_answers_on_its_own_row(self):
        rows = self._task_rows(self._model())
        assert len(rows) == len(self.TASKS)
        for row, name in zip(rows, self.TASKS):
            kinds = [i["kind"] for i in row["items"]]
            assert kinds.count("toggle") == 2, "done and refused, one row"
            assert name in [i.get("txt") for i in row["items"]]

    def test_the_refusal_is_marked_as_one(self):
        """The two controls must never be told apart by position alone."""
        for row in self._task_rows(self._model()):
            toggles = [i for i in row["items"] if i["kind"] == "toggle"]
            assert [bool(t.get("refuses")) for t in toggles] == [False, True]

    def test_the_words_el_paciente_se_niega_are_not_left_as_a_row(self):
        """It was a row of its own, reading like another task."""
        everything = [i.get("txt") for r in self._model()["rows"]
                      for i in r["items"]]
        assert "El paciente se niega" not in everything

    def test_a_task_with_no_refusal_control_is_untouched(self):
        """HHAeXchange+ and the plan-of-care pages have no such column."""
        rows = self._task_rows(self._model(refusals=False))
        assert len(rows) == len(self.TASKS)
        for row in rows:
            toggles = [i for i in row["items"] if i["kind"] == "toggle"]
            assert len(toggles) == 1 and not toggles[0].get("refuses")

    def test_a_section_header_never_collects_a_refusal(self):
        """"Personal Care (3 H)" has no toggle of its own, so a refusal
        must not fold onto it — that would hand the header an answer about
        a task it is not."""
        m = self._model()
        for row in m["rows"]:
            if any(i.get("txt") == "Personal Care (3 H)" for i in row["items"]):
                assert not [i for i in row["items"] if i.get("refuses")]

    def test_the_refusal_is_drawn_as_a_warning_not_as_a_switch(self):
        """A switch identical to the one beside it is the whole fault. The
        template gives it its own class, its own word, and the bad tone."""
        from pathlib import Path

        page = Path("src/apt_log/ui/templates/_screen.html").read_text(
            encoding="utf-8")
        assert "it.kind == 'toggle' and it.refuses" in page
        assert "a-refuse" in page
        assert "papp.refused" in page
        css = Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")
        assert ".a-refuse {" in css and "var(--bad)" in css

    def test_the_word_is_written_in_both_languages(self):
        import json
        from pathlib import Path

        for code in ("en", "es"):
            words = json.loads(Path(
                f"src/apt_log/ui/locales/{code}.json").read_text(
                    encoding="utf-8"))
            assert words["papp.refused"]

    def test_a_heading_names_the_two_columns(self):
        """"The check mark next to the task doesn't display what it's for …
        the denial has no label or indication of what it does." Said once
        above the list rather than on all twenty-one rows: a red word
        against every task shouts, a column heading is how a form of two
        answers is read anywhere else."""
        heads = [i for r in self._model()["rows"] for i in r["items"]
                 if i["kind"] == "taskhead"]
        assert len(heads) == 1, "once, at the top — not per row"

    def test_the_heading_sits_directly_above_the_first_task(self):
        rows = self._model()["rows"]
        at = next(i for i, r in enumerate(rows)
                  if any(x["kind"] == "taskhead" for x in r["items"]))
        after = rows[at + 1]["items"]
        assert [x["kind"] for x in after].count("toggle") == 2
        assert self.TASKS[0] in [x.get("txt") for x in after]

    def test_no_heading_where_there_is_no_refusal_column(self):
        """A plan of care with one answer per task needs no explaining."""
        assert not [i for r in self._model(refusals=False)["rows"]
                    for i in r["items"] if i["kind"] == "taskhead"]

    def test_the_heading_is_drawn_with_both_words(self):
        from pathlib import Path

        page = Path("src/apt_log/ui/templates/_screen.html").read_text(
            encoding="utf-8")
        assert "it.kind == 'taskhead'" in page
        assert "papp.task_col" in page and "papp.refused_col" in page

    def test_the_refusal_is_drawn_before_the_plain_checkbox_branch(self):
        """THE BUG THE SCREENSHOT CAUGHT. These controls are checkboxes, so
        `it.check` is true for them as well — and that branch sat first, so
        the refusal rendered as an unlabelled empty box exactly like the
        one it must never be mistaken for. Order is the whole fix."""
        from pathlib import Path

        page = Path("src/apt_log/ui/templates/_screen.html").read_text(
            encoding="utf-8")
        assert (page.index("it.kind == 'toggle' and it.refuses")
                < page.index("it.kind == 'toggle' and it.check")), \
            "the refusal must be matched before the plain checkbox"

    def test_the_refusal_keeps_the_warning_colour(self):
        from pathlib import Path

        css = Path("src/apt_log/ui/templates/phone.html").read_text(
            encoding="utf-8")
        assert ".a-refuse { border-color:var(--bad); }" in css


class TestSelectAllNeverAnswersForARefusal:
    """"Select all must just be for the tasks not for denying them" — the
    fear on reading two identical switches. The macro behind that button
    reads only the treatment column, and this is the test that keeps it
    that way: `starred_tasks` is confined to ticks whose right edge falls
    inside the first quarter of the screen, and every refusal box on this
    sheet sits at x 868-900 on a 1080 screen."""

    def test_a_refusal_box_is_never_a_pending_task(self):
        from apt_log import macros

        els, sts = [], []
        for k in range(3):
            y = 369 + 62 * k
            els.append({"cls": "CheckBox", "b": [26, y, 58, y + 32],
                        "checked": False, "enabled": True})
            els.append({"cls": "CheckBox", "b": [868, y + 27, 900, y + 59],
                        "checked": False, "enabled": True})
            sts.append({"cls": "TextView", "b": [10, y + 3, 22, y + 29],
                        "txt": "*"})
            sts.append({"cls": "TextView", "b": [640, y + 28, 860, y + 58],
                        "txt": "*"})
        wanted = macros.starred_tasks(els, sts, 1080)
        assert len(wanted) == 3
        assert all(t["b"][0] < 540 for t in wanted), \
            "the treatment column only, never the refusal column"

class TestSalidaIsTheButtonTheSheetIsFor:
    """`Salida`, at the foot of inMyTeam's check-out sheet, is the control
    that commits the check-out — and it drew as one more grey chevroned
    cell, indistinguishable from `Firma del personal` and `Subir fichero`
    above it.

    The app does not agree, and its own weight is the standard: it gives
    that row the full width of the page and CENTRES its caption (x 514-567
    of 1080, read off the phone 2026-08-29), where every other row on the
    sheet is left-aligned at x 29.
    """

    def test_the_bare_word_is_a_call_to_action(self):
        assert screenview._looks_like_cta("Salida")

    def test_a_recorded_exit_is_not(self):
        """The same visit's detail carries `Salida 05:39 p.m.` — the record
        of a check-out that already happened. A fact drawn as a filled
        button is an invitation to press it."""
        assert not screenview._looks_like_cta("Salida 05:39 p.m.")
        assert not screenview._looks_like_cta("Salida 05:15 PM")

    def test_it_is_matched_whole_and_not_by_prefix(self):
        """Which is the whole reason these live apart from ACTION_WORDS."""
        assert "salida" in screenview.EXACT_ACTION_WORDS
        assert not any(w == "salida" for w in screenview.ACTION_WORDS)

    def test_the_english_forms_come_too(self):
        for word in ("Check out", "CLOCK OUT"):
            assert screenview._looks_like_cta(word), word

    def test_the_row_renders_filled_rather_than_chevroned(self):
        """A cta gets the filled treatment and NO chevron — a chevron says
        "opens more", and this one ends the visit."""
        m = screenview.build(doc(
            size=(1080, 2340),
            elements=[el("", "android.view.View", [13, 2253, 1067, 2300])],
            statics=[st([514, 2265, 567, 2288], "Salida")],
        ))
        (cell,) = [i for r in m["rows"] for i in r["items"]
                   if i["kind"] == "row"]
        assert cell["cta"] is True

    def test_the_signature_rows_beside_it_stay_ordinary(self):
        """They are steps on the way, not the act itself."""
        for word in ("Firma del personal", "Firma del Paciente",
                     "Subir fichero", "Nota adicional"):
            assert not screenview._looks_like_cta(word), word


class TestTabBarFurniture:
    """Mobile Caregiver+ labels each tab cell with a description as well as
    a caption, and hangs an unread count on one — so the patients list
    ended its body with a stray "Beneficiarios" and a bare "370"."""

    def _model(self, extra):
        return screenview.build(doc(
            elements=[el("action_tab_home", "FrameLayout", [226, 1539, 315, 1568]),
                      el("action_tab_messages", "FrameLayout", [404, 1539, 493, 1568])],
            statics=[st([262, 1556, 279, 1565], "Visitas"),
                     st([337, 1555, 381, 1565], "Beneficiarios"),
                     st([436, 1556, 460, 1565], "Mensajes")] + extra,
        ))

    def _body(self, m):
        return [t for r in m["rows"] for i in r["items"]
                for t in ([i.get("txt")] + (i.get("lines") or [])) if t]

    def test_a_word_the_bar_already_said_is_not_a_row(self):
        m = self._model([st([315, 1539, 404, 1568], "Beneficiarios")])
        assert [t["txt"] for t in m["apptabs"]] == ["Visitas", "Beneficiarios",
                                                    "Mensajes"]
        assert "Beneficiarios" not in self._body(m)

    def test_an_unread_count_is_not_a_row(self):
        m = self._model([st([447, 1539, 463, 1548], "370")])
        assert "370" not in self._body(m)

    def test_a_number_ABOVE_the_bar_is_still_content(self):
        m = self._model([st([10, 400, 60, 420], "370")])
        assert "370" in self._body(m)


class TestDisabledRendersDisabled:
    def test_the_item_carries_the_greyed_state(self):
        """The real control is a View — it renders as a cell, not a button,
        which is why both branches of the template had to learn this."""
        m = screenview.build(doc(
            elements=[dict(el("visit_details_clock_out_button_disabled",
                              "View", [11, 1532, 709, 1557],
                              "Registrar salida"), enabled=False)],
            statics=[],
        ))
        (cell,) = [i for r in m["rows"] for i in r["items"] if i.get("aim")]
        assert cell["kind"] == "row"
        assert cell["enabled"] is False

    def test_a_real_button_carries_it_too(self):
        m = screenview.build(doc(
            elements=[dict(el("save", "Button", [11, 1532, 709, 1557],
                              "Salvar"), enabled=False)],
            statics=[],
        ))
        (btn,) = [i for r in m["rows"] for i in r["items"]
                  if i["kind"] == "button"]
        assert btn["enabled"] is False

    def test_a_screen_from_an_older_feed_is_not_greyed_end_to_end(self):
        """Absent means enabled: every element published before the field
        existed was one."""
        m = screenview.build(doc(
            elements=[el("go", "Button", [11, 200, 709, 240], "Continuar")],
            statics=[],
        ))
        (btn,) = [i for r in m["rows"] for i in r["items"]
                  if i["kind"] == "button"]
        assert btn["enabled"] is True


class TestDisabledRowsAreStatements:
    """HHAeXchange+ marks its EVV record lines disabled — "Registros de
    entrada de EVV 6:00 a. m." is a fact the app is stating, not a control.
    Dimming those to a greyed button would have hidden the very thing they
    exist to show, so a disabled row reads as information instead. A
    disabled BUTTON is a different case: a real call to action that is not
    available yet, and it stays a dimmed button."""

    def test_a_disabled_row_becomes_information(self):
        m = screenview.build(doc(
            elements=[dict(el("", "View", [20, 217, 201, 242]),
                           enabled=False)],
            statics=[st([35, 223, 195, 236],
                        "Registros de entrada de EVV 6:00 a. m.")],
        ))
        (row,) = [i for r in m["rows"] for i in r["items"]
                  if i["kind"] == "row"]
        assert row["info"] is True, "a stated fact, not a door"

    def test_a_wide_disabled_row_is_information_too(self):
        """Wider than the shape rules would call informational: the app
        saying so outranks the shape."""
        m = screenview.build(doc(
            elements=[dict(el("", "View", [11, 1400, 709, 1440]),
                           enabled=False)],
            statics=[st([20, 1410, 700, 1430],
                        "La visita se registró correctamente")],
        ))
        (row,) = [i for r in m["rows"] for i in r["items"]
                  if i["kind"] == "row"]
        assert row["info"] is True

    def test_an_enabled_row_is_still_a_door(self):
        m = screenview.build(doc(
            elements=[el("schedule_screen_edit_visit", "View",
                         [22, 271, 704, 296])],
            statics=[st([339, 278, 388, 289], "Ver detalles")],
        ))
        (row,) = [i for r in m["rows"] for i in r["items"]
                  if i["kind"] == "row"]
        assert row["info"] is False

    def test_a_disabled_button_stays_a_dimmed_button(self):
        m = screenview.build(doc(
            elements=[dict(el("save", "Button", [11, 1532, 709, 1557],
                              "Salvar"), enabled=False)],
            statics=[],
        ))
        (btn,) = [i for r in m["rows"] for i in r["items"]
                  if i["kind"] == "button"]
        assert btn["enabled"] is False


class TestVisitDetailScrub:
    """The Visit Detail page, from the tree the phone actually published.

    Reported as "visit details looks really bad", with a screenshot: three
    grey blocks reading "···" stacked above three homeless sentences, two
    enormous voids in the middle of the page, the section she was looking at
    greyed out as though broken, and the two controls that change a record
    dressed as list rows.

    Bounds, classes and enabled flags below are the real ones, to the pixel,
    off a live capture at 720x1600. The words are stand-ins — a patient's
    name and address do not belong in a repository — and the layout does not
    depend on them.
    """

    def _doc(self):
        return doc(
            elements=[
                # The app's own up-arrow, chained to the portal's Back.
                {**el("", "ImageButton", [5, 64, 42, 106], "Navigate up")},
                # A clickable View over the WHOLE content area — scaffolding.
                el("", "View", [0, 106, 720, 1561]),
                el("", "View", [289, 175, 431, 207]),          # More info
                # The section switch. The app DISABLES the pane you are on.
                {**el("", "View", [19, 215, 360, 247]), "enabled": False},
                el("", "View", [360, 215, 701, 247]),          # Plan of care
                # Three margin icons. Two decorative, the middle one tappable.
                {**el("", "Button", [16, 296, 48, 328]), "enabled": False},
                el("", "Button", [16, 341, 48, 373]),
                {**el("", "Button", [16, 386, 48, 418]), "enabled": False},
                el("", "View", [172, 1510, 274, 1549]),        # Check in
                el("", "View", [446, 1508, 548, 1550]),        # Note & out
            ],
            statics=[
                st([52, 76, 119, 94], "Visit Detail"),
                st([354, 119, 367, 139], "H"),
                st([273, 155, 447, 175], "HOME CARE ON CALL, LLC"),
                st([297, 181, 423, 201], "More Patient info +"),
                st([175, 224, 204, 238], "Visits"),
                st([502, 224, 559, 238], "Plan of care"),
                st([19, 261, 454, 277], "Patient Name"),
                st([467, 261, 701, 277], "2026-8-19"),
                st([52, 296, 167, 328], "05:00 AM | 06:00 AM PCA (1 H)"),
                st([52, 349, 318, 365], "1 Example Street, Somewhere"),
                st([52, 394, 331, 410],
                   "No check in and check out data has been recorded"),
                st([199, 1522, 247, 1537], "Check in"),
                st([467, 1515, 528, 1543], "Note & Check out"),
            ],
        )

    def _model(self):
        return screenview.build(self._doc())

    def _flat(self):
        return [it for row in self._model()["rows"] for it in row["items"]]

    # ---------------------------------------------------------- the ··· blocks
    def test_no_nameless_button_survives(self):
        """Each one rendered as a full-width "···" — the portal inventing a
        control out of a square the tree says nothing else about."""
        for it in self._flat():
            if it["kind"] == "button":
                assert (it.get("txt") or "").strip(), \
                    "a captionless button reached the page again"

    def test_the_sentences_are_not_stranded(self):
        """The icon took its own row, so the line it decorated fell below it.
        Every one of the three is back on a line of its own."""
        rows = self._model()["rows"]
        for text in ("05:00 AM | 06:00 AM PCA (1 H)",
                     "1 Example Street, Somewhere",
                     "No check in and check out data has been recorded"):
            band = next(r for r in rows if any(
                text in (it.get("txt") or "") or text in (it.get("lines") or [])
                for it in r["items"]))
            assert len(band["items"]) == 1, f"{text!r} still shares its band"

    def test_a_tappable_icon_hands_its_aim_to_its_sentence(self):
        """The middle icon is the only enabled one. Its tap survives the
        fold — onto the line it belonged to, which is now the target."""
        it = next(i for i in self._flat()
                  if "1 Example Street, Somewhere" in (i.get("lines") or []))
        assert it["kind"] == "row"
        assert it["aim"]["b"] == [16, 341, 48, 373], \
            "the tap must still land where the app drew the control"

    def test_a_disabled_icon_takes_nothing_with_it(self):
        """It is decoration. Nothing in the tree says otherwise, so the page
        must not offer a press that would do nothing."""
        for text in ("05:00 AM | 06:00 AM PCA (1 H)",
                     "No check in and check out data has been recorded"):
            it = next(i for i in self._flat() if (i.get("txt") or "") == text)
            assert not it.get("aim"), f"{text!r} became a fake control"

    # ------------------------------------------------------------- the switch
    def test_the_section_switch_is_a_segmented_control(self):
        seg = next(i for i in self._flat() if i["kind"] == "segment")
        assert [p["txt"] for p in seg["parts"]] == ["Visits", "Plan of care"]

    def test_the_pane_she_is_on_reads_as_current(self):
        """The app disables the tab you are already on. Greyed out, that said
        "unavailable" about the one section actually showing."""
        seg = next(i for i in self._flat() if i["kind"] == "segment")
        assert [p["checked"] for p in seg["parts"]] == [True, False]

    def test_the_other_pane_is_still_reachable(self):
        seg = next(i for i in self._flat() if i["kind"] == "segment")
        assert seg["parts"][1]["aim"]["b"] == [360, 215, 701, 247]

    # ------------------------------------------------------------ the columns
    def test_the_name_and_the_date_share_one_line(self):
        """Two headings side by side are the app's own columns. Splitting them
        stacked two section headings with a heading's margin under each —
        the pair of voids in the middle of the screenshot."""
        rows = self._model()["rows"]
        band = next(r for r in rows
                    if any((it.get("txt") or "") == "Patient Name"
                           for it in r["items"]))
        assert [it.get("txt") for it in band["items"]] == ["Patient Name",
                                                           "2026-8-19"]

    def test_stacked_headings_are_still_split(self):
        """The rule this replaced exists for the stitch's day dividers, which
        land a few pixels apart and overlap in x. Those still separate."""
        m = screenview.build(doc(
            elements=[],
            statics=[st([20, 300, 300, 320], "August 19"),
                     st([22, 316, 300, 336], "August 20")],
        ))
        assert [len(r["items"]) for r in m["rows"]] == [1, 1]

    # ------------------------------------------------------------ the actions
    def test_the_record_changing_controls_are_actions(self):
        rows = self._model()["rows"]
        band = next(r for r in rows if any(
            "Check in" in (it.get("lines") or []) for it in r["items"]))
        assert band.get("actions"), \
            "Check in and Note & Check out rendered as list rows again"

    def test_and_they_still_aim_where_they_always_did(self):
        """Prominence is a drawing change. What pressing them means is not
        this module's to alter."""
        rows = self._model()["rows"]
        band = next(r for r in rows if r.get("actions"))
        assert [it["aim"]["b"] for it in band["items"]] == [
            [172, 1510, 274, 1549], [446, 1508, 548, 1550]]

    # ----------------------------------------------------------- scaffolding
    def test_the_full_page_container_is_not_a_row(self):
        for it in self._flat():
            assert it["b"] != [0, 106, 720, 1561], \
                "the whole content area rendered as one giant control"

    def test_the_app_up_arrow_is_still_suppressed(self):
        assert self._model()["nav"]["back"] is None

    def test_the_title_is_the_pages_own(self):
        assert self._model()["nav"]["title"] == "Visit Detail"


class TestPlanOfCareScrub:
    """inMyTeam's check-out page, from the live tree at 720x1600.

    Reported as a mess. Four separate faults, all of them the reflow reading
    a two-column form as though it were a list.
    """

    def _tasks(self, n=3):
        els, sts = [], []
        names = ["Ambulation Assist", "Assist With dressing", "Bath-Chair Bath"]
        for i in range(n):
            top = 307 + i * 42
            els.append(el("", "CheckBox", [24, top, 56, top + 32]))
            els.append(el("", "CheckBox", [591, top + 17, 623, top + 49]))
            sts.append(st([16, top + 9, 24, top + 25], "*"))
            sts.append(st([61, top + 8, 164, top + 24], names[i]))
            sts.append(st([628, top + 25, 704, top + 41], "Patient refused"))
        return els, sts

    def _model(self, extra_sts=()):
        els, sts = self._tasks()
        sts = [st([19, 288, 153, 308], "Personal Care (1 H)")] + sts
        return screenview.build(doc(els, sts + list(extra_sts)))

    def test_a_task_is_one_row_not_two(self):
        """The task's own tick sits on one baseline and its "Patient refused"
        seventeen pixels lower, so banding split every task in half: thirteen
        tasks became twenty-six rows to scroll."""
        rows = [r for r in self._model()["rows"]
                if any(i["kind"] == "toggle" for i in r["items"])]
        assert len(rows) == 3
        for row in rows:
            kinds = [i["kind"] for i in row["items"]]
            assert kinds.count("toggle") == 2, "both choices belong together"

    def test_each_tick_keeps_its_own_caption(self):
        row = next(r for r in self._model()["rows"]
                   if any(i["kind"] == "toggle" for i in r["items"]))
        words = [i.get("txt") for i in row["items"] if i["kind"] == "label"]
        assert words == ["Ambulation Assist", "Patient refused"]

    def test_the_section_header_does_not_eat_the_first_task(self):
        """"Personal Care (1 H)" at x=19 swallowed "Ambulation Assist" at
        x=61 as its value — 42px of indent squeaking under a 43px allowance.
        The task's name vanished and its tick was left captioned by nothing."""
        flat = [i for r in self._model()["rows"] for i in r["items"]]
        assert any((i.get("txt") or "") == "Personal Care (1 H)"
                   and i["kind"] == "label" for i in flat)
        assert any("Ambulation Assist" in (i.get("txt") or "") for i in flat)
        assert not [i for i in flat if i["kind"] == "kv"]

    def test_a_real_caption_and_value_still_pair(self):
        """The rule this tightened exists for the patient detail, where a
        caption and its value share a left edge to the pixel."""
        m = screenview.build(doc([], [
            st([40, 300, 300, 320], "Admission ID"),
            st([40, 322, 300, 342], "XOR-900196"),
        ]))
        flat = [i for r in m["rows"] for i in r["items"]]
        assert any(i["kind"] == "kv" for i in flat)

    def test_a_signature_box_takes_the_caption_above_it(self):
        """Two tall empty boxes with chevrons, one above the other, where the
        two things she has to collect should be."""
        m = screenview.build(doc(
            elements=[el("", "View", [16, 1023, 704, 1121]),
                      el("", "View", [16, 1148, 704, 1246])],
            statics=[st([27, 1007, 125, 1023], "Patient Signature"),
                     st([27, 1132, 112, 1148], "Staff Signature")],
        ))
        named = [i.get("lines") for r in m["rows"] for i in r["items"]
                 if i["kind"] == "row"]
        assert ["Patient Signature"] in named
        assert ["Staff Signature"] in named

    def test_a_row_that_already_speaks_is_never_renamed(self):
        """Only a control with NOTHING to say adopts a caption, or a day
        divider would rename the visit card beneath it."""
        m = screenview.build(doc(
            elements=[el("card", "View", [16, 320, 704, 420])],
            statics=[st([20, 296, 300, 314], "WEDNESDAY, AUGUST 19"),
                     st([30, 340, 400, 360], "05:00 AM")],
        ))
        card = next(i for r in m["rows"] for i in r["items"]
                    if i["kind"] == "row")
        assert card["lines"] == ["05:00 AM"]

    def test_two_headings_on_a_line_are_a_heading_line(self):
        """Every heading rule was written with :only-child, so the moment the
        patient and the date shared a band they landed in a card together at
        body size — blocky, too much space, a grey block."""
        m = screenview.build(doc([], [
            st([19, 261, 454, 277], "Patient Name"),
            st([467, 261, 701, 277], "2026-8-19"),
        ]))
        assert m["rows"][0].get("heads") is True

    def test_a_lone_heading_is_not_a_heading_line(self):
        """It already had its own treatment and keeps it."""
        m = screenview.build(doc([], [st([19, 261, 454, 277], "Just One")]))
        assert not m["rows"][0].get("heads")

    def test_a_vertical_list_is_never_merged_into_one_row(self):
        """The merge is safe only because a list's rows share horizontal
        space. Full-width cells stacked down the page must stay stacked."""
        m = screenview.build(doc(
            elements=[el(f"r{i}", "View", [16, 300 + i * 40, 704,
                                           336 + i * 40]) for i in range(4)],
            statics=[st([30, 308 + i * 40, 300, 328 + i * 40], f"Row {i}")
                     for i in range(4)],
        ))
        assert len(m["rows"]) == 4


class TestChecklistNotSwitches:
    """A plan of care is a checklist. Reported as "it's a check list design
    not an on off switch…. taking up too much vertical space"."""

    def _row(self):
        m = screenview.build(doc(
            elements=[el("", "CheckBox", [24, 307, 56, 339]),
                      el("", "CheckBox", [591, 324, 623, 356])],
            statics=[st([61, 315, 164, 331], "Ambulation Assist"),
                     st([628, 332, 704, 348], "Patient refused")],
        ))
        return m["rows"][0]

    def test_a_checkbox_is_marked_as_a_tick_box(self):
        ticks = [i for i in self._row()["items"] if i["kind"] == "toggle"]
        assert ticks and all(i["check"] for i in ticks)

    def test_a_real_switch_is_not(self):
        """A Switch says a setting is on and keeps the switch dress."""
        m = screenview.build(doc(
            elements=[el("s", "Switch", [600, 300, 660, 332])],
            statics=[st([40, 305, 300, 325], "Notifications")],
        ))
        sw = next(i for r in m["rows"] for i in r["items"]
                  if i["kind"] == "toggle")
        assert sw["check"] is False

    def test_the_band_is_a_checklist_line(self):
        assert self._row().get("checks") is True

    def test_a_band_with_no_tick_box_is_not(self):
        m = screenview.build(doc(
            elements=[el("s", "Switch", [600, 300, 660, 332])],
            statics=[st([40, 305, 300, 325], "Notifications")],
        ))
        assert not m["rows"][0].get("checks")


class TestTheHeadingClassDoesNotCollideWithTheChrome:
    """`.hdr` is the portal's own frosted app bar. The reflow marked every
    short label with the same class, so each one wore the app bar's
    background and padding — the grey blocks behind every task name."""

    def test_the_reflow_uses_its_own_class(self):
        from pathlib import Path

        frag = (Path(__file__).resolve().parents[1]
                / "src/apt_log/ui/templates/_screen.html"
                ).read_text(encoding="utf-8")
        assert "a-hdr" in frag
        assert " hdr{% endif %}" not in frag

    def test_the_chrome_still_owns_the_bare_name(self):
        """Renaming the reflow's class must not have renamed the app bar."""
        from pathlib import Path

        page = (Path(__file__).resolve().parents[1]
                / "src/apt_log/ui/templates/phone.html"
                ).read_text(encoding="utf-8")
        assert '<div class="hdr">' in page or 'class="hdr"' in page


class TestTheKeypadClassIsNotAlwaysOn:
    """`row.keys` in Jinja resolves to dict.keys — a bound method, always
    truthy — so EVERY row on every screen was rendered as a keypad:
    centred, with an 18px gap. Nothing in the suite could see it, because
    every test reads the model rather than the markup."""

    def test_the_template_asks_for_the_value_not_the_method(self):
        from pathlib import Path

        frag = (Path(__file__).resolve().parents[1]
                / "src/apt_log/ui/templates/_screen.html"
                ).read_text(encoding="utf-8")
        assert "row.get('keys')" in frag
        assert "{% if row.keys %}" not in frag

    def test_an_ordinary_row_does_not_render_as_a_keypad(self):
        from apt_log.ui.app import templates

        class T:
            language = "en"
            def __call__(self, k): return k

        m = screenview.build(doc(
            elements=[el("r", "View", [16, 300, 704, 348])],
            statics=[st([30, 310, 300, 330], "An ordinary row")],
        ))
        html = templates.get_template("_screen.html").render(
            m=screenview.label_keys(m, T()), t=T())
        assert "a-keys" not in html

    def test_a_real_keypad_still_gets_it(self):
        from apt_log.ui.app import templates

        class T:
            language = "en"
            def __call__(self, k): return k

        keys = [el(f"k{n}", "Button", [60 + n * 90, 900, 130 + n * 90, 970],
                   str(n)) for n in range(3)]
        m = screenview.build(doc(elements=keys, statics=[]))
        assert any(r.get("keys") for r in m["rows"])
        html = templates.get_template("_screen.html").render(
            m=screenview.label_keys(m, T()), t=T())
        assert "a-keys" in html


class TestTheWayOutOfAModal:
    """A bottom sheet the portal could not close.

    inMyTeam's signature pad has two exits on the phone and the portal drew
    NEITHER: the scrim went as a curtain (right, for a scrim that means
    nothing) and the ✕ went as an accessibility annotation (right, for
    twelve characters in a sixteen-pixel box). Each rule correct alone; the
    pair stranded her on the staff signature with Done, Clear and nothing
    else. KEYCODE_BACK does not close this sheet — verified on the phone —
    so the portal's own Back was inert too.
    """

    def _sheet(self, extra_small=()):
        els = [el("touch_outside", "View", [0, 64, 720, 1561]),
               el("", "View", [13, 946, 45, 978]),
               el("", "View", [172, 1514, 274, 1545]),
               el("", "View", [446, 1514, 548, 1545])]
        els += [el("", "View", list(b)) for b in extra_small]
        sts = [st([21, 954, 37, 970], "Cancel Visit"),
               st([13, 985, 97, 998], "Patient Signature"),
               st([217, 1522, 245, 1537], "Done"),
               st([489, 1522, 520, 1537], "Clear")]
        return screenview.build(doc(els, sts))

    def test_the_modal_gets_a_way_out(self):
        assert self._sheet()["dismiss"] is not None

    def test_it_aims_at_the_control_that_actually_closes(self):
        """Not the scrim. Tapping the middle of that was tried on the real
        sheet and did nothing — this one was pressed twice and closed it."""
        assert self._sheet()["dismiss"]["aim"]["b"] == [13, 946, 45, 978]

    def test_it_is_named_by_the_portal(self):
        """The app's word for it is a resource id, and its description names
        something it does not do."""
        assert self._sheet()["dismiss"]["txt_key"] == "papp.close_sheet"

    def test_the_scrim_is_never_the_target(self):
        d = self._sheet()["dismiss"]
        assert d["aim"]["rid"] != "touch_outside"
        assert d["aim"]["b"] != [0, 64, 720, 1561]

    def test_the_sheets_own_buttons_are_untouched(self):
        flat = [i for r in self._sheet()["rows"] for i in r["items"]]
        said = [i.get("txt") or (i.get("lines") or [None])[0] for i in flat]
        assert "Done" in said and "Clear" in said

    def test_an_ordinary_screen_gets_none(self):
        """No scrim, no modal, no exit to offer."""
        m = screenview.build(doc(
            elements=[el("", "View", [13, 100, 45, 132]),
                      el("", "View", [172, 1514, 274, 1545])],
            statics=[st([217, 1522, 245, 1537], "Done")],
        ))
        assert m["dismiss"] is None

    def test_two_candidates_mean_no_guess(self):
        """Exactly one or nothing — the discipline the canvas finder uses. A
        wrong "Close" is a button she presses to escape that does something
        else."""
        assert self._sheet(extra_small=([60, 946, 92, 978],))["dismiss"] is None


class TestATabBarWhoseCurrentTabIsNotAControl:
    """HHAeXchange+'s EVV screen, as the phone actually publishes it.

    Two tabs — "GPS" and "Dispositivo o FOB" — and only ONE of them is an
    element. The app marks the tab you are on by taking its click handler
    away, so the open tab arrives as a bare word centred in its half of the
    screen and nothing in the tree says it is a tab at all.

    Rendered as they arrive, the portal showed a loose caption beside one
    live button, and the only thing that looked current was the tab she was
    NOT on. Reported from the field on a real check-in: "the GPS tab wasn't
    selected, it doesn't say which option is selected and we need that
    indicator."

    Bounds below are the ones recorded on the phone, not invented.
    """

    def _model(self, **kw):
        return screenview.build(doc(
            elements=[el("map_screen_tab_bar_security_token", "View",
                         [360, 105, 713, 130])],
            statics=[st([175, 112, 192, 123], "GPS"),
                     st([506, 112, 568, 123], "Dispositivo o FOB")],
            **kw))

    def _segment(self, model):
        for row in model["rows"]:
            for item in row["items"]:
                if item["kind"] == "segment":
                    return item
        return None

    def test_the_pair_becomes_one_control(self):
        assert self._segment(self._model()) is not None

    def test_and_it_says_which_one_she_is_on(self):
        parts = self._segment(self._model())["parts"]
        assert [p["checked"] for p in parts] == [True, False]

    def test_the_open_tab_reads_in_screen_order(self):
        parts = self._segment(self._model())["parts"]
        assert [p["txt"] for p in parts] == ["GPS", "Dispositivo o FOB"]

    def test_the_open_tab_is_not_pressable(self):
        """It is not a control on the phone either — there is nothing to
        aim at, and a press that goes nowhere reads as the portal being
        broken."""
        parts = self._segment(self._model())["parts"]
        assert parts[0]["aim"] is None

    def test_the_other_tab_still_switches(self):
        parts = self._segment(self._model())["parts"]
        assert parts[1]["aim"]["rid"] == "map_screen_tab_bar_security_token"

    def test_three_slots_work_the_same_way(self):
        """The app's own bottom bar, recorded on the schedule: three tabs,
        the open one published as a caption and the other two as containers.
        Left over by the lifted tab bar, which needs all three."""
        model = screenview.build(doc(
            elements=[el("", "View", [246, 1312, 474, 1492]),
                      el("", "View", [492, 1312, 720, 1492])],
            statics=[st([0, 1427, 228, 1475], "Programación"),
                     st([268, 1427, 452, 1475], "Mensajes"),
                     st([552, 1427, 659, 1475], "Menú")]))
        parts = self._segment(model)["parts"]
        assert [p["checked"] for p in parts] == [True, False, False]
        assert [p["txt"] for p in parts] == ["Programación", "Mensajes", "Menú"]

    # ------------------------------------------------- and what it must NOT do
    def test_a_spent_action_beside_a_live_one_is_not_a_tab_bar(self):
        """The legacy visit detail's "Registrar entrada", already used, is a
        non-clickable BUTTON filling its half beside a live Clock Out — the
        same shape as a tab bar and not one. Reading it as tabs would have
        the portal announce "you are on the check-in tab" about two things
        that are neither tabs nor a choice. The class is what tells them
        apart: a caption is a TextView."""
        model = screenview.build(doc(
            elements=[el("btn_clock_out", "Button", [365, 148, 715, 174],
                         "Registrar salida")],
            statics=[st([5, 148, 354, 174], "Registrar entrada", cls="Button")]))
        assert self._segment(model) is None

    def test_a_list_row_is_not_a_tab_bar(self):
        """A caption at its left margin beside a button is a row. Only a
        caption on the slot's CENTRE line is a tab."""
        model = screenview.build(doc(
            elements=[el("btn_go", "Button", [360, 105, 713, 130], "Abrir")],
            statics=[st([20, 112, 200, 123], "Notas del paciente")]))
        assert self._segment(model) is None

    def test_a_half_filled_bar_is_refused_rather_than_guessed(self):
        """One tab, one caption, and a slot with nothing in it: a tab bar
        drawn with the wrong tab lit is worse than one drawn plainly."""
        model = screenview.build(doc(
            elements=[el("tab_c", "View", [480, 105, 713, 130])],
            statics=[st([175, 112, 192, 123], "GPS")],
            size=(720, 1600)))
        seg = self._segment(model)
        assert seg is None or all(p.get("aim") for p in seg["parts"])

    def test_chips_are_too_small_to_be_tabs(self):
        """A tab is a big target. The care plan's ✓/✗ pair is not one, and
        it has its own reading a few rules further down."""
        model = screenview.build(doc(
            elements=[el("chk_yes", "RadioButton", [640, 775, 690, 835], "✓")],
            statics=[st([90, 795, 300, 815], "127 - Toilet Use")]))
        seg = self._segment(model)
        assert seg is None or all(p.get("aim") for p in seg["parts"])


class TestASearchBoxIsOneControl:
    """The patients tab and the visit search, at the bounds the phone
    published during a live scrub.

    "There are some input fields like search for patient in the patients tab
    that we should try to integrate / make nice for searching."

    Both arrived as an empty rectangle with its words floating loose beside
    or above it, and — on the patients tab — the app's own magnifier as an
    unlabelled "···" bubble. Three items for one box, and nothing that
    looked like somewhere to type a name.
    """

    def _fields(self, model):
        return [it for row in model["rows"] for it in row["items"]
                if it["kind"] == "field"]

    def _patients(self):
        return screenview.build(doc(
            elements=[el("patient_screen_title_bar", "EditText",
                         [7, 131, 713, 169]),
                      el("patient_screen_title_bar_search_icon", "View",
                         [682, 134, 708, 159])],
            statics=[st([15, 140, 98, 153], "Buscar por nombre")]))

    # ------------------------------------------------ the hint sits INSIDE it
    def test_the_words_in_the_box_are_the_boxs_own(self):
        assert self._fields(self._patients())[0]["hint"] == "Buscar por nombre"

    def test_the_loose_caption_is_gone_from_the_page(self):
        said = [it.get("txt") for row in self._patients()["rows"]
                for it in row["items"] if it["kind"] == "label"]
        assert "Buscar por nombre" not in said

    def test_the_apps_own_magnifier_belongs_to_the_box(self):
        field = self._fields(self._patients())[0]
        assert field["submit"]["rid"] == "patient_screen_title_bar_search_icon"

    def test_and_is_no_longer_a_bubble_of_its_own(self):
        rows = [it for row in self._patients()["rows"] for it in row["items"]
                if it["kind"] == "row"]
        assert not any((it.get("aim") or {}).get("rid", "").endswith(
            "search_icon") for it in rows)

    def test_the_box_is_still_the_thing_that_gets_typed_into(self):
        """The fold must not cost the field its own aim, or there would be
        nowhere to put the name."""
        assert self._fields(self._patients())[0]["aim"]["rid"] \
            == "patient_screen_title_bar"

    # ----------------------------------------------- ...or directly above it
    def _visit_search(self):
        return screenview.build(doc(
            elements=[el("search_visit_screen_patient_name_field", "EditText",
                         [7, 120, 713, 158]),
                      el("search_visit_screen_search_button", "View",
                         [7, 270, 713, 295], "Buscar")],
            statics=[st([7, 105, 97, 118], "Nombre del paciente"),
                     st([346, 277, 375, 288], "Buscar")]))

    def test_a_caption_written_above_the_box_is_the_boxs_name_too(self):
        assert self._fields(self._visit_search())[0]["hint"] \
            == "Nombre del paciente"

    def test_a_heading_further_up_the_page_is_not(self):
        """Directly above, left-aligned, and alone on its line — a section
        heading with a field somewhere under it is not a caption."""
        model = screenview.build(doc(
            elements=[el("f", "EditText", [7, 600, 713, 638])],
            statics=[st([7, 105, 200, 118], "Buscar visitas")]))
        assert not self._fields(model)[0].get("hint")

    def test_the_page_still_has_its_search_button(self):
        """Folding the caption must not swallow the action."""
        said = [line for row in self._visit_search()["rows"]
                for it in row["items"] for line in (it.get("lines") or [])]
        assert "Buscar" in said


class TestWhatTalkBackIsToldToSay:
    """An app hangs instructions on a control so a screen reader can explain
    how to work it. The portal is not a screen reader, and the visit search's
    date range came through reading "Ingrese el formato de datos como mes,
    fecha, año, doble toque para activar" — a sentence about double-tapping,
    on a page where nothing is double-tapped.
    """

    def _lines(self, caption):
        model = screenview.build(doc(
            elements=[el("date_range", "View", [7, 173, 713, 202])],
            statics=[st([7, 173, 713, 202], caption),
                     st([12, 181, 331, 194], "07/19/2026"),
                     st([382, 181, 702, 194], "08/18/2026")]))
        return [line for row in model["rows"] for it in row["items"]
                for line in (it.get("lines") or [])]

    def test_the_instruction_does_not_reach_the_page(self):
        lines = self._lines("Ingrese el formato de datos como mes, fecha, "
                            "año, doble toque para activar")
        assert not any("doble toque" in line for line in lines)

    def test_but_the_dates_still_do(self):
        lines = self._lines("Ingrese el formato de datos como mes, fecha, "
                            "año, doble toque para activar")
        assert "07/19/2026" in lines and "08/18/2026" in lines

    def test_english_says_it_too(self):
        assert not any("double tap" in line.lower()
                       for line in self._lines("Date range, double tap to "
                                               "activate"))

    def test_an_ordinary_sentence_is_left_alone(self):
        assert any("Carmen" in line
                   for line in self._lines("Carmen Villalon, 8:00 PM"))


class TestTheSignatureScreenSheIsStandingOn:
    """HHAeXchange+'s patient signature, at the bounds recorded live while a
    caregiver was on it — rotated, 1534x681 inside a 720x1600 activity.

    "I need this page FLESHED out and perfect... prioritize the front end
    view... on the app it's the patient first then the staff."

    What it looked like before: a patient's name truncated into the title
    slot, a loose date, the canvas drawn as an ON/OFF SWITCH, and the app's
    Borrar as a list row with a chevron beside the button that submits a
    signature. The step counter — the only thing that says WHICH signature
    this is — was thrown away entirely.
    """

    CANVAS = [18, 171, 1518, 525]

    def _doc(self, canvas_role=None):
        canvas = {"rid": "", "cls": "View", "b": self.CANVAS, "txt": "",
                  "checked": False, "focused": False, "enabled": True}
        if canvas_role:
            canvas["role"] = canvas_role
        return {"id": "f", "size": [720, 1600], "canvas": True, "blocked": "",
                "notice": "",
                "elements": [
                    canvas,
                    {"rid": "signature_screen_clear_button", "cls": "View",
                     "b": [14, 656, 44, 681], "txt": "", "checked": False,
                     "focused": False, "enabled": True},
                    {"rid": "signature_screen_submit_button", "cls": "View",
                     "b": [1474, 656, 1529, 681], "txt": "", "checked": False,
                     "focused": False, "enabled": True},
                    {"rid": "menu_top_bar_back_button", "cls": "View",
                     "b": [6, 17, 31, 42], "txt": "", "checked": False,
                     "focused": False, "enabled": True},
                    {"rid": "help_button", "cls": "View",
                     "b": [1507, 17, 1534, 43], "txt": "", "checked": False,
                     "focused": False, "enabled": True},
                ],
                "statics": [
                    st([7, 54, 96, 67], "08/19/2026 10:04 p. m."),
                    st([14, 662, 44, 675], "Borrar"),
                    st([1488, 663, 1515, 674], "Enviar"),
                    st([6, 17, 31, 42], "Atrás", cls="View"),
                    st([747, 15, 790, 30], "Paso 2 de 3"),
                    st([688, 30, 849, 45], "CARIDAD M ROJAS BATISTA Firma de"),
                    st([1507, 18, 1534, 42], "Ayuda", cls="View"),
                ]}

    def _items(self, model):
        return [it for row in model["rows"] for it in row["items"]]

    # ------------------------------------------------------------ the canvas
    def test_the_thing_she_signs_on_is_a_canvas(self):
        kinds = [it["kind"] for it in self._items(screenview.build(self._doc()))]
        assert "canvas" in kinds

    def test_not_a_switch(self):
        """A screen reader's "double tap" says a node accepts a tap, not that
        it is a checkbox — and the canvas carries one. Drawn as a switch, on
        the page where a patient's signature is collected."""
        model = screenview.build(self._doc(canvas_role="toggle"))
        canvas = [it for it in self._items(model) if it["b"] == self.CANVAS]
        assert canvas and canvas[0]["kind"] == "canvas"

    def test_and_not_an_empty_list_cell(self):
        model = screenview.build(self._doc())
        rows = [it for it in self._items(model)
                if it["kind"] == "row" and it["b"] == self.CANVAS]
        assert not rows

    def test_it_does_not_tap_through_to_the_phone(self):
        """A tap on a canvas is a dot of ink where the finger landed. The
        only place a signature can be drawn from here is the pad."""
        model = screenview.build(self._doc())
        canvas = next(it for it in self._items(model) if it["kind"] == "canvas")
        assert canvas["b"] == self.CANVAS

    def test_a_page_with_no_canvas_grows_none(self):
        doc = self._doc()
        doc["canvas"] = False
        assert "canvas" not in [it["kind"] for it in
                                self._items(screenview.build(doc))]

    # -------------------------------------------------------------- the step
    def test_the_step_survives(self):
        """"Paso 2 de 3" is the difference between the patient's signature
        and the caregiver's, and she has to know which one she is collecting
        BEFORE she collects it."""
        assert screenview.build(self._doc())["nav"]["step"] == "Paso 2 de 3"

    def test_the_page_still_has_its_title(self):
        assert screenview.build(self._doc())["nav"]["title"] \
            == "CARIDAD M ROJAS BATISTA Firma de"

    def test_a_step_counter_is_not_a_spelled_out_brand(self):
        """Four tokens, two of them one character — the shape the logo rule
        was written for. Nobody spells a brand out in numbers."""
        assert not screenview._is_spelled_out("Paso 2 de 3")
        assert not screenview._is_spelled_out("Step 2 of 3")
        assert screenview._is_spelled_out("Logotipo de H H AeXchange +")

    def test_a_name_with_initials_is_still_safe(self):
        assert not screenview._is_spelled_out("A B Smith")

    # ----------------------------------------------------------- the actions
    def test_borrar_and_enviar_are_the_pages_actions(self):
        """Not a list row with a chevron pointing at somewhere to browse,
        beside the button that submits a patient's signature."""
        model = screenview.build(self._doc())
        assert any(row.get("actions") for row in model["rows"])

    def test_the_affirmative_one_comes_last(self):
        """The template fills the last action; the last one has to be the
        one that submits."""
        model = screenview.build(self._doc())
        row = next(r for r in model["rows"] if r.get("actions"))
        captions = [(it.get("txt") or (it.get("lines") or [""])[0])
                    for it in row["items"]]
        assert captions == ["Borrar", "Enviar"]

    def test_the_bubbles_are_gone_from_this_screen_too(self):
        nav = screenview.build(self._doc())["nav"]
        assert nav["back"] is None and nav["trailing"] == []


class TestWhichTickIsWhich:
    """The legacy Today Visits list marks a verified EVV record with a drawn
    check — one for the check-in, one for the check-out — and the tree
    carries them as textless ImageViews named `imgStartTime` and `imgEndTime`.

    Both mapped to the same green tick, so a row that had recorded both
    showed two identical marks, and a row that had recorded ONE showed a
    single tick that could equally have been either. Which of the two is
    recorded is the entire content of an EVV record, and it is what the list
    is read for.

    Bounds below are from the recording of a real visits list.
    """

    def _row(self, marks=("imgStartTime", "imgEndTime")):
        statics = [st([39, 126, 529, 148], "CARMEN VILLALON"),
                   st([39, 154, 535, 173], "1000 NW 1 ST, MIAMI FL")]
        for i, rid in enumerate(marks):
            statics.append({"cls": "ImageView", "rid": rid, "txt": "",
                            "b": [594 + i * 83, 189, 611 + i * 83, 206]})
        return screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 120, 720, 226])],
            statics=statics))

    def _marks(self, model):
        return next((it["marks"] for row in model["rows"]
                     for it in row["items"] if it.get("marks")), [])

    def test_both_ticks_reach_the_row(self):
        assert len(self._marks(self._row())) == 2

    def test_each_one_carries_a_name_of_its_own(self):
        keys = [m.get("txt_key") for m in self._marks(self._row())]
        assert keys == ["papp.mark.entry", "papp.mark.exit"]

    def test_a_lone_tick_says_which_it_is(self):
        """The case the bare glyph could not express at all."""
        assert [m.get("txt_key") for m in
                self._marks(self._row(marks=("imgStartTime",)))] \
            == ["papp.mark.entry"]

    def test_and_the_other_lone_tick_says_the_other(self):
        assert [m.get("txt_key") for m in
                self._marks(self._row(marks=("imgEndTime",)))] \
            == ["papp.mark.exit"]

    def test_they_are_still_green_ticks(self):
        """The name is added to the mark, not instead of it."""
        for m in self._marks(self._row()):
            assert m["sym"] == "✓" and m["tone"] == "ok"

    def test_the_names_arrive_in_her_language(self):
        from apt_log.ui.i18n import Translator

        model = self._row()
        screenview.label_keys(model, Translator("es"))
        assert [m["txt"] for m in self._marks(model)] == ["Entrada", "Salida"]

    def test_and_in_his(self):
        from apt_log.ui.i18n import Translator

        model = self._row()
        screenview.label_keys(model, Translator("en"))
        assert [m["txt"] for m in self._marks(model)] == ["In", "Out"]

    def test_a_mark_the_portal_has_no_name_for_stays_a_bare_glyph(self):
        """Every other state mark is a character the app itself drew, and it
        already says what it means."""
        model = screenview.build(doc(
            elements=[el("", "RelativeLayout", [0, 120, 720, 226])],
            statics=[st([39, 126, 529, 148], "CARMEN VILLALON"),
                     st([594, 189, 611, 206], "\uf00c")]))
        marks = self._marks(model)
        assert marks and not any(m.get("txt_key") for m in marks)

    def test_both_languages_have_the_words(self):
        import json as json_mod
        from pathlib import Path

        base = Path(__file__).resolve().parents[1] / "src/apt_log/ui/locales"
        for name in ("en.json", "es.json"):
            words = json_mod.loads((base / name).read_text(encoding="utf-8"))
            assert words.get("papp.mark.entry") and words.get("papp.mark.exit")


class TestAButtonWearsItsOwnCaption:
    """Reported as "those two icons next to Borrar and Enviar are broken,
    they don't show anything". They were not icons: they were the app's
    buttons, which carry no text of their own, rendered blank beside the
    loose labels that should have been written on them.

    All three care apps draw controls this way.
    """

    def _screen(self, extra_static=None):
        return screenview.build(doc(
            elements=[
                el("signature_screen_clear_button", "Button",
                   [20, 1500, 340, 1560]),
                el("signature_screen_submit_button", "Button",
                   [380, 1500, 700, 1560]),
            ],
            statics=[st([120, 1515, 240, 1545], "Borrar"),
                     st([480, 1515, 600, 1545], "Enviar")]
                    + (extra_static or [])))

    def _buttons(self, model):
        out = []
        for row in model.get("rows") or ():
            for item in row.get("items") or ():
                if item.get("kind") == "button":
                    out.append(item)
        return out

    def test_the_caption_lands_on_the_button(self):
        words = [b["txt"] for b in self._buttons(self._screen())]
        assert "Borrar" in words and "Enviar" in words

    def test_neither_button_is_blank(self):
        assert all(b["txt"] for b in self._buttons(self._screen()))

    def test_the_word_is_not_also_left_lying_beside_it(self):
        """Otherwise the screen reads "Borrar Borrar"."""
        model = self._screen()
        loose = [s for row in (model.get("rows") or ())
                 for s in (row.get("items") or ())
                 if s.get("kind") != "button" and s.get("txt") in ("Borrar",
                                                                   "Enviar")]
        assert not loose

    def test_a_word_that_merely_overlaps_is_not_claimed(self):
        """Strict containment. A label that is only near a button belongs to
        whatever it is actually inside — guessing there is how "Enviar" ends
        up written on the Clear button."""
        model = self._screen(extra_static=[
            st([300, 1490, 420, 1580], "Straddling")])
        assert "Straddling" not in [b["txt"] for b in self._buttons(model)]

    def test_an_icon_glyph_is_never_adopted_as_a_caption(self):
        """A private-use codepoint is a drawn icon, not a word — writing one
        on a button gives it a tofu box for a label."""
        model = self._screen(extra_static=[
            {"b": [30, 1510, 60, 1550], "txt": ""}])
        assert all("" not in (b["txt"] or "")
                   for b in self._buttons(model))


class TestAndroidsPermissionPrompt:
    """WATCHED LIVE, AND IT RENDERED AS A MESS. The first automated check-in
    raised "Allow ... to access this device's location?" and the general
    reflow banded the whole dialog into ONE row: the question squeezed into a
    column two characters wide, the accuracy radios reduced to bare
    checkboxes, and the three STACKED buttons laid side by side as though
    they were a segmented control.

    Rebuilt here from the live capture (2026-08-21). This is the dialog
    standing between a scheduled check-in and the record it exists to write.
    """

    def _doc(self, **over):
        doc = {
            "id": "p", "size": [720, 1536],
            "elements": [
                {"rid": "grant_dialog", "cls": "LinearLayout",
                 "b": [101, 638, 619, 987]},
                {"rid": "permission_location_accuracy_radio_fine",
                 "cls": "RadioButton", "txt": "Precise", "checked": True,
                 "checkable": True, "b": [264, 727, 349, 830]},
                {"rid": "permission_location_accuracy_radio_coarse",
                 "cls": "RadioButton", "txt": "Approximate", "checked": False,
                 "checkable": True, "b": [371, 727, 456, 830]},
                {"rid": "permission_allow_foreground_only_button",
                 "cls": "Button", "txt": "While using the app",
                 "b": [127, 847, 593, 884]},
                {"rid": "permission_allow_one_time_button", "cls": "Button",
                 "txt": "Only this time", "b": [127, 886, 593, 923]},
                {"rid": "permission_deny_button", "cls": "Button",
                 "txt": "Don’t allow", "b": [127, 925, 593, 962]},
            ],
            "statics": [
                {"rid": "permission_icon", "txt": "", "b": [349, 664, 370, 685]},
                # The publisher does not carry this node's resource-id — which
                # is why the question is found by what is LEFT OVER inside the
                # dialog, not by an id.
                {"rid": None,
                 "txt": "Allow Inmyteam to access this device’s location?",
                 "b": [127, 693, 593, 711]},
            ],
        }
        doc.update(over)
        return doc

    def _p(self, doc=None):
        return screenview.build(doc or self._doc())["permission"]

    def test_the_dialog_is_recognised(self):
        assert self._p() is not None

    def test_the_question_is_whole_and_not_two_characters(self):
        assert self._p()["message"] == \
            "Allow Inmyteam to access this device’s location?"

    def test_the_question_survives_the_id_not_being_published(self):
        """An id rule found nothing on the very screen this was written from:
        the question came back empty while sitting plainly in the statics."""
        doc = self._doc()
        assert all(s.get("rid") != "permission_message"
                   for s in doc["statics"])
        assert self._p(doc)["message"].startswith("Allow ")

    def test_the_accuracy_choice_is_named_not_a_bare_checkbox(self):
        got = [(c["txt"], c["on"]) for c in self._p()["choices"]]
        assert got == [("Precise", True), ("Approximate", False)]

    def test_the_three_answers_are_all_there_and_told_apart(self):
        got = [(a["txt"], a["tone"]) for a in self._p()["actions"]]
        assert got == [("While using the app", "allow"),
                       ("Only this time", "once"),
                       ("Don’t allow", "deny")]

    def test_the_buttons_carry_real_aims(self):
        for action in self._p()["actions"]:
            assert action["aim"]["rid"].startswith("permission_")
            assert len(action["aim"]["b"]) == 4

    def test_an_ordinary_screen_has_no_permission_block(self):
        doc = self._doc(elements=[{"rid": "some_button", "cls": "Button",
                                   "txt": "Go", "b": [0, 0, 10, 10]}],
                        statics=[])
        assert screenview.build(doc)["permission"] is None

    def test_a_dialog_with_no_buttons_is_not_claimed(self):
        """Half a dump is not a prompt, and rendering one with nothing to
        press would strand her on a screen with no way forward."""
        doc = self._doc()
        doc["elements"] = [e for e in doc["elements"]
                           if not e["rid"].startswith("permission_allow")
                           and e["rid"] != "permission_deny_button"]
        assert screenview.build(doc)["permission"] is None

    def test_the_rows_do_not_say_it_all_over_again(self):
        """CAUGHT BY PHOTOGRAPHING THE FIX, NOT BY READING THE CSS. The new
        dialog rendered correctly and the old mangled band was still sitting
        underneath it — the page offered the same three buttons twice, once
        properly and once as the garble this was written to remove. Two
        "Don't allow"s on a screen is worse than one badly drawn one."""
        model = screenview.build(self._doc())
        words = []
        for row in model["rows"]:
            for item in row.get("items") or []:
                words.append((item.get("txt") or "").strip())
                words.extend(l.strip() for l in (item.get("lines") or []))
        assert "While using the app" not in words
        assert "Don’t allow" not in words
        assert "Precise" not in words

    def test_content_outside_the_dialog_is_kept(self):
        """A prompt can sit over a screen that still has content of its own
        around it, and dropping every row would blank that."""
        doc = self._doc()
        doc["elements"] = doc["elements"] + [
            {"rid": "elsewhere", "cls": "Button", "txt": "Underneath",
             "b": [10, 100, 700, 160]}]
        model = screenview.build(doc)
        words = [(i.get("txt") or "") for r in model["rows"]
                 for i in (r.get("items") or [])]
        assert "Underneath" in words


class TestAModalTheAppRaised:
    """inMyTeam answers a check-in outside the visit's window with a small
    centred box — "Warning! / Invalid time / Accept" — and the portal drew a
    page with almost nothing on it: not the warning, not the patient, not the
    time, not the address, and not the "Failed  Check in 11:55 PM" the app had
    just written.

    A dialog is its own WINDOW and the dump lists it BEFORE the activity
    behind it. The overlay rule reads document order as z-order — right for a
    surface an app slides over its own page, wrong for two windows — so the
    activity looked like a curtain that had arrived over the dialog and
    everything "beneath" it was thrown away. The dialog was on top all along.

    Rebuilt from the live capture, 2026-08-21.
    """

    def _doc(self):
        return doc(
            size=[720, 1600],
            elements=[
                el("", "View", [275, 839, 446, 874]),           # Accept
                el("", "ImageButton", [5, 64, 42, 106], "Navigate up"),
                el("", "View", [0, 106, 720, 1561]),            # the activity
                el("", "Button", [16, 296, 48, 328]),
            ],
            statics=[
                st([320, 755, 400, 778], "Warning!"),
                st([331, 781, 389, 794], "Invalid time"),
                st([339, 849, 382, 864], "Accept"),
                st([52, 76, 119, 94], "Visit Detail"),
                st([19, 261, 454, 277], "Ada Farthingale"),
                st([52, 296, 167, 328], "05:00 AM | 06:00 AM PCA (1 H)"),
                st([39, 440, 71, 454], "Failed"),
                st([100, 440, 196, 456], "Check in 11:55 PM"),
            ])

    def _model(self):
        return screenview.build(self._doc())

    def _words(self, m):
        out = []
        for row in m["rows"]:
            for item in row.get("items") or []:
                out.append((item.get("txt") or "").strip())
                out.extend(l.strip() for l in (item.get("lines") or []))
        return out

    def test_the_alert_is_recognised(self):
        a = self._model()["alert"]
        assert a is not None
        assert a["title"] == "Warning!"
        assert a["lines"] == ["Invalid time"]
        assert a["action"]["txt"] == "Accept"

    def test_the_page_underneath_comes_back(self):
        """THE HALF THAT MATTERED. All of this was being dropped."""
        words = self._words(self._model())
        for expected in ("Ada Farthingale", "05:00 AM | 06:00 AM PCA (1 H)",
                         "Failed", "Check in 11:55 PM"):
            assert expected in words, expected

    def test_the_alert_is_not_also_scattered_through_the_page(self):
        words = self._words(self._model())
        assert "Warning!" not in words
        assert "Invalid time" not in words
        assert "Accept" not in words

    def test_the_action_carries_a_real_aim(self):
        aim = self._model()["alert"]["action"]["aim"]
        assert aim["b"] == [275, 839, 446, 874]

    def test_an_ordinary_page_raises_no_alert(self):
        m = screenview.build(doc(
            elements=[el("go", "Button", [10, 600, 700, 660], "Entrar")],
            statics=[st([10, 300, 700, 340], "Bienvenida")]))
        assert m["alert"] is None

    def test_a_lone_ok_button_with_no_message_is_not_an_alert(self):
        """Without the message above it this would claim any small OK-ish
        button on an ordinary form."""
        m = screenview.build(doc(
            elements=[el("", "View", [275, 839, 446, 874])],
            statics=[st([339, 849, 382, 864], "Accept")]))
        assert m["alert"] is None


class TestTheAppsOwnBackNeverRidesInTheList:
    """Suppressing it in the title bar was only ever half the rule. It is a
    duplicate of the pill's Back whether or not a nav bar was recognised, and
    on a page where none was it fell into the LIST — a full-width row reading
    "Navigate up", three inches from a Back that does the same thing."""

    def test_it_is_gone_from_a_page_with_no_nav_bar(self):
        m = screenview.build(doc(
            size=[720, 1600],
            elements=[el("", "ImageButton", [5, 700, 42, 742], "Navigate up"),
                      el("go", "Button", [10, 900, 700, 960], "Entrar")],
            statics=[]))
        words = [(i.get("txt") or "") for r in m["rows"]
                 for i in (r.get("items") or [])]
        assert "Navigate up" not in words
        assert "Entrar" in words

    def test_and_still_gone_when_a_nav_bar_is_recognised(self):
        m = screenview.build(doc(
            elements=[el("", "ImageButton", [5, 64, 42, 106], "Navigate up")],
            statics=[st([52, 76, 119, 94], "Visit Detail")]))
        assert m["nav"]["back"] is None
        words = [(i.get("txt") or "") for r in m["rows"]
                 for i in (r.get("items") or [])]
        assert "Navigate up" not in words


class TestADropdownIsDrawnAsADropdown:
    """"Hoy is just a header and we can make this waaay better ... it's
    coming up as something clickable? Not sure but it doesn't look good ...
    looks like Hoy opens up a filter which is nice but it doesn't behave like
    a filter pick if you know what I mean. As you can see I started out the
    prompt confused as to what this header even does."

    Read off Mobile Caregiver+'s visit list: the control is an Android
    Spinner — a native select — with its chosen value drawn inside it and the
    range that choice yields sitting loose beside it.
    """

    def _doc(self):
        return {"app": "com.tellus.evv.v2", "size": [1080, 2340],
                "elements": [
                    {"cls": "Spinner", "rid": "spinnerPeriod", "txt": "",
                     "b": [742, 181, 1080, 241], "enabled": True,
                     "checked": False, "focused": False, "has_text": False}],
                "statics": [
                    {"cls": "TextView", "rid": "visits_scheduleperiod",
                     "txt": "ago 23 - ago 29", "b": [13, 192, 220, 230]},
                    {"cls": "CheckedTextView", "rid": "text1",
                     "txt": "La semana Pasada", "b": [749, 192, 1080, 230]}]}

    def _only(self, model):
        items = [it for row in model["rows"] for it in row["items"]]
        return [it for it in items if it["kind"] == "select"]

    def test_a_spinner_is_a_select_not_a_list_row(self):
        picks = self._only(screenview.build(self._doc()))
        assert len(picks) == 1, "the picker was not recognised"

    def test_it_says_what_it_is_set_to(self):
        """Android draws the chosen option in a CheckedTextView laid over the
        control, so the Spinner node itself carries no text. A picker that
        cannot say what it is set to is a button labelled nothing."""
        assert self._only(screenview.build(self._doc()))[0]["txt"] == \
            "La semana Pasada"

    def test_the_range_rides_on_the_control_that_decides_it(self):
        """Loose beside the picker it read as a page heading — which is
        exactly what was reported. It is what the choice currently yields."""
        assert self._only(screenview.build(self._doc()))[0]["note"] == \
            "ago 23 - ago 29"

    def test_nothing_is_left_floating_next_to_it(self):
        model = screenview.build(self._doc())
        band = [row for row in model["rows"]
                if any(it["kind"] == "select" for it in row["items"])][0]
        assert len(band["items"]) == 1

    def test_a_band_with_a_second_control_is_left_alone(self):
        """Nothing here can say which words belong to which control, so a
        band that is not simply a picker and its caption is left as found."""
        doc = self._doc()
        doc["elements"].append(
            {"cls": "Button", "rid": "go", "txt": "Ir", "b": [600, 181, 700, 241],
             "enabled": True, "checked": False, "focused": False,
             "has_text": True})
        items = [it for row in screenview.build(doc)["rows"]
                 for it in row["items"]]
        assert any(it["kind"] == "label" and it.get("txt") == "ago 23 - ago 29"
                   for it in items)

    def _today(self):
        """The state the report came from: the picker set to Today, and the
        app writing "Hoy - sáb, ago 29" beside a control reading "Hoy"."""
        return {"app": "com.tellus.evv.v2", "size": [1080, 2340],
                "elements": [
                    {"cls": "Spinner", "rid": "spinnerPeriod", "txt": "",
                     "b": [742, 181, 1080, 241], "enabled": True,
                     "checked": False, "focused": False, "has_text": False}],
                "statics": [
                    {"cls": "TextView", "rid": "visits_scheduleperiod",
                     "txt": "Hoy - sáb, ago 29", "b": [13, 192, 220, 230]},
                    {"cls": "CheckedTextView", "rid": "text1", "txt": "Hoy",
                     "b": [749, 192, 1080, 230]},
                    {"cls": "TextView", "rid": "visits_event0_title",
                     "txt": "sáb, ago 29", "b": [15, 257, 182, 295]}]}

    def test_the_value_is_not_repeated_in_its_own_note(self):
        pick = self._only(screenview.build(self._today()))[0]
        assert pick["txt"] == "Hoy"
        assert pick["note"] == "sáb, ago 29"

    def test_a_lone_day_header_repeating_the_picker_is_dropped(self):
        """On a single day the group header is the date the picker has just
        said, printed again immediately underneath."""
        items = [it for row in screenview.build(self._today())["rows"]
                 for it in row["items"]]
        assert not [it for it in items
                    if it["kind"] == "label" and it.get("txt") == "sáb, ago 29"]

    def test_a_week_keeps_every_one_of_its_days(self):
        """With several headings each is telling the reader something the
        picker cannot, so none of them go."""
        doc = self._today()
        doc["statics"][0]["txt"] = "ago 23 - ago 29"
        doc["statics"][1]["txt"] = "La semana Pasada"
        doc["statics"].append({"cls": "TextView", "rid": "visits_event1_title",
                               "txt": "dom, ago 23", "b": [15, 500, 182, 538]})
        heads = [it for row in screenview.build(doc)["rows"]
                 for it in row["items"]
                 if it["kind"] == "label" and it.get("header")]
        assert len(heads) == 2

    def test_no_band_is_left_empty(self):
        rows = screenview.build(self._today())["rows"]
        assert all(row["items"] for row in rows)
