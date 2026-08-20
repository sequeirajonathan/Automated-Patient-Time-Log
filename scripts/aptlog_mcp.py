"""The controller's own tools, for whoever is working on it from elsewhere.

Everything here is something that was being done by hand, over and over, in
shell: deploy and read back the verdict, ask what the phone is showing, take
its picture, read a service's log. Each one is three or four moving parts —
the tailnet, ssh, a python one-liner on the far side, a file copied back —
and getting any of them slightly wrong produces an answer that looks fine
and is not. A tool that always does it the same way is worth more than the
shell it replaces.

The one piece of state that made this worth building: the tailnet daemon in
an ephemeral container dies without warning, and every command through it
then fails in its own way. It is started here, once, before anything else
runs, rather than remembered as a step.

Run over stdio, alongside a checkout — see .mcp.json.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import time
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from mcp.types import ToolAnnotations
from pydantic import Field

server = MCPServer(
    name="aptlog_mcp",
    instructions=(
        "Tools for the Automated Patient Time Log controller: a Raspberry Pi "
        "on a tailnet driving an Android phone for a caregiver. Deploys are "
        "gated by the full test suite and roll back on failure, so a refused "
        "deploy is a normal answer, not an error."
    ),
)

HOST = os.environ.get("APTLOG_HOST", "apt@aptlog-fl")
TAILSCALE_SOCKET = os.environ.get("APTLOG_TS_SOCKET", "/tmp/tailscaled.sock")
DEPLOY_REF = os.environ.get("APTLOG_DEPLOY_REF", "release")
# The working tree the services actually run from — the only place that can
# answer "what is running", as against what some branch points at.
DEPLOY_TREE = os.environ.get("APTLOG_DEPLOY_TREE", "/opt/aptlog")
REPO = os.environ.get("APTLOG_REPO", os.getcwd())

# Long enough for the deploy gate: the test suite alone runs about fifty
# seconds on the Pi, and the health check waits on services binding.
DEPLOY_TIMEOUT = 600.0
QUICK_TIMEOUT = 120.0

# HOW LONG THIS TOOL MAY WAIT BEFORE ANSWERING.
#
# Not how long the deploy takes — how long the CALLER is willing to sit there.
# The two were the same number for a long time and it made every deploy look
# like a failure: the gate needs roughly ninety seconds end to end, the client
# calling this tool gives up at sixty, so the call was killed every single
# time while the deploy went on to succeed on the Pi without anybody watching.
# "The deployment keeps failing" was the reasonable conclusion and it was
# never true.
#
# So the tool now answers inside the window with whatever is actually true at
# that moment, and says plainly when the gate is still running. A deploy in
# progress is a normal answer, like a refused one.
DEPLOY_ANSWER_BY = 45.0
DEPLOY_POLL = 3.0

# The same rule for macros, arrived at the same way. `update_app` downloads an
# APK: it ran for minutes and the caller was cut off at sixty seconds while it
# went on to finish perfectly. That one is not even a mismatch to be tuned
# away — no caller's window covers an app install, and the ones for the other
# three apps will be no shorter.
#
# Note this is a REPORTING window and nothing else. Whatever is in flight
# keeps running on the Pi; the portal's own spinner waits eight minutes, so
# the person driving the phone never sees any of this.
MACRO_ANSWER_BY = 45.0
MACRO_POLL = 2.0


class TailnetDown(RuntimeError):
    """The tailnet could not be brought up, so the Pi cannot be reached."""


def _tailscale() -> str:
    found = shutil.which("tailscale")
    if not found:
        raise TailnetDown("the tailscale client is not installed here")
    return found


def _tailnet_up() -> bool:
    try:
        done = subprocess.run(
            [_tailscale(), f"--socket={TAILSCALE_SOCKET}", "status"],
            capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and HOST.split("@")[-1].encode() in done.stdout


def ensure_tailnet() -> None:
    """Bring the tailnet up if it is down. Idempotent, and the reason this
    module exists: the daemon dies on its own in an ephemeral container, and
    every tool below fails differently when it does."""
    if _tailnet_up():
        return
    daemon = shutil.which("tailscaled") or "/usr/sbin/tailscaled"
    subprocess.Popen(
        ["setsid", daemon, "--tun=userspace-networking",
         f"--socket={TAILSCALE_SOCKET}", "--statedir=/tmp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    for _ in range(15):
        time.sleep(2)
        if _tailnet_up():
            return
    raise TailnetDown(
        f"{HOST} is not reachable on the tailnet. The daemon was started and "
        "did not come up within thirty seconds; check whether the auth key "
        "has expired.")


def ssh(command: str, timeout: float = QUICK_TIMEOUT) -> subprocess.CompletedProcess:
    """One command on the Pi, over the tailnet."""
    ensure_tailnet()
    return subprocess.run(
        [_tailscale(), f"--socket={TAILSCALE_SOCKET}", "ssh", HOST, command],
        capture_output=True, timeout=timeout)


def _text(done: subprocess.CompletedProcess) -> str:
    out = done.stdout.decode("utf-8", "replace").strip()
    err = done.stderr.decode("utf-8", "replace").strip()
    if err and not out:
        return err
    return f"{out}\n{err}".strip() if err else out


# --------------------------------------------------------------------- deploy
@server.tool(
    name="aptlog_deploy",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True,
        openWorldHint=False),
)
def aptlog_deploy(
    ref: Annotated[str, Field(
        description="What to deploy — any revision this checkout can name "
                    "(e.g. 'HEAD', a branch, a sha).")] = "HEAD",
) -> str:
    """Deploy a revision to the Pi and report what is true within the minute.

    Pushes to the controller and runs its gate: install dependencies, run the
    whole test suite against the revision, restart the services, check health,
    and roll back if it does not come up. About a minute and a half end to
    end.

    THREE answers, all of them normal. DEPLOYED means the working tree the
    services run from is on the revision asked for. REFUSED means the gate
    rejected it and the machine is untouched. GATE RUNNING means neither has
    happened yet — the gate is still working, and `aptlog_status` will show it
    land. That third answer is why this tool no longer blocks: the gate
    outlasts what a caller will wait, so waiting for it reported a failure on
    every successful deploy for months.

    A refused deploy is a normal answer. Git cannot fail a push from the hook
    that runs the gate, so the answer here is read from the machine afterwards
    rather than from the push's exit status.

    Read from the MACHINE, specifically — the revision checked out in the
    working tree the services run from. Reading the deploy branch instead was
    wrong in a way that took a while to see: a push with nothing to push is a
    no-op, the hook never runs, and the branch still points at the revision an
    earlier interrupted push left it on. That reported DEPLOYED over a machine
    running something else entirely, which is the exact failure this tool
    exists to prevent.
    """
    ensure_tailnet()
    env = dict(os.environ)
    env["GIT_SSH_COMMAND"] = f"{_tailscale()} --socket={TAILSCALE_SOCKET} ssh"
    try:
        wanted = subprocess.run(
            ["git", "-C", REPO, "rev-parse", ref],
            capture_output=True, timeout=30, check=True
        ).stdout.decode().strip()
    except subprocess.CalledProcessError:
        return f"'{ref}' is not a revision in {REPO}."

    # The push is left to run on its own rather than waited on, because the
    # hook on the other end holds it open for the whole gate — tests, restart,
    # health check — and that is longer than the caller will wait. It is NOT
    # abandoned: it keeps running, the gate still gates, and the loop below
    # watches the machine for its result. Killing this tool's call does not
    # stop the deploy either, which is precisely why the old version looked
    # like it failed while succeeding.
    push = subprocess.Popen(
        ["git", "-C", REPO, "push", "pi", f"{ref}:{DEPLOY_REF}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

    deadline = time.monotonic() + DEPLOY_ANSWER_BY
    running = ""
    while True:
        # What is actually checked out where the services run. This is the
        # answer — a branch can point anywhere.
        running = _text(ssh(f"git -C {DEPLOY_TREE} rev-parse HEAD")).strip()
        if running == wanted or push.poll() is not None:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(DEPLOY_POLL)

    finished = push.poll() is not None
    log = ""
    if finished and push.stdout is not None:
        log = push.stdout.read().decode("utf-8", "replace").strip()

    if running == wanted:
        return f"DEPLOYED {wanted[:7]} — live and healthy.\n\n{log}".strip()

    if not finished:
        # The honest middle answer, and the one that was missing. The gate is
        # still working; nothing has gone wrong; ask again in a moment.
        return (
            f"GATE RUNNING for {wanted[:7]}. The controller is still on "
            f"{running[:7] or 'its previous revision'} — tests, restart and "
            f"health check take about a minute and a half in total. Nothing "
            f"has failed; call aptlog_status to see when it lands.")

    landed = subprocess.run(
        ["git", "-C", REPO, "ls-remote", "pi", DEPLOY_REF],
        capture_output=True, timeout=60, env=env
    ).stdout.decode().split("\t")[0].strip()

    if landed != wanted:
        return (
            f"REFUSED. {wanted[:7]} did not deploy; the controller is still "
            f"running {running[:7] or 'its previous revision'} and is "
            f"unchanged.\n\n{log}")
    # The branch says one thing and the machine another: the push was a no-op,
    # so the hook that runs the gate never fired. Naming the fix here rather
    # than leaving a puzzle — the reconciliation path is one command.
    return (
        f"NOT DEPLOYED. {DEPLOY_REF} already pointed at {wanted[:7]}, so the "
        f"push had nothing to send and the gate never ran; the machine is "
        f"still on {running[:7] or 'an unknown revision'}.\n"
        f"Reconcile it with: ssh {HOST} sudo systemctl start "
        f"aptlog-manager.service\n\n{log}")


# --------------------------------------------------------------------- status
@server.tool(
    name="aptlog_status",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def aptlog_status() -> str:
    """What the controller and the phone are doing right now.

    The deployed revision, whether the services are up and healthy, whether
    the phone is attached and authorised, which app is in front, how old the
    published screen is, and any macro still in flight — the questions that
    decide whether anything else is worth trying.

    The macro line appears only when one is running or has just finished, and
    it is what makes "still running" a checkable answer rather than a shrug.
    """
    probe = r"""
