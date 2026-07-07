"""Live game-state persistence — survive a crash or power loss mid-game.

The roster, songs and pages live in config.json (cloud-synced). What is *not*
config is the live state of the game right now: which batter is up, and which
page the Stream Deck is showing. That used to be memory-only, so a power
outage at the field meant the deck came back on Home with the lineup reset to
the leadoff hitter.

This module keeps that state in ``$ONDECK_HOME/game_state.json`` — local to
the Stream Deck Pi and never synced (like bluetooth.json on the Audio Pi).
Writes are tiny, atomic (temp file + fsync + rename, same as config.json) and
happen only on human-scale events (batter change, page change), so the SD
card is not stressed.

On startup, ``LineupManager`` restores the batter index and re-cues that
batter as soon as the Audio Pi is reachable, and ``StreamDeckController``
returns to the saved page — the game picks up where it left off.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

from config_manager import ONDECK_HOME

GAME_STATE_PATH = ONDECK_HOME / "game_state.json"

_lock = threading.Lock()


def load_game_state() -> dict[str, Any]:
    """The saved state, or {} when there is none (or it is unreadable)."""
    try:
        data = json.loads(GAME_STATE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def update_game_state(**fields: Any) -> None:
    """Merge ``fields`` into the state file atomically. Never raises."""
    with _lock:
        try:
            state = load_game_state()
            state.update(fields)
            ONDECK_HOME.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(ONDECK_HOME), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(state, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, GAME_STATE_PATH)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except Exception:
            # Persistence is best-effort; losing a save must never break play.
            pass
