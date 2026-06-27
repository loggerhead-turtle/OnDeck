#!/usr/bin/env python3
"""Stream Deck XL controller for OnDeck walk-up music.

32 buttons (4 rows x 8 cols), dynamic pages driven entirely by config — the same
layout play-call uses, retargeted from LED signs to baseball walk-up music.

  Left column   0 / 8 / 16     Prev / Home / Next      (always visible)
  Bottom-left   24 / 25 / 26   Play / Stop / Fade      (always visible)
  Bottom row    27-31          page shortcuts          (first 5 pages)
  Content area  1-7,9-15,17-23 21 slots, per-page content

Walk-up flow: on the Lineup page a tile *cues* (queues) the batter's walk-up;
Play runs it; when the song ends the lineup auto-advances and re-cues the next
hitter, ready for the next Play. Other pages play immediately on press.

Every content button turns into an Audio Pi cue (a player walk-up, a library
song, or a celebration stinger). The deck holds no audio itself — it calls
``MusicClient``, which talks to the Audio Pi over the local network.
"""

from __future__ import annotations

import logging
import threading
import time

from PIL import Image, ImageDraw, ImageFont
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper

from config_manager import (
    ConfigManager,
    DECK_COLS,
    DECK_DEFAULT_FONT,
    DECK_DEFAULT_FONT_SIZE,
    DECK_FONTS,
)
from lineup_manager import LineupManager
from music_client import MusicClient

log = logging.getLogger("streamdeck")

# ── Fixed button indices ─────────────────────────────────
BTN_PREV = 0     # top-left
BTN_HOME = 8     # mid-left
BTN_NEXT = 16    # bottom-left of the nav column
BTN_PLAY = 24    # bottom row — run the cued walk-up
BTN_STOP = 25    # bottom row — stop instantly
BTN_FADE = 26    # bottom row — fade out
# Page shortcut buttons 27-31 (up to 5 pages shown along the bottom).
BOTTOM_PAGE_BTNS = list(range(27, 32))

FIXED_BTNS = ({BTN_PREV, BTN_HOME, BTN_NEXT, BTN_PLAY, BTN_STOP, BTN_FADE}
              | set(BOTTOM_PAGE_BTNS))

# Content slots: every button that isn't fixed → [1-7, 9-15, 17-23] (21 slots).
CONTENT_SLOTS = [i for i in range(32) if i not in FIXED_BTNS]

ACTIVE_COLOR = (255, 220, 0)   # bright yellow — "this is live / selected"
EMPTY_COLOR = (18, 18, 18)     # unused slot

# Per-page background tint, keyed by the page's stable id.
PAGE_BG = {
    "home":           (30, 30, 30),
    "lineup":         (20, 60, 90),
    "players":        (20, 80, 40),
    "hype":           (90, 50, 20),
    "mid_inning":     (60, 60, 20),
    "mound_visit":    (80, 30, 80),
    "dead_ball":      (50, 50, 50),
    "celebrations":   (100, 20, 40),
    "pitcher_warmup": (20, 80, 80),
}
DEFAULT_BG = (40, 40, 40)

# Celebration stingers, in the fixed order they appear on the page.
CELEBRATIONS = [
    ("hit", "Hit"),
    ("extra_base", "XBH"),
    ("home_run", "HR"),
    ("strikeout", "K"),
]

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_DIR = "/usr/share/fonts/truetype/dejavu"

# Faint look for unassigned keys: a slightly-lifted background plus dim text so
# the "page.col.row" coordinate is legible but never loud.
BLANK_BG = (28, 28, 30)
BLANK_FG = (120, 120, 128)

_font_cache: dict[tuple, ImageFont.FreeTypeFont] = {}


