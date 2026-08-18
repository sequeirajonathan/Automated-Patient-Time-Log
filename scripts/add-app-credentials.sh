#!/usr/bin/env bash
# Add or update the OTHER agency apps' credentials in /etc/aptlog/secrets.env
# WITHOUT touching the legacy HHAeXchange ones already there.
#
#   sudo /opt/aptlog/scripts/add-app-credentials.sh
#
# set-credentials.sh REPLACES the whole file, which was right on first setup
# and wrong ever after — running it to add a PIN would wipe the working
# password. This one edits keys in place and leaves everything else alone.
#
# Values are read into shell variables and written straight to the file:
# never command-line arguments, so they do not appear in `ps` or history.
# Press Enter at any prompt to leave that credential as it is.

set -euo pipefail

SECRETS="${SECRETS:-/etc/aptlog/secrets.env}"
SERVICE_USER="${SERVICE_USER:-apt}"

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ -f "$SECRETS" ]] || { echo "$SECRETS does not exist — run set-credentials.sh first" >&2; exit 1; }

umask 077

echo
echo "Adding credentials for the other agency apps. Enter skips a prompt and"
echo "keeps whatever is already stored. Stored in plaintext on this device —"
echo "if the SD card is ever lost, rotate these."
echo
echo "HHAeXchange+ uses the SAME account as the legacy HHAeXchange app, and"
echo "the sign-in falls back to those credentials by itself — press Enter"
echo "here unless the vendor ever gives it a separate login."
echo

read -rp  "HHAeXchange+ email          : " UMA_USER || true
UMA_PASS=""
if [[ -n "${UMA_USER}" ]]; then
    read -rsp "HHAeXchange+ password       : " UMA_PASS; echo
    read -rsp "Confirm password            : " UMA_PASS2; echo
    [[ "$UMA_PASS" == "$UMA_PASS2" ]] || { echo "passwords do not match" >&2; exit 1; }
fi

read -rp  "Mobile Caregiver+ PIN       : " MC_PIN_VAL || true
if [[ -n "${MC_PIN_VAL}" && ! "$MC_PIN_VAL" =~ ^[0-9]+$ ]]; then
    echo "the PIN is numeric — it is typed on a digit keypad" >&2
    exit 1
fi

echo
echo "inMyTeam signs in with the caregiver's CELL NUMBER (digits only, as"
echo "the app expects it typed). The SMS code arrives on the tethered phone"
echo "itself, so the sign-in can heal without anyone reading a text aloud."
read -rp  "inMyTeam phone number       : " IMT_PHONE_VAL || true
if [[ -n "${IMT_PHONE_VAL}" && ! "$IMT_PHONE_VAL" =~ ^[0-9]+$ ]]; then
    echo "digits only — no dashes, spaces, or country-code plus sign" >&2
    exit 1
fi

# Update-or-append each key, never disturbing the others.
upsert() {
    local key="$1" value="$2"
    [[ -n "$value" ]] || return 0
    if grep -q "^${key}=" "$SECRETS"; then
        # sed with | as delimiter; strip any | from the value first (none of
        # these credential types can contain one legitimately).
        value="${value//|/}"
        sed -i "s|^${key}=.*|${key}=${value}|" "$SECRETS"
    else
        printf '%s=%s\n' "$key" "$value" >> "$SECRETS"
    fi
}

upsert "UMA_USERNAME" "${UMA_USER:-}"
upsert "UMA_PASSWORD" "${UMA_PASS:-}"
upsert "MC_PIN"       "${MC_PIN_VAL:-}"
upsert "INMYTEAM_PHONE" "${IMT_PHONE_VAL:-}"

unset UMA_PASS UMA_PASS2 MC_PIN_VAL IMT_PHONE_VAL

chown "$SERVICE_USER:$SERVICE_USER" "$SECRETS"
chmod 0600 "$SECRETS"

echo
# Lengths, never values.
VENV="/opt/aptlog/.venv/bin/python"
[[ -x "$VENV" ]] || VENV="$(command -v python3)"
sudo -u "$SERVICE_USER" "$VENV" - <<'PY' 2>/dev/null || echo "  (could not verify)"
import sys
sys.path.insert(0, "/opt/aptlog/src")
from apt_log.secrets import (INMYTEAM_PHONE, MC_PIN, UMA_PASSWORD,
                             UMA_USERNAME, FileSecretProvider, SecretNotFound)
p = FileSecretProvider()
for label, key, masked in (("UMA_USERNAME", UMA_USERNAME, False),
                           ("UMA_PASSWORD", UMA_PASSWORD, True),
                           ("MC_PIN", MC_PIN, True),
                           ("INMYTEAM_PHONE", INMYTEAM_PHONE, True)):
    try:
        v = p.get(key)
        print("  %-13s: %s" % (label, v if not masked else f"set ({len(v)} chars)"))
    except SecretNotFound:
        print("  %-13s: not set" % label)
PY
echo
echo "Done. The legacy HHAeXchange credentials were not touched."
