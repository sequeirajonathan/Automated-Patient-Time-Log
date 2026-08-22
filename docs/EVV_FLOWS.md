# EVV flows, per app

What each app requires to get from a cold start to a patient's check-in control, walked
on the real device rather than assumed. REQ-1's argument applies here too: none of this
is worth automating until it has been observed once.

**No patient appears in this document.** Names, addresses and phone numbers are on every
one of these screens and none of them belong in a repository. What is written down is
structure — resource ids, control captions, the words a status is reported in — which is
what a macro needs and what a reader six months from now cannot re-derive.

**The first pass pressed nothing.** Walking to a screen is reading; pressing the button
writes a record asserting that a caregiver was at somebody's home. That held until
2026-08-21, when HHAeXchange+'s check-in was pressed deliberately, on a real visit, with
the owner watching and confirming each step — because it was the last thing standing
between that app and arm-and-fire, and it could not be learned any other way. See "The
check-in, walked at last" below, and "The question in front of arm-and-fire" at the
bottom.

---

## Mobile Caregiver+ — `com.tellus.evv.v2`

Walked 2026-08-20. Signed in already (the `mobile_caregiver_pin` macro); the passcode
only unlocks the app, and an expired *server* session falls back to username and password
(see `secrets.py`).

### Getting to a patient

One activity does everything: `com.tellus.evv.activities.DashboardActivity` is the week
list *and* the visit detail. `feed.py`'s atlas already notes this about the app, and it
means "which screen am I on" must be answered from the page's own shape, never from the
activity name.

The week list is a run of rows with stable ids:

```
visits_event<D>_title     the day header      text/content-desc "jue, ago 20"
visits_event<D>_<N>       one visit, clickable, no text of its own
```

`<D>` counts days down the visible week, `<N>` counts visits within the day. **Do not
navigate by these numbers.** They are positions, they shift when a week has a different
shape, and REQ-4 already forbids locating a patient by index.

Navigate by the row's `content-desc`, which carries everything needed:

```
La visita está programada para <PATIENT> en <weekday>, <D> de <month> de <year>
de <H:MM AM> a <H:MM PM> y su estado es <STATUS>
```

That single string gives the patient, the date, **the app's own scheduled window**, and
the visit's state. Two consequences worth taking seriously:

- **The app knows what the AGENCY scheduled.** That is not the same thing as knowing
  where the caregiver will be, and on this round the two differ. See "Reconciling times"
  below before treating either as the last word.
- **State is machine-readable**, so a macro can tell "not yet" from "already done"
  without inferring it from which buttons are drawn.

### Status vocabulary

Observed on one week of real data, Spanish locale:

| `su estado es …` | Means |
|---|---|
| `Sin empezar` | not started, and not yet late |
| `Completada` | done |
| `Completadas, Tarde` | done, but started or finished outside the window |
| `No Empezadas, Tarde` | never started and the window has passed |
| `Perdida` | missed |

The last three are the ones this project exists to prevent, and they were all present in
a single week of live data — this is not a hypothetical failure mode.

### The visit detail

Tapping a row swaps the same activity to `Detalles de la visita`. What is on it depends
entirely on the visit's state:

**Not started** — the service code, the address, the phone number, and two controls
side by side at the foot of the page:

```
Cancelar Visita     bounds [6,1507][354,1532]      LEFT
Comenzar Visita     bounds [360,1507][714,1532]    RIGHT
```

**`Comenzar Visita` is the EVV check-in.** `Cancelar Visita` is its immediate left-hand
neighbour, and cancelling a visit is not something to do by accident.

Both are **non-clickable `View`s carrying only a `content-desc`** — the caption is not on
the thing that receives the tap. This is the same shape as the Play Store's Update
button, and `macros._store_update_button` already solves it: find the node whose words
match, then take the smallest enclosing clickable box. Reusing that is not a
convenience, it is the difference between starting a visit and cancelling one.

**Completed** — neither control. `Servicio Completada` instead, with the times actually
recorded:

```
Hora de inicio real - 9:41:10 AM
Hora de finalización real - 11:41:14 AM
```

So "did the check-in work" has an honest answer available afterwards: the real start
time appears and the buttons go away. That is a much better verification than "the
screen changed", and REQ-4's *verify after acting* requires it.

