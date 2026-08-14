#!/usr/bin/env bash
# Write /etc/aptlog/secrets.env (REQ-3), prompting for each value.
#
#   sudo /opt/aptlog/scripts/set-credentials.sh
#
# Run this on the device. The values are read into shell variables and written
# straight to the file: they are never command-line arguments, so they do not
# appear in `ps` or in shell history, and they are never transmitted anywhere.
#
# The password is not echoed while typing.

set -euo pipefail

CONF_DIR="${CONF_DIR:-/etc/aptlog}"
SECRETS="$CONF_DIR/secrets.env"
SERVICE_USER="${SERVICE_USER:-apt}"

[[ $EUID -eq 0 ]] || { echo "run with sudo — $CONF_DIR is root-owned" >&2; exit 1; }

umask 077

if [[ -f "$SECRETS" ]]; then
    echo "!! $SECRETS already exists and will be replaced."
    read -rp "   continue? [y/N] " ok
    [[ "$ok" == [yY] ]] || { echo "aborted"; exit 1; }
fi

echo
echo "Agency app credentials. These are stored in plaintext on this device —"
echo "full-disk encryption is incompatible with unattended reboot (PI_SETUP.md)."
echo "If this SD card is ever lost, rotate the password."
echo

read -rp  "App email/username : " APP_USER
[[ -n "$APP_USER" ]] || { echo "username cannot be empty" >&2; exit 1; }

read -rsp "App password       : " APP_PASS; echo
[[ -n "$APP_PASS" ]] || { echo "password cannot be empty" >&2; exit 1; }

read -rsp "Confirm password   : " APP_PASS2; echo
[[ "$APP_PASS" == "$APP_PASS2" ]] || { echo "passwords do not match" >&2; exit 1; }

read -rp  "Phone unlock PIN   : " PHONE_PIN
if [[ -n "$PHONE_PIN" && ! "$PHONE_PIN" =~ ^[0-9]+$ ]]; then
    # `input text` cannot draw a pattern or present a fingerprint.
    echo "PIN must be numeric — pattern and biometric locks cannot be scripted" >&2
    exit 1
fi

install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 /dev/null "$SECRETS"
{
    printf 'APP_USERNAME=%s\n' "$APP_USER"
    printf 'APP_PASSWORD=%s\n' "$APP_PASS"
    [[ -n "$PHONE_PIN" ]] && printf 'PHONE_PIN=%s\n' "$PHONE_PIN"
} > "$SECRETS"

unset APP_PASS APP_PASS2 PHONE_PIN

echo
ls -l "$SECRETS"

# Prove it parses before anyone finds out at 3am. Prints lengths, never values.
VENV="/opt/aptlog/.venv/bin/python"
[[ -x "$VENV" ]] || VENV="$(command -v python3)"
sudo -u "$SERVICE_USER" "$VENV" - <<'PY' 2>/dev/null || echo "  (could not verify — check PYTHONPATH)"
import sys
sys.path.insert(0, "/opt/aptlog/src")
try:
    from apt_log.secrets import (APP_PASSWORD, APP_USERNAME, PHONE_PIN,
                                 FileSecretProvider, SecretNotFound)
except ImportError:
    raise SystemExit(1)
p = FileSecretProvider()
print("  APP_USERNAME : %s" % p.get(APP_USERNAME))
print("  APP_PASSWORD : set (%d chars)" % len(p.get(APP_PASSWORD)))
try:
    print("  PHONE_PIN    : set (%d digits)" % len(p.get(PHONE_PIN)))
except SecretNotFound:
    print("  PHONE_PIN    : not set (swipe-only lock assumed)")
PY

echo
echo "Done. sanitize-for-image.sh strips this file before a golden image is"
echo "captured, so it will not travel to another device."
