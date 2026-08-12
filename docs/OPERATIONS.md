# Operations

How the controller keeps itself alive, updates itself, and gets observed — given that
nobody technical is ever in the room with it.

Physical build and flashing are in [PI_SETUP.md](./PI_SETUP.md).

---

## 1. Self-healing

Design assumption: **the only on-site intervention available is a power cycle**, done by
someone non-technical. Everything short of that has to recover on its own.

### 1.1 The recovery ladder

The agent escalates through these on any failed check-off, taking the cheapest step that
could plausibly fix things and only climbing when it doesn't:

| # | Trigger | Action | Cost |
|---|---|---|---|
| 1 | Appium call errors | retry with backoff (3×) | seconds |
| 2 | Session wedged | tear down and recreate the Appium session | ~15s |
| 3 | `adb devices` not `device` | `adb reconnect` | ~5s |
| 4 | Still not `device` | `adb kill-server && adb start-server` | ~10s |
| 5 | Still not `device` | `uhubctl` power-cycle the phone's port | ~30s |
| 6 | Still not `device` | reboot the Pi | ~3min |
| 7 | Reboot didn't fix it | alert and stop trying | — |

Step 5 is why the powered hub needs per-port switching — it's the software equivalent of
unplugging and replugging the phone, and it clears most wedged-USB states without anyone
being present.

**If the hub doesn't support `uhubctl`**, step 5 degrades to a kernel-level bus reset,
which clears most wedged states but cannot force the phone itself through a power cycle:

```bash
echo '1-1' | tee /sys/bus/usb/drivers/usb/unbind   # device path from `lsusb -t`
sleep 3
echo '1-1' | tee /sys/bus/usb/drivers/usb/bind
```

Note that a hub with *physical* per-port buttons is not necessarily `uhubctl`-capable —
mechanical switches and the USB per-port power switching descriptor are different
features. Verify with `sudo uhubctl` and `lsusb -v | grep -i "per-port power"`.

Because the hub is self-powered, rebooting the Pi does **not** power-cycle the phone.
Where a true remote power cycle is needed and the hub can't provide it, a network smart
plug on the hub's power brick restores that capability.

Step 6 is deliberately near the bottom. Rebooting the Pi is cheap; rebooting the *phone*
is not, because Android requires the lock PIN to decrypt user storage after a cold boot
and that has to be scripted (see §1.4).

Step 7 matters as much as the rest. An automation that retries forever looks identical to
one that's working. Cap it, alert, and stop.

### 1.2 Process supervision

- Every unit is `Restart=always` with `StartLimitIntervalSec=0` — a normal systemd start
  limit would leave a unit permanently failed after a few crashes, with nobody to clear it.
- `aptlog-agent` uses `Type=notify` with `WatchdogSec=120`. The main loop pings systemd
  each pass, so a *hung* loop is restarted even though the process is alive. A plain
  restart policy would never catch that.
- `RuntimeWatchdogSec=15` arms the Pi's hardware watchdog, which recovers from a kernel
  hang that no userspace policy can reach.

### 1.3 What survives a power cut

Everything. All units are `enable`d, so a cold boot restores full operation with no login
and no interaction — which is exactly what makes "unplug it and plug it back in" a
sufficient recovery procedure for a 70-year-old.

The RTC battery keeps the clock across the outage. Without it a rebooted Pi can briefly
believe it's 1970 and fire the whole day's schedule at once.

`Persistent=true` on the manager timer means a missed update check runs on next boot.
**The check-off scheduler must not behave this way** — see REQ-6. A missed visit window
is not something to backfill on recovery; it's something to report.

### 1.4 Phone lock screen

After a phone reboot Android needs the PIN before user storage is readable. The agent
scripts it:

```
adb shell input keyevent 224      # wake
adb shell input keyevent 82       # dismiss keyguard
adb shell input text "$PIN"
adb shell input keyevent 66       # enter
```

The PIN lives in the same secret store as the app credential. It must be a **numeric PIN**
— pattern and biometric locks can't be driven this way.

---

## 2. Updates (the self-CI)

`scripts/manager.sh`, fired by `aptlog-manager.timer` every 10 minutes.

### 2.1 Why pull, not push

The Pi is behind the building's NAT on a residential Xfinity line. A pull loop needs no
inbound access, no port forwarding on your mother's router, and no webhook endpoint. It
also keeps working when Tailscale is down, which matters because Tailscale is how you'd
otherwise fix it.

