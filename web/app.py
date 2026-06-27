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
from datetime import datetime, timedelta, timezone
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
# ElevenLabs key/voice are normally configured in Settings → Announcer Voice;
# these env vars act as fallbacks when the config fields are left blank.
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")

AUTH_FILE      = ONDECK_HOME / "auth.json"
COOKIES_FILE   = ONDECK_HOME / "youtube_cookies.txt"
HOMEPAGE_FILE  = ONDECK_HOME / "homepage.json"

_HOMEPAGE_DEFAULTS: dict = {
    "tagline":     "Builder, coach, and creator. I make tools that solve real problems.",
    "bio_1":       "",
    "bio_2":       "",
    "bio_3":       "",
    "ondeck_desc": "Baseball walk-up music system. Manage player walk-up songs, generate stadium announcements, and trigger live playback from the dugout via Stream Deck.",
}

def _load_homepage() -> dict:
    if HOMEPAGE_FILE.exists():
        try:
            data = json.loads(HOMEPAGE_FILE.read_text())
            return {**_HOMEPAGE_DEFAULTS, **data}
        except Exception:
            pass
    return dict(_HOMEPAGE_DEFAULTS)

def _save_homepage(data: dict) -> None:
    HOMEPAGE_FILE.write_text(json.dumps(data, indent=2))

app = Flask(__name__)
app.secret_key = os.environ.get("ONDECK_SECRET", "ondeck-dev-secret-change-me")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

cfg = ConfigManager()


# Custom Jinja filters
@app.template_filter("datetime")
def format_datetime(ms: int) -> str:
    """Format milliseconds since epoch as a human-readable datetime."""
    if not ms:
        return "unknown"
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%b %d, %Y %I:%M %p")


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
# Notifications — coach-facing activity log of player-made changes
# ---------------------------------------------------------------------------

NOTIFICATIONS_CAP = 200  # keep the newest N; older entries roll off


def _add_notification(pid: str, player: dict, action: str, detail: str = "") -> None:
    """Record a player-made change for the coach to review (newest first).

    Called while holding cfg._lock by the player self-service routes.
    """
    notes = cfg.data.setdefault("notifications", [])
    notes.insert(0, {
        "id": uuid.uuid4().hex,
        "ts": datetime.now(timezone.utc).isoformat(),
        "player_id": pid,
        "jersey": player.get("jersey"),
        "player_name": f"{player.get('first_name', '')} {player.get('last_name', '')}".strip(),
        "action": action,
        "detail": detail,
        "seen": False,
    })
    del notes[NOTIFICATIONS_CAP:]


def _unread_notification_count() -> int:
    return sum(1 for n in cfg.data.get("notifications", []) if not n.get("seen"))


def _check_song_duplicates(pid: str, song_id: str, song_type: str) -> tuple[list[str], list[str]]:
    """Check if a song is already assigned to other players.

    Returns (duplicate_player_ids, duplicate_player_names) of players who have this song assigned.
    """
    duplicate_ids = []
    duplicate_names = []

    for other_pid, other_player in cfg.players.items():
        if other_pid == pid:
            continue

        if song_type == "walkup" and song_id == other_player.get("walkup_song_id"):
            duplicate_ids.append(other_pid)
            duplicate_names.append(f"{other_player.get('first_name')} {other_player.get('last_name')}".strip())
        elif song_type == "warmup" and song_id == other_player.get("pitching_warmup_song_id"):
            duplicate_ids.append(other_pid)
            duplicate_names.append(f"{other_player.get('first_name')} {other_player.get('last_name')}".strip())
        elif song_type == "midgame" and song_id == other_player.get("midgame_song_id"):
            duplicate_ids.append(other_pid)
            duplicate_names.append(f"{other_player.get('first_name')} {other_player.get('last_name')}".strip())

    return duplicate_ids, duplicate_names


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

@app.before_request
def _check_auth(allowed_roles=None):
    # Sync API: authenticated by Bearer token, not session.
    if request.path.startswith("/sync/"):
        return
    # Always accessible.
    always_ok = {
        "public_home", "static",
        "ondeck_login", "ondeck_login_post", "ondeck_logout",
        "ondeck_setup", "ondeck_setup_post", "player_signup", "player_signup_post",
    }
    if request.endpoint in always_ok:
        return
    # No users configured yet — force first-time setup.
    if not _load_users():
        return redirect(url_for("ondeck_setup"))

    role = session.get("role")

    # Check if specific roles are required for this route
    if allowed_roles and role not in allowed_roles:
        flash("Access denied.", "error")
        return redirect(url_for("ondeck_dashboard"))

    if role == "admin":
        return

    if role == "editor":
        admin_only = {
            "admin_panel", "admin_add_user", "admin_delete_user", "admin_change_role",
            "admin_homepage", "admin_homepage_save",
            "ondeck_settings", "ondeck_settings_save", "ondeck_settings_youtube_cookies",
        }
        if request.endpoint in admin_only:
            flash("Admin access required.", "error")
            return redirect(url_for("ondeck_dashboard"))
        return

    if role == "player":
        player_ok = {
            "ondeck_my_profile", "ondeck_my_profile_upload", "ondeck_my_profile_save",
            "ondeck_player_upload",
            "ondeck_carousel_add", "ondeck_carousel_remove", "ondeck_carousel_activate", "ondeck_carousel_deactivate",
            "ondeck_trim_save",
            "ondeck_serve_audio",
            "ondeck_my_profile_change_password", "ondeck_my_profile_change_password_post",
            "ondeck_team_roster",
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
        "elevenlabs_ready": _elevenlabs_ready(),
        "unread_notifications": _unread_notification_count() if role in ("admin", "editor") else 0,
    }


