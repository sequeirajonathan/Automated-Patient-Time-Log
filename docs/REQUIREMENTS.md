# Automated Patient Time Log — Requirements

**Status:** building on a Raspberry Pi 5 in Texas against a test Android phone, to be
cloned as an SD image and deployed to a resident's room in Florida.

This document is the spec. Where anything else disagrees with it, this wins.

| Document | Covers |
|---|---|
| **REQUIREMENTS.md** (this) | what the system must do |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | how the pieces fit together |
| [PI_SETUP.md](./PI_SETUP.md) | building and flashing a unit |
| [OPERATIONS.md](./OPERATIONS.md) | self-healing, updates, remote access |
| [IMAGE_BUILD.md](./IMAGE_BUILD.md) | capturing and shipping the golden image |
| [AGENT_PROMPT.md](./AGENT_PROMPT.md) | kickoff prompt for a development session |

---

## 1. Problem context

The operator works a multi-floor care site. Site policy requires personal devices to be
stored in a dormitory/locker area, so the phone is physically at the building but not on
the operator's person during the shift. Patient visits happen on a fixed schedule, but the
operator cannot return to the storage area after each visit to check patients off in the
agency's app. Time discrepancies accumulate as a result.

The operator keeps a **handwritten log** during the shift as the ground-truth record.

This system automates the in-app check-off on schedule, gated on the device being
verifiably at the building, and produces an end-of-shift reconciliation report that the
operator compares against the handwritten log.

### 1.1 Record-accuracy principle (drives several requirements below)

A scheduled job records the **scheduled** time. The **actual** time may differ — the
operator runs late, a patient refuses care, a visit is skipped or reordered. The system
must therefore:

- never conflate scheduled time with observed time (REQ-7)
- treat the handwritten log as authoritative, not the automation (REQ-8)
- make amendment of a wrong entry easy and expected, not exceptional (REQ-8)
- refuse to act when it cannot verify presence, rather than assume (REQ-5)

The automation produces a **draft** for the operator to confirm or correct. It must not be
built or described as a system of record.

---

## 2. Scope

### In scope
- The Raspberry Pi 5 controller, driving a real Android phone over USB
- The status and signature UI (REQ-10, REQ-11) — the caregiver works on other floors and
  this is her only view of what the system is doing
- Self-update, self-healing, and liveness monitoring (REQ-12, OPERATIONS.md)
- The golden-image build and deployment path (REQ-13, IMAGE_BUILD.md)

### Out of scope
- Any write to a **production** EVV/agency system until REQ-0 is satisfied
- Multi-operator or multi-site support
- A dedicated GPS receiver — see §7

---

## 3. Architecture decisions (already settled — do not re-litigate)

| Layer | Choice | Rationale |
|---|---|---|
| Controller | Raspberry Pi 5, Raspberry Pi OS Lite 64-bit, microSD | see §7 for what was tried and rejected |
| Language | Python 3.13 | glue for secrets, scheduling, parsing; the version Raspberry Pi OS trixie ships |
| Driver | Appium 3 + `uiautomator2` driver, both **version-pinned** | the mature Android equivalent of Playwright. `appium-uiautomator2-driver` requires `appium ^3`, so Appium 2 is no longer available with a current driver. Pinned in `firstboot.sh` because an unpinned install makes a rebuilt image unreproducible — see §3.1 |
| Client | `Appium-Python-Client` | thin WebDriver wrapper |
| Device | **real phone over USB** (not emulator) | emulators fail Play Integrity; the phone's own GPS and wifi are presence signals |
| Runner | `pytest` + `pytest-rerunfailures` | fixtures for device lifecycle, free retries |
| Scheduling | APScheduler **inside** the agent process; systemd supervises the process | one long-lived daemon is easier to watchdog than many timer-fired shots |
| Secrets | `SecretProvider` interface; file-backed at `/etc/aptlog/`, service-user readable | a headless Pi has no unlocked OS keychain — see REQ-3 |
| Store | SQLite (WAL) at `/var/lib/aptlog/` + append-only JSONL audit | idempotency constraints + tamper-evident trail |
| UI | FastAPI + Jinja, SSE for signature prompts, EN/ES | no build step, no `node_modules` on the Pi |
| Publishing | `tailscale serve` → `https://aptlog.<tailnet>.ts.net` | no domain, no port forwarding, not public |
| Logging | `structlog` with a redaction processor | credentials and PHI must never reach a log sink |
| CLI | `typer` | subcommands for probe / run / report |

