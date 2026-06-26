"""OnDeck web portal.

Routes are grouped under /ondeck/* (app) and /admin (user management).
The public personal homepage lives at /.
Sync endpoints (/sync/*) are unchanged so the Pi agent keeps working.
"""

from __future__ import annotations

import functools
import hashlib
import json
import sys
import uuid
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

CLOUD_MODE          = os.environ.get("ONDECK_MODE", "").lower() == "cloud"
SYNC_TOKEN          = os.environ.get("ONDECK_SYNC_TOKEN", "")
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")

AUTH_FILE    = ONDECK_HOME / "auth.json"
COOKIES_FILE = ONDECK_HOME / "youtube_cookies.txt"

app = Flask(__name__)
app.secret_key = os.environ.get("ONDECK_SECRET", "ondeck-dev-secret-change-me")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

cfg = ConfigManager()


# ---------------------------------------------------------------------------
# Multi-user auth helpers
# ---------------------------------------------------------------------------

def _load_users() -> list[dict]:
    """Return list of {id, username, password_hash, role} for admin/editor accounts.

    Transparently migrates the old single-user auth.json format on first read.
    """
    env_hash = os.environ.get("ONDECK_PASSWORD_HASH", "")
    if env_hash:
        return [{"id": "env-admin",
                 "username": os.environ.get("ONDECK_USERNAME", "admin"),
                 "password_hash": env_hash,
                 "role": "admin"}]
    if not AUTH_FILE.exists():
        return []
    try:
        data = json.loads(AUTH_FILE.read_text())
        if "users" in data:
            return data["users"]
        # Old single-user format — migrate to new format automatically.
        if data.get("password_hash"):
            users = [{"id": str(uuid.uuid4()),
                      "username": data.get("username", "admin"),
                      "password_hash": data["password_hash"],
                      "role": "admin"}]
            _save_users(users)
            return users
    except Exception:
        pass
    return []


def _save_users(users: list[dict]) -> None:
    ONDECK_HOME.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps({"users": users}, indent=2))


def _find_user(username: str) -> dict | None:
    for u in _load_users():
        if u["username"].lower() == username.lower():
            return u
    return None


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

@app.before_request
def _check_auth():
    # Sync API: authenticated by Bearer token, not session.
    if request.path.startswith("/sync/"):
        return
    # Always accessible.
    always_ok = {
        "public_home", "static",
        "ondeck_login", "ondeck_login_post", "ondeck_logout",
        "ondeck_setup", "ondeck_setup_post",
    }
    if request.endpoint in always_ok:
        return
    # No users configured yet — force first-time setup.
    if not _load_users():
        return redirect(url_for("ondeck_setup"))

    role = session.get("role")

    if role == "admin":
        return

    if role == "editor":
        admin_only = {
            "admin_panel", "admin_add_user", "admin_delete_user", "admin_change_role",
            "ondeck_settings", "ondeck_settings_save", "ondeck_settings_youtube_cookies",
        }
        if request.endpoint in admin_only:
            flash("Admin access required.", "error")
            return redirect(url_for("ondeck_dashboard"))
        return

    if role == "player":
        player_ok = {
            "ondeck_my_profile", "ondeck_my_profile_upload", "ondeck_my_profile_save",
            "ondeck_serve_audio",
            "ondeck_my_profile_change_password", "ondeck_my_profile_change_password_post",
            "ondeck_logout",
        }
        if request.endpoint not in player_ok:
            return redirect(url_for("ondeck_my_profile"))
        p = cfg.players.get(session.get("player_id"))
        reset_ok = {
            "ondeck_my_profile_change_password", "ondeck_my_profile_change_password_post",
            "ondeck_logout",
        }
        if p and p.get("force_reset") and request.endpoint not in reset_ok:
            return redirect(url_for("ondeck_my_profile_change_password"))
        return

    # Not logged in.
    player_routes = {"ondeck_my_profile", "ondeck_my_profile_upload", "ondeck_my_profile_save"}
    if request.endpoint in player_routes:
        return redirect(url_for("ondeck_login"))
    next_url = request.full_path if request.path not in ("/", "/ondeck") else None
    if next_url:
        return redirect(url_for("ondeck_login", next=next_url))
    return redirect(url_for("ondeck_login"))