`Agregar nota` and `Regresar` (`main_back_button`) are present in both states.

### Load time

Not yet measured across a cold start. The arm lead stays at the default until it is.

---

## HHAeXchange+ (Exchange+) — `com.hhaexchange.uma`

Walked 2026-08-20 as far as the schedule; the check-in walked 2026-08-21 — see "The
check-in, walked at last" at the end of this section.

### The two screens before any patient

`OnboardingActivity` holds the **agency picker**: "Seleccionar un proveedor" and one row
per agency on the account. `feed.screen_for` already calls this `agency` rather than
`startup`, from the words on the page — the activity cannot tell the two apart.

Choosing one moves to `HomeActivity`, and this is where the app's reputation for being
slow comes from. Measured: the **activity** swaps within five seconds and the schedule
is not on it. The list arrives later. Nothing should read this screen — or decide the
app is broken — until the visit rows are actually present.

### The schedule

`HomeActivity`, `screen_for` → `home`. Several days at once, each under a date heading,
each visit a collapsed accordion ("Contraído"). Known controls:

```
schedule_screen_visit_search              find a patient by name
schedule_screen_create_unscheduled_visit  "Iniciar visita no programada"
help_button
```

A bottom bar carries **Programación** / **Pacientes** / **Menú**.

Each visit row prints the patient and the app's own scheduled window, so — as with
Mobile Caregiver+ — the times can be read rather than assumed, and a patient can be
found by name rather than by position. `macros.EXPAND_APPS` already opens the
accordions.

### Back leaves the app, and that is not a metaphor

Walked live: **Back from the schedule landed in the Play Store**, because that was the
app used before it. This is the reported complaint reproduced exactly — "the back button
even takes me to the previous app I was on" — and it is Android popping the task stack,
not the app misbehaving.

### The Custom Tab counts as the app

HHAeXchange+ signs in through a **Chrome Custom Tab**, so during that form the
foreground package is `com.android.chrome` while the app is, to anyone holding the
phone, still the app. Resuming the app can land straight back on a pending tab.

`macros.WEB_FLOW_HOST` records this, and `_app_home` treats Chrome as inside the app for
this package. Found the hard way: without it the walk read Chrome as "you have left",
activated the app — which resumed onto the same tab — and reported success from inside
a browser.

### An expanded card, and what a recorded check-in looks like

A card that is open ("Expandido" rather than "Contraído") on a visit already under way
shows:

```
Registros de entrada de EVV 8:05 p. m.     the check-in, with the time it was taken
La visita está en curso                    the state, in words
Detalles del paciente                      schedule_screen_patient_details
Continuar visitando                        schedule_screen_continue_shift
```

So this app, like Mobile Caregiver+, reports the **actual** recorded time after the
fact — which is what makes "did the check-in work" answerable rather than inferred.

### The check-in, walked at last

**Walked 2026-08-21 at 20:05**, four minutes before a real visit, with the owner
watching and confirming each press. This is the entry that held this app out of
`autoentry.SUPPORTED` for months, because its control had only ever been seen on a visit
already under way and a live agency record is not a thing to experiment on.

It is **two presses on the landing screen**, and nothing else:

1. **`Registro de entrada de EVV`** — a full-width primary button on the visit's own
   card. Programación lists the day's visits **already expanded**, each with its own
   button (`…:id/schedule_screen_clock_in`). Future days are collapsed rows with a
   chevron and no button.
2. **`Continuar`** (`…:id/map_screen_gps_continue_button`) on the screen that opens:
   `Verificación electrónica de visitas`, tabs **GPS** and **Dispositivo FOB**, a Google
   map with a `PIN de paciente` and a `PIN de profesional sanitario/a`. GPS is the tab
   that opens and the one to use — the owner's standing instruction, "GPS always"; the
   FOB reader is hardware this project cannot hold.

Afterwards the visit screen carries the patient, the address, the window, and a banner:
**"Llamada EVV en H:MM p. m. pendiente de aprobación de la oficina"** — so the record is
written and awaiting the office. The button becomes **`Registro de salida de EVV`**,
which is the check-out and stays hers.

