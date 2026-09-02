#!/bin/bash
# What a Claude Code on the web session needs before it can do anything
# useful here: the package and its test suite installed, and — when the
# environment carries a tailnet key — the Pi reachable, so the `aptlog`
# MCP tools (scripts/aptlog_mcp.py) can watch a deploy land instead of
# only shipping it. Web sessions only; a laptop already has all of this.
#
# Every step is idempotent and best-effort where the environment may not
# allow it: a sandbox that blocks Tailscale loses interactive debugging,
# not the session (docs/OPERATIONS.md §3.2). Nothing here ever prints the
# auth key, and the key only ever reaches `tailscale up` via the script
# that already handles it.
set -uo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
say() { printf '[session-start] %s\n' "$1"; }

# ------------------------------------------------------- 1. the package
# The project pins Python >= 3.13 (pyproject.toml); the image's default
# python3 is older, so the suite gets its own venv. Cached with the
# container, so a second run is a no-op.
PY="$(command -v python3.13 || command -v python3)"
if [ ! -x .venv/bin/python ]; then
  say "creating .venv with $PY"
  if command -v uv >/dev/null; then uv venv --python "$PY" .venv >/dev/null
  else "$PY" -m venv .venv; fi
fi
if command -v uv >/dev/null; then
  uv pip install --python .venv/bin/python -q -e '.[dev]' || say "dev install failed — tests may not run"
else
  .venv/bin/pip install -q -e '.[dev]' || say "dev install failed — tests may not run"
fi
# So `pytest` and `apt-log` resolve for the rest of the session.
echo "export PATH=\"$PWD/.venv/bin:\$PATH\"" >> "${CLAUDE_ENV_FILE:-/dev/null}"

# ------------------------------------------------- 2. the aptlog MCP tools
# .mcp.json runs scripts/aptlog_mcp.py under the SYSTEM python3, so its
# one dependency goes there. --ignore-installed steps around a Debian-owned
# PyJWT that pip otherwise refuses to replace (seen on the web image).
if ! python3 -c 'import mcp' 2>/dev/null; then
  say "installing mcp for the aptlog tools"
  python3 -m pip install -q --ignore-installed PyJWT 'mcp>=2.0' \
    || say "mcp install failed — the aptlog MCP server will not start"
fi

# ------------------------------------------------------------ 3. the tailnet
# Only when the environment carries a key (claude.ai/code → environment →
# variables: TS_AUTH_KEY, and PI_HOST=aptlog-fl). Runs in its own session
# so the daemon it starts outlives this hook. Failure is reported and is
# not fatal: deploys are pull-based and need no tunnel (DEPLOYING.md).
export PI_HOST="${PI_HOST:-aptlog-fl}"   # the name the aptlog tools use too
already_up() {
  command -v tailscale >/dev/null \
    && tailscale --socket=/tmp/tailscaled.sock status 2>/dev/null \
       | grep -q "[[:space:]]${PI_HOST}[[:space:]]"
}
# `tailscale ssh` shells out to the system ssh client, which the web image
# does not carry — the aptlog tools answered "no system 'ssh' command found"
# with the tunnel up. Quiet, and only when missing.
if ! command -v ssh >/dev/null; then
  say "installing the ssh client for tailscale ssh"
  (apt-get install -y -qq openssh-client >/dev/null 2>&1) \
    || say "could not install openssh-client — tailscale ssh will not work"
fi
if already_up; then
  # NOT re-joined. `tailscale up` with a fresh hostname on a node that is
  # already enrolled logs it out (seen live on a resumed session); a tunnel
  # that is up and can see the Pi is left exactly as it is.
  say "tailnet already up — ${PI_HOST} visible"
elif [ -n "${TS_AUTH_KEY:-}" ]; then
  say "joining the tailnet"
  if setsid -w bash scripts/cloud-session-up.sh; then
    say "tailnet up — the aptlog tools can reach the Pi"
  else
    say "tailnet join failed — see the output above; deploys still work"
  fi
else
  say "TS_AUTH_KEY not set — skipping the tailnet (deploys still work)"
fi