def _bearer_token() -> str:
    """The token from an ``Authorization: Bearer <token>`` header, or ""."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return ""


def _valid_sync_token(token: str) -> bool:
    """A token is valid if it's the global sync token or a live device token."""
    if SYNC_TOKEN and token == SYNC_TOKEN:
        return True
    return cfg.device_for_token(token) is not None


def _require_sync_token(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        # Only enforce when a global token is configured (cloud). In that mode a
        # request is authorized by the global token OR any non-revoked
        # per-device token minted through pairing. With no global token set
        # (local single-Pi dev), the sync endpoints stay open as before.
        if SYNC_TOKEN and not _valid_sync_token(_bearer_token()):
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
    return render_template("public_home.html", hp=_load_homepage())


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
# Player signup with access code (public, team-based)
# ---------------------------------------------------------------------------

@app.get("/signup/<code>")
def player_signup(code: str):
    link = cfg.get_signup_link(code)
    if not link:
        flash("Invalid or expired signup link.", "error")
        return redirect(url_for("ondeck_login"))

    return render_template("player_signup.html", code=code, team_ids=link["team_ids"],
                          teams={tid: cfg.teams[tid] for tid in link["team_ids"]})


@app.post("/signup/<code>")
def player_signup_post(code: str):
    link = cfg.get_signup_link(code)
    if not link:
        flash("Invalid or expired signup link.", "error")
        return redirect(url_for("ondeck_login"))

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    confirm  = request.form.get("confirm", "")

    if not username:
        flash("Username is required.", "error")
        return redirect(url_for("player_signup", code=code))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("player_signup", code=code))
    if password != confirm:
        flash("Passwords do not match.", "error")
        return redirect(url_for("player_signup", code=code))

    users = _load_users()
    if any(u["username"].lower() == username.lower() for u in users):
        flash(f"Username '{username}' already exists.", "error")
        return redirect(url_for("player_signup", code=code))

    new_user = {"id": str(uuid.uuid4()), "username": username,
                "password_hash": generate_password_hash(password), "role": "player"}
    _save_users(users + [new_user])

    session.permanent = True
    session["user_id"] = new_user["id"]
    session["role"] = "player"
    session["username"] = username
    flash("Account created. Welcome to the team!", "success")
    return redirect(url_for("ondeck_dashboard"))


# ---------------------------------------------------------------------------
# Admin panel  /admin  — user management (admin role only)
# ---------------------------------------------------------------------------

@app.get("/admin")
def admin_panel():
    users = _load_users()
    players = cfg.players_by_jersey()
    teams = cfg.teams
    return render_template("admin.html", users=users, players=players, teams=teams)


@app.post("/admin/players/<pid>/teams")
def admin_update_player_teams(pid: str):
    """Update team assignments for a player via AJAX."""
    if pid not in cfg.players:
        return jsonify(error="player not found"), 404
    team_ids = request.form.getlist("team_ids")
    cfg.set_player_teams(pid, team_ids)
    return jsonify(ok=True)


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
# Homepage editor  /admin/homepage  (admin only)
# ---------------------------------------------------------------------------

@app.get("/admin/homepage")
def admin_homepage():
    return render_template("admin_homepage.html", hp=_load_homepage())


@app.post("/admin/homepage/save")
def admin_homepage_save():
    hp = {
        "tagline":     request.form.get("tagline", "").strip(),
        "bio_1":       request.form.get("bio_1", "").strip(),
        "bio_2":       request.form.get("bio_2", "").strip(),
        "bio_3":       request.form.get("bio_3", "").strip(),
        "ondeck_desc": request.form.get("ondeck_desc", "").strip(),
    }
    _save_homepage(hp)
    flash("Homepage updated.", "success")
    return redirect(url_for("admin_homepage"))


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
    return render_template("library.html", songs=cfg.songs, cfg=cfg)


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
    teams = [(tid, t) for tid, t in cfg.teams.items()]
    teams.sort(key=lambda kv: kv[1].get("name", ""))
    return render_template("player_edit.html",
                           pid=None, player=None, songs=cfg.songs, teams=teams)


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

    team_ids = request.form.getlist("team_ids")
    pid = cfg.add_player(jersey, first, last, team_ids=team_ids)
    flash(f"Added #{jersey} {first} {last}.", "success")
    return redirect(url_for("ondeck_players_edit", pid=pid))