**No Docker**, and no emulator. Rationale in ARCHITECTURE.md §6 — the short version is
that container USB passthrough is fragile in a box whose recovery mechanism *is* USB
power-cycling, and the golden image already provides the reproducibility Docker would.

### 3.1 Pin every third-party version

Every externally-sourced dependency — the Appium server, its drivers, Python packages —
must be pinned to an explicit version in the script or manifest that installs it. Never
`npm install -g <pkg>` or an unbounded `pip install`.

The golden image's whole claim is that Florida receives something **already proven**
(ARCHITECTURE.md §5.1), and IMAGE_BUILD.md §6 makes `firstboot.sh` the way back if the
reference Pi is lost. An unpinned install breaks both: a rebuild months later installs
whatever shipped that day rather than what was validated, and it does so silently, at the
moment you are already recovering from a failure.

This is a reproducibility requirement, not a stability preference. Upgrades are a
deliberate edit to the pin, validated against the checklist in IMAGE_BUILD.md §1.

---

## 4. Functional requirements

### REQ-0 — Production write guard (blocking, applies to all phases)
The POC must **not** submit check-offs into a live agency/EVV system.

- Target must be a staging/sandbox account or a stand-in app.
- A `--production` flag must exist but hard-fail with an explanatory error unless a
  `PRODUCTION_AUTHORIZED` config value is explicitly set, and must refuse entirely while
  the presence gate is running on stub signals (REQ-5.7) **or while the transport mode is
  `dev` rather than `usb` (REQ-5.4.1)**. Both are development affordances that let the gate
  reach a verdict it did not earn from observation.
- Rationale: the development unit in Texas is not at the building, so any presence it
  attests to is not presence it observed. This holds for the Texas Pi exactly as it held
  for a laptop. See §1.1.

**Before any production use**, check whether the agency's EVV vendor (HHAeXchange, Sandata,
CareBridge, etc.) offers a sanctioned API, batch import, or supervisor-correction path. If
one exists, that path replaces this entire UI-automation stack and should be preferred.

### REQ-1 — Feasibility probe (go/no-go; gates all other work)
A `probe` command that answers, against the real target app on a real device:

1. Does the app launch and remain running under Appium?
2. Is the view hierarchy dumpable (`driver.page_source` returns a usable tree)?
3. Does the app set `FLAG_SECURE` (blocks screenshots — breaks Appium screen capture)?
4. Does the app detect and refuse the accessibility/UiAutomator service?
5. Does it run at all under Play Integrity on this device?
6. Is the login screen reachable from a cold start (`pm clear` → launch)?

Output a PASS/FAIL report per check. **If 1, 2, or 4 fail, stop** — no stack choice
rescues it, and the vendor-API route becomes the only viable path.

### REQ-2 — Device session management
- Connect over `adb` USB; detect and report device disconnect.
- Wake and unlock the device before a run (`input keyevent 82`).
- Cold-start the app for every scheduled run; never assume a live session.
- Watchdog: re-initialize the adb connection if the device disappears mid-run.

### REQ-3 — Authentication
- Credentials resolved through a `SecretProvider` interface. The production
  implementation is **file-backed** at `/etc/aptlog/secrets.env`, mode `0600`, owned by
  the service user.
- A headless Pi that must boot unattended has no unlocked OS keychain and no operator to
  unlock one, so `keyring` is not usable here. This is obfuscation against casual access,
  not protection against someone holding the SD card — see PI_SETUP.md "Known constraint".
- Never accept a password as a CLI argument (leaks to `ps` and shell history).
- Detect the login screen at the start of every run and re-authenticate if present.
- Suppress screenshots while a password field has focus.
- The phone's unlock PIN lives in the same store (OPERATIONS.md §1.4).
- Secrets are stripped by `sanitize-for-image.sh` and never ship in an image (REQ-13).

