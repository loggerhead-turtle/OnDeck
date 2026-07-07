"""Batting-order state for live game-day operation.

The lineup lives in config as a fixed-length list of player ids (``None`` for an
empty slot). This module tracks *who is up right now* and drives the live
walk-up flow the coach asked for:

  1. Press a batter's tile  → their walk-up is **cued** (queued on the Audio Pi
     but silent).
  2. Press **Play**         → the cued walk-up runs (announcement + song).
  3. Song ends              → the lineup **auto-advances** to the next hitter and
     **re-cues** them, queued and ready. The coach just presses Play again.

The "current batter" is live game state, not configuration, so it is held in
memory here rather than persisted — restarting mid-game starts at the top of the
order. End-of-song is detected by polling the Audio Pi's status (the song plays
on a different Pi, so there is no local end-of-song callback to hook).

Playing non-lineup audio (hype, a stinger, a one-off player) must NOT advance the
batting order, so the controller calls :meth:`note_external_playback` whenever it
fires a non-lineup cue; that disarms auto-advance until the next batter is cued.
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
        # Live playback state for the cue → play → auto-advance flow.
        self._queued_batter = False     # the Audio Pi queue holds a walk-up
        self._armed = False             # auto-advance when this playback ends
        self._was_playing = False       # for edge detection in the poller
        self._poller_started = False
        self._last_played: int | None = None  # slot of the last walk-up run

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
        """Jump to an explicit batting-order slot (a direct Stream Deck press)."""
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

    # -- cue / play -------------------------------------------------------

    def cue_current(self) -> bool:
        """Cue (queue, but don't play) the current batter's walk-up."""
        pid = self.current_player_id()
        if not pid:
            return False
        ok = self.music.cue_walkup(pid)
        with self._lock:
            self._queued_batter = ok
            self._armed = False
        return ok

    def play(self) -> bool:
        """Run whatever walk-up is cued; arm auto-advance for when it ends."""
        ok = self.music.play()
        with self._lock:
            if ok and self._queued_batter:
                self._armed = True
                self._last_played = self._index
        return ok

    def replay(self) -> bool:
        """Run the most recent walk-up again.

        After a walk-up ends the order has already advanced to the next hitter,
        so "replay" jumps back to whoever just played, cues them, and plays
        immediately. When it finishes, auto-advance resumes from there as
        normal. Before anything has played, it just runs the current batter.
        """
        with self._lock:
            target = self._last_played if self._last_played is not None else self._index
        self.set_current(target)
        return self.cue_current() and self.play()

    def note_external_playback(self) -> None:
        """Coach played non-lineup audio — don't auto-advance the order for it."""
        with self._lock:
            self._queued_batter = False
            self._armed = False

    # -- auto-advance poller ---------------------------------------------

    def start_auto_advance(self) -> None:
        """Begin watching the Audio Pi so the lineup advances when a song ends."""
        with self._lock:
            if self._poller_started:
                return
            self._poller_started = True
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _poll_loop(self) -> None:
        while True:
            time.sleep(_POLL_INTERVAL)
            status = self.music.status()
            if not status:
                continue
            state = status.get("state")
            advance = False
            with self._lock:
                if state == "playing":
                    self._was_playing = True
                elif state == "stopped":
                    advance = self._was_playing and self._armed
                    self._was_playing = False
                    self._armed = False
                # "queued" is a resting state — leave the flags untouched.
            if advance:
                log.info("Walk-up finished — advancing and re-cueing lineup")
                self.advance()
                self.cue_current()   # queue the next batter, ready for Play

    # -- internal ---------------------------------------------------------

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception as exc:  # a render error must not break navigation
                log.warning("lineup on_change handler failed: %s", exc)
