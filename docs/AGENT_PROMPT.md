# Kickoff prompt for a local Claude Code session

Paste the block below into a local `claude` session opened in this repo, on the branch
`claude/android-automation-stack-lifpw8`. It is written to be self-contained — the local
session starts cold with no memory of the design conversation.

---

```
Read docs/REQUIREMENTS.md first — it is the spec for this work. Then build the Phase 1
POC it describes.

CONTEXT
I do patient visits on a fixed schedule at a multi-floor care site. Site policy requires
personal devices to be stored in a dormitory area, so my phone is physically at the
building but not on me during the shift. I can't walk back to the storage area after each
visit to check patients off in the agency's app, which is causing time discrepancies. I
keep a handwritten log during the shift as the real record.

I want scheduled check-offs that only fire when the device is verifiably at the building,
plus an end-of-shift report I can reconcile against my handwritten log. Eventually this
moves to a Raspberry Pi with a GPS module driving the same phone; right now I need to
prove it works via scripts and automation.

ENVIRONMENT
- This local machine, plus my real Android phone connected over USB.
- Do NOT use an emulator as the primary target. The app is a healthcare/EVV app and will
  likely refuse to run under Play Integrity on one. A real device is also what the Pi
  will drive in production, so it's the representative target.
- Confirm `adb devices` lists the phone before doing anything else.

START HERE — this gates everything
Build and run REQ-1, the feasibility probe, before writing any other feature code. It
answers whether this project is possible at all: whether the app runs under Appium,
whether the view hierarchy is dumpable, and whether it detects and blocks the
UiAutomator/accessibility service. If checks 1, 2, or 4 fail, STOP and tell me — no
amount of stack work fixes that, and the fallback is the vendor's API instead. Don't
build the rest speculatively while that's unknown.

THEN
Work through REQ-2 to REQ-9 in order. After the probe passes, get one real patient
check-off working end to end before generalizing to the full schedule.

NON-NEGOTIABLE CONSTRAINTS (from REQUIREMENTS.md §1.1 and REQ-0/REQ-5)
- The location gate fails CLOSED. If presence can't be verified, skip the action and
  alert. Never queue it to fire later from an unverified location.
- Do NOT spoof, mock, or inject GPS coordinates as a way of passing the gate. The whole
  point is that the device really is at the building. In the POC the gate is tested with
  scripted StubLocationSource fixtures to prove it correctly ALLOWS and DENIES; that stub
  must be recorded in every audit entry so a stub-sourced record can never be mistaken
  for an observed one.
- Do NOT wire this to my live agency/EVV account. Use a staging account or a stand-in
  app. The `--production` flag must hard-fail while the location source is a stub.
- Scheduled time and observed time are separate fields everywhere. A cron job records
  when a visit was SUPPOSED to happen, not when it did. My handwritten log is the
  authority; this tool produces a draft I confirm or correct.
- Patient names and IDs are PHI. Redact them from logs, keep the audit log out of any
  synced folder, and never commit real patient data or credentials.

STACK (already decided — build on it, don't re-litigate)
Python 3.12, Appium 2 + uiautomator2 driver, Appium-Python-Client, pytest +
pytest-rerunfailures, APScheduler, keyring behind a SecretProvider interface, SQLite +
append-only JSONL audit, structlog with a redaction processor, typer CLI.
Full rationale is in docs/REQUIREMENTS.md §3.

LAYOUT
├── pyproject.toml
├── src/apt_log/
│   ├── cli.py            # typer: probe / run / report
│   ├── device.py         # adb + Appium session lifecycle, wake/unlock, watchdog
│   ├── location.py       # LocationSource interface, StubLocationSource, geofence gate
│   ├── secrets/          # SecretProvider, KeyringProvider
│   ├── screens/          # page objects — one file per screen, selectors ONLY here
│   ├── schedule.py       # APScheduler wiring, tz-aware
│   ├── audit.py          # JSONL writer + SQLite idempotency store
│   └── report.py         # reconciliation report
├── tests/
└── config/schedule.example.yaml   # example data only, never real patients

FIRST TASK
1. Verify `adb devices` sees the phone; tell me the Android version and the target app's
   package name (`adb shell pm list packages`).
2. Scaffold the project skeleton above.
3. Implement and run the REQ-1 probe.
4. Report the PASS/FAIL table and stop for my go/no-go before continuing.

Ask me for the app package name and my site's lat/lon when you need them — don't guess or
use placeholders that could end up in a committed config.
```

---

## Before you run it

On the local machine:

```bash
npm install -g @anthropic-ai/claude-code
npm install -g appium && appium driver install uiautomator2

# adb — one of:
#   macOS:  brew install --cask android-platform-tools
#   Linux:  apt install android-sdk-platform-tools
#   Win:    winget install Google.PlatformTools

git clone <repo> && cd Automated-Patient-Time-Log
git checkout claude/android-automation-stack-lifpw8
claude
```

Also needed: JDK 17, Python 3.12.

On the phone: Developer Options → USB debugging enabled, then confirm `adb devices` lists
it as `device` (not `unauthorized` — accept the RSA prompt on the phone if so).

## Things to have ready

The local agent will ask for these; having them to hand saves a round trip:

- **App package name** — `adb shell pm list packages | grep -i <vendor>`
- **Site coordinates** — the building's lat/lon, and how tight a geofence radius you want
  (100 m is a reasonable default; tighter risks false denials from indoor GPS drift)
- **A staging/sandbox account**, or a decision to probe against a stand-in app
- **Whether the agency's EVV vendor has an API** — worth ten minutes before any of this,
  since it would replace the whole stack with a supported path
