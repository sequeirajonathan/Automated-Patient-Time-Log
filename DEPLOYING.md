# Deploying

## Branches

| Branch | What it is |
|---|---|
| `main` | The trunk. Everything arrives by pull request. |
| `claude/*` | One branch per piece of work, one pull request each. **Deleted once merged.** |
| `release` | A *pointer*, not a line of development: what the Pi runs. |

`release` being ahead of `main` is normal — work is deployed to the Pi
before its pull request lands, because the Pi is where it gets tested. It
is never merged *from*; it is moved to whatever revision should be live.

A merged `claude/*` branch is history twice over, and a pile of them is
what makes it hard to see which branch is the live one. After anything
merges:

```
scripts/branch-audit.sh            # what is orphaned, what is still live
scripts/branch-audit.sh --prune    # delete the orphans
```

The audit only ever offers to delete a branch **fully contained in main**,
where deleting loses nothing. Anything unmerged it reports and leaves
alone; unmerged work is not a script's to throw away.

Worth setting once, so most of this never accumulates: GitHub's
**Settings → General → "Automatically delete head branches"** removes a
branch the moment its pull request merges.

## Deploying

Two ways in, one gate. Whichever route a revision arrives by, the same
`scripts/manager.sh` decides whether it may run: install the target's
dependencies, run the whole test suite against it, restart the services,
check the health endpoint, and roll back to the previous revision if the
new one does not come up healthy.

A deploy takes about a minute — roughly fifty seconds of that is the test
suite, and it is the part worth having.

## Push to deploy (immediate)

```
git remote add pi apt@aptlog-fl:/opt/aptlog.git   # once
git push pi HEAD:release
```

The gate runs the instant the push lands. Git streams the hook's output
back, so the log prints in the terminal that ran the push and the push does
not return until the deploy has finished:

```
[deploy] 56ad7f0e received — running the gate
[manager] update available: f2d1f958 -> 56ad7f0e
[manager] running tests against 56ad7f0e
[manager] deploying 56ad7f0e
[manager] healthy on 56ad7f0e
[deploy] live: 56ad7f0
```

A push whose tests fail is refused, the machine stays on the revision it
was already running, and the push says so:

```
[deploy] REFUSED — the gate rejected 77702c86; the machine is
[deploy] still on 601a682
[deploy] release wound back to 601a682 — nothing here is live
```

One caveat worth knowing, because it cannot be fixed from this side: **git
ignores what a post-receive hook exits with.** The refs are written before
the hook runs, so `git push` reports success even for a revision the gate
refused — the loud REFUSED above is in the output, not in the exit status.
So the branch is wound back instead, which means the state can be trusted
even though the status cannot:

```
git ls-remote pi release      # what actually deployed
```

If that matches what you pushed, it is live. If it is the older revision,
the push was refused. (A scripted deploy should check that rather than
`$?`.)

Requires access to the tailnet — the Pi has no address on the open
internet, which is also why this is a push rather than a GitHub webhook: a
webhook would mean publishing a port on the phone's own controller to
everyone, and defending it, for a fleet of one machine the deployer can
already reach.

## Push to GitHub (within ten minutes)

```
git push origin HEAD:release
```

`aptlog-manager.timer` fetches every ten minutes and runs the same gate.
This is what catches a revision pushed from a machine that is not on the
tailnet, and what heals the Pi if a push-deploy is interrupted halfway.

To run that reconciliation immediately instead of waiting for the timer:

```
sudo systemctl start aptlog-manager.service    # blocks until the deploy ends
```

It is a `oneshot` unit, so the command returns when the deploy is over and
its exit status is the deploy's. There is nothing to poll.

## When a deploy fails

The manager alerts and rolls back on its own. To see what happened:

```
journalctl -u aptlog-manager -n 40 --no-pager
```

The messages distinguish the three failures that look alike from a
distance and are not alike at all: *the tests failed* (the revision is
bad), *dependencies would not install* (the revision is untestable), and
*pytest missing after install* (the environment judging the revision is
broken, not the revision). Only the first means the code is at fault.

## From a Claude Code session

The same operations are exposed as an MCP server (`scripts/aptlog_mcp.py`,
registered by `.mcp.json`), so a session working on this repo can deploy and
inspect the controller without reassembling the tailnet-plus-ssh incantation
each time:

| Tool | What it does |
|---|---|
| `aptlog_deploy` | Push a revision, run the gate, report DEPLOYED or REFUSED |
| `aptlog_status` | Revision, services, health, adb, focused app, screen age |
| `aptlog_screen` | The published accessibility document, as the portal reads it |
| `aptlog_screenshot` | A picture of the phone, straight from the device |
| `aptlog_logs` | Recent lines from a service, filterable |
| `aptlog_run_macro` | Run a macro and report how it ended |

`aptlog_deploy` never trusts the push's exit status — see the caveat above. It
reads the revision **checked out on the machine**, which is the only thing that
answers "what is running".

Reading the branch instead was wrong in a way worth writing down, because it
looks right until it isn't. A push with nothing to push is a no-op: the hook
never runs, and `release` still points wherever an earlier interrupted push
left it. That combination reports a successful deploy over a machine running
something else. It also has a second cause — **the ten-minute timer**. The
timer reconciles the machine to `origin/release`, so a revision push-deployed
straight to the Pi is rolled *back* within ten minutes unless `origin/release`
moved too. Both paths, both pointers:

```
git push pi HEAD:release        # deploy now
git push origin HEAD:release    # and stop the timer undoing it
```

When the two disagree, the tool says NOT DEPLOYED and names the fix, which is
the reconciliation path:

```
ssh apt@aptlog-fl sudo systemctl start aptlog-manager.service
```

The tools bring the tailnet daemon up themselves if it has died, which in an
ephemeral container it does, and which is most of what made these operations
tedious by hand.

Install the dependency where you run the session — never on the Pi, whose
deploy gate must not grow new ways to fail:

```
pip install -e ".[tools]"
```
