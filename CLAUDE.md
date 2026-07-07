# OnDeck — Project Context

OnDeck is a baseball walk-up music system. A coach manages players and music through a web portal; a Stream Deck triggers playback at the field; a Raspberry Pi (Audio Pi) drives the PA speaker.

## Architecture

```
Cloud (Render)          Stream Deck Pi        Audio Pi
┌─────────────┐         ┌──────────────┐      ┌──────────────┐
│ web/app.py  │◄──sync──│ sync_agent.py│      │music_server.py│
│ Flask :443  │         │ Flask :5000  │─────►│ Flask :5100   │
│ ONDECK_MODE │         │              │HTTP  │ PA speaker    │
│ =cloud      │         │ Stream Deck  │      └──────────────┘
└─────────────┘         └──────────────┘
```

The cloud instance is the source of truth. The Pi polls `/sync/*` endpoints every 5 minutes via `sync_agent.py` (systemd timer) to pull config and audio files down.

## Key Files

| File | Purpose |
|------|---------|
| `web/app.py` | Flask portal — all routes, auth, sync API, YouTube import |
| `config_manager.py` | Thread-safe JSON config (players, songs, system settings) |
| `sync_agent.py` | Pi-side sync client — polls cloud, downloads files, posts ping |
| `web/templates/base.html` | Dark Tailwind layout, nav, flash messages |
| `web/templates/_trim_editor.html` | WaveSurfer.js v7 DAW-style trim editor macro |
| `web/templates/player_edit.html` | Player editor with MediaRecorder announcement recording |
| `web/templates/library_edit.html` | Song editor with trim editor |
| `web/templates/deck_editor.html` | Stream Deck button editor (8×4 grid, per-key slots) |
| `web/templates/devices.html` | Pi device pairing + management (codes, rename, revoke) |
| `streamdeck_controller.py` | Stream Deck runtime (any size) — OnDeck-specific pages/actions on the shared `pideck` library (github.com/loggerhead-turtle/pi-deck; installed by `install.sh`, sibling-checkout fallback) |
| `pi/kiosk_routes.py` | No-login field pages: `/deck-settings` (volume, transport, replay, lineup reset, deck pages, model) + `/pi-settings` (sync now, restart, reboot, shutdown) |
| `web/templates/admin_activity.html` | Admin "New Activity" feed — recent uploads/placements, persists until Clear |
| `bluetooth_manager.py` | Audio Pi BlueZ control (`bluetoothctl`) + preferred-speaker auto-connect + sink routing |
| `web/templates/bluetooth.html` | Bluetooth speaker management page (proxied to the Audio Pi) |
| `web/templates/login.html` | Standalone login page |
| `web/templates/setup.html` | First-run account creation |
| `web/templates/settings.html` | Settings + YouTube cookie upload |
| `render.yaml` | Render deploy config (native Python, persistent disk) |
| `Dockerfile` | Docker alternative (not currently used by Render) |
| `install.sh` | Pi setup script — installs systemd service + timer |
| `requirements.txt` | Python deps |

## Environment Variables

| Var | Value on Render | Purpose |
|-----|-----------------|---------|
| `ONDECK_HOME` | `/data/ondeck` | Base dir for all data (persistent disk) |
| `ONDECK_MODE` | `cloud` | Enables cloud mode (hides Audio Pi transport strip, runs yt-dlp locally) |
| `ONDECK_SECRET` | auto-generated | Flask session secret |
| `ONDECK_SYNC_TOKEN` | auto-generated | Bearer token for `/sync/*` API endpoints |

On the Pi, `ONDECK_HOME` defaults to `~/ondeck`. Never hardcode Pi username — always use `Path.home()`.

## Data Layout (ONDECK_HOME)

```
/data/ondeck/
  config.json          # players, songs, system settings
  auth.json            # {"username": "...", "password_hash": "..."}
  youtube_cookies.txt  # Netscape cookie file for yt-dlp (optional)
  music/               # uploaded + imported MP3 files
```

## Auth

