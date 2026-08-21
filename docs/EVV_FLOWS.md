# EVV flows, per app

What each app requires to get from a cold start to a patient's check-in control, walked
on the real device rather than assumed. REQ-1's argument applies here too: none of this
is worth automating until it has been observed once.

**No patient appears in this document.** Names, addresses and phone numbers are on every
one of these screens and none of them belong in a repository. What is written down is
structure — resource ids, control captions, the words a status is reported in — which is
what a macro needs and what a reader six months from now cannot re-derive.

**Nothing in this pass pressed a check-in control.** Walking to the screen is reading;
pressing the button writes a record asserting that a caregiver was at somebody's home.
See "The question in front of arm-and-fire" at the bottom.

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

- **The app knows the times.** A config file is not the only source, and where the two
  disagree the app is the one the agency will bill from. See "Reconciling times" below.
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

Walked 2026-08-20 as far as the schedule. The check-in control itself is still
unobserved — see the end of this section.

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

### Still to observe

The navigation path from a visit row to the check-in control, the **`registro de
entrada`** button itself, whether a GPS confirmation precedes submission, and the status
vocabulary the rows use.

---

## inMyTeam — `com.inmyteam.inmyteam`

Not yet walked for check-in. Sign-in is the mapped part (phone number, then a texted
code — `sms.py`, `macros._inmyteam_login`). One patient is currently tracked on it.

Still to observe: everything downstream of sign-in.

---

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

**Back stays inside the app.** Back from an app's root pops Android's task stack into
whatever was under it — the launcher, or another care app — which is how pressing Back
in Exchange+ landed in Mobile Caregiver+. The portal now treats "the front app changed"
as the signal (not "this is the launcher"), and puts her straight back: to her, that
Back did nothing. Leaving an app is **Home's** job. The **app-home** control is the
third move — back to the app's *own* first page, by pressing Back and looking until the
atlas recognises a front page, bounded, and never out of the app.

## Reconciling times

The Mobile Caregiver+ walk turned up a discrepancy worth stating plainly, because it
would have made the automation an hour late twice a week.

For one patient the app's own schedule shows **two different windows in the same week** —
one time Monday through Wednesday and a different, earlier one on Thursday and Friday.
The written schedule this project was given has a single weekday time for that patient,
and treats a reported Thursday/Friday difference for *another* patient as a display
error that had since been corrected.

It is live in the app now, on a future visit that has not happened yet, so it is not a
stale render.

**Design consequence.** Where an app publishes its own scheduled window — Mobile
Caregiver+ does in every row's `content-desc`, HHAeXchange+ prints it on the row — that
is the authority, and a config file should be reconciled against it rather than trusted
over it. The config's job is the part no app knows: the travel buffer between two
different patients' homes, which exists nowhere but in the caregiver's day.

**Both apps were then read, and the written schedule was wrong about two people.** One
patient's Thursday/Friday window is an hour earlier than the rest of her week in one
app; another's is an hour earlier in the other app, and her Monday-to-Wednesday time was
five minutes out on every day besides. The written schedule had treated the first of
those as a display error that had since been corrected — a conclusion that rested on the
second patient's times being uniform, which they are not.

The device's `/etc/aptlog/schedule.json` was corrected from what the two apps say. The
travel buffer still falls out of the rule rather than being written in: on Saturday one
visit ends at 8:00 and the next is scheduled for 8:00, so it fires at 8:05.

**One conflict is left standing, because it is the agency's to resolve and not this
project's.** On Thursday and Friday two visits overlap by fifty minutes across two
different apps and two different homes. Both are printed by their own app. Nothing here
can make a caregiver be in two places, and inventing a resolution would hide it.

---

## The question in front of arm-and-fire

Everything above is navigation, and navigation is safe: the portal already drives these
apps and no macro commits a visit.

Pressing `Comenzar Visita` is different in kind. It writes a record asserting that a
caregiver was at a patient's home at a moment in time, and this project has already
taken a position on that in two places:

- **REQ-0** — must not write to a production EVV system while the presence gate is
  running on stub signals or while the transport mode is `dev`.
- **REQ-5** — refuse to act when presence cannot be verified, rather than assume.
- **§1 of REQUIREMENTS.md** — the automation produces a draft for an operator to confirm.
  It is not to be built or described as a system of record.

The phone this controller drives is tethered by USB to a Pi that does not move. The
caregiver does. So a timer that presses this button attests to a presence the machine did
not observe, and it would do so from a fixed location for visits at several different
addresses.

That is a decision for the person who owns the account and the liability, not something
to settle in a commit. Until it is settled, the scheduler's useful and uncontroversial
half is the one already built: knowing what is next, arming the app, and getting the
check-in screen in front of a human at the right minute.
