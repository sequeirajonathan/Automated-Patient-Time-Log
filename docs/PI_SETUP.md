# Raspberry Pi 5 — build and flash checklist

Audience: whoever physically assembles the Pi at the building. Follow in order.

Do the **Before you travel** section somewhere with good internet and a laptop. The
on-site steps then need no troubleshooting.

---

## 0. Before you travel

### 0.1 Flash the card

Use **Raspberry Pi Imager** (raspberrypi.com/software) on a laptop with the card reader
from the kit.

- **Device:** Raspberry Pi 5
- **OS:** `Raspberry Pi OS Lite (64-bit)` — under *Raspberry Pi OS (other)*

Lite, not Desktop. No monitor is ever attached to this machine, a desktop session is
one more thing that can hang, and the GUI roughly triples SD card writes.

Before writing, open **⚙ / "Edit Settings"** and set all of the following. This is what
makes the Pi boot ready with nothing to configure on site:

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

After Imager finishes, the card remounts as `bootfs`. Copy
[`scripts/firstboot.sh`](../scripts/firstboot.sh) to the root of that partition and put
the auth key in it where marked.

---

## 1. Assemble

1. Fit the **Active Cooler** (or the case's fan) to the board before anything else — the
   fan connector is awkward to reach once the board is in the case.
   **Only one cooling solution.** The official case's built-in fan and the Active Cooler
   occupy the same space; don't try to fit both.
2. Insert the microSD.
3. Fit the RTC battery if the kit includes one. It keeps the clock across power cuts,
   which matters here — a scheduler that reboots at 3am shouldn't run on a wrong clock
   before it reaches an NTP server.
4. Board into case.
5. **Ethernet** from the Pi to a LAN port on the Xfinity gateway.
6. **Powered USB hub** into its own wall outlet, then hub → a **USB 3.0** port on the Pi
   (the blue ones).
7. **Phone → the powered hub**, not the Pi directly. The Pi's ports supply 1.6 A across
   all four combined, which will not reliably charge a phone. Powering the phone from
   the Pi causes random disconnects and slow battery drain.
8. Pi power last, using the **27 W USB-C supply from the kit**. A lower-wattage supply
   silently drops the USB budget to 600 mA and the phone will misbehave in ways that
   look like software bugs.

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
  the card.
- Treat physical access to the SD card as full compromise, and rotate the app password
  if the hardware is ever lost or replaced.

The device lives in a private residence rather than a public area, which is the main
control here. That is a real limitation, not a solved problem — it should be a conscious
acceptance rather than an assumption.
