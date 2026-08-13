# Automated Patient Time Log — POC Requirements

Status: **Phase 1 (POC)** — local machine + one real Android phone over USB.
Target production environment (Phase 2) is a Raspberry Pi driving the same phone.

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

### In scope for this POC
Everything needed to prove the mechanics work end to end on a local machine against a
real phone, using a non-production target (staging/sandbox account, or a stand-in app).

### Out of scope for this POC
- Raspberry Pi deployment and the GPS HAT (Phase 2)
- Any write to a **production** EVV/agency system — see REQ-0
- Multi-operator or multi-site support
- Real-time UI or dashboard

---

## 3. Architecture decisions (already settled — do not re-litigate)

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 | glue for secrets, scheduling, parsing |
| Driver | Appium 2 + `uiautomator2` driver | the mature Android equivalent of Playwright |
| Client | `Appium-Python-Client` | thin WebDriver wrapper |
| Device | **real phone over USB** (not emulator) | emulators fail Play Integrity; matches Phase 2 target |
| Runner | `pytest` + `pytest-rerunfailures` | fixtures for device lifecycle, free retries |
| Scheduler | APScheduler (POC) → systemd timers (Pi) | in-process is fine locally; systemd gives `OnFailure=` on the Pi |
| Secrets | `keyring` (OS keychain) behind a `SecretProvider` interface | nothing in the repo, nothing in `.env` |
| Store | SQLite + append-only JSONL audit | idempotency constraints + tamper-evident trail |
| Logging | `structlog` with a redaction processor | credentials and PHI must never reach a log sink |
| CLI | `typer` | subcommands for probe / run / report |

Emulator support via `budtmo/docker-android` is optional and secondary — useful only for
cheap selector iteration if and only if the target app tolerates running on one.

---

## 4. Functional requirements

### REQ-0 — Production write guard (blocking, applies to all phases)
The POC must **not** submit check-offs into a live agency/EVV system.

- Target must be a staging/sandbox account or a stand-in app.
- A `--production` flag must exist but hard-fail with an explanatory error unless a
  `POC_PRODUCTION_AUTHORIZED` config value is explicitly set, and must refuse entirely
  while `LocationSource` is a stub (REQ-5.4).
- Rationale: a cloud/laptop run is not at the building, so any location it attests to is
  not one it observed. See §1.1.

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
- Credentials resolved through a `SecretProvider` interface with a `KeyringProvider`
  implementation (OS keychain) for local use.
- Never accept a password as a CLI argument (leaks to `ps` and shell history).
- Detect the login screen at the start of every run and re-authenticate if present.
- Suppress screenshots while a password field has focus.

### REQ-4 — Patient check-off flow
- Locate the patient by **search-by-name**, never by list index or scroll position.
- **Idempotent**: a retry must not produce a duplicate check-off. Enforce with a UNIQUE
  constraint on `(patient_id, visit_date, scheduled_slot)` in SQLite.
- **Verify after acting**: confirm the UI reflects the check-off before recording success.
  A tap that appeared to land is not evidence it did.
- On ambiguity (multiple name matches, unexpected screen), abort and alert — never guess.

### REQ-5 — Location gate
1. `LocationSource` interface with two implementations: `StubLocationSource` (POC,
   scripted fixtures) and `GpsdLocationSource` (Phase 2, real receiver).
2. A check-off may proceed only if **all** hold:
   - 3D fix (`mode >= 3`)
   - `>= 6` satellites
   - horizontal accuracy `< 25 m`
   - haversine distance to site `< GEOFENCE_M` (default 100 m)
   - `N >= 3` consecutive qualifying fixes
3. **Fail closed.** If the gate fails, skip the action and alert. Never queue it to fire
   later from an unverified location.
4. The active `LocationSource` implementation must be recorded in every audit entry, so a
   stub-sourced record is never mistakable for an observed one.

### REQ-6 — Scheduler
- Load a shift schedule (patient, scheduled time, slot) from a local config file.
- Fire each check-off at its scheduled time, tz-aware; store UTC, display local; handle DST.
- Missed-run policy must be explicit and conservative: do not silently backfill.

### REQ-7 — Audit log
Append-only JSONL, one record per attempt, containing at minimum:

```
attempt_id, patient_id, scheduled_time_utc, observed_time_utc,
gate_result, location_source_type, fix{lat, lon, accuracy_m, sats, mode, timestamp},
action_taken, ui_verification_result, error, app_version
```

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

**Security / PHI.** Patient names and IDs are PHI. Encrypt the local store at rest; keep
the audit log outside any synced folder (iCloud/Dropbox/OneDrive); redact credentials and
patient identifiers from all log output via a `structlog` processor; never commit
schedules, fixtures, or audit output containing real patient data.

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
- [ ] Gate denies on a low-accuracy / too-few-satellites fixture
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

## 7. Phase 2 (not this POC)

Raspberry Pi + u-blox NEO-M9N HAT via `gpsd`, driving the same phone over USB. Swap
`StubLocationSource` → `GpsdLocationSource`, APScheduler → systemd timers with
`OnFailure=`. Add full-disk encryption and TPM/systemd-sealed credentials. The phone stays
on a charge-limited hub (~60–80%) to avoid battery swelling, with Play Store auto-update
disabled so an overnight app change cannot silently break every selector.
