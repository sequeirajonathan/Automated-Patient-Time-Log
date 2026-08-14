"""REQ-3 — the credential store.

File-backed because a headless Pi has no keychain to unlock. That makes the file
permissions the only thing standing between the credentials and any other account
on the box, so the provider checks them rather than assuming the installer got it
right.
"""

from __future__ import annotations

import os

import pytest

from apt_log.secrets import (
    APP_PASSWORD,
    APP_USERNAME,
    FileSecretProvider,
    MemorySecretProvider,
    SecretNotFound,
)


@pytest.fixture
def secrets_file(tmp_path):
    path = tmp_path / "secrets.env"

    def _write(text: str, mode: int = 0o600):
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path

    return _write


class TestReading:
    def test_reads_key_value_pairs(self, secrets_file):
        path = secrets_file(f"{APP_USERNAME}=nurse1\n{APP_PASSWORD}=hunter2\n")
        provider = FileSecretProvider(path)
        assert provider.get(APP_USERNAME) == "nurse1"
        assert provider.get(APP_PASSWORD) == "hunter2"

    def test_ignores_comments_and_blank_lines(self, secrets_file):
        path = secrets_file(f"# a comment\n\n{APP_USERNAME}=nurse1\n")
        assert FileSecretProvider(path).get(APP_USERNAME) == "nurse1"

    def test_strips_surrounding_quotes(self, secrets_file):
        path = secrets_file(f'{APP_PASSWORD}="p@ss word"\n')
        assert FileSecretProvider(path).get(APP_PASSWORD) == "p@ss word"


class TestMissingSecretsRaise:
    """Raising beats returning None: a None cannot be typed into a login form."""

    def test_missing_key_raises(self, secrets_file):
        path = secrets_file(f"{APP_USERNAME}=nurse1\n")
        with pytest.raises(SecretNotFound, match=APP_PASSWORD):
            FileSecretProvider(path).get(APP_PASSWORD)

    def test_empty_value_raises(self, secrets_file):
        path = secrets_file(f"{APP_PASSWORD}=\n")
        with pytest.raises(SecretNotFound):
            FileSecretProvider(path).get(APP_PASSWORD)

    def test_missing_file_raises_without_leaking_the_path_contents(self, tmp_path):
        with pytest.raises(SecretNotFound, match="does not exist"):
            FileSecretProvider(tmp_path / "absent.env").get(APP_PASSWORD)


class TestPermissions:
    def test_group_readable_file_is_refused(self, secrets_file):
        path = secrets_file(f"{APP_PASSWORD}=hunter2\n", mode=0o640)
        with pytest.raises(PermissionError, match="group- or world-readable"):
            FileSecretProvider(path).get(APP_PASSWORD)

    def test_world_readable_file_is_refused(self, secrets_file):
        path = secrets_file(f"{APP_PASSWORD}=hunter2\n", mode=0o644)
        with pytest.raises(PermissionError, match="group- or world-readable"):
            FileSecretProvider(path).get(APP_PASSWORD)

    def test_0600_is_accepted(self, secrets_file):
        path = secrets_file(f"{APP_PASSWORD}=hunter2\n", mode=0o600)
        assert FileSecretProvider(path).get(APP_PASSWORD) == "hunter2"

    def test_check_can_be_waived_only_explicitly(self, secrets_file):
        path = secrets_file(f"{APP_PASSWORD}=hunter2\n", mode=0o644)
        provider = FileSecretProvider(path, require_secure_mode=False)
        assert provider.get(APP_PASSWORD) == "hunter2"


class TestErrorsDoNotLeak:
    def test_missing_key_message_names_no_other_keys(self, secrets_file):
        path = secrets_file(f"{APP_USERNAME}=nurse1\nPHONE_PIN=1234\n")
        with pytest.raises(SecretNotFound) as exc:
            FileSecretProvider(path).get(APP_PASSWORD)
        assert "1234" not in str(exc.value)
        assert "nurse1" not in str(exc.value)


class TestMemoryProvider:
    def test_returns_configured_values(self):
        assert MemorySecretProvider(A="1").get("A") == "1"

    def test_missing_raises(self):
        with pytest.raises(SecretNotFound):
            MemorySecretProvider().get("A")
