# Building and shipping the golden image

Build in Texas on a Windows laptop, ship a file, clone it in Florida.

> ## ⚠ Before any `dd` or imaging command
>
> The development machine has **other drives that must not be touched** — notably a 1 TB
> SSD carrying an unrelated Batocera arcade install.
>
> `dd` and raw disk writes are instant and irreversible. There is no undo and no
> recycle bin.
>
> **Identify the target device explicitly every single time**, immediately before the
> command, and confirm the size matches the SD card (~64 GB, not ~1 TB). Never reuse a
> device path from an earlier step or an earlier session — Windows and Linux both
> renumber devices between reboots and replugs.

---

## 0. Tooling on Windows

| Need | Tool |
|---|---|
| Write cards, flash the final image | **Raspberry Pi Imager** |
| Read a card to a file, shrink, compress | **WSL2** (Ubuntu) |
| Everything else | PowerShell |

```powershell
winget install RaspberryPiFoundation.RaspberryPiImager
wsl --install -d Ubuntu     # skip if WSL2 is already present
```

Inside WSL:

```bash
sudo apt update && sudo apt install -y xz-utils parted
git clone https://github.com/Drewsif/PiShrink.git
sudo install -m 755 PiShrink/pishrink.sh /usr/local/bin/pishrink
```

**Free disk space:** the raw read of a 64 GB card is a 64 GB file, before shrinking.
Budget ~80 GB free. The final artefact is 1–2 GB.

---

## 1. Build the reference Pi

Follow [PI_SETUP.md](./PI_SETUP.md) normally: flash the card, boot, run
`scripts/firstboot.sh`, attach the test phone.

Then **validate against the real app before imaging anything.** The point of a golden
image is that Florida gets something already proven.

- [ ] `adb devices` stable across a reboot
- [ ] REQ-1 probe passes against the target app
- [ ] Location gate allows and denies correctly on fixtures
- [ ] Scheduler fires and writes a complete audit record
- [ ] Signature replays legibly and undistorted (REQ-10)
- [ ] UI reachable over Tailscale Serve, both languages
- [ ] `manager.sh` deploys an update *and* rolls back a deliberately broken one
- [ ] Heartbeat pings, and stopping the agent raises the alarm
- [ ] **Pull the plug — everything returns unattended. Twice.**

---

## 2. Sanitize

On the Pi, as the last thing before shutdown:

```bash
sudo /opt/aptlog/scripts/sanitize-for-image.sh
sudo shutdown -h now
```

This strips SSH host keys, `/etc/machine-id`, Tailscale state, `/var/lib/aptlog/*`,
secrets, logs, and shell history. See ARCHITECTURE.md §5.2 for why each one matters.

**Do not boot the Pi again after sanitizing.** First boot regenerates everything the
script removed, and you would be imaging a machine that has re-acquired an identity.
If you do boot it accidentally, re-run the script.

---

## 3. Capture the card

Pull the card and put it in the Windows laptop.

### 3.1 Identify the disk — carefully

```powershell
Get-Disk | Format-Table Number, FriendlyName, @{n='GB';e={[int]($_.Size/1GB)}}, BusType
```

Find the row that is **~59–64 GB** with `BusType` of `USB` or `SD`. Note its `Number`.

> Confirm the size. A `Number` pointing at a ~931 GB disk is the arcade SSD. Writing
> to it destroys that install.

### 3.2 Mount it into WSL and read it

```powershell
# PowerShell, as Administrator. Replace 2 with YOUR disk number from 3.1.
wsl --mount \\.\PHYSICALDRIVE2 --bare
```

```bash
# In WSL — confirm the size again before reading
lsblk -o NAME,SIZE,TYPE,MODEL

sudo dd if=/dev/sdX of=/mnt/c/pi/aptlog-raw.img bs=4M status=progress
```

Replace `/dev/sdX` with the device whose size matches the card. `lsblk` output is the
check — if it says 931 G, stop.

```powershell
wsl --unmount \\.\PHYSICALDRIVE2
```

### 3.3 Shrink and compress

```bash
sudo pishrink -Z /mnt/c/pi/aptlog-raw.img /mnt/c/pi/aptlog.img
```

PiShrink does two things that matter:

- shrinks the root filesystem to its actual used size, so a 64 GB image becomes ~3 GB
- **adds a first-boot auto-expand**, so the image fits any card at least as large as the
  used space and grows to fill whatever it lands on

Without it the recipient's card would have to be **at least as large as yours**, and the
upload would be 64 GB.

`-Z` produces `aptlog.img.xz`, typically 1–2 GB.

---

## 4. Ship it

Google Drive, Dropbox, or anything that handles ~2 GB. Send the recipient:

1. the `.img.xz` link
2. the Tailscale auth key, **separately** — see below
3. the flashing instructions in §5

Verify the transfer:

```bash
sha256sum /mnt/c/pi/aptlog.img.xz
```

Send the hash alongside; a truncated upload produces a card that boots to nothing.

---

## 5. Flashing it in Florida

The recipient needs Raspberry Pi Imager and a card reader. Nothing else.

1. **Raspberry Pi Imager → Choose OS → "Use custom"** → select `aptlog.img.xz`
   (Imager decompresses `.xz` directly; no need to extract it first)
2. Choose the card. **Skip OS customization entirely** — the image already carries
   hostname, user, SSH keys, timezone, and services. Imager's settings interact
   unpredictably with custom images.
3. Write, then **leave the card in the reader.**
4. Open the `bootfs` partition that appears in Explorer. Edit
   **`tailscale-authkey.txt`** in Notepad: paste the auth key, save, close.
5. Eject, card into the Pi, assemble per PI_SETUP.md §1.
6. Power on. First boot regenerates host keys and machine-id, expands the filesystem,
   joins Tailscale, and starts the services. Allow ~5 minutes and one automatic reboot.

That is the whole on-site procedure. No terminal, no SSH, no jumper.

### 5.1 Then, from Texas

```bash
ssh apt@aptlog          # over Tailscale
```

- Confirm the five checks in PI_SETUP.md §4
- **Disable node key expiry** in the Tailscale console (PI_SETUP.md §4.1)
- Confirm the heartbeat is arriving
- Have someone on site accept the phone's **"Allow USB debugging?"** prompt — the adb key
  was stripped, so the new phone must authorise the new Pi. This is the one step that
  cannot be done remotely.

---

## 6. Rebuilding the image later

A hand-built image is not reproducible. If the reference Pi is lost, the way back is
`firstboot.sh` against a fresh card, not memory — which is why that script stays the
source of truth and configuration changes belong in it rather than typed ad hoc into a
shell.

For a second deployment, re-run this document from §1.
