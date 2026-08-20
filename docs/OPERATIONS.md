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

### 2.4 Reaching a person

`scripts/alert.sh` is the one outbound channel, carrying deploy failures, any unit that
dies via `OnFailure=`, and — the only thing that is *waiting on a human* — the login code
inMyTeam texts. It speaks ntfy or Pushover; both reach iOS, which matters because the
portal is used from an iPhone home-screen bookmark.

```
sudo scripts/add-alert-channel.sh      # once; writes /etc/aptlog/alert.env, mode 0600
```

Notifications carry a `--url`, so tapping one opens the portal at the thing it is about.
They never carry a patient, a visit, or a credential: this goes to a public relay and
lands on a lock screen, and neither is a place for any of that.

Web Push from the portal itself was added later and is now the *first* road for the
login code, with the relay behind it — see `apt_log/push.py`. It is worth the second
channel for one reason: a push notification comes from the portal, so tapping it opens
the portal at the code screen. A relay can only open a browser at a URL, which is the
wrong app on the one notice whose entire job is to be tapped.

Unconfigured, the script says so in the journal rather than failing quietly, because a
silent alerting path is indistinguishable from a healthy system:

```
journalctl -t aptlog-alert -n 10 --no-pager
```

### 2.5 Texting the code onward

The phone has a SIM and cell service, so a code that lands on it can be passed on to the
people who would otherwise be locked out. Off by default. Turn it on by setting one key
in `/etc/aptlog/secrets.env` — `sudo scripts/add-app-credentials.sh` prompts for it:

```
CODE_RECIPIENTS=Jonathan:9995551234,Sadia:9995555678
```

**Know what this is before setting it.** A sign-in code is the second factor on an
account whose records attest who delivered care to a patient. Everyone on that list can
sign in as the caregiver. That is the owner's call to make, not a default.

The numbers live there and *not* in this repository, because a git history is the one
place a phone number can never be taken back out of.

How it behaves:

- Sent from the phone's own SIM via `service call isms`, not through the messaging app.
  The moment a code needs forwarding is the moment inMyTeam is holding its code screen
  open, and walking away to Messages would abandon the sign-in this exists to help.
- **Once per code, ever.** Keyed on the message's own timestamp, written to
  `/var/lib/aptlog/code-forwarded.json`. A guard that lives in a process is a guard a
  deploy forgets — which is exactly how the "over 100 notifications" storm happened.
- Forwarded **before** the code is typed, because these people are the fallback for the
  typing going wrong.
- Also forwarded for codes this system never asked for: a poll behind the feed's tick
  notices a text arriving because *somebody else* tried to sign in.
- Carries no patient, no visit and no agency. It lands on lock screens.

`SEND_TXN` in `apt_log/sms.py` is the `ISms` transaction number for
`sendTextForSubscriber` — a fact about the *device*, not about Android, because the AIDL
renumbers between versions. **On a new phone, verify it** with the self-text probe
documented beside that constant before trusting it.

### 2.6 The visit schedule

`/etc/aptlog/schedule.json`, mode 0600, service-user readable. **It is not in this
repository and must not be put there.** It names who is cared for, at whose home, at what
hour — a git history is permanent, and this is the same argument `config.py` makes at the
top of itself about the site's own particulars. `sanitize-for-image.sh` removes it.

```json
{
  "zone": "America/New_York",
  "visits": [
    {"patient": "…", "app": "com.inmyteam.inmyteam",
     "days": ["mon","tue","wed","thu","fri"], "start": "05:00", "end": "06:00"},
    {"patient": "…", "app": "com.hhaexchange.uma", "agency": "…",
     "days": ["mon","wed","fri"], "start": "20:05", "end": "21:05",
     "part": 1, "of": 2}
  ]
}
```

- `app` is the package: `com.inmyteam.inmyteam`, `com.hhaexchange.uma` (Exchange+),
  `com.tellus.evv.v2` (Mobile Care).
- `days` accepts English or Spanish names.
- `start`/`end` are the **nominal** times — what the app displays. Nothing recomputes them.
- `part`/`of` split one stretch of care into separate entries.