@app.context_processor
def _inject_globals():
    role = session.get("role")
    current_player = None
    if role == "player":
        p = cfg.players.get(session.get("player_id"))
        if p:
            current_player = p
    return {
        "cloud_mode": CLOUD_MODE,
        "current_user": session.get("username") if role in ("admin", "editor") else None,
        "current_role": role,
        "current_player": current_player,
        "elevenlabs_ready": bool(ELEVENLABS_API_KEY),
    }


def _require_sync_token(f):
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
    try:
        r = rq.request(method, _audio_pi_url(path), timeout=15, **kwargs)
        return r.json(), r.status_code
    except rq.exceptions.ConnectionError:
        return {"error": "Audio Pi unreachable"}, 503
    except Exception as exc:
        return {"error": str(exc)}, 500


# ---------------------------------------------------------------------------
# Public homepage
# ---------------------------------------------------------------------------

@app.get("/")
def public_home():
    if session.get("role") in ("admin", "editor"):
        return redirect(url_for("ondeck_dashboard"))
    return render_template("public_home.html")


# ---------------------------------------------------------------------------
# OnDeck entry point  /ondeck  →  dashboard or login
# ---------------------------------------------------------------------------

@app.get("/ondeck")
def ondeck_home():
    if session.get("role") in ("admin", "editor"):
        return redirect(url_for("ondeck_dashboard"))
    return redirect(url_for("ondeck_login"))


# ---------------------------------------------------------------------------
# Auth  (/ondeck/login, /ondeck/logout, /ondeck/setup)
# ---------------------------------------------------------------------------

@app.get("/ondeck/login")
def ondeck_login():
    if session.get("role") in ("admin", "editor"):
        return redirect(url_for("ondeck_dashboard"))
    if session.get("role") == "player":
        return redirect(url_for("ondeck_my_profile"))
    return render_template("login.html")


@app.post("/ondeck/login")
def ondeck_login_post():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    remember = request.form.get("remember") == "on"

    # Try admin/editor users first.
    user = _find_user(username)
    if user and check_password_hash(user["password_hash"], password):
        session.permanent = remember
        session["user_id"] = user["id"]
        session["role"] = user["role"]
        session["username"] = user["username"]
        next_url = request.args.get("next") or url_for("ondeck_dashboard")
        if not next_url.startswith("/"):
            next_url = url_for("ondeck_dashboard")
        return redirect(next_url)

    # Try player credentials.
    for pid, player in cfg.players.items():
        pu = (player.get("player_username") or "").lower()
        if pu == username.lower() and player.get("player_password_hash"):
            if check_password_hash(player["player_password_hash"], password):
                session.permanent = True
                session["role"] = "player"
                session["player_id"] = pid
                return redirect(url_for("ondeck_my_profile"))

    flash("Incorrect username or password.", "error")
    return redirect(url_for("ondeck_login"))


@app.get("/ondeck/logout")
def ondeck_logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("ondeck_login"))


@app.get("/ondeck/setup")
def ondeck_setup():
    if _load_users():
        return redirect(url_for("ondeck_dashboard"))
    return render_template("setup.html")


