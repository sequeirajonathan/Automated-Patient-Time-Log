#!/usr/bin/env bash
# Dead-man's switch. Pings an external monitor only when the system is actually
# healthy; silence is what raises the alarm.
#
# Everything else in this system alerts on failures it notices. Nothing else
# catches the Pi being dead, unplugged, or offline — because a dead Pi sends
# nothing. This inverts that.
#
# Run from aptlog-heartbeat.timer. Outbound only, so it needs no inbound access
# and keeps working when Tailscale is down.

set -uo pipefail

PING_URL="${HEARTBEAT_URL:-}"          # e.g. https://hc-ping.com/<uuid>
[[ -n "$PING_URL" ]] || exit 0          # not configured: stay silent

fail() {
    # Signal a known-bad state explicitly rather than just going quiet, so the
    # monitor can distinguish "broken" from "unreachable".
    curl -fsS -m 10 --data-raw "$1" "$PING_URL/fail" >/dev/null 2>&1 || true
    exit 1
}

# 1. the phone is attached, authorised, and over USB rather than TCP
adb devices 2>/dev/null | grep -qE '^\S+\s+device$' || fail "adb: no authorised device"
adb devices 2>/dev/null | grep -qE ':[0-9]+\s+device$' && fail "adb: device is over TCP, not USB"

# 2. services are actually running
for unit in aptlog-agent aptlog-appium aptlog-ui; do
    systemctl is-active --quiet "$unit" || fail "unit down: $unit"
done

# 3. the UI answers
curl -fsS -m 5 http://127.0.0.1:8080/healthz >/dev/null 2>&1 || fail "healthz not responding"

# Only now is a ping honest.
curl -fsS -m 10 "$PING_URL" >/dev/null 2>&1 || true
