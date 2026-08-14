# Raspberry Pi 5 — build and flash checklist

Hardware: **Raspberry Pi 5** booting from microSD.

Audience: whoever physically assembles the unit at the building. Follow in order.

Do the **Before you travel** section somewhere with good internet and a laptop. The
on-site steps then need no troubleshooting.

> **Why not the Compute Module.** An earlier revision of this document targeted the CM5
> Development Kit. The CM5's eMMC is more durable than an SD card, but flashing it
> requires putting the board into USB device mode with an **nRPIBOOT jumper shunt that
> the kit does not include** — and with no jumper there is no way to flash it and no
> fallback, because eMMC variants cannot boot from SD. The Pi 5 trades a wear-item card
> for a flashing process that works with any laptop and no special parts.

---

## 0. Before you travel

### 0.1 Flash the card

Use **Raspberry Pi Imager** (raspberrypi.com/software) on a laptop with a microSD reader.

- **Device:** Raspberry Pi 5
- **OS:** `Raspberry Pi OS Lite (64-bit)` — under *Raspberry Pi OS (other)*

Lite, not Desktop. No monitor is ever attached to this machine, a desktop session is one
more thing that can hang, and the GUI roughly triples card writes.

Before writing, open **⚙ / "Edit Settings"** and set all of the following. This is what
makes the Pi boot ready with nothing to configure on site:

| Setting | Value |
|---|---|
| Hostname | `aptlog` |
| Username | `apt` |
| Password | set one — see the note below |
| Wi-Fi | **skip — this machine runs on ethernet** |
| Locale / timezone | the building's timezone (matters — the scheduler uses it) |
| Enable SSH | ✅ |
| Public keys | paste **every** key that may need access, one per line |

**Set a password *and* the SSH keys, and leave password authentication enabled.** If
nobody on site has a keyboard, password-over-SSH is the only way back in when key auth
fails. Harden it later over Tailscale — `PasswordAuthentication no` in
`/etc/ssh/sshd_config` — once you know key auth works.

Paste the public key of **whoever is doing the build**, not only the developer's. Someone
holding an image they cannot log into is the most common way this goes wrong.

### 0.2 Generate a Tailscale auth key

At `login.tailscale.com` → Settings → Keys → **Generate auth key**:

- Reusable: no
- Ephemeral: no
- Tags: `tag:aptlog`
- Expiry: 90 days

Copy it. It goes into the first-boot script below and lets the Pi join the network with
no interactive login at the building.

Generate a fresh key rather than reusing one. Auth keys are single-use by default, and
the tag is what allows ACLs to scope what this device can reach.

**Check the prefix.** An auth key starts with `tskey-auth-`. A key starting with
`tskey-api-` is an API access token — a different object that manages the tailnet and
cannot enrol a device. Generating the wrong one is easy and only shows up as a failure at
the building.

### 0.3 Stage the first-boot script

After Imager finishes, the card remounts as `bootfs`. Copy
[`scripts/firstboot.sh`](../scripts/firstboot.sh) to the root of that partition and put
the auth key in it where marked.

### 0.4 Parts

The Pi 5 board on its own is not enough. Either buy a kit that covers the first four rows
below, or assemble them individually.

| Item | Requirement |
|---|---|
| Raspberry Pi 5 | 4 GB is sufficient; 8 GB gives debugging headroom |
| **27 W USB-C PD power supply** | **not optional — see below** |
| Active Cooler *or* a case with a fan | one or the other, never both |
| Case | must fit whichever cooling you chose |
| microSD, 32 GB or larger | **high-endurance** (Samsung PRO Endurance, SanDisk Max Endurance) |
| microSD reader | skip if the laptop has a slot |
| RTC battery | ML2020 / LIR2025 with a **2-pin JST connector** — *not* a bare CR2032 |
| Powered USB hub | self-powered, ideally `uhubctl`-capable |
| Ethernet cable, Cat 6 | measure the run to the gateway first |
| Phone data cable | USB-A to the phone's connector — **data, not charge-only** |

**The 27 W supply is a functional requirement, not a recommendation.** The Pi 5 limits its
four USB ports to 600 mA combined by default, raising that to 1.6 A only when it detects
an official USB-C PD supply. Underpower it and the phone misbehaves in ways that look
exactly like software faults.

**The RTC battery is a connectorized cell, not a coin-cell socket.** A loose CR2032 has
nowhere to plug in on a Pi 5.

**High-endurance card, not a standard one.** This machine logs continuously; ordinary
cards fail within 6–18 months of that, in a room with nobody in it. Endurance cards are
built for dashcam-style continuous writing. `firstboot.sh` also caps the journal to
volatile storage to cut writes further.

---

## 1. Assemble

1. Cooling — **pick one, they are alternatives**:
   - The **Active Cooler**, in a case with clearance for it, or
   - a case with its own integrated fan.

   The official Pi 5 case's built-in fan and the Active Cooler occupy the same space.
   Fit whichever you chose before the board goes in the case; the fan connector is
   awkward to reach afterwards.
2. Insert the **microSD**.
3. Connect the **RTC battery** to its 2-pin JST connector and stick it down with the
   adhesive pad. It keeps the clock across power cuts, which matters here — a unit that
   reboots at 3am shouldn't run on a wrong clock and fire the whole day's schedule at
   once.
4. Board into the case.
5. **Ethernet** from the Pi to a LAN port on the Xfinity gateway.
6. **Powered USB hub** into its own wall outlet, then hub → one of the Pi's **USB 3.0**
   ports (the blue ones).