@app.post("/ondeck/setup")
def ondeck_setup_post():
    if _load_users():
        return redirect(url_for("ondeck_dashboard"))

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    confirm  = request.form.get("confirm", "")

    if not username:
        flash("Username is required.", "error")
        return redirect(url_for("ondeck_setup"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("ondeck_setup"))
    if password != confirm:
        flash("Passwords do not match.", "error")
        return redirect(url_for("ondeck_setup"))

    new_user = {"id": str(uuid.uuid4()), "username": username,
                "password_hash": generate_password_hash(password), "role": "admin"}
    _save_users([new_user])

    session.permanent = True
    session["user_id"] = new_user["id"]
    session["role"] = "admin"
    session["username"] = username
    flash("Account created. Welcome to OnDeck!", "success")
    return redirect(url_for("ondeck_dashboard"))


# ---------------------------------------------------------------------------
# Admin panel  /admin  — user management (admin role only)
# ---------------------------------------------------------------------------

@app.get("/admin")
def admin_panel():
    users = _load_users()
    return render_template("admin.html", users=users)


@app.post("/admin/users/add")
def admin_add_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role     = request.form.get("role", "editor")

    if not username or len(password) < 8:
        flash("Username required and password must be at least 8 characters.", "error")
        return redirect(url_for("admin_panel"))
    if role not in ("admin", "editor"):
        role = "editor"
    users = _load_users()
    if any(u["username"].lower() == username.lower() for u in users):
        flash(f"Username '{username}' already exists.", "error")
        return redirect(url_for("admin_panel"))
    users.append({"id": str(uuid.uuid4()), "username": username,
                  "password_hash": generate_password_hash(password), "role": role})
    _save_users(users)
    flash(f"Added {role} '{username}'.", "success")
    return redirect(url_for("admin_panel"))


@app.post("/admin/users/<uid>/delete")
def admin_delete_user(uid: str):
    users = _load_users()
    target = next((u for u in users if u["id"] == uid), None)
    if not target:
        flash("User not found.", "error")
        return redirect(url_for("admin_panel"))
    if target["id"] == session.get("user_id"):
        flash("You can't delete your own account.", "error")
        return redirect(url_for("admin_panel"))
    admins = [u for u in users if u["role"] == "admin"]
    if target["role"] == "admin" and len(admins) <= 1:
        flash("Can't delete the last admin account.", "error")
        return redirect(url_for("admin_panel"))
    _save_users([u for u in users if u["id"] != uid])
    flash(f"Deleted '{target['username']}'.", "success")
    return redirect(url_for("admin_panel"))


@app.post("/admin/users/<uid>/role")
def admin_change_role(uid: str):
    new_role = request.form.get("role", "editor")
    if new_role not in ("admin", "editor"):
        new_role = "editor"
    users = _load_users()
    target = next((u for u in users if u["id"] == uid), None)
    if not target:
        flash("User not found.", "error")
        return redirect(url_for("admin_panel"))
    admins = [u for u in users if u["role"] == "admin"]
    if target["role"] == "admin" and new_role != "admin" and len(admins) <= 1:
        flash("Can't demote the last admin.", "error")
        return redirect(url_for("admin_panel"))
    target["role"] = new_role
    _save_users(users)
    flash(f"Updated '{target['username']}' to {new_role}.", "success")
    return redirect(url_for("admin_panel"))


# ---------------------------------------------------------------------------
# Audio file serving
# ---------------------------------------------------------------------------

@app.get("/ondeck/audio/<path:filename>")
def ondeck_serve_audio(filename: str):
    safe = Path(filename).name
    path = MUSIC_DIR / safe
    if not path.exists():
        abort(404)
    return send_file(str(path))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/ondeck/dashboard")
def ondeck_dashboard():
    status, _ = _proxy("GET", "/status")
    players = cfg.players_by_jersey()
    return render_template("index.html", status=status, system=cfg.system,
                           players=players, songs=cfg.songs)


# ---------------------------------------------------------------------------
# Music library
# ---------------------------------------------------------------------------

@app.get("/ondeck/library")
def ondeck_library():
    return render_template("library.html", songs=cfg.songs)


@app.post("/ondeck/library/upload")
def ondeck_library_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("ondeck_library"))

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
    return redirect(url_for("ondeck_library"))


@app.post("/ondeck/library/import")
def ondeck_library_import():
    url = request.form.get("url", "").strip()
    if not url:
        flash("No URL provided.", "error")
        return redirect(url_for("ondeck_library"))

    if CLOUD_MODE:
        filename, error = _yt_dlp_import(url)
        if error:
            flash(f"Import failed: {error}", "error")
            return redirect(url_for("ondeck_library"))
    else:
        data, status = _proxy("POST", "/import", json={"url": url})
        if status != 200:
            flash(f"Import failed: {data.get('error', 'unknown')}", "error")
            return redirect(url_for("ondeck_library"))
        filename = data.get("filename")

    if filename:
        existing = {s["filename"] for s in cfg.songs.values()}
        if filename not in existing:
            cfg.add_song(filename, Path(filename).stem.replace("_", " "))
        flash(f'Imported "{filename}".', "success")
    else:
        flash("Import completed.", "success")
    return redirect(url_for("ondeck_library"))


