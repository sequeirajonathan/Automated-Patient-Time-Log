#!/usr/bin/env bash
# REQ-9 alerting. Delivers a message outward, to a person who is not in the room.
#
#   alert.sh "message"                        # from manager.sh, the agent, anywhere
#   alert.sh --unit aptlog-agent.service      # from systemd OnFailure=
#
# Configure in /etc/aptlog/alert.env (mode 0600):
#   ALERT_NTFY_TOPIC=https://ntfy.sh/<something-unguessable>
#   # or
#   ALERT_PUSHOVER_TOKEN=...
#   ALERT_PUSHOVER_USER=...
#
# Design notes:
#   - Never fails the caller. An alert that breaks a rollback is worse than a
#     missed alert.
#   - Never silent. If it cannot deliver, it says so in the journal, because a
#     silent alerting path is indistinguishable from a healthy system — which is
#     the exact failure this whole project is built to avoid.

set -uo pipefail

CONF="${ALERT_CONF:-/etc/aptlog/alert.env}"
HOSTNAME_SHORT="$(hostname -s 2>/dev/null || echo aptlog)"

# shellcheck disable=SC1090
[[ -r "$CONF" ]] && . "$CONF"

log() { logger -t aptlog-alert "$1" 2>/dev/null || echo "aptlog-alert: $1" >&2; }

# ------------------------------------------------------------------ build message
if [[ "${1:-}" == "--unit" ]]; then
    unit="${2:-unknown}"
    # Called by systemd OnFailure=; pull the tail of the unit's log for context,
    # since whoever reads this alert cannot get to the machine.
    detail="$(journalctl -u "$unit" -n 15 --no-pager -o cat 2>/dev/null | tail -c 800)"
    message="[$HOSTNAME_SHORT] unit failed: $unit"
    [[ -n "$detail" ]] && message="$message"$'\n\n'"$detail"
else
    message="[$HOSTNAME_SHORT] ${*:-unspecified alert}"
fi

# ---------------------------------------------------------------------- deliver
delivered=0

if [[ -n "${ALERT_NTFY_TOPIC:-}" ]]; then
    if curl -fsS -m 15 -H "Title: APT Log" -H "Priority: high" \
            -d "$message" "$ALERT_NTFY_TOPIC" >/dev/null 2>&1; then
        delivered=1
    else
        log "ntfy delivery FAILED"
    fi
fi

if [[ -n "${ALERT_PUSHOVER_TOKEN:-}" && -n "${ALERT_PUSHOVER_USER:-}" ]]; then
    if curl -fsS -m 15 \
            --form-string "token=$ALERT_PUSHOVER_TOKEN" \
            --form-string "user=$ALERT_PUSHOVER_USER" \
            --form-string "title=APT Log" \
            --form-string "message=$message" \
            https://api.pushover.net/1/messages.json >/dev/null 2>&1; then
        delivered=1
    else
        log "pushover delivery FAILED"
    fi
fi

# Always leave a local trace, whether or not anything went out.
log "$message"

if [[ "$delivered" -eq 0 ]]; then
    log "NO ALERT CHANNEL CONFIGURED — nothing was sent. Set ALERT_NTFY_TOPIC or ALERT_PUSHOVER_* in $CONF"
fi

# Deliberately always succeeds: callers use this on their failure paths.
exit 0