@app.get("/ondeck/players/<pid>/edit")
def ondeck_players_edit(pid: str):
    player = cfg.players.get(pid)
    if not player:
        abort(404)
    teams = [(tid, t) for tid, t in cfg.teams.items()]
    teams.sort(key=lambda kv: kv[1].get("name", ""))
    return render_template("player_edit.html",
                           pid=pid, player=player, songs=cfg.songs, teams=teams,
                           default_announcement=_default_announcement(player))


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
        player["pitching_warmup_song_id"] = request.form.get("pitching_warmup_song_id") or None
        player["midgame_song_id"]      = request.form.get("midgame_song_id") or None
        player["announcement_start_ms"] = _form_ms("ann_start_ms", 0)
        player["announcement_end_ms"]   = _form_ms_or_none("ann_end_ms")
        player["music_cue_ms"]          = _form_ms("music_cue_ms", 0)

        # Update team assignments
        team_ids = request.form.getlist("team_ids")
        player["team_ids"] = [tid for tid in team_ids if tid in cfg.teams]

        # Update walk-up song trim
        sid = player.get("walkup_song_id")
        if sid and sid in cfg.songs:
            cfg.songs[sid]["start_ms"] = _form_ms("song_start_ms", 0)
            cfg.songs[sid]["end_ms"]   = _form_ms_or_none("song_end_ms")

        # Update pitching warm-up song trim
        warmup_id = player.get("pitching_warmup_song_id")
        if warmup_id and warmup_id in cfg.songs:
            cfg.songs[warmup_id]["start_ms"] = _form_ms("warmup_start_ms", 0)
            cfg.songs[warmup_id]["end_ms"]   = _form_ms_or_none("warmup_end_ms")

        # Update mid-game song trim
        midgame_id = player.get("midgame_song_id")
        if midgame_id and midgame_id in cfg.songs:
            cfg.songs[midgame_id]["start_ms"] = _form_ms("midgame_start_ms", 0)
            cfg.songs[midgame_id]["end_ms"]   = _form_ms_or_none("midgame_end_ms")

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
    # Editable text from the player editor; fall back to the saved template.
    text = (request.form.get("text", "").strip()
            or _default_announcement(player))

    audio, error = _elevenlabs_announce(text)
    if error:
        flash(f"TTS failed: {error}", "error")
        return redirect(url_for("ondeck_players_edit", pid=pid))

    filename = f"ann_{pid}_tts.mp3"
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    (MUSIC_DIR / filename).write_bytes(audio)

    with cfg._lock:
        old = player.get("announcement_file")
        player["announcement_file"]     = filename
        player["announcement_text"]     = text
        player["announcement_start_ms"] = 0
        player["announcement_end_ms"]   = None
        cfg.save()

    # Drop a previous take under a different name (e.g. an old webm recording)
    # so it doesn't linger in the music dir and the sync manifest.
    if old and old != filename:
        (MUSIC_DIR / old).unlink(missing_ok=True)

    try:
        rq.post(_audio_pi_url("/upload"),
                files={"file": (filename, audio, "audio/mpeg")}, timeout=15)
    except Exception:
        pass

    flash("AI announcement generated!", "success")
    return redirect(url_for("ondeck_players_edit", pid=pid))


@app.post("/ondeck/api/elevenlabs/voices")
def ondeck_api_elevenlabs_voices():
    """List the account's voices so an admin can pick one in Settings.

    Accepts an optional ``key`` in the JSON body so a just-typed (unsaved) key
    can be verified before saving.
    """
    body = request.get_json(silent=True) or {}
    key = (body.get("key") or "").strip() or _elevenlabs_key()
    if not key:
        return jsonify(error="Enter an API key first."), 400
    try:
        r = rq.get("https://api.elevenlabs.io/v1/voices",
                   headers={"xi-api-key": key}, timeout=15)
    except Exception as exc:
        return jsonify(error=f"Could not reach ElevenLabs: {exc}"), 502
    if r.status_code != 200:
        return jsonify(error=_elevenlabs_error(r)), 502
    voices = [{"voice_id": v.get("voice_id"), "name": v.get("name", "")}
              for v in (r.json().get("voices") or [])]
    return jsonify(voices=voices)


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
    walkup_song = cfg.songs.get(player["walkup_song_id"]) if player.get("walkup_song_id") else None
    warmup_song = cfg.songs.get(player["pitching_warmup_song_id"]) if player.get("pitching_warmup_song_id") else None
    midgame_song = cfg.songs.get(player["midgame_song_id"]) if player.get("midgame_song_id") else None

    # Get player's team names
    player_teams = [cfg.teams.get(tid) for tid in player.get("team_ids", [])]
    player_teams = [t for t in player_teams if t]  # Filter out None values

    return render_template("my_profile.html", pid=pid, player=player,
                          walkup_song=walkup_song, warmup_song=warmup_song,
                          midgame_song=midgame_song, songs=cfg.songs,
                          player_teams=player_teams)


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
        replacing = bool(player.get("walkup_song_id"))
        player["walkup_song_id"] = song_id
        _add_notification(pid, player,
                          "replaced their walk-up song" if replacing
                          else "added a walk-up song",
                          detail=Path(name).stem.replace("_", " ").replace("-", " "))
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

        # Update walk-up song selection and trim
        new_walkup = request.form.get("walkup_song_id")
        if new_walkup != player.get("walkup_song_id"):
            player["walkup_song_id"] = new_walkup if new_walkup else None
            _add_notification(pid, player, "changed their walk-up song")
            # Check for duplicate song assignment
            if new_walkup:
                dups, dup_names = _check_song_duplicates(pid, new_walkup, "walkup")
                if dups:
                    song = cfg.songs.get(new_walkup)
                    song_name = song.get("display_name", "Unknown") if song else "Unknown"
                    _add_notification(pid, player,
                                    f"chose a song already used by {', '.join(dup_names)}",
                                    detail=f"Walk-up: {song_name}")

        sid = player.get("walkup_song_id")
        if sid and sid in cfg.songs:
            cfg.songs[sid]["start_ms"] = _form_ms("walkup_start_ms", 0)
            cfg.songs[sid]["end_ms"]   = _form_ms_or_none("walkup_end_ms")

        # Update warm-up song selection and trim
        new_warmup = request.form.get("warmup_song_id")
        if new_warmup != player.get("pitching_warmup_song_id"):
            player["pitching_warmup_song_id"] = new_warmup if new_warmup else None
            _add_notification(pid, player, "changed their warm-up song")
            # Check for duplicate song assignment
            if new_warmup:
                dups, dup_names = _check_song_duplicates(pid, new_warmup, "warmup")
                if dups:
                    song = cfg.songs.get(new_warmup)
                    song_name = song.get("display_name", "Unknown") if song else "Unknown"
                    _add_notification(pid, player,
                                    f"chose a song already used by {', '.join(dup_names)}",
                                    detail=f"Warm-up: {song_name}")

        warmup_id = player.get("pitching_warmup_song_id")
        if warmup_id and warmup_id in cfg.songs:
            cfg.songs[warmup_id]["start_ms"] = _form_ms("warmup_start_ms", 0)
            cfg.songs[warmup_id]["end_ms"]   = _form_ms_or_none("warmup_end_ms")

        # Update mid-game song selection and trim
        new_midgame = request.form.get("midgame_song_id")
        if new_midgame != player.get("midgame_song_id"):
            player["midgame_song_id"] = new_midgame if new_midgame else None
            _add_notification(pid, player, "changed their mid-inning song")
            # Check for duplicate song assignment
            if new_midgame:
                dups, dup_names = _check_song_duplicates(pid, new_midgame, "midgame")
                if dups:
                    song = cfg.songs.get(new_midgame)
                    song_name = song.get("display_name", "Unknown") if song else "Unknown"
                    _add_notification(pid, player,
                                    f"chose a song already used by {', '.join(dup_names)}",
                                    detail=f"Mid-inning: {song_name}")

        midgame_id = player.get("midgame_song_id")
        if midgame_id and midgame_id in cfg.songs:
            cfg.songs[midgame_id]["start_ms"] = _form_ms("midgame_start_ms", 0)
            cfg.songs[midgame_id]["end_ms"]   = _form_ms_or_none("midgame_end_ms")

        # Players can set the music cue only when they have an announcement.
        if player.get("announcement_file") and "music_cue_ms" in request.form:
            player["music_cue_ms"] = _form_ms("music_cue_ms", 0)

        cfg.save()
    flash("Saved!", "success")
    return redirect(url_for("ondeck_my_profile"))


