#!/usr/bin/env bash
# Push-to-deploy: `git push pi release` and the gate runs at once.
#
#   sudo /opt/aptlog/scripts/install-push-deploy.sh
#
# WHY NOT A GITHUB WEBHOOK. A webhook needs an address GitHub can reach, and
# this box sits behind a building's NAT — so it would mean publishing a port
# on the phone's own controller to the open internet (Tailscale Funnel or a
# forwarded port), then defending it with signature checks, for a fleet of
# one machine that the person deploying can already reach over the tailnet.
# A push over that tailnet is the same event, arriving the same instant,
# through a door that is already there and already authenticated.
#
# It is also better to WATCH: git streams a hook's output back to whoever
# pushed, so the gate's own log — tests, deploy, health check — prints in the
# terminal that ran the push, and the push does not return until the deploy
# has finished. Nothing to poll, and no wondering which revision is live.
#
# The timer stays. It is what catches a revision pushed to GitHub from
# somewhere else, and what heals the machine if a push-deploy is interrupted
# halfway — reconciliation, now that it is no longer the only way in.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/aptlog}"
BARE="${BARE:-/opt/aptlog.git}"
SERVICE_USER="${SERVICE_USER:-apt}"
DEPLOY_REF="${DEPLOY_REF:-release}"

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

# ------------------------------------------------------------------ the bare repo
if [[ ! -d "$BARE" ]]; then
    git init --quiet --bare "$BARE"
    echo "created $BARE"
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$BARE"
git config --system --add safe.directory "$BARE" 2>/dev/null || true

# ------------------------------------------------------------------- the hook
cat > "$BARE/hooks/post-receive" <<'HOOK'
#!/usr/bin/env bash
# Runs as the pushing user, with the push still open: everything printed here
# reaches the terminal that ran `git push`, and the push waits for it.
set -uo pipefail

DEPLOY_REF="${DEPLOY_REF:-release}"
APP_DIR="/opt/aptlog"

while read -r _old new ref; do
    branch="${ref#refs/heads/}"
    [[ "$branch" == "$DEPLOY_REF" ]] || {
        echo "[deploy] $branch is not the deploy branch; stored, not deployed"
        continue
    }
    echo "[deploy] ${new:0:8} received — running the gate"
    # The SAME manager that the timer runs, pointed at this repo instead of
    # GitHub. One gate, one rollback path, no second thinner copy to drift.
    # It needs root (it installs unit files and restarts services); the
    # service user's passwordless sudo is what the rest of the operations
    # tooling already relies on.
    sudo -n env DEPLOY_REMOTE=push DEPLOY_REF="$DEPLOY_REF" \
        "$APP_DIR/scripts/manager.sh"
    rc=$?
    live="$(git --git-dir="$APP_DIR/.git" rev-parse HEAD)"
    if [[ $rc -eq 0 ]]; then
        echo "[deploy] live: ${live:0:7}"
    else
        # The manager has already rolled back and alerted by here. Saying so
        # again in the pusher's terminal is the point: a deploy that failed
        # must not look like a deploy that worked.
        echo "[deploy] REFUSED — the gate rejected ${new:0:8}; the machine is"
        echo "[deploy] still on ${live:0:7}"
        # And the BRANCH must not claim otherwise. Git ignores what a
        # post-receive hook exits with — the refs are already written by the
        # time it runs, so `git push` reports success even for a revision
        # that was refused. The exit status cannot be made honest from here;
        # the repository can. Winding the branch back to what is actually
        # running means `git ls-remote pi release` answers the question the
        # exit code cannot, and a second push of a fixed revision is an
        # ordinary fast-forward rather than a force.
        if [[ -n "$_old" && "$_old" != "0000000000000000000000000000000000000000" ]]; then
            git update-ref "$ref" "$_old" "$new" \
                && echo "[deploy] $branch wound back to ${_old:0:7} — nothing here is live"
        else
            git update-ref -d "$ref" \
                && echo "[deploy] $branch removed — nothing here is live"
        fi
    fi
    exit $rc
done
HOOK
chmod 0755 "$BARE/hooks/post-receive"
chown "$SERVICE_USER:$SERVICE_USER" "$BARE/hooks/post-receive"

# --------------------------------------------------- the work tree's own remote
# The manager fetches from here by name, so the bare repo is a remote of the
# working copy rather than a path spelled out inside the script.
if ! git -C "$APP_DIR" remote get-url push >/dev/null 2>&1; then
    git -C "$APP_DIR" remote add push "$BARE"
else
    git -C "$APP_DIR" remote set-url push "$BARE"
fi

echo
echo "Push-to-deploy is installed. From a clone that can reach the tailnet:"
echo
echo "    git remote add pi $SERVICE_USER@aptlog-fl:$BARE"
echo "    git push pi HEAD:$DEPLOY_REF"
echo
echo "The gate's log prints in your terminal and the push returns when the"
echo "deploy has finished. The ten-minute timer stays as reconciliation."
