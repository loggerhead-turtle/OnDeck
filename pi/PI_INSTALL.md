# OnDeck — Raspberry Pi install, Wi-Fi & account linking

OnDeck runs on one or two Raspberry Pis on a local field network. **All** Pis
support headless Wi-Fi onboarding — if no Wi-Fi is configured, the Pi raises
an `OnDeck-Setup` hotspot with a captive portal:

| Pi | Role | What it does |
|----|------|--------------|
| **Stream Deck Pi** | `deck` | Stream Deck XL + local web portal + Wi-Fi onboarding |
| **Audio Pi** | `audio` | Plugged into the field PA; plays the audio + Wi-Fi onboarding |
| **Single Pi** | `both` | Deck **and** audio on one Pi — play straight to a Bluetooth speaker, no second Pi (see §5b) |

(`coach` is accepted as an alias of `deck` for back-compat.)

---

## 0. Fastest path — flash a ready-made OnDeck image

Instead of §1–2 below, you can build (or download, if published on the repo's
Releases page) a flashable image with OnDeck fully baked in:

```bash
# On any Linux machine with Docker (~30 GB free disk, 30–60 min):
ROLE=audio ./pi/build_image.sh    # Audio Pi image (default)
ROLE=deck  ./pi/build_image.sh    # Stream Deck Pi image
ROLE=both  ./pi/build_image.sh    # single-Pi image (deck + Bluetooth audio)
```

Flash the resulting `deploy/image_*OnDeck-<role>.img.xz` with **Raspberry Pi
Imager → Use custom**, boot the Pi, and that's it:

- All packages, the code, and the Python environment are pre-installed —
  first boot just wires up services, no internet required.
- With no Wi-Fi configured, the Pi opens the **`OnDeck-Setup`** hotspot —
  connect a phone, pick your Wi-Fi, and enter your Cloud URL + pairing code.
- To change a card's role after flashing, edit the `ondeck-role` file on the
  boot partition (`audio`, `deck`, or `both`) before first boot.
- Default login `ondeck` / `ondeck` — change it on first login
  (`passwd`), or pre-set your own user with Imager's settings gear.

Prefer stock Raspberry Pi OS? Use the classic path below.

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

## 5. Bluetooth speaker (Audio Pi)

The Audio Pi can play through a Bluetooth speaker (e.g. **Bose S1 Pro+**)
instead of a cable. Manage it from a browser on the **field Wi-Fi**: open the
Stream Deck Pi's portal and go to **Bluetooth** (the page proxies to the Audio
Pi).

**One-time pairing:**
1. Put the speaker in Bluetooth pairing mode.
2. On the **Bluetooth** page, click **Scan**, then **Pair** next to the speaker.
3. It's automatically set as the **preferred speaker** with **auto-connect on**.

**Every game after that:** just power the speaker on near the Audio Pi. A
background loop reconnects within a few seconds — no app, no cloud, fully
offline. Pressing a song on the Stream Deck then plays through the speaker.

Notes:
- Preferred speaker + auto-connect are stored on the Audio Pi
  (`~/ondeck/bluetooth.json`), not in the cloud.
- When connected, audio routes to the speaker's PipeWire sink; otherwise it uses
  the Pi's default (3.5 mm / HDMI / USB).
- The **cloud** portal can't reach the Audio Pi (NAT) — the Bluetooth page only
  controls it from the field network. It shows "Audio Pi unreachable" elsewhere.

---

## 5b. Single-Pi mode — Bluetooth straight from the Stream Deck Pi

You can skip the Audio Pi entirely and let the Stream Deck Pi play the music
itself — over Bluetooth to the speaker, or out its own 3.5 mm / USB / HDMI
audio if you'd rather run a cable:

1. Install the Stream Deck Pi with role **`both`** (one-liner in §2, or the
   `both` image from §0). That puts the audio server on the same Pi.
2. In the portal: **Settings → Audio Output → "This Pi (single-Pi /
   Bluetooth)"**. From then on every deck press and portal preview plays
   locally instead of calling out to an Audio Pi.
3. Pair the speaker once on the **Bluetooth** page (scan → pair). Auto-connect
   is remembered, so on game day you just power the speaker on.

Switching back to a two-Pi setup later is the same setting — pick **"Audio Pi
(two-Pi setup)"** (or **Auto**) again. If no Bluetooth speaker is connected in
single-Pi mode, audio falls back to the Pi's default ALSA output (wired).

---

## 6. Services & logs

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

## 7. Updating

- **Config + audio** sync from the cloud automatically (every 5 min and on
  boot) — no reinstall needed.
- **Code:** re-run the bootstrap one-liner (§2); it updates the checkout in
  place and restarts the services.
