#!/usr/bin/env python3
"""OnDeck Stream Deck XL controller — built on the shared pideck runtime.

pideck (github.com/loggerhead-turtle/pi-deck) owns the device lifecycle,
hot-replug watchdog, fixed Prev/Home/Next column, bottom-row page shortcuts
and key rendering. This file keeps only what is OnDeck-specific: the
Play/Stop/Fade transport keys, walk-up cueing, lineup editing, and the
dynamic Lineup / Players / Celebrations / song pages.

Layout (32 keys, 4 rows x 8 cols):
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
import subprocess
import threading
import time

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
from lineup_manager import LineupManager
from music_client import MusicClient

log = logging.getLogger("streamdeck")

BTN_PLAY = 24    # bottom row — run the cued walk-up
BTN_STOP = 25    # bottom row — stop instantly
BTN_FADE = 26    # bottom row — fade out

# Celebration stingers, in the fixed order they appear on the page.
CELEBRATIONS = [
    ("hit", "Hit"),
    ("extra_base", "XBH"),
    ("home_run", "HR"),
    ("strikeout", "K"),
]


class StreamDeckController(BaseDeckController):
    PAGE_SHORTCUT_BTNS = tuple(range(27, 32))
    EXTRA_FIXED_BTNS = (BTN_PLAY, BTN_STOP, BTN_FADE)
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

        # Edit-lineup mode: tap "Edit Lineup", tap a batting-order slot to arm
        # it, then tap a player on the players page to fill that spot.
        self._edit_lineup = False
        self._lineup_assign_pos = None   # 1-based position awaiting a player
        self._lineup_return_page = None  # page to bounce back to after picking

        # Repaint the deck whenever the lineup auto-advances.
        self.lineup.on_change = self.refresh

        # Every attribute a render hook reads is set BEFORE the base
        # constructor: super().__init__ opens the deck and paints it, the
        # first paint calls before_render()/render hooks, and a hook
        # reading subclass state that does not exist yet dies on
        # AttributeError — the controller never claims the deck and the
        # hardware sits on its Elgato logo with the service "running".
        self._lineup_watch = None
        # Result of the last Sync press's AUDIO-PI half. The music plays
        # from that box's disk, so its sync outcome (and any files it is
        # still missing) belongs on the key.
        self._audio_sync = {'running': False, 'ok': None, 'missing': None}

        super().__init__(config)         # opens the deck + first render
        self._start_lineup_watch()

    # ── pideck hooks ─────────────────────────────────────

    def before_render(self) -> None:
        # Pick up any edits the web portal wrote to config.json since the last
        # paint — the portal runs its own ConfigManager against the same file.
        self.config.load()
        # Re-baseline from what we actually hold, so a sync by the 5-minute
        # timer or from the portal clears the "changed" flag too — not only
        # a press of the deck's own Sync key. getattr, not a bare read: this
        # hook runs during the BASE constructor's first paint, and a paint
        # must never depend on subclass state existing yet.
        if getattr(self, '_lineup_watch', None):
            self._lineup_watch.note_local_lineup(self.config.lineup)

    # ── Lineup watch ─────────────────────────────────────
    # Tells the operator a substitution happened. Never acts on it: the
    # deck must not repaint a key out from under a finger mid-press.

    def _start_lineup_watch(self) -> None:
        self._lineup_watch = None
        try:
            import lineup_watch
        except Exception as exc:
            log.info("lineup watch unavailable: %s", exc)
            return
        self._lineup_watch = lineup_watch
        try:
            lineup_watch.note_local_lineup(self.config.lineup)
            lineup_watch.start(on_change=lambda _s: self.refresh())
        except Exception:
            log.exception("could not start the lineup watch")
            self._lineup_watch = None

    def render_fixed_keys(self) -> None:
        self.btn(BTN_PLAY, "▶\nPlay", (30, 110, 40))
        self.btn(BTN_STOP, "■\nStop", (90, 30, 30))
        self.btn(BTN_FADE, "↘\nFade", (30, 60, 90))

    def on_fixed_key(self, idx) -> None:
        if idx == BTN_PLAY:
            # Play used to flash green whether or not anything played —
            # the flash is the deck's word that sound is coming, so it has
            # to be conditional on the Audio Pi actually accepting it.
            if self.lineup.play():
                self.flash(BTN_PLAY)
            else:
                self._press_failed(BTN_PLAY)
        elif idx == BTN_STOP:
            if self.music.stop():
                self.flash(BTN_STOP)
            else:
                self._press_failed(BTN_STOP)
        elif idx == BTN_FADE:
            if self.music.fade():
                self.flash(BTN_FADE)
            else:
                self._press_failed(BTN_FADE)

    # ── Content handler ──────────────────────────────────

    def on_content_key(self, btn_idx) -> None:
        page = self.config.pages.get(self.current_page_id, {})
        if self.current_page_id == "__status":
            self._press_status_key(btn_idx)
            return
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
            elif slot == len(order):
                self._reboot_armed = 0.0
                self.current_page_id = "__status"
                self.render_all()

        elif kind == "lineup":
            # Cue (queue) this batter; the coach presses Play to run it.
            filled = [i for i, pid in enumerate(self.config.lineup) if pid]
            if slot < len(filled):
                self.lineup.set_current(filled[slot])
                if self.lineup.cue_current():
                    self.flash(btn_idx)
                else:
                    self._press_failed(btn_idx)

        elif kind == "players":
            players = self.config.players_by_jersey()
            if slot < len(players):
                pid, _ = players[slot]
                # Edit-lineup mode: fill the armed batting spot instead of playing.
                if self._edit_lineup and self._lineup_assign_pos is not None:
                    self._assign_player_to_lineup(pid, btn_idx)
                    return
                self.lineup.note_external_playback()
                # CUE-FIRST, always: a song press loads the clip; the
                # green Play key runs it. Nothing blares mid-inning
                # because a thumb brushed a tile.
                if self.music.cue_walkup(pid):
                    self.flash(btn_idx)
                else:
                    self._press_failed(btn_idx)

        elif kind == "celebrations":
            if slot < len(CELEBRATIONS):
                key, _ = CELEBRATIONS[slot]
                self.lineup.note_external_playback()
                if self.music.cue_celebration(key):
                    self.flash(btn_idx)
                else:
                    self._press_failed(btn_idx)

        else:
            # Song-list pages: hype / mid_inning / mound_visit / dead_ball /
            # pitcher_warmup. One button per assigned song, played immediately.
            songs = self.config.get_songs_for_page(self.current_page_id)
            if slot < len(songs):
                sid, _ = songs[slot]
                self.lineup.note_external_playback()
                if self.music.cue_song(sid):
                    self.flash(btn_idx)
                else:
                    self._press_failed(btn_idx)

    # ── Rendering ────────────────────────────────────────

    def render_content(self) -> None:
        page = self.config.pages.get(self.current_page_id, {})
        if page.get("slots"):
            self._render_slot_page()
            return
        kind = page.get("kind", self.current_page_id)
        if self.current_page_id == "__status":
            self._render_status_page()
            return
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
            elif i == len(order):
                # the deck's own health: CPU/temp/IP/uptime + Sync,
                # Update (git pull) and Reboot, right on the hardware
                self.btn(btn_idx, "Status", (40, 44, 60), (160, 200, 255))
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
        if kind == "action":
            if ref == "sync":
                return "⟳\n" + self._sync_label()
            return {"play": "Play", "stop": "Stop", "fade": "Fade"}.get(ref, ref)
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
    def _wrap_label(text: str, width: int = 8, max_lines: int = 3) -> str:
        """Word-wrap a key label the way the web editor previews it.

        The deck used to hard-truncate at 16 characters on one or two
        lines, so 'Gunnar Gulbrandsen' became 'Gunnar Gulbrand' on the
        key while the portal showed the full name — the two never looked
        the same and long words just fell off the edge. Words wrap whole
        when they fit; a word longer than a line is split rather than
        dropped; anything past the last line ends in an ellipsis so the
        cut is at least visible.
        """
        text = str(text or '')
        if '\n' in text:                 # the editor's own line breaks win
            return '\n'.join(ln[:width + 2] for ln in
                             text.split('\n')[:max_lines])
        words, lines, cur, dropped = text.split(), [], '', False
        for w in words:
            if not cur and len(w) > width:
                while len(w) > width and len(lines) < max_lines - 1:
                    lines.append(w[:width])
                    w = w[width:]
                cur = w
            elif not cur:
                cur = w
            elif len(cur) + 1 + len(w) <= width:
                cur += ' ' + w
            else:
                lines.append(cur)
                cur = w
            if len(lines) >= max_lines:
                dropped = True     # cur (and any later words) never render
                break
        if cur and len(lines) < max_lines:
            lines.append(cur)
            cur = ''
        if cur:
            dropped = True
        if dropped and lines:
            lines[-1] = (lines[-1][:width - 1] + '…')
        return '\n'.join(lines[:max_lines])

    @staticmethod
    def _fit_font(label: str, base: int) -> int:
        """Shrink the font just enough for the wrapped label's longest
        line — so the key and the web preview read the same instead of the
        deck clipping what the portal displays."""
        longest = max((len(ln) for ln in label.split('\n')), default=0)
        if longest <= 8:
            return base
        if longest <= 10:
            return max(10, int(base * 0.85))
        return max(9, int(base * 0.7))

    def _press_failed(self, btn_idx: int) -> None:
        """A press that made no sound says WHY on the key it was pressed.

        Silence used to come with nothing at all — no flash, no message —
        and 'I pressed a song, then play, and nothing played' was a bug
        report only because a coach took the time to file it. The reason
        (missing file, unreachable Audio Pi) paints the key red for a
        moment, then the page repaints itself.
        """
        # No default guess: blaming the Audio Pi for a press that never
        # reached it sent a coach chasing the wrong box.
        why = (getattr(self.music, 'last_error', '') or
               "didn't play — no reason logged")
        if '\n' in why:
            # Pre-formatted reasons (the unreachable one carries the IP on
            # its own line) pass through unclamped — _wrap_label would cut
            # a long address, and the address IS the diagnosis.
            label = '\n'.join(('✗ ' + why).split('\n')[:4])
        else:
            # Wider and smaller than a normal key: the reason is a
            # sentence, and a cut-off sentence reads as a new mystery.
            label = self._wrap_label('✗ ' + why, width=14, max_lines=4)
        try:
            self.btn(btn_idx, label, (150, 24, 24), (255, 255, 255),
                     font_size=10)
        except Exception:
            log.exception('could not paint the failure key')
        threading.Timer(4.0, self.refresh).start()

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
            elif kind == "action" and slot.get("ref") == "sync":
                wbg, wfg = self._sync_key_colors()
                if wbg:
                    bg, fg = wbg, wfg
                # The Sync key IS the deck's status surface. A hand-typed
                # label used to replace the narration entirely, so a
                # failing sync looked exactly like a working one.
                if slot.get("label"):
                    label = slot["label"] + "\n" + self._sync_label()
            wrapped = self._wrap_label(label)
            self.btn(btn_idx, wrapped, bg, fg, font=font,
                     font_size=self._fit_font(wrapped, size))

    def _handle_slot_press(self, btn_idx: int) -> None:
        slots = self.config.pages.get(self.current_page_id, {}).get("slots", {})
        slot = slots.get(str(btn_idx))
        if not slot:
            return
        kind, ref = slot.get("type"), slot.get("ref", "")
        # EVERY music slot cues — the Play key is the one thing that makes
        # sound (the Lineup flow always worked this way; now nothing else
        # can fire mid-inning by accident). The old per-slot "immediate"
        # mode is ignored on music keys.
        queue_only = True
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
            ok = self.music.queue(self.config.build_song_clip(ref) or {})
        elif kind == "celebration":
            self.lineup.note_external_playback()
            ok = self.music.cue_celebration(ref)
        elif kind == "nav":
            self.go_to_page(ref)
            return
        elif kind == "action":
            if ref == "play":
                ok = self.lineup.play()
            elif ref == "stop":
                ok = self.music.stop()
            elif ref == "fade":
                ok = self.music.fade(int(slot.get("fade_ms") or 1000))
            elif ref == "sync":
                ok = self._start_sync()
        if ok:
            self.flash(btn_idx)
        elif kind in ("player_walkup", "song", "celebration") or (
                kind == "action" and ref in ("play", "stop", "fade")):
            # sync is excluded: the Sync key narrates its own progress
            self._press_failed(btn_idx)

    # ── Sync-now key ─────────────────────────────────────
    # The timer polls every 5 minutes. When a coach changes a walk-up song
    # or makes a substitution during a game, the person on audio wants it
    # now — without leaving the deck to find a browser.

    def _sync_label(self) -> str:
        try:
            import sync_now
        except Exception:
            return "Sync"
        s = sync_now.status()
        if s["running"] or self._audio_sync.get('running'):
            return "..."
        watch = self._lineup_state()
        if watch == "changed":
            return "LINEUP"          # the one thing worth interrupting for
        # The audio half: a sync that "worked" on the deck but left the
        # AUDIO Pi unreached or short of files is exactly the silent-lineup
        # failure — say so on the key instead of showing a happy timestamp.
        if self._audio_sync.get('ok') is False:
            return "aud ✗"
        miss = self._audio_sync.get('missing')
        if miss:
            return f"{miss} miss"
        if s["ok"] is True:
            ts = time.strftime("%H:%M", time.localtime(s["at"]))
            # An unreachable lineup WATCH must not hide a working SYNC —
            # 'offline' over a deck that was pulling changes fine sent the
            # coach chasing the network. The ⚠ says the watch is blind;
            # the timestamp says the sync itself worked.
            return ts + "⚠" if watch == "unknown" else ts
        if s["ok"] is False:
            return "failed"
        return "offline" if watch == "unknown" else "Sync"

    def _lineup_state(self) -> str:
        """'fresh' | 'changed' | 'unknown'. Anything unexpected reads as
        unknown — claiming to be up to date is the one wrong answer."""
        if not self._lineup_watch:
            return "unknown"
        try:
            return self._lineup_watch.status().get("state") or "unknown"
        except Exception:
            return "unknown"

    def _sync_key_colors(self):
        """(bg, fg) for the Sync key.

        Amber shouts because a substitution is the only state that needs the
        operator to do something. 'unknown' is deliberately drawn dim rather
        than left looking like 'fresh' — a dark key that means "I can't
        reach the cloud" must not read as "no substitutions have happened".
        """
        state = self._lineup_state()
        if state == "changed":
            return self.ACTIVE_COLOR, (0, 0, 0)
        if state == "unknown":
            return (28, 28, 32), (120, 120, 130)
        # A failed sync (either box) paints the key red — the label says
        # which ("failed" = this Pi, "aud ✗" = the Audio Pi's half).
        # getattr: this can run from a paint before __init__ finishes.
        if getattr(self, '_audio_sync', {}).get('ok') is False:
            return (150, 24, 24), (255, 255, 255)
        try:
            import sync_now
            if sync_now.status()["ok"] is False:
                return (150, 24, 24), (255, 255, 255)
        except Exception:
            pass
        return None, None            # fresh — the slot's own colours

    # ── the deck's own status page (built-in, id "__status") ────────────
    _ST_KEYS = ("sync", "update", "reboot", "back")

    @staticmethod
    def _read_file(path, default=""):
        try:
            with open(path) as fh:
                return fh.read().strip()
        except OSError:
            return default

    def _status_lines(self):
        """CPU load, temperature, IP and uptime — same numbers the Audio
        Pi's web status page shows, painted on keys."""
        load = self._read_file("/proc/loadavg", "—").split(" ")[0]
        temp = self._read_file("/sys/class/thermal/thermal_zone0/temp")
        try:
            temp = f"{int(temp) / 1000:.0f}C"
        except (TypeError, ValueError):
            temp = "—"
        up = self._read_file("/proc/uptime", "0").split(" ")[0]
        try:
            mins = int(float(up) // 60)
            up = f"{mins // 1440}d{mins % 1440 // 60}h{mins % 60}m"
        except ValueError:
            up = "—"
        try:
            import socket
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.connect(("8.8.8.8", 80))
            ip = sk.getsockname()[0]
            sk.close()
        except OSError:
            ip = "no net"
        return [("CPU", load), ("TEMP", temp), ("IP", ip), ("UP", up)]

    def _render_status_page(self) -> None:
        rows = self._status_lines()
        for i, btn_idx in enumerate(self.content_slots):
            if i < len(rows):
                k, v = rows[i]
                self.btn(btn_idx, f"{k}\n{v}"[:22], (24, 30, 40),
                         (170, 200, 230))
            elif i - len(rows) == 0:
                self.btn(btn_idx, "Sync", (18, 60, 34), (140, 230, 170))
            elif i - len(rows) == 1:
                self.btn(btn_idx, "Update\n(code)", (18, 40, 70),
                         (150, 200, 255))
            elif i - len(rows) == 2:
                armed = time.monotonic() - getattr(
                    self, "_reboot_armed", 0.0) < 3.0
                self.btn(btn_idx,
                         "SURE?\nReboot" if armed else "Reboot",
                         (90, 20, 20) if armed else (50, 22, 22),
                         (255, 170, 170))
            elif i - len(rows) == 3:
                self.btn(btn_idx, "< Home", (40, 40, 40), (200, 200, 200))
            else:
                self.blank(btn_idx)

    def _press_status_key(self, btn_idx) -> None:
        rows = 4
        slot = self.content_slots.index(btn_idx)
        act = slot - rows
        if slot < rows:
            self.render_all()          # tapping a stat refreshes them all
            return
        if act == 0:                   # Sync (config + audio, both boxes)
            if self._start_sync():
                self.flash(btn_idx)
            else:
                self._press_failed(btn_idx)
        elif act == 1:                 # Update: git pull + restart services
            self.btn(btn_idx, "pulling\u2026", (18, 40, 70),
                     (150, 200, 255))
            threading.Thread(target=self._run_code_update,
                             args=(btn_idx,), daemon=True).start()
        elif act == 2:                 # Reboot: double-press within 3 s
            if time.monotonic() - getattr(self, "_reboot_armed", 0.0) < 3.0:
                subprocess.run(["sudo", "reboot"], capture_output=True)
            else:
                self._reboot_armed = time.monotonic()
                self.render_all()
        elif act == 3:
            self.current_page_id = "home"
            self.render_all()

    def _run_code_update(self, btn_idx) -> None:
        """git pull --ff-only on this checkout, then restart the OnDeck
        services — the same thing the Audio Pi's web Update button does.
        Restarting our own service is the point: systemd brings the deck
        back up running the new code."""
        import pathlib
        repo = pathlib.Path(__file__).resolve().parent
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=120)
            ok = out.returncode == 0
        except Exception:
            ok = False
        if not ok:
            self._press_failed(btn_idx)
            return
        self.flash(btn_idx)
        for unit in ("ondeck-audio", "ondeck-coach"):
            chk = subprocess.run(["systemctl", "is-enabled", unit],
                                 capture_output=True, text=True)
            if (chk.stdout or "").strip() in ("enabled", "static"):
                subprocess.run(["sudo", "systemctl", "restart", unit],
                               capture_output=True, timeout=60)

    def _start_sync(self) -> bool:
        """One press, BOTH boxes: sync this Pi (config/lineup) and tell
        the Audio Pi to sync itself (the music files it plays from). The
        deck syncing alone is how 'I pressed sync and nothing plays'
        happens — the labels update while the speaker's disk stays empty."""
        try:
            import sync_now
        except Exception as exc:
            log.warning("sync-now unavailable: %s", exc)
            return False
        sync_now.start()     # False = already running; watcher still applies
        self._audio_sync = {'running': True, 'ok': None, 'missing': None}
        threading.Thread(target=self._watch_sync, args=(sync_now,),
                         daemon=True).start()
        return True          # a second press mid-sync still counts as handled

    def _watch_sync(self, sync_now) -> None:
        """Drive the audio half, tick the key while either box works, then
        repaint — before_render reloads config.json, so the new lineup and
        songs appear on that paint."""
        started = False
        try:
            started = self.music.sync_audio_start()
        except Exception:
            log.exception("audio sync trigger failed")
        deadline = time.monotonic() + 300     # a first sync downloads a lot
        audio_ok = None
        while True:
            local_busy = sync_now.status()["running"]
            audio_busy = False
            if started and time.monotonic() < deadline:
                st = None
                try:
                    st = self.music.sync_audio_status()
                except Exception:
                    pass
                if st is None:
                    audio_ok = False          # went unreachable mid-sync
                elif st.get("running"):
                    audio_busy = True
                else:
                    audio_ok = st.get("ok")
            if not local_busy and not audio_busy:
                break
            self.refresh()
            time.sleep(1)
        if not started:
            audio_ok = False                  # never reached the Audio Pi
        missing = None
        if audio_ok:
            try:
                missing = self.music.library_missing()
            except Exception:
                pass
        self._audio_sync = {'running': False, 'ok': audio_ok,
                            'missing': missing}
        self.refresh()

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
