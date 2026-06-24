"""OnDeck web portal.

Runs on the Coach Pi (:5000) in local mode, or on Render in cloud mode
(set ONDECK_MODE=cloud).  Cloud mode is the primary management UI; the Pi
pulls config and audio files from the cloud via the /sync/* endpoints.
"""

from __future__ import annotations

import functools
import hashlib
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
import requests as rq

from config_manager import ConfigManager, MUSIC_DIR, ONDECK_HOME

# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------

CLOUD_MODE  = os.environ.get("ONDECK_MODE", "").lower() == "cloud"
SYNC_TOKEN  = os.environ.get("ONDECK_SYNC_TOKEN", "")

AUTH_FILE    = ONDECK_HOME / "auth.json"
COOKIES_FILE = ONDECK_HOME / "youtube_cookies.txt"

app = Flask(__name__)
app.secret_key = os.environ.get("ONDECK_SECRET", "ondeck-dev-secret-change-me")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

cfg = ConfigManager()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _load_auth() -> dict | None:
    """Return {username, password_hash}, or None if not yet configured."""
    # Env var takes precedence (useful for CI / scripted Render deploys).
    env_hash = os.environ.get("ONDECK_PASSWORD_HASH", "")
    if env_hash:
        return {
            "username": os.environ.get("ONDECK_USERNAME", "admin"),
            "password_hash": env_hash,
        }
    if AUTH_FILE.exists():
        try:
            data = json.loads(AUTH_FILE.read_text())
            if data.get("password_hash"):
                return data
        except Exception:
            pass
    return None


@app.before_request
def _check_auth():
    # Sync API: authenticated by Bearer token, not session.
    if request.path.startswith("/sync/"):
        return
    # Auth routes and static files are always accessible.
    if request.endpoint in ("login", "login_post", "logout", "setup", "setup_post", "static"):
        return
    # No password configured yet — force first-time setup.
    if _load_auth() is None:
        if request.endpoint != "setup":
            return redirect(url_for("setup"))
        return
    # All other routes require a logged-in session.
    if not session.get("logged_in"):
        dest = request.full_path if request.path != "/" else None
        return redirect(url_for("login", next=dest))


@app.context_processor
def _inject_globals():
    return {
        "cloud_mode": CLOUD_MODE,
        "current_user": session.get("username"),
    }


