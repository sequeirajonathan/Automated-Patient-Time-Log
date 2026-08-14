# Cloud session handoff

You are picking up development of this project from a laptop session in Texas. The
reference Raspberry Pi is built, booted, and on the tailnet. **No application code exists
yet.**

Read [REQUIREMENTS.md](./REQUIREMENTS.md) before writing anything. It is the spec, and the
person you are working with wrote it — see §8.

> **This repository is public.** Never commit auth keys, Wi-Fi passphrases, gateway MACs,
> BSSIDs, site coordinates, schedules, fixtures, audit output, or anything naming a
> patient. Ask for those values in chat; they live on the device, not in git.

---

## 1. Connecting to the Pi

```bash
export TS_EPHEMERAL_KEY='tskey-auth-...'    # ask; reusable + ephemeral + tag:cloud
bash scripts/cloud-session-up.sh
```

That joins the tailnet with userspace networking and verifies the Pi is actually
reachable. Then:

```bash
tailscale --socket=/tmp/tailscaled.sock ssh apt@aptlog
```

**Userspace networking means a plain `ssh` will not route through the tunnel.** Use
`tailscale ssh`, or `ALL_PROXY=socks5://localhost:1055 ssh apt@aptlog`.

If the bootstrap fails to join, the sandbox's egress policy is blocking Tailscale — it
prefers UDP 41641 and falls back to DERP over TCP/443, and a restrictive policy blocks
both. **That costs you interactive debugging, not the ability to ship.** See §5.

Host: `aptlog` · user: `apt` · both key and password SSH auth are enabled.

---

## 2. State of the machine (verified, not assumed)

| | |
|---|---|
| Board | Raspberry Pi 5 Model B Rev 1.1, 8 GB |
| OS | Debian 13 (trixie), arm64, kernel 6.18.34+rpt-rpi-2712 |
| Power | `vcgencmd get_throttled` = `0x0` — no undervoltage on the 27 W supply |
| Network | ethernet primary (route metric 100), Wi-Fi standby (metric 600) |
| Wi-Fi | two NetworkManager profiles: the Texas dev SSID (active) and the Florida SSID (stored, activates in range) |
| Tailscale | enrolled, `RunSSH: True`, `tailscale serve` publishing `:8080` |
| Timezone | `America/New_York` — the *building's* zone, deliberately not Texas |
| Watchdog | `RuntimeWatchdogUSec=15s` armed |
| journald | `Storage=volatile` (SD-card wear) |
| Checkout | `/opt/aptlog` tracking the `release` branch |

Installed: `node` 22.23.2 · `appium` **3.6.0** · `uiautomator2` 8.4.0 · `adb` 1.0.41 ·
`python3` **3.13.5** · `uhubctl` 2.6.0 · `sqlite3` 3.46.1 · `tailscale` 1.102.2

Two of those contradict REQUIREMENTS §3 — see §7.

---

## 3. What is NOT built

Everything. The repo contains specs, systemd units, and shell scripts. There is no
`src/`, no `pyproject.toml`, no `tests/`.

Consequently `aptlog-agent` and `aptlog-ui` **crash-loop by design**:

- `aptlog-agent` — `ModuleNotFoundError: apt_log`, exit 1
- `aptlog-ui` — exit 203/EXEC, no `uvicorn` in the venv
- `tailscale serve` therefore proxies to a dead backend; `/healthz` does not answer

`aptlog-appium` **is** healthy and listening on `127.0.0.1:4723`.

Do not "fix" the crash loops. They resolve when the package exists.

---

## 4. ⚠️ Do not push to `release`

`manager.sh` polls `release` every 10 minutes and **cannot currently deploy anything**.
Missing: `pyproject.toml`, `tests/`, `src/apt_log/`, `scripts/alert.sh`, and `pytest`
itself is not in the venv.

The moment `release` moves it will: record last-good → check out the new revision → run
pytest → fail → attempt rollback → `pip install -e` errors → **`set -e` kills the script
mid-rollback**, so services are never restarted. Then `alert.sh` does not exist and
`|| true` swallows the 127, so you get a half-rollback in total silence.

**Push to `main`. `main` is where work lands; `release` is what the device runs**
(OPERATIONS §2.2). Before the first `release` push, the deploy path needs:

