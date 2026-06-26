# OnDeck — Project Context

OnDeck is a baseball walk-up music system. A coach manages players and music through a web portal; a Stream Deck triggers playback at the field; a Raspberry Pi (Audio Pi) drives the PA speaker.

## Architecture

```
Cloud (Render)          Coach Pi              Audio Pi
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
| `POST /sync/ping` | Bearer token | Pi posts `{hostname, ip}`, cloud records last_seen |

Pi reads `~/ondeck/sync.env` for `ONDECK_CLOUD_URL` and `ONDECK_SYNC_TOKEN`.

## Render Deployment

- Service URL: `https://ondeck-43di.onrender.com`
- Persistent disk: 5 GB at `/data`
- Branch: `claude/ondeck-web-portal-trim-hvgzup` (dev branch — PR #3 open to main)
- Current buildCommand: `pip install --upgrade -r requirements.txt && pip install nodeenv && nodeenv --prebuilt /opt/render/project/src/.node`
- On first deploy: visit `/setup` to create account

Firebase redirect: `example.com/ondeck` → `https://ondeck-43di.onrender.com` (302, configured in `firebase.json`)

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
