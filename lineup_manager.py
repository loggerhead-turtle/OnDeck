"""Batting-order state for live game-day operation.

The lineup lives in config as a fixed-length list of player ids (``None`` for an
empty slot). This module tracks *who is up right now* and drives the live
walk-up flow the coach asked for:

  1. Press a batter's tile  → their walk-up is **cued** (queued on the Audio Pi
     but silent).
  2. Press **Play**         → the cued walk-up runs (announcement + song).
  3. Song ends              → the lineup **auto-advances** to the next hitter and
     **re-cues** them, queued and ready. The coach just presses Play again.

A batter cued WHILE a walk-up plays does not cut it: the Audio Pi parks the
clip and, when the song ends, rests in ``queued`` with it loaded rather than
``stopped`` — so the manual choice stands and the poller below does not
advance past it. Pressing Play swaps to the parked clip at once.

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

from config_manager import ConfigManager, cue_tag
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
        # Fires with the cue tag of whatever the Audio Pi now holds (None
        # when nothing is loaded), so the deck can keep the cued key lit.
        # It rides THIS poller rather than starting a second one: the deck
        # already asks the Audio Pi for its status twice a second and a
        # parallel loop would double that traffic to say the same thing.
        self.on_cue_change: Callable[[str | None], None] | None = None
        self._cue_tag: str | None = None
        # Same idea for the clip actually on the PA: its key paints red
        # while it plays, so the coach can tell "loaded" from "sounding".
        self.on_play_change: Callable[[str | None], None] | None = None
        self._play_tag: str | None = None
        # Live playback state for the cue → play → auto-advance flow.
        self._queued_batter = False     # the Audio Pi queue holds a walk-up
        self._armed = False             # auto-advance when this playback ends
        self._was_playing = False       # for edge detection in the poller
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
        if ok:
            # Announce it rather than waiting for the poller to notice.
            # This is the auto-advance path too: a song ends, the next
            # batter is cued here, and his key has to light straight away
            # or the deck spends half a second showing nothing loaded.
            self._set_cue(cue_tag("player", pid))
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
            self._poll_once(status)

    def _poll_once(self, status: dict) -> bool:
        """One poll of the Audio Pi; True when the lineup advanced."""
        state = status.get("state")
        advance = False
        with self._lock:
            if state == "playing":
                self._was_playing = True
            elif state == "stopped":
                advance = self._was_playing and self._armed
                self._was_playing = False
                self._armed = False
            # "queued" is a resting state — leave the flags untouched. It is
            # also where a song lands when the coach cued the next batter
            # during it: that choice already advanced the order by hand.
        self._note_cue(status)
        if advance:
            log.info("Walk-up finished — advancing and re-cueing lineup")
            self.advance()
            self.cue_current()   # queue the next batter, ready for Play
        return advance

    # -- cued-key tracking ------------------------------------------------

    def note_cue_tag(self, tag: str | None) -> None:
        """Record the cue the deck just fired, without waiting for a poll.

        A key that lights up half a second after the thumb leaves it reads
        as a laggy deck. The press knows what it cued, so it says so
        immediately; the poller below is what corrects the display when
        the clip changes from somewhere else — the portal's transport, a
        song ending, Stop.
        """
        with self._lock:
            self._cue_tag = tag

    def _note_cue(self, status: dict) -> None:
        """Fire on_cue_change when the Audio Pi's loaded clip changes.

        This is the half that catches what the deck did NOT do: a song
        reaching its end, Stop, or the portal's transport cueing something
        of its own. Those all have to un-light a key the deck lit.
        """
        queued = status.get("queued")
        self._set_cue(queued.get("cue") if isinstance(queued, dict) else None)
        playing = status.get("playing")
        self._set_play(playing.get("cue") if isinstance(playing, dict)
                       else None)

    def _set_cue(self, tag: str | None) -> None:
        with self._lock:
            if tag == self._cue_tag:
                return
            self._cue_tag = tag
        if self.on_cue_change:
            try:
                self.on_cue_change(tag)
            except Exception as exc:
                log.warning("lineup on_cue_change handler failed: %s", exc)

    def note_play_tag(self, tag: str | None) -> None:
        """Record what Play just started, without waiting for a poll."""
        with self._lock:
            self._play_tag = tag

    def _set_play(self, tag: str | None) -> None:
        with self._lock:
            if tag == self._play_tag:
                return
            self._play_tag = tag
        if self.on_play_change:
            try:
                self.on_play_change(tag)
            except Exception as exc:
                log.warning("lineup on_play_change handler failed: %s", exc)

    # -- internal ---------------------------------------------------------

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception as exc:  # a render error must not break navigation
                log.warning("lineup on_change handler failed: %s", exc)