def _resolve_font(family: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a deck font by family key + point size, cached, with fallbacks."""
    key = (family, size)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    meta = DECK_FONTS.get(family) or DECK_FONTS.get(DECK_DEFAULT_FONT, {})
    candidates = []
    if meta.get("file"):
        candidates.append(f"{_FONT_DIR}/{meta['file']}")
    candidates.append(FONT_PATH)
    font = None
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


class StreamDeckController:
    def __init__(self, config: ConfigManager, lineup: LineupManager,
                 music: MusicClient) -> None:
        self.config = config
        self.lineup = lineup
        self.music = music
        self.current_page_id = "home"

        # Repaint the deck whenever the lineup auto-advances.
        self.lineup.on_change = self.refresh

        self.deck = self._open_deck()
        if self.deck:
            self.deck.set_key_callback(self._on_key)
            self._render_all()

    # ── Public ───────────────────────────────────────────

    def run(self) -> None:
        if not self.deck:
            log.info("No Stream Deck — idle loop")
            while True:
                time.sleep(1)
        else:
            with self.deck:
                while True:
                    time.sleep(0.1)

    def close(self) -> None:
        if self.deck:
            self.deck.reset()
            self.deck.close()

    def refresh(self) -> None:
        """Repaint the deck (called by the web portal or lineup auto-advance)."""
        self._render_all()

    def go_to_page(self, page_id: str) -> None:
        if page_id in self.config.pages:
            self.current_page_id = page_id
            self._render_all()

    # ── Key handler ──────────────────────────────────────

    def _on_key(self, deck, idx, pressed) -> None:
        if not pressed:
            return
        if idx == BTN_PREV:
            self._nav(-1)
        elif idx == BTN_HOME:
            self.go_to_page("home")
        elif idx == BTN_NEXT:
            self._nav(1)
        elif idx == BTN_PLAY:
            self.lineup.play()
            self._flash(BTN_PLAY)
        elif idx == BTN_STOP:
            self.music.stop()
            self._flash(BTN_STOP)
        elif idx == BTN_FADE:
            self.music.fade()
            self._flash(BTN_FADE)
        elif idx in BOTTOM_PAGE_BTNS:
            self._handle_bottom_page_btn(idx)
        elif idx in CONTENT_SLOTS:
            self._handle_content(idx)

    # ── Navigation ───────────────────────────────────────

    def _nav(self, direction: int) -> None:
        order = self.config.get_page_order()
        if not order:
            return
        try:
            i = order.index(self.current_page_id)
        except ValueError:
            i = 0
        self.go_to_page(order[(i + direction) % len(order)])

    def _handle_bottom_page_btn(self, btn_idx: int) -> None:
        order = self.config.get_page_order()
        slot = btn_idx - BOTTOM_PAGE_BTNS[0]
        if slot < len(order):
            self.go_to_page(order[slot])

    # ── Content handler ──────────────────────────────────

    def _handle_content(self, btn_idx: int) -> None:
        page = self.config.pages.get(self.current_page_id, {})
        # A page with hand-edited slots is driven entirely by them (the web
        # Stream Deck editor); otherwise fall back to the built-in auto-layout.
        if page.get("slots"):
            self._handle_slot_press(btn_idx)
            return

        kind = page.get("kind", self.current_page_id)
        slot = CONTENT_SLOTS.index(btn_idx)

        if kind == "home":
            order = self.config.get_page_order()
            if slot < len(order):
                self.go_to_page(order[slot])

        elif kind == "lineup":
            # Cue (queue) this batter; the coach presses Play to run it.
            filled = [i for i, pid in enumerate(self.config.lineup) if pid]
            if slot < len(filled):
                self.lineup.set_current(filled[slot])
                if self.lineup.cue_current():
                    self._flash(btn_idx)

        elif kind == "players":
            players = self.config.players_by_jersey()
            if slot < len(players):
                pid, _ = players[slot]
                self.lineup.note_external_playback()
                if self.music.play_walkup(pid):
                    self._flash(btn_idx)

        elif kind == "celebrations":
            if slot < len(CELEBRATIONS):
                key, _ = CELEBRATIONS[slot]
                self.lineup.note_external_playback()
                if self.music.play_celebration(key):
                    self._flash(btn_idx)

        else:
            # Song-list pages: hype / mid_inning / mound_visit / dead_ball /
            # pitcher_warmup. One button per assigned song, played immediately.
            songs = self.config.get_songs_for_page(self.current_page_id)
            if slot < len(songs):
                sid, _ = songs[slot]
                self.lineup.note_external_playback()
                if self.music.play_song(sid):
                    self._flash(btn_idx)

    # ── Rendering ────────────────────────────────────────

    def _render_all(self) -> None:
        if not self.deck:
            return
        # Pick up any edits the web portal wrote to config.json since the last
        # paint — the portal runs its own ConfigManager against the same file.
        self.config.load()
        self._render_left_column()
        self._render_content_area()
        self._render_bottom_row()

    def _render_left_column(self) -> None:
        self._btn(BTN_PREV, "▲\nPrev", (40, 40, 40))
        self._btn(BTN_HOME, "⌂\nHome", (60, 60, 20))
        self._btn(BTN_NEXT, "▼\nNext", (40, 40, 40))

    def _render_bottom_row(self) -> None:
        self._btn(BTN_PLAY, "▶\nPlay", (30, 110, 40))
        self._btn(BTN_STOP, "■\nStop", (90, 30, 30))
        self._btn(BTN_FADE, "↘\nFade", (30, 60, 90))
        order = self.config.get_page_order()
        pages = self.config.pages
        for i, btn_idx in enumerate(BOTTOM_PAGE_BTNS):
            if i < len(order):
                pid = order[i]
                pname = pages[pid].get("name", pid)
                active = (pid == self.current_page_id)
                col = ACTIVE_COLOR if active else PAGE_BG.get(pid, DEFAULT_BG)
                fg = (0, 0, 0) if active else (200, 200, 200)
                self._btn(btn_idx, pname[:8], col, fg)
            else:
                self._btn(btn_idx, "", (15, 15, 15))

    def _render_content_area(self) -> None:
        page = self.config.pages.get(self.current_page_id, {})
        if page.get("slots"):
            self._render_slot_page()
            return
        kind = page.get("kind", self.current_page_id)
        if kind == "home":
            self._render_home_page()
        elif kind == "lineup":
            self._render_lineup_page()
        elif kind == "players":
            self._render_players_page()
        elif kind == "celebrations":
            self._render_celebrations_page()
        else:
            self._render_song_page()

    def _render_home_page(self) -> None:
        """One nav button per page, in order; the active page glows yellow."""
        order = self.config.get_page_order()
        pages = self.config.pages
        for i, btn_idx in enumerate(CONTENT_SLOTS):
            if i < len(order):
                pid = order[i]
                pname = pages.get(pid, {}).get("name", pid)
                active = (pid == self.current_page_id)
                col = ACTIVE_COLOR if active else PAGE_BG.get(pid, DEFAULT_BG)
                fg = (0, 0, 0) if active else (200, 200, 200)
                self._btn(btn_idx, pname[:10], col, fg)
            else:
                self._btn(btn_idx, "", EMPTY_COLOR)

    def _render_lineup_page(self) -> None:
        """Batting order — one button per filled slot, current batter glows."""
        lineup = self.config.lineup
        filled = [i for i, pid in enumerate(lineup) if pid]
        cur = self.lineup.current_index
        for i, btn_idx in enumerate(CONTENT_SLOTS):
            if i < len(filled):
                slot_idx = filled[i]
                player = self.config.players.get(lineup[slot_idx], {})
                jersey = player.get("jersey", "")
                last = (player.get("last_name", "") or "")[:8]
                active = (slot_idx == cur)
                bg = ACTIVE_COLOR if active else PAGE_BG["lineup"]
                fg = (0, 0, 0) if active else (255, 255, 255)
                self._btn(btn_idx, f"{i + 1}. #{jersey}\n{last}", bg, fg)
            else:
                self._btn(btn_idx, "", EMPTY_COLOR)

    def _render_players_page(self) -> None:
        """Every player, by jersey number — a press plays their walk-up."""
        players = self.config.players_by_jersey()
        for i, btn_idx in enumerate(CONTENT_SLOTS):
            if i < len(players):
                _pid, p = players[i]
                jersey = p.get("jersey", "")
                last = (p.get("last_name", "") or "")[:8]
                has_walkup = bool(p.get("walkup_song_id"))
                bg = PAGE_BG["players"] if has_walkup else (35, 35, 35)
                self._btn(btn_idx, f"#{jersey}\n{last}", bg)
            else:
                self._btn(btn_idx, "", EMPTY_COLOR)

    def _render_celebrations_page(self) -> None:
        """Four stingers — dim if no song is assigned yet."""
        for i, btn_idx in enumerate(CONTENT_SLOTS):
            if i < len(CELEBRATIONS):
                key, label = CELEBRATIONS[i]
                configured = bool(self.config.get_celebration_song(key))
                bg = PAGE_BG["celebrations"] if configured else (35, 20, 25)
                self._btn(btn_idx, label, bg)
            else:
                self._btn(btn_idx, "", EMPTY_COLOR)

    def _render_song_page(self) -> None:
        """A song-list page — one labelled button per assigned song."""
        songs = self.config.get_songs_for_page(self.current_page_id)
        bg = PAGE_BG.get(self.current_page_id, DEFAULT_BG)
        for i, btn_idx in enumerate(CONTENT_SLOTS):
            if i < len(songs):
                _sid, song = songs[i]
                name = (song.get("display_name", "") or "")[:14]
                self._btn(btn_idx, name, bg)
            else:
                self._btn(btn_idx, "", EMPTY_COLOR)

    # ── Slot-driven pages (web Stream Deck editor) ───────

    def _slot_default_label(self, slot: dict) -> str:
        """A sensible button label when the editor left the label blank."""
        kind, ref = slot.get("type"), slot.get("ref", "")
        if kind == "player_walkup":
            p = self.config.players.get(ref, {})
            return f"#{p.get('jersey', '')}\n{(p.get('last_name', '') or '')[:8]}"
        if kind == "song":
            return (self.config.get_song_display_name(ref) or "")[:14]
        if kind == "celebration":
            return dict(CELEBRATIONS).get(ref, ref)
        if kind == "nav":
            return (self.config.pages.get(ref, {}).get("name", ref) or "")[:10]
        if kind == "action":
            return {"play": "Play", "stop": "Stop", "fade": "Fade"}.get(ref, ref)
        return ""

    def _coord_label(self, btn_idx: int) -> str:
        """Addressable "page.col.row" tag for a key (all 1-based).

        The page number is the page's 1-based position in the deck order, so an
        unassigned key can be referenced when wiring buttons together later.
        """
        order = self.config.get_page_order()
        try:
            page_no = order.index(self.current_page_id) + 1
        except ValueError:
            page_no = 1
        col = btn_idx % DECK_COLS + 1
        row = btn_idx // DECK_COLS + 1
        return f"{page_no}.{col}.{row}"

    def _render_slot_page(self) -> None:
        """Paint a page from its hand-edited slots.

        Empty keys show a faint "page.col.row" coordinate so they stay
        addressable; assigned keys honour the editor's label/colours/font.
        """
        slots = self.config.pages.get(self.current_page_id, {}).get("slots", {})
        for btn_idx in CONTENT_SLOTS:
            slot = slots.get(str(btn_idx))
            if not slot or slot.get("type") in (None, "", "blank"):
                self._btn(btn_idx, self._coord_label(btn_idx), BLANK_BG, BLANK_FG,
                          font="mono", font_size=12)
                continue
            label = slot.get("label") or self._slot_default_label(slot)
            bg = self._hex2rgb(slot["color"]) if slot.get("color") else DEFAULT_BG
            fg = self._hex2rgb(slot["text_color"]) if slot.get("text_color") else (255, 255, 255)
            font = slot.get("font") or DECK_DEFAULT_FONT
            size = int(slot.get("font_size") or DECK_DEFAULT_FONT_SIZE)
            self._btn(btn_idx, label[:16], bg, fg, font=font, font_size=size)

    def _handle_slot_press(self, btn_idx: int) -> None:
        slots = self.config.pages.get(self.current_page_id, {}).get("slots", {})
        slot = slots.get(str(btn_idx))
        if not slot:
            return
        kind, ref = slot.get("type"), slot.get("ref", "")
        # "immediate" plays at once; "cue"/"queue" only loads the clip so the
        # coach runs it with the Play button (mirrors the Lineup cue flow).
        queue_only = slot.get("mode") in ("cue", "queue")
        ok = False
        if kind == "player_walkup":
            self.lineup.note_external_playback()
            ok = (self.music.cue_walkup(ref) if queue_only
                  else self.music.play_walkup(ref))
        elif kind == "song":
            self.lineup.note_external_playback()
            if queue_only:
                ok = self.music.queue(self.config.build_song_clip(ref) or {})
            else:
                ok = self.music.play_song(ref)
        elif kind == "celebration":
            self.lineup.note_external_playback()
            ok = self.music.play_celebration(ref)
        elif kind == "nav":
            self.go_to_page(ref)
            return
        elif kind == "action":
            if ref == "play":
                self.lineup.play()
                ok = True
            elif ref == "stop":
                ok = self.music.stop()
            elif ref == "fade":
                ok = self.music.fade(int(slot.get("fade_ms") or 1000))
        if ok:
            self._flash(btn_idx)

    # ── Button drawing ───────────────────────────────────

    def _btn(self, idx: int, label: str, bg: tuple,
             fg: tuple = (255, 255, 255), font: str = DECK_DEFAULT_FONT,
             font_size: int = DECK_DEFAULT_FONT_SIZE) -> None:
        if not self.deck:
            return
        try:
            sz = self.deck.key_image_format()["size"]
            img = Image.new("RGB", sz, bg)
            drw = ImageDraw.Draw(img)
            if label:
                fnt = _resolve_font(font, font_size)
                line_h = font_size + 3
                lines = label.split("\n")
                total = len(lines) * line_h
                y0 = (sz[1] - total) // 2
                for li, line in enumerate(lines):
                    bb = drw.textbbox((0, 0), line, font=fnt)
                    x = (sz[0] - (bb[2] - bb[0])) // 2
                    drw.text((x, y0 + li * line_h), line, font=fnt, fill=fg)
            self.deck.set_key_image(
                idx, PILHelper.to_native_key_format(self.deck, img))
        except Exception as exc:
            log.error("Button render error (idx %s): %s", idx, exc)

    def _flash(self, idx: int) -> None:
        """Briefly show a checkmark to confirm a press, then repaint."""
        def _do():
            self._btn(idx, "✓", (255, 255, 255), (0, 0, 0))
            time.sleep(0.35)
            self._render_all()
        threading.Thread(target=_do, daemon=True).start()

    # ── Helpers ──────────────────────────────────────────

    def _hex2rgb(self, h: str) -> tuple:
        h = h.lstrip("#")
        try:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        except Exception:
            return (50, 50, 50)

    def _open_deck(self):
        try:
            decks = DeviceManager().enumerate()
            if not decks:
                log.warning("No Stream Deck found — running without hardware")
                return None
            deck = decks[0]
            deck.open()
            deck.reset()
            deck.set_brightness(80)
            log.info("Stream Deck: %s (%s keys)",
                     deck.deck_type(), deck.key_count())
            return deck
        except Exception as exc:
            log.error("Stream Deck open failed: %s", exc)
            return None
