"""Which version of each app is on the phone, and when that last changed.

This project reads four apps by resource-id. Every reflow rule, every macro
that finds a button, every atlas entry is an assumption about a build that
Google Play can replace overnight without asking anybody. When something
that worked yesterday stops working at six in the morning, the first
question is "did the app change?" — and until this module existed, nothing
on either machine could answer it.

So the versions are read on a slow timer, written down, and a change is
logged as a change. That is the whole job. Nothing here installs, updates or
refuses an update: the phone's Play Store does that, and a build this code
has never seen is a thing for a person to look at on a morning when somebody
can check afterwards, not for a machine to accept at 6am.

The read costs one adb call. It runs every fifteen minutes, next to a
screenshot every few seconds, so it is free in every sense that matters.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

from apt_log.feed import CARE_APPS, retired

log = logging.getLogger(__name__)

STATE_DIR = Path("/var/lib/aptlog")
VERSIONS_PATH = STATE_DIR / "app-versions.json"

# The Play Store is watched alongside the care apps because it is the thing
# that changes them. When an app updates itself and the reflow breaks, the
# Store's own version is part of the answer to what happened.
PLAY_STORE = "com.android.vending"


def watched() -> tuple[str, ...]:
    """The packages worth a version. Retired apps are not among them.

    The legacy app still gets read when it is in front, but the phone never
    goes there any more, so an update to it changes nothing and a line about
    it in the log is a line that trains people to skim.
    """
    return tuple(p for p in CARE_APPS if not retired(p)) + (PLAY_STORE,)


# How often the phone is asked. Apps update a handful of times a year; this
# is a heartbeat, not a poll, and it is cheap enough to leave running.
CHECK_EVERY = 900.0

_PKG_LINE = re.compile(r"^pkg=(\S+)", re.M)
_CODE = re.compile(r"\bversionCode=(\d+)")
_NAME = re.compile(r"\bversionName=(\S+)")

_lock = threading.Lock()
_last_check = [0.0]


def _shell(serial: str | None) -> str:
    """One adb call for every package, or "" if the phone would not answer."""
    script = ("for p in " + " ".join(watched()) + "; do echo pkg=$p; "
              'dumpsys package $p | grep -E "versionName=|versionCode="; done')
    cmd = ["adb"] + (["-s", serial] if serial else []) + ["shell", script]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("cannot read app versions: %s", exc)
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", "replace")


def parse(text: str) -> dict[str, dict]:
    """package -> {"name": ..., "code": ...}, from the dumpsys output.

    The FIRST versionCode/versionName in each package's block wins, and that
    is not a detail. A system app that has been updated dumps twice — the
    installed build first, the factory one still on /system after it — so the
    Play Store on this phone reports 52.7.34 followed by 38.8.31. Taking the
    last would have quietly published a version from 2023.
    """
    found: dict[str, dict] = {}
    marks = list(_PKG_LINE.finditer(text or ""))
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        block = text[mark.end():end]
        name = _NAME.search(block)
        code = _CODE.search(block)
        if not name and not code:
            # An app that is not installed dumps nothing between the markers.
            # That is worth knowing but it is not a version.
            continue
        found[mark.group(1)] = {
            "name": name.group(1) if name else "",
            "code": code.group(1) if code else "",
        }
    return found


def of(package: str, serial: str | None = None) -> dict:
    """One app's version, read off the phone right now.

    Straight from the device rather than from the record, because the two
    callers that want this are asking "has it changed *since a second ago*" —
    an update macro watching an install finish, and whatever checks afterwards
    that it did.
    """
    script = (f"dumpsys package {package} | "
              'grep -E "versionName=|versionCode="')
    cmd = ["adb"] + (["-s", serial] if serial else []) + ["shell", script]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("cannot read %s: %s", package, exc)
        return {}
    if out.returncode != 0:
        return {}
    found = parse(f"pkg={package}\n" + out.stdout.decode("utf-8", "replace"))
    return found.get(package, {})


def stored(path: Path | None = None) -> dict:
    """What was on the phone last time anybody looked.

    The path is resolved on the call, not baked into the signature, so that
    redirecting VERSIONS_PATH actually redirects it. The deploy gate runs the
    whole suite ON THE PI against the real /var/lib/aptlog — see conftest —
    and a default bound at import time would sail straight past that.
    """
    path = path or VERSIONS_PATH
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _write(doc: dict, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.debug("cannot write app versions: %s", exc)


def changes(before: dict, after: dict) -> list[dict]:
    """What moved between two readings, newly-installed apps included.

    A package that has stopped answering is NOT a change. The phone can be
    asleep, adb can be half-attached, an app can be mid-install — all of
    which read as "gone" for a moment, and none of which is news worth
    waking anybody for. Only an app that answers with a different version
    counts.
    """
    moved = []
    for package, now in sorted(after.items()):
        was = before.get(package) or {}
        if was.get("name") == now.get("name") and \
                was.get("code") == now.get("code"):
            continue
        moved.append({"package": package,
                      "from": was.get("name", ""),
                      "from_code": was.get("code", ""),
                      "to": now.get("name", ""),
                      "to_code": now.get("code", "")})
    return moved


def check(serial: str | None = None, force: bool = False,
          path: Path | None = None) -> list[dict]:
    """Read the versions if it is time, and say what changed.

    Safe to call every tick of the feed loop: the timer is in here, so the
    caller does not have to hold one and cannot get it wrong.
    """
    path = path or VERSIONS_PATH
    with _lock:
        now = time.time()
        if not force and now - _last_check[0] < CHECK_EVERY:
            return []
        _last_check[0] = now

    found = parse(_shell(serial))
    if not found:
        # A phone that answered nothing at all is a phone that was not
        # there. Writing that down would erase the record and then report
        # every app as "new" the moment the cable came back.
        return []

    before = stored(path)
    if not before:
        # The first reading ever. Everything is new, and announcing four
        # updates on the day this shipped would be four false alarms — which
        # is how a warning stops being read. Baseline it quietly.
        log.info("app versions, first reading: %s",
                 ", ".join(f"{p} {v['name']}" for p, v in sorted(found.items())))
        _write({"at": time.time(), "apps": found}, path)
        return []

    known = before.get("apps") or {}
    moved = changes(known, found)
    if moved:
        for change in moved:
            log.warning(
                "app version changed: %s %s (%s) -> %s (%s)",
                change["package"], change["from"] or "absent",
                change["from_code"] or "-", change["to"], change["to_code"])
        _write({"at": time.time(), "apps": found,
                "changed_at": time.time(), "changed": moved}, path)
    else:
        before["at"] = time.time()
        before["apps"] = found
        _write(before, path)
    return moved