@app.post("/ondeck/player-upload")
def ondeck_player_upload():
    """AJAX: Handle player music uploads for walk-up, warm-up, and mid-inning songs."""
    pid = session.get("player_id")
    player = cfg.players.get(pid)
    if not player:
        return {"error": "Unauthorized"}, 403

    f = request.files.get("file")
    if not f or not f.filename:
        return {"error": "No file selected"}, 400

    song_type = request.form.get("song_type", "walkup")
    display_name = request.form.get("display_name", "")

    if not display_name:
        return {"error": "No display name provided"}, 400

    filename = Path(f.filename).name
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    f.save(str(MUSIC_DIR / filename))

    # Check if this filename already exists in config
    existing = {s["filename"]: sid for sid, s in cfg.songs.items()}
    if filename in existing:
        song_id = existing[filename]
        new_song = False
    else:
        song_id = cfg.add_song(filename, display_name)
        new_song = True

    # Update the song display name (might differ from filename)
    with cfg._lock:
        cfg.songs[song_id]["display_name"] = display_name
        cfg.save()

    return {
        "success": True,
        "song_id": song_id,
        "new_song": new_song,
        "filename": filename
    }


@app.post("/ondeck/carousel/add/<song_type>")
def ondeck_carousel_add(song_type: str):
    """AJAX: Add a song to player's carousel (walkup/warmup)."""
    pid = session.get("player_id")
    if not pid:
        return {"error": "Unauthorized"}, 403
    song_id = request.form.get("song_id")
    if not song_id or song_id not in cfg.songs:
        return {"error": "Invalid song"}, 400

    with cfg._lock:
        player = cfg.players.get(pid)
        if not player:
            return {"error": "Player not found"}, 404

        if song_type == "walkup":
            cfg.add_player_walkup_song(pid, song_id)
        elif song_type == "warmup":
            cfg.add_player_warmup_song(pid, song_id)
        else:
            return {"error": "Invalid song type"}, 400

    return {"success": True}


@app.post("/ondeck/carousel/remove/<song_type>/<song_id>")
def ondeck_carousel_remove(song_type: str, song_id: str):
    """AJAX: Remove a song from player's carousel."""
    pid = session.get("player_id")
    if not pid:
        return {"error": "Unauthorized"}, 403

    with cfg._lock:
        player = cfg.players.get(pid)
        if not player:
            return {"error": "Player not found"}, 404

        if song_type == "walkup":
            cfg.remove_player_walkup_song(pid, song_id)
        elif song_type == "warmup":
            cfg.remove_player_warmup_song(pid, song_id)
        else:
            return {"error": "Invalid song type"}, 400

    return {"success": True}


@app.post("/ondeck/carousel/activate/<song_type>/<song_id>")
def ondeck_carousel_activate(song_type: str, song_id: str):
    """AJAX: Set a carousel song as the active/current song.

    If activating a base song, automatically creates or uses the player's variant.
    """
    pid = session.get("player_id")
    if not pid:
        return {"error": "Unauthorized"}, 403

    with cfg._lock:
        player = cfg.players.get(pid)
        if not player:
            return {"error": "Player not found"}, 404

        # If this is a base song, get/create the player's variant
        base_song_id = cfg.get_base_song_id(song_id)
        if base_song_id != song_id:
            # song_id is already a variant, use it as-is
            active_song_id = song_id
        else:
            # song_id is a base song, get/create player's variant
            variant_id = cfg.get_or_create_player_song_variant(base_song_id, pid)
            if not variant_id:
                return {"error": "Failed to create variant"}, 500
            active_song_id = variant_id

        if song_type == "walkup":
            if song_id in player.get("walkup_songs", []):
                player["walkup_song_id"] = active_song_id
                cfg.save()
            else:
                return {"error": "Song not in carousel"}, 400
        elif song_type == "warmup":
            if song_id in player.get("warmup_songs", []):
                player["pitching_warmup_song_id"] = active_song_id
                cfg.save()
            else:
                return {"error": "Song not in carousel"}, 400
        else:
            return {"error": "Invalid song type"}, 400

    return {"success": True}


