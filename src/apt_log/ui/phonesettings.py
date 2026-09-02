"""The debug map of the phone's own Settings.

The portal exists to keep the phone INSIDE four care apps, so for a long time
the phone's own Settings had no page: the one macro (`phone_settings`) opened
Settings at its front door and everything after that was tapping through the
mirror from memory. That works for somebody who knows where Android keeps its
language screen; it does not work at 9pm over the phone with somebody who does
not, and it leaves no record of which screens this project has actually needed.

So this module writes the map down. Two tables, one rule each:

**PANELS** — the Settings screens the portal can be asked to open, each one an
`android.settings.*` intent action. An allow-list rather than an intent
parameter, for the same reason `device.UI_ACTIONS` is: the moment the page can
post an arbitrary intent action it is a remote control for the whole phone —
`am start` reaches dialers, installers and every exported activity on the
device — and "the UI cannot do anything she did not ask for" stops being a
property of the routing. A new screen is a new row here, added on purpose.

Opening one of these is contained by design: the containment watchdog already
sanctions `feed.SETTINGS_APPS` as somewhere the phone can be SENT (as opposed
to somewhere it wandered), and everything past the opened screen rides the
same verified-tap path as every other press on the mirror.

**READINGS** — what the phone says its settings currently are, read over adb.
Read-only by construction: every probe is a `getprop` or a `settings get`,
there is no `settings put` anywhere in this module, and changing a value means
opening the screen and tapping the phone's own control for it — where the
phone applies its own validation, confirmation dialogs and side effects,
none of which a blind `settings put` would run.

Everything here degrades rather than breaks: a Pi whose adb has gone away
renders a page of dashes, not a stack trace, because the person opening a
debug page is exactly the person diagnosing why adb went away.
"""

from __future__ import annotations

import logging
import subprocess
import time

log = logging.getLogger(__name__)

# One bound for every adb call this module makes. The debug page renders on
# request; a probe that hangs on a flaky cable must cost seconds, not a tab.
ADB_TIMEOUT = 8.0

# The Settings screens the portal can open, in the order the page offers them.
#
# `language` is first because it is the reason this page exists: the phone's
# own language decides what every care app renders, and walking somebody to
# Settings → General management → Language over a phone call is the failure
# this map replaces.
#
# Every action here is one of Android's public `android.settings.*` intents
# (battery is the one odd spelling — AOSP kept its pre-Settings name). A row
# whose screen a given phone does not have fails visibly on the phone and
# harms nothing; a screen this table does not name cannot be opened at all.
PANELS = (
    {"id": "language", "group": "system",
     "action": "android.settings.LOCALE_SETTINGS",
     "label_key": "debug.panel.language"},
    {"id": "date", "group": "system",
     "action": "android.settings.DATE_SETTINGS",
     "label_key": "debug.panel.date"},
    {"id": "display", "group": "system",
     "action": "android.settings.DISPLAY_SETTINGS",
     "label_key": "debug.panel.display"},
    {"id": "sound", "group": "system",
     "action": "android.settings.SOUND_SETTINGS",
     "label_key": "debug.panel.sound"},
    {"id": "accessibility", "group": "system",
     "action": "android.settings.ACCESSIBILITY_SETTINGS",
     "label_key": "debug.panel.accessibility"},
    {"id": "wifi", "group": "connect",
     "action": "android.settings.WIFI_SETTINGS",
     "label_key": "debug.panel.wifi"},
    {"id": "bluetooth", "group": "connect",
     "action": "android.settings.BLUETOOTH_SETTINGS",
     "label_key": "debug.panel.bluetooth"},
    {"id": "apps", "group": "device",
     "action": "android.settings.APPLICATION_SETTINGS",
     "label_key": "debug.panel.apps"},
    {"id": "battery", "group": "device",
     "action": "android.intent.action.POWER_USAGE_SUMMARY",
     "label_key": "debug.panel.battery"},
    {"id": "storage", "group": "device",
     "action": "android.settings.INTERNAL_STORAGE_SETTINGS",
     "label_key": "debug.panel.storage"},
    {"id": "developer", "group": "device",
     "action": "android.settings.APPLICATION_DEVELOPMENT_SETTINGS",
     "label_key": "debug.panel.developer"},
    {"id": "all", "group": "device",
     "action": "android.settings.SETTINGS",
     "label_key": "debug.panel.all"},
)

