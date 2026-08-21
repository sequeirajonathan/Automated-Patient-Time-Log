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

Not yet walked for check-in. What is already known from earlier work in this repo:

- Multi-agency. The agency picker is mapped (`screens/agency.py`, and `feed.py` treats
  the picker as its own screen rather than as "startup").
- After an agency is chosen the app must be **left loading until today's schedule
  expands** before anything can be read.
- The schedule's visit cards fold their details behind an accordion, and `macros.py`
  already opens them (`EXPAND_APPS`).
- The check-in control is reported as **`registro de entrada`**.

Still to observe: the navigation path to a named patient, whether a GPS confirmation
step precedes submission, the status vocabulary, and load time from cold.

---

## inMyTeam — `com.inmyteam.inmyteam`

Not yet walked for check-in. Sign-in is the mapped part (phone number, then a texted
code — `sms.py`, `macros._inmyteam_login`). One patient is currently tracked on it.

Still to observe: everything downstream of sign-in.

---

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
Caregiver+ does, in every row's `content-desc` — that is the authority, and a config
file should be reconciled against it rather than trusted over it. The config's job is
the part no app knows: the travel buffer between two different patients' homes, which
exists nowhere but in the caregiver's day.

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