@app.post("/ondeck/carousel/deactivate/<song_type>/<song_id>")
def ondeck_carousel_deactivate(song_type: str, song_id: str):
    """AJAX: Deactivate an active song (remove from active list)."""
    pid = session.get("player_id")
    if not pid:
        return {"error": "Unauthorized"}, 403

    with cfg._lock:
        player = cfg.players.get(pid)
        if not player:
            return {"error": "Player not found"}, 404

        if song_type == "walkup":
            if player.get("walkup_song_id") == song_id:
                player["walkup_song_id"] = None
                cfg.save()
            else:
                return {"error": "Song is not active"}, 400
        elif song_type == "warmup":
            if player.get("pitching_warmup_song_id") == song_id:
                player["pitching_warmup_song_id"] = None
                cfg.save()
            else:
                return {"error": "Song is not active"}, 400
        elif song_type == "midgame":
            if player.get("midgame_song_id") == song_id:
                player["midgame_song_id"] = None
                cfg.save()
            else:
                return {"error": "Song is not active"}, 400
        else:
            return {"error": "Invalid song type"}, 400

    return {"success": True}


@app.post("/ondeck/trim-save/<song_type>/<song_id>")
def ondeck_trim_save(song_type: str, song_id: str):
    """AJAX: Save trim editor changes for a song.

    Creates or updates a player-specific variant of the song with the trim bounds.
    The original song is never modified.
    """
    pid = session.get("player_id")
    if not pid:
        return {"error": "Unauthorized"}, 403

    if song_id not in cfg.songs:
        return {"error": "Song not found"}, 404

    # Get the base song (or use the provided song_id if it's already a base)
    base_song_id = cfg.get_base_song_id(song_id)

    # Get or create the player's variant
    variant_id = cfg.get_or_create_player_song_variant(base_song_id, pid)
    if not variant_id:
        return {"error": "Failed to create variant"}, 500

    start_ms = request.form.get("start_ms")
    end_ms = request.form.get("end_ms")

    with cfg._lock:
        variant = cfg.songs[variant_id]
        if start_ms is not None:
            variant["start_ms"] = int(start_ms) if start_ms else 0
        if end_ms is not None:
            variant["end_ms"] = int(end_ms) if end_ms else None
        cfg.save()

    return {"success": True, "variant_id": variant_id}


# ---------------------------------------------------------------------------
# Notifications page (coach reviews player activity; admin/editor only)
# ---------------------------------------------------------------------------

@app.get("/ondeck/notifications")
def ondeck_notifications():
    notes = cfg.data.get("notifications", [])
    # Highlight what's new this visit, then mark everything reviewed so the
    # nav badge clears — no explicit confirm step needed.
    new_ids = {n["id"] for n in notes if not n.get("seen")}
    if new_ids:
        with cfg._lock:
            for n in cfg.data.get("notifications", []):
                n["seen"] = True
            cfg.save(mark_dirty=False)
    return render_template("notifications.html", notes=notes, new_ids=new_ids,
                           players=cfg.players)


@app.post("/ondeck/notifications/clear")
def ondeck_notifications_clear():
    with cfg._lock:
        cfg.data["notifications"] = []
        cfg.save(mark_dirty=False)
    flash("Cleared activity history.", "success")
    return redirect(url_for("ondeck_notifications"))


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
# Bluetooth speaker — admin/editor manage the Audio Pi's A2DP speaker
# ---------------------------------------------------------------------------
# The portal cannot do Bluetooth itself; it proxies to the Audio Pi's
# music_server (/bluetooth/*). This works from any browser that can reach the
# Audio Pi — i.e. the Stream Deck Pi's portal on the field Wi-Fi. From the cloud
# portal (which can't route to the Pi) the calls return 503 and the page shows
# "Audio Pi unreachable".

@app.get("/ondeck/bluetooth")
def ondeck_bluetooth():
    _check_auth(["admin", "editor"])
    return render_template("bluetooth.html")


@app.get("/ondeck/api/bluetooth/status")
def ondeck_api_bt_status():
    data, code = _proxy("GET", "/bluetooth/status")
    return jsonify(data), code


@app.post("/ondeck/api/bluetooth/scan")
def ondeck_api_bt_scan():
    data, code = _proxy("POST", "/bluetooth/scan", json=request.get_json(silent=True) or {})
    return jsonify(data), code


@app.post("/ondeck/api/bluetooth/<action>")
def ondeck_api_bt_action(action: str):
    if action not in {"pair", "connect", "disconnect", "forget", "preferred"}:
        abort(404)
    body = request.get_json(force=True, silent=True) or {}
    data, code = _proxy("POST", f"/bluetooth/{action}", json=body)
    return jsonify(data), code


# ---------------------------------------------------------------------------
# Lineup editor (admin + editor)
# ---------------------------------------------------------------------------

@app.get("/ondeck/lineup")
def ondeck_lineup():
    size = cfg.data.get("lineup_size", 9)
    order = (cfg.data.get("lineup", []) + [None] * size)[:size]
    slots = [(i, cfg.players.get(pid)) for i, pid in enumerate(order)]
    return render_template("lineup.html", slots=slots, size=size,
                           players=cfg.players_by_jersey())


@app.post("/ondeck/lineup/save")
def ondeck_lineup_save():
    size = cfg.data.get("lineup_size", 9)
    order = [request.form.get(f"slot_{i}") or None for i in range(size)]
    cfg.set_lineup(order)
    flash("Lineup saved.", "success")
    return redirect(url_for("ondeck_lineup"))


@app.post("/ondeck/lineup/size")
def ondeck_lineup_size():
    try:
        cfg.set_lineup_size(int(request.form.get("size", 9)))
        flash("Lineup size updated.", "success")
    except (ValueError, TypeError):
        flash("Lineup size must be a number.", "error")
    return redirect(url_for("ondeck_lineup"))


# ---------------------------------------------------------------------------
# Sounds editor (song-list pages + celebration stingers) — admin + editor
# ---------------------------------------------------------------------------

