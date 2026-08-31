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
# it names the people cared for, their hours, and who cares for them. An image is
# copied, handed over and archived, and none of that is a place for it.
# site.conf goes with it — it is one building's particulars (config.py).
rm -f /etc/aptlog/schedule.json /etc/aptlog/site.conf 2>/dev/null || true
# The adopted signatures, for the same reason one degree stronger: the schedule
# NAMES the people cared for, and this REPRODUCES THEIR SIGNATURES. A stroke set
# is the thing itself, not a reference to it, and an image is copied, handed over
# and archived. It goes before the image does, without exception.
#
# BOTH PATHS. The store lives in the state directory now — /etc/aptlog is root-
# owned so the service could not write there — and the old path is still swept
# because a Pi imaged today may have been registered on before the move.
# Removing a file that is not there costs nothing; leaving one behind costs a
# patient's signature.
rm -f /var/lib/aptlog/signatures.json /etc/aptlog/signatures.json \
      2>/dev/null || true
# The trail of applications, which names patients and dates. Not reproducible
# ink, and not something to hand over either.
rm -f /var/lib/aptlog/signings.jsonl 2>/dev/null || true
# THE FIRE LEDGER, for the same reason and it was missed. It is the record of
# EVV entries this machine asserted: dates, which app, and WHO ATTESTED that
# the caregiver was present. The occurrence keys are hashes, which is why this
# looked harmless, but the attestation is a person's name and the dates are a
# person's working week. It now also quotes the message of a failed fire, and
# a macro's message can name whatever was on the screen.
rm -f /var/lib/aptlog/fired.json 2>/dev/null || true
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
