# Kickoff prompt for a development session

Paste the fenced block below into a `claude` session on the Texas development machine.
It is written to be self-contained — the session starts cold with no memory of the design
conversation.

Supersedes the earlier version of this file, which described a laptop-and-phone POC
before the Raspberry Pi, the signature flow, and the golden-image pipeline existed.

---

```
We're building an automated patient time-log system. I'm developing in Texas; the
finished unit ships to Florida as a cloned SD image. Work through this in order and
stop at the checkpoints — don't run ahead.

FIRST: read the specs
This repo is the project. Pull main and read these before doing anything:

    git checkout main && git pull origin main

  docs/ARCHITECTURE.md   — the whole system, read this first
  docs/REQUIREMENTS.md   — REQ-0..REQ-13, the spec you're building to
  docs/PI_SETUP.md       — flashing and assembly
  docs/OPERATIONS.md     — self-healing, updates, remote access
  docs/IMAGE_BUILD.md    — the ship step, last

REQUIREMENTS.md is authoritative. If anything else disagrees with it, it wins — tell
me about the conflict rather than picking one silently.

WHERE I AM RIGHT NOW
- Raspberry Pi 5, in hand, not yet flashed
- A 32GB microSD card attached to this machine, ready to be flashed
- A test Android phone I can use for development
- Windows laptop with a lot of dev tooling already installed
- Nothing built yet — there is no application code in the repo, only specs, systemd
  units, and shell scripts

⚠ HARDWARE SAFETY — READ BEFORE ANY DISK COMMAND
This laptop also has a 1TB SSD with a Batocera arcade install that must NOT be touched.
dd and raw disk writes are instant and irreversible.

Before every imaging or dd command: list the disks, confirm the target is the ~32GB SD
card and NOT the ~931GB SSD, and show me the command for confirmation before running
it. Never reuse a device path from an earlier step — Windows and Linux both renumber
devices between replugs.

ENVIRONMENT
- Windows laptop. Prefer PowerShell for Windows-native work.
- Use WSL2 for anything needing bash, dd, or PiShrink.
- Check what's already installed before installing anything — I have a lot of tooling
  already and don't want duplicates.
- The Pi will be on my LAN; develop against it over SSH.
- The test phone plugs into the Pi, not the laptop — that matches production and means
  adb and Appium behave the way they will in Florida.

PHASE 1 — get the Pi running
Follow docs/PI_SETUP.md. Flash the SD card, boot, SSH in, run scripts/firstboot.sh.
Then run the five verification checks in section 4, including the pull-the-plug test.
Report the results and stop.

Note: firstboot.sh is for this reference Pi, run by hand. scripts/firstrun.sh is a
different thing — it runs automatically on the first boot of a *cloned* image later.
Don't confuse them.

PHASE 2 — the go/no-go (before any feature code)
Build and run the REQ-1 feasibility probe against the real target app on the phone.
It answers whether this project is possible at all: does the app run under Appium, is
the view hierarchy dumpable, does it detect and block the UiAutomator service.

If checks 1, 2 or 4 fail, STOP and tell me — no amount of engineering fixes that, and
the fallback is the vendor's API instead. Report the PASS/FAIL table and stop for my
go/no-go.

PHASE 3 — build the application
Only after I greenlight Phase 2. Work through REQ-2 to REQ-13 in order. Get one real
check-off working end to end before generalizing.

Layout:
  src/apt_log/
    cli.py          typer: probe / run / report
    device.py       adb + Appium lifecycle, wake/unlock, recovery ladder
    presence.py     REQ-5 multi-signal gate: USB transport, gateway MAC, BSSID, fix
    location.py     LocationSource interface: Stub + PhoneLocationSource
    signature.py    REQ-10 capture, mapping, W3C pointer replay
    secrets/        SecretProvider, file-backed at /etc/aptlog/
    screens/        page objects — selectors ONLY here
    schedule.py     APScheduler inside the agent process, tz-aware
    audit.py        JSONL + SQLite idempotency store
    ui/             FastAPI + Jinja, SSE, en.json / es.json
    report.py       reconciliation

  State goes in /var/lib/aptlog/ — NEVER inside the repo checkout. manager.sh
  force-checkouts /opt/aptlog on every update and will wipe anything in there.

NON-NEGOTIABLE CONSTRAINTS (from the specs — don't relax these)
- The presence gate fails CLOSED. If presence can't be verified, skip and alert. Never
  queue an action to fire later from an unverified position.
- adb over TCP is prohibited in code, not just convention (REQ-5.4). It would let the
  phone be anywhere on the network and breaks the whole chain of reasoning.
- Never spoof or inject GPS to pass the gate. StubLocationSource exists to test that
  the gate correctly ALLOWS and DENIES, and must be stamped into every audit entry so a
  stub-sourced record can't be mistaken for an observed one.
- Signatures are captured fresh per prompt and discarded after replay. Never cache a
  signature and re-stamp it (REQ-10.6).
- Scheduled time and observed time are separate fields everywhere. The handwritten log
  is authoritative; this tool produces a draft to confirm or correct.
- Don't wire this to the live agency account (REQ-0).
- Patient names and IDs are PHI. Redact from logs, keep identifiers not names on the
  device, never commit real data.

PHASE 4 — validate before imaging
Everything in docs/IMAGE_BUILD.md section 1 must pass, including pull-the-plug twice.
The point of the golden image is that Florida receives something already proven.

PHASE 5 — build the image
docs/IMAGE_BUILD.md. Sanitize, capture, PiShrink, compress, hash.
Re-read the hardware safety warning above before this phase.

Start with Phase 1. Ask me for the app package name, the site coordinates, and the
Tailscale auth key when you need them — don't guess or use placeholders that could end
up committed.
```

---

## Inputs to have ready

The session will ask for these:

- **App package name** — `adb shell pm list packages | grep -i <vendor>`
- **Site coordinates** and geofence radius, plus the building's **gateway MAC** and
  **wifi BSSID** (REQ-5 anchors) — collect these on site or from someone there
- **Tailscale auth key** — must start `tskey-auth-`, not `tskey-api-`
- **A staging/sandbox account**, or a decision to probe against a stand-in app
- **Whether the agency's EVV vendor has an API** — worth ten minutes before any of this,
  since it would replace the whole stack with a supported path (REQ-0)
