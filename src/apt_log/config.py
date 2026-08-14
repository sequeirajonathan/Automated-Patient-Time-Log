"""Site-specific configuration.

Everything here describes one deployment — which agency, which building, which
router — and none of it belongs in a public repository. It lives on the device,
in the same root-owned directory as the credentials.

That is not only a privacy rule. These values are also what REQ-5 checks presence
against, so a gateway MAC committed to git is a gateway MAC that outlives the
router it describes and quietly starts asserting the wrong thing.

Missing values return None rather than a default. A geofence with a made-up
centre would pass a check it never actually performed.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

SITE_CONFIG_PATH = Path("/etc/aptlog/site.conf")

# Known keys, documented here rather than scattered through the code.
AGENCY_NAME = "AGENCY_NAME"        # exact label as the app shows it
SITE_LAT = "SITE_LAT"
SITE_LON = "SITE_LON"
GEOFENCE_M = "GEOFENCE_M"
GATEWAY_MAC = "GATEWAY_MAC"        # REQ-5 strong anchor
WIFI_BSSID = "WIFI_BSSID"          # REQ-5 strong anchor
HEARTBEAT_URL = "HEARTBEAT_URL"


class MissingConfig(KeyError):
    """A required site value is not configured."""


@lru_cache(maxsize=1)
def _load(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        log.info("no site config at %s", path)
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def reload() -> None:
    _load.cache_clear()


def _resolve(path: Path | str | None) -> str:
    """Read the module global at call time, not at import.

    A default argument would snapshot SITE_CONFIG_PATH when this module is first
    imported, which makes the location impossible to redirect afterwards — for a
    test, or for a deployment that keeps its config somewhere else.
    """
    return str(path if path is not None else SITE_CONFIG_PATH)


def get(key: str, path: Path | str | None = None) -> str | None:
    return _load(_resolve(path)).get(key) or None


def require(key: str, path: Path | str | None = None) -> str:
    """For values whose absence must stop the run rather than be guessed."""
    value = get(key, path)
    if not value:
        raise MissingConfig(
            f"{key} is not set in {_resolve(path)}. This value is specific to "
            "the deployment and cannot be inferred."
        )
    return value


def get_float(key: str, path: Path | str | None = None) -> float | None:
    raw = get(key, path)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        log.error("%s is not a number: %r", key, raw)
        return None
