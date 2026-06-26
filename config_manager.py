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
        },
        "players": {},          # player_id -> player dict
        "songs": {},            # song_id -> song dict
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

    # -- mutations --------------------------------------------------------

    def add_player(self, jersey: int, first_name: str, last_name: str) -> str:
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
            }
            self.save()
        return sid

    def players_by_jersey(self) -> list[tuple[str, dict[str, Any]]]:
        """Players sorted by jersey number, for stable page layout."""
        return sorted(
            self.players.items(),
            key=lambda kv: (kv[1].get("jersey") or 0, kv[1].get("last_name", "")),
        )


if __name__ == "__main__":
    # Smoke test: create a config, add a player, reload it.
    cm = ConfigManager()
    pid = cm.add_player(12, "Jake", "Smith")
    print(f"config at {cm.path}")
    print(f"added player {pid}: {cm.players[pid]}")
