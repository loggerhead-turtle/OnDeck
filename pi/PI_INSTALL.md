# OnDeck — Raspberry Pi install, Wi-Fi & account linking

OnDeck runs on Raspberry Pis on a local field network. There are two roles
(a single Pi can do both for testing):

| Pi | Role | What it does |
|----|------|--------------|
| **Coach Pi** | `coach` | Stream Deck XL + web portal + Wi-Fi onboarding |
| **Audio Pi** | `audio` | Plugged into the field PA; plays the audio |

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
# Coach Pi (Stream Deck + web portal)
curl -fsSL https://raw.githubusercontent.com/loggerhead-turtle/OnDeck/main/pi/bootstrap.sh | sudo bash -s -- coach

# Audio Pi (field PA)
curl -fsSL https://raw.githubusercontent.com/loggerhead-turtle/OnDeck/main/pi/bootstrap.sh | sudo bash -s -- audio

# Both roles on one Pi (testing)
curl -fsSL https://raw.githubusercontent.com/loggerhead-turtle/OnDeck/main/pi/bootstrap.sh | sudo bash -s -- both
```

Bootstrap downloads over **HTTPS** (no SSH deploy key). For a private repo:

```bash
curl -fsSL .../pi/bootstrap.sh | sudo ONDECK_GIT_TOKEN=ghp_xxx bash -s -- coach
```

Then reboot: `sudo reboot`.

---

## 3. Link the device to the cloud

OnDeck links a Pi to the cloud with a **Cloud URL** and a **Sync Token**
(`ONDECK_SYNC_TOKEN` — the value Render generated for your service; also shown in
the portal Settings). Three ways to provide them:

- **Captive portal (default):** an unlinked Coach Pi broadcasts an
  **`OnDeck-Setup`** Wi-Fi network. Connect a phone; a setup page opens — pick
  your Wi-Fi, then paste the Cloud URL + Sync Token. The Pi reboots linked.
- **Zero-touch:** drop an `ondeck.json` on the SD card's boot partition
  (see `pi/ondeck.boot.example.json`) with `cloud_url` + `sync_token`. Combined
  with Wi-Fi baked in via Imager, the Pi self-links on first boot — no portal.
  The file is deleted after linking so the token isn't left on the card.
- **From the portal:** once online, **Settings → Cloud** (`/cloud-settings`)
  on the Coach Pi.

---

## 4. Wi-Fi: adding networks (field, school, away games)

Three ways, any time:

1. **Edit the SD card:** drop `ondeck-wifi.json` (or `ondeck-wifi.txt`) on the
   boot partition (see `pi/ondeck-wifi.example.json`). Applied on next boot,
   then deleted.
2. **No working Wi-Fi → portal opens automatically.** If a linked Pi can't get
   online, it reopens the `OnDeck-Setup` network with an **Add Wi-Fi** page
   (existing networks are kept).
3. **From the portal:** **Wi-Fi** (`/wifi`) on the Coach Pi.

### Force the setup portal (guaranteed)

If Wi-Fi was pre-baked and the portal never appeared, drop an empty file named
**`ondeck-setup`** on the boot partition. The portal then runs on the next boot
regardless of Wi-Fi/link state, and the marker is consumed after one run.

---

## 5. Services & logs

| Service | Role | Purpose |
|---------|------|---------|
| `ondeck-setup` | coach | Boot gate: applies boot-partition Wi-Fi, links, or opens the portal. Runs **before** `ondeck-coach`. |
| `ondeck-coach` | coach | Stream Deck loop + web portal (`:5000`). |
| `ondeck-audio` | audio | Audio Pi playback server (`:5100`). |
| `ondeck-sync.timer` | all | Pulls config + audio from the cloud every 5 min. |

```bash
journalctl -u ondeck-setup -f     # boot/Wi-Fi onboarding
journalctl -u ondeck-coach -f     # Stream Deck + portal
journalctl -u ondeck-sync  -f     # cloud sync
```

---

## 6. Updating

- **Config + audio** sync from the cloud automatically (every 5 min and on
  boot) — no reinstall needed.
- **Code:** re-run the bootstrap one-liner (§2); it updates the checkout in
  place and restarts the services.