# The deck's song-list pages, in display order, with friendly labels.
SOUND_PAGES = [
    ("hype", "Hype"),
    ("mid_inning", "Mid-Inning"),
    ("mound_visit", "Mound Visit"),
    ("dead_ball", "Dead Ball"),
    ("pitcher_warmup", "Pitcher Warm-Up"),
]
CELEBRATIONS = [
    ("hit", "Hit"),
    ("extra_base", "Extra-Base Hit"),
    ("home_run", "Home Run"),
    ("strikeout", "Strikeout"),
]

# Fixed Stream Deck XL keys the controller owns (shown locked in the editor).
_DECK_FIXED_LABELS = {
    0: "▲ Prev", 8: "⌂ Home", 16: "▼ Next",
    24: "▶ Play", 25: "■ Stop", 26: "↘ Fade",
    27: "Page", 28: "Page", 29: "Page", 30: "Page", 31: "Page",
}


@app.get("/ondeck/sounds")
def ondeck_sounds():
    page_songs = cfg.data.get("page_songs", {})
    songs_sorted = sorted(cfg.songs.items(),
                          key=lambda kv: kv[1].get("display_name", "").lower())
    return render_template(
        "sounds.html",
        sound_pages=SOUND_PAGES,
        celebrations=CELEBRATIONS,
        page_songs=page_songs,
        celebration_songs=cfg.celebrations,
        songs=songs_sorted,
    )


@app.post("/ondeck/sounds/page/<page_id>")
def ondeck_sounds_page_save(page_id: str):
    if page_id not in dict(SOUND_PAGES):
        abort(404)
    selected = request.form.getlist("song_ids")
    cfg.set_page_songs(page_id, selected)
    flash(f"{dict(SOUND_PAGES)[page_id]} sounds saved.", "success")
    return redirect(url_for("ondeck_sounds"))


@app.post("/ondeck/sounds/celebration/<kind>")
def ondeck_sounds_celebration_save(kind: str):
    if kind not in dict(CELEBRATIONS):
        abort(404)
    cfg.set_celebration(kind, request.form.get("song_id") or None)
    flash(f"{dict(CELEBRATIONS)[kind]} stinger saved.", "success")
    return redirect(url_for("ondeck_sounds"))


# ---------------------------------------------------------------------------
# Stream Deck button editor — admin/editor lay out the physical deck keys
# ---------------------------------------------------------------------------

def _deck_song_choices() -> list[tuple[str, str]]:
    """(song_id, name) for base songs (variants excluded), sorted by name."""
    out = [
        (sid, cfg.get_song_display_name(sid))
        for sid, s in cfg.songs.items()
        if not s.get("base_song_id")
    ]
    return sorted(out, key=lambda kv: kv[1].lower())


@app.get("/ondeck/deck")
def ondeck_deck():
    _check_auth(["admin", "editor"])
    order = cfg.get_page_order()
    page_id = request.args.get("page") or (order[0] if order else "home")
    if page_id not in cfg.pages:
        page_id = order[0] if order else "home"
    page = cfg.pages.get(page_id, {})
    return render_template(
        "deck_editor.html",
        pages=[(pid, cfg.pages[pid]) for pid in order],
        page_id=page_id,
        page=page,
        slots=page.get("slots", {}),
        content_slots=cfg.DECK_CONTENT_SLOTS,
        fixed_labels=_DECK_FIXED_LABELS,
        players=cfg.players_by_jersey(),
        songs=_deck_song_choices(),
        celebrations=CELEBRATIONS,
    )


@app.post("/ondeck/deck/<page_id>/slot")
def ondeck_deck_slot(page_id: str):
    _check_auth(["admin", "editor"])
    if page_id not in cfg.pages:
        abort(404)
    try:
        idx = int(request.form.get("idx", ""))
    except ValueError:
        abort(400)
    # The button assignment arrives as "type:ref" (e.g. "song:abc123",
    # "action:fade", "blank:" to clear).
    assign = request.form.get("assign", "blank:")
    kind, _, ref = assign.partition(":")
    cfg.set_page_slot(page_id, idx, {
        "type": kind,
        "ref": ref,
        "label": request.form.get("label", "").strip(),
        "color": request.form.get("color", "").strip(),
    })
    flash("Button saved.", "success")
    return redirect(url_for("ondeck_deck", page=page_id))


@app.post("/ondeck/deck/<page_id>/fill-players")
def ondeck_deck_fill_players(page_id: str):
    _check_auth(["admin", "editor"])
    if page_id not in cfg.pages:
        abort(404)
    order = request.args.get("order", "jersey")
    cfg.fill_player_slots(page_id, "alpha" if order == "alpha" else "jersey")
    flash(f"Filled buttons with players ({order} order).", "success")
    return redirect(url_for("ondeck_deck", page=page_id))


@app.post("/ondeck/deck/<page_id>/clear")
def ondeck_deck_clear(page_id: str):
    _check_auth(["admin", "editor"])
    if page_id not in cfg.pages:
        abort(404)
    cfg.clear_page_slots(page_id)
    flash("Page buttons cleared.", "success")
    return redirect(url_for("ondeck_deck", page=page_id))


@app.post("/ondeck/deck/pages/add")
def ondeck_deck_pages_add():
    _check_auth(["admin", "editor"])
    name = request.form.get("name", "").strip()
    if not name:
        flash("Page name is required.", "error")
        return redirect(url_for("ondeck_deck"))
    pid = cfg.add_page(name)
    flash(f"Page '{name}' added.", "success")
    return redirect(url_for("ondeck_deck", page=pid))


@app.post("/ondeck/deck/pages/<page_id>/rename")
def ondeck_deck_pages_rename(page_id: str):
    _check_auth(["admin", "editor"])
    if page_id not in cfg.pages:
        abort(404)
    cfg.rename_page(page_id, request.form.get("name", ""))
    flash("Page renamed.", "success")
    return redirect(url_for("ondeck_deck", page=page_id))