### REQ-4 — Patient check-off flow
- Locate the patient by **search-by-name**, never by list index or scroll position.
- **Idempotent**: a retry must not produce a duplicate check-off. Enforce with a UNIQUE
  constraint on `(patient_id, visit_date, scheduled_slot)` in SQLite.
- **Verify after acting**: confirm the UI reflects the check-off before recording success.
  A tap that appeared to land is not evidence it did.
- On ambiguity (multiple name matches, unexpected screen), abort and alert — never guess.

### REQ-5 — Presence gate (multi-signal)

The controller and phone live indoors, in a resident's room. GPS alone is the *weakest*
available signal there — concrete and interior walls make a satellite fix unreliable or
absent. The strongest evidence is physical and network attachment, so the gate is built
on those and treats location as corroboration.

**5.1 — Signals.**

| Signal | Source | Strength |
|---|---|---|
| adb transport is **USB, not TCP** | `adb devices -l` on the controller | strong — the phone is physically attached to this machine |
| Default gateway MAC matches the recorded building gateway | `ip neigh show default` | strong — the router does not move |
| Phone's associated wifi **BSSID** matches the building AP | `adb shell cmd wifi status` | strong — APs do not move, and range is ~30 m |
| Phone location fix inside the geofence | `LocationSource` | corroborating |
| WAN IP matches | outbound lookup, cached | weak — residential DHCP rotates |

**5.2 — Passing condition.** A check-off may proceed only if **both**:
- the adb transport is USB, **and**
- at least one **strong** network anchor matches (gateway MAC or wifi BSSID)

A location fix alone is never sufficient. Its absence is not disqualifying, since a fix
may be genuinely unavailable indoors.

**5.3 — Per-provider thresholds.** Location thresholds are **configuration, not
constants**, and differ by provider. A GPS fix and a fused/network fix have different
accuracy characteristics; a single threshold rejects one or rubber-stamps the other. As a
starting point, `gps` ≤ 25 m and `fused`/`network` ≤ 100 m, tuned by a documented site
survey — take an hour of readings where the phone will actually sit before fixing values.

**5.4 — adb over TCP is prohibited in production.** Enforce in code, not convention.
`adb tcpip` would let the phone be anywhere on the network and silently breaks the whole
chain of reasoning. Reject any device entry matching `<ip>:<port>`.

**5.4.1 — The development transport exception.** A `dev` transport mode exists so work can
continue when no USB data link is available. It is contained exactly as
`StubLocationSource` is (REQ-5.7), for exactly the same reason.

- **The default is `usb`.** The rejection in 5.4 is the default code path and is not
  weakened, removed, or made conditional on anything a caller passes.
- **`dev` is enabled only by a config value under `/etc/aptlog/`.** Never a CLI flag, never
  a default, never an environment variable a stray shell could set. Turning it on is a
  deliberate act on the device.
- **`transport_mode` is stamped into every audit entry** (REQ-7), so a TCP-sourced record
  can never be mistaken for one observed over USB — including by a reader who was not
  present when it was written.
- **`--production` refuses entirely while it is active** (REQ-0).

While `dev` is active the gate's transport precondition is treated as satisfied, so the
check-off flow, scheduler, audit trail and signature path can be exercised end to end
against a real device. **That concession is the whole reason for the clauses above**: a
gate that can pass without USB is tolerable only because the record says so and production
will not run.

**What `dev` cannot do.** It cannot validate REQ-5 end to end — the gate's central claim is
that the phone is physically attached to *this* controller, and no amount of stamping
substitutes for it. It also cannot satisfy REQ-12, whose heartbeat deliberately signals
`/fail` on a TCP-attached device. Both require USB before sign-off, and neither may be
marked complete on the strength of a `dev` run.

**5.5 — Fail closed.** If the gate fails, skip the action and alert. Never queue it to
fire later from an unverified position.

**5.6 — Record everything.** Every audit entry carries all five signals with their
individual results, not just the verdict, so a later reader can see *why* a check-off was
allowed.

