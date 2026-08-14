"""Agency selection.

The account carries a former employer alongside the current one, so this is the
screen where a wrong tap records a visit against the wrong company. Names below
are invented — the real one is site config, not repository content.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apt_log.screens.agency import AgencyError, AgencyScreen

LIST = "com.hhaexchange.caregiver:id/list_agencies"
LABEL = "com.hhaexchange.caregiver:id/label_agency_name"

# Mirrors the real layout: two employers, the wanted one first, and both share
# the words "Home" and "Care".
WANTED = "Example Home Care, Inc."
OTHER = "Sample Home Health Care, Inc."


def _row(text, checked="false"):
    el = MagicMock()
    el.text = f"{text} ({text})"          # the app doubles the name
    el.get_attribute.return_value = checked
    return el


class FakeDriver:
    def __init__(self, rows, activity=".AgencySelectionActivity"):
        self.rows = rows
        self.current_activity = activity

    def find_elements(self, _by, value):
        if value == LABEL:
            return self.rows
        if value == LIST:
            return [MagicMock()] if self.rows else []
        return []


class TestDetection:
    def test_detects_by_activity(self):
        assert AgencyScreen(FakeDriver([])).is_displayed() is True

    def test_detects_by_list_on_a_renamed_activity(self):
        d = FakeDriver([_row(WANTED)], activity=".Other")
        assert AgencyScreen(d).is_displayed() is True

    def test_absent_elsewhere(self):
        assert AgencyScreen(FakeDriver([], activity=".Main")).is_displayed() is False


class TestSelectsByNameNotPosition:
    def test_selects_the_configured_agency(self):
        wanted, other = _row(WANTED), _row(OTHER)
        d = FakeDriver([wanted, other], activity=".NextActivity")
        AgencyScreen(d).select(WANTED)
        wanted.click.assert_called_once()
        other.click.assert_not_called()

    def test_selects_it_even_when_it_is_not_first(self):
        """An index would silently switch employers if the order changed."""
        wanted, other = _row(WANTED), _row(OTHER)
        d = FakeDriver([other, wanted], activity=".NextActivity")
        AgencyScreen(d).select(WANTED)
        wanted.click.assert_called_once()
        other.click.assert_not_called()

    def test_never_taps_the_former_employer(self):
        wanted, other = _row(WANTED), _row(OTHER)
        d = FakeDriver([wanted, other], activity=".NextActivity")
        AgencyScreen(d).select(WANTED)
        assert other.click.call_count == 0

    def test_matches_despite_wrapping_and_case(self):
        el = _row(WANTED)
        el.text = f"  {WANTED.upper()}  \n ({WANTED.upper()}) "
        d = FakeDriver([el], activity=".NextActivity")
        AgencyScreen(d).select(WANTED)
        el.click.assert_called_once()


class TestRefusesToGuess:
    def test_no_match_raises_and_lists_what_was_offered(self):
        d = FakeDriver([_row(OTHER)])
        with pytest.raises(AgencyError, match="no agency matching"):
            AgencyScreen(d).select(WANTED)

    def test_no_match_taps_nothing(self):
        other = _row(OTHER)
        d = FakeDriver([other])
        with pytest.raises(AgencyError):
            AgencyScreen(d).select(WANTED)
        other.click.assert_not_called()

    def test_ambiguous_match_raises(self):
        a, b = _row(WANTED), _row(WANTED)
        d = FakeDriver([a, b])
        with pytest.raises(AgencyError, match="Refusing to guess"):
            AgencyScreen(d).select(WANTED)
        a.click.assert_not_called()

    def test_empty_list_raises_rather_than_waiting_forever(self):
        with pytest.raises(AgencyError, match="empty"):
            AgencyScreen(FakeDriver([])).select(WANTED)

    def test_a_substring_of_another_agency_is_not_accepted_silently(self):
        """"Home Care" appears in both names; a loose match must not pass."""
        a, b = _row("Alpha Home Care"), _row("Beta Home Care")
        d = FakeDriver([a, b])
        with pytest.raises(AgencyError, match="Refusing to guess"):
            AgencyScreen(d).select("Home Care")


class TestVerification:
    def test_advancing_past_the_screen_counts_as_success(self):
        d = FakeDriver([_row(WANTED)], activity=".NextActivity")
        AgencyScreen(d).select(WANTED)

    def test_still_on_screen_and_unchecked_raises(self):
        """A tap that appeared to land is not evidence it did."""
        d = FakeDriver([_row(WANTED, checked="false")])
        with pytest.raises(AgencyError, match="not selected"):
            AgencyScreen(d).select(WANTED)

    def test_still_on_screen_but_checked_is_accepted(self):
        d = FakeDriver([_row(WANTED, checked="true")])
        AgencyScreen(d).select(WANTED)

class TestConfigSourced:
    def test_uses_the_configured_name_when_none_is_passed(self, tmp_path):
        from apt_log import config
        conf = tmp_path / "site.conf"
        conf.write_text(f"AGENCY_NAME={WANTED}\n", encoding="utf-8")
        config.reload()

        wanted, other = _row(WANTED), _row(OTHER)
        d = FakeDriver([wanted, other], activity=".NextActivity")
        with patch("apt_log.config.SITE_CONFIG_PATH", conf):
            AgencyScreen(d).select()
        wanted.click.assert_called_once()
        other.click.assert_not_called()
        config.reload()


class TestSiteConfig:
    """Site values live on the device, never in this repository."""

    def test_missing_file_yields_none_not_a_default(self, tmp_path):
        from apt_log import config
        config.reload()
        assert config.get(config.GATEWAY_MAC, tmp_path / "absent.conf") is None

    def test_require_raises_with_the_key_named(self, tmp_path):
        from apt_log import config
        config.reload()
        with pytest.raises(config.MissingConfig, match="AGENCY_NAME"):
            config.require(config.AGENCY_NAME, tmp_path / "absent.conf")

    def test_reads_key_value_pairs_ignoring_comments(self, tmp_path):
        from apt_log import config
        conf = tmp_path / "site.conf"
        conf.write_text("# a comment\n\nGEOFENCE_M = 100\nWIFI_BSSID='aa:bb'\n",
                        encoding="utf-8")
        config.reload()
        assert config.get(config.WIFI_BSSID, conf) == "aa:bb"
        assert config.get_float(config.GEOFENCE_M, conf) == 100.0
        config.reload()

    def test_a_non_numeric_value_is_none_rather_than_an_exception(self, tmp_path):
        from apt_log import config
        conf = tmp_path / "site.conf"
        conf.write_text("SITE_LAT=north\n", encoding="utf-8")
        config.reload()
        assert config.get_float(config.SITE_LAT, conf) is None
        config.reload()

    def test_empty_value_is_treated_as_absent(self, tmp_path):
        from apt_log import config
        conf = tmp_path / "site.conf"
        conf.write_text("AGENCY_NAME=\n", encoding="utf-8")
        config.reload()
        with pytest.raises(config.MissingConfig):
            config.require(config.AGENCY_NAME, conf)
        config.reload()