Single-user. On first visit with no `auth.json`, redirects to `/setup` to create account. Password stored as bcrypt hash via `werkzeug.security`. 30-day session cookie. `ONDECK_PASSWORD_HASH` env var overrides `auth.json` for scripted deploys.

## Cloud Mode Differences (ONDECK_MODE=cloud)

- Transport strip (play/pause/volume) hidden in nav
- Audio Pi connection test hidden on Settings page  
- YouTube import runs `yt-dlp` directly on the cloud instance (not proxied to Pi)
- `/sync/*` endpoints active and protected by Bearer token
- Pi sync status panel shown on Home page

## YouTube Import — Current Status (KNOWN ISSUE)

YouTube import is broken on Render due to bot detection + n-challenge JS obfuscation. History of attempts:

1. Default web client → "Sign in to confirm you're not a bot" (datacenter IP blocked)
2. `--extractor-args youtube:player_client=android` → same bot detection
3. `--extractor-args youtube:player_client=ios` → iOS ignores browser cookies, same error
4. Added YouTube cookie upload in Settings (cookies saved to `youtube_cookies.txt`) → cookies authenticate, but n-challenge still fails
5. Bumped yt-dlp to `>=2025.6.1` + `pip install --upgrade` → still n-challenge fails
6. Tried nodeenv to install Node.js for JS solving → node PATH not reaching subprocess
7. Currently trying `--extractor-args youtube:player_client=tv_embedded` → in testing

