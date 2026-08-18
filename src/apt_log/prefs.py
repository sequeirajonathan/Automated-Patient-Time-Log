"""Who is looking, and how they like to look.

There is no database in this system and this does not add one. The agency's
record is the database (see screens/today.py); everything here is a
preference, which is worth keeping and worth nothing if it is lost — a JSON
file beside the other state files, written the same atomic way.

Two people use this portal at once and want opposite things from it. The
caregiver reads Spanish and wants the phone the size it has always been.
Whoever is helping her reads English and wants to change what he is looking
at without changing anything on her screen. So a preference belongs to a
DEVICE, not to the installation: the language you choose here does not
follow her, and the density you set for one app does not disturb another.

DENSITY IS THE DANGEROUS ONE. Below the floor the phone has actually
crashed — 72 took it down hard enough to need a physical restart — so this
module clamps rather than trusts, and keeps DEFAULTS and OVERRIDES apart:
an override can be cleared back to nothing, and what was always there is
still underneath. Nothing a slider does can lose the value that works.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from apt_log.ui.state import STATE_DIR

PREFS_PATH = STATE_DIR / "prefs.json"

# The languages the portal speaks. Spanish first, and the default: the
# person using this all day reads Spanish, and English is the visiting
# language.
LANGUAGES = ("es", "en")
DEFAULT_LANGUAGE = "es"

# Density, in dpi. The floor is not a guess: 72 crashed the phone during
# tuning and needed someone in the room to power-cycle it, so the slider
# cannot reach it. The ceiling is where the screen stops being usable by a
# finger rather than anything dangerous.
DENSITY_FLOOR = 84
DENSITY_CEILING = 200
DENSITY_STEP = 4

# The key an override uses when it is about "whatever is on the phone" rather
# than one of the four care apps — the case where somebody who is not the
# caregiver borrows the phone for something this system knows nothing about,
# and wants the text bigger while they do it.
GLOBAL = "*"

# A device is forgotten if it has not been seen in this long. Long enough
# that a phone left in a drawer over a weekend is still itself on Monday.
DEVICE_TTL = 60 * 60 * 24 * 30

_lock = threading.Lock()


def new_device_id() -> str:
    """An opaque id for a browser. Not a name and not a secret — it says
    'the same browser as last time' and nothing else."""
    return secrets.token_urlsafe(9)


def _blank() -> dict[str, Any]:
    return {"devices": {}, "density": {}}


def load(path: Path | None = None) -> dict[str, Any]:
    """Everything stored. Never raises: a portal that will not render
    because a preference file is malformed is worse than one with default
    preferences."""
    target = path or PREFS_PATH
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _blank()
    if not isinstance(doc, dict):
        return _blank()
    doc.setdefault("devices", {})
    doc.setdefault("density", {})
    return doc


def save(doc: dict[str, Any], path: Path | None = None) -> None:
    target = path or PREFS_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        # A preference that cannot be written is not worth failing a page
        # over; the caller carries on with what it has.
        pass


def _mutate(fn, path: Path | None = None) -> dict[str, Any]:
    with _lock:
        doc = load(path)
        fn(doc)
        _forget_old(doc)
        _merge_duplicates(doc)
        save(doc, path)
        return doc


def _forget_old(doc: dict[str, Any]) -> None:
    cutoff = time.time() - DEVICE_TTL
    for did, dev in list(doc.get("devices", {}).items()):
        if float(dev.get("seen") or 0) < cutoff:
            doc["devices"].pop(did, None)


# ------------------------------------------------------------------ devices
def fingerprint(agent: str) -> str:
    """What identifies a browser when its cookie is gone.

    A cookie is the identity; this is how the identity is RECOVERED. Cookies
    on a phone go missing constantly — a bookmark opened as an app has its own
    jar, private tabs have another, iOS evicts them from sites you have not
    opened in a week — and every one of those minted a fresh device, so the
    list of "who is on" filled up with four rows that were all the same
    person's phone.

    The user-agent string is coarse and that is the point: it is stable across
    everything that loses a cookie, and it says nothing about who is holding
    the phone. Two identical phones on identical browsers do collapse into one
    row. That is the trade, and it is the right way round — a list with one
    row too few is readable, and a list with a new row every morning is not.
    """
    return hashlib.sha256((agent or "").encode("utf-8")).hexdigest()[:16]


def resolve(device_id: str, agent: str = "",
            path: Path | None = None) -> str:
    """The id this browser should actually be recorded under.

    Its own, if it has one that is known. Otherwise the id of a device with
    the same fingerprint — the same browser, back without its cookie. Only
    then a new one.
    """
    doc = load(path)
    if device_id and device_id in doc["devices"]:
        return device_id
    mark = fingerprint(agent)
    if agent:
        known = [(dev.get("seen") or 0, did)
                 for did, dev in doc["devices"].items()
                 if dev.get("mark") == mark]
        if known:
            return max(known)[1]
    return device_id or new_device_id()


def _merge_duplicates(doc: dict[str, Any]) -> None:
    """Collapse devices that are the same browser, keeping the best of each.

    Runs on every write, so a store that already accumulated duplicates heals
    itself rather than needing anybody to go and tidy it. A name always
    survives a merge: it is the only thing here somebody typed.
    """
    by_mark: dict[str, str] = {}
    for did, dev in sorted(doc["devices"].items(),
                           key=lambda kv: kv[1].get("seen") or 0, reverse=True):
        mark = dev.get("mark")
        if not mark and dev.get("agent"):
            # Written before fingerprints existed. Backfilled here rather than
            # left alone, or the rows that made duplicates a problem would be
            # the only ones that never heal.
            mark = dev["mark"] = fingerprint(dev["agent"])
        if not mark:
            continue
        keeper = by_mark.get(mark)
        if keeper is None:
            by_mark[mark] = did
            continue
        kept = doc["devices"][keeper]
        kept["name"] = kept.get("name") or dev.get("name", "")
        # The keeper is the more recent, so its language and page are the
        # current ones; only fill in what it does not have.
        kept["language"] = kept.get("language") or dev.get("language")
        if not kept.get("language"):
            kept.pop("language", None)
        kept["where"] = kept.get("where") or dev.get("where", "")
        doc["devices"].pop(did, None)


def seen(device_id: str, *, agent: str = "", where: str = "",
         path: Path | None = None) -> dict[str, Any]:
    """Record that a device is here, and return its preferences.

    `where` is which page it is on, so the control centre can say what the
    other person is looking at — the whole point of being able to help
    somebody without taking their screen away from them.
    """
    def edit(doc):
        dev = doc["devices"].setdefault(device_id, {})
        dev.setdefault("name", "")
        # Deliberately NOT defaulting the language here. A device that has
        # never touched the switch has not chosen anything, and writing the
        # default down as a choice would make it outrank the browser's own
        # Accept-Language for every device that never had an opinion.
        dev["seen"] = time.time()
        if agent:
            dev["agent"] = agent[:180]
            dev["mark"] = fingerprint(agent)
        if where:
            dev["where"] = where
    return _mutate(edit, path)["devices"].get(device_id, {})


def device(device_id: str, path: Path | None = None) -> dict[str, Any]:
    dev = load(path)["devices"].get(device_id) or {}
    return {"name": dev.get("name", ""),
            "language": dev.get("language") or DEFAULT_LANGUAGE,
            "seen": dev.get("seen", 0.0),
            "agent": dev.get("agent", ""),
            "where": dev.get("where", "")}


def language_of(device_id: str, path: Path | None = None) -> str:
    """The language this device CHOSE, or "" if it never has.

    Distinct from device()["language"], which fills in the default: a caller
    deciding between a stored choice and the browser's own Accept-Language
    needs to know which of those two it is holding, or the default silently
    outranks the header for every device that has never touched the switch.
    """
    if not device_id:
        return ""
    stored = load(path)["devices"].get(device_id, {}).get("language")
    return stored if stored in LANGUAGES else ""


def rename(device_id: str, name: str, path: Path | None = None) -> None:
    """Give a device a human name — 'Sadia's phone', 'the kitchen iPad'.
    A list of opaque ids answers nobody's question about who is on."""
    def edit(doc):
        doc["devices"].setdefault(device_id, {})["name"] = name.strip()[:40]
    _mutate(edit, path)