**Times are wall clock, and that is deliberate.** 5:00am is 5:00am in March and in July,
which is what the agency means. Resolving those to real instants is `schedule.py`'s job,
and it handles both awkward days: a wall time inside the March gap is refused rather than
silently fired an hour out, and a repeated November hour takes the earlier of the two.

#### The travel buffer

A visit fires at its nominal start **unless a different patient's visit ends at exactly
that minute**, in which case five minutes are added so the caregiver can drive there. The
gap exists in no app and cannot be read from one.

It is not applied where a gap already exists — the rule is "no gap at all", not "always
five minutes" — and it is not applied between a patient's own split entries, because
nobody drives anywhere between those.

Whether it applies is a question about a **day**, not about a visit: the same block is
buffered on a weekday when somebody precedes it and fires on time on Saturday when
nobody does.

#### Checking it

The full-schedule view in the portal renders exactly what the engine computed, so the
fastest check on a freshly edited file is to open that page and read the week back.

### 2.7 Deliberate limits

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

The Pi joins with a pre-generated auth key — the person at the building never logs into a
Tailscale account. Where that key comes from depends on how the unit was built:

| Unit | Mechanism |
|---|---|
| Reference Pi (built by hand) | key placed in `scripts/firstboot.sh`, run once over SSH |
| Cloned from a golden image | `aptlog-firstrun.service` reads `tailscale-authkey.txt` from the FAT32 boot partition on first boot, then clears it |

The image path deliberately keeps the key **out** of the shipped file, so a live
credential never rides along on a cloud drive. See ARCHITECTURE.md §5.3.

**Disable node key expiry on the `aptlog` machine once it has joined** (PI_SETUP.md §4.1).
Node key expiry is distinct from auth key expiry and defaults to 180 days; left enabled,
remote access to a console-less device in someone else's home disappears half a year after
installation. The Pi's key and the cloud-session key in §3.2 are separate — the Pi's is
single-use and persistent, the cloud one reusable and tagged.

### 3.2 Reaching it from a cloud session

Cloud sessions are ephemeral, so the container has to join the tailnet each time:

```bash
tailscaled --tun=userspace-networking --socks5-server=localhost:1055 &
tailscale up --authkey="$TS_AUTH_KEY" --hostname="cloud-$(date +%s)"
```

Use a **reusable, tagged** auth key. Ephemeral looks like the obvious choice — dead
container nodes clean themselves up instead of accumulating — but it is metered in
minutes against a monthly allowance, and Tailscale reclassifies any node present for four
or more hours as an ordinary tagged device anyway. A provisioning or debugging session
runs for hours, so the ephemeral billing buys nothing on the sessions that matter and
charges for the container recycles in between.

The cost of not using it is that each container enrols as a new machine — the daemon
state lives in `/tmp` and dies with the container — so stale `tag:cloud` entries collect
in the admin console. Prune them when it gets untidy; nothing depends on them.

**This may not work from every environment.** Tailscale prefers UDP and falls back to
DERP relays over TCP/443; a restrictive egress policy can block both. Test it early
rather than designing around the assumption.

It doesn't need to work for deploys — that's §2, pull-based and independent. If the
tunnel is unavailable you lose interactive debugging, not the ability to ship.

### 3.3 Access tiers

| Who | Path | Sees |
|---|---|---|
| Developer | Tailscale SSH | everything |
| Sister (on shift) | `/app` over Tailscale on her personal phone | the phone, as components she can use |
| Whoever is helping her | `/console`, same tailnet | the same phone with nothing folded away, plus both machines' vital signs |
| Mother | nothing digital | the printed card on the Pi |

Both people reach the same server over the same tailnet; the difference is which page
they open and what it does with what it knows, not what they are permitted to see. There
is no login — the tailnet is the fence, and a password on top of it would be one more
thing to get past while standing in somebody's kitchen. Preferences (language, density,
a device's name) belong to a browser, so switching to English on one phone changes
nothing on the other.

---

## 4. The UI

### 4.1 Two pages, and why they are two

The phone and Pi sit on the first floor; the work happens on all the others, and
increasingly in another state. What began as a status dashboard is now two pages with
opposite jobs, because one page could not do both honestly.