**NO AGENCY HAS TO BE CHOSEN.** Confirmed by the owner for every patient in this app,
"regardless of agency selected". The agency picker is a filter for reading, not a step in
checking in — so `uma_agency_for` stays a button on the console and is not wired into the
fire.

**AND NO VISIT DETAIL HAS TO BE OPENED**, which is the difference from the other two
apps: their control lives one screen in, this one is on the list.

#### Which card, when a patient has two

A patient whose evening is written as two entries has **two cards, each with its own
button**, and pressing the wrong one records the wrong half. They are told apart by the
hours printed on the card — and those are the **agency's** window, not ours: the cards
read `8:00 p. m. - 9:00 p. m.` and `9:00 p. m. - 10:00 p. m.` where the schedule says
8:05 and 9:05, because the five minutes are the travel buffer.

So `_uma_pick` matches **nearest, not equal**, within twenty minutes, and refuses on a
tie, on nothing close enough, and on two cards with no time to tell them apart. A
check-in on the wrong half is worse than one not made.

#### Two things seen and not explained

* **The two map pins were about eight miles apart** — the caregiver's near Opa-Locka, the
  patient's at her real address in North Miami — and the app **accepted the check-in
  anyway**. So this GPS check does not appear to be enforced at submission. Do not build
  on that.
* A **stitched** capture of the map repeats the fixed `Continuar` footer at every scroll
  step, on a screen that does not scroll. The walk takes the step-0 copy and refuses if
  it cannot find exactly one.

#### Still to observe

The status vocabulary for a missed or late visit, and what the office's approval of a
pending call looks like from the phone.

---

## inMyTeam — `com.inmyteam.inmyteam`

Walked 2026-08-20, signed in already (`open_inmyteam`; the sign-in ends in a text
message, so the open-only macro is the right one for a walk).

**This app is in English on this device** while the other two are in Spanish. Any word
matching here has to allow for both anyway, but the default is not the same.

### Which screen actually knows whether a visit happened

**Read this before trusting anything this app says about whether a visit happened.
Three of its screens answer that question and only one of them is right.**

`My Work` → **Checks** is the record. Everything else is a partial view of it.

Established live on 2026-08-21, on one visit that ended up carrying four events —
the caregiver's real `Check in 05:00 AM` and `Check out 06:00 AM` from her own phone,
and two accidental check-ins made from this device at `09:54` and `10:00`:

| Screen | What it showed | What it is |
| --- | --- | --- |
| `My Work` → **Checks**, dated to the day | all four events, as plain `TextView` text | **the server's record** |
| Visit Detail, "Your activity on this patient" | only the two made **on this device** | a **per-device** log |
| Visit Detail, before this device had any | *"No check in and check out data has been recorded"* | the same per-device log, empty |
| The list card | two check marks | the server, but drawn as pixels |
| `My Work` → **Visits**, same day, same range | nothing at all | unexplained; do not rely on it |
| `Past Visits` | *"Sorry, there is no information"* | *past days*, not *completed visits* |

**The Visit Detail's activity list is device-local, and that is the trap.** It is not
a stale render: it still omitted the caregiver's two events after the app's own
Refresh and after a **force-stop and cold relaunch**, at the same moment `My Work` →
Checks listed all four. A device that has never checked this patient in will say
nothing was ever recorded, however much was.

Two things follow, and the second is the one with teeth:

* **Never conclude "nothing was recorded" from the Visit Detail**, and never go hunting
  for an account mismatch on the strength of it. This document previously drew exactly
  that conclusion from exactly that line and it was wrong. The signed-in identity is a
  bare number with no name set (`INMYTEAM_PHONE`, in the drawer header and My Profile),
  the caregiver works **the same account** from her own phone, and her check-in was
  reachable from this device the whole time — one screen further in.
* **A fire can double-enter, and has.** `_evv_entry` looks for the `Check in` control
  and presses it, and this app draws that control on a visit already checked in *and
  checked out*, accepts the press, and answers `Success`. That is how one 05:00–06:00
  visit came to hold two extra check-ins four hours after it ended.

The guard is available and it is text: walk to `My Work`, set both date fields to the
day, press `SEARCH`, open **Checks**, and read the lines under the patient. They are
ordinary `TextView`s reading `Check in HH:MM AM` / `Check out HH:MM AM` — no pixels
involved. A visit whose patient already carries a `Check in` for the day must not be
fired.

