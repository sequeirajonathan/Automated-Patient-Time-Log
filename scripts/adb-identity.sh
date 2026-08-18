#!/usr/bin/env bash
# The phone knows this box by ONE RSA key: /home/apt/.android/adbkey.
#
# Lose that key — a wiped home, a regenerated pair, an adb server started
# by another user presenting its own key — and the phone stops trusting
# the Pi until a human taps "Allow USB debugging" on its screen. That tap
# landed in the middle of the first field test, twice, with a patient
# waiting. It must never be needed again.
#
# Three protections, all idempotent, run by firstrun and by every deploy:
#
#   1. VAULT.  The key pair is copied to /etc/aptlog/adb-identity (root,
#      0600) the first time this runs. From then on the vault is the
#      identity: a missing live key is restored FROM it, and a live key
#      that differs (something regenerated it) is overwritten BY it —
#      loudly, because the old key is the one the phone authorized.
#      Rotating on purpose means replacing the vault copy first.
#
#   2. ONE KEY FOR EVERY SERVER.  ADB_VENDOR_KEYS is exported system-wide
#      (profile.d) and in the appium unit, pointed at the same file — so
#      even an adb server started by root presents the identity the phone
#      already trusts, instead of minting a stranger.
#
#   3. NOTHING HERE TOUCHES THE PHONE.  Deploys restart services; they
#      never restart the phone's trust. If adb still says "unauthorized"
#      after this, the cause is physical (the USB cable re-enumerating —
#      it did that four times on the night of the first field test) or
#      the phone-side store was cleared; the fix is the "Always allow
#      from this computer" checkbox, once.
set -euo pipefail

KEY_DIR="/home/apt/.android"
VAULT="/etc/aptlog/adb-identity"
PROFILE="/etc/profile.d/adb-identity.sh"

log() { printf '[adb-identity] %s\n' "$1"; }

mkdir -p "$VAULT"
chmod 700 "$VAULT"

if [[ ! -f "$VAULT/adbkey" && -f "$KEY_DIR/adbkey" ]]; then
    cp "$KEY_DIR/adbkey" "$KEY_DIR/adbkey.pub" "$VAULT/"
    chmod 600 "$VAULT/adbkey"
    log "identity vaulted"
fi

if [[ -f "$VAULT/adbkey" ]]; then
    if [[ ! -f "$KEY_DIR/adbkey" ]]; then
        mkdir -p "$KEY_DIR"
        cp "$VAULT/adbkey" "$VAULT/adbkey.pub" "$KEY_DIR/"
        chown -R apt:apt "$KEY_DIR"
        chmod 600 "$KEY_DIR/adbkey"
        log "live key was MISSING; identity restored from the vault"
    elif ! cmp -s "$VAULT/adbkey" "$KEY_DIR/adbkey"; then
        cp "$VAULT/adbkey" "$VAULT/adbkey.pub" "$KEY_DIR/"
        chown apt:apt "$KEY_DIR/adbkey" "$KEY_DIR/adbkey.pub"
        chmod 600 "$KEY_DIR/adbkey"
        log "live key DIFFERED from the vault; the authorized identity" \
            "was put back (rotate on purpose by replacing the vault first)"
    fi
fi

cat > "$PROFILE" <<'EOF'
# Every adb server on this box presents the ONE key the phone authorized.
# See scripts/adb-identity.sh in the aptlog repo.
export ADB_VENDOR_KEYS=/home/apt/.android/adbkey
EOF
chmod 644 "$PROFILE"
