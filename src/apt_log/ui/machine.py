"""What the two machines are doing — the Pi, and the phone on the end of it.

The health panel answers "is it working". This answers the question that comes
straight after it, from four floors or two thousand kilometres away: *why*, and
*is it about to stop*. A phone at 4% battery and a disk with 100 MB left are
both working right up until they are not, and neither shows up in a service
being active.

Everything here degrades to None rather than raising, for the same reason the
health probes do: a control centre that will not render because /proc changed
shape is worse than one with a blank line in it. None means "not known", which
the page renders differently from a zero — those are not the same news.

The phone costs a subprocess, so the whole reading is cached for a few seconds.
This is a page somebody leaves open.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# The three that have to be up for the portal to do anything, plus the one
# that carries it to Texas. tailscaled is not ours and is not restarted by
# anything here, which is exactly why it is worth showing: when it is down the
# portal is simply unreachable, and the person who would notice cannot see this
# page to find out.
#
# aptlog-agent is deliberately absent. It is the scheduler — the machine
# deciding on its own when to record a visit — and it is disabled on purpose.
# A unit somebody turned off is not a fault, and a row that is red every day is
# a row people stop reading.
UNITS = ("aptlog-feed", "aptlog-appium", "aptlog-ui", "tailscaled")

CACHE_TTL = 5.0
_cache: dict = {"at": 0.0, "doc": None}
_lock = threading.Lock()


# --------------------------------------------------------------------- the Pi
def _meminfo() -> dict | None:
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    fields = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        try:
            fields[name] = int(rest.strip().split()[0]) * 1024
        except (IndexError, ValueError):
            continue
    total = fields.get("MemTotal")
    available = fields.get("MemAvailable")
    if not total or available is None:
        return None
    # Used = total - available, not total - free. Free excludes cache, which
    # on a machine that has been up for weeks reads as 98% used forever and
    # teaches whoever is looking to ignore the number.
    used = total - available
    return {"used": used, "total": total, "pct": round(used * 100 / total)}


def _disk(path: str = "/") -> dict | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    if not usage.total:
        return None
    return {"used": usage.used, "total": usage.total,
            "pct": round(usage.used * 100 / usage.total)}


def _temperature() -> float | None:
    for candidate in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            raw = int(Path(candidate).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        # Millidegrees on every Pi kernel; degrees on some others.
        return round(raw / 1000 if raw > 1000 else float(raw), 1)
    return None


def _uptime() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _load() -> list[float] | None:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except OSError:
        return None


def _cpu_count() -> int:
    return os.cpu_count() or 1


def _unit(unit: str) -> str:
    """ok / bad / off / unknown.

    "off" is the one worth having and the reason this asks two questions in one
    call: a unit that is stopped BECAUSE SOMEBODY DISABLED IT is not a fault,
    and showing it in the same red as a crash teaches whoever is looking to
    ignore the colour. Stopped while still enabled is a real failure and stays
    red.
    """
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", "ActiveState", "-p", "UnitFileState"],
            capture_output=True, text=True, timeout=4, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    fields = {}
    for line in out.stdout.splitlines():
        name, _, value = line.partition("=")
        fields[name.strip()] = value.strip()
    state = fields.get("ActiveState", "")
    if state == "active":
        return "ok"
    if state in ("activating", "reloading"):
        return "unknown"
    if not state:
        return "unknown"
    if fields.get("UnitFileState") in ("disabled", "masked"):
        return "off"
    return "bad"


def _tailscale() -> dict:
    """The address this page arrived on, and whether the tailnet is up.

    `tailscale ip -4` is the cheapest question that fails when the daemon is
    not running, which is the only failure worth a row here.
    """
    doc = {"up": "unknown", "address": ""}
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=4, check=False)
    except (OSError, subprocess.SubprocessError):
        return doc
    address = out.stdout.strip().splitlines()
    if out.returncode == 0 and address:
        doc["up"] = "ok"
        doc["address"] = address[0]
    else:
        doc["up"] = "bad"
    return doc


# ------------------------------------------------------------------ the phone
_BATTERY_LEVEL = re.compile(r"^\s*level:\s*(\d+)", re.M)
_BATTERY_TEMP = re.compile(r"^\s*temperature:\s*(-?\d+)", re.M)
_BATTERY_PLUG = re.compile(r"^\s*(?:AC|USB|Wireless) powered:\s*(\w+)", re.M)
_DENSITY_OVERRIDE = re.compile(r"Override density:\s*(\d+)")
_DENSITY_PHYSICAL = re.compile(r"Physical density:\s*(\d+)")

# How long an app update stays worth pointing at. Long enough to still be
# on the panel on Monday when something that worked on Friday does not.
RECENT_UPDATE = 7 * 24 * 3600.0


def _phone(serial: str | None = None) -> dict:
    """Battery, charge state and the density actually in force.

    One adb invocation for all of it. Three would be three chances to hang a
    page render on a cable somebody is holding.
    """
    doc: dict = {"attached": "unknown", "serial": serial or "",
                 "battery": None, "charging": None, "temp_c": None,
                 "density": None, "density_physical": None,
                 # Read off a file, not the phone — so it still answers with
                 # the cable out, which is one of the times somebody is most
                 # likely to be asking what changed.
                 "apps": _app_versions()}
    try:
        from apt_log.transport import attached_devices

        serials, mode = attached_devices()
        doc["transport"] = getattr(mode, "value", str(mode))
        doc["attached"] = "ok" if serials else "bad"
        if serials and not serial:
            serial = serials[0]
            doc["serial"] = serial
    except Exception as exc:  # noqa: BLE001
        log.debug("cannot read devices: %s", exc)
        doc.setdefault("transport", "unknown")
    if doc["attached"] != "ok":
        return doc

    cmd = ["adb"] + (["-s", serial] if serial else []) + [
        "shell", "dumpsys battery; wm density"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=8,
                             check=False).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("cannot read the phone: %s", exc)
        return doc

    level = _BATTERY_LEVEL.search(out)
    if level:
        doc["battery"] = int(level.group(1))
    temp = _BATTERY_TEMP.search(out)
    if temp:
        # dumpsys reports tenths of a degree.
        doc["temp_c"] = round(int(temp.group(1)) / 10, 1)
    plugs = _BATTERY_PLUG.findall(out)
    # False and None are different answers — "running on battery" against
    # "the phone did not say" — so an empty match stays None.
    doc["charging"] = (any(p.lower() == "true" for p in plugs)
                       if plugs else None)
    override = _DENSITY_OVERRIDE.search(out)
    physical = _DENSITY_PHYSICAL.search(out)
    if physical:
        doc["density_physical"] = int(physical.group(1))
    if override:
        doc["density"] = int(override.group(1))
    elif physical:
        doc["density"] = int(physical.group(1))
    return doc


def _app_versions() -> list[dict]:
    """Which build of each app is on the phone, newest change first.

    Read off the file the feed writes rather than off the phone: this runs on
    a page render, and a version that changes twice a year does not need an
    adb call every time somebody opens the console.
    """
    try:
        from apt_log import versions as versions_mod

        doc = versions_mod.stored()
    except Exception as exc:  # noqa: BLE001
        log.debug("cannot read app versions: %s", exc)
        return []
    apps = doc.get("apps") or {}
    if not isinstance(apps, dict):
        return []
    # The stored change is the LAST one ever recorded, which may be from
    # months ago. Only a recent one is news; an old one is history, and a
    # badge that never clears is a badge nobody sees.
    recent = time.time() - float(doc.get("changed_at") or 0) < RECENT_UPDATE
    moved = {c.get("package") for c in (doc.get("changed") or [])
             if isinstance(c, dict)} if recent else set()
    return [{"package": pkg,
             "name": (info or {}).get("name", ""),
             "code": (info or {}).get("code", ""),
             # Flagged so the panel can say WHICH app moved. "An app updated
             # last week" is not the news; "this one did" is.
             "changed": pkg in moved}
            for pkg, info in sorted(apps.items())]


# ------------------------------------------------------------------- assembly
def read(force: bool = False) -> dict:
    """Everything, cached. Safe to call from a page render or a socket tick."""
    with _lock:
        fresh = time.time() - _cache["at"] < CACHE_TTL
        if _cache["doc"] is not None and fresh and not force:
            return _cache["doc"]
    doc = {
        "at": time.time(),
        "host": _hostname(),
        "mem": _meminfo(),
        "disk": _disk("/"),
        "state_disk": _disk("/var/lib"),
        "temp_c": _temperature(),
        "uptime": _uptime(),
        "load": _load(),
        "cpus": _cpu_count(),
        "services": [{"unit": u, "state": _unit(u)} for u in UNITS],
        "net": _tailscale(),
        "phone": _phone(),
    }
    with _lock:
        _cache["at"] = time.time()
        _cache["doc"] = doc
    return doc


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def worst(doc: dict | None = None) -> str:
    """One word for the whole machine, for a badge that has to be readable at
    arm's length: bad if anything is bad or nearly out of something, unknown
    if anything is unsettled, ok otherwise."""
    doc = doc or read()
    # "off" is not in this set on purpose: a unit somebody turned off is a
    # decision, not a symptom, and it must not colour the whole machine.
    states = {s["state"] for s in doc.get("services", ())
              if s["state"] != "off"}
    phone = (doc.get("phone") or {}).get("attached", "unknown")
    states.add(phone)
    states.add((doc.get("net") or {}).get("up", "unknown"))
    for part in ("mem", "disk", "state_disk"):
        block = doc.get(part)
        if block and block.get("pct", 0) >= 95:
            states.add("bad")
    battery = (doc.get("phone") or {}).get("battery")
    if battery is not None and battery <= 10:
        states.add("bad")
    if "bad" in states:
        return "bad"
    if "unknown" in states:
        return "unknown"
    return "ok"
