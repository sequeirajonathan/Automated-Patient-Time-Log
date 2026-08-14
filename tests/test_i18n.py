"""REQ-11 — the bilingual contract.

The parity tests are the point. A key added in English and forgotten in Spanish
is invisible in review and shows up as an English sentence on a Spanish page,
which is exactly the failure this project cannot afford: the caregiver is making
decisions about patient records in her second language.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from apt_log.ui.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED,
    Translator,
    catalog_keys,
    load_catalog,
    normalise,
)

PLACEHOLDER = re.compile(r"\{(\w+)\}")


class TestCatalogParity:
    def test_both_catalogs_have_identical_keys(self):
        en, es = catalog_keys("en"), catalog_keys("es")
        assert en - es == set(), f"missing from Spanish: {sorted(en - es)}"
        assert es - en == set(), f"missing from English: {sorted(es - en)}"

    def test_no_value_is_empty(self):
        for lang in SUPPORTED:
            for key, value in load_catalog(lang).items():
                assert value.strip(), f"{lang}:{key} is empty"

    def test_placeholders_match_across_languages(self):
        """A translation that drops {patient} renders a sentence naming nobody."""
        en, es = load_catalog("en"), load_catalog("es")
        for key in en:
            assert set(PLACEHOLDER.findall(en[key])) == set(PLACEHOLDER.findall(es[key])), \
                f"placeholder mismatch in {key}"

    def test_spanish_is_actually_translated(self):
        """Guards against a catalog copied from English and never filled in."""
        en, es = load_catalog("en"), load_catalog("es")
        content = [k for k in en if not k.startswith(("format.", "common.none"))]
        identical = [k for k in content if en[k] == es[k]]
        # A handful of proper nouns and symbols legitimately match.
        assert len(identical) < len(content) * 0.1, f"suspiciously untranslated: {identical}"


class TestNoFragmentConcatenation:
    """Sentences are whole, so word order survives translation."""

    def test_user_facing_sentences_are_not_bare_fragments(self):
        # Keys that interpolate data must carry the surrounding sentence with
        # them, not just the data.
        for lang in SUPPORTED:
            cat = load_catalog(lang)
            for key, value in cat.items():
                if PLACEHOLDER.search(value):
                    stripped = PLACEHOLDER.sub("", value).strip()
                    assert len(stripped) > 3, (
                        f"{lang}:{key} is a bare placeholder — the sentence around "
                        "it must live in the catalog, not in a template"
                    )

    def test_key_sentences_carry_their_own_context(self):
        for lang in SUPPORTED:
            cat = load_catalog(lang)
            for key in ("today.diverged", "signature.prompt",
                        "attention.gate_failed", "attention.no_signature"):
                assert len(cat[key]) > 30, f"{lang}:{key} looks like a fragment"


class TestTranslator:
    def test_interpolates_named_parameters(self):
        t = Translator("es")
        out = t("signature.prompt", patient="PT-0042", time="14:00")
        assert "PT-0042" in out and "14:00" in out
        assert "Firme" in out

    def test_english_and_spanish_differ(self):
        params = {"patient": "PT-1", "time": "09:00"}
        assert Translator("en").t("signature.prompt", **params) != \
               Translator("es").t("signature.prompt", **params)

    def test_missing_key_degrades_to_the_key_rather_than_raising(self):
        """A blank panel is worse than an ugly one."""
        assert Translator("es").t("no.such.key") == "no.such.key"

    def test_missing_placeholder_renders_the_sentence_not_an_exception(self):
        out = Translator("en").t("signature.prompt", patient="PT-1")
        assert "{time}" in out or "PT-1" in out


class TestNormalise:
    @pytest.mark.parametrize("given,expected", [
        ("es", "es"), ("ES", "es"), ("es-MX", "es"), ("es-419,es;q=0.9", "es"),
        ("en", "en"), ("en-US", "en"),
        (None, DEFAULT_LANGUAGE), ("", DEFAULT_LANGUAGE), ("de", DEFAULT_LANGUAGE),
    ])
    def test_accepts_what_browsers_actually_send(self, given, expected):
        assert normalise(given) == expected

    def test_default_is_the_caregivers_language(self):
        assert DEFAULT_LANGUAGE == "es"


class TestFormatting:
    DT = datetime(2026, 8, 14, 14, 5)

    def test_date_order_differs_by_locale(self):
        assert Translator("en").date(self.DT) == "08/14/2026"
        assert Translator("es").date(self.DT) == "14/08/2026"

    def test_time_is_24_hour_in_spanish(self):
        assert Translator("es").time(self.DT) == "14:05"
        assert "PM" in Translator("en").time(self.DT)

    def test_none_renders_as_a_dash_not_the_word_none(self):
        assert Translator("es").datetime(None) == "—"

    def test_decimal_separator_follows_locale(self):
        assert Translator("en").number(1234.5, 1) == "1,234.5"
        assert Translator("es").number(1234.5, 1) == "1.234,5"


class TestSafetyCriticalStringsExist:
    """REQ-10.3 and REQ-11: she cannot attest to what she cannot read."""

    @pytest.mark.parametrize("key", [
        "signature.prompt", "signature.instruction", "signature.expired",
        "gate.refused", "gate.dev_transport",
        "attention.gate_failed", "attention.no_signature", "attention.run_failed",
        "control.paused_notice",
    ])
    def test_present_and_translated_in_both(self, key):
        en, es = load_catalog("en"), load_catalog("es")
        assert key in en and key in es
        assert en[key] != es[key]