echo "revision: $(git -C /opt/aptlog rev-parse --short HEAD 2>/dev/null)"
echo "services: $(systemctl is-active aptlog-ui aptlog-feed aptlog-appium | tr '\n' ' ')"
echo "health:   $(curl -sf --max-time 5 http://127.0.0.1:8080/healthz >/dev/null && echo ok || echo UNREACHABLE)"
echo "adb:      $(adb get-state 2>&1 | head -1)"
# The focused window, or — when that window is a popup rather than an activity
# — the app that OWNS it. A dropdown reads back as the bare word "Window", and
# this tool reporting that while the portal correctly reported the app cost a
# round of confusion during the very investigation that fixed it. Same rule as
# feed._is_a_window_title: a real focus carries a slash and a dot.
DUMP=$(adb shell dumpsys window 2>/dev/null)
FOCUS=$(printf '%s' "$DUMP" | grep -m1 -oE 'mCurrentFocus=Window\{[^}]*' | sed 's/.* //')
case "$FOCUS" in
  */*|*.*) ;;
  ?*) OWNER=$(printf '%s' "$DUMP" | grep -m1 -oE 'mFocusedApp=[^}]*' \
              | grep -oE '[A-Za-z0-9_.]+/[A-Za-z0-9_.]+' | head -1)
      [ -n "$OWNER" ] && FOCUS="$OWNER (under a popup)" ;;
esac
echo "focus:    $FOCUS"
python3 - <<'PY'
import datetime, json
try:
    d = json.load(open("/var/lib/aptlog/screen.json"))
except Exception as exc:
    print("screen:   unreadable (%s)" % exc); raise SystemExit
age = "?"
try:
    age = "%.0fs" % (datetime.datetime.now() -
                     datetime.datetime.fromisoformat(d["at"])).total_seconds()
except Exception:
    pass
print("screen:   %s / %s (%s ago), blocked=%r, %d tappable" % (
    d.get("app") or "-", d.get("activity") or "-", age,
    d.get("blocked") or "", len(d.get("elements") or [])))
PY
# What the controller is DOING, which is the other half of what it is doing.
# A macro can hold the phone for minutes — an app install does — and without
# this line the only reading available was "the screen has not changed",
# which looks identical to a machine that has stopped.
python3 - <<'PY'
import datetime, json
try:
    s = json.load(open("/var/lib/aptlog/macro-status.json"))
except Exception:
    raise SystemExit
state = s.get("state") or "idle"
if state == "idle":
    raise SystemExit
age = ""
try:
    age = " for %.0fs" % (datetime.datetime.now() -
                          datetime.datetime.fromisoformat(s["at"])).total_seconds()
except Exception:
    pass
print("macro:    %s %s%s%s" % (
    s.get("name") or "?", state, age,
    (" — " + s["error"]) if s.get("error") else
    ((" (%s)" % s["step"]) if s.get("step") else "")))
PY
"""
    return _text(ssh(probe)) or "no answer from the controller"


# --------------------------------------------------------------------- screen
@server.tool(
    name="aptlog_screen",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def aptlog_screen(
    detail: Annotated[Literal["summary", "full"], Field(
        description="'summary' lists the controls and the words on screen; "
                    "'full' adds every bound, for working on the reflow.")]
    = "summary",
) -> str:
    """What the phone's current screen contains, as the portal reads it.

    This is the accessibility tree as published — the same document the
    front end renders and taps against — so it is the right thing to look at
    when a screen renders wrongly. Text is included; treat it as the
    patient-facing content it is.
    """
    full = "1" if detail == "full" else ""
    probe = f"""python3 - <<'PY'
import json
FULL = bool({full!r})
d = json.load(open("/var/lib/aptlog/screen.json"))
print("app:", d.get("app"), "| activity:", d.get("activity"),
      "| screen:", d.get("screen"), "| blocked:", repr(d.get("blocked")))
print("landscape:", d.get("landscape"), "| canvas:", d.get("canvas"),
      "| scrollable:", d.get("scrollable"), "| whole page:", d.get("full"))
print("--- tappable")
for e in d.get("elements") or []:
    line = "  %-34s %-14s %r" % (e.get("rid") or "-", e.get("cls") or "",
                                 (e.get("txt") or "")[:44])
    if FULL:
        line += " %s enabled=%s" % (e.get("b"), e.get("enabled"))
    print(line)
print("--- words")
for s in d.get("statics") or []:
    line = "  %r" % ((s.get("txt") or "")[:70],)
    if FULL:
        line += " %s" % (s.get("b"),)
    print(line)
PY
"""
    return _text(ssh(probe)) or "no screen has been published"


@server.tool(
    name="aptlog_screenshot",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def aptlog_screenshot() -> Image:
    """A picture of the phone's screen, right now.

    Straight from the device rather than from the published frame, so it is
    current even when the feed is behind — which is exactly when a picture is
    worth having.
    """
    ensure_tailnet()
    done = subprocess.run(
        [_tailscale(), f"--socket={TAILSCALE_SOCKET}", "ssh", HOST,
         "adb exec-out screencap -p | base64 -w0"],
        capture_output=True, timeout=QUICK_TIMEOUT)
    raw = done.stdout.decode("ascii", "ignore").strip()
    if not raw:
        raise RuntimeError(
            "no picture: " + (_text(done) or "the phone did not answer adb"))
    return Image(data=base64.b64decode(raw), format="png")


# ----------------------------------------------------------------------- logs
@server.tool(
    name="aptlog_logs",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def aptlog_logs(
    unit: Annotated[
        Literal["aptlog-manager", "aptlog-feed", "aptlog-ui", "aptlog-appium"],
        Field(description="Which service. The manager holds deploy history; "
                          "the feed holds everything about the phone.")],
    lines: Annotated[int, Field(
        description="How many recent lines.", ge=1, le=400)] = 40,
    grep: Annotated[str, Field(
        description="Optional case-insensitive filter, applied before the "
                    "line limit. The feed logs a line per captured frame, so "
                    "filtering is usually what makes its log readable.")] = "",
) -> str:
    """Recent log lines from one of the controller's services."""
    safe = grep.replace("'", "")
    pipe = f" | grep -i -- '{safe}'" if safe else ""
    return _text(ssh(
        f"journalctl -u {unit} --no-pager -n 4000 -o short-iso 2>/dev/null"
        f"{pipe} | tail -n {int(lines)}"
    )) or f"nothing in {unit}'s log"


# ---------------------------------------------------------------------- macro
@server.tool(
    name="aptlog_run_macro",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False,
        openWorldHint=False),
)
def aptlog_run_macro(
    name: Annotated[str, Field(
        description="The macro's registered name, e.g. 'hhax_legacy_login', "
                    "'hhax_uma_login', 'mobile_caregiver_pin', 'read_page'.")],
) -> str:
    """Run one of the controller's macros and report how it ended — or that
    it is still going.

    Macros fill and navigate; none of them commits a visit — no clock-in,
    no clock-out, no signature is saved by a macro. Sign-in macros refuse
    unless the app is actually showing a credential screen.

    THREE answers, all normal: done, failed, and STILL RUNNING. The last one
    is not a timeout and not a failure — `update_app` downloads an APK and
    takes minutes, and no caller's window covers that. The macro carries on
    regardless of what this call returns; `aptlog_status` names what is in
    flight, and the portal's own spinner is what the person driving the phone
    actually sees.
    """
    safe = "".join(c for c in name if c.isalnum() or c == "_")
    if not safe:
        return "a macro name is letters, digits and underscores"
    rounds = max(1, int(MACRO_ANSWER_BY // MACRO_POLL))
    probe = f"""
sudo -u apt /opt/aptlog/.venv/bin/python - <<'PY'
import sys, time
sys.path.insert(0, "/opt/aptlog/src")
from apt_log import macros
try:
    rid = macros.request({safe!r})
except KeyError:
    print("no such macro: {safe}")
    print("known:", ", ".join(sorted(macros.MACROS)))
    raise SystemExit
print("requested", rid)
for _ in range({rounds}):
    time.sleep({MACRO_POLL})
    s = macros.read_status()
    if s.state in ("done", "failed"):
        print("%s | %s | %s" % (s.state, s.step, s.error or ""))
        break
else:
    # Reported as a state, not as an apology. The step is the useful half:
    # "installing" and "signing in" fail in completely different ways.
    s = macros.read_status()
    print("STILL RUNNING | %s | nothing has failed; it keeps going on the "
          "Pi. Check aptlog_status." % (s.step or "no step reported",))
PY
"""
    return _text(ssh(probe, timeout=DEPLOY_TIMEOUT)) or "no answer"


# ----------------------------------------------------------------------- tree
@server.tool(
    name="aptlog_tree",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def aptlog_tree(
    grep: Annotated[str, Field(
        description="Only nodes containing this, case-insensitively. Empty "
                    "returns the whole tree.")] = "",
    lines: Annotated[int, Field(
        description="Most nodes to return (default 120).")] = 120,
) -> str:
    """The RAW accessibility tree, read the way the feed reads it.

    `aptlog_screen` publishes the reflow's view — clickables, statics, the
    words. This is the document underneath it, for the times that is not
    enough: a resource-id on a node nobody taps, an attribute the reflow
    drops, a screen being mapped on a phone for the first time.

    READ FROM DISK, NOT FROM THE PHONE, and that is the whole point. There
    are two ways to ask a device for its tree and both fight the feed.
    `adb shell uiautomator dump` spawns a fresh instrumentation and competes
    for the same service — doing it during setup work wedged Appium hard
    enough to need a restart. Calling `read_hierarchy` from another process
    is worse: it opens a SECOND Appium session, and UiAutomator2 allows
    exactly one.

    So the feed writes the tree down every time it reads one, and this reads
    the file. Nothing is asked of the phone, nothing can contend, and the
    answer is available even while Appium is restarting. Its age is printed
    because a file is only as current as the last read.

    TYPED TEXT IS BLANKED, and not as an afterthought. The published document
    withholds the contents of editable fields on purpose — that is where a
    passcode and a texted code live while she is typing them — and a raw tree
    would hand back precisely what the rest of the system refuses to carry.
    The node, its id and its bounds all survive; only what was typed into it
    does not.
    """
    want = "".join(c for c in grep if c.isalnum() or c in " ._-:/")[:60]
    probe = """
python3 - <<'INNER'
import os, time
PATH = "/var/lib/aptlog/hierarchy.xml"
try:
    xml = open(PATH, encoding="utf-8").read()
    age = time.time() - os.path.getmtime(PATH)
except OSError as exc:
    print("no tree on disk yet (%s)" % exc)
    raise SystemExit

nodes = []
for n in xml.split("<"):
    n = n.strip()
    if n and not n.startswith("?") and not n.startswith("/"):
        nodes.append("<" + n)
want = WANT.lower()
shown = [n for n in nodes if want in n.lower()] if want else nodes
print("%d nodes%s, tree is %.0fs old"
      % (len(shown), (" matching %r" % want) if want else "", age))
for n in shown[:LIMIT]:
    print(n[:400])
if len(shown) > LIMIT:
    print("... %d more" % (len(shown) - LIMIT))
INNER
"""
    probe = probe.replace("WANT", repr(want)).replace("LIMIT", str(int(lines)))
    return _text(ssh(probe, timeout=DEPLOY_TIMEOUT)) or "no answer"


if __name__ == "__main__":
    server.run("stdio")
