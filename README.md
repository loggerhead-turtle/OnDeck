# OnDeck ⚾🎵 — retired, merged into Play-Call

**This repository is retired.** OnDeck (walk-up music, announcements,
game-day sounds, Stream Deck control) now lives inside
[Play-Call](https://github.com/loggerhead-turtle/play-call):

- **Web portal** → Play-Call pages under `/ondeck/*`
  (`cloud/routes/ondeck.py`, `cloud/routes/ondeck_sync.py`, `cloud/ondeck/`)
  — one login, one roster (`sk_players`), and the batting order comes
  straight from the Dugout lineup.
- **Pi-side code** (sync agent, music server, Stream Deck runtime, installers)
  → `ondeck-pi/` in the Play-Call repo, unchanged.
- **Pi migration**: set `ONDECK_CLOUD_URL=https://playsigns.net/ondeck` and a
  new device token (portal → Walk-Up → Devices) in `~/ondeck/sync.env`.
  The `/ondeck/sync/*` API is byte-compatible with this repo's `/sync/*`.

The standalone Render service (`ondeck-43di.onrender.com`) is being shut
down. Nothing else in this repo is maintained; the full history is preserved
here for reference.
