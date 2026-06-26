#!/usr/bin/env python3
"""Render the Stream Deck pages to PNGs without any hardware.

Drives the real ``StreamDeckController`` rendering code against a fake deck and
composes the 32 keys into one image per page — so you can iterate on layout and
colours from a laptop, and see exactly what the physical Stream Deck XL shows.

Usage:
    python tools/preview_deck.py                 # uses your ~/ondeck config
    python tools/preview_deck.py --demo          # seed a demo team first
    python tools/preview_deck.py --out /tmp/deck # choose output directory

Pages rendered: home, lineup, players, hype, mid_inning, mound_visit,
dead_ball, celebrations, pitcher_warmup.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# Allow running from the repo root: `python tools/preview_deck.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Stream Deck XL geometry, used only for the composite preview image.
KEY = 96
GAP = 10
COLS = 8
ROWS = 4

PAGES = [
    ("home", "1_home"),
    ("lineup", "2_lineup"),
    ("players", "3_players"),
    ("hype", "4_hype"),
    ("mid_inning", "5_mid_inning"),
    ("mound_visit", "6_mound_visit"),
    ("dead_ball", "7_dead_ball"),
    ("celebrations", "8_celebrations"),
    ("pitcher_warmup", "9_pitcher_warmup"),
]


def _seed_demo(cfg) -> None:
    """Populate a config with a 9-player team and sample songs."""
    roster = [
        (24, "Bryce", "Carter"), (7, "Diego", "Ramos"), (12, "Jake", "Smith"),
        (33, "Marcus", "Lee"), (5, "Owen", "Walsh"), (9, "Tyler", "Nguyen"),
        (15, "Sam", "Brooks"), (2, "Eli", "Foster"), (44, "Cole", "Hayes"),
    ]
    pids = [cfg.add_player(j, f, l) for (j, f, l) in roster]
    songs = {}
    for key, name in [
        ("walk", "Walk-Up"), ("hype", "Stadium Hype"), ("mid", "Seventh Stretch"),
        ("mound", "Mound Visit"), ("dead", "Rain Delay"), ("warm", "Bullpen Warm"),
        ("hr", "Home Run Horn"), ("hit", "Base Hit"), ("xbh", "Double Trouble"),
        ("k", "Strikeout K"),
    ]:
        songs[key] = cfg.add_song(f"{key}.mp3", name)
    for pid in pids:
        cfg.players[pid]["walkup_song_id"] = songs["walk"]
        cfg.players[pid]["announcement_file"] = "ann.webm"
        cfg.players[pid]["music_cue_ms"] = 1500
    for i, pid in enumerate(pids):
        cfg.data["lineup"][i] = pid
    cfg.data["page_songs"].update(
        hype=[songs["hype"]], mid_inning=[songs["mid"]],
        mound_visit=[songs["mound"]], dead_ball=[songs["dead"]],
        pitcher_warmup=[songs["warm"]],
    )
    cfg.celebrations.update(hit=songs["hit"], extra_base=songs["xbh"],
                            home_run=songs["hr"], strikeout=songs["k"])
    cfg.save()


def main() -> None:
    parser = argparse.ArgumentParser(description="Render Stream Deck pages to PNGs.")
    parser.add_argument("--demo", action="store_true",
                        help="seed a demo team into a throwaway config first")
    parser.add_argument("--out", default="deck_preview",
                        help="output directory for the PNGs (default: deck_preview)")
    parser.add_argument("--current", type=int, default=2,
                        help="batting-order index to highlight on the lineup page")
    args = parser.parse_args()

    if args.demo:
        os.environ["ONDECK_HOME"] = tempfile.mkdtemp(prefix="ondeck_preview_")

    from PIL import Image
    from config_manager import ConfigManager
    from music_client import MusicClient
    from lineup_manager import LineupManager
    import streamdeck_controller as sdc

    cfg = ConfigManager()
    if args.demo:
        _seed_demo(cfg)

    imgs: dict[int, "Image.Image"] = {}

    class FakeDeck:
        def key_image_format(self):
            return {"size": (KEY, KEY)}

        def set_key_image(self, idx, img):
            imgs[idx] = img

        def reset(self):
            pass

        def close(self):
            pass

        def set_key_callback(self, cb):
            pass

    # Keep the rendered PIL image instead of converting to the wire format.
    sdc.PILHelper.to_native_key_format = lambda deck, img: img

    music = MusicClient(cfg)
    lineup = LineupManager(cfg, music)
    ctrl = sdc.StreamDeckController.__new__(sdc.StreamDeckController)
    ctrl.config, ctrl.lineup, ctrl.music = cfg, lineup, music
    ctrl.current_page_id, ctrl.deck = "home", FakeDeck()
    lineup.on_change = ctrl.refresh
    lineup.set_current(args.current)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    width = COLS * KEY + (COLS + 1) * GAP
    height = ROWS * KEY + (ROWS + 1) * GAP
    for page_id, fname in PAGES:
        imgs.clear()
        ctrl.go_to_page(page_id)
        canvas = Image.new("RGB", (width, height), (8, 8, 8))
        for idx, img in imgs.items():
            r, c = divmod(idx, COLS)
            canvas.paste(img, (GAP + c * (KEY + GAP), GAP + r * (KEY + GAP)))
        path = out_dir / f"deck_{fname}.png"
        canvas.save(path)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
