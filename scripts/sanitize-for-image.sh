#!/usr/bin/env bash
# Strip per-device identity and all operational data, immediately before capturing
# this card as a golden image.
#
#   sudo /opt/aptlog/scripts/sanitize-for-image.sh
#   sudo shutdown -h now
#
# DO NOT BOOT AGAIN AFTERWARDS. First boot regenerates everything removed here; a
# rebooted machine has re-acquired an identity and is no longer safe to clone.
# If it is booted by accident, re-run this script.

set -euo pipefail

BOOT_DIR="${BOOT_DIR:-/boot/firmware}"
STATE_DIR="${STATE_DIR:-/var/lib/aptlog}"
SERVICE_USER="${SERVICE_USER:-apt}"

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
log() { printf '[sanitize] %s\n' "$1"; }

# ------------------------------------------------------- stop everything first
log "stopping services"
systemctl stop aptlog-agent aptlog-ui aptlog-appium aptlog-heartbeat.timer \
               aptlog-manager.timer 2>/dev/null || true

# ------------------------------------------------------------- operational data
# Patient data must never travel inside an image file.
log "clearing operational data"
rm -rf "${STATE_DIR:?}"/* 2>/dev/null || true
mkdir -p "$STATE_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR"

# ------------------------------------------------------------------- secrets
log "clearing secrets"
rm -f /etc/aptlog/secrets.env /etc/aptlog/*.key 2>/dev/null || true
# The schedule is not a credential and it is removed for a different reason:
# it names six people, their homes' hours, and who cares for them. An image is
# copied, handed over and archived, and none of that is a place for it.
# site.conf goes with it — it is one building's particulars (config.py).
rm -f /etc/aptlog/schedule.json /etc/aptlog/site.conf 2>/dev/null || true
rm -rf "/home/$SERVICE_USER/.local/share/python_keyring" \
       "/home/$SERVICE_USER/.android" 2>/dev/null || true

# ------------------------------------------------------------------ tailscale
# A clone carrying this state would claim the source Pi's node identity.
log "removing tailscale node identity"
systemctl stop tailscaled 2>/dev/null || true
rm -rf /var/lib/tailscale/* 2>/dev/null || true

# ------------------------------------------------------------- ssh host keys
# Two machines presenting the same host key is both a security problem and a
# source of host-key-mismatch errors on every laptop that has connected.
log "removing ssh host keys"
rm -f /etc/ssh/ssh_host_*

# ------------------------------------------------------------------ machine-id
# Duplicates confuse journald and cause both machines to request the same DHCP
# lease identity.
log "clearing machine-id"
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
ln -sf /etc/machine-id /var/lib/dbus/machine-id

# --------------------------------------------------------------- logs, history
log "clearing logs and history"
journalctl --rotate --quiet 2>/dev/null || true
journalctl --vacuum-time=1s --quiet 2>/dev/null || true
rm -rf /var/log/*.log /var/log/*.gz /var/log/journal/* 2>/dev/null || true
rm -f "/home/$SERVICE_USER/.bash_history" /root/.bash_history 2>/dev/null || true
rm -f /var/lib/dhcp/* /var/lib/dhcpcd/* 2>/dev/null || true

# ------------------------------------------------- stage the auth key placeholder
# FAT32, so the recipient can edit this in Notepad on Windows before the card ever
# goes into the Pi. Keeps a live credential out of the shipped image.
log "staging boot-partition auth key placeholder"
cat > "$BOOT_DIR/tailscale-authkey.txt" <<'EOF'
# Paste the Tailscale auth key on the line below, save, and close.
# It should start with:  tskey-auth-
# (A key starting with tskey-api- is the wrong kind and will not work.)

EOF

# -------------------------------------------------------------- arm first boot
log "arming first-boot service"
systemctl enable aptlog-firstrun.service 2>/dev/null || \
    echo "!! aptlog-firstrun.service missing — the clone will not self-configure" >&2

sync
log "done — shut down now and DO NOT boot again"