**What to tell the caregiver:** her history is under `My Work` → **Checks**, not
`My Work` → Visits and not `Past Visits`. Both of those read empty for work she has
genuinely done, which is why she reports having no record of her own day.

### The check marks are pixels, not nodes

The visits list is a `ComposeView`, and its card publishes five children: the agency
avatar and four `TextView`s (agency, `HH:MM AM | HH:MM AM`, patient, address). The two
check marks and the note icon at the card's right edge are **not in the accessibility
tree at all** — no node, no resource-id, no content-desc, nothing at their bounds.
Confirmed against the raw tree, not just the reflow.

This is not the case `screenview.IMAGE_MARKS` handles. That one works because the
**legacy** HHAeXchange list draws its ticks as real `ImageView`s under `imgstarttime`
and `imgendtime`, which is exactly what makes them nameable. inMyTeam gives a reader
nothing to hold, so the portal cannot show the caregiver whether a visit is done from
this screen.

That is a rendering gap, not a blocker: the same fact is written in words one screen
away, under `My Work` → Checks. Read it there rather than reaching for the pixels.

Anything that changes this has to come off the screenshot, which runs straight into
the decision recorded in "Remove pixel dependencies before the new device arrives".
It is a real trade, not an oversight.

### One activity again

`com.inmyteam.inmyteam.view.activities.MainActivity` is the visits home, every bucket
list, and the visit detail. Its atlas entry (`feed.ACTIVITY_SCREENS`) now names it
`home`, which it had never had.

### The visits home

Four buckets, each with its own counts — **Today**, **Tomorrow**, **Next**, **Past** —
plus `Filter by Agencies…` (an EditText), the nav drawer, and `notification_history`.
The bottom bar is `assigned` (My patients), `open`, `chat_log_fragment`, `myTripsFragment`.

### A bucket list

Titled "Today Visits" / "Tomorrow Visits", with a **Refresh** control beside the title,
and one card per visit carrying the agency, `HH:MM AM | HH:MM AM`, the patient, the
address and the weekday and date. The card is the only clickable thing on the row.

### The visit detail

Tabs **Visits** / **Plan of care**, "More Patient info +", then the patient, the date as
`YYYY-M-D`, `HH:MM AM | HH:MM AM PCA (1 H)`, and the address. Below that, the state:

```
No check in and check out data has been recorded
```

**And then it depends on the day.** This is the finding that matters:

| Visit is… | What the detail shows |
|---|---|
| today | `Check in` and `Note & Check out` |
| not today | `This visit is not scheduled for today`, and neither control |

**inMyTeam gates the action to the scheduled day.** A macro cannot arm this app by
walking to the check-in control the night before — the control is not drawn until the
day arrives.

Both captions are **non-clickable `TextView`s inside a clickable `View`**, the same shape
as the other two apps:

```
"Check in"            TextView [199,1522][247,1537]   →  clickable View [172,1510][274,1549]
"Note & \nCheck out"  TextView [467,1515][528,1543]   →  clickable View [446,1508][548,1550]
```

Note the newline inside the second caption (`Note &` then `Check out`). Match on a
substring, never on the whole string.

Three small square Buttons sit stacked at the left margin of the detail; on a future
visit two are `enabled="false"`. They look like a timeline of the visit's steps.
**Deliberately not pressed** — identifying them by pressing one on a live agency app for
a real patient is not a thing to do to find out.

### Switching agencies in HHAeXchange+

Walked because the round crosses between two providers twice a day. Four taps, and only
two of them name themselves:

```
Menú (bottom bar, by its words — no id)
  → Agencias                        menu_screen_connections
    → Cambiar proveedor activo      agency_configuration_screen_change_connection_button
      → the provider picker         OnboardingActivity
```

The Agencias page also **marks which provider is active** ("Activa"), which is what lets
`_uma_agency_for` skip the whole walk when the wanted one is already in use — switching
to where you already are costs a full schedule reload, the slowest thing this app does.

`macros.uma_agency` opens the picker and stops. `macros.uma_agency_for` takes the agency
name from the visit row that was pressed and chooses it. The argument is matched against
the rows the app is drawing, so it cannot name a control that is not on screen; an agency
that is not on the account fails rather than pressing the other one.

