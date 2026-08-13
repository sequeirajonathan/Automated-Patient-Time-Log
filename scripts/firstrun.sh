#!/usr/bin/env bash
# Per-device setup, run once on the first boot of a cloned image, then disabled.
#
# Restores the identity that sanitize-for-image.sh deliberately stripped, so every
# unit flashed from the same image is a distinct machine.
#
# Runs from aptlog-firstrun.service. No human interaction — the only on-site input
# is the auth key pasted into tailscale-authkey.txt on the boot partition.

set -euo pipefail

BOOT_DIR="${BOOT_DIR:-/boot/firmware}"
KEYFILE="$BOOT_DIR/tailscale-authkey.txt"
STATE_DIR="${STATE_DIR:-/var/lib/aptlog}"
SERVICE_USER="${SERVICE_USER:-apt}"

log() { printf '[firstrun] %s\n' "$1"; systemd-cat -t aptlog-firstrun echo "$1" 2>/dev/null || true; }

# ------------------------------------------------------------------ machine-id
if [[ ! -s /etc/machine-id ]]; then
    log "generating machine-id"
    systemd-machine-id-setup
fi

# --------------------------------------------------------------- ssh host keys
if ! ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1; then
    log "generating ssh host keys"
    ssh-keygen -A
    systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
fi

# ------------------------------------------------------------------- state dir
mkdir -p "$STATE_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR"

# ------------------------------------------------------------------- tailscale
if ! tailscale status >/dev/null 2>&1; then
    if [[ -f "$KEYFILE" ]]; then
        # First non-comment, non-blank line.
        AUTHKEY="$(grep -vE '^\s*(#|$)' "$KEYFILE" | head -n1 | tr -d '[:space:]')"
    else
        AUTHKEY=""
    fi

    if [[ -z "$AUTHKEY" ]]; then
        log "NO AUTH KEY — edit tailscale-authkey.txt on the boot partition and reboot"
    elif [[ "$AUTHKEY" == tskey-api-* ]]; then
        log "WRONG KEY TYPE — that is an API token, not an auth key. Need tskey-auth-"
    else
        log "joining tailnet"
        if tailscale up --ssh --authkey="$AUTHKEY" --hostname=aptlog; then
            # The key has done its job; do not leave it readable on a FAT partition.
            log "clearing auth key from boot partition"
            cat > "$KEYFILE" <<'EOF'
# Key consumed successfully on first boot. Nothing further is needed here.
EOF
            sync
        else
            log "tailscale up FAILED — key may be expired or already used"
        fi
    fi
fi

# --------------------------------------------------------------- serve the UI
if tailscale status >/dev/null 2>&1; then
    log "publishing UI over tailscale serve"
    tailscale serve --bg 8080 2>/dev/null || log "serve failed — check HTTPS is enabled on the tailnet"
fi

# ----------------------------------------------------------------- services up
log "enabling services"
systemctl daemon-reload
for unit in aptlog-appium aptlog-agent aptlog-ui; do
    systemctl enable --now "$unit" 2>/dev/null || log "note: $unit not present"
done
systemctl enable --now aptlog-manager.timer aptlog-heartbeat.timer 2>/dev/null || true

# --------------------------------------------------------------- disable self
log "first run complete"
systemctl disable aptlog-firstrun.service 2>/dev/null || true
