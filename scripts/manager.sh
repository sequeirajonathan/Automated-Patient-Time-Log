#!/usr/bin/env bash
# Self-updating deploy manager.
#
# Polls the deploy branch, and when it moves: updates, verifies, and restarts.
# If the new revision fails to come up healthy, rolls back to the previous one.
#
# Pull-based on purpose — the Pi sits behind the building's NAT, so nothing
# inbound is required for a deploy. Run from aptlog-manager.timer.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/aptlog}"
DEPLOY_REF="${DEPLOY_REF:-release}"
SERVICE_USER="${SERVICE_USER:-apt}"
STATE_DIR="/var/lib/aptlog"
LAST_GOOD="$STATE_DIR/last-good-sha"
SERVICES=(aptlog-agent aptlog-ui)
HEALTH_URL="http://127.0.0.1:8080/healthz"

mkdir -p "$STATE_DIR"
log() { printf '[manager] %s\n' "$1"; }

cd "$APP_DIR"

# ------------------------------------------------------------------ is there work
git fetch --quiet origin "$DEPLOY_REF"
current="$(git rev-parse HEAD)"
target="$(git rev-parse "origin/$DEPLOY_REF")"

if [[ "$current" == "$target" ]]; then
    exit 0
fi

log "update available: ${current:0:8} -> ${target:0:8}"
echo "$current" > "$LAST_GOOD"

# --------------------------------------------------------------------- apply it
apply() {
    local ref="$1"
    git checkout --quiet --force "$ref"
    sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR"

    # Units can change between revisions; reinstall before restarting.
    if [[ -d "$APP_DIR/deploy/systemd" ]]; then
        install -m 0644 "$APP_DIR"/deploy/systemd/*.service /etc/systemd/system/
        install -m 0644 "$APP_DIR"/deploy/systemd/*.timer   /etc/systemd/system/ 2>/dev/null || true
        systemctl daemon-reload
    fi

    systemctl restart "${SERVICES[@]}"
}

# ------------------------------------------------------------------ verify it
healthy() {
    # Give services time to bind before believing a failure.
    for _ in $(seq 1 12); do
        sleep 5
        systemctl is-active --quiet "${SERVICES[@]}" || continue
        curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1 && return 0
    done
    return 1
}

log "running tests against ${target:0:8}"
git checkout --quiet --force "$target"
if ! sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/python" -m pytest -q --maxfail=1 \
        "$APP_DIR/tests" 2>&1 | tail -20; then
    log "TESTS FAILED — staying on ${current:0:8}"
    apply "$current"
    "$APP_DIR/scripts/alert.sh" "Deploy blocked: tests failed on ${target:0:8}" || true
    exit 1
fi

log "deploying ${target:0:8}"
apply "$target"

if healthy; then
    log "healthy on ${target:0:8}"
    echo "$target" > "$LAST_GOOD"
else
    log "UNHEALTHY — rolling back to ${current:0:8}"
    apply "$current"
    if healthy; then
        "$APP_DIR/scripts/alert.sh" "Rolled back to ${current:0:8}; ${target:0:8} failed health check" || true
    else
        # Both revisions down is the worst case and must be loud.
        "$APP_DIR/scripts/alert.sh" "CRITICAL: unhealthy after rollback to ${current:0:8}" || true
    fi
    exit 1
fi
