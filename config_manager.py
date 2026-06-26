"""Configuration and on-disk state for OnDeck.

All runtime data lives under ``~/ondeck`` for whatever user is running the
process. Nothing here assumes the ``pi`` account, so the same code works on a
hand-installed Pi, a developer laptop, or the shipped image.

The config file is the single source of truth during a game. Writes are atomic
(temp file + ``os.replace``) so a power loss mid-write can never corrupt it.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any


# Base directory for everything OnDeck stores on disk.
ONDECK_HOME = Path(os.environ.get("ONDECK_HOME", Path.home() / "ondeck"))
MUSIC_DIR = ONDECK_HOME / "music"
CONFIG_PATH = ONDECK_HOME / "config.json"


def _default_config() -> dict[str, Any]:
    """A fresh config with empty rosters and the built-in pages.

    Pages are keyed by a stable UUID, never by name, so a coach can rename a
    page without breaking the Stream Deck navigation that points at it.
    """
    return {
        "system": {
            "audio_pi_ip": "",
            "audio_pi_port": 5100,
            "wifi_configured": False,
            "bluetooth_device": None,
            "volume": 80,
            # AI announcer voice (ElevenLabs text-to-speech). The API key may
            # also be supplied via the ELEVENLABS_API_KEY env var.
            "elevenlabs_api_key": "",
            "elevenlabs_voice_id": "",
            "elevenlabs_model": "eleven_multilingual_v2",
            "announcement_template": "Now batting, number {jersey}, {first_name} {last_name}",
            # Cloud sync bookkeeping.
            "last_synced_at": None,
            "dirty": False,
            # Signup link settings.
            "signup_link_expires_hours": 168,  # 1 week
        },
        "players": {},          # player_id -> player dict
        "songs": {},            # song_id -> song dict
        # Activity log of player-made changes for the coach to review. Newest
        # first; capped in app.py. Each entry: {id, ts, player_id, jersey,
        # player_name, action, detail, seen}.
        "notifications": [],
        "lineup": [None] * 9,   # batting order, indexes 0..size-1 used
        "lineup_size": 9,
        "celebrations": {
            "hit": None,
            "extra_base": None,
            "home_run": None,
            "strikeout": None,
        },
        "mid_inning": [],       # list of song_ids (legacy; mirrored in page_songs)
        # Songs assigned to each game-day page, keyed by page id. The Stream
        # Deck renders one button per song in order; the web portal manages the
        # lists. Player/lineup/celebration pages are special-cased and ignore
        # this map.
        "page_songs": {
            "hype": [],
            "mid_inning": [],
            "mound_visit": [],
            "dead_ball": [],
            "pitcher_warmup": [],
        },
        "pages": _default_pages(),
        "teams": {},            # team_id -> {name, created_at}
        "signup_links": [],     # [{code, team_ids, created_at, expires_at}]
    }


def _default_pages() -> dict[str, Any]:
    """Built-in pages, each with a stable id and an ordering hint."""
    builtin = [
        ("home", "Home", 0),
        ("lineup", "Lineup", 1),
        ("players", "Players", 2),
        ("hype", "Hype", 3),
        ("mid_inning", "Mid-Inning", 4),
        ("mound_visit", "Mound Visit", 5),
        ("dead_ball", "Dead Ball", 6),
        ("celebrations", "Celebrations", 7),
        ("pitcher_warmup", "Pitcher Warm-Up", 8),
    ]
    pages: dict[str, Any] = {}
    for kind, name, order in builtin:
        pages[kind] = {
            "name": name,
            "kind": kind,         # what the page does; drives rendering
            "order": order,
            "deletable": False,
            "slots": {},          # slot_index -> button config (color/text/font)
        }
    return pages


class ConfigManager:
    """Thread-safe loader/saver for the OnDeck config file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else CONFIG_PATH
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self.load()

    # -- loading / saving -------------------------------------------------

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self.path.exists():
                try:
                    self._data = json.loads(self.path.read_text())
                except (json.JSONDecodeError, OSError):
                    # Corrupt or unreadable: fall back to defaults rather than
                    # crashing on game day.
                    self._data = _default_config()
            else:
                self._data = _default_config()
            self._ensure_shape()
            return self._data

    def save(self, mark_dirty: bool = True) -> None:
        """Atomically persist the current config.

        ``mark_dirty`` flags that there are local changes not yet pushed to the
        cloud; the sync layer clears it after a successful push.
        """
        with self._lock:
            if mark_dirty:
                self._data.setdefault("system", {})["dirty"] = True
            ONDECK_HOME.mkdir(parents=True, exist_ok=True)
            MUSIC_DIR.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(ONDECK_HOME), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(self._data, fh, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    def _ensure_shape(self) -> None:
        """Backfill any missing top-level keys so upgrades never KeyError."""
        defaults = _default_config()
        for key, value in defaults.items():
            self._data.setdefault(key, value)
        # Backfill any new system sub-keys (e.g. the ElevenLabs settings) so a
        # config written by an older version doesn't KeyError on upgrade.
        system = self._data.setdefault("system", {})
        for key, value in defaults["system"].items():
            system.setdefault(key, value)
        # Make sure built-in pages always exist even if a config predates them.
        pages = self._data.setdefault("pages", {})
        for pid, page in _default_pages().items():
            pages.setdefault(pid, page)
        # Backfill any missing per-page song lists (added after the first ship).
        page_songs = self._data.setdefault("page_songs", {})
        for pid, lst in defaults["page_songs"].items():
            page_songs.setdefault(pid, list(lst))
        # Migrate the legacy top-level mid_inning list into page_songs once.
        legacy_mid = self._data.get("mid_inning") or []
        if legacy_mid and not page_songs.get("mid_inning"):
            page_songs["mid_inning"] = list(legacy_mid)
        # Ensure all players have team_ids and personal song fields (added later).
        for player in self._data.get("players", {}).values():
            player.setdefault("team_ids", [])
            player.setdefault("pitching_warmup_song_id", None)
            player.setdefault("midgame_song_id", None)
            player.setdefault("walkup_songs", [])  # List of walkup song IDs (carousel)
            player.setdefault("warmup_songs", [])  # List of warmup song IDs (carousel)
        # Ensure all songs have alias field.
        for song in self._data.get("songs", {}).values():
            song.setdefault("alias", "")

    # -- convenient accessors --------------------------------------------

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    @property
    def system(self) -> dict[str, Any]:
        return self._data["system"]

    @property
    def players(self) -> dict[str, Any]:
        return self._data["players"]

    @property
    def songs(self) -> dict[str, Any]:
        return self._data["songs"]

    @property
    def pages(self) -> dict[str, Any]:
        return self._data["pages"]

    @property
    def lineup(self) -> list[Any]:
        return self._data["lineup"]

    @property
    def celebrations(self) -> dict[str, Any]:
        return self._data["celebrations"]

    @property
    def teams(self) -> dict[str, Any]:
        return self._data.setdefault("teams", {})

    @property
    def signup_links(self) -> list[Any]:
        return self._data.setdefault("signup_links", [])

    # -- Stream Deck helpers ---------------------------------------------
    # These mirror the read-only accessors the play-call deck relied on, so
    # the controller can stay config-driven and dumb about storage details.

    def get_page_order(self) -> list[str]:
        """Page ids in display order (the deck's nav + bottom-row order)."""
        return [
            pid
            for pid, _ in sorted(
                self.pages.items(), key=lambda kv: kv[1].get("order", 99)
            )
        ]

    def get_songs_for_page(self, page_id: str) -> list[tuple[str, dict[str, Any]]]:
        """(song_id, song) pairs assigned to a song-list page, in order.

        Missing or stale song ids are skipped so a deleted song never leaves a
        dead button on the deck.
        """
        ids = self._data.get("page_songs", {}).get(page_id, []) or []
        out: list[tuple[str, dict[str, Any]]] = []
        for sid in ids:
            song = self.songs.get(sid)
            if song:
                out.append((sid, song))
        return out

    def get_celebration_song(self, kind: str) -> str | None:
        """song_id for a celebration stinger (hit/extra_base/home_run/strikeout)."""
        return self.celebrations.get(kind)

    def build_walkup_clip(self, player_id: str) -> dict[str, Any] | None:
        """Compose the Audio Pi /queue payload for a player's walk-up.

        Returns None if the player has no walk-up song. The announcement (if
        recorded) plays first and the trimmed song fades in at ``music_cue_ms``
        — exactly the shape ``music_server.Player._build_command`` expects.
        """
        player = self.players.get(player_id)
        if not player:
            return None
        sid = player.get("walkup_song_id")
        song = self.songs.get(sid) if sid else None
        if not song:
            return None
        clip: dict[str, Any] = {
            "file": song["filename"],
            "start_ms": song.get("start_ms", 0),
            "end_ms": song.get("end_ms"),
        }
        ann = player.get("announcement_file")
        if ann:
            clip["announcement"] = ann
            clip["cue_ms"] = player.get("music_cue_ms", 0)
        return clip

    def build_song_clip(self, song_id: str) -> dict[str, Any] | None:
        """Audio Pi /queue payload for a plain song (no announcement)."""
        song = self.songs.get(song_id)
        if not song:
            return None
        return {
            "file": song["filename"],
            "start_ms": song.get("start_ms", 0),
            "end_ms": song.get("end_ms"),
        }

    def get_player_pitching_warmup(self, player_id: str) -> dict[str, Any] | None:
        """Get player's pitching warm-up song info."""
        player = self.players.get(player_id)
        if not player:
            return None
        sid = player.get("pitching_warmup_song_id")
        song = self.songs.get(sid) if sid else None
        if not song:
            return None
        return {
            "file": song["filename"],
            "start_ms": song.get("start_ms", 0),
            "end_ms": song.get("end_ms"),
        }

    def get_player_midgame_song(self, player_id: str) -> dict[str, Any] | None:
        """Get player's mid-game/mid-inning song info."""
        player = self.players.get(player_id)
        if not player:
            return None
        sid = player.get("midgame_song_id")
        song = self.songs.get(sid) if sid else None
        if not song:
            return None
        return {
            "file": song["filename"],
            "start_ms": song.get("start_ms", 0),
            "end_ms": song.get("end_ms"),
        }

    # -- mutations --------------------------------------------------------

    def add_player(self, jersey: int, first_name: str, last_name: str, team_ids: list[str] | None = None) -> str:
        pid = uuid.uuid4().hex
        with self._lock:
            self.players[pid] = {
                "jersey": jersey,
                "first_name": first_name,
                "last_name": last_name,
                "announcement_file": None,
                "announcement_text": "",
                "announcement_start_ms": 0,
                "announcement_end_ms": None,
                "walkup_song_id": None,
                "music_cue_ms": 0,
                "pitching_warmup_song_id": None,
                "midgame_song_id": None,
                "team_ids": team_ids or [],
            }
            self.save()
        return pid

    def add_song(self, filename: str, display_name: str) -> str:
        sid = uuid.uuid4().hex
        with self._lock:
            self.songs[sid] = {
                "filename": filename,
                "display_name": display_name,
                "start_ms": 0,
                "end_ms": None,
                "alias": "",  # User-friendly name override
            }
            self.save()
        return sid

    def players_by_jersey(self) -> list[tuple[str, dict[str, Any]]]:
        """Players sorted by jersey number, for stable page layout."""
        return sorted(
            self.players.items(),
            key=lambda kv: (kv[1].get("jersey") or 0, kv[1].get("last_name", "")),
        )

    # -- game-day editors (lineup / page songs / celebrations) -----------

    def set_lineup(self, player_ids: list[str | None]) -> None:
        """Replace the batting order. Unknown ids and overflow are dropped; the
        list is padded with None to ``lineup_size``."""
        size = self._data.get("lineup_size", 9)
        cleaned: list[str | None] = []
        for pid in player_ids[:size]:
            cleaned.append(pid if pid and pid in self.players else None)
        cleaned += [None] * (size - len(cleaned))
        with self._lock:
            self._data["lineup"] = cleaned
            self.save()

    def set_lineup_size(self, size: int) -> None:
        """Grow/shrink the batting order, preserving existing assignments."""
        size = max(1, min(int(size), 20))
        with self._lock:
            current = self._data.get("lineup", [])
            current = (current + [None] * size)[:size]
            self._data["lineup_size"] = size
            self._data["lineup"] = current
            self.save()

    def set_page_songs(self, page_id: str, song_ids: list[str]) -> None:
        """Set the ordered song list for a song-list page (unknown ids dropped)."""
        valid = [sid for sid in song_ids if sid in self.songs]
        with self._lock:
            self._data.setdefault("page_songs", {})[page_id] = valid
            # Keep the legacy mirror in step so older readers stay correct.
            if page_id == "mid_inning":
                self._data["mid_inning"] = list(valid)
            self.save()

    def set_celebration(self, kind: str, song_id: str | None) -> None:
        """Assign (or clear) a celebration stinger song."""
        if kind not in self._data["celebrations"]:
            return
        with self._lock:
            self._data["celebrations"][kind] = (
                song_id if song_id and song_id in self.songs else None
            )
            self.save()

    # -- teams and signup links ------------------------------------------

    def add_team(self, name: str) -> str:
        """Create a new team."""
        tid = uuid.uuid4().hex
        with self._lock:
            self.teams[tid] = {"name": name, "created_at": _now_ms()}
            self.save()
        return tid

    def update_team(self, team_id: str, name: str) -> None:
        """Rename a team."""
        with self._lock:
            if team_id in self.teams:
                self.teams[team_id]["name"] = name
                self.save()

    def delete_team(self, team_id: str) -> None:
        """Delete a team and remove it from all players."""
        with self._lock:
            if team_id in self.teams:
                del self.teams[team_id]
                for player in self.players.values():
                    if team_id in player.get("team_ids", []):
                        player["team_ids"].remove(team_id)
                self.save()

    def set_player_teams(self, player_id: str, team_ids: list[str]) -> None:
        """Assign a player to one or more teams."""
        valid_ids = [tid for tid in team_ids if tid in self.teams]
        with self._lock:
            if player_id in self.players:
                self.players[player_id]["team_ids"] = valid_ids
                self.save()

    def create_signup_link(self, team_ids: list[str]) -> tuple[str, dict[str, Any]]:
        """Create a time-limited signup link for one or more teams.

        Returns (code, link_data) where code is the shareable access code.
        """
        import time
        code = uuid.uuid4().hex[:12]
        expires_hours = self.system.get("signup_link_expires_hours", 168)
        expires_at = int(time.time() * 1000) + (expires_hours * 3600 * 1000)
        valid_ids = [tid for tid in team_ids if tid in self.teams]

        link = {
            "code": code,
            "team_ids": valid_ids,
            "created_at": int(time.time() * 1000),
            "expires_at": expires_at,
        }
        with self._lock:
            self.signup_links.append(link)
            self.save()
        return code, link

    def get_signup_link(self, code: str) -> dict[str, Any] | None:
        """Get signup link by code if valid and not expired."""
        import time
        now = int(time.time() * 1000)
        with self._lock:
            for link in self.signup_links:
                if link["code"] == code and link["expires_at"] > now:
                    return link
        return None

    def revoke_signup_link(self, code: str) -> None:
        """Delete a signup link."""
        with self._lock:
            self.signup_links[:] = [l for l in self.signup_links if l["code"] != code]
            self.save()

    def get_active_signup_links(self) -> list[dict[str, Any]]:
        """Get all non-expired signup links."""
        import time
        now = int(time.time() * 1000)
        with self._lock:
            return [l for l in self.signup_links if l["expires_at"] > now]

    def get_team_members(self, team_id: str) -> list[tuple[str, dict[str, Any]]]:
        """Get all players on a team, sorted by jersey."""
        members = [
            (pid, p) for pid, p in self.players.items()
            if team_id in p.get("team_ids", [])
        ]
        return sorted(members, key=lambda kv: (kv[1].get("jersey") or 0, kv[1].get("last_name", "")))

    # -- song carousel helpers ---

    def add_player_walkup_song(self, player_id: str, song_id: str) -> None:
        """Add a song to a player's walk-up carousel."""
        with self._lock:
            player = self.players.get(player_id)
            if player and song_id in self.songs:
                if "walkup_songs" not in player:
                    player["walkup_songs"] = []
                if song_id not in player["walkup_songs"]:
                    player["walkup_songs"].append(song_id)
                    self.save()

    def remove_player_walkup_song(self, player_id: str, song_id: str) -> None:
        """Remove a song from a player's walk-up carousel."""
        with self._lock:
            player = self.players.get(player_id)
            if player and song_id in player.get("walkup_songs", []):
                player["walkup_songs"].remove(song_id)
                self.save()

    def add_player_warmup_song(self, player_id: str, song_id: str) -> None:
        """Add a song to a player's warm-up carousel."""
        with self._lock:
            player = self.players.get(player_id)
            if player and song_id in self.songs:
                if "warmup_songs" not in player:
                    player["warmup_songs"] = []
                if song_id not in player["warmup_songs"]:
                    player["warmup_songs"].append(song_id)
                    self.save()

    def remove_player_warmup_song(self, player_id: str, song_id: str) -> None:
        """Remove a song from a player's warm-up carousel."""
        with self._lock:
            player = self.players.get(player_id)
            if player and song_id in player.get("warmup_songs", []):
                player["warmup_songs"].remove(song_id)
                self.save()

    def get_song_display_name(self, song_id: str) -> str:
        """Get display name for a song (alias if set, otherwise display_name)."""
        song = self.songs.get(song_id)
        if not song:
            return "Unknown"
        return song.get("alias") or song.get("display_name", "")

    def set_song_alias(self, song_id: str, alias: str) -> None:
        """Set a display alias for a song."""
        with self._lock:
            if song_id in self.songs:
                self.songs[song_id]["alias"] = alias.strip()
                self.save()


def _now_ms() -> int:
    """Current time in milliseconds since epoch."""
    import time
    return int(time.time() * 1000)


if __name__ == "__main__":
    # Smoke test: create a config, add a player, reload it.
    cm = ConfigManager()
    pid = cm.add_player(12, "Jake", "Smith")
    print(f"config at {cm.path}")
    print(f"added player {pid}: {cm.players[pid]}")
