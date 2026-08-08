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
- [ ] No real patient data or credentials in git history

---

## 7. Phase 2 (not this POC)

Raspberry Pi + u-blox NEO-M9N HAT via `gpsd`, driving the same phone over USB. Swap
`StubLocationSource` → `GpsdLocationSource`, APScheduler → systemd timers with
`OnFailure=`. Add full-disk encryption and TPM/systemd-sealed credentials. The phone stays
on a charge-limited hub (~60–80%) to avoid battery swelling, with Play Store auto-update
disabled so an overnight app change cannot silently break every selector.
