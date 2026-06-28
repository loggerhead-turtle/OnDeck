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

    # -- target resolution ------------------------------------------------

    def _base_url(self) -> str:
        ip, port = self.config.audio_pi_endpoint()
        return f"http://{ip}:{port}"

    def _post(self, path: str, payload: dict | None = None) -> dict | None:
        try:
            r = rq.post(self._base_url() + path, json=payload or {}, timeout=_TIMEOUT)
            return r.json()
        except Exception as exc:  # connection refused, timeout, bad JSON…
            log.warning("Audio Pi POST %s failed: %s", path, exc)
            return None

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

    def spotify_off(self) -> bool:
        """Hard-kill the Audio Pi's Spotify Connect service (off-only)."""
        return self._post("/spotify/disable") is not None

    def set_volume(self, level: int) -> bool:
        return self._post("/volume", {"level": int(level)}) is not None

    def status(self) -> dict | None:
        return self._get("/status")

    # -- high-level cues --------------------------------------------------
    # These are what the Stream Deck actually calls. Each one builds the clip
    # from config and fires queue→play in a single hop.

    def play_clip(self, clip: dict[str, Any] | None) -> bool:
        """Queue then immediately play a pre-built clip."""
        if not clip:
            return False
        return self.queue(clip) and self.play()

    def play_walkup(self, player_id: str) -> bool:
        """Announce a player and drop their walk-up song on cue (queue + play)."""
        return self.play_clip(self.config.build_walkup_clip(player_id))

    def cue_walkup(self, player_id: str) -> bool:
        """Queue a player's walk-up without playing it (the lineup cue step)."""
        clip = self.config.build_walkup_clip(player_id)
        return self.queue(clip) if clip else False

    def play_song(self, song_id: str) -> bool:
        """Play a plain library song (hype, mid-inning, stingers, …)."""
        return self.play_clip(self.config.build_song_clip(song_id))

    def play_celebration(self, kind: str) -> bool:
        """Fire a celebration stinger (hit/extra_base/home_run/strikeout)."""
        sid = self.config.get_celebration_song(kind)
        return self.play_song(sid) if sid else False