### 2.2 Deploy from `release`, not `main`

The manager tracks a **`release` branch**. Pushing to `main` does not touch the Pi.

This is the difference between "I'm iterating from a cloud session" and "my sister's
visit records changed under her while she was on the fourth floor." Merge to `release`
when you mean it:

```bash
git checkout release && git merge --ff-only main && git push origin release
```

### 2.3 The cycle

```
fetch release
  └─ same SHA? exit
  └─ new SHA?
       record current as last-good
       run pytest against the new revision
         └─ fail → stay on current, alert, exit
       checkout, pip install, reinstall units, restart services
       health check (12 × 5s: units active + /healthz responding)
         └─ pass → record new SHA as last-good
         └─ fail → roll back, re-verify, alert
```

Tests run **before** the restart, so a broken revision never takes the agent down. The
rollback path is verified too — if both revisions come up unhealthy, that's the loudest
alert the system has.

### 2.4 Deliberate limits

- Migrations aren't handled. Any schema change needs a manual step until there's a real
  migration story.
- The manager runs as root (it writes unit files). It executes code from the repo, so
  **push access to `release` is root on the Pi**. Protect the branch accordingly.

---

## 3. Remote access

### 3.1 Tailscale

Chosen over an SSH tunnel or port forwarding because it traverses NAT with no router
configuration on a family member's internet connection, survives the WAN IP rotating,
and can be revoked from a web console without physical access to the device.

`tailscale up --ssh` handles SSH auth through the tailnet, so there are no keys to
distribute or rotate when a laptop changes.

The Pi joins with a pre-generated auth key baked into `firstboot.sh` — the person at the
building never logs into a Tailscale account.

### 3.2 Reaching it from a cloud session

Cloud sessions are ephemeral, so the container has to join the tailnet each time:

```bash
tailscaled --tun=userspace-networking --socks5-server=localhost:1055 &
tailscale up --authkey="$TS_EPHEMERAL_KEY" --hostname="cloud-$(date +%s)"
```

Use an **ephemeral, reusable, tagged** auth key so dead container nodes clean themselves
up instead of accumulating.

**This may not work from every environment.** Tailscale prefers UDP and falls back to
DERP relays over TCP/443; a restrictive egress policy can block both. Test it early
rather than designing around the assumption.

It doesn't need to work for deploys — that's §2, pull-based and independent. If the
tunnel is unavailable you lose interactive debugging, not the ability to ship.

### 3.3 Access tiers

| Who | Path | Sees |
|---|---|---|
| Developer | Tailscale SSH | everything |
| Sister (on shift) | UI over Tailscale on her personal phone | status, schedule, alerts |
| Mother | nothing digital | the printed card on the Pi |

---

## 4. The status UI

### 4.1 What it's for

The phone and Pi sit on the first floor; the work happens on all the others. The UI
answers, from wherever she is: *is this thing working, and did it do what it should have?*

### 4.2 Panels

1. **Health** — Pi up, phone attached, Appium up, last heartbeat. One green/red line each.
2. **Today** — each scheduled visit with status (pending / done / skipped / failed),
   **scheduled time and observed time side by side**, divergences highlighted.
3. **Gate** — the live presence signals: USB transport, gateway MAC match, wifi BSSID
   match, last location fix. Shows *why* a check-off was allowed or refused.
4. **Phone view** — most recent screenshot, so she can see what the phone sees without
   walking down. Subject to `FLAG_SECURE` (REQ-1) — if the app blocks screen capture this
   panel shows a placeholder rather than a black rectangle.
5. **Needs attention** — failures and skips, with an acknowledge action.
6. **Reconciliation** — end-of-shift view for checking against the handwritten log, with
   an explicit "amend in app" column (REQ-8).

### 4.3 Build

FastAPI + server-rendered HTML, polling every 5–10s. No build step, no framework, no
node_modules on the Pi. It's six panels of status — anything more is overhead on a device
whose main job is elsewhere.

Expose `/healthz` for the manager's health gate (§2.3).

### 4.4 Binding

`aptlog-ui.service` binds the **Tailscale interface only**, never `0.0.0.0`.

The building LAN is a shared residential network — other residents are on it. This page
shows visit state, so it doesn't belong on that broadcast domain. Tailscale gives
device-level access control instead, and it works from any floor over cell if the wifi
doesn't reach.

Show patient **initials or IDs by default**, full names only behind an explicit reveal.
Most glances at this page are "did the 2pm fire?", which needs no identifying detail.
