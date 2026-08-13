# Architecture

Three locations, one system.

| Where | What | Role |
|---|---|---|
| **Texas** | Windows laptop + Raspberry Pi 5 + test Android phone | development, image build |
| **Cloud** | Claude session | development after the unit ships |
| **Florida** | Raspberry Pi 5 + the working phone, in a resident's room | production |

The Florida unit is built by cloning an image produced in Texas. Nobody technical is ever
in the room with it.

---

## 1. Runtime topology

```
                    ┌───────────────────────────────────────┐
                    │  Raspberry Pi 5  (Florida, ethernet)   │
                    │                                       │
   Tailscale ───────┤  aptlog-agent    scheduler, gate,     │
   (SSH + Serve)    │                  check-off, signature │
                    │  aptlog-appium   Appium server        │
                    │  aptlog-ui       FastAPI, EN/ES       │
                    │  aptlog-manager  self-update timer    │
                    │  aptlog-heartbeat  dead-man's switch  │
                    │                                       │
                    │  /var/lib/aptlog/   SQLite + audit    │
                    └──────────────┬────────────────────────┘
                                   │ USB (data)
                    ┌──────────────▼──────────┐
                    │  powered hub (own PSU)   │
                    └──────────────┬──────────┘
                                   │ USB-A → USB-C
                            ┌──────▼──────┐
                            │  Android    │  the app, real GPS
                            └─────────────┘
```

Everything runs **native under systemd**. See §6 for why not Docker.

---

## 2. How the front end is reached

No domain, no port forwarding, no DNS.

```bash
tailscale serve --bg 8080
# → https://aptlog.<tailnet>.ts.net
```

Stable, bookmarkable, real TLS certificate, issued by Tailscale.

### 2.1 Serve, not Funnel

`tailscale funnel` publishes to the open internet. This page shows patient visit state, so
it stays on `serve`, reachable only by devices on the tailnet.

### 2.2 Giving the caregiver access

Use **Tailscale node sharing**, not tailnet membership: share the `aptlog` machine with
her Tailscale account. She installs the app, accepts one invite, bookmarks the URL. She
never joins the tailnet and can see nothing else on it. Revocable from the admin console
without touching the device.

Her phone reaches it over building wifi or cell, from any floor.

### 2.3 Access tiers

| Who | Path | Sees |
|---|---|---|
| Developer | Tailscale SSH | everything |
| Caregiver | shared node → `https://aptlog.<tailnet>.ts.net` | status, schedule, signature prompts, alerts |
| Resident (70s) | nothing digital | the printed card taped to the Pi |

---

## 3. The front end

FastAPI, server-rendered Jinja templates, polling for state and SSE for signature
prompts. No build step and no `node_modules` on the Pi.

### 3.1 Panels

1. **Health** — Pi, phone, Appium, last heartbeat. One line each, green or red.
2. **Today** — each visit: status, **scheduled time and observed time side by side**,
   divergences highlighted.
3. **Gate** — live presence signals: USB transport, gateway MAC, wifi BSSID, last fix.
   Shows *why* a check-off was allowed or refused.
4. **Phone view** — latest screenshot, subject to `FLAG_SECURE` (REQ-1).
5. **Signature** — the SSE-driven prompt (REQ-10). Names the patient and visit, then a
   canvas with brush width, undo, and clear.
6. **Needs attention** — failures and skips, with acknowledge.
7. **Reconciliation** — end-of-shift view to check against the handwritten log.

### 3.2 Acting on its behalf

The UI is mostly read-only by design. Three write paths exist, all deliberate:

- **Signature** — REQ-10, the main one
- **Acknowledge** — clears an alert
- **Pause / resume the scheduler** — an emergency stop when something is visibly wrong

There is no "check off this patient anyway" button. Overriding the location gate from a
web page would defeat the gate.

### 3.3 Language (REQ-11)

`en.json` / `es.json` catalogs, toggle persisted in localStorage, lookup in the
templates. No i18n framework for seven panels.

**The signature prompt and alerts must both be translated.** REQ-10.3 requires she can
see what she's attesting to before signing — that is only true in a language she reads,
and a browser auto-translating a page underneath a signature is not good enough.

---

## 4. Data