def set_language(device_id: str, language: str,
                 path: Path | None = None) -> str:
    chosen = language if language in LANGUAGES else DEFAULT_LANGUAGE

    def edit(doc):
        dev = doc["devices"].setdefault(device_id, {})
        dev["language"] = chosen
        dev.setdefault("seen", time.time())
    _mutate(edit, path)
    return chosen


def devices(path: Path | None = None) -> list[dict[str, Any]]:
    """Everyone who has been here lately, most recent first."""
    out = []
    for did, dev in load(path)["devices"].items():
        out.append({"id": did,
                    "name": dev.get("name", ""),
                    "language": dev.get("language") or DEFAULT_LANGUAGE,
                    "seen": float(dev.get("seen") or 0),
                    "agent": dev.get("agent", ""),
                    "where": dev.get("where", "")})
    out.sort(key=lambda d: d["seen"], reverse=True)
    return out


# ------------------------------------------------------------------ density
def clamp(value: Any) -> int | None:
    """A density that cannot hurt the phone, or None for 'no opinion'.

    None is a real answer and the reason overrides can be cleared: it means
    fall through to what was there before, which is never overwritten.
    """
    if value is None or value == "":
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(DENSITY_FLOOR, min(DENSITY_CEILING, n))


def set_density(app: str, value: Any, page: str = "",
                path: Path | None = None) -> int | None:
    """Set — or with None, clear — the override for an app, or for one page
    of one app. Clearing restores whatever the app's own default is."""
    wanted = clamp(value)
    key = f"{app}::{page}" if page else app

    def edit(doc):
        if wanted is None:
            doc["density"].pop(key, None)
        else:
            doc["density"][key] = wanted
    _mutate(edit, path)
    return wanted


def density_for(app: str, page: str = "", default: int | None = None,
                path: Path | None = None) -> int | None:
    """The override in force for this app and page, or None for 'nobody has
    said'.

    Most specific first: this page of this app, then this app, then the
    default handed in — which is the code's own table, still intact under
    every override.

    GLOBAL is deliberately not in this chain. It is the answer for apps this
    system has no table for, and if it were consulted here it would quietly
    outrank the per-app values that were tuned against the real screens.
    """
    store = load(path)["density"]
    for key in (f"{app}::{page}" if page else None, app):
        if key and key in store:
            return clamp(store[key])
    return clamp(default) if default is not None else None


def global_density(path: Path | None = None) -> int | None:
    """What to use on a screen this system knows nothing about, or None to
    leave the phone exactly as it is."""
    return clamp(load(path)["density"].get(GLOBAL))


def set_global_density(value: Any, path: Path | None = None) -> int | None:
    return set_density(GLOBAL, value, path=path)


def overrides(path: Path | None = None) -> dict[str, int]:
    """Every override in force, for a page that lets you see and clear them."""
    return {k: v for k, v in load(path)["density"].items()
            if isinstance(v, int)}
