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
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any


# Base directory for everything OnDeck stores on disk.
ONDECK_HOME = Path(os.environ.get("ONDECK_HOME", Path.home() / "ondeck"))
MUSIC_DIR = ONDECK_HOME / "music"
CONFIG_PATH = ONDECK_HOME / "config.json"


def rating_summary(ratings: dict[str, Any] | None) -> dict[str, Any]:
    """Aggregate a ``{rater_key: stars}`` map into ``{avg, count}``.

    ``avg`` is a float rounded to two places (0.0 when there are no ratings).
    Used by both the player rating page and the editor heat-map curation view.
    """
    vals = [int(v) for v in (ratings or {}).values() if v]
    if not vals:
        return {"avg": 0.0, "count": 0}
    return {"avg": round(sum(vals) / len(vals), 2), "count": len(vals)}


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
            # Spotify Web API (read-only) — turns a shared playlist link into a
            # list of pending songs. App-level credentials; no per-user login.
            "spotify_client_id": "",
            "spotify_client_secret": "",
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
        # Linked Raspberry Pis. A Pi is paired with a short code generated in the
        # portal; redeeming the code mints a per-device sync token here so each
        # device can be named, seen, and revoked independently of the others.
        "devices": {},          # device_id -> {id, name, role, token, hostname,
                                #               ip, paired_at, last_seen, revoked}
        "pairing_codes": [],    # [{code, name, role, created_at, expires_at, redeemed_by}]
        # Player-submitted song suggestions (a Spotify link or just a title). An
        # editor reviews these and sources the actual audio. Never auto-played.
        # Each entry: {id, created_at, requested_by, requester_name, title,
        #              artist, spotify_url, note, status, ratings:{rater:stars}}.
        "song_requests": [],
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


