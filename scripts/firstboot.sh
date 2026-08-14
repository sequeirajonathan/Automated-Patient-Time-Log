#!/usr/bin/env bash
# One-time bootstrap for the Raspberry Pi controller.
#
# Staged on the boot partition before travelling, then run once over SSH:
#   sudo bash /boot/firmware/firstboot.sh
#
# Safe to re-run: every step is idempotent.

set -euo pipefail

# ---------------------------------------------------------------- configuration
TAILSCALE_AUTHKEY="${TAILSCALE_AUTHKEY:-PASTE_AUTH_KEY_HERE}"
REPO_URL="${REPO_URL:-https://github.com/sequeirajonathan/Automated-Patient-Time-Log.git}"
DEPLOY_REF="${DEPLOY_REF:-release}"   # deploy branch, NOT main — see docs/OPERATIONS.md
SERVICE_USER="${SERVICE_USER:-apt}"
APP_DIR="/opt/aptlog"
NODE_MAJOR=22

# Pinned per REQUIREMENTS.md section 3.1. IMAGE_BUILD.md section 6 makes this
# script the way back from a lost reference Pi, so an unpinned install would
# rebuild with whatever shipped that day rather than what was validated, and the
# golden image stops being proven (ARCHITECTURE.md section 5.1).
#
# Appium 3 because appium-uiautomator2-driver declares peerDependencies
# { appium: ^3.0.0-rc.2 } -- Appium 2 is no longer available with a current driver.
# Changing either pin is a deliberate edit, revalidated per IMAGE_BUILD.md section 1.
APPIUM_VERSION="${APPIUM_VERSION:-3.6.0}"
UIA2_VERSION="${UIA2_VERSION:-8.4.0}"

log() { printf '\n=== %s\n' "$1"; }

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

# ---------------------------------------------------------------- base packages
log "installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    git curl ca-certificates jq sqlite3 \
    python3 python3-venv python3-pip \
    android-tools-adb usbutils uhubctl \
    log2ram || true   # log2ram may not be in the archive; handled below

# ------------------------------------------------------------------------ node
if ! command -v node >/dev/null || [[ "$(node -v | cut -c2- | cut -d. -f1)" -lt "$NODE_MAJOR" ]]; then
    log "installing node ${NODE_MAJOR}"
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
    apt-get install -y -qq nodejs
fi

# ---------------------------------------------------------------------- appium
if [[ "$(appium --version 2>/dev/null)" != "$APPIUM_VERSION" ]]; then
    log "installing appium ${APPIUM_VERSION}"
    npm install -g "appium@${APPIUM_VERSION}"
fi

if ! sudo -u "$SERVICE_USER" appium driver list --installed 2>&1 | grep -q "uiautomator2@${UIA2_VERSION}"; then
    log "installing uiautomator2 driver ${UIA2_VERSION}"
    sudo -u "$SERVICE_USER" appium driver install "uiautomator2@${UIA2_VERSION}" || \
        appium driver install "uiautomator2@${UIA2_VERSION}"
fi

# ------------------------------------------------------------------- tailscale
if ! command -v tailscale >/dev/null; then
    log "installing tailscale"
    curl -fsSL https://tailscale.com/install.sh | sh
fi

if ! tailscale status >/dev/null 2>&1; then
    log "joining tailnet"
    if [[ "$TAILSCALE_AUTHKEY" == "PASTE_AUTH_KEY_HERE" ]]; then
        echo "!! TAILSCALE_AUTHKEY not set — skipping. Run 'sudo tailscale up --ssh' manually." >&2
    else
        tailscale up --ssh --authkey="$TAILSCALE_AUTHKEY" --hostname=aptlog
    fi
fi

# ----------------------------------------------------------- sd card longevity
# The controller writes logs continuously; unmitigated this kills a card in months.
log "reducing SD writes"
if ! systemctl list-unit-files | grep -q log2ram; then
    echo "note: log2ram unavailable — journal is capped instead"