**SQLite in WAL mode**, at `/var/lib/aptlog/`. At a few dozen writes a day, Postgres would
be overhead with a daemon to supervise.

Two placement rules:

- **Outside the git checkout.** `manager.sh` runs `git checkout --force`; everything under
  `/opt/aptlog` is disposable. State lives in `/var/lib/aptlog/`, which updates never touch.
- **Never in the golden image.** See §5.2.

Alongside it, the append-only JSONL audit log (REQ-7) — one record per check-off attempt,
carrying scheduled and observed times as distinct fields, the gate signals, and the
signature hash.

---

## 5. The golden image

### 5.1 Why

Everything fiddly happens in Texas with a keyboard, a console, and time. Florida becomes:
write card, assemble, plug in. The previous attempt failed on a part discovered on site
with no way to fix it there.

The image also ships **proven** — validated against a real phone before it leaves.

### 5.2 What must not be cloned

A disk image copies identity, not just software. `scripts/sanitize-for-image.sh` strips:

| What | Why |
|---|---|
| SSH host keys | both Pis would present the same identity |
| `/etc/machine-id` | duplicates confuse journald and DHCP leases |
| Tailscale state | the clone would claim the source Pi's node |
| **`/var/lib/aptlog/*`** | her visit data must never travel inside an image file |
| Secrets, logs, shell history, `~/.android/adbkey` | per-deployment or noise |

`aptlog-firstrun.service` regenerates the first three on first boot, then disables itself.

### 5.3 The Tailscale key

Not baked into the image. `firstrun` reads `tailscale-authkey.txt` from the **boot
partition**, which is FAT32 and editable from Windows Notepad before the card ever enters
the Pi.

One paste, no terminal, and a live credential never rides along in a file on a cloud
drive.

Build and flash mechanics are in [IMAGE_BUILD.md](./IMAGE_BUILD.md).

---

## 6. Why not Docker

Considered and declined for v1.

The agent needs `adb` device access and `uhubctl` raw USB control. Both work in
containers only with privileged device passthrough, and both get fragile across restarts —
in a box whose primary recovery mechanism *is* USB power-cycling, that is the wrong layer
to add.

More directly: **the golden image already provides what Docker would.** Reproducible,
identical every time, clone-and-go. Docker on top is a second packaging system solving a
problem already solved, and one more thing to debug from 1,000 miles away.

The UI and database touch no hardware and could be containerised later without affecting
the agent. Not worth it while the whole system is one process tree on one board.

---

## 7. Staying connected after it ships

Three independent channels, deliberately not sharing a failure mode.

### 7.1 Deploys — pull, always

`manager.sh` polls the `release` branch every 10 minutes: test, deploy, health-gate, roll
back on failure. No inbound access, no webhook, no port forwarding.

It keeps working when Tailscale is down — which matters, because Tailscale is how you
would otherwise fix things.

**`release`, not `main`.** Pushing to main does not touch the device.

### 7.2 Interactive — Tailscale

SSH from a cloud session, joining the tailnet with an ephemeral key (OPERATIONS.md §3.2).
May be blocked by a restrictive egress policy; test early. Losing it costs debugging, not
the ability to ship.

### 7.3 Liveness — heartbeat (REQ-12)

Everything above alerts on failures it *notices*. Nothing catches the Pi being dead,
unplugged, or offline, because a dead Pi sends nothing.

A **dead-man's switch** inverts it: the Pi pings outward on a timer, and silence raises
the alarm.

```bash
curl -fsS -m 10 https://hc-ping.com/<uuid>
```

Ping only **after** a real check — phone attached, services active, no stuck run. A
heartbeat that fires while the agent is broken is worse than no heartbeat.

Outbound-only, so it works behind the building's NAT with nothing forwarded, and it keeps
reporting when Tailscale is down.

---

## 8. Development flow

```
Texas laptop  ──push──►  GitHub main  ──►  merge to release  ──►  Florida Pi pulls
Cloud session ──push──►       ▲                                        │
                              │                                    heartbeat
                         Tailscale SSH ─────────────────────────────────┘
                         (interactive, best-effort)
```

`main` is where work lands. `release` is what the device runs. The gap between them is
deliberate and is the only thing standing between an experiment and someone's visit
records.
