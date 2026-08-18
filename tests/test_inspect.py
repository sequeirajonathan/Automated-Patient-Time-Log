"""Screen inspection must not leak patient data, including by omission.

The redactor's failure mode is silent and one-directional: nobody notices a name
that should not have been printed until it is already in a transcript.
"""

from __future__ import annotations

import pytest

from apt_log.inspect import inspect_source, looks_like_phi

P = "com.hhaexchange.caregiver:id/"

SOURCE = f'''
<hierarchy>
<android.widget.ListView class="android.widget.ListView" resource-id="{P}list_today_schedule" clickable="false" text="" bounds="[0,0][750,1200]"/>
<android.widget.TextView class="android.widget.TextView" resource-id="{P}lbl_patient_name" clickable="false" text="Alice Example" bounds="[0,0][750,60]"/>
<android.widget.TextView class="android.widget.TextView" resource-id="{P}lbl_address" clickable="false" text="12 Privacy Lane" bounds="[0,60][750,120]"/>
<android.widget.TextView class="android.widget.TextView" resource-id="{P}lbl_schedule_start_time" clickable="false" text="08:00PM" bounds="[0,120][750,180]"/>
<android.widget.Button class="android.widget.Button" resource-id="{P}btn_clock_in" clickable="true" text="Clock In" bounds="[0,180][750,240]"/>
</hierarchy>
'''


class TestTextWithheldByDefault:
    def test_no_text_is_printed_unless_asked_for(self):
        out = inspect_source(SOURCE).render()
        assert "Alice Example" not in out
        assert "12 Privacy Lane" not in out
        assert "08:00PM" not in out
        assert "Clock In" not in out

    def test_structure_is_still_useful(self):
        out = inspect_source(SOURCE).render()
        assert "list_today_schedule" in out
        assert "btn_clock_in" in out
        assert "True" in out          # clickable column


class TestSelectiveReveal:
    def test_reveals_only_the_requested_id(self):
        out = inspect_source(SOURCE, text_for=("lbl_schedule_start_time",)).render()
        assert "08:00PM" in out
        assert "Alice Example" not in out

    def test_reveals_button_labels(self):
        out = inspect_source(SOURCE, text_for=("btn_clock_in",)).render()
        assert "Clock In" in out
        assert "12 Privacy Lane" not in out


class TestPhiRefusedEvenWhenRequested:
    """Asking for it is not authorisation — the id is the signal."""

    def test_patient_name_is_refused(self):
        report = inspect_source(SOURCE, text_for=("lbl_patient_name",))
        out = report.render()
        assert "Alice Example" not in out
        assert "lbl_patient_name" in report.refused

    def test_address_is_refused(self):
        report = inspect_source(SOURCE, text_for=("lbl_address",))
        assert "12 Privacy Lane" not in report.render()
        assert "lbl_address" in report.refused

    def test_wildcard_still_refuses_identifying_ids(self):
        """"Show me everything" is the most likely way this gets misused."""
        out = inspect_source(SOURCE, text_for=("*",)).render()
        assert "08:00PM" in out and "Clock In" in out
        assert "Alice Example" not in out
        assert "12 Privacy Lane" not in out

    def test_explicit_override_exists_but_must_be_deliberate(self):
        out = inspect_source(SOURCE, text_for=("lbl_patient_name",),
                             allow_phi_text=True).render()
        assert "Alice Example" in out


class TestMarkerHeuristic:
    @pytest.mark.parametrize("rid", [
        "lbl_patient_name", "lbl_address", "txt_phone", "lbl_dob",
        "edit_email", "lbl_member_id", "lbl_emergency_contact", "lbl_note",
    ])
    def test_identifying_ids_are_recognised(self, rid):
        assert looks_like_phi(rid) is True

    @pytest.mark.parametrize("rid", [
        "lbl_schedule_start_time", "btn_clock_in", "list_today_schedule",
        "lbl_header", "txtApply",
    ])
    def test_structural_ids_are_not(self, rid):
        assert looks_like_phi(rid) is False

    def test_a_new_field_is_caught_without_anyone_updating_a_list(self):
        """lbl_patient_phone does not exist yet; it would still be refused."""
        assert looks_like_phi("lbl_patient_phone") is True


