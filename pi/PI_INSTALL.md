# OnDeck — Raspberry Pi install, Wi-Fi & account linking

OnDeck runs on two Raspberry Pis on a local field network (a single Pi can do
both roles for testing). **Both** Pis support headless Wi-Fi onboarding — if no
Wi-Fi is configured, the Pi raises an `OnDeck-Setup` hotspot with a captive
portal:

| Pi | Role | What it does |
|----|------|--------------|
| **Stream Deck Pi** | `deck` | Stream Deck XL + local web portal + Wi-Fi onboarding |
| **Audio Pi** | `audio` | Plugged into the field PA; plays the audio + Wi-Fi onboarding |

(`coach` is accepted as an alias of `deck` for back-compat.)

---

## 1. Flash the SD card with Raspberry Pi Imager

1. Choose **Raspberry Pi OS Lite (64-bit)**.
2. Open **Edit settings** (gear) and set:
   - **Hostname** — e.g. `ondeck-coach` / `ondeck-audio`.
   - **Username & password** — any username; the installer auto-detects it
     (nothing is hardcoded to `pi`).
   - **Wireless LAN** — your home/practice Wi-Fi so the Pi can get online to
     install and sync. (You can add field/away networks later — §4.)
   - **Enable SSH**.
3. Write the card and boot the Pi.

---

## 2. Install (one line)

SSH in (`ssh <youruser>@<hostname>.local`) and run **one** of:

```bash
# Stream Deck Pi (Stream Deck + web portal)
curl -fsSL https://raw.githubusercontent.com/loggerhead-turtle/OnDeck/main/pi/bootstrap.sh | sudo bash -s -- deck

# Audio Pi (field PA)
curl -fsSL https://raw.githubusercontent.com/loggerhead-turtle/OnDeck/main/pi/bootstrap.sh | sudo bash -s -- audio

# Both roles on one Pi (testing)
curl -fsSL https://raw.githubusercontent.com/loggerhead-turtle/OnDeck/main/pi/bootstrap.sh | sudo bash -s -- both
```

Bootstrap downloads over **HTTPS** (no SSH deploy key). For a private repo:

```bash
curl -fsSL .../pi/bootstrap.sh | sudo ONDECK_GIT_TOKEN=ghp_xxx bash -s -- deck
```

Then reboot: `sudo reboot`.

---

## 3. Link the device to your account

Each Pi links to your cloud account with a **pairing code** you generate in the
portal under **Devices**. Generate one code per Pi (name it, pick the role); the
code mints that device its own sync token, so you can see it, rename it, or
**revoke** it independently. Three ways to enter it:

- **Captive portal (default):** an unlinked Pi broadcasts an **`OnDeck-Setup`**
  Wi-Fi network. Connect a phone; a setup page opens — pick your Wi-Fi, then
  enter the **Cloud URL + Pairing Code**. The Pi reboots, joins Wi-Fi, redeems
  the code, and comes up linked.
- **Zero-touch:** drop an `ondeck.json` on the SD card's boot partition
  (see `pi/ondeck.boot.example.json`) with `cloud_url` + `pairing_code` (a raw
  `sync_token` also works). Combined with Wi-Fi baked in via Imager, the Pi
  self-links on first boot — no portal. The file is deleted after linking.
- **From the portal:** once online, **Cloud Settings** (`/cloud-settings`) on
  the Stream Deck Pi accepts a pairing code (or a raw token).

The code is short-lived and single-use; if it expires, generate a new one on the
Devices page. To unlink a Pi, **Revoke** it on the Devices page — its token
stops working immediately.

---

## 4. Wi-Fi: adding networks (field, school, away games)

Three ways, any time:

1. **Edit the SD card:** drop `ondeck-wifi.json` (or `ondeck-wifi.txt`) on the
   boot partition (see `pi/ondeck-wifi.example.json`). Applied on next boot,
   then deleted.
2. **No working Wi-Fi → portal opens automatically.** If a linked Pi can't get
   online, it reopens the `OnDeck-Setup` network with an **Add Wi-Fi** page
   (existing networks are kept).
3. **From the portal:** **Wi-Fi** (`/wifi`) on the Stream Deck Pi.

### Force the setup portal (guaranteed)

If Wi-Fi was pre-baked and the portal never appeared, drop an empty file named
**`ondeck-setup`** on the boot partition. The portal then runs on the next boot
regardless of Wi-Fi/link state, and the marker is consumed after one run.

---

## 5. Services & logs

| Service | Role | Purpose |
|---------|------|---------|
| `ondeck-setup` | all | Boot gate: applies boot-partition Wi-Fi, redeems a pending pairing code, or opens the captive portal. Runs **before** the main service on every role. |
| `ondeck-coach` | deck | Stream Deck loop + web portal (`:5000`). |
| `ondeck-audio` | audio | Audio Pi playback server (`:5100`). |
| `ondeck-sync.timer` | all | Pulls config + audio from the cloud every 5 min. |

```bash
journalctl -u ondeck-setup -f     # boot/Wi-Fi onboarding + pairing
journalctl -u ondeck-coach -f     # Stream Deck + portal
journalctl -u ondeck-sync  -f     # cloud sync
```

---

## 6. Updating

- **Config + audio** sync from the cloud automatically (every 5 min and on
  boot) — no reinstall needed.
- **Code:** re-run the bootstrap one-liner (§2); it updates the checkout in
  place and restarts the services.