@app.get("/ondeck/library/<sid>/edit")
def ondeck_library_edit(sid: str):
    song = cfg.songs.get(sid)
    if not song:
        abort(404)
    return render_template("library_edit.html", sid=sid, song=song)


@app.post("/ondeck/library/<sid>/save")
def ondeck_library_save(sid: str):
    with cfg._lock:
        song = cfg.songs.get(sid)
        if not song:
            abort(404)
        song["display_name"] = (request.form.get("display_name", song["display_name"]).strip()
                                or song["display_name"])
        song["start_ms"] = _form_ms("start_ms", 0)
        song["end_ms"] = _form_ms_or_none("end_ms")
        cfg.save()
    flash("Saved.", "success")
    return redirect(url_for("ondeck_library_edit", sid=sid))


@app.post("/ondeck/library/<sid>/delete")
def ondeck_library_delete(sid: str):
    with cfg._lock:
        song = cfg.songs.pop(sid, None)
        for p in cfg.players.values():
            if p.get("walkup_song_id") == sid:
                p["walkup_song_id"] = None
        cfg.save()
    if song:
        (MUSIC_DIR / song["filename"]).unlink(missing_ok=True)
        flash(f"Deleted '{song['display_name']}'.", "success")
    return redirect(url_for("ondeck_library"))


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------

@app.get("/ondeck/players")
def ondeck_players():
    return render_template("players.html",
                           players=cfg.players_by_jersey(), songs=cfg.songs)


@app.get("/ondeck/players/new")
def ondeck_players_new():
    return render_template("player_edit.html",
                           pid=None, player=None, songs=cfg.songs)


@app.post("/ondeck/players")
def ondeck_players_create():
    first = request.form.get("first_name", "").strip()
    last  = request.form.get("last_name", "").strip()
    try:
        jersey = int(request.form.get("jersey", ""))
    except ValueError:
        flash("Jersey must be a number.", "error")
        return redirect(url_for("ondeck_players_new"))
    if not first or not last:
        flash("First and last name are required.", "error")
        return redirect(url_for("ondeck_players_new"))

    pid = cfg.add_player(jersey, first, last)
    flash(f"Added #{jersey} {first} {last}.", "success")
    return redirect(url_for("ondeck_players_edit", pid=pid))


@app.get("/ondeck/players/<pid>/edit")
def ondeck_players_edit(pid: str):
    player = cfg.players.get(pid)
    if not player:
        abort(404)
    return render_template("player_edit.html",
                           pid=pid, player=player, songs=cfg.songs)


@app.post("/ondeck/players/<pid>/save")
def ondeck_players_save(pid: str):
    with cfg._lock:
        player = cfg.players.get(pid)
        if not player:
            abort(404)
        try:
            player["jersey"] = int(request.form.get("jersey", player["jersey"]))
        except (ValueError, TypeError):
            pass
        player["first_name"] = (request.form.get("first_name", player["first_name"]).strip()
                                 or player["first_name"])
        player["last_name"]  = (request.form.get("last_name", player["last_name"]).strip()
                                 or player["last_name"])
        player["walkup_song_id"]       = request.form.get("walkup_song_id") or None
        player["announcement_start_ms"] = _form_ms("ann_start_ms", 0)
        player["announcement_end_ms"]   = _form_ms_or_none("ann_end_ms")
        player["music_cue_ms"]          = _form_ms("music_cue_ms", 0)

        sid = player.get("walkup_song_id")
        if sid and sid in cfg.songs:
            cfg.songs[sid]["start_ms"] = _form_ms("song_start_ms", 0)
            cfg.songs[sid]["end_ms"]   = _form_ms_or_none("song_end_ms")

        cfg.save()

    flash("Saved.", "success")
    return redirect(url_for("ondeck_players_edit", pid=pid))


@app.post("/ondeck/players/<pid>/delete")
def ondeck_players_delete(pid: str):
    with cfg._lock:
        player = cfg.players.pop(pid, None)
        cfg.save()
    if player:
        flash(f"Deleted #{player['jersey']} {player['first_name']} {player['last_name']}.", "success")
    return redirect(url_for("ondeck_players"))