PANEL_INDEX = {p["id"]: p for p in PANELS}

# The order the page draws the groups in. System first because language —
# the reason the page exists — is a system screen.
GROUPS = ("system", "connect", "device")


class SettingsUnavailable(RuntimeError):
    """The phone did not open the screen — adb refused, or `am` reported
    an error on a device that does not have that screen."""


def panels_by_group() -> list[tuple[str, list[dict]]]:
    """The panels as the template draws them: grouped, order preserved."""
    return [(g, [p for p in PANELS if p["group"] == g]) for g in GROUPS]


def open_panel(panel_id: str) -> dict:
    """Open one Settings screen from the table. KeyError for anything else.

    The KeyError is the security property: the browser posts an id, the id
    selects a row, and only the row knows an intent action. There is no code
    path from a form field to `am start`.
    """
    panel = PANEL_INDEX[panel_id]

    # The screen may be dark — Settings opening invisibly reads as a broken
    # button. Best effort only: a wake that fails is not a reason to refuse
    # the open, and the open failing will say so for itself.
    from apt_log.device import send_ui_action

    try:
        send_ui_action("wake")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not wake the display (%s)", exc)

    try:
        result = subprocess.run(
            ["adb", "shell", "am", "start", "-a", panel["action"]],
            capture_output=True, timeout=ADB_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SettingsUnavailable(f"adb did not answer ({exc})") from exc
    said = (result.stdout + result.stderr).decode("utf-8", "replace")
    # `am start` reports "Error: Activity not started…" on stdout with exit
    # code 0, so the words have to be read, not just the code.
    if result.returncode != 0 or "Error" in said:
        raise SettingsUnavailable(
            f"the phone refused {panel['action']}: {said.strip()[:200]}")
    log.info("opened phone settings screen %r", panel_id)
    return panel


# ----------------------------------------------------------------- readings
# What the phone says its settings are, each row one read-only probe.
#
#   kind "text"   — shown as-is
#   kind "onoff"  — "1"/"0", translated to on/off words by the page
#   kind "ms"     — a duration in milliseconds, humanised below
#   kind "range"  — a value out of 255 (the brightness scale)
#
# The probe strings are fixed here and joined into ONE adb invocation: a
# dozen separate calls would be a dozen chances to hang a page render on a
# cable somebody is holding (the same reasoning as machine._phone).
READINGS = (
    {"id": "locale", "kind": "text", "label_key": "debug.reading.locale",
     "probe": "getprop persist.sys.locale"},
    # The factory locale, read as the fallback for a phone whose locale has
    # never been changed — persist.sys.locale is empty there.
    {"id": "locale_default", "kind": "hidden", "label_key": "",
     "probe": "getprop ro.product.locale"},
    {"id": "timezone", "kind": "text", "label_key": "debug.reading.timezone",
     "probe": "getprop persist.sys.timezone"},
    {"id": "android", "kind": "text", "label_key": "debug.reading.android",
     "probe": "getprop ro.build.version.release"},
    {"id": "model", "kind": "text", "label_key": "debug.reading.model",
     "probe": "getprop ro.product.model"},
    {"id": "font_scale", "kind": "text",
     "label_key": "debug.reading.font_scale",
     "probe": "settings get system font_scale"},
    {"id": "brightness", "kind": "range",
     "label_key": "debug.reading.brightness",
     "probe": "settings get system screen_brightness"},
    {"id": "screen_timeout", "kind": "ms",
     "label_key": "debug.reading.screen_timeout",
     "probe": "settings get system screen_off_timeout"},
    {"id": "wifi", "kind": "onoff", "label_key": "debug.reading.wifi",
     "probe": "settings get global wifi_on"},
    {"id": "bluetooth", "kind": "onoff",
     "label_key": "debug.reading.bluetooth",
     "probe": "settings get global bluetooth_on"},
    {"id": "airplane", "kind": "onoff",
     "label_key": "debug.reading.airplane",
     "probe": "settings get global airplane_mode_on"},
    {"id": "auto_time", "kind": "onoff",
     "label_key": "debug.reading.auto_time",
     "probe": "settings get global auto_time"},
    {"id": "auto_zone", "kind": "onoff",
     "label_key": "debug.reading.auto_zone",
     "probe": "settings get global auto_time_zone"},
)

# Separates one probe's output from the next inside the single invocation.
# A character no probe's answer contains: locales, packages and numbers do
# not carry tildes.
_MARK = "~"


def _probe_all() -> list[str]:
    """Every probe's raw answer, in READINGS order. [] when adb is away."""
    # The mark is quoted: an unquoted `echo ~` is a tilde the device's own
    # shell expands into a home directory before echo ever sees it.
    command = f"; echo '{_MARK}'; ".join(r["probe"] for r in READINGS)
    try:
        out = subprocess.run(["adb", "shell", command], capture_output=True,
                             timeout=ADB_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("cannot read the phone's settings (%s)", exc)
        return []
    if out.returncode != 0:
        return []
    parts = out.stdout.decode("utf-8", "replace").split(_MARK)
    if len(parts) != len(READINGS):
        # A phone mid-reboot answers with half a transcript; half-parsed
        # rows would put the wifi answer on the bluetooth row.
        log.debug("settings transcript has %d parts, wanted %d",
                  len(parts), len(READINGS))
        return []
    return [p.strip() for p in parts]


def _value_of(reading: dict, raw: str, fallbacks: dict[str, str]) -> str | None:
    """One probe's answer as the page shows it. None for no answer.

    `settings get` answers the literal string "null" for a value nothing has
    ever set, which for every row here means "the default", not "unknown" —
    but only font_scale has a default worth asserting (1.0, the app's own
    text size). The rest render as a dash.
    """
    if raw == "" and reading["id"] == "locale":
        raw = fallbacks.get("locale_default", "")
    if raw in ("", "null"):
        return "1.0" if reading["id"] == "font_scale" else None
    if reading["kind"] == "ms":
        try:
            ms = int(raw)
        except ValueError:
            return raw
        return f"{ms // 60000} min" if ms >= 60000 else f"{ms // 1000} s"
    if reading["kind"] == "range":
        return f"{raw} / 255"
    return raw


def readings() -> dict:
    """The phone's current settings, shaped for the page and the API.

    {"ok": bool, "at": epoch, "front": focused package, "rows": [...]}
    with each row {"id", "label_key", "kind", "value"}. `ok` False means
    the phone did not answer and every value is None — the page renders
    dashes and says why, rather than an exception page on the one tab
    somebody opened to diagnose exactly that.
    """
    raws = _probe_all()
    ok = bool(raws)
    fallbacks = {}
    if ok:
        fallbacks = {r["id"]: raw for r, raw in zip(READINGS, raws)
                     if r["kind"] == "hidden"}
    rows = []
    for index, reading in enumerate(READINGS):
        if reading["kind"] == "hidden":
            continue
        rows.append({
            "id": reading["id"],
            "label_key": reading["label_key"],
            "kind": reading["kind"],
            "value": _value_of(reading, raws[index], fallbacks) if ok
                     else None,
        })

    # Which app holds the screen, so the page can say "the phone is in
    # Settings now" against "your open never landed". Best effort: the
    # readings are the product, the focus is a caption on them.
    front = ""
    try:
        from apt_log import feed as feed_mod

        front = (feed_mod.current_focus() or "").split("/")[0]
    except Exception:  # noqa: BLE001 — a caption is not worth a 500
        front = ""

    return {"ok": ok, "at": time.time(), "front": front, "rows": rows}