def _require_sync_token(f):
    """Decorator: require Bearer token when ONDECK_SYNC_TOKEN is set."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if SYNC_TOKEN:
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {SYNC_TOKEN}":
                return jsonify(error="unauthorized"), 401
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Audio Pi forwarding
# ---------------------------------------------------------------------------

def _audio_pi_url(path: str) -> str:
    s = cfg.system
    ip = s.get("audio_pi_ip") or "127.0.0.1"
    port = s.get("audio_pi_port", 5100)
    return f"http://{ip}:{port}{path}"


def _proxy(method: str, path: str, **kwargs):
    """Forward a request to the Audio Pi. Returns (dict, status_code)."""
    try:
        r = rq.request(method, _audio_pi_url(path), timeout=15, **kwargs)
        return r.json(), r.status_code
    except rq.exceptions.ConnectionError:
        return {"error": "Audio Pi unreachable"}, 503
    except Exception as exc:
        return {"error": str(exc)}, 500


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/login")
def login():
    if session.get("logged_in"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.post("/login")
def login_post():
    auth = _load_auth()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    remember = request.form.get("remember") == "on"

    if auth and username == auth["username"] and check_password_hash(auth["password_hash"], password):
        session.permanent = remember
        session["logged_in"] = True
        session["username"] = username
        next_url = request.args.get("next") or url_for("index")
        if not next_url.startswith("/"):   # prevent open redirect
            next_url = url_for("index")
        return redirect(next_url)

    flash("Incorrect username or password.", "error")
    return redirect(url_for("login"))


@app.get("/logout")
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("login"))


@app.get("/setup")
def setup():
    if _load_auth() is not None:
        return redirect(url_for("index"))
    return render_template("setup.html")


@app.post("/setup")
def setup_post():
    if _load_auth() is not None:
        return redirect(url_for("index"))

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    confirm  = request.form.get("confirm", "")

    if not username:
        flash("Username is required.", "error")
        return redirect(url_for("setup"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("setup"))
    if password != confirm:
        flash("Passwords do not match.", "error")
        return redirect(url_for("setup"))

    pw_hash = generate_password_hash(password)
    ONDECK_HOME.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps({"username": username, "password_hash": pw_hash}, indent=2))

    session.permanent = True
    session["logged_in"] = True
    session["username"] = username
    flash("Account created. Welcome to OnDeck!", "success")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Audio file serving (WaveSurfer loads audio from here)
# ---------------------------------------------------------------------------

@app.get("/audio/<path:filename>")
def serve_audio(filename: str):
    safe = Path(filename).name
    path = MUSIC_DIR / safe
    if not path.exists():
        abort(404)
    return send_file(str(path))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    status, _ = _proxy("GET", "/status")
    players = cfg.players_by_jersey()
    return render_template("index.html", status=status, system=cfg.system,
                           players=players, songs=cfg.songs)


# ---------------------------------------------------------------------------
# Music library
# ---------------------------------------------------------------------------

@app.get("/library")
def library():
    return render_template("library.html", songs=cfg.songs)


@app.post("/library/upload")
def library_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("library"))

    name = Path(f.filename).name
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    dest = MUSIC_DIR / name
    f.save(str(dest))

    existing = {s["filename"] for s in cfg.songs.values()}
    if name not in existing:
        cfg.add_song(name, Path(name).stem.replace("_", " ").replace("-", " "))

    try:
        with open(str(dest), "rb") as fh:
            rq.post(_audio_pi_url("/upload"), files={"file": (name, fh)}, timeout=30)
    except Exception:
        pass

    flash(f'Uploaded "{name}".', "success")
    return redirect(url_for("library"))


@app.post("/library/import")
def library_import():
    url = request.form.get("url", "").strip()
    if not url:
        flash("No URL provided.", "error")
        return redirect(url_for("library"))

    if CLOUD_MODE:
        # Run yt-dlp directly on the cloud instance (no Audio Pi).
        filename, error = _yt_dlp_import(url)
        if error:
            flash(f"Import failed: {error}", "error")
            return redirect(url_for("library"))
    else:
        data, status = _proxy("POST", "/import", json={"url": url})
        if status != 200:
            flash(f"Import failed: {data.get('error', 'unknown')}", "error")
            return redirect(url_for("library"))
        filename = data.get("filename")

    if filename:
        existing = {s["filename"] for s in cfg.songs.values()}
        if filename not in existing:
            cfg.add_song(filename, Path(filename).stem.replace("_", " "))
        flash(f"Imported \"{filename}\".", "success")
    else:
        flash("Import completed.", "success")
    return redirect(url_for("library"))


@app.get("/library/<sid>/edit")
def library_edit(sid: str):
    song = cfg.songs.get(sid)
    if not song:
        abort(404)
    return render_template("library_edit.html", sid=sid, song=song)


@app.post("/library/<sid>/save")
def library_save(sid: str):
    with cfg._lock:
        song = cfg.songs.get(sid)
        if not song:
            abort(404)
        song["display_name"] = request.form.get("display_name", song["display_name"]).strip() or song["display_name"]
        song["start_ms"] = _form_ms("start_ms", 0)
        song["end_ms"] = _form_ms_or_none("end_ms")
        cfg.save()
    flash("Saved.", "success")
    return redirect(url_for("library_edit", sid=sid))


@app.post("/library/<sid>/delete")
def library_delete(sid: str):
    with cfg._lock:
        song = cfg.songs.pop(sid, None)
        # Remove from any player that references this song.
        for p in cfg.players.values():
            if p.get("walkup_song_id") == sid:
                p["walkup_song_id"] = None
        cfg.save()
    if song:
        (MUSIC_DIR / song["filename"]).unlink(missing_ok=True)
        flash(f"Deleted '{song['display_name']}'.", "success")
    return redirect(url_for("library"))


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------

@app.get("/players")
def players():
    return render_template("players.html",
                           players=cfg.players_by_jersey(), songs=cfg.songs)


@app.get("/players/new")
def players_new():
    return render_template("player_edit.html",
                           pid=None, player=None, songs=cfg.songs)


@app.post("/players")
def players_create():
    first = request.form.get("first_name", "").strip()
    last = request.form.get("last_name", "").strip()
    try:
        jersey = int(request.form.get("jersey", ""))
    except ValueError:
        flash("Jersey must be a number.", "error")
        return redirect(url_for("players_new"))
    if not first or not last:
        flash("First and last name are required.", "error")
        return redirect(url_for("players_new"))

    pid = cfg.add_player(jersey, first, last)
    flash(f"Added #{jersey} {first} {last}.", "success")
    return redirect(url_for("players_edit", pid=pid))


@app.get("/players/<pid>/edit")
def players_edit(pid: str):
    player = cfg.players.get(pid)
    if not player:
        abort(404)
    return render_template("player_edit.html",
                           pid=pid, player=player, songs=cfg.songs)


@app.post("/players/<pid>/save")
def players_save(pid: str):
    with cfg._lock:
        player = cfg.players.get(pid)
        if not player:
            abort(404)

        try:
            player["jersey"] = int(request.form.get("jersey", player["jersey"]))
        except (ValueError, TypeError):
            pass
        player["first_name"] = request.form.get("first_name", player["first_name"]).strip() or player["first_name"]
        player["last_name"] = request.form.get("last_name", player["last_name"]).strip() or player["last_name"]
        player["walkup_song_id"] = request.form.get("walkup_song_id") or None
        player["announcement_start_ms"] = _form_ms("ann_start_ms", 0)
        player["announcement_end_ms"] = _form_ms_or_none("ann_end_ms")
        player["music_cue_ms"] = _form_ms("music_cue_ms", 0)

        sid = player.get("walkup_song_id")
        if sid and sid in cfg.songs:
            song = cfg.songs[sid]
            song["start_ms"] = _form_ms("song_start_ms", 0)
            song["end_ms"] = _form_ms_or_none("song_end_ms")

        cfg.save()

    flash("Saved.", "success")
    return redirect(url_for("players_edit", pid=pid))


@app.post("/players/<pid>/delete")
def players_delete(pid: str):
    with cfg._lock:
        player = cfg.players.pop(pid, None)
        cfg.save()
    if player:
        flash(f"Deleted #{player['jersey']} {player['first_name']} {player['last_name']}.", "success")
    return redirect(url_for("players"))


@app.post("/players/<pid>/record")
def players_record(pid: str):
    """Save a browser-recorded announcement blob (webm/ogg from MediaRecorder)."""
    player = cfg.players.get(pid)
    if not player:
        abort(404)
    blob = request.data
    if not blob:
        return jsonify(error="empty body"), 400

    filename = f"ann_{pid}.webm"
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    (MUSIC_DIR / filename).write_bytes(blob)

    with cfg._lock:
        player["announcement_file"] = filename
        player["announcement_start_ms"] = 0
        player["announcement_end_ms"] = None
        cfg.save()

    try:
        rq.post(_audio_pi_url("/upload"),
                files={"file": (filename, blob, "audio/webm")}, timeout=15)
    except Exception:
        pass

    return jsonify(ok=True, filename=filename)


# ---------------------------------------------------------------------------
# Audio Pi proxy endpoints (called by browser JS)
# ---------------------------------------------------------------------------

@app.get("/api/status")
def api_status():
    data, code = _proxy("GET", "/status")
    return jsonify(data), code


@app.post("/api/preview")
def api_preview():
    body = request.get_json(force=True) or {}
    data, code = _proxy("POST", "/queue", json=body)
    if code != 200:
        return jsonify(data), code
    data, code = _proxy("POST", "/play")
    return jsonify(data), code


@app.post("/api/stop")
def api_stop():
    data, code = _proxy("POST", "/stop")
    return jsonify(data), code


@app.post("/api/fade")
def api_fade():
    body = request.get_json(force=True, silent=True) or {}
    data, code = _proxy("POST", "/fade", json=body)
    return jsonify(data), code


@app.post("/api/volume")
def api_volume():
    body = request.get_json(force=True) or {}
    data, code = _proxy("POST", "/volume", json=body)
    return jsonify(data), code


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/settings")
def settings():
    return render_template("settings.html", system=cfg.system,
                           has_yt_cookies=COOKIES_FILE.exists())


@app.post("/settings/save")
def settings_save():
    with cfg._lock:
        s = cfg.system
        s["audio_pi_ip"] = request.form.get("audio_pi_ip", "").strip()
        try:
            s["audio_pi_port"] = int(request.form.get("audio_pi_port", 5100))
        except ValueError:
            pass
        try:
            s["volume"] = max(0, min(100, int(request.form.get("volume", 80))))
        except ValueError:
            pass
        cfg.save(mark_dirty=False)
    flash("Settings saved.", "success")
    return redirect(url_for("settings"))


@app.post("/settings/youtube-cookies")
def settings_youtube_cookies():
    text = request.form.get("cookies", "").strip()
    if text:
        ONDECK_HOME.mkdir(parents=True, exist_ok=True)
        COOKIES_FILE.write_text(text)
        flash("YouTube cookies saved.", "success")
    else:
        COOKIES_FILE.unlink(missing_ok=True)
        flash("YouTube cookies cleared.", "success")
    return redirect(url_for("settings"))


# ---------------------------------------------------------------------------
# Sync API — Pi polls these to stay in sync with the cloud config
# ---------------------------------------------------------------------------

@app.get("/sync/config")
@_require_sync_token
def sync_config():
    """Full config JSON — Pi replaces its local copy with this."""
    return jsonify(cfg.data)


@app.get("/sync/files")
@_require_sync_token
def sync_list_files():
    """Manifest of every audio file: name, size, md5."""
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for f in sorted(MUSIC_DIR.iterdir()):
        if f.is_file():
            digest = hashlib.md5(f.read_bytes()).hexdigest()
            files.append({"filename": f.name, "size": f.stat().st_size, "md5": digest})
    return jsonify(files=files)


@app.get("/sync/files/<path:filename>")
@_require_sync_token
def sync_download_file(filename: str):
    """Download one audio file."""
    safe = Path(filename).name
    path = MUSIC_DIR / safe
    if not path.exists():
        abort(404)
    return send_file(str(path))


@app.post("/sync/ping")
@_require_sync_token
def sync_ping():
    """Pi reports its local IP and hostname so the dashboard can show it."""
    body = request.get_json(force=True) or {}
    pi_id = (body.get("pi_id") or "default")[:64]
    with cfg._lock:
        cfg.system.setdefault("known_pis", {})[pi_id] = {
            "ip":        body.get("ip") or request.remote_addr,
            "hostname":  body.get("hostname", pi_id),
            "last_seen": body.get("timestamp", ""),
        }
        cfg.save(mark_dirty=False)
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _yt_dlp_import(url: str) -> tuple[str | None, str | None]:
    """Run yt-dlp on this process (cloud mode). Returns (filename, error)."""
    import subprocess
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(MUSIC_DIR / "%(title)s.%(ext)s")

    # Use imageio-ffmpeg's bundled binary so we don't need a system ffmpeg.
    ffmpeg_location = None
    try:
        import imageio_ffmpeg
        ffmpeg_location = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass

    cmd = [
        "yt-dlp", "-x", "--audio-format", "mp3", "--no-playlist",
        "--print", "after_move:filepath", "-o", out_tmpl,
        # tv_embedded uses a different YouTube API endpoint that doesn't
        # require n-challenge JS solving — intended for TV/headless environments.
        "--extractor-args", "youtube:player_client=tv_embedded",
        url,
    ]
    if ffmpeg_location:
        cmd += ["--ffmpeg-location", ffmpeg_location]
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        return None, "yt-dlp not installed"
    except subprocess.TimeoutExpired:
        return None, "import timed out"

    if result.returncode != 0:
        return None, (result.stderr.strip()[-500:] or "import failed")

    filepath = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return (Path(filepath).name if filepath else None), None


def _form_ms(field: str, default: int = 0) -> int:
    v = request.form.get(field, "").strip()
    return int(v) if v else default


def _form_ms_or_none(field: str):
    v = request.form.get(field, "").strip()
    return int(v) if v else None


if __name__ == "__main__":
    port = int(os.environ.get("ONDECK_PORTAL_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)