| Route | Who it is for | What it does with what it knows |
|---|---|---|
| `/app` — **the phone** | the caregiver, mid-visit | **edits**: folds bands, sweeps furniture, refuses pictures of credential screens, renders the phone as components |
| `/console` — **the control centre** | whoever is helping her | **prints**: the whole screen document, every override, every metric |

`/` redirects to `/app`. Nobody opens this portal to read a status page.

The editing that makes `/app` usable is exactly what makes it impossible to teach
through: when a row does not appear there, you cannot tell whether the app did not show
it or the portal folded it away. `/console` is the other half of that answer, and it is
why it hides nothing.

What is *not* negotiable on either page: typed field contents never reach the screen
document at all (feed.write_screen), and a capture is refused while a password field has
focus (REQ-3). Those are enforced below anything a page can undo — a rule about
credentials, not a display preference.

### 4.2 Panels

**`/app`** — the launcher, the live wireframe of the current screen, the relay (the four
things only she can answer), the signature pad, and the phone's own navigation.

**`/console`**

1. **Live** — the phone's own screenshot beside a live iframe of `/app`: what the app is
   doing, and what she is seeing of it, at the same moment.
2. **The screen, unabridged** — every node the device reported, tappable and not, in the
   order they sit on the glass, with the resource ids that name them.
3. **Density** — a clamped slider (never below the value that has actually crashed the
   phone) writing *overrides* which are kept apart from the tuned defaults, per page, per
   app, or for everything else. Clearing one uncovers the default; the default is never
   written over.
4. **Who is on** — every browser that has opened the portal lately, nameable, with its
   language and the page it is on.
5. **The phone** — battery, temperature, the density actually in force, the cable.
6. **The controller** — memory, disk, load, temperature, uptime, tailnet address.
7. **Services** — the three units the deploy gate restarts, plus `tailscaled`.
8. **Location check** — the live presence signals: whether a visit could have been
   recorded at all.
9. **Shortcuts** — the macros, with the line saying they never clock a visit in or out.

Panels that were removed rather than rewritten, and why: *Scheduler* and *Last check-in*
(a unit and a file reporting "is the controller alive" less directly than the three
signals that already do), *Today* and *Reconciliation* (an end-of-shift check against a
handwritten log that is not kept — the agency's record is the record), *Needs attention*
(empty every day), and *What the controller is doing* (the agent's idea of which screen
it was on, which drifted from what the phone was showing).

### 4.3 Build

FastAPI + server-rendered HTML. No build step, no framework, no node_modules on the Pi.
`/app` rides a websocket; `/console` is forms and redirects with three small scripts that
nothing depends on — every control on it works with JavaScript off.

Preferences live in `/var/lib/aptlog/prefs.json`, per device, written the same atomic way
as the other state files. There is still no database: the agency's record is the database,
and a preference is worth keeping and worth nothing if it is lost.

Expose `/healthz` for the manager's health gate (§2.3).

The suite runs on the Pi as part of that gate, against the live `/var/lib/aptlog`. Tests
must never write there — `tests/conftest.py` redirects the preferences file to a temporary
path for every test, so a deploy cannot edit somebody's settings as a side effect of
checking that it is safe to deploy.

### 4.4 Binding

`aptlog-ui.service` binds **loopback only** — `127.0.0.1:8080`, never `0.0.0.0` and never
a Tailscale address directly.

`tailscale serve --bg 8080` proxies it onto the tailnet at
`https://aptlog.<tailnet>.ts.net`. Binding loopback and letting `serve` publish avoids
hardcoding a CGNAT address that can change, and gives real TLS without a certificate to
manage.

**`serve`, not `funnel`.** Funnel publishes to the open internet; this page shows visit
state. The caregiver reaches it through Tailscale **node sharing** — she is invited to the
`aptlog` machine alone, never joins the tailnet, and sees nothing else on it.

The building LAN is a shared residential network with other residents on it, which is why
the page never binds an interface reachable from it.

Show patient **initials or IDs by default**, full names only behind an explicit reveal.
Most glances at this page are "did the 2pm fire?", which needs no identifying detail.
