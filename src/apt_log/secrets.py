"""REQ-3 — credential access.

File-backed rather than an OS keychain: this machine boots unattended with nobody
present to unlock anything, so a keychain has no one to ask. That makes this
obfuscation against casual access, not protection against someone holding the SD
card — PI_SETUP.md records that as a conscious acceptance.

What it *can* do is refuse to read a file the rest of the machine can also read,
and never let a secret reach a log or a process listing.
"""

from __future__ import annotations

import logging
import os
import stat as stat_module
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

SECRETS_PATH = Path("/etc/aptlog/secrets.env")

# Keys this system expects. Named here so a typo fails loudly at the call site
# rather than returning None into a login flow.
APP_USERNAME = "APP_USERNAME"      # legacy HHAeXchange
APP_PASSWORD = "APP_PASSWORD"
PHONE_PIN = "PHONE_PIN"

# The other agency apps, profiled in the first discovery session. Each auth
# macro exists only if its secrets do — see macros.auth_macro_for.
UMA_USERNAME = "UMA_USERNAME"      # HHAeXchange+ signs in through a web form
UMA_PASSWORD = "UMA_PASSWORD"
MC_PIN = "MC_PIN"                  # Mobile Caregiver+ guards its session
                                   # with a numeric passcode keypad
INMYTEAM_PHONE = "INMYTEAM_PHONE"  # inMyTeam signs in with a cell number
                                   # (walked live: "Sign in with your phone
                                   # number" — the SMS code lands on the
                                   # tethered phone itself)


class SecretNotFound(KeyError):
    """Raised rather than returning None, so a missing secret cannot be typed."""


class SecretProvider(Protocol):
    def get(self, key: str) -> str: ...


class FileSecretProvider:
    """Reads `KEY=value` lines from a 0600 file owned by the reading user."""

    def __init__(self, path: Path = SECRETS_PATH, require_secure_mode: bool = True):
        self._path = path
        self._require_secure_mode = require_secure_mode
        self._cache: dict[str, str] | None = None

    def _check_mode(self) -> None:
        st = self._path.stat()
        if st.st_mode & (stat_module.S_IRGRP | stat_module.S_IROTH):
            raise PermissionError(
                f"{self._path} is mode {stat_module.S_IMODE(st.st_mode):o}; it holds "
                "credentials and must not be group- or world-readable. chmod 0600."
            )
        if st.st_uid != os.geteuid() and os.geteuid() != 0:
            raise PermissionError(
                f"{self._path} is owned by uid {st.st_uid}, not the reading user."
            )

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if self._require_secure_mode:
            self._check_mode()
        values: dict[str, str] = {}
        for raw in self._path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("\"'")
        self._cache = values
        return values

    def get(self, key: str) -> str:
        try:
            values = self._load()
        except FileNotFoundError as exc:
            raise SecretNotFound(
                f"{self._path} does not exist; no credentials are configured"
            ) from exc
        if key not in values or not values[key]:
            # Deliberately does not echo the file's contents or other key names.
            raise SecretNotFound(f"{key} is not set in {self._path}")
        return values[key]


class MemorySecretProvider:
    """For tests. Never reads or writes a file."""

    def __init__(self, **values: str):
        self._values = values

    def get(self, key: str) -> str:
        if key not in self._values:
            raise SecretNotFound(key)
        return self._values[key]