**Next steps if tv_embedded fails:**
- Switch Render service to Docker runtime (requires creating a new Render service; existing can't change runtime)
- Dockerfile already exists — just needs `nodejs` added to the apt-get line
- Or: user can download MP3 locally and upload via Library → Upload

The cookie upload UI is in Settings (cloud mode only). User exports Netscape-format cookies from youtube.com using the "Get cookies.txt LOCALLY" Chrome extension.

## Trim Editor

WaveSurfer.js v7 ESM + RegionsPlugin. Implemented as a Jinja2 macro in `_trim_editor.html`. Features:
- Draggable start/end handles (green region)
- Optional cue point marker (indigo, drag-only)
- Nudge buttons: ±0.01s and ±0.1s
- Direct time input in `0:14.52` format (arrow keys ±0.01s, shift ±0.1s)
- Hidden form fields: `<input type="hidden" name="{{ start_field }}" id="{{ editor_id }}-h-start">`
- All Tailwind classes explicit (no @apply — Play CDN doesn't support it)
- `window.editors[editor_id]` for onclick= access

## Sync API (Pi ↔ Cloud)

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /sync/config` | Bearer token | Returns full config.json |
| `GET /sync/files` | Bearer token | Returns list of `{filename, md5}` for all music files |
| `GET /sync/files/<filename>` | Bearer token | Streams the audio file |
| `POST /sync/ping` | Bearer token | Pi posts `{hostname, ip}`; cloud updates the device's last_seen |
| `POST /sync/pair` | none (code) | Pi posts `{code, hostname}`; cloud redeems a pairing code → returns a per-device `sync_token` |

Bearer token = the global `ONDECK_SYNC_TOKEN` **or** any non-revoked per-device
token minted via pairing. Pi reads `~/ondeck/sync.env` for `ONDECK_CLOUD_URL` and
`ONDECK_SYNC_TOKEN`.

## Devices & Pairing

Admins link each Pi from the portal **Devices** page (`/ondeck/devices`):
generate a short pairing code (named, role `deck`/`audio`); the Pi redeems it via
`/sync/pair` (captive portal, boot file, or `/cloud-settings`) and gets its own
revocable token. `config["devices"]` holds linked Pis (name, role, token,
last_seen); `config["pairing_codes"]` holds outstanding codes (TTL like signup
links). Helpers live in `config_manager.py` (`create_pairing_code`,
`redeem_pairing_code`, `device_for_token`, `touch_device`, `revoke_device`).

## Stream Deck Editor

The portal **Stream Deck** page (`/ondeck/deck`) lays out the physical keys.
Each page stores `pages[id].slots["<idx>"] = {type, ref, label, color, …}` for
the content keys (`type ∈ player_walkup|song|celebration|nav|remote_nav|
lineup_slot|edit_lineup|action|text|blank`; `action` refs: `play|stop|fade|
replay|reset_lineup`). `remote_nav` also stores `target` (a deck device id or
`"all"`). The deck runtime (`streamdeck_controller.py`) renders/handles from
slots when a page has any, else falls back to the built-in auto-layout.
Everything syncs via `/sync/config` (no new endpoints). Fixed
nav/transport/page-shortcut keys stay owned by the controller.

## Deck Sizes & Knobs

`system["deck_model"]` (Settings → Stream Deck) picks the physical model:
`mini|original|neo|plus|xl|studio` (`DECK_MODELS` in `config_manager.py`).
`compute_deck_layout(rows, cols)` derives the fixed-key roles per size —
rows≥4: nav col + bottom-row transport/shortcuts (classic XL); rows==3: nav
col + transport after Next; rows==2: Prev/Next only (put transport on slots).
The same rules live in `pideck.layout` (duplicated on purpose — the cloud has
no pideck install; keep them in step). The controller follows the *attached
hardware's* grid if it differs from the config and logs a warning. On a
Stream Deck + / Studio, dial 0 turns the Audio Pi volume; pressing it mutes.

## Replay, Lineup Reset & Deck-to-Deck Nav

- Audio Pi remembers the last played clip; `POST /replay` re-runs it
  (`MusicClient.replay()`, deck action `replay`, portal `/ondeck/api/replay`).
- `LineupManager.reset()` jumps back to the leadoff hitter and re-cues (deck
  action `reset_lineup`, portal `POST /ondeck/api/lineup/reset`).
- Deck-to-deck: a `remote_nav` key posts `{page}` to the target deck Pi's
  `POST /ondeck/api/deck/goto` (:5000, session-exempt, Pi-only — main.py
  registers the live controller via `web.app.attach_runtime`). Peer IPs come
  from `config["devices"]` (kept fresh by `/sync/ping`).

## Upload Auto-Placement & New Activity

Player AJAX uploads (`/ondeck/player-upload`) auto-place by `song_type`:
`walkup` → player's walk-up; `warmup` → player's warm-up **and** the Pitcher
Warm-Up page's song list; `midgame` → player's mid-inning song **and** the
Mid-Inning page. The Library upload form has a destination select
(`player:<pid>` / `warmup` / `midgame` / `page:<page_id>`). Every
upload/import/placement is recorded in `config["recent_additions"]`
(`cfg.add_recent_addition`), shown on **Admin → New Activity**
(`/admin/activity`) until the admin presses Clear — entries are never
auto-marked seen (unlike `/ondeck/notifications`).

## Failure & Power-Loss Recovery

Layered so a crash or outage mid-game resumes where it left off:
- **Process/boot**: both Pi services are systemd units with `Restart=always`
  (3s) and start on boot; the deck survives USB unplug/replug; the Bluetooth
  speaker auto-reconnects.
- **Config**: config.json writes are atomic (tmp + fsync + rename) so power
  loss can't corrupt it; the cloud re-syncs config + music every 5 min.
- **Live game state** (`game_state.py` → `$ONDECK_HOME/game_state.json`,
  local, never synced): the current batter index and the deck's visible page
  are mirrored on every change. On startup `LineupManager.restore()` (called
  in main.py) reloads the index and re-cues that batter — if the Audio Pi is
  still booting, the auto-advance poller retries the cue until it answers —
  and the controller returns to the saved page.
- **Audio Pi** (`$ONDECK_HOME/player_state.json`): volume and the last-played
  clip persist, so `/replay` works right after a reboot. A song that was
  *playing* when power died does not auto-resume mid-note (deliberate — use
  Replay); stale/corrupt state files are ignored.

## Kiosk Pages (no login, field LAN only)

Registered only on the Pis (never the cloud): `/deck-settings` on the deck
Pi's portal (volume slider, Play/Stop/Fade/Replay, lineup reset, jump deck to
page, brightness, deck model, Audio Pi target) and `/pi-settings` on **both**
Pis (hostname/IP, Wi-Fi + cloud-link shortcuts, sync now, service restart,
reboot, shutdown — via the `ondeck-power` NOPASSWD sudoers rule from
install.sh). Endpoint names are exempted in `_check_auth`'s `always_ok`.

## Pi Roles

`install.sh` / `bootstrap.sh` take `ROLE=audio|deck|both` (`coach` == `deck`).
**Both** roles install the `ondeck-setup` boot gate, so the Audio Pi and the
Stream Deck Pi can each be onboarded headlessly over the `OnDeck-Setup` hotspot.

## Bluetooth Speaker (Audio Pi)

`bluetooth_manager.py` runs inside `music_server.py` on the Audio Pi. It wraps
`bluetoothctl` to scan/pair/connect/forget an A2DP speaker (e.g. Bose S1 Pro+),
remembers a **preferred speaker + auto-connect** flag in
`$ONDECK_HOME/bluetooth.json` (local to the Audio Pi — not synced), and runs a
~20s loop that reconnects the preferred speaker whenever it powers on (offline,
no cloud needed). When a speaker is connected, `Player._output_args()` routes
ffmpeg to its PipeWire/Pulse sink (`-f pulse <sink>`); otherwise ALSA `default`.
`ONDECK_NO_BLUETOOTH=1` disables it (laptops/CI); `ONDECK_FFMPEG_OUT` still
overrides output.

Endpoints on the Audio Pi: `GET /bluetooth/status`, `POST /bluetooth/{scan,
pair,connect,disconnect,forget,preferred}`. The portal page `/ondeck/bluetooth`
manages it by **proxying** to the Audio Pi via `/ondeck/api/bluetooth/*` — so it
works from any browser that can reach the Pi (the Stream Deck Pi's portal on the
field Wi-Fi). The cloud portal can't route to the Pi, so there it shows "Audio Pi
unreachable". Audio role installs PipeWire + `libspa-0.2-bluetooth`, enables
user-session lingering, and drops a WirePlumber config disabling
`monitor.bluez.seat-monitoring` so the bluez sink exists headless. (Without
that, WirePlumber gates its Bluetooth monitor on an active logind seat — which
a headless/lingering-only Pi lacks — and `connect` fails with
`org.bluez.Error.Failed br-connection-profile-unavailable`.)

## Render Deployment

- Service URL: `https://ondeck-43di.onrender.com`
- Persistent disk: 5 GB at `/data`
- Branch: `claude/ondeck-web-portal-trim-hvgzup` (dev branch — PR #3 open to main)
- Current buildCommand: `pip install --upgrade -r requirements.txt && pip install nodeenv && nodeenv --prebuilt /opt/render/project/src/.node`
- On first deploy: visit `/setup` to create account

Optional: a custom domain can 302-redirect `your-domain.com/ondeck` → the Render
service URL (e.g. via a `firebase.json` redirect, kept out of the repo).

## Tech Stack

- Python 3.12, Flask 3.x, Jinja2
- Tailwind CSS (Play CDN — no build step)
- WaveSurfer.js v7 (ESM from jsDelivr)
- werkzeug password hashing (bcrypt)
- gunicorn (1 worker, 300s timeout for slow yt-dlp imports)
- imageio-ffmpeg (bundles ffmpeg binary — no apt-get needed for ffmpeg)
- yt-dlp for YouTube audio import
- Render (cloud), Raspberry Pi (field)

## Conventions

- `cfg` = global `ConfigManager` instance
- `CLOUD_MODE` = `os.environ.get("ONDECK_MODE", "").lower() == "cloud"`
- `MUSIC_DIR`, `ONDECK_HOME` imported from `config_manager`
- `COOKIES_FILE = ONDECK_HOME / "youtube_cookies.txt"`
- Flash categories: `"success"` (green) or `"error"` (red)
- All times stored as milliseconds (integers) in config JSON
- Config saves are atomic: write to tempfile, `os.replace` — thread-safe with `RLock`
