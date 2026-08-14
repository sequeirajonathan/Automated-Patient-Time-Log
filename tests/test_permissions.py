"""Android runtime permission dialogs.

Platform UI, so it renders in the system locale. Everything here keys on
framework resource-ids for that reason — the production phone's dialogs will be
in Spanish.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apt_log.screens.permissions import (
    PermissionsScreen,
    grant_pending_permissions,
)

PC = "com.android.permissioncontroller:id/"
PI = "com.android.packageinstaller:id/"


def _el(text=""):
    e = MagicMock()
    e.text = text
    return e


class FakeDriver:
    def __init__(self, ids: dict[str, list], activity=".permission.ui.GrantPermissionsActivity"):
        self.ids = ids
        self.current_activity = activity

    def find_elements(self, _by, value):
        return self.ids.get(value, [])


class TestDetection:
    def test_detects_by_activity(self):
        assert PermissionsScreen(FakeDriver({})).is_displayed() is True

    def test_detects_by_allow_button_on_a_renamed_activity(self):
        d = FakeDriver({PC + "permission_allow_button": [_el()]}, activity=".Other")
        assert PermissionsScreen(d).is_displayed() is True

    def test_absent_when_neither(self):
        assert PermissionsScreen(FakeDriver({}, activity=".Main")).is_displayed() is False


class TestPreferenceOrder:
    def test_prefers_allow_always_over_foreground(self):
        always = _el()
        fg = _el()
        d = FakeDriver({
            PC + "permission_allow_always_button": [always],
            PC + "permission_allow_foreground_only_button": [fg],
        })
        assert PermissionsScreen(d).allow() == "permission_allow_always_button"
        always.click.assert_called_once()
        fg.click.assert_not_called()

    def test_falls_back_to_foreground_only(self):
        fg = _el()
        d = FakeDriver({PC + "permission_allow_foreground_only_button": [fg]})
        assert PermissionsScreen(d).allow() == "permission_allow_foreground_only_button"
        fg.click.assert_called_once()

    def test_plain_allow_for_non_location_permissions(self):
        allow = _el()
        d = FakeDriver({PC + "permission_allow_button": [allow]})
        assert PermissionsScreen(d).allow() == "permission_allow_button"
        allow.click.assert_called_once()

    def test_supports_the_pre_android_10_package_name(self):
        allow = _el()
        d = FakeDriver({PI + "permission_allow_button": [allow]})
        assert PermissionsScreen(d).allow() == "permission_allow_button"


class TestNeverPressed:
    def test_one_time_is_never_chosen(self):
        """It re-prompts next launch, and nobody is there to answer."""
        once = _el()
        fg = _el()
        d = FakeDriver({
            PC + "permission_allow_one_time_button": [once],
            PC + "permission_allow_foreground_only_button": [fg],
        })
        PermissionsScreen(d).allow()
        once.click.assert_not_called()
        fg.click.assert_called_once()

    def test_deny_is_never_chosen(self):
        deny = _el()
        allow = _el()
        d = FakeDriver({
            PC + "permission_deny_button": [deny],
            PC + "permission_allow_button": [allow],
        })
        PermissionsScreen(d).allow()
        deny.click.assert_not_called()
        allow.click.assert_called_once()

    def test_refuses_when_only_one_time_is_offered(self):
        """Better to fail loudly than to accept a grant that expires."""
        d = FakeDriver({PC + "permission_allow_one_time_button": [_el()]})
        with pytest.raises(RuntimeError, match="refusing to press"):
            PermissionsScreen(d).allow()

    def test_refuses_an_unrecognised_dialog(self):
        d = FakeDriver({PC + "permission_deny_button": [_el()]})
        with pytest.raises(RuntimeError, match="none of the expected allow"):
            PermissionsScreen(d).allow()


class TestPreciseLocation:
    def test_selects_precise_before_allowing(self):
        fine = _el()
        fg = _el()
        d = FakeDriver({
            PC + "permission_location_accuracy_radio_fine": [fine],
            PC + "permission_allow_foreground_only_button": [fg],
        })
        PermissionsScreen(d).allow()
        fine.click.assert_called_once()
        fg.click.assert_called_once()

    def test_absent_precise_control_is_not_an_error(self):
        d = FakeDriver({PC + "permission_allow_button": [_el()]})
        PermissionsScreen(d).allow()


class TestGrantChain:
    def test_grants_consecutive_dialogs(self):
        class Chain:
            def __init__(self, n):
                self.left = n
                self.current_activity = ".permission.ui.GrantPermissionsActivity"

            def find_elements(self, _by, value):
                if self.left <= 0:
                    self.current_activity = ".MainActivity"
                    return []
                if value.endswith("permission_allow_button"):
                    el = MagicMock()
                    el.text = ""
                    el.click.side_effect = self._next
                    return [el]
                if value.endswith("permission_message"):
                    return [_el(f"permission {self.left}")]
                return []

            def _next(self, *_a, **_k):
                self.left -= 1

        d = Chain(3)
        granted = grant_pending_permissions(d, settle=0)
        assert len(granted) == 3

    def test_no_dialog_is_a_no_op(self):
        assert grant_pending_permissions(FakeDriver({}, activity=".Main"), settle=0) == []

    def test_a_dialog_that_will_not_dismiss_raises(self):
        d = FakeDriver({PC + "permission_allow_button": [_el()]})
        with pytest.raises(RuntimeError, match="still on a permission dialog"):
            grant_pending_permissions(d, max_dialogs=3, settle=0)


class TestDescribe:
    def test_reports_the_request_for_the_log(self):
        d = FakeDriver({PC + "permission_message": [_el("Allow app to access location?")]})
        assert "location" in PermissionsScreen(d).describe()

    def test_unknown_when_no_message_element(self):
        assert PermissionsScreen(FakeDriver({})).describe() == "unknown permission"
