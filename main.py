#!/usr/bin/env python3
"""OnDeck Coach Pi entry point (Raspberry Pi + Stream Deck XL).

Wires together the field-side control unit:

  ConfigManager        — local config.json (synced from the cloud by the
                         sync_agent timer); single source of truth on game day
  MusicClient          — HTTP control of the Audio Pi (music_server.py)
  LineupManager        — batting-order state + auto-advance
  StreamDeckController  — the physical Stream Deck XL buttons
  web portal (Flask)    — phone/iPad control + admin, served on :5000

The web portal runs on a background thread; the Stream Deck event loop owns
the main thread. Everything is offline-first — no internet is needed once the
config and audio files are on the Pi.
"""

from __future__ import annotations

import logging
import os
import threading

from config_manager import ConfigManager
from lineup_manager import LineupManager
from music_client import MusicClient
from streamdeck_controller import StreamDeckController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-12s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.environ.get("ONDECK_PORTAL_PORT", "5000"))


def _serve_web() -> None:
    """Run the existing web portal (web/app.py) on a background thread.

    The portal keeps its own ConfigManager against the same config.json, so the
    Stream Deck (which reloads the file on each paint) reflects portal edits.
    """
    from web.app import app

    # Coach Pi-only routes (Wi-Fi + cloud link). Registered here so they never
    # exist on the cloud deployment, which runs web.app:app directly.
    try:
        from pi.web_routes import register as register_pi_routes
        register_pi_routes(app)
    except Exception as exc:  # missing optional deps must not stop the portal
        log.warning("Pi web routes not registered: %s", exc)

    log.info("Web portal → http://%s:%s", WEB_HOST, WEB_PORT)
    app.run(host=WEB_HOST, port=WEB_PORT, threaded=True, use_reloader=False)


def main() -> None:
    log.info("════════ OnDeck Coach Unit starting ════════")

    # Core objects — all share one config instance for the deck side.
    config = ConfigManager()
    music = MusicClient(config)
    lineup = LineupManager(config, music)

    # Stream Deck controller (built before the web thread so it's ready first).
    deck = StreamDeckController(config, lineup, music)

    # Auto-advance the batting order when a walk-up song ends on the Audio Pi.
    lineup.start_auto_advance()

    # Web portal on a background thread.
    threading.Thread(target=_serve_web, daemon=True).start()

    # Stream Deck event loop owns the main thread.
    try:
        log.info("System online — Stream Deck loop running")
        deck.run()
    except KeyboardInterrupt:
        log.info("Shutting down…")
    finally:
        deck.close()


if __name__ == "__main__":
    main()