fi
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/aptlog.conf <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=64M
EOF

# ------------------------------------------------------------ hardware watchdog
# Recovers from a kernel-level hang, which no userspace restart policy can catch.
log "enabling hardware watchdog"
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/aptlog-watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=15
RebootWatchdogSec=2min
EOF

# ------------------------------------------------------------------ udev / adb
# Lets the service user talk to the phone without root.
log "configuring adb access"
usermod -aG plugdev "$SERVICE_USER"
cat > /etc/udev/rules.d/51-android.rules <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="*", MODE="0664", GROUP="plugdev"
EOF
udevadm control --reload-rules || true

# The uiautomator2 driver will not create a session without an SDK root. Debian's
# adb package ships one; verify it rather than discovering the gap during the
# REQ-1 probe, which reports it as a session failure a long way from this cause.
ANDROID_SDK_HOME=/usr/lib/android-sdk
if [[ -x "$ANDROID_SDK_HOME/platform-tools/adb" ]]; then
    cat > /etc/profile.d/android-sdk.sh <<EOF
export ANDROID_HOME=$ANDROID_SDK_HOME
export ANDROID_SDK_ROOT=$ANDROID_SDK_HOME
EOF
    chmod 0644 /etc/profile.d/android-sdk.sh
else
    echo "!! $ANDROID_SDK_HOME/platform-tools/adb missing — Appium cannot start a session" >&2
fi

# ------------------------------------------------------------------ config dir
# Root-owned and not group- or world-writable, on purpose. REQ-5.4.1 justifies the
# dev transport exception on the grounds that enabling it is "an act on the device,
# by someone with root, who meant it" — but that is only true if the service user
# cannot create /etc/aptlog/transport.conf itself. The agent runs as $SERVICE_USER;
# a writable config dir would let it switch off its own containment.
#
# Individual secrets inside stay 0600 owned by the service user (REQ-3) so the agent
# can still read them. 0755 on the directory permits that and forbids creation.
log "creating config directory"
install -d -o root -g root -m 0755 /etc/aptlog

# --------------------------------------------------------------------- the app
log "deploying application"
if [[ ! -d "$APP_DIR/.git" ]]; then
    git clone --branch "$DEPLOY_REF" "$REPO_URL" "$APP_DIR"
else
    git -C "$APP_DIR" fetch --all --quiet
    git -C "$APP_DIR" checkout "$DEPLOY_REF" --quiet
    git -C "$APP_DIR" pull --quiet
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
if [[ -f "$APP_DIR/pyproject.toml" ]]; then
    # [dev] is required, not optional: manager.sh gates every deploy on a pytest
    # run, so without pytest present every update "fails tests" and rolls back.
    sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR[dev]"
fi

# ------------------------------------------------------------------- services
log "installing services"
if [[ -d "$APP_DIR/deploy/systemd" ]]; then
    install -m 0644 "$APP_DIR"/deploy/systemd/*.service /etc/systemd/system/
    install -m 0644 "$APP_DIR"/deploy/systemd/*.timer   /etc/systemd/system/ 2>/dev/null || true
fi

systemctl daemon-reload
for unit in aptlog-appium aptlog-agent aptlog-ui; do
    systemctl enable --now "$unit" 2>/dev/null || echo "note: $unit not present yet"
done
systemctl enable --now aptlog-manager.timer aptlog-heartbeat.timer 2>/dev/null || true

# aptlog-firstrun is deliberately NOT enabled here — it is armed by
# sanitize-for-image.sh so it runs on the first boot of a *cloned* image, not on
# the reference machine that produced it.

# Publish the UI to the tailnet. Serve, not Funnel: this page shows visit state
# and must not reach the public internet.
if tailscale status >/dev/null 2>&1; then
    tailscale serve --bg 8080 2>/dev/null || \
        echo "note: tailscale serve failed — enable HTTPS certs on the tailnet"
fi

log "done — reboot now, then run the section 4 checks in docs/PI_SETUP.md"