@app.post("/ondeck/deck/pages/<page_id>/move")
def ondeck_deck_pages_move(page_id: str):
    _check_auth(["admin", "editor"])
    if page_id not in cfg.pages:
        abort(404)
    direction = -1 if request.form.get("dir") == "up" else 1
    cfg.move_page(page_id, direction)
    return redirect(url_for("ondeck_deck", page=page_id))


@app.post("/ondeck/deck/pages/<page_id>/delete")
def ondeck_deck_pages_delete(page_id: str):
    _check_auth(["admin", "editor"])
    page = cfg.pages.get(page_id)
    if not page:
        abort(404)
    if not page.get("deletable"):
        flash("Built-in pages can't be deleted.", "error")
        return redirect(url_for("ondeck_deck", page=page_id))
    cfg.delete_page(page_id)
    flash("Page deleted.", "success")
    return redirect(url_for("ondeck_deck"))


# ---------------------------------------------------------------------------
# Team Management — admin + editor (signup links, team assignments)
# ---------------------------------------------------------------------------

@app.get("/ondeck/teams")
def ondeck_teams():
    _check_auth(["admin", "editor"])
    teams = cfg.teams
    teams_list = [(tid, t) for tid, t in teams.items()]
    teams_list.sort(key=lambda kv: kv[1].get("name", ""))
    return render_template("team_management.html", teams=teams_list)


@app.post("/ondeck/teams/add")
def ondeck_teams_add():
    _check_auth(["admin", "editor"])
    name = request.form.get("name", "").strip()
    if not name:
        flash("Team name is required.", "error")
    else:
        tid = cfg.add_team(name)
        flash(f"Team '{name}' created.", "success")
    return redirect(url_for("ondeck_teams"))


@app.post("/ondeck/teams/<tid>/update")
def ondeck_teams_update(tid: str):
    _check_auth(["admin", "editor"])
    if tid not in cfg.teams:
        abort(404)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Team name is required.", "error")
    else:
        cfg.update_team(tid, name)
        flash("Team updated.", "success")
    return redirect(url_for("ondeck_teams"))


@app.post("/ondeck/teams/<tid>/delete")
def ondeck_teams_delete(tid: str):
    _check_auth(["admin"])
    if tid not in cfg.teams:
        abort(404)
    cfg.delete_team(tid)
    flash("Team deleted.", "success")
    return redirect(url_for("ondeck_teams"))


@app.get("/ondeck/signup-links")
def ondeck_signup_links():
    _check_auth(["admin"])
    active_links = cfg.get_active_signup_links()
    teams = cfg.teams
    return render_template("signup_links.html", links=active_links, teams=teams)


@app.post("/ondeck/signup-links/create")
def ondeck_signup_links_create():
    _check_auth(["admin"])
    team_ids = request.form.getlist("team_ids")
    if not team_ids:
        flash("Select at least one team.", "error")
    else:
        code, link = cfg.create_signup_link(team_ids)
        flash(f"Signup link created. Share this code: {code}", "success")
    return redirect(url_for("ondeck_signup_links"))


@app.post("/ondeck/signup-links/<code>/revoke")
def ondeck_signup_links_revoke(code: str):
    _check_auth(["admin"])
    cfg.revoke_signup_link(code)
    flash("Signup link revoked.", "success")
    return redirect(url_for("ondeck_signup_links"))


# ---------------------------------------------------------------------------
# Devices — admin: pair, name, and revoke field Raspberry Pis
# ---------------------------------------------------------------------------

@app.get("/ondeck/devices")
def ondeck_devices():
    _check_auth(["admin"])
    # Surface a freshly-created code once (passed via the redirect) so the coach
    # can read it off to the device.
    new_code = request.args.get("code", "")
    new_name = request.args.get("name", "")
    return render_template(
        "devices.html",
        devices=cfg.list_devices(),
        new_code=new_code,
        new_name=new_name,
        cloud_url=request.host_url.rstrip("/"),
    )


@app.post("/ondeck/devices/pair-code")
def ondeck_devices_pair_code():
    _check_auth(["admin"])
    name = request.form.get("name", "").strip() or "OnDeck Pi"
    role = request.form.get("role", "deck")
    entry = cfg.create_pairing_code(name, role)
    flash(f"Pairing code for '{entry['name']}': {entry['code']}", "success")
    return redirect(url_for("ondeck_devices", code=entry["code"], name=entry["name"]))


@app.post("/ondeck/devices/<device_id>/rename")
def ondeck_devices_rename(device_id: str):
    _check_auth(["admin"])
    if device_id not in cfg.devices:
        abort(404)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Device name is required.", "error")
    else:
        cfg.rename_device(device_id, name)
        flash("Device renamed.", "success")
    return redirect(url_for("ondeck_devices"))


@app.post("/ondeck/devices/<device_id>/revoke")
def ondeck_devices_revoke(device_id: str):
    _check_auth(["admin"])
    if device_id not in cfg.devices:
        abort(404)
    cfg.revoke_device(device_id)
    flash("Device revoked. It can no longer sync until re-paired.", "success")
    return redirect(url_for("ondeck_devices"))


@app.post("/ondeck/settings/signup-link-expires")
def ondeck_settings_signup_expires():
    _check_auth(["admin"])
    try:
        hours = int(request.form.get("signup_link_expires_hours", 168))
        hours = max(1, min(hours, 52560))  # 1 hour to 6 years
        cfg.system["signup_link_expires_hours"] = hours
        cfg.save()
        flash(f"Signup link expiration set to {hours} hours.", "success")
    except (ValueError, TypeError):
        flash("Invalid value for expiration hours.", "error")
    return redirect(url_for("ondeck_settings"))


# ---------------------------------------------------------------------------
# Team Roster — player page to see teammates and walk-up songs
# ---------------------------------------------------------------------------