## App lifecycle — what switching actually does

Three questions were asked directly, and the answers are already true of the code
rather than things that had to be built. Written down because they are not obvious
from the outside, and "what if it closes the app" is a reasonable fear to have.

**Switching apps does not close anything.** A tile calls `driver.activate_app`, which
brings an existing task to the front with its state intact — the same thing tapping an
icon on the phone does. Nothing is force-stopped. The only two things that force-stop
are the **Close** and **Restart** buttons, which exist for a wedged app and say so.

**A live session is not signed in again.** Every sign-in macro checks for an actual
credential screen before it types anything:

- HHAeXchange+ looks for its auth activity, its sign-in form (which lives in a Chrome
  Custom Tab, so the *package* changes rather than the activity), and its own expiry
  dialog — and refuses to conclude "signed in" from an empty accessibility tree, which
  is what a freshly woken Compose UI serves for several seconds.
- Mobile Caregiver+ distinguishes its passcode keypad from its server-session expiry,
  because the passcode cannot answer the second one.
- inMyTeam stops if the app is past the walk.

So switching to an app that is already signed in costs an activate and a look, not a
sign-in. Pressing the tile of the app **already in front** does not even do that — the
front end treats it as a view switch.

**An app kept open is an app kept on the day it was opened.** The other side of "switching
apps does not close anything": a process that has been in front since yesterday is still
in front today, holding the list it fetched then. `_bring_up` returns the moment the right
package is in front and does not ask which day that front is showing, so the walk can
arrive at a correctly-named screen — right title, right layout — and every check after it,
the day check included, reads stale data.

Pressing the app's own **Refresh** does not settle this. It re-renders what the app
already holds; on inMyTeam it was pressed twice against a record that was genuinely
absent and changed nothing either time. A **force-stop and cold relaunch** is the only
lever here that an app cannot ignore, and it is what `_freshen` does:

- The **lead window always starts from cold** (`max_age=0`). Getting ready fifteen
  minutes early is what that window is for, so the restart is free there.
- The **fire starts from cold only if nothing else has** (`max_age=FRESH_FOR`). When the
  lead window ran, the fire pays nothing; when it failed — or the entry is fired by hand
  from the portal — this is what stops the press landing on yesterday's list.

`FRESH_FOR` is process-local on purpose. A Runner that restarted between getting ready
and firing has no business claiming a fetch it did not watch, and pays for another.

**Back stays inside the app.** Back from an app's root pops Android's task stack into
whatever was under it — the launcher, or another care app — which is how pressing Back
in Exchange+ landed in Mobile Caregiver+. The portal now treats "the front app changed"
as the signal (not "this is the launcher"), and puts her straight back: to her, that
Back did nothing. Leaving an app is **Home's** job. The **app-home** control is the
third move — back to the app's *own* first page, by pressing Back and looking until the
atlas recognises a front page, bounded, and never out of the app.

## Reconciling times — and who is the authority

An earlier draft of this document said that where an app publishes its own scheduled
window, that window is the authority. **That was wrong, and it was corrected by the
person who does the round.** It is written down here because the mistake is an easy one
to make twice.

Two different things are being called "the schedule", and only one of them is a fact
about a caregiver's day:

- **The app is authoritative about what the agency scheduled.** That is the record the
  agency bills from and marks lateness against, and no amount of local config changes it.
- **The caregiver is authoritative about where she actually is.** Her round is one
  person driving between homes, and it is the thing the machine has to fit into.

They can disagree, and on this round they do. Two of the apps showed windows an hour
earlier than the written schedule for two patients on the same two days — read live, on
future visits, so not a stale render. Taking those at face value produced a Thursday and
Friday in which two visits overlapped by fifty minutes, at two different homes, in two
different apps.

**The overlap was the tell.** An agency scheduling system that does not know about a
caregiver's other agency cannot see the conflict it is creating; a person looking at
their own week can see it immediately. The written round was right and the apps were
carrying a scheduling error.

So: `/etc/aptlog/schedule.json` records **the round as the caregiver works it**. Where an
app disagrees, that is worth knowing and worth raising with the agency — it is not a
reason to move her.

### What that costs, and it is not nothing