@app.post("/ondeck/players/<pid>/record")
def ondeck_players_record(pid: str):
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
        player["announcement_file"]     = filename
        player["announcement_start_ms"] = 0
        player["announcement_end_ms"]   = None
        cfg.save()

    try:
        rq.post(_audio_pi_url("/upload"),
                files={"file": (filename, blob, "audio/webm")}, timeout=15)
    except Exception:
        pass

    return jsonify(ok=True, filename=filename)


@app.post("/ondeck/players/<pid>/set-credentials")
def ondeck_player_set_credentials(pid: str):
    player = cfg.players.get(pid)
    if not player:
        abort(404)
    username = request.form.get("player_username", "").strip()
    password = request.form.get("player_password", "")
    with cfg._lock:
        if username:
            player["player_username"] = username
        if password:
            if len(password) < 6:
                flash("Password must be at least 6 characters.", "error")
                return redirect(url_for("ondeck_players_edit", pid=pid))
            player["player_password_hash"] = generate_password_hash(password)
            player["force_reset"] = True
        cfg.save()
    flash("Player login updated.", "success")
    return redirect(url_for("ondeck_players_edit", pid=pid))


@app.post("/ondeck/players/<pid>/generate-announcement")
def ondeck_player_generate_announcement(pid: str):
    player = cfg.players.get(pid)
    if not player:
        abort(404)
    text = (f"Now batting, number {player['jersey']}, "
            f"{player['first_name']} {player['last_name']}.")
    audio, error = _elevenlabs_announce(text)
    if error:
        flash(f"TTS failed: {error}", "error")
        return redirect(url_for("ondeck_players_edit", pid=pid))
    filename = f"ann_{pid}_tts.mp3"
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    (MUSIC_DIR / filename).write_bytes(audio)
    with cfg._lock:
        player["announcement_file"]     = filename
        player["announcement_start_ms"] = 0
        player["announcement_end_ms"]   = None
        cfg.save()
    flash("AI announcement generated!", "success")
    return redirect(url_for("ondeck_players_edit", pid=pid))


# ---------------------------------------------------------------------------
# Player self-service portal
# ---------------------------------------------------------------------------

@app.get("/ondeck/my-profile/change-password")
def ondeck_my_profile_change_password():
    pid = session.get("player_id")
    player = cfg.players.get(pid)
    if not player:
        return redirect(url_for("ondeck_login"))
    return render_template("change_password.html", player=player)


@app.post("/ondeck/my-profile/change-password")
def ondeck_my_profile_change_password_post():
    pid    = session.get("player_id")
    new_pw  = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    if len(new_pw) < 6:
        flash("Password must be at least 6 characters.", "error")
        return redirect(url_for("ondeck_my_profile_change_password"))
    if new_pw != confirm:
        flash("Passwords do not match.", "error")
        return redirect(url_for("ondeck_my_profile_change_password"))
    with cfg._lock:
        player = cfg.players.get(pid)
        if not player:
            abort(403)
        player["player_password_hash"] = generate_password_hash(new_pw)
        player["force_reset"] = False
        cfg.save()
    flash("Password updated!", "success")
    return redirect(url_for("ondeck_my_profile"))


@app.get("/ondeck/my-profile")
def ondeck_my_profile():
    pid = session.get("player_id")
    player = cfg.players.get(pid)
    if not player:
        session.pop("player_id", None)
        session.pop("role", None)
        return redirect(url_for("ondeck_login"))
    song = cfg.songs.get(player["walkup_song_id"]) if player.get("walkup_song_id") else None
    return render_template("my_profile.html", pid=pid, player=player, song=song)


@app.post("/ondeck/my-profile/upload")
def ondeck_my_profile_upload():
    pid = session.get("player_id")
    player = cfg.players.get(pid)
    if not player:
        abort(403)
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("ondeck_my_profile"))
    name = Path(f.filename).name
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    f.save(str(MUSIC_DIR / name))
    existing = {s["filename"]: sid for sid, s in cfg.songs.items()}
    song_id = existing.get(name) or cfg.add_song(
        name, Path(name).stem.replace("_", " ").replace("-", " ")
    )
    with cfg._lock:
        player["walkup_song_id"] = song_id
        cfg.save()
    flash("Song uploaded!", "success")
    return redirect(url_for("ondeck_my_profile"))