@app.get("/ondeck/team-roster")
def ondeck_team_roster():
    role = session.get("role")

    # Players see their own teams; admins/editors see all
    if role == "player":
        pid = session.get("player_id")
        player = cfg.players.get(pid)
        if not player:
            abort(403)
        team_ids = player.get("team_ids", [])
        teams_to_show = [(tid, cfg.teams[tid]) for tid in team_ids if tid in cfg.teams]
    elif role in ("admin", "editor"):
        teams_to_show = [(tid, cfg.teams[tid]) for tid in cfg.teams.keys()]
    else:
        abort(403)

    teams_data = []
    for team_id, team in teams_to_show:
        members = cfg.get_team_members(team_id)
        teams_data.append({
            "id": team_id,
            "name": team.get("name", ""),
            "members": members,
        })

    return render_template("team_roster.html", teams=teams_data, cfg=cfg)


# ---------------------------------------------------------------------------
# Settings (admin only — enforced in _check_auth)
# ---------------------------------------------------------------------------

@app.get("/ondeck/settings")
def ondeck_settings():
    return render_template("settings.html", system=cfg.system,
                           has_yt_cookies=COOKIES_FILE.exists(),
                           elevenlabs_key_set=bool(_elevenlabs_key()))


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

        # AI announcer voice (ElevenLabs).
        s["elevenlabs_voice_id"] = request.form.get("elevenlabs_voice_id", "").strip()
        s["elevenlabs_model"] = (request.form.get("elevenlabs_model", "").strip()
                                 or "eleven_multilingual_v2")
        s["announcement_template"] = (request.form.get("announcement_template", "").strip()
                                      or s.get("announcement_template", ""))
        # Only overwrite the stored key when a new one is typed; an empty field
        # leaves the existing key untouched so it's never echoed back to the page.
        new_key = request.form.get("elevenlabs_api_key", "").strip()
        if new_key:
            s["elevenlabs_api_key"] = new_key
        elif request.form.get("clear_elevenlabs_key") == "on":
            s["elevenlabs_api_key"] = ""

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
    ip = body.get("ip") or request.remote_addr
    hostname = body.get("hostname", pi_id)
    # If the Pi authenticated with a per-device token, update that device's
    # check-in. Fall back to the legacy known_pis map (global token / unpaired).
    if not cfg.touch_device(_bearer_token(), ip=ip, hostname=hostname):
        with cfg._lock:
            cfg.system.setdefault("known_pis", {})[pi_id] = {
                "ip":        ip,
                "hostname":  hostname,
                "last_seen": body.get("timestamp", ""),
            }
            cfg.save(mark_dirty=False)
    return jsonify(ok=True)


@app.post("/sync/pair")
def sync_pair():
    """Redeem a pairing code → mint a per-device sync token.

    Unauthenticated by design: the short-lived code *is* the credential. The Pi
    captive portal (or a boot file) posts the code and gets back a token it then
    stores in sync.env for all future /sync/* calls.
    """
    body = request.get_json(force=True, silent=True) or {}
    code = (body.get("code") or "").strip()
    hostname = (body.get("hostname") or "")[:64]
    ip = body.get("ip") or request.remote_addr
    result = cfg.redeem_pairing_code(code, hostname=hostname, ip=ip)
    if not result:
        return jsonify(ok=False, error="invalid or expired code"), 410
    device_id, token, name = result
    return jsonify(ok=True, sync_token=token, device_id=device_id, name=name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _elevenlabs_key() -> str:
    """API key from config, falling back to the ELEVENLABS_API_KEY env var."""
    return (cfg.system.get("elevenlabs_api_key") or ELEVENLABS_API_KEY).strip()


def _elevenlabs_voice() -> str:
    """Voice ID from config, falling back to the ELEVENLABS_VOICE_ID env var."""
    return (cfg.system.get("elevenlabs_voice_id") or ELEVENLABS_VOICE_ID).strip()


def _elevenlabs_model() -> str:
    return cfg.system.get("elevenlabs_model") or "eleven_multilingual_v2"


def _elevenlabs_ready() -> bool:
    return bool(_elevenlabs_key() and _elevenlabs_voice())


def _default_announcement(player: dict) -> str:
    """Fill the announcement template with this player's details."""
    tmpl = (cfg.system.get("announcement_template")
            or "Now batting, number {jersey}, {first_name} {last_name}")
    try:
        return tmpl.format(
            jersey=player.get("jersey", ""),
            first_name=player.get("first_name", ""),
            last_name=player.get("last_name", ""),
        )
    except Exception:
        # A malformed template (bad placeholder) shouldn't break the page.
        return tmpl


def _elevenlabs_error(r) -> str:
    """Pull a human-readable message out of an ElevenLabs error response."""
    try:
        detail = r.json().get("detail", "")
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("status") or ""
        if detail:
            return f"ElevenLabs error {r.status_code}: {detail}"
    except Exception:
        pass
    return f"ElevenLabs {r.status_code}: {r.text[:300]}"


def _elevenlabs_announce(text: str) -> tuple[bytes | None, str | None]:
    """Synthesize ``text`` to MP3 bytes. Returns (audio_bytes, error_message)."""
    key = _elevenlabs_key()
    if not key:
        return None, "No ElevenLabs API key set (Settings → Announcer Voice)."
    voice = _elevenlabs_voice()
    if not voice:
        return None, "No ElevenLabs voice selected (Settings → Announcer Voice)."
    try:
        r = rq.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
            params={"output_format": "mp3_44100_128"},
            headers={
                "xi-api-key": key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": _elevenlabs_model(),
                "voice_settings": {"stability": 0.55, "similarity_boost": 0.75},
            },
            timeout=30,
        )
    except Exception as exc:
        return None, f"Could not reach ElevenLabs: {exc}"
    if r.status_code != 200:
        return None, _elevenlabs_error(r)
    return r.content, None


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
