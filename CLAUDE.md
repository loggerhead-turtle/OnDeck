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
| `streamdeck_controller.py` | Stream Deck XL runtime — OnDeck-specific pages/actions on the shared `pideck` library (github.com/loggerhead-turtle/pi-deck; installed by `install.sh`, sibling-checkout fallback) |
| `system_stats.py` | Dependency-free system health (CPU temp/%, memory, disk, Wi-Fi, `vcgencmd get_throttled`) — used by `/ondeck/resources`, the deck `stats` key, Audio Pi `/stats`, and the sync ping |
| `web/templates/record.html` | Mobile phone recorder (`/ondeck/record`) — MediaRecorder with music-friendly constraints, iOS `audio/mp4` support, level meter |
| `web/templates/resources.html` | Live diagnostics page (`/ondeck/resources`) — local + audio-server stats, linked-device health from sync pings |
| `pi/build_image.sh` | pi-gen flashable SD image builder (`ROLE=audio\|deck\|both`) — packages/repo/venv baked in, first-boot finisher |
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

## Security conventions

- Session secret: `ONDECK_SECRET` env var, else a random secret persisted to `$ONDECK_HOME/secret_key` (0600). Never a hardcoded fallback.
- Session cookies: HttpOnly + SameSite=Lax always; `Secure` added in cloud mode.
- Sync tokens compared with `hmac.compare_digest` (app.py `_valid_sync_token` + `config_manager.device_for_token`).
- Per-IP rate limits: login 15/5 min, `/sync/pair` 10/10 min (`_rate_limited` in app.py, in-memory).
- Redirect targets go through `_safe_next_url` (blocks `//host` open redirects).
- Every upload passes `_safe_audio_filename` (secure_filename + `ALLOWED_AUDIO_EXTS` allowlist); `MAX_CONTENT_LENGTH` = 100 MB. The Audio Pi `/upload` mirrors the same check.
- Audio is served with `send_file(..., conditional=True)` — byte ranges, which iOS Safari requires for playback/scrubbing.
- `auth.json` written with mode 0600.

## Audio Output Modes (`system.audio_output`)

`auto` (default): explicit `audio_pi_ip` → last audio-role device seen by `/sync/ping` → localhost. `remote`: never falls back to localhost. `local`: always `127.0.0.1` — the Stream Deck Pi runs its own `music_server` (install `ROLE=both`) and plays straight to a Bluetooth speaker or line out, bypassing the second Pi. Selected in Settings → Audio Output; resolved by `config_manager.audio_pi_endpoint()`.

## Diagnostics

- `/ondeck/resources` (admin/editor): live stats for the portal host + audio server, plus per-device health reported in sync pings (stored whitelisted under `devices[id].stats`).
- `system_stats.gather(brief=…)` is the single collector; cached ~2 s.
- Audio Pi exposes `GET /stats`; portal proxies at `/ondeck/api/audio-stats`; local host at `/ondeck/api/local-stats`.
- Stream Deck: slot type `stats` renders live `48° / CPU 7%` with temperature-tinted background, repainting every 5 s while visible (thread in `streamdeck_controller`).

## Phone Recording (`/ondeck/record`)

MediaRecorder with `echoCancellation/noiseSuppression/autoGainControl: false` (music capture, not voice). Mime pick order: `audio/webm;codecs=opus` → `audio/webm` → `audio/mp4` (iOS Safari) → ogg; stored extension derived server-side via `_record_ext`. Requires HTTPS (secure context) — the page warns on plain-HTTP Pi portals. Players can auto-assign the take as their walk-up; staff takes land in the library. Announcement recording in `player_edit.html` uses the same mime logic.

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

## Deck Lineup Flow (built-in lineup page)

Live: press a batter key → cues their walk-up; **Play** runs it; natural end
**or Stop** advances + re-cues the next hitter (wraps at the end). Celebrations
(`note_external_playback`) never move the order. Reserved keys: **↻ Replay**
(re-runs the last walk-up via `LineupManager.replay()`; also a deck `action`
ref) and **✎ Edit**. Edit mode: every spot 1..size shown (incl. Empty), press a
spot → players page to pick (roster pages via a **More ▶** key when > 1 screen;
paging never disarms the pick), pick bounces back still in edit; **Size −/+**
and **✓ Done** keys. Current batter is in-memory (`LineupManager._index`).

## Song Fade-Out (`song.fade_out_ms`)

Per-song fade at the end of the clip (0 = hard cut). Set in every trim editor
(`fade_field`/`fade_ms` macro params; field names: `fade_out_ms` (library),
`song_fade_ms`/`warmup_fade_ms`/`midgame_fade_ms` (player editor),
`walkup_fade_ms`/`warmup_fade_ms` (my-profile), `fade_out_ms` in trim-save
AJAX). Carried in clip payloads; `music_server` applies `afade=t=out` before
the end trim — or before the *probed* file duration (`_media_duration_s`,
ffmpeg-banner parse, cached) when no end trim is set. Editor preview + demo
playback honor it (iOS ignores element volume, so no preview fade there).

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

The portal **Stream Deck** page (`/ondeck/deck`) lays out the physical XL keys.
Each page stores `pages[id].slots["<idx>"] = {type, ref, label, color}` for the 21
content keys (`type ∈ player_walkup|song|celebration|nav|action|blank`). The deck
runtime (`streamdeck_controller.py`) renders/handles from slots when a page has
any, else falls back to the built-in auto-layout. Everything syncs via
`/sync/config` (no new endpoints). Fixed nav/transport/page-shortcut keys stay
owned by the controller.

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
