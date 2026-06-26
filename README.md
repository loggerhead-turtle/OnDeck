# OnDeck ⚾🎵

A baseball walk-up music system controlled by an Elgato Stream Deck and managed
through a web portal. Built for high-school game-day operation: announce a
player, drop their walk-up song right on cue, fire celebration sounds, and
manage your lineup — all from a Stream Deck at the field, with no internet
required during the game.

## What it does

- **Walk-up music** — each player gets a recorded announcement plus a walk-up
  song. Press the player's button, the announcer plays, and the music fades in
  at the exact moment you choose.
- **Lineup management** — assign players to batting-order slots on the Stream
  Deck. Press a batter's tile to cue their walk-up, press Play to run it; when
  the song ends the lineup auto-advances and re-cues the next hitter, ready for
  the next Play.
- **Game-day sounds** — hype music, mid-inning music, mound-visit music,
  dead-ball music, celebration stingers (hit / extra-base / home run /
  strikeout), and pitcher warm-up songs, each on its own page.
- **Smooth audio** — precise trim points (hundredths of a second), one-second
  fade-out, instant stop, and an announcement→music crossfade.
- **Web portal** — upload songs, record announcements straight from your phone's
  browser, import audio from a YouTube link, trim everything in a waveform
  editor with live playback, build the batting order, and assign songs to each
  game-day page and celebration stinger.
- **Offline-first** — runs entirely on a local Wi-Fi network at the field, and
  syncs back to the cloud when internet is available.

## Architecture

```
[Coach Pi — Stream Deck XL]
  Flask web portal (:5000)
  lineup_manager.py
  streamdeck_controller.py
        |
        |  HTTP over local Wi-Fi
        v
[Audio Pi — connected to field PA]
  music_server.py (:5100)
  ffmpeg playback / fade / crossfade
  yt-dlp audio import
  ~/ondeck/music/*.mp3
```

Three roles, each a Raspberry Pi on the same local network:

- **Coach Pi** — drives the Stream Deck and serves the web portal.
- **Audio Pi** — plugged into the field PA, plays the audio.
- (optional) a display Pi for scoreboard/LED, unchanged from sibling projects.

A single Pi can play more than one role for testing.

## Install

On a Raspberry Pi running Raspberry Pi OS:

```bash
git clone https://github.com/loggerhead-turtle/OnDeck.git
cd OnDeck
./install.sh
```

The installer detects the current user (it never assumes the `pi` account),
installs dependencies (`ffmpeg`, `yt-dlp`, Python packages), and sets up the
services. See [`install.sh`](install.sh) for details.

## Status

In development. The Audio Pi server, web portal, cloud sync, and the Coach Pi
Stream Deck controller are all in place:

- `main.py` — Coach Pi entry point (Stream Deck loop + web portal)
- `streamdeck_controller.py` — Stream Deck XL pages for lineup, players,
  game-day sounds, and celebrations
- `lineup_manager.py` — batting order with auto-advance when a walk-up ends
- `music_client.py` — Coach Pi → Audio Pi playback control over HTTP

## License

MIT — free for anyone to use. Not for sale.
