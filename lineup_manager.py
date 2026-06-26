"""Batting-order state for live game-day operation.

The lineup lives in config as a fixed-length list of player ids (``None`` for an
empty slot). This module tracks *who is up right now* and drives the two live
behaviours the README promises:

  * Each press of the lineup's "next batter" cues the next filled slot.
  * When a walk-up song ends on its own, the lineup auto-advances so the next
    press is already pointing at the following hitter.

The "current batter" is live game state, not configuration, so it is held in
memory here rather than persisted — restarting mid-game simply starts at the top
of the order. Auto-advance is detected by polling the Audio Pi's status (the
song plays on a different Pi, so there is no local end-of-song callback to hook).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from config_manager import ConfigManager
from music_client import MusicClient

log = logging.getLogger("lineup")

# How often to poll the Audio Pi for a playing→stopped transition.
_POLL_INTERVAL = 0.5


class LineupManager:
    def __init__(self, config: ConfigManager, music: MusicClient) -> None:
        self.config = config
        self.music = music
        self._index = 0                 # index into config.lineup
        self._lock = threading.RLock()
        # Set by the controller so auto-advance can repaint the deck.
        self.on_change: Callable[[], None] | None = None
        # Auto-advance bookkeeping.
        self._was_playing = False
        self._poller_started = False

    # -- order helpers ----------------------------------------------------

    def _filled_indices(self) -> list[int]:
        """Indices of lineup slots that actually hold a player."""
        return [i for i, pid in enumerate(self.config.lineup) if pid]

    @property
    def current_index(self) -> int:
        return self._index

    def current_player_id(self) -> str | None:
        lineup = self.config.lineup
        if 0 <= self._index < len(lineup):
            return lineup[self._index]
        return None

    # -- navigation -------------------------------------------------------

    def set_current(self, index: int) -> None:
        """Jump to an explicit batting-order slot (e.g. a direct Stream Deck press)."""
        with self._lock:
            if 0 <= index < len(self.config.lineup):
                self._index = index
        self._notify()

    def advance(self) -> None:
        """Move to the next filled slot, wrapping at the end of the order."""
        with self._lock:
            filled = self._filled_indices()
            if not filled:
                return
            after = [i for i in filled if i > self._index]
            self._index = after[0] if after else filled[0]
        self._notify()

    def cue_current(self) -> bool:
        """Play the current batter's walk-up (announcement + song)."""
        pid = self.current_player_id()
        if not pid:
            return False
        self._mark_playing()
        return self.music.play_walkup(pid)

    def cue_next(self) -> bool:
        """Advance to the next batter and cue their walk-up — the live-mode press."""
        self.advance()
        return self.cue_current()

    # -- auto-advance poller ---------------------------------------------

    def start_auto_advance(self) -> None:
        """Begin watching the Audio Pi so the lineup advances when a song ends."""
        with self._lock:
            if self._poller_started:
                return
            self._poller_started = True
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _mark_playing(self) -> None:
        # Remember that we just started something, so the poller can detect the
        # eventual stop and treat it as "song finished → advance".
        with self._lock:
            self._was_playing = True

    def _poll_loop(self) -> None:
        while True:
            time.sleep(_POLL_INTERVAL)
            status = self.music.status()
            if not status:
                continue
            state = status.get("state")
            with self._lock:
                was = self._was_playing
                if state == "playing":
                    self._was_playing = True
                    continue
                # We saw a clip we started reach its natural end.
                if was and state == "stopped":
                    self._was_playing = False
                    advance = True
                else:
                    advance = False
            if advance:
                log.info("Walk-up finished — auto-advancing lineup")
                self.advance()

    # -- internal ---------------------------------------------------------

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception as exc:  # a render error must not break navigation
                log.warning("lineup on_change handler failed: %s", exc)
