"""Session expiry, and the dialogs that are not it.

The wording captured from the device is the one the app actually renders:
"Debes de iniciar sesión." above a single "DE ACUERDO".
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apt_log.screens.session import (
    DISMISS,
    MESSAGE,
    UnknownDialog,
    dismiss,
    interrupted,
    is_expired,
    modal_message,
)

EXPIRED_ES = "Debes de iniciar sesión."
EXPIRED_EN = "You must log in."


def _el(text=""):
    e = MagicMock()
    e.text = text
    return e


class FakeDriver:
    def __init__(self, message=None, has_button=True):
        self.message = message
        self.has_button = has_button
        self.button = _el("DE ACUERDO")

    def find_elements(self, by, value):
        if value == MESSAGE:
            return [_el(self.message)] if self.message is not None else []
        if value == DISMISS:
            return [self.button] if self.has_button else []
        return []


class TestDetection:
    def test_recognises_the_wording_from_the_device(self):
        assert is_expired(FakeDriver(EXPIRED_ES)) is True

    def test_recognises_the_english_wording_too(self):
        assert is_expired(FakeDriver(EXPIRED_EN)) is True

    def test_no_modal_is_not_an_expiry(self):
        assert is_expired(FakeDriver()) is False
        assert interrupted(FakeDriver()) is False

    def test_an_empty_message_is_not_an_expiry(self):
        assert is_expired(FakeDriver("")) is False

    def test_case_and_spacing_do_not_matter(self):
        assert is_expired(FakeDriver("  DEBES DE\n INICIAR SESIÓN. ")) is True

    def test_a_different_alert_is_not_an_expiry(self):
        """The two ids are this app's generic alert shape, not an expiry marker."""
        d = FakeDriver("No hay visitas programadas para hoy.")
        assert is_expired(d) is False
        assert interrupted(d) is True


class TestDismiss:
    def test_taps_the_button_and_reports_what_it_cleared(self):
        d = FakeDriver(EXPIRED_ES)
        assert dismiss(d) == EXPIRED_ES
        d.button.click.assert_called_once()

    def test_refuses_an_unrecognised_modal_without_tapping(self):
        """"DE ACUERDO" means "I agree". Nothing here has read what it would be
        agreeing to, so it does not press it."""
        d = FakeDriver("Se eliminarán los registros no enviados.")
        with pytest.raises(UnknownDialog):
            dismiss(d)
        d.button.click.assert_not_called()

    def test_the_unknown_message_reaches_the_operator(self):
        d = FakeDriver("Algo inesperado")
        with pytest.raises(UnknownDialog) as exc:
            dismiss(d)
        assert "Algo inesperado" in str(exc.value)
        assert exc.value.message == "Algo inesperado"

    def test_refuses_when_there_is_no_modal_at_all(self):
        with pytest.raises(UnknownDialog):
            dismiss(FakeDriver())

    def test_refuses_when_the_expected_button_is_missing(self):
        """Right message, no button — the layout changed, so this is not the
        dialog this module was written against."""
        with pytest.raises(UnknownDialog):
            dismiss(FakeDriver(EXPIRED_ES, has_button=False))


class TestInterrupted:
    def test_any_modal_counts_as_an_interruption(self):
        assert interrupted(FakeDriver("cualquier cosa")) is True

    def test_separate_question_from_whether_it_is_expiry(self):
        """A caller mid-flow needs "was I interrupted" before "why" — an
        unrecognised interruption is a stop, not a retry."""
        d = FakeDriver("Mensaje desconocido")
        assert interrupted(d) is True
        assert is_expired(d) is False


class TestMessage:
    def test_reads_the_message_for_the_audit_trail(self):
        assert modal_message(FakeDriver(EXPIRED_ES)) == EXPIRED_ES

    def test_empty_when_nothing_is_showing(self):
        assert modal_message(FakeDriver()) == ""