- [ ] `pyproject.toml` with the package and `pytest` as a dev dependency
- [ ] `src/apt_log/` importable, with `cli.py` exposing `run --daemon`
- [ ] `ui.py` serving `/healthz` on `127.0.0.1:8080` (the manager's health gate)
- [ ] `tests/` with at least one passing test
- [ ] `scripts/alert.sh`, executable — REQ-9 alerting is a no-op without it

---

## 5. The two channels, and which one matters

**Deploys are pull-based and independent of the tunnel.** `manager.sh` fetches `release`
on a timer. No inbound access, no port forwarding, and it keeps working when Tailscale is
down — which matters, because Tailscale is how you would otherwise fix things.

**Interactive SSH over Tailscale is best-effort.** If your sandbox blocks it, say so
plainly and fall back to pushing to `main`, then having the operator merge to `release`.
Do not design around the assumption that the tunnel works.

---

## 6. Non-negotiable constraints

These come from the spec and from the project owner directly. Do not relax them, and do
not treat them as defaults to be optimised away.

- **The presence gate fails closed.** If presence cannot be verified, skip and alert.
  Never queue an action to fire later from an unverified position (REQ-5.5).
- **adb over TCP is prohibited in code, not by convention.** Reject any device entry
  matching `<ip>:<port>`. It would let the phone be anywhere on the network (REQ-5.4).
- **Never spoof or inject GPS to pass the gate.** `StubLocationSource` exists to test that
  the gate correctly *allows and denies*, and the active implementation is stamped into
  every audit entry so a stub-sourced record cannot be mistaken for an observed one
  (REQ-5.7).
- **Signatures are captured fresh per prompt and discarded after replay.** Never cache a
  signature and re-stamp it. That would make the system sign on her behalf rather than
  transmit her signing (REQ-10.6).
- **Scheduled time and observed time are separate fields everywhere**, never collapsed
  (REQ-7, §1.1).
- **The handwritten log is authoritative.** This tool produces a draft to confirm or
  correct. It is not a system of record.
- **Do not wire this to the live agency account** (REQ-0). `--production` must hard-fail
  unless `PRODUCTION_AUTHORIZED` is set, and must refuse entirely while the gate is on
  stub signals.
- **Patient names and IDs are PHI.** Store identifiers, never names. Redact from all logs.
  Never commit real data.
- **State lives in `/var/lib/aptlog/`, never inside the checkout.** `manager.sh` runs
  `git checkout --force` on `/opt/aptlog` and will wipe anything there.

---

## 7. Open questions — get answers before building

**~~Appium 3 vs 2~~ — SETTLED. Appium 3, pinned.** `appium-uiautomator2-driver` declares
`peerDependencies { appium: ^3.0.0-rc.2 }`, so "Appium 2 + uiautomator2" no longer exists
upstream. `firstboot.sh` now pins `APPIUM_VERSION=3.6.0` and `UIA2_VERSION=8.4.0`, and
REQUIREMENTS §3 was amended to match.

**~~Python 3.13 vs 3.12~~ — SETTLED. 3.13**, the version trixie ships. Spec amended.

**~~Which `Appium-Python-Client`~~ — SETTLED. `6.0.0` against server `3.6.0`**, proven by a
full six-check probe run. No stepping back through 5.x was needed. Keep it pinned.

**Lesson for the probe's UNKNOWN heuristic.** It twice reported "resembles an
Appium-Python-Client / server major mismatch" when the real causes were different: first a
missing `ANDROID_HOME`, then `io.appium.settings` failing to start. The instinct to say
UNKNOWN rather than FAIL was right and saved the project from a false negative — but both
of those have distinctive error strings and should be matched explicitly so the hint points
at the actual cause.

**REQUIREMENTS.md contradicts itself on the secret store.** §6's acceptance list says
credentials come from "the OS keychain"; REQ-3 mandates a file-backed provider at
`/etc/aptlog/secrets.env` because a headless Pi has no keychain to unlock. **REQ-3 is
authoritative** — the owner confirmed this, on the grounds that nothing on the Pi may
require a human to enter anything at boot. The §6 line is stale and should be fixed.

**`firstrun.sh` auth-key parsing is fragile.** It takes the first non-comment, non-blank
line of `tailscale-authkey.txt`, so any stray label a non-technical person types above the
key becomes "the key" and enrolment fails silently in Florida. Harden to
`grep -oE 'tskey-[a-zA-Z0-9-]+'` before the image ships (REQ-13).

**Site survey values are unobtainable from Texas.** REQ-5's two strong anchors — the
building's default gateway MAC and the Wi-Fi BSSID the phone associates to — are physical
facts about the Florida site. Without them the gate has only the USB signal, and REQ-5.2
requires USB **plus** one strong anchor, so it fails closed on every check-off. This is
the project's longest-lead item.

---

## 8. Working with the project owner

He wrote REQUIREMENTS.md himself. **Precedence: his instruction in chat → REQUIREMENTS.md
→ every other doc.** When he says something the spec contradicts, do what he says *and*
name the conflict in the same reply so he can confirm or correct it. Never resolve a
conflict silently in either direction.

Ask for the app package name, site coordinates, and any auth keys when you need them.
**Do not guess, and do not invent placeholder values that could end up committed.**

---

## 9. Where the work is

**Phase 2 is done. REQ-1 passed all six checks — the project is feasible. REQ-2 is
greenlit.**

| # | Check | Result |
|---|---|---|
| 1 | launches under Appium *(blocking)* | **PASS** — process alive 8s after session start |
| 2 | view hierarchy dumpable *(blocking)* | **PASS** — 8,709 chars of XML |
| 3 | FLAG_SECURE | **PASS** — screenshot captured, not set |
| 4 | automation not blocked *(blocking)* | **PASS** — ran with UiAutomator attached |
| 5 | Play Integrity | **PASS** — runs on an uncertified handheld |
| 6 | cold start reaches login | **PASS** — login screen present after `pm clear` |

Because check 3 passed, the UI's phone-view panel renders real screenshots rather than the
`FLAG_SECURE` placeholder the spec allows for.

**This run is not a validated result and must be repeated.** It deviated from production
in three ways, all recorded here so nobody mistakes it for the real thing:

- **adb over TCP**, not USB — a one-off operator-authorised exception because the USB
  cable is dead. REQ-5.4 still stands and the code guard was never weakened; the override
  lived in a throwaway clone and was never committed.
- **A Retroid Pocket 4 Pro gaming handheld**, not a phone.
- **`appium:skipDeviceInitialization`** was required, because `io.appium.settings` cannot
  start on that device — the intent is accepted and no process ever spawns. **That
  workaround must not reach production**; a real phone will not need it.

Checks 1, 2, 3, 4 and 6 are properties of *the app* and transfer. Check 4 is sound despite
the workaround, because `skipServerInstallation` was left `False`, so UiAutomator really
was attached and the app really did tolerate it.

**Re-run the probe on the production phone, over USB, before the golden image ships.**

**Phase 1 is still incomplete.** Three of the five PI_SETUP §4 checks need a phone attached
over *USB*: `adb devices -l`, the `uhubctl` port check, and the pull-the-plug test.

---

## 10. Hardware notes

**There is currently no working USB link to the device.** `lsusb` shows only the Pi's four
root hubs and the kernel logged zero enumeration events across a monitored replug — so the
cable carries no data lines, or the connection isn't seating. PI_SETUP §0.4 asks for "USB-A
to the phone's connector — **data, not charge-only**" precisely because of this.

Until that is fixed:

- REQ-5's USB-transport signal cannot be exercised, so **the presence gate can never pass**
  (REQ-5.2 requires USB *plus* one strong anchor). Gate work can be unit-tested against
  `StubLocationSource`, but not validated end to end.
- Three PI_SETUP §4 checks stay blocked.

**The powered hub is also still absent.** It is required before the golden image ships:
the Pi caps all four ports at 1.6 A combined, and PI_SETUP §1.7 is explicit that a phone
drawing more produces random disconnects that read exactly like software faults. `uhubctl`
2.6.0 is installed and the Pi 5's root hubs report `ppps`, so recovery-ladder rung 5 works
against the Pi's own ports in the meantime.

**The Appium server needs `ANDROID_HOME`.** Without it the uiautomator2 driver refuses to
create any session. Fixed in the unit file and `firstboot.sh`; if you rebuild a Pi from an
older revision, that is the first thing to check.
