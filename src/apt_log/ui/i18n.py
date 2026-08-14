"""REQ-11 — bilingual interface.

Two rules shape this, and both come from the same place: the person reading this
UI is making decisions about patient records in her second language.

**No string is built from fragments.** Every key holds a complete sentence with
named placeholders. Concatenating "Visit for " + name + " at " + time produces
English word order with Spanish words in it — grammatical in neither. It is also
untranslatable, because the translator never sees the whole sentence.

**A missing translation is a test failure, not a runtime surprise.** The catalogs
are asserted to have identical key sets, so a key added in English and forgotten
in Spanish fails in CI rather than rendering an English string into a Spanish page
at 2am. At runtime a missing key degrades to the key name and logs loudly, because
a blank panel is worse than an ugly one.

Date and number formats live in the catalogs rather than in code, so a locale that
writes 14/08 instead of 08/14 is a data change.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LANGUAGE = "es"          # the caregiver's language; English is the fallback
FALLBACK_LANGUAGE = "en"
SUPPORTED = ("en", "es")


class MissingTranslation(KeyError):
    """Raised only by the strict loader used in tests."""


@lru_cache(maxsize=None)
def load_catalog(language: str) -> dict[str, str]:
    if language not in SUPPORTED:
        raise ValueError(f"unsupported language {language!r}; expected {SUPPORTED}")
    path = LOCALES_DIR / f"{language}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def normalise(language: str | None) -> str:
    """Accept 'es-MX', 'ES', None — anything a browser or a cookie might carry."""
    if not language:
        return DEFAULT_LANGUAGE
    code = language.split(",")[0].split("-")[0].strip().lower()
    return code if code in SUPPORTED else DEFAULT_LANGUAGE


class Translator:
    """Bound to one language. Templates get one of these, not the raw dict."""

    def __init__(self, language: str):
        self.language = normalise(language)
        self._catalog = load_catalog(self.language)
        self._fallback = load_catalog(FALLBACK_LANGUAGE)

    def __call__(self, key: str, **params) -> str:
        return self.t(key, **params)

    def t(self, key: str, **params) -> str:
        template = self._catalog.get(key)
        if template is None:
            template = self._fallback.get(key)
            if template is None:
                log.error("missing translation key %r in %s and fallback",
                          key, self.language)
                return key
            log.error("missing %r in %s — rendered in %s",
                      key, self.language, FALLBACK_LANGUAGE)
        try:
            return template.format(**params) if params else template
        except KeyError as exc:
            # A placeholder the caller did not supply. Show the raw sentence
            # rather than an exception on a page someone is relying on.
            log.error("key %r wants placeholder %s", key, exc)
            return template

    # ------------------------------------------------------------ formatting
    def datetime(self, value: datetime | None) -> str:
        if value is None:
            return self.t("common.none")
        return value.strftime(self.t("format.datetime"))

    def time(self, value: datetime | None) -> str:
        if value is None:
            return self.t("common.none")
        return value.strftime(self.t("format.time"))

    def date(self, value: datetime | None) -> str:
        if value is None:
            return self.t("common.none")
        return value.strftime(self.t("format.date"))

    def number(self, value: float, decimals: int = 0) -> str:
        """Swap separators per locale — 1,234.5 in en, 1.234,5 in es."""
        formatted = f"{value:,.{decimals}f}"
        if self.t("format.decimal_separator") == ",":
            formatted = formatted.translate(str.maketrans({",": ".", ".": ","}))
        return formatted


def catalog_keys(language: str) -> set[str]:
    return set(load_catalog(language).keys())
