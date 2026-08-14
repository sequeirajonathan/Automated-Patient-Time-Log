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

# Never let a missing or broken alerter abort a deploy path. A `|| true` on a
# direct call would swallow a 127 silently, which is the same as having no
# alerting at all.
alert() {
    if [[ -x "$APP_DIR/scripts/alert.sh" ]]; then
        "$APP_DIR/scripts/alert.sh" "$1" || log "alert.sh failed: $1"
    else
        log "NO ALERTER (scripts/alert.sh missing or not executable): $1"
        logger -t aptlog-manager "$1" 2>/dev/null || true
    fi
}

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
# Applies a revision. Deliberately tolerant of internal failure: this runs on the
# ROLLBACK path too, where `set -e` aborting partway would leave the services
# stopped and nobody in the room to notice. Always reaches the restart.
apply() {
    local ref="$1"
    local rc=0

    git checkout --quiet --force "$ref" || { log "checkout of $ref FAILED"; rc=1; }

    sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR" \
        || { log "pip install FAILED on $ref"; rc=1; }

    # Units can change between revisions; reinstall before restarting.
    if [[ -d "$APP_DIR/deploy/systemd" ]]; then
        install -m 0644 "$APP_DIR"/deploy/systemd/*.service /etc/systemd/system/ 2>/dev/null || true
        install -m 0644 "$APP_DIR"/deploy/systemd/*.timer   /etc/systemd/system/ 2>/dev/null || true
        systemctl daemon-reload || true
    fi

    # Unconditional: whatever went wrong above, the services must come back up.
    systemctl restart "${SERVICES[@]}" || { log "service restart FAILED"; rc=1; }

    return "$rc"
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
if ! git checkout --quiet --force "$target"; then
    log "cannot check out ${target:0:8} — staying on ${current:0:8}"
    apply "$current" || log "restore to ${current:0:8} reported errors"
    alert "Deploy blocked: could not check out ${target:0:8}"
    exit 1
fi
if ! sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/python" -m pytest -q --maxfail=1 \
        "$APP_DIR/tests" 2>&1 | tail -20; then
    log "TESTS FAILED — staying on ${current:0:8}"
    apply "$current" || log "rollback to ${current:0:8} reported errors"
    alert "Deploy blocked: tests failed on ${target:0:8}"
    exit 1
fi

log "deploying ${target:0:8}"
apply "$target" || log "deploy of ${target:0:8} reported errors"

if healthy; then
    log "healthy on ${target:0:8}"
    echo "$target" > "$LAST_GOOD"
else
    log "UNHEALTHY — rolling back to ${current:0:8}"
    apply "$current" || log "rollback to ${current:0:8} reported errors"
    if healthy; then
        alert "Rolled back to ${current:0:8}; ${target:0:8} failed health check"
    else
        # Both revisions down is the worst case and must be loud.
        alert "CRITICAL: unhealthy after rollback to ${current:0:8}"
    fi
    exit 1
fi