@app.post("/ondeck/my-profile/save")
def ondeck_my_profile_save():
    pid = session.get("player_id")
    with cfg._lock:
        player = cfg.players.get(pid)
        if not player:
            abort(403)
        sid = player.get("walkup_song_id")
        if sid and sid in cfg.songs:
            cfg.songs[sid]["start_ms"] = _form_ms("start_ms", 0)
            cfg.songs[sid]["end_ms"]   = _form_ms_or_none("end_ms")
        cfg.save()
    flash("Saved!", "success")
    return redirect(url_for("ondeck_my_profile"))


# ---------------------------------------------------------------------------
# Audio Pi proxy endpoints (called by browser JS)
# ---------------------------------------------------------------------------

@app.get("/ondeck/api/status")
def ondeck_api_status():
    data, code = _proxy("GET", "/status")
    return jsonify(data), code


@app.post("/ondeck/api/preview")
def ondeck_api_preview():
    body = request.get_json(force=True) or {}
    data, code = _proxy("POST", "/queue", json=body)
    if code != 200:
        return jsonify(data), code
    data, code = _proxy("POST", "/play")
    return jsonify(data), code


@app.post("/ondeck/api/stop")
def ondeck_api_stop():
    data, code = _proxy("POST", "/stop")
    return jsonify(data), code


@app.post("/ondeck/api/fade")
def ondeck_api_fade():
    body = request.get_json(force=True, silent=True) or {}
    data, code = _proxy("POST", "/fade", json=body)
    return jsonify(data), code


@app.post("/ondeck/api/volume")
def ondeck_api_volume():
    body = request.get_json(force=True) or {}
    data, code = _proxy("POST", "/volume", json=body)
    return jsonify(data), code


# ---------------------------------------------------------------------------
# Settings (admin only — enforced in _check_auth)
# ---------------------------------------------------------------------------

@app.get("/ondeck/settings")
def ondeck_settings():
    return render_template("settings.html", system=cfg.system,
                           has_yt_cookies=COOKIES_FILE.exists())


@app.post("/ondeck/settings/save")
def ondeck_settings_save():
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
    return redirect(url_for("ondeck_settings"))


@app.post("/ondeck/settings/youtube-cookies")
def ondeck_settings_youtube_cookies():
    text = request.form.get("cookies", "").strip()
    if text:
        ONDECK_HOME.mkdir(parents=True, exist_ok=True)
        COOKIES_FILE.write_text(text)
        flash("YouTube cookies saved.", "success")
    else:
        COOKIES_FILE.unlink(missing_ok=True)
        flash("YouTube cookies cleared.", "success")
    return redirect(url_for("ondeck_settings"))


# ---------------------------------------------------------------------------
# Sync API — Pi polls these to stay in sync with cloud config
# ---------------------------------------------------------------------------

@app.get("/sync/config")
@_require_sync_token
def sync_config():
    return jsonify(cfg.data)


@app.get("/sync/files")
@_require_sync_token
def sync_list_files():
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
    safe = Path(filename).name
    path = MUSIC_DIR / safe
    if not path.exists():
        abort(404)
    return send_file(str(path))


@app.post("/sync/ping")
@_require_sync_token
def sync_ping():
    body  = request.get_json(force=True) or {}
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

def _elevenlabs_announce(text: str) -> tuple[bytes | None, str | None]:
    if not ELEVENLABS_API_KEY:
        return None, "ELEVENLABS_API_KEY not configured"
    try:
        r = rq.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": "eleven_monolingual_v1",
                "voice_settings": {"stability": 0.55, "similarity_boost": 0.75},
            },
            timeout=30,
        )
        if r.status_code != 200:
            return None, f"ElevenLabs {r.status_code}: {r.text[:300]}"
        return r.content, None
    except Exception as exc:
        return None, str(exc)


def _yt_dlp_import(url: str) -> tuple[str | None, str | None]:
    import subprocess
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(MUSIC_DIR / "%(title)s.%(ext)s")

    ffmpeg_location = None
    try:
        import imageio_ffmpeg
        ffmpeg_location = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass

    cmd = [
        "yt-dlp", "-x", "--audio-format", "mp3", "--no-playlist",
        "--print", "after_move:filepath", "-o", out_tmpl,
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
