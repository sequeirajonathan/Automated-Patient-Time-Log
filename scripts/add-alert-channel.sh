#!/usr/bin/env bash
# Where the Pi reaches you when it needs something.
#
#   sudo scripts/add-alert-channel.sh
#
# There is one thing this system cannot do for itself: the login code inMyTeam
# texts you. It stops, asks, and — with this configured — tells your phone it
# is asking. The same channel already carries deploy failures and any unit that
# dies (REQ-9), so setting it up once covers all of them.
#
# ntfy is the default because it needs no account: a topic is just an
# unguessable string, the iOS app is free, and a notification can carry a link
# that opens the portal at the field you need. Pushover works too and is
# offered for anyone who already has it.
#
# What goes over this channel: "inMyTeam texted you a sign-in code", "a deploy
# failed", "a unit died". Never a patient, never a visit, never a credential —
# it is a public relay and it lands on a lock screen, and neither is a place
# for any of that.
set -uo pipefail

CONF="${ALERT_CONF:-/etc/aptlog/alert.env}"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "run this with sudo — $CONF is root-owned and mode 0600" >&2
    exit 1
fi

echo "Where should the Pi reach you?"
echo "  1) ntfy      — free, no account; install 'ntfy' from the App Store"
echo "  2) Pushover  — if you already use it"
read -rp "Choose [1]: " CHOICE || true
CHOICE="${CHOICE:-1}"

NTFY="" ; PUSH_TOKEN="" ; PUSH_USER=""

case "$CHOICE" in
  1)
    echo
    echo "Pick a topic nobody could guess — it is the only thing protecting it."
    echo "Anything unguessable works, e.g. aptlog-$(head -c 6 /dev/urandom | base64 | tr -dc 'a-z0-9')"
    read -rp "Topic name: " TOPIC || true
    if [[ -z "${TOPIC:-}" ]]; then echo "nothing entered; giving up" >&2; exit 1; fi
    # Accept a bare topic or a whole URL, since both are natural to paste.
    if [[ "$TOPIC" == http*://* ]]; then NTFY="$TOPIC"; else NTFY="https://ntfy.sh/$TOPIC"; fi
    echo
    echo "Now subscribe to that same topic in the ntfy app on your phone."
    ;;
  2)
    read -rp "Pushover application token: " PUSH_TOKEN || true
    read -rp "Pushover user key         : " PUSH_USER || true
    if [[ -z "${PUSH_TOKEN:-}" || -z "${PUSH_USER:-}" ]]; then
        echo "both are needed; giving up" >&2; exit 1
    fi
    ;;
  *) echo "unrecognised choice" >&2; exit 1 ;;
esac

mkdir -p "$(dirname "$CONF")"
umask 077
{
    echo "# Written by scripts/add-alert-channel.sh"
    [[ -n "$NTFY" ]] && echo "ALERT_NTFY_TOPIC=$NTFY"
    [[ -n "$PUSH_TOKEN" ]] && echo "ALERT_PUSHOVER_TOKEN=$PUSH_TOKEN"
    [[ -n "$PUSH_USER" ]] && echo "ALERT_PUSHOVER_USER=$PUSH_USER"
} > "$CONF"
chmod 600 "$CONF"
chown root:root "$CONF" 2>/dev/null || true
unset PUSH_TOKEN PUSH_USER

echo
echo "Wrote $CONF (mode 0600)."
echo "Sending a test notification — it should appear on your phone."
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$HERE/alert.sh" --url "${APTLOG_PORTAL_URL:-https://aptlog-fl/app}" \
    "Test from the controller. If you can read this and tapping it opens the portal, the code notification will reach you too."
echo
echo "Nothing arrived? Check the journal, which never stays quiet about it:"
echo "  journalctl -t aptlog-alert -n 10 --no-pager"
