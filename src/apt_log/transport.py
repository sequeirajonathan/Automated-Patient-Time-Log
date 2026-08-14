"""REQ-5.4.1 — the development transport exception.

REQ-5.4 rejects adb-over-TCP outright: a network-attached phone could be anywhere,
which breaks the gate's central claim that the phone is physically attached to *this*
controller. That rejection lives in `probe.usb_devices()` and is the default code
path here. Nothing in this module weakens it.

`dev` mode exists so work can continue when no USB data link is available. It is
contained exactly as `StubLocationSource` is, for exactly the same reason: it lets
the gate reach a verdict it did not earn from observation, so every record it
touches has to say so, and production must refuse to run on it.

The containment, all of it load-bearing:

- default is `usb`
- `dev` is switched on only by a file under /etc/aptlog — deliberately not a CLI
  flag, not an environment variable, not an argument any caller can pass. Turning
  it on is an act on the device, by someone with root, who meant it.
- `transport_mode` is stamped into every audit record (REQ-7) and cannot be omitted
- `--production` refuses entirely while it is active (REQ-0)
"""

from __future__ import annotations

import logging
import os
import re
import stat as stat_module
from enum import StrEnum
from pathlib import Path

from apt_log.probe import _adb, usb_devices

log = logging.getLogger(__name__)

# Deliberately a module constant rather than anything injectable at runtime. Tests
# patch it; production code never passes a different path.
CONFIG_PATH = Path("/etc/aptlog/transport.conf")

_CONFIG_KEY = "TRANSPORT_MODE"
_TCP_SERIAL = re.compile(r"^.+:\d+$")


class TransportMode(StrEnum):
    USB = "usb"
    DEV = "dev"


class ProductionRefused(RuntimeError):
    """Raised when --production is attempted on a development affordance (REQ-0)."""


def _stat_config() -> os.stat_result | None:
    """Isolated so the ownership rules can be tested without a real chown."""
    try:
        return CONFIG_PATH.stat()
    except OSError:
        return None


def _config_is_trustworthy() -> bool:
    """Refuse a transport.conf that a non-root account could have written.

    The justification for `dev` existing at all is that enabling it is an act on
    the device by someone with root. This module enforces that itself rather than
    trusting the bootstrap to have run: firstboot.sh creating /etc/aptlog as
    root:root 0755 is a second layer, not the only one. An agent that could write
    its own config could switch off its own containment.
    """
    st = _stat_config()
    if st is None:
        return False
    if st.st_uid != 0:
        log.error(
            "REFUSING %s: owned by uid %d, not root. The dev transport exception "
            "requires a root-owned config (REQ-5.4.1); falling back to usb.",
            CONFIG_PATH, st.st_uid,
        )
        return False
    if st.st_mode & (stat_module.S_IWGRP | stat_module.S_IWOTH):
        log.error(
            "REFUSING %s: mode %o is group- or world-writable, so a non-root "
            "account could enable dev transport; falling back to usb.",
            CONFIG_PATH, stat_module.S_IMODE(st.st_mode),
        )
        return False
    return True


def resolve_transport_mode() -> TransportMode:
    """Read the transport mode from config. Never from argv or the environment.

    Any absence, parse failure, unrecognised value, or untrustworthy ownership
    resolves to USB. The safe direction is the strict one: a corrupt or
    attacker-writable config must not silently unlock the permissive mode.
    """
    if not _config_is_trustworthy():
        return TransportMode.USB

    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return TransportMode.USB

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != _CONFIG_KEY:
            continue
        candidate = value.strip().strip("\"'").lower()
        if candidate == TransportMode.DEV.value:
            return TransportMode.DEV
        return TransportMode.USB
    return TransportMode.USB


def _authorised_devices_including_tcp() -> list[str]:
    """Every authorised device, TCP included. Only reachable in dev mode."""
    serials = []
    for line in _adb("devices").stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def attached_devices() -> tuple[list[str], TransportMode]:
    """Devices usable for automation, and the mode that decided it.

    The mode is returned rather than looked up separately so a caller cannot
    record a device list under the wrong transport_mode.
    """
    mode = resolve_transport_mode()
    if mode is TransportMode.USB:
        return usb_devices(), mode
    return _authorised_devices_including_tcp(), mode


def is_tcp_serial(serial: str) -> bool:
    return bool(_TCP_SERIAL.match(serial))


def transport_precondition_satisfied(mode: TransportMode | None = None) -> bool:
    """REQ-5.2's transport half of the passing condition.

    In `dev` this is treated as satisfied so the check-off flow, scheduler, audit
    trail and signature path can be exercised end to end against a real device.
    That concession is the entire reason for the containment above — and it is why
    a `dev` run can never sign off REQ-5 itself (REQ-5.4.1, "What dev cannot do").
    """
    mode = resolve_transport_mode() if mode is None else mode
    if mode is TransportMode.DEV:
        return True
    return bool(usb_devices())


def assert_production_allowed(
    mode: TransportMode | None = None,
    location_source_type: str | None = None,
) -> None:
    """REQ-0. Raises unless every development affordance is off.

    Both conditions are refusals for the same reason: each lets the gate reach a
    verdict it did not observe.
    """
    mode = resolve_transport_mode() if mode is None else mode
    if mode is not TransportMode.USB:
        raise ProductionRefused(
            f"--production refused: transport_mode is '{mode.value}', not 'usb'. "
            "A TCP-attached phone cannot establish that it is physically attached "
            "to this controller (REQ-5.4.1)."
        )
    if location_source_type and "stub" in location_source_type.lower():
        raise ProductionRefused(
            f"--production refused: location source is '{location_source_type}'. "
            "Stub fixtures cannot attest to an observed position (REQ-5.7)."
        )
