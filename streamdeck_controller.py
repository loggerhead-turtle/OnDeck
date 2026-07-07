#!/usr/bin/env python3
"""OnDeck Stream Deck controller — built on the shared pideck runtime.

pideck (github.com/loggerhead-turtle/pi-deck) owns the device lifecycle,
hot-replug watchdog, size-adaptive key layout (Mini → XL → Studio), the fixed
Prev/Home/Next column, bottom-row page shortcuts, dial events and key
rendering. This file keeps only what is OnDeck-specific: the Play/Stop/Fade
transport keys, walk-up cueing, lineup editing, replay, remote deck
navigation, knob volume, and the dynamic Lineup / Players / Celebrations /
song pages.

Layout (XL example — 32 keys, 4 rows x 8 cols; other sizes re-derive):
  Left column   0 / 8 / 16     Prev / Home / Next      (always visible)
  Bottom-left   24 / 25 / 26   Play / Stop / Fade      (always visible)
  Bottom row    27-31          page shortcuts          (first 5 pages)
  Content area  1-7,9-15,17-23 21 slots, per-page content

On a 2-row deck (Mini / Plus / Neo / Studio) only Prev/Next are fixed; the
transport lives on button slots (action: play/stop/fade) or, on a Stream
Deck +, on the knobs — dial 0 turns the Audio Pi volume and pushes to mute.

Walk-up flow: on the Lineup page a tile *cues* (queues) the batter's walk-up;
Play runs it; when the song ends the lineup auto-advances and re-cues the next
hitter, ready for the next Play. Other pages play immediately on press.

Every content button turns into an Audio Pi cue (a player walk-up, a library
song, or a celebration stinger). The deck holds no audio itself — it calls
``MusicClient``, which talks to the Audio Pi over the local network.
"""

from __future__ import annotations

import logging
import os
import threading

import requests as rq

try:
    from pideck import BaseDeckController
except ImportError:                       # dev/Pi checkout: sibling repo
    import sys
    from pathlib import Path
    for _cand in (Path(__file__).resolve().parent.parent / "pi-deck",
                  Path.home() / "pi-deck"):
        if (_cand / "pideck").is_dir():
            sys.path.insert(0, str(_cand))
            break
    from pideck import BaseDeckController

from config_manager import (
    ConfigManager,
    DECK_DEFAULT_FONT,
    DECK_DEFAULT_FONT_SIZE,
)
from game_state import load_game_state, update_game_state
from lineup_manager import LineupManager
from music_client import MusicClient

log = logging.getLogger("streamdeck")

# Celebration stingers, in the fixed order they appear on the page.
CELEBRATIONS = [
    ("hit", "Hit"),
    ("extra_base", "XBH"),
    ("home_run", "HR"),
    ("strikeout", "K"),
]

# Labels for the transport/utility action refs (slot actions + editor).
ACTION_LABELS = {
    "play": "Play",
    "stop": "Stop",
    "fade": "Fade",
    "replay": "↻\nReplay",
    "reset_lineup": "⟲ Reset\nLineup",
}


