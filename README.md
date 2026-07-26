# OnDeck — field hardware (Raspberry Pi side)

> This repo is the **OnDeck device runtime** — what the field Pis clone and
> update from. It is mirrored out of `Play-call/ondeck-pi/`, so send code
> changes there, not here. (The standalone OnDeck *cloud portal* is retired;
> walk-up music is managed inside Play-Call now.)

OnDeck is Play-Call's walk-up music system: announcements, walk-up songs,
game-day sounds, and celebration stingers, driven from an Elgato Stream Deck
at the field with no internet required during the game.

**The cloud portal that used to live in this tree is retired.** Walk-up music
is now managed inside Play-Call itself — pages under `/ondeck/*`
(`cloud/routes/ondeck.py`, `cloud/routes/ondeck_sync.py`,
`cloud/ondeck/`), on the Play-Call roster and the Dugout lineup. This
directory holds everything that runs **on the field hardware**, unchanged:

| File | Runs on | Purpose |
|------|---------|---------|
| `sync_agent.py` | both Pis | polls the cloud every 5 min (config + audio) |
| `music_server.py` | Audio Pi | ffmpeg playback → PA speaker (`:5100`) |
| `bluetooth_manager.py` | Audio Pi | A2DP speaker pairing + auto-connect |
| `streamdeck_controller.py` | Stream Deck Pi | XL runtime on the shared `pideck` lib |
| `main.py`, `web/` | Stream Deck Pi | local field portal (`:5000`, offline mode) |
| `config_manager.py` | both Pis | local `config.json` store |
| `pi/` | both Pis | boot gate, captive portal, Wi-Fi + pairing |
| `install.sh` | both Pis | systemd services + timer (`ROLE=audio|deck|both`) |

## Pointing a Pi at Play-Call

Each Pi reads `~/ondeck/sync.env`:

```
ONDECK_CLOUD_URL=https://playsigns.net/ondeck
ONDECK_SYNC_TOKEN=<device token from pairing>
```

Pairing: generate a code on the portal (**Walk-Up → Devices**), enter it on
the Pi's captive portal or `/cloud-settings` with the cloud URL above. The
sync API (`/ondeck/sync/config|files|ping|pair`) is byte-compatible with the
old standalone service, so existing Pis only need the two `sync.env` values
updated — no reinstall.

The lineup on the Stream Deck follows the batting order set in Dugout
(Scorekeeper → game → Lineups); the standalone OnDeck lineup page no longer
exists.

## Syncing on demand

The timer pulls from the cloud every 5 minutes. To get a change immediately —
a new walk-up song, or a substitution the coach just made — either press a
**Sync** key on the deck (add one in the portal's deck editor: any key →
type `action` → *Sync Now*), or open `http://<pi>:5000/sync-now` on the deck
Pi or `:5100/sync-now` on the Audio Pi.

## If the Stream Deck sits on its Elgato logo

That means nothing claimed the device. The controller only looks for
hardware **at startup**, so a deck that enumerates late — a half-seated
cable, a slow boot, a swapped deck — leaves it idling with
`No Stream Deck found` in the journal.

`install.sh` handles this: a udev rule tags the device so plugging in any
Stream Deck runs `ondeck-deck-attached.service`, which restarts the
controller. If you're on an older install, re-run `install.sh` to get it.

To diagnose by hand:

```bash
lsusb | grep -i 0fd9                  # is it on the bus at all?
journalctl -u ondeck-coach -n 30      # "No Stream Deck found"?
sudo systemctl restart ondeck-coach   # claim a deck that showed up late
```

A deck that lights up (its own logo) but never appears in `lsusb` has power
but no data — a charge-only or damaged cable, or a bad port.