Where the agency has a visit on the books at a time she is not there, the app will keep
recording her as late. That is not hypothetical either: on the day this was walked, the
patient whose agency window had moved an hour earlier was marked **`Completadas,
Tarde`** — done, outside the window — for exactly this reason.

Nothing in this project can fix that, and nothing in it should try: an EVV record that
quietly reported an on-time arrival at a time she was somewhere else would be worse than
one that reports her late. It is a conversation with the agency about their own
schedule.

### The travel buffer, checked against a real record

The written round gives one evening patient a start five minutes past the hour, and the
app's nominal for that visit is on the hour. The card for a visit already under way
showed the EVV check-in recorded at **five past** — so the stored time and the real
behaviour agree, and the rule reproduces it.

Run over the whole week as written, exactly one buffer is computed: a morning visit that
begins the minute the previous patient's ends. Every other gap is already in the round.
That is the rule behaving as intended — "no gap at all", not "always five minutes".

A contradiction in the original written schedule was also settled by the same
conversation: one evening patient has no Friday visit, so nothing precedes the visit
after her, and Friday needs no buffer. The verification table that implied otherwise was
loose; the schedule table was right.

## The question in front of arm-and-fire, and its answer

Everything above is navigation, and navigation is safe: the portal already drives these
apps and reading a screen commits nothing.

Pressing `Comenzar Visita` is different in kind. It writes a record asserting that a
caregiver was at a patient's home at a moment in time. The phone this controller drives
is tethered by USB to a Pi that does not move, and the caregiver does — so REQ-5's
network anchors attest to *the house the Pi is in*, identically, for every patient on the
round. That gate would return the same strong PASS whichever address she was at, and a
gate that cannot fail is not a gate.

**The account holder settled it**, and the resolution is now REQ-5.9:

> "If armed you can assume my sister is already taking care of the patient. The issue
> wasn't made clear — she's already there at that time, she just doesn't have the time to
> do the entry because she has to start right away. So that requirement had it wrong in
> the case that she wasn't present. She is. Arming is a commitment."

So the problem was never "she is not there yet". It is "she is there, with her hands
full, and the entry has to land on the minute the agency expects." The presence claim
comes from a person, in advance, and **arming is the act that makes it** — recorded with
who threw the switch and when, and stamped into the fire's ledger entry as `attested` so
it can never be read as something a machine observed.

### What fires today, and what does not

| App | Entry | Why |
|---|---|---|
| Mobile Caregiver+ | **fires** | `Comenzar Visita` walked, with a machine-readable confirmation afterwards |
| inMyTeam | **fires** | `Check in` walked; the lead-window walk gets the control drawn (below) |
| HHAeXchange+ | **refuses** | its control has only ever been seen on a visit already under way |

The refusal is by name (`autoentry.UNSUPPORTED_REASON`) and the arming page dims those
rows and says why on their face — a switch that cannot fire must never look like one that
can.

**No check-OUT fires on any app.** Mobile Caregiver+ was only ever seen in its
not-started and completed states, so the control that ends a running visit is unobserved;
inMyTeam's is `Note & Check out`, which opens the note and the signature flow — a screen
where the caregiver signs, and not one a timer should be pressing on her behalf.

### inMyTeam and the day it gates on

inMyTeam draws `Check in` only on the scheduled day; the evening before shows "This visit
is not scheduled for today" and no control at all. That looked like a reason inMyTeam
could not be armed the night before.

It is not, because **arming is not the walk.** Arming is a standing decision about a
recurring block, made whenever she likes. The walk belongs on the day, inside the lead
window, and that is what `autoentry.preparing` and the `evv_prepare` macro do: open the
app, find the patient, open the visit, stop. By the time the entry is due the app is
open, signed in and on the patient's own detail, so the fire is one press rather than a
cold start, a login and a search against the clock.

That helps every app. It is the only thing that makes inMyTeam work at all.

### The three refusals that remain

- **Late is not caught up.** Past a five-minute grace the fire is dropped and announced,
  because the record would claim an arrival minute that has already passed.
- **Nothing fires twice.** The occurrence is spent *before* the phone is touched, so a
  crash mid-press cannot leave the slot open for the next tick.
- **A fire that cannot complete fails closed** (REQ-5.5) and alerts, carrying no patient,
  visit or time — those notices land on a lock screen and a public relay.