**5.7 — Implementations.** `LocationSource` has `StubLocationSource` (scripted fixtures,
for testing that the gate correctly allows *and* denies) and `PhoneLocationSource`
(production; reads the phone's own fix over adb). The active implementation is stamped
into every audit entry so a stub-sourced record can never be mistaken for an observed one,
and REQ-0 refuses production while a stub is active.

**5.8 — What this does and does not prove.** The gate establishes that the *devices* are
at the building. It cannot establish that the caregiver is. That limit is inherent, and it
is why the handwritten log stays authoritative (§1.1) — no amount of signal strength
changes it.

### REQ-6 — Scheduler
- Load a shift schedule (patient, scheduled time, slot) from a local config file.
- Fire each check-off at its scheduled time, tz-aware; store UTC, display local; handle DST.
- Missed-run policy must be explicit and conservative: do not silently backfill.

### REQ-7 — Audit log
Append-only JSONL, one record per attempt, containing at minimum:

```
attempt_id, patient_id, scheduled_time_utc, observed_time_utc,
gate_result, location_source_type, transport_mode,
signals{ usb_transport, gateway_mac_match, bssid_match, wan_ip_match,
         fix{lat, lon, accuracy_m, provider, sats, timestamp} },
signature{ occurred, nonce, stroke_count, duration_ms, hash },
action_taken, ui_verification_result, error, app_version
```

`transport_mode` is `usb` or `dev` (REQ-5.4.1), and `location_source_type` names the active
`LocationSource` (REQ-5.7). **Both are mandatory on every record.** They are what let a
later reader tell an observed check-off from one produced under a development affordance,
and that distinction cannot be reconstructed after the fact — so a record missing either
field is not a valid record.

Record the individual signal results, not only `gate_result` (REQ-5.6). Store the
signature hash and metadata only, never the strokes or a bitmap (REQ-10.11).

`scheduled_time_utc` and `observed_time_utc` are **separate fields** and must never be
collapsed into one (§1.1).

### REQ-8 — Reconciliation report
- End-of-shift command rendering all attempts side by side: scheduled vs observed vs gate
  result vs outcome.
- Flag every divergence for operator review.
- Output a format the operator can check line by line against the handwritten log, with a
  clear column for "amend in app" so corrections are a normal step in the workflow.

### REQ-9 — Failure alerting
- Any failed or skipped run emits a notification (ntfy/Pushover) — the operator is away
  from the device and must not discover a silent failure at end of shift.
- Include the reason, the patient, and whether the gate passed.

### REQ-10 — Remote signature capture and replay

The app requires the **operator's own attestation signature** on each visit. The phone is
on the ground floor and she is not, so the signature is drawn by her on her own device and
replayed onto the target phone as a touch gesture.

This is a remote input device, not a signing service. The distinction is REQ-10.6 and it
is what keeps the signature hers.

**10.1 — Detection.** The agent detects the signature field being rendered, pauses the
check-off, and emits a `signature_requested` event. It must not proceed past this point
by any other route.

**10.2 — Notification.** The event is pushed to the UI over SSE or WebSocket, not polled.
She may be mid-visit on another floor; a page she has to refresh is a page she won't see.

**10.3 — Informed prompt.** Before the canvas is enabled, the UI must show **which patient
and which scheduled visit** the signature attests to. She cannot attest to something she
cannot see, and a signature request that doesn't identify its subject is not a valid one.

**10.4 — Capture.** An HTML canvas records pointer events as strokes of
`(x, y, t)` where x and y are **normalised to 0–1** against the canvas dimensions and `t`
is milliseconds from stroke start. Multiple strokes are preserved as separate arrays —
signatures lift the pen.

**10.5 — Transport.** Strokes POST to the agent with a **single-use nonce** issued with
the `signature_requested` event. A payload without a matching outstanding nonce is
rejected, so a captured request body cannot be submitted twice.

**10.6 — Freshness (non-negotiable).** Every signature is captured fresh, in response to a
specific prompt, and **discarded from memory once replayed**. The system must never store
a signature bitmap or stroke set for reuse on a later patient. Caching and re-stamping
would make the system sign on her behalf rather than transmit her signing, which is a
different thing entirely and not one this project builds.

**10.7 — Mapping.** Normalised strokes map onto the target element's bounds, not the
screen:
- source of truth is `element.rect` → `{x, y, width, height}` of the signature field
- `scale = min(rect.w / cap_w, rect.h / cap_h)`, then centre the result within the field
- **preserve aspect ratio** — letterbox rather than stretch; a stretched signature does
  not look like hers

**10.8 — Replay.** Appium W3C pointer actions: `pointer_down`, a sequence of
`pointer_move` interleaved with `pause` durations taken from the captured deltas, then
`pointer_up` — one batched `perform()` per stroke. Preserve the original timing; some
signature widgets apply velocity-based stroke smoothing, and uniform-speed replay is
visibly wrong. `adb shell sendevent` is the fallback if a widget rejects synthetic
pointer events.

**10.9 — Verification.** Confirm the signature registered before advancing — normally the
Submit/Next control becoming enabled. Do not assume the strokes landed.

**10.10 — Timeout.** If no signature arrives within a configurable window, **abandon the
check-off and alert**. Never submit the visit unsigned, and never hold the session open
indefinitely waiting.

**10.11 — Audit.** Record that a signature occurred: timestamp, patient, stroke count,
duration, nonce, and a hash of the stroke data. **Do not store the bitmap or the raw
strokes.** The hash proves a distinct signature happened for that visit without leaving a
reusable copy of her signature on the device.

### REQ-11 — Bilingual interface (English / Spanish)

- All operator-facing text ships in both `en` and `es`. JSON message catalogs, a toggle
  persisted in localStorage, lookup in the templates. No i18n framework is warranted at
  this size.
- **The signature prompt (REQ-10.3) and all alerts must be translated.** REQ-10.3 requires
  she can see which patient and visit she is attesting to *before* signing; that is only
  satisfied in a language she reads. A browser auto-translating a page underneath a
  signature does not satisfy it.
- Dates, times, and numbers follow the selected locale.
- No string is concatenated from fragments — full sentences per key, so grammar survives
  translation.

### REQ-12 — Liveness heartbeat

Every other alert in this system fires on a failure the agent *notices*. A dead,
unplugged, or offline Pi notices nothing and reports nothing, which is indistinguishable
from a quiet, healthy day.

- A timer pings an external monitor (healthchecks.io or equivalent) on a fixed interval.
  **Missing pings raise the alarm** — silence is the signal.
- The ping fires **only after** verifying: phone attached over USB and authorised, all
  units active, `/healthz` responding. A heartbeat sent while the agent is broken is worse
  than no heartbeat.
- A known-bad state signals explicitly (`/fail`) rather than going silent, so "broken" is
  distinguishable from "unreachable".
- Outbound only — no inbound access, no port forwarding, and it must keep reporting when
  Tailscale is down.
- The timer must **not** be `Persistent=true`. A replayed heartbeat would assert liveness
  for a period the machine was not alive.

### REQ-13 — Image hygiene

The production unit is cloned from an image built on a different machine. Identity and
data must not travel with it.

- `sanitize-for-image.sh` removes, before capture: SSH host keys, `/etc/machine-id`,
  Tailscale node state, **all of `/var/lib/aptlog/`**, secrets, logs, shell history, and
  `~/.android/adbkey`.
- **No patient data may exist in a shipped image**, under any circumstances. The
  operational database and audit log ship empty.
- `aptlog-firstrun.service` regenerates host keys and machine-id on first boot, enrols in
  Tailscale, starts services, then disables itself. It must be idempotent and must remain
  armed if it fails, so a missing auth key is recoverable by pasting one in and rebooting.
- The Tailscale auth key is **not** baked into the image. It is read from
  `tailscale-authkey.txt` on the FAT32 boot partition — editable from Windows without a
  terminal — and cleared once consumed.
- Application state lives in `/var/lib/aptlog/`, never inside the git checkout, since
  `manager.sh` force-checkouts `/opt/aptlog` on every update.

---

## 5. Non-functional requirements

**Security / PHI.** Patient names and IDs are PHI.

- **Full-disk encryption is not available** — it is incompatible with unattended
  power-cycle recovery (§7). The mitigation is to reduce what is worth taking: store
  patient **identifiers, never names**, in the database and audit log, and resolve names
  in the app on the phone.
- Treat physical possession of the SD card as full compromise, and rotate the app
  password if the hardware is lost or replaced.
- Redact credentials and patient identifiers from all log output via a `structlog`
  processor.
- On the development machine, keep the audit log and any fixtures outside synced folders
  (OneDrive/Dropbox/iCloud).
- Never commit schedules, fixtures, or audit output containing real patient data.

**Reliability.** Every failure mode is silent by default here, because nobody is watching
the device. Prefer loud failure over graceful degradation in every ambiguous case.

**Maintainability.** Selectors live only in page objects under `screens/`. An app update
will break them; the blast radius should be one file per screen.

---

## 6. Acceptance criteria

- [ ] `probe` runs against the real app on a real device and emits a PASS/FAIL report
- [ ] Login automates from a cold start, credentials sourced from the OS keychain
- [ ] One patient check-off completes and is verified in-UI
- [ ] Re-running the same check-off does **not** create a duplicate
- [ ] Gate **denies** on an out-of-range fixture and **allows** on an in-range one
- [ ] Gate denies when the gateway MAC and BSSID both fail to match, even with a good fix
- [ ] Gate denies when adb reports the device over TCP rather than USB
- [ ] Gate **allows** with no location fix at all, provided USB plus one strong anchor hold
- [ ] Audit entry records each signal individually, not just the verdict
- [ ] Every audit entry carries `transport_mode` and `location_source_type`
- [ ] `--production` refuses while `transport_mode` is `dev`
- [ ] `dev` transport cannot be enabled by a CLI flag or environment variable — only by
      config under `/etc/aptlog/`
- [ ] With `dev` off, a TCP device entry is still rejected outright
- [ ] Scheduler fires a run at its scheduled time and writes a complete audit record
- [ ] Audit record carries scheduled and observed times as distinct values
- [ ] Reconciliation report renders and flags an injected divergence
- [ ] A forced failure produces an alert
- [ ] `--production` refuses to run while `LocationSource` is a stub
- [ ] Signature request reaches the UI by push, naming the patient and visit
- [ ] A signature drawn on a phone-sized canvas replays legibly and undistorted into a
      differently-proportioned signature field
- [ ] Replaying a captured payload a second time is rejected (nonce consumed)
- [ ] No signature bitmap or stroke data survives on disk after replay
- [ ] Signature timeout abandons the check-off and alerts rather than submitting unsigned
- [ ] Every operator-facing string renders in both English and Spanish, signature prompt
      and alerts included
- [ ] Stopping the agent causes the external monitor to alarm within one interval
- [ ] Heartbeat does **not** ping while the phone is detached
- [ ] A sanitized image contains no patient data, no secrets, and no Tailscale identity
- [ ] Two Pis flashed from the same image come up with distinct host keys and machine-ids
- [ ] First boot with an empty auth key file leaves `aptlog-firstrun` armed to retry
- [ ] No real patient data or credentials in git history

---

## 7. Explicitly dropped

Recorded so these are not rediscovered and re-proposed.

**Dedicated GPS receiver (u-blox HAT, `gpsd`, USB puck).** The phone has its own GPS and
sits in the same room as the controller, so a second receiver adds a part and a failure
mode without adding evidence. Indoors it would also perform no better than the phone.
`GpsdLocationSource` is not to be built — REQ-5.7 defines the two implementations.

**Compute Module 5.** Its eMMC is more durable than an SD card, but flashing requires an
nRPIBOOT jumper shunt the dev kit does not ship, and eMMC variants cannot boot from SD —
so without that part there is no way to flash it and no fallback. Cost an afternoon on
site. See PI_SETUP.md.

**Emulators** (AVD, Genymotion, redroid). Healthcare/EVV apps refuse to run under Play
Integrity on them, and an emulator has no real GPS or wifi association, so it cannot
produce any of the REQ-5 signals.

**Docker.** ARCHITECTURE.md §6.

**Full-disk encryption.** Directly incompatible with the requirement that a
non-technical person can power-cycle the unit and have it recover unattended — FDE needs
a passphrase at boot and a Pi has no TPM-sealed unlock that survives a cold start with
nobody present. Mitigated by storing identifiers rather than names. PI_SETUP.md records
this as a conscious acceptance.

**Cloud-hosted emulator POC.** No `/dev/kvm` in the target container, and a cloud host is
not at the building, so it could not exercise REQ-5 at all.
