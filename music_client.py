"""HTTP client for the OnDeck Audio Pi.

The Coach Pi (Stream Deck + web portal) never plays audio itself — it tells the
Audio Pi what to do over the local network. This module is the single place that
knows how to talk to ``music_server.py``; the Stream Deck controller and the
lineup manager both go through it so playback behaviour stays consistent.

It is the OnDeck equivalent of play-call's ``AudioEngine``: same idea (turn a
button press into a sound), but the speakers live on a *different* Pi, so every
call is a short HTTP request instead of a local subprocess. Every call is
wrapped defensively — a missing or unreachable Audio Pi must never crash the
deck mid-game.
"""

from __future__ import annotations

import logging
from typing import Any

import requests as rq

from config_manager import ConfigManager

log = logging.getLogger("music")

# Keep requests short so a flaky Audio Pi never freezes the Stream Deck loop.
_TIMEOUT = 5


class MusicClient:
    """Thin wrapper over the Audio Pi's JSON endpoints."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        # The most recent refusal ("missing file: x.mp3") — shown on the
        # deck so silence always comes with a reason.
        self.last_error = ""


    # -- target resolution ------------------------------------------------

    def _base_url(self) -> str:
        ip, port = self.config.audio_pi_endpoint()
        return f"http://{ip}:{port}"

    def _post(self, path: str, payload: dict | None = None) -> dict | None:
        """POST and return the JSON body — or None when the call failed OR
        the Audio Pi answered ok=false.

        The ok check is what makes a missing music file audible to the
        deck: /play used to answer 200 for a file that wasn't on disk, so
        the key flashed 'played' while the speaker stayed silent."""
        try:
            r = rq.post(self._base_url() + path, json=payload or {}, timeout=_TIMEOUT)
            d = r.json()
        except Exception as exc:  # connection refused, timeout, bad JSON…
            log.warning("Audio Pi POST %s failed: %s", path, exc)
            # The deck shows this string when a press makes no sound. A
            # refusal set it; a box that never answered left the PREVIOUS
            # reason on display, which is worse than none.
            ip, port = self.config.audio_pi_endpoint()
            # Own line so the deck's key wrap can never truncate the
            # address — the IP is the diagnosis.
            self.last_error = f"no reply from\n{ip}"
            return None
        if isinstance(d, dict) and d.get("ok") is False:
            log.warning("Audio Pi POST %s refused: %s", path,
                        d.get("error") or "not ok")
            self.last_error = d.get("error") or "Audio Pi said no"
            return None
        self.last_error = ""
        return d

    def _get(self, path: str) -> dict | None:
        try:
            r = rq.get(self._base_url() + path, timeout=_TIMEOUT)
            return r.json()
        except Exception as exc:
            log.warning("Audio Pi GET %s failed: %s", path, exc)
            return None

    # -- transport controls ----------------------------------------------

    def queue(self, clip: dict[str, Any]) -> bool:
        return self._post("/queue", clip) is not None

    def play(self) -> bool:
        return self._post("/play") is not None

    def stop(self) -> bool:
        return self._post("/stop") is not None

    def fade(self, ms: int = 1000) -> bool:
        return self._post("/fade", {"ms": ms}) is not None

    def set_volume(self, level: int) -> bool:
        return self._post("/volume", {"level": int(level)}) is not None

    def status(self) -> dict | None:
        return self._get("/status")

    # -- Audio Pi sync ----------------------------------------------------
    # The music plays from the AUDIO Pi's disk, so 'press Sync, get all the
    # music' has to reach that box too — the deck syncing itself only
    # updates labels and lineups.

    def sync_audio_start(self) -> bool:
        """Kick off a sync ON the Audio Pi (its own sync_agent run)."""
        return self._post("/api/sync-now") is not None

    def sync_audio_status(self) -> dict | None:
        """{'running','ok','detail'} from the Audio Pi, or None when it
        cannot be reached."""
        return self._get("/api/sync-status")

    def library_missing(self) -> int | None:
        """How many library files the AUDIO Pi is missing on disk, or
        None when it can't say. 0 is the good answer."""
        st = self.status()
        lib = (st or {}).get("library")
        if not isinstance(lib, dict):
            return None
        return max(0, int(lib.get("expected", 0)) - int(lib.get("present", 0)))

    # -- high-level cues --------------------------------------------------
    # These are what the Stream Deck actually calls. Each one builds the clip
    # from config and fires queue→play in a single hop.

    def play_clip(self, clip: dict[str, Any] | None) -> bool:
        """Queue then immediately play a pre-built clip."""
        if not clip:
            # No HTTP call happens for a clip the local config can't
            # build, so without this the key would blame the Audio Pi
            # for a song this DECK doesn't know about.
            self.last_error = "not in this deck's config — press Sync"
            return False
        return self.queue(clip) and self.play()

    def play_walkup(self, player_id: str) -> bool:
        """Announce a player and drop their walk-up song on cue (queue + play)."""
        return self.play_clip(self.config.build_walkup_clip(player_id))

    def cue_walkup(self, player_id: str) -> bool:
        """Queue a player's walk-up without playing it (the lineup cue step)."""
        clip = self.config.build_walkup_clip(player_id)
        if not clip:
            self.last_error = "no walk-up set — check the portal, then Sync"
            return False
        return self.queue(clip)

    def play_song(self, song_id: str) -> bool:
        """Play a plain library song (hype, mid-inning, stingers, …)."""
        return self.play_clip(self.config.build_song_clip(song_id))

    def play_celebration(self, kind: str) -> bool:
        """Fire a celebration stinger (hit/extra_base/home_run/strikeout)."""
        sid = self.config.get_celebration_song(kind)
        if not sid:
            self.last_error = "no celebration song set"
            return False
        return self.play_song(sid)
