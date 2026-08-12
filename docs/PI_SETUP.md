# Compute Module 5 — build and flash checklist

Hardware: **Raspberry Pi Development Kit for Compute Module 5** (CM5104032 — wireless,
4 GB RAM, 32 GB eMMC) on the CM5 IO Board.

The eMMC is the reason this kit suits the job: no microSD to wear out under continuous
logging, which is the usual way an always-on Pi dies unattended.

Audience: whoever physically assembles the unit at the building. Follow in order.

Do the **Before you travel** section somewhere with good internet and a laptop. The
on-site steps then need no troubleshooting.

---

## 0. Before you travel

### 0.1 Flash the eMMC

There is no card to image — the CM5 has onboard eMMC, which is written over USB with the
board in device mode.

1. Fit the **nRPIBOOT jumper** on the IO Board (marked on the silkscreen).
2. Connect the board's **USB-C port** to the laptop with the bundled USB-A to USB-C
   cable. This is what that cable is for — it is **not** the phone cable.
3. Install and run `rpiboot`
   ([instructions](https://www.raspberrypi.com/documentation/computers/compute-module.html)).
   The eMMC then appears on the laptop as a mass-storage device.
4. Flash it with **Raspberry Pi Imager**, selecting the eMMC as the target:
   - **Device:** Raspberry Pi 5 (the CM5 shares the Pi 5's SoC)
   - **OS:** `Raspberry Pi OS Lite (64-bit)` — under *Raspberry Pi OS (other)*
5. **Remove the nRPIBOOT jumper afterwards**, or the board will keep booting into device
   mode instead of starting normally.

Lite, not Desktop. No monitor is ever attached to this machine, and a desktop session is
one more thing that can hang.

Before writing, open **⚙ / "Edit Settings"** and set all of the following. This is what
makes it boot ready with nothing to configure on site:

| Setting | Value |
|---|---|
| Hostname | `aptlog` |
| Username | `apt` |
| Password | set one, record it in a password manager |
| Wi-Fi | **skip — this machine runs on ethernet** |
| Locale / timezone | the building's timezone (matters — the scheduler uses it) |
| Enable SSH | ✅ **Use public-key authentication only** |
| Public key | paste the developer's SSH public key |

Password auth stays off. The account password is only for a keyboard-and-monitor
rescue, which should never be needed.

### 0.2 Generate a Tailscale auth key

At `login.tailscale.com` → Settings → Keys → **Generate auth key**:

- Reusable: no
- Ephemeral: no
- Tags: `tag:aptlog`
- Expiry: 90 days

Copy it. It goes into the first-boot script below and lets the Pi join the network with
no interactive login at the building.

### 0.3 Stage the first-boot script

While the eMMC is still mounted on the laptop from step 0.1, its boot partition appears
as `bootfs`. Copy [`scripts/firstboot.sh`](../scripts/firstboot.sh) to the root of that
partition and put the auth key in it where marked.

### 0.4 Buy the four things the kit doesn't include

| Item | Note |
|---|---|
| Powered USB hub | self-powered, ideally `uhubctl`-capable (e.g. Plugable USB3-HUB7BC) |
| Ethernet cable, Cat 6 | measure the run to the gateway |
| Phone data cable | USB-A to the phone's connector — **data, not charge-only** |
| CR2032 battery | the RTC socket is on the board; the cell is not in the kit |

No microSD and no card reader — the eMMC replaces both.

---

## 1. Assemble

1. Fit the **CM5 Cooler** to the module before anything else; the fan connector is
   awkward to reach later.
   **Note the conflict:** the cooler is not designed to be used with the IO Case lid.
   Pick one. For 24/7 operation choose the cooler and run without the lid, accepting more
   dust, and plan to blow it out periodically.
2. Seat the Compute Module on the IO Board, and fit the **antenna kit** — the wireless
   variant needs it for any wifi at all. Ethernet is the primary link, but wifi is a
   useful fallback for recovery.
3. Fit the **CR2032** in the RTC socket. It keeps the clock across power cuts, which
   matters here — a unit that reboots at 3am shouldn't run on a wrong clock and fire the
   whole day's schedule at once.
4. Board into the case.
5. **Ethernet** from the IO Board to a LAN port on the Xfinity gateway.
6. **Powered USB hub** into its own wall outlet, then hub → one of the board's two
   **USB 3.0 Type-A** ports.
7. **Phone → the powered hub**, never the board. The IO Board's two USB 3.0 ports share
   roughly **1.2 A total** through an internal current switch — well under what a phone
   draws. Powering the phone from the board causes random disconnects and slow battery
   drain that read as software bugs.
8. Power last, using the **27 W USB-C supply from the kit**. A lower-wattage supply
   reduces the USB budget further and the phone will misbehave in ways that look like
   software faults.

---

## 2. Prepare the phone

Do this once, with the phone in hand.

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

Power on and give it about a minute to come up.

The Pi is not on Tailscale yet, so find it on the building LAN — check the Xfinity admin
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

Also physically label the phone cable and the ethernet cable so they don't get
tidied away.

---

## Known constraint: disk encryption

The requirement that a non-technical person can power-cycle the Pi and have it recover
unattended **rules out full-disk encryption**. FDE needs a passphrase at boot; there is
no TPM-sealed unlock path on a Pi that survives a cold start with nobody present.

The mitigation is to keep little worth stealing on the device:

- Store patient **identifiers**, never names, in the local database and the audit log.
  Name resolution stays in the app, on the phone.
- Keep the app credential and the phone PIN in a file readable only by the service user;
  this is obfuscation against casual access, not protection against someone who takes
  the hardware.
- Treat physical possession of the unit as full compromise, and rotate the app password
  if it is ever lost or replaced. The eMMC is soldered to the module, so unlike a
  microSD it can't be quietly pulled and read in a laptop — a small but real gain.

The device lives in a private residence rather than a public area, which is the main
control here. That is a real limitation, not a solved problem — it should be a conscious
acceptance rather than an assumption.