class StreamDeckController(BaseDeckController):
    PAGE_SHORTCUT_BTNS = tuple(range(27, 32))
    EXTRA_FIXED_BTNS = (24, 25, 26)
    HOME_PAGE_ID = "home"
    DEFAULT_BG = (40, 40, 40)
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

    def __init__(self, config: ConfigManager, lineup: LineupManager,
                 music: MusicClient) -> None:
        self.config = config
        self.lineup = lineup
        self.music = music

        # Transport keys — assigned per-layout in on_layout (None when the
        # deck is too small for fixed transport keys).
        self._btn_play = self._btn_stop = self._btn_fade = None

        # Edit-lineup mode: tap "Edit Lineup", tap a batting-order slot to arm
        # it, then tap a player on the players page to fill that spot.
        self._edit_lineup = False
        self._lineup_assign_pos = None   # 1-based position awaiting a player
        self._lineup_return_page = None  # page to bounce back to after picking

        # Knob volume (Stream Deck + / Studio): mirrors the Audio Pi level.
        self._volume = int(config.system.get("volume", 80) or 80)
        self._pre_mute = self._volume

        # Repaint the deck whenever the lineup auto-advances.
        self.lineup.on_change = self.refresh

        super().__init__(config)         # opens the deck + first render

        # Crash / power-loss recovery: come back on the page the coach was
        # using, not Home (the batter index is restored by LineupManager).
        saved_page = load_game_state().get("deck_page")
        if saved_page and saved_page in self.config.pages \
                and saved_page != self.current_page_id:
            self.current_page_id = saved_page
            log.info("Restored deck page %r from game_state.json", saved_page)
            self.render_all()

    # ── pideck hooks ─────────────────────────────────────

    def on_page_change(self, page_id) -> None:
        # Remember the visible page so a restart resumes where the coach was.
        update_game_state(deck_page=page_id)

    def on_layout(self, layout) -> None:
        """Claim the layout's transport keys for Play / Stop / Fade."""
        transport = list(layout.transport_btns)[:3]
        transport += [None] * (3 - len(transport))
        self._btn_play, self._btn_stop, self._btn_fade = transport
        self.EXTRA_FIXED_BTNS = tuple(b for b in transport if b is not None)

    def before_render(self) -> None:
        # Pick up any edits the web portal wrote to config.json since the last
        # paint — the portal runs its own ConfigManager against the same file.
        self.config.load()

    def render_fixed_keys(self) -> None:
        self.btn(self._btn_play, "▶\nPlay", (30, 110, 40))
        self.btn(self._btn_stop, "■\nStop", (90, 30, 30))
        self.btn(self._btn_fade, "↘\nFade", (30, 60, 90))

    def on_fixed_key(self, idx) -> None:
        if idx == self._btn_play:
            self.lineup.play()
            self.flash(idx)
        elif idx == self._btn_stop:
            self.music.stop()
            self.flash(idx)
        elif idx == self._btn_fade:
            self.music.fade()
            self.flash(idx)

    # ── Dials (Stream Deck + / Studio) ───────────────────
    # Dial 0 is the volume knob: turn to adjust the Audio Pi level, push to
    # mute / un-mute. Other dials are left for future assignments.

    def on_dial_turn(self, dial, steps) -> None:
        if dial != 0 or not steps:
            return
        self._volume = max(0, min(100, self._volume + steps * 2))
        if self._volume > 0:
            self._pre_mute = self._volume
        self.music.set_volume(self._volume)
        log.debug("Dial volume → %s", self._volume)

    def on_dial_push(self, dial, pressed) -> None:
        if dial != 0 or not pressed:
            return
        if self._volume > 0:
            self._pre_mute, self._volume = self._volume, 0
        else:
            self._volume = self._pre_mute or 80
        self.music.set_volume(self._volume)

    # ── Content handler ──────────────────────────────────

    def on_content_key(self, btn_idx) -> None:
        page = self.config.pages.get(self.current_page_id, {})
        # A page with hand-edited slots is driven entirely by them (the web
        # Stream Deck editor); otherwise fall back to the built-in auto-layout.
        if page.get("slots"):
            self._handle_slot_press(btn_idx)
            return

        kind = page.get("kind", self.current_page_id)
        slot = self.content_slots.index(btn_idx)

        if kind == "home":
            order = self.page_order()
            if slot < len(order):
                self.go_to_page(order[slot])

        elif kind == "lineup":
            # Cue (queue) this batter; the coach presses Play to run it.
            filled = [i for i, pid in enumerate(self.config.lineup) if pid]
            if slot < len(filled):
                self.lineup.set_current(filled[slot])
                if self.lineup.cue_current():
                    self.flash(btn_idx)

        elif kind == "players":
            players = self.config.players_by_jersey()
            if slot < len(players):
                pid, _ = players[slot]
                # Edit-lineup mode: fill the armed batting spot instead of playing.
                if self._edit_lineup and self._lineup_assign_pos is not None:
                    self._assign_player_to_lineup(pid, btn_idx)
                    return
                self.lineup.note_external_playback()
                if self.music.play_walkup(pid):
                    self.flash(btn_idx)

        elif kind == "celebrations":
            if slot < len(CELEBRATIONS):
                key, _ = CELEBRATIONS[slot]
                self.lineup.note_external_playback()
                if self.music.play_celebration(key):
                    self.flash(btn_idx)

        else:
            # Song-list pages: hype / mid_inning / mound_visit / dead_ball /
            # pitcher_warmup. One button per assigned song, played immediately.
            songs = self.config.get_songs_for_page(self.current_page_id)
            if slot < len(songs):
                sid, _ = songs[slot]
                self.lineup.note_external_playback()
                if self.music.play_song(sid):
                    self.flash(btn_idx)

    # ── Rendering ────────────────────────────────────────

    def render_content(self) -> None:
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
        order = self.page_order()
        pages = self.pages
        for i, btn_idx in enumerate(self.content_slots):
            if i < len(order):
                pid = order[i]
                pname = pages.get(pid, {}).get("name", pid)
                active = (pid == self.current_page_id)
                col = self.ACTIVE_COLOR if active else self.PAGE_BG.get(pid, self.DEFAULT_BG)
                fg = (0, 0, 0) if active else (200, 200, 200)
                self.btn(btn_idx, pname[:10], col, fg)
            else:
                self.blank(btn_idx)

    def _render_lineup_page(self) -> None:
        """Batting order — one button per filled slot, current batter glows."""
        lineup = self.config.lineup
        filled = [i for i, pid in enumerate(lineup) if pid]
        cur = self.lineup.current_index
        for i, btn_idx in enumerate(self.content_slots):
            if i < len(filled):
                slot_idx = filled[i]
                player = self.config.players.get(lineup[slot_idx], {})
                jersey = player.get("jersey", "")
                first = (player.get("first_name", "") or "")[:8]
                active = (slot_idx == cur)
                bg = self.ACTIVE_COLOR if active else self.PAGE_BG["lineup"]
                fg = (0, 0, 0) if active else (255, 255, 255)
                self.btn(btn_idx, f"{i + 1}. #{jersey}\n{first}", bg, fg)
            else:
                self.blank(btn_idx)

    def _render_players_page(self) -> None:
        """Every player, by jersey number — a press plays their walk-up."""
        players = self.config.players_by_jersey()
        for i, btn_idx in enumerate(self.content_slots):
            if i < len(players):
                _pid, p = players[i]
                jersey = p.get("jersey", "")
                first = (p.get("first_name", "") or "")[:8]
                has_walkup = bool(p.get("walkup_song_id"))
                bg = self.PAGE_BG["players"] if has_walkup else (35, 35, 35)
                self.btn(btn_idx, f"#{jersey}\n{first}", bg, (255, 255, 255))
            else:
                self.blank(btn_idx)

    def _render_celebrations_page(self) -> None:
        """Four stingers — dim if no song is assigned yet."""
        for i, btn_idx in enumerate(self.content_slots):
            if i < len(CELEBRATIONS):
                key, label = CELEBRATIONS[i]
                configured = bool(self.config.get_celebration_song(key))
                bg = self.PAGE_BG["celebrations"] if configured else (35, 20, 25)
                self.btn(btn_idx, label, bg, (255, 255, 255))
            else:
                self.blank(btn_idx)

    def _render_song_page(self) -> None:
        """A song-list page — one labelled button per assigned song."""
        songs = self.config.get_songs_for_page(self.current_page_id)
        bg = self.PAGE_BG.get(self.current_page_id, self.DEFAULT_BG)
        for i, btn_idx in enumerate(self.content_slots):
            if i < len(songs):
                _sid, song = songs[i]
                name = (song.get("display_name", "") or "")[:14]
                self.btn(btn_idx, name, bg, (255, 255, 255))
            else:
                self.blank(btn_idx)

    # ── Slot-driven pages (web Stream Deck editor) ───────

    def _player_label(self, player_id: str) -> str:
        """Number + first name — the canonical look for a player/lineup key."""
        p = self.config.players.get(player_id, {})
        return f"#{p.get('jersey', '')}\n{(p.get('first_name', '') or '')[:8]}"

    def _slot_default_label(self, slot: dict) -> str:
        """A sensible button label when the editor left the label blank."""
        kind, ref = slot.get("type"), slot.get("ref", "")
        if kind == "player_walkup":
            return self._player_label(ref)
        if kind == "song":
            return (self.config.get_song_display_name(ref) or "")[:14]
        if kind == "celebration":
            return dict(CELEBRATIONS).get(ref, ref)
        if kind == "nav":
            return (self.config.pages.get(ref, {}).get("name", ref) or "")[:10]
        if kind == "remote_nav":
            name = (self.config.pages.get(ref, {}).get("name", ref) or "")[:9]
            return f"→ {name}"
        if kind == "action":
            return ACTION_LABELS.get(ref, ref)
        if kind == "edit_lineup":
            return "Edit\nLineup"
        if kind == "lineup_slot":
            pos = self._slot_position(slot)
            lineup = self.config.lineup
            if pos and 0 < pos <= len(lineup) and lineup[pos - 1]:
                return f"{pos}. " + self._player_label(lineup[pos - 1])
            return f"{pos or '?'}.\nEmpty"
        return ""

    @staticmethod
    def _slot_position(slot: dict) -> int:
        """The 1-based batting-order position stored on a lineup_slot key."""
        try:
            return int(slot.get("ref") or 0)
        except (TypeError, ValueError):
            return 0

    def _render_slot_page(self) -> None:
        """Paint a page from its hand-edited slots.

        Empty keys stay dark (the "page.col.row" address is an editor-only aid);
        assigned and text-only keys honour the editor's label/colours/font.
        """
        slots = self.config.pages.get(self.current_page_id, {}).get("slots", {})
        for btn_idx in self.content_slots:
            slot = slots.get(str(btn_idx))
            if not slot or slot.get("type") in (None, "", "blank"):
                self.blank(btn_idx)
                continue
            kind = slot.get("type")
            label = slot.get("label") or self._slot_default_label(slot)
            bg = self.hex2rgb(slot["color"]) if slot.get("color") else self.DEFAULT_BG
            fg = self.hex2rgb(slot["text_color"]) if slot.get("text_color") else (255, 255, 255)
            font = slot.get("font") or DECK_DEFAULT_FONT
            size = int(slot.get("font_size") or DECK_DEFAULT_FONT_SIZE)
            # Live state: the active Edit-Lineup key and the armed batting slot
            # glow yellow so the coach can see what's being edited.
            if kind == "edit_lineup" and self._edit_lineup:
                bg, fg = self.ACTIVE_COLOR, (0, 0, 0)
            elif (kind == "lineup_slot" and self._edit_lineup
                  and self._slot_position(slot) == self._lineup_assign_pos):
                bg, fg = self.ACTIVE_COLOR, (0, 0, 0)
            self.btn(btn_idx, label[:16], bg, fg, font=font, font_size=size)

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
        if kind == "text":
            return  # a label-only key — nothing to do
        elif kind == "edit_lineup":
            self._toggle_edit_lineup(btn_idx)
            return
        elif kind == "lineup_slot":
            self._press_lineup_slot(slot, btn_idx)
            return
        elif kind == "player_walkup":
            # In edit-lineup mode a player press fills the armed batting slot
            # instead of playing the walk-up.
            if self._edit_lineup and self._lineup_assign_pos is not None:
                self._assign_player_to_lineup(ref, btn_idx)
                return
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
        elif kind == "remote_nav":
            # Navigation deck: send another Stream Deck Pi (or all of them)
            # to a page. Fire-and-forget on a thread so the HID callback
            # never blocks on a slow peer.
            self._send_remote_nav(ref, slot.get("target") or "all")
            self.flash(btn_idx, "→")
            return
        elif kind == "action":
            if ref == "play":
                self.lineup.play()
                ok = True
            elif ref == "stop":
                ok = self.music.stop()
            elif ref == "fade":
                ok = self.music.fade(int(slot.get("fade_ms") or 1000))
            elif ref == "replay":
                # Re-run the last thing that played, without advancing the
                # batting order for it.
                self.lineup.note_external_playback()
                ok = self.music.replay()
            elif ref == "reset_lineup":
                ok = self.lineup.reset()
        if ok:
            self.flash(btn_idx)

    # ── Remote deck navigation ───────────────────────────

    def _send_remote_nav(self, page_id: str, target: str) -> None:
        """POST /ondeck/api/deck/goto to peer Stream Deck Pis.

        Peers are the non-revoked deck-role devices from config (their IPs are
        kept fresh by /sync/ping and synced down to every Pi). ``target`` is a
        device id or "all". Page ids are shared config, so ``page_id`` means
        the same page on every deck.
        """
        port = int(os.environ.get("ONDECK_PORTAL_PORT", "5000"))
        peers = [
            d for did, d in self.config.devices.items()
            if d.get("role") == "deck" and not d.get("revoked") and d.get("ip")
            and (target == "all" or did == target or d.get("id") == target)
        ]
        if not peers:
            log.warning("remote_nav: no reachable deck devices for target %r", target)
            return

        def _post():
            for dev in peers:
                url = f"http://{dev['ip']}:{port}/ondeck/api/deck/goto"
                try:
                    rq.post(url, json={"page": page_id}, timeout=3)
                    log.info("remote_nav → %s (%s) page=%s",
                             dev.get("name"), dev.get("ip"), page_id)
                except Exception as exc:
                    log.warning("remote_nav to %s failed: %s", dev.get("ip"), exc)

        threading.Thread(target=_post, daemon=True).start()

    # ── Edit-lineup flow ─────────────────────────────────

    def _players_page_id(self) -> str | None:
        """The first built-in 'players' page — where lineup picks happen."""
        for pid in self.page_order():
            if self.config.pages.get(pid, {}).get("kind") == "players":
                return pid
        return None

    def _toggle_edit_lineup(self, btn_idx: int) -> None:
        self._edit_lineup = not self._edit_lineup
        if not self._edit_lineup:
            self._lineup_assign_pos = None
            self._lineup_return_page = None
        self.flash(btn_idx)

    def _press_lineup_slot(self, slot: dict, btn_idx: int) -> None:
        pos = self._slot_position(slot)
        if not pos:
            return
        if self._edit_lineup:
            # Arm this batting spot and jump to the players page to pick someone.
            self._lineup_assign_pos = pos
            self._lineup_return_page = self.current_page_id
            players_page = self._players_page_id()
            if players_page:
                self.go_to_page(players_page)
            else:
                self.render_all()
            return
        # Normal press: cue this batter so the coach can hit Play.
        self.lineup.set_current(pos - 1)
        if self.lineup.cue_current():
            self.flash(btn_idx)

    def _assign_player_to_lineup(self, player_id: str, btn_idx: int) -> None:
        self.config.set_lineup_slot(self._lineup_assign_pos, player_id)
        return_page = self._lineup_return_page or "lineup"
        self._lineup_assign_pos = None
        self._lineup_return_page = None
        # Bounce back to where editing started; Edit-Lineup mode stays on so the
        # coach can set the next spot straight away.
        self.flash(btn_idx)
        if return_page in self.config.pages:
            self.go_to_page(return_page)
        else:
            self.render_all()
