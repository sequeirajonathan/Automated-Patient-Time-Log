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
# aptlog-agent is deliberately absent until REQ-6 exists: `apt-log run` is still
# the placeholder that exits 2, so gating deploys on it would roll back every
# revision forever now that the health check actually works. Put it back the
# moment the agent has a main loop -- it is the service that matters most here.
SERVICES=(aptlog-ui)
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

# ------------------------------------------------------------ git safe.directory
# The repo is owned by the service user; this script runs as root. Modern git
# refuses that combination outright -- "detected dubious ownership" -- and every
# command below is a git command, so the whole self-update and rollback path dies
# silently on a timer nobody is watching. Found on the Florida unit, where the
# manager had failed on every single fire since install.
#
# Git's objection is a real one: root executing from a tree a lesser user can
# write is an escalation path. It is also a property this design already accepts
# and documents -- OPERATIONS.md section 2.4 states that push access to the
# deploy branch is root on the Pi, because the manager installs unit files. So
# this records a decision already made rather than waving away a surprise.
#
# --system, not --global: under systemd HOME is not reliably /root, and a global
# config written to the wrong home is a fix that silently does nothing.
if ! git config --system --get-all safe.directory 2>/dev/null | grep -qx "$APP_DIR"; then
    git config --system --add safe.directory "$APP_DIR" || \
        log "could not mark $APP_DIR safe for root -- git operations will fail"
fi

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

    sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR[dev]" \
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
# `systemctl is-active --quiet a b c` exits 0 when ANY ONE of them is active,
# not all. This gate was written as though it meant all, so on the Florida unit a
# dead, crash-looping agent passed the health check because the UI beside it was
# up -- and the deploy was blessed and recorded as last-good. Every rollback this
# mechanism claims to provide was fiction for as long as one service survived.
#
# Checked one at a time, where the exit code means what it reads like.
all_active() {
    local unit
    for unit in "${SERVICES[@]}"; do
        systemctl is-active --quiet "$unit" || return 1
    done
    return 0
}

healthy() {
    # Give services time to bind before believing a failure.
    for _ in $(seq 1 12); do
        sleep 5
        all_active || continue
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
# Install the target's dependencies BEFORE testing it. Skipping this produced two
# distinct failures on the Florida unit, and the second one is unrecoverable.
#
# The mild version: a revision that adds a dependency gets tested against the
# previous revision's venv, fails for a reason that has nothing to do with its
# tests, and rolls back.
#
# The serious version: a venv with no pytest in it at all reports "TESTS FAILED"
# and rolls back -- and rolling back can never install pytest. The machine then
# refuses every future update forever, with the rollback being the thing holding
# it stuck. That is not a failing gate, it is a deadlock, and it took a hand-run
# pip install over SSH to break. On a box in someone else's building, that is a
# trip.
#
# "Cannot run the tests" and "the tests failed" are different answers. Only the
# second one means the revision is bad.
if ! sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR[dev]"; then
    log "DEPENDENCIES WOULD NOT INSTALL for ${target:0:8} — staying on ${current:0:8}"
    apply "$current" || log "rollback to ${current:0:8} reported errors"
    alert "Deploy blocked: dependencies would not install for ${target:0:8}"
    exit 1
fi

if ! sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/python" -c "import pytest" 2>/dev/null; then
    # Distinct message on purpose. This exact condition spent an hour looking
    # like a failing test suite, which sent every diagnosis in the wrong
    # direction -- toward the revision under test rather than the environment
    # meant to be judging it.
    log "CANNOT RUN TESTS: pytest missing after install — staying on ${current:0:8}"
    apply "$current" || log "rollback to ${current:0:8} reported errors"
    alert "Deploy blocked: test environment is broken on ${target:0:8}, not the tests"
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
