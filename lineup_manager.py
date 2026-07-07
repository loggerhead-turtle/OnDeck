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
memory here — and mirrored to ``game_state.json`` on every change so a crash,
service restart or power outage resumes mid-game instead of starting the order
over. On startup :meth:`restore` reloads the saved index and re-cues that
batter as soon as the Audio Pi answers (the status poller retries the cue, so
it works even when the Audio Pi boots slower than the deck Pi). End-of-song is
detected by polling the Audio Pi's status (the song plays on a different Pi,
so there is no local end-of-song callback to hook).

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
from game_state import load_game_state, update_game_state
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
        # Set when the restored batter couldn't be cued yet (Audio Pi still
        # booting); the status poller retries until the cue lands.
        self._restore_cue_pending = False

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
        self._persist()
        self._notify()

    def advance(self) -> None:
        """Move to the next filled slot, wrapping at the end of the order."""
        with self._lock:
            filled = self._filled_indices()
            if not filled:
                return
            after = [i for i in filled if i > self._index]
            self._index = after[0] if after else filled[0]
        self._persist()
        self._notify()

    def reset(self) -> bool:
        """Jump back to the top of the order (leadoff) and cue that batter.

        The "start the lineup over" button: clears any armed auto-advance,
        moves to the first filled slot, and cues them so Play is ready.
        """
        with self._lock:
            self._queued_batter = False
            self._armed = False
            filled = self._filled_indices()
            self._index = filled[0] if filled else 0
        self._persist()
        self._notify()
        return self.cue_current()

    # -- crash / power-loss recovery ---------------------------------------

    def _persist(self) -> None:
        """Mirror the current batter to game_state.json (best-effort)."""
        update_game_state(lineup_index=self._index)

    def restore(self) -> None:
        """Resume the saved batting position after a restart.

        Re-cues the restored batter so Play works immediately; if the Audio Pi
        isn't up yet (both Pis rebooting after an outage), the status poller
        retries the cue until it lands.
        """
        saved = load_game_state().get("lineup_index")
        if not isinstance(saved, int):
            return
        with self._lock:
            if not (0 <= saved < len(self.config.lineup)):
                return
            self._index = saved
        log.info("Restored batting position %s from game_state.json", saved + 1)
        if self.current_player_id() and not self.cue_current():
            with self._lock:
                self._restore_cue_pending = True

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
        return ok

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
            # The Audio Pi is answering — finish a pending post-restart cue so
            # the restored batter is loaded and Play works right away.
            with self._lock:
                retry_cue = self._restore_cue_pending
                self._restore_cue_pending = False
            if retry_cue and self.current_player_id():
                log.info("Audio Pi is back — re-cueing the restored batter")
                self.cue_current()
                if self.on_change:
                    self._notify()
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