class TestContentScrubbing:
    """An id-based rule cannot tell a safe header from an identifying one.

    lbl_header holds the agency name on one screen and
    "Detalle de Visita\\n<PATIENT>" on another. This was a real leak, caught
    only after the name had already been printed.
    """

    HEADER_SOURCE = f'''
    <android.widget.TextView class="android.widget.TextView" resource-id="{P}lbl_header" clickable="false" text="Detalle de Visita &#10;PACIENTE FICTICIA" bounds="[0,0][750,60]"/>
    '''

    def test_name_after_a_newline_is_not_printed(self):
        out = inspect_source(self.HEADER_SOURCE, text_for=("lbl_header",)).render()
        assert "PACIENTE" not in out
        assert "FICTICIA" not in out

    def test_the_label_half_is_still_shown(self):
        out = inspect_source(self.HEADER_SOURCE, text_for=("lbl_header",)).render()
        assert "Detalle de Visita" in out

    def test_reduction_is_reported_not_silent(self):
        report = inspect_source(self.HEADER_SOURCE, text_for=("lbl_header",))
        assert "lbl_header" in report.reduced

    def test_a_shouted_name_alone_is_redacted_entirely(self):
        src = f'<android.widget.TextView class="android.widget.TextView" resource-id="{P}lbl_thing" text="PACIENTE FICTICIA"/>'
        out = inspect_source(src, text_for=("lbl_thing",)).render()
        assert "PACIENTE" not in out

    def test_title_case_app_chrome_is_untouched(self):
        src = f'<android.widget.Button class="android.widget.Button" resource-id="{P}btn_clock_in" text="Registrar Entrada"/>'
        out = inspect_source(src, text_for=("btn_clock_in",)).render()
        assert "Registrar Entrada" in out

    def test_single_caps_word_is_not_treated_as_a_name(self):
        src = f'<android.widget.TextView class="android.widget.TextView" resource-id="{P}lbl_x" text="OK"/>'
        out = inspect_source(src, text_for=("lbl_x",)).render()
        assert "OK" in out

    def test_explicit_override_still_shows_everything(self):
        out = inspect_source(self.HEADER_SOURCE, text_for=("lbl_header",),
                             allow_phi_text=True).render()
        assert "PACIENTE" in out


class TestDistinctValuesSurvivedCollapsing:
    """Collapsing repeated rows must not collapse their values.

    The EVV chooser has two label_title rows, GPS and token de seguridad. The
    first version of this renderer reported only "GPS" — it hid the alternative
    that turned out to matter most.
    """

    CHOOSER = f'''
    <android.widget.TextView class="android.widget.TextView" resource-id="{P}label_title" clickable="false" text="GPS"/>
    <android.widget.TextView class="android.widget.TextView" resource-id="{P}label_title" clickable="false" text="token de seguridad"/>
    <android.widget.Button class="android.widget.Button" resource-id="{P}button_cancel" clickable="true" text="Anular"/>
    '''

    def test_both_options_are_shown(self):
        out = inspect_source(self.CHOOSER, text_for=("label_title",)).render()
        assert "GPS" in out
        assert "token de seguridad" in out

    def test_the_row_is_still_collapsed_to_one_line(self):
        out = inspect_source(self.CHOOSER, text_for=("label_title",)).render()
        assert out.count("label_title") == 1

    def test_count_still_reports_how_many(self):
        out = inspect_source(self.CHOOSER, text_for=("label_title",)).render()
        line = [x for x in out.splitlines() if "label_title" in x][0]
        assert " 2 " in line

    def test_identical_values_are_not_repeated(self):
        src = f'''
        <android.widget.TextView class="android.widget.TextView" resource-id="{P}lbl_x" text="Same"/>
        <android.widget.TextView class="android.widget.TextView" resource-id="{P}lbl_x" text="Same"/>
        '''
        out = inspect_source(src, text_for=("lbl_x",)).render()
        line = [x for x in out.splitlines() if "lbl_x" in x][0]
        assert line.count("Same") == 1
