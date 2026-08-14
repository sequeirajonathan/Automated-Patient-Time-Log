#!/usr/bin/env bash
# Join the tailnet from an ephemeral cloud session, then verify the Pi is
# actually reachable.
#
#   export TS_EPHEMERAL_KEY='tskey-auth-...'
#   bash scripts/cloud-session-up.sh
#
# Uses userspace networking because a cloud sandbox usually has no /dev/net/tun
# and no NET_ADMIN. The consequence is that a plain `ssh` will NOT route through
# the tunnel — use `tailscale ssh`, or the SOCKS5 proxy this starts. See
# docs/OPERATIONS.md section 3.2.
#
# Exits non-zero with a specific reason if the tunnel cannot be established, so
# a failure here is a clear answer ("this environment blocks Tailscale") rather
# than a hang.

set -uo pipefail

PI_HOST="${PI_HOST:-aptlog}"
SOCKS_PORT="${SOCKS_PORT:-1055}"
STATE="${TS_STATE:-/tmp/tailscaled.state}"
SOCK="${TS_SOCK:-/tmp/tailscaled.sock}"

log()  { printf '\n=== %s\n' "$1"; }
die()  { printf '\n!! %s\n' "$1" >&2; exit 1; }

[[ -n "${TS_EPHEMERAL_KEY:-}" ]] || die "TS_EPHEMERAL_KEY is not set.
Generate one at login.tailscale.com -> Settings -> Keys:
  Reusable: YES   Ephemeral: YES   Tags: tag:cloud
Ephemeral so dead container nodes clean themselves up instead of accumulating."

case "$TS_EPHEMERAL_KEY" in
    tskey-auth-*) : ;;
    tskey-api-*)  die "That is an API access token (tskey-api-), not an auth key.
It manages the tailnet and cannot enrol a node. Generate an auth key instead." ;;
    *)            die "Key does not start with tskey-auth-. Wrong value?" ;;
esac

# ---------------------------------------------------------------------- install
if ! command -v tailscale >/dev/null; then
    log "installing tailscale"
    curl -fsSL https://tailscale.com/install.sh | sh || die "install failed"
fi

# ---------------------------------------------------------------------- daemon
if ! tailscale --socket="$SOCK" status >/dev/null 2>&1; then
    log "starting tailscaled (userspace networking)"
    tailscaled \
        --tun=userspace-networking \
        --socks5-server="localhost:${SOCKS_PORT}" \
        --outbound-http-proxy-listen="localhost:$((SOCKS_PORT + 1))" \
        --socket="$SOCK" \
        --statedir="$(dirname "$STATE")" \
        >/tmp/tailscaled.log 2>&1 &

    for _ in $(seq 1 20); do
        sleep 1
        tailscale --socket="$SOCK" status >/dev/null 2>&1 && break
    done
fi

# -------------------------------------------------------------------------- up
log "joining tailnet"
if ! tailscale --socket="$SOCK" up \
        --authkey="$TS_EPHEMERAL_KEY" \
        --hostname="cloud-$(date +%s)" \
        --accept-routes=false \
        --timeout=90s; then
    printf '\n--- tailscaled.log (tail) ---\n' >&2
    tail -30 /tmp/tailscaled.log >&2
    die "Could not join the tailnet.

Most likely this environment blocks Tailscale egress. It prefers UDP 41641 and
falls back to DERP over TCP/443; a restrictive policy can block both.

This costs interactive debugging, NOT the ability to ship: deploys are pull-based
(docs/OPERATIONS.md section 2), so pushing to the release branch still reaches the
Pi without any tunnel."
fi

# -------------------------------------------------------------------- verify
log "tailnet status"
tailscale --socket="$SOCK" status

log "can we see ${PI_HOST}?"
if ! tailscale --socket="$SOCK" status | grep -q "[[:space:]]${PI_HOST}[[:space:]]"; then
    die "${PI_HOST} is not visible on the tailnet. Is the Pi online, and does the
ACL policy permit this node to see it?"
fi

log "pinging ${PI_HOST} through the tunnel"
tailscale --socket="$SOCK" ping --c=3 --until-direct=false "$PI_HOST" \
    || printf '!! ping failed — DERP relay may be blocked\n' >&2

cat <<EOF

=== ready ===

Use this. Tailscale SSH, authenticated by the tailnet policy — no private key
needed anywhere in this container:

    tailscale --socket=$SOCK ssh apt@${PI_HOST}

Do NOT reach for the SOCKS5 proxy as a substitute. It carries you to the Pi's
own sshd rather than to Tailscale SSH, so it wants a private key this container
does not have and fails with "Permission denied (publickey,password)". Verified,
not assumed. The proxy is for other TCP traffic:

    ALL_PROXY=socks5://localhost:${SOCKS_PORT} <some-tcp-client>

If Tailscale SSH itself is refused, the tailnet policy is missing an "ssh" block
whose dst matches this node's target. "tailscale up --ssh" on the Pi only starts
the server; access is denied by default until the policy grants it. See
docs/OPERATIONS.md section 3.2.

EOF