# Stream Deck XL fixed/content key indices (module level so the class body can
# reference them — a class-scope comprehension can't see sibling class vars).
_DECK_FIXED_SLOTS = {0, 8, 16, 24, 25, 26, 27, 28, 29, 30, 31}
_DECK_CONTENT_SLOTS = [i for i in range(32) if i not in _DECK_FIXED_SLOTS]


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
        # Ensure all songs have alias and variant fields.
        for song in self._data.get("songs", {}).values():
            song.setdefault("alias", "")
            song.setdefault("base_song_id", None)  # null for originals; song_id for variants
            song.setdefault("created_by_player", None)  # player_id who created this variant
            song.setdefault("ratings", {})  # rater_key -> stars (1..5)
            song.setdefault("spotify_url", "")  # set when promoted from a Spotify request
        # Backfill fields added to pending song requests after their first ship.
        for req in self._data.get("song_requests", []):
            req.setdefault("source", "player")
            req.setdefault("album_art", "")
            req.setdefault("ratings", {})

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

    @property
    def devices(self) -> dict[str, Any]:
        return self._data.setdefault("devices", {})

    @property
    def pairing_codes(self) -> list[Any]:
        return self._data.setdefault("pairing_codes", [])

    @property
    def song_requests(self) -> list[Any]:
        return self._data.setdefault("song_requests", [])

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
                "ratings": {},  # rater_key -> stars (1..5)
            }
            self.save()
        return sid

    # -- ratings & song requests -----------------------------------------

    def set_song_rating(self, song_id: str, rater_key: str, stars: int) -> bool:
        """Record one rater's star rating (1-5) for a song.

        ``stars`` of 0 (or out of range) clears that rater's vote. Returns
        ``False`` if the song doesn't exist. Ratings are per-rater so a song
        keeps an average across everyone who voted, and re-voting overwrites.
        """
        with self._lock:
            song = self.songs.get(song_id)
            if not song:
                return False
            ratings = song.setdefault("ratings", {})
            stars = int(stars) if stars else 0
            if 1 <= stars <= 5:
                ratings[rater_key] = stars
            else:
                ratings.pop(rater_key, None)
            self.save()
            return True

    def add_song_request(self, requester_key: str, requester_name: str,
                         title: str, artist: str = "", spotify_url: str = "",
                         note: str = "", source: str = "player",
                         album_art: str = "") -> str:
        """Add a pending song to the request queue. Returns its id.

        ``source`` is ``player`` for a hand-typed suggestion or
        ``spotify_import`` for a track pulled from a shared playlist.
        """
        rid = uuid.uuid4().hex
        with self._lock:
            self.song_requests.insert(0, {
                "id": rid,
                "created_at": time.time(),
                "requested_by": requester_key,
                "requester_name": requester_name,
                "title": title,
                "artist": artist,
                "spotify_url": spotify_url,
                "album_art": album_art,
                "note": note,
                "source": source,
                "status": "open",   # open | sourced | dismissed
                "ratings": {},      # rater_key -> stars (1..5)
            })
            self.save()
        return rid

    def promote_request_to_song(self, req_id: str, filename: str,
                               display_name: str | None = None) -> str | None:
        """Turn a pending request into a real library song.

        The uploaded ``filename`` becomes the song's audio; the request's
        accumulated player ratings carry over verbatim, and the pending entry
        is removed. Returns the new song_id, or ``None`` if the request is gone.
        """
        with self._lock:
            req = self._request(req_id)
            if not req:
                return None
            name = display_name or req.get("title") or Path(filename).stem
            artist = req.get("artist") or ""
            if artist and artist.lower() not in name.lower():
                name = f"{name} — {artist}"
            sid = uuid.uuid4().hex
            self.songs[sid] = {
                "filename": filename,
                "display_name": name,
                "start_ms": 0,
                "end_ms": None,
                "alias": "",
                "base_song_id": None,
                "created_by_player": None,
                "ratings": dict(req.get("ratings") or {}),
                "spotify_url": req.get("spotify_url", ""),
            }
            self._data["song_requests"] = [
                r for r in self.song_requests if r.get("id") != req_id
            ]
            self.save()
        return sid

    def _request(self, req_id: str) -> dict[str, Any] | None:
        for r in self.song_requests:
            if r.get("id") == req_id:
                return r
        return None

    def set_request_rating(self, req_id: str, rater_key: str, stars: int) -> bool:
        with self._lock:
            req = self._request(req_id)
            if not req:
                return False
            ratings = req.setdefault("ratings", {})
            stars = int(stars) if stars else 0
            if 1 <= stars <= 5:
                ratings[rater_key] = stars
            else:
                ratings.pop(rater_key, None)
            self.save()
            return True

    def set_request_status(self, req_id: str, status: str) -> bool:
        if status not in ("open", "sourced", "dismissed"):
            return False
        with self._lock:
            req = self._request(req_id)
            if not req:
                return False
            req["status"] = status
            self.save()
            return True

    def delete_song_request(self, req_id: str) -> bool:
        with self._lock:
            before = len(self.song_requests)
            self._data["song_requests"] = [
                r for r in self.song_requests if r.get("id") != req_id
            ]
            if len(self._data["song_requests"]) == before:
                return False
            self.save()
            return True

    def get_or_create_player_song_variant(
        self, base_song_id: str, player_id: str
    ) -> str | None:
        """Get or create a song variant for a player.

        Returns the variant song_id. If base_song_id doesn't exist, returns None.
        """
        base_song = self.songs.get(base_song_id)
        if not base_song:
            return None

        # Check if variant already exists for this player
        with self._lock:
            for sid, song in self.songs.items():
                if (song.get("base_song_id") == base_song_id and
                    song.get("created_by_player") == player_id):
                    return sid

            # Create new variant
            variant_id = uuid.uuid4().hex
            self.songs[variant_id] = {
                "filename": base_song["filename"],
                "display_name": base_song["display_name"],
                "start_ms": base_song.get("start_ms", 0),
                "end_ms": base_song.get("end_ms"),
                "alias": base_song.get("alias", ""),
                "base_song_id": base_song_id,
                "created_by_player": player_id,
            }
            self.save()
            return variant_id

    def get_base_song_id(self, song_id: str) -> str:
        """Get the base song id for a variant, or the song_id itself if it's a base."""
        song = self.songs.get(song_id)
        if song and song.get("base_song_id"):
            return song.get("base_song_id")
        return song_id

    def players_by_jersey(self) -> list[tuple[str, dict[str, Any]]]:
        """Players sorted by jersey number, for stable page layout."""
        return sorted(
            self.players.items(),
            key=lambda kv: (kv[1].get("jersey") or 0, kv[1].get("last_name", "")),
        )

    def players_by_name(self) -> list[tuple[str, dict[str, Any]]]:
        """Players sorted alphabetically by last then first name."""
        return sorted(
            self.players.items(),
            key=lambda kv: (
                (kv[1].get("last_name", "") or "").lower(),
                (kv[1].get("first_name", "") or "").lower(),
            ),
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

    # -- Stream Deck button editor (per-page slots) ----------------------
    # The physical deck has fixed nav/transport keys; the editor only assigns
    # the 21 "content" keys below. A page with any slots set is rendered from
    # those slots; a page with none falls back to its built-in auto-layout.

    # Content key indices on a Stream Deck XL (8x4) — everything that isn't a
    # fixed nav (0/8/16), transport (24/25/26) or page-shortcut (27-31) key.
    DECK_FIXED_SLOTS = _DECK_FIXED_SLOTS
    DECK_CONTENT_SLOTS = _DECK_CONTENT_SLOTS

    def set_page_slot(self, page_id: str, idx: int, slot: dict | None) -> None:
        """Assign or clear one content key on a page.

        ``slot`` = {type, ref, label, color}. A None/blank type clears the key.
        Fixed nav/transport keys are owned by the deck and ignored here.
        """
        idx = int(idx)
        if page_id not in self.pages or idx in self.DECK_FIXED_SLOTS:
            return
        with self._lock:
            slots = self.pages[page_id].setdefault("slots", {})
            if not slot or slot.get("type") in (None, "", "blank"):
                slots.pop(str(idx), None)
            else:
                slots[str(idx)] = {
                    "type": slot.get("type", ""),
                    "ref": slot.get("ref", ""),
                    "label": slot.get("label", ""),
                    "color": slot.get("color", ""),
                }
            self.save()

    def clear_page_slots(self, page_id: str) -> None:
        with self._lock:
            if page_id in self.pages:
                self.pages[page_id]["slots"] = {}
                self.save()

    def fill_player_slots(self, page_id: str, order: str = "jersey") -> None:
        """Bulk-fill a page's content keys with player walk-up buttons."""
        if page_id not in self.pages:
            return
        players = self.players_by_name() if order == "alpha" else self.players_by_jersey()
        slots: dict[str, Any] = {}
        for i, content_idx in enumerate(self.DECK_CONTENT_SLOTS):
            if i >= len(players):
                break
            pid, p = players[i]
            jersey = p.get("jersey", "")
            last = (p.get("last_name", "") or "")[:8]
            slots[str(content_idx)] = {
                "type": "player_walkup",
                "ref": pid,
                "label": f"#{jersey}\n{last}",
                "color": "",
            }
        with self._lock:
            self.pages[page_id]["slots"] = slots
            self.save()

    def add_page(self, name: str) -> str:
        """Create a custom (deletable) deck page; returns its id."""
        pid = uuid.uuid4().hex
        with self._lock:
            order = max((p.get("order", 0) for p in self.pages.values()), default=0) + 1
            self.pages[pid] = {
                "name": name.strip() or "Page",
                "kind": "custom",
                "order": order,
                "deletable": True,
                "slots": {},
            }
            self.save()
        return pid

    def rename_page(self, page_id: str, name: str) -> None:
        with self._lock:
            if page_id in self.pages and name.strip():
                self.pages[page_id]["name"] = name.strip()
                self.save()

    def delete_page(self, page_id: str) -> None:
        """Delete a custom page (built-in pages are protected)."""
        with self._lock:
            page = self.pages.get(page_id)
            if page and page.get("deletable"):
                del self.pages[page_id]
                self.save()

    def move_page(self, page_id: str, direction: int) -> None:
        """Reorder a page up (-1) or down (+1) by swapping order with a neighbor."""
        with self._lock:
            ordered = sorted(self.pages.items(), key=lambda kv: kv[1].get("order", 99))
            ids = [pid for pid, _ in ordered]
            if page_id not in ids:
                return
            i = ids.index(page_id)
            j = i + direction
            if 0 <= j < len(ids):
                a, b = ids[i], ids[j]
                self.pages[a]["order"], self.pages[b]["order"] = (
                    self.pages[b].get("order", 0), self.pages[a].get("order", 0))
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

    # -- device pairing / linking ----------------------------------------
    # A coach links a Pi by generating a short pairing code in the portal and
    # entering it on the Pi (captive portal or boot file). Redeeming the code
    # mints a per-device sync token, so devices can be named and revoked one at
    # a time without disturbing the others or the global ONDECK_SYNC_TOKEN.

    def create_pairing_code(self, name: str, role: str) -> dict[str, Any]:
        """Mint a short, time-limited pairing code for a named device."""
        code = secrets.token_hex(3).upper()  # 6 hex chars, e.g. "A1B2C3"
        expires_hours = self.system.get("signup_link_expires_hours", 168)
        now = _now_ms()
        entry = {
            "code": code,
            "name": (name or "OnDeck Pi").strip(),
            "role": role if role in ("audio", "deck") else "deck",
            "created_at": now,
            "expires_at": now + expires_hours * 3600 * 1000,
            "redeemed_by": None,
        }
        with self._lock:
            self.pairing_codes.append(entry)
            self.save()
        return entry

    def _find_pairing_code(self, code: str) -> dict[str, Any] | None:
        code = (code or "").strip().upper()
        now = _now_ms()
        for entry in self.pairing_codes:
            if (entry.get("code") == code
                    and not entry.get("redeemed_by")
                    and entry.get("expires_at", 0) > now):
                return entry
        return None

    def redeem_pairing_code(
        self, code: str, hostname: str = "", ip: str = ""
    ) -> tuple[str, str, str] | None:
        """Redeem a valid code → create a device and return (id, token, name).

        Returns None if the code is unknown, expired, or already used.
        """
        with self._lock:
            entry = self._find_pairing_code(code)
            if not entry:
                return None
            device_id = uuid.uuid4().hex
            token = secrets.token_hex(24)
            now = _now_ms()
            self.devices[device_id] = {
                "id": device_id,
                "name": entry["name"],
                "role": entry["role"],
                "token": token,
                "hostname": hostname or "",
                "ip": ip or "",
                "paired_at": now,
                "last_seen": "",
                "revoked": False,
            }
            entry["redeemed_by"] = device_id
            self.save()
            return device_id, token, entry["name"]

    def discovered_audio_ip(self) -> str | None:
        """LAN IP of the most-recently-seen, non-revoked audio-role device.

        Populated from `/sync/ping` (the Audio Pi reports its own IP), so the
        Stream Deck Pi can reach the Audio Pi without anyone hand-entering an IP.
        """
        best = None
        for d in self.devices.values():
            if d.get("role") == "audio" and not d.get("revoked") and d.get("ip"):
                if best is None or d.get("last_seen", "") > best.get("last_seen", ""):
                    best = d
        return best.get("ip") if best else None

    def audio_pi_endpoint(self) -> tuple[str, int]:
        """(ip, port) of the Audio Pi: explicit `audio_pi_ip` setting first, then
        the auto-discovered audio device, then localhost."""
        s = self.system
        try:
            port = int(s.get("audio_pi_port", 5100) or 5100)
        except (TypeError, ValueError):
            port = 5100
        ip = s.get("audio_pi_ip") or self.discovered_audio_ip() or "127.0.0.1"
        return ip, port

    def device_for_token(self, token: str) -> dict[str, Any] | None:
        """The (non-revoked) device that owns a sync token, if any."""
        if not token:
            return None
        for device in self.devices.values():
            if device.get("token") == token and not device.get("revoked"):
                return device
        return None

    def touch_device(self, token: str, ip: str = "", hostname: str = "") -> bool:
        """Record a check-in from the device that owns ``token``."""
        from datetime import datetime, timezone
        with self._lock:
            device = self.device_for_token(token)
            if not device:
                return False
            device["last_seen"] = datetime.now(timezone.utc).isoformat()
            if ip:
                device["ip"] = ip
            if hostname:
                device["hostname"] = hostname
            self.save(mark_dirty=False)
            return True

    def list_devices(self) -> list[dict[str, Any]]:
        """Devices for the admin page, newest pairing first."""
        return sorted(
            self.devices.values(),
            key=lambda d: d.get("paired_at", 0),
            reverse=True,
        )

    def rename_device(self, device_id: str, name: str) -> None:
        with self._lock:
            if device_id in self.devices:
                self.devices[device_id]["name"] = (name or "OnDeck Pi").strip()
                self.save()

    def revoke_device(self, device_id: str) -> None:
        """Remove a device; its sync token stops working immediately."""
        with self._lock:
            if device_id in self.devices:
                del self.devices[device_id]
                self.save()

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