7. **Phone → the powered hub**, never the Pi directly. The Pi's four ports share 1.6 A
   total, well under what a phone draws. Powering the phone from the Pi causes random
   disconnects and slow battery drain that read as software bugs.
8. Pi power last, using the **27 W USB-C supply**.

---

## 2. Prepare the phone

Do this once, with the phone in hand.

0. **Set the phone's language to the caregiver's language** (Spanish for the Florida
   deployment) in Settings → System → Languages.

   This is a human step and cannot be automated. The agency app's language picker is a
   custom-drawn view with no accessibility exposure — nothing to select by name and no
   way to read back what is selected — so the automation can only verify which language
   the app is already rendering in, not change it. Getting this wrong means the caregiver
   reads her visit records in the wrong language.

   Setting it also requires no root, unlike changing the locale over adb.
1. Settings → About → tap Build number 7× → Developer options.
2. Enable **USB debugging**.
3. Enable **Stay awake while charging**.
4. Screen timeout → longest available.
5. Set screen lock to a **PIN** (not pattern, not biometric-only). The unlock is scripted
   with `input text`, which needs a numeric PIN. Record the PIN in the password manager —
   it goes in the same secret store as the app credentials.
6. Disable auto-update in the Play Store → Settings → Network preferences → Auto-update
   apps → **Don't auto-update**. An overnight app update silently breaks every selector.
7. Battery protection: if the phone has "Protect battery" / "Optimised charging"
   (Samsung, Sony, OnePlus), turn it on to cap around 85%. A phone held at 100% on a
   charger 24/7 swells within a year.
8. Plug into the hub and accept the **"Allow USB debugging?"** prompt — tick *Always
   allow from this computer*. If this is missed, `adb devices` shows `unauthorized`
   forever and nothing works.

---

## 3. First boot

Power on and give it about a minute.

The Pi is not on Tailscale yet, so find it on the building LAN. If a TV is available,
HDMI shows the login prompt with the IP address on it. Otherwise check the Xfinity admin
page for a device named `aptlog`, or try the hostname directly:

```bash
ssh apt@aptlog.local            # from a laptop on the same network
```

Then run the bootstrap once. It installs everything, joins the tailnet, and enables the
services:

```bash
sudo bash /boot/firmware/firstboot.sh
sudo reboot
```

Expect 10–15 minutes; it compiles nothing but pulls a lot. It is safe to re-run if it
fails partway.

After the reboot the Pi is on Tailscale and reachable from anywhere as `ssh apt@aptlog`.

---

## 4. Verify before leaving

Run these on the Pi. **All five must pass** — do not leave the building until they do.

```bash
# 1. phone is attached over USB (not TCP) and authorised
adb devices -l
#    want: one entry, state "device", NOT "unauthorized" and NOT a 192.168.x.x:5555 entry

# 2. per-port power control, for automated recovery
sudo uhubctl
#    want: the hub listed with "Current status" per port
#    if absent, see OPERATIONS.md §1.1 for the fallback

# 3. network anchor
ip neigh show default          # gateway MAC — record it
tailscale ip -4                # Tailscale address — record it

# 4. services came up on their own
systemctl status aptlog-appium aptlog-agent --no-pager

# 5. the real test: pull the plug
```

For (5): **unplug the Pi's power, wait ten seconds, plug it back in.** Wait three
minutes, then re-run 1 through 4. Everything must return with zero interaction.

That power-cycle is the entire recovery procedure for a non-technical person on site, so
it has to work before you leave the building.

### 4.1 Disable node key expiry — do this the same day

In the Tailscale admin console: **Machines → `aptlog` → disable key expiry**.

This is separate from the auth key's expiry, and conflating the two is the usual mistake.
The auth key expiring is harmless once it has enrolled the device. The **node** key
expires on its own schedule (180 days by default), and when it does, the Pi is logged out
of the tailnet and needs interactive re-authentication.

For a device in someone else's home with no console and no technical user nearby, that
means remote access disappears roughly six months after installation, with no warning and
no way back in short of another site visit.

---

## 5. Leave behind

Tape a card to the Pi:

```
  APT LOG — if something seems wrong

  1. Unplug the small black power cord.
  2. Count to ten.
  3. Plug it back in.
  4. Wait five minutes.

  Nothing else needs to be done.
  Do not unplug the other cables.

  Questions: <developer name and number>
```

Also physically label the phone cable and the ethernet cable so they don't get tidied
away, and tape over the hub's per-port switches — a bumped switch disconnects the phone
and looks identical to a software failure.

---

## Known constraint: disk encryption

The requirement that a non-technical person can power-cycle the Pi and have it recover
unattended **rules out full-disk encryption**. FDE needs a passphrase at boot; there is no
TPM-sealed unlock path on a Pi that survives a cold start with nobody present.

The mitigation is to keep little worth stealing on the device:

- Store patient **identifiers**, never names, in the local database and the audit log.
  Name resolution stays in the app, on the phone.
- Keep the app credential and the phone PIN in a file readable only by the service user;
  this is obfuscation against casual access, not protection against someone who takes the
  card.
- Treat physical access to the SD card as full compromise, and rotate the app password if
  the hardware is ever lost or replaced. A card is easier to remove and read than soldered
  storage would be, which makes this a real consideration rather than a theoretical one.

The device lives in a private residence rather than a public area, which is the main
control here. That is a real limitation, not a solved problem — it should be a conscious
acceptance rather than an assumption.
