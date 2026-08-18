# Deploying

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
was already running, and the push says so rather than returning quietly.

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
