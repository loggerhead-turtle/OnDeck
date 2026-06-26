# OnDeck — Stream Deck Editor, Song Clips, Categories & Filtering (SPEC)

Status: **approved spec, not yet implemented.** Written in a session scoped to `ondeck` only.
Implementation happens in a session that also has the **`play-call`** repo (see
`CONTEXT_BRIDGE.md`).

## Why

OnDeck has **no Stream Deck runtime** today — `config["pages"]`/`slots` exist in
`config_manager.py` but are never read or written, `main.py`/`streamdeck_controller.py` don't
exist, and nothing imports the `streamdeck` library. Songs are a single trim window per file
(`filename, display_name, start_ms, end_ms`); there are no categories, no per-use segments,
no artist/album metadata, and no teams.

The coach needs:
1. A web **button editor** for the physical deck, incl. bulk-adding players in **numeric** and
   **alphabetical** order.
2. A **music vs sound-effects** model where one uploaded file is **immutable storage** that can
   be cut into many **clips**, each with its own in/out and **situation** tags (walkup,
   mid-inning, foul ball, DC them, DC us, coach umpire, …). Example: "Hotel California" →
   a walkup clip, a short SFX clip, and a long mid-inning clip — each its own in/out.
3. **Filtering** of a large library (hundreds–thousands of songs) by situation + artist /
   album / title (+ team later).
4. A per-clip **push-to-deck** toggle that auto-builds paginated music/filter pages on the deck.

Deck hardware: **Stream Deck XL (32 keys, 8×4)**.

## Decisions (from interview)

| Topic | Decision |
|---|---|
| Clip intro/outro | **In/out points only** (reuse `_trim_editor` start/end; no fades) |
| Situations | **Multi-tag, editable list**; situations **become the deck filter pages** (replace fixed built-in page kinds for music) |
| Push-to-deck | **Per clip**; a pushed clip appears on the deck filter page(s) of its situation tag(s), auto-paginated |
| Metadata (artist/album/title) | **Auto (ID3 / YouTube) + manual override** |
| Deck pages | **Hand-edited structural pages + auto-generated music/filter pages** |
| Player walk-up | Becomes a **`walkup`-tagged clip** (migrate existing `walkup_song_id`) |
| Editor button types | **player_walkup, clip, nav, action** |
| Teams | **Deferred (Phase C)**; model stays forward-compatible (`team_ids: []` reserved now) |

## Data model (`config_manager.py`)

**Song = immutable storage + metadata.** `config["songs"][sid]`:
```
filename, display_name (title), artist, album, duration_ms, source ("upload"|"youtube"), added_at
```
Keep legacy `start_ms`/`end_ms` only to drive the one-time migration, then leave unused.

**Clip = a usage of a song.** New top-level `config["clips"][cid]`:
```
song_id, name, category ("music"|"sfx"), situations: [situation_id, …],
start_ms, end_ms,                 # in/out points ("intro/outro")
on_deck: bool,                    # push-to-deck (per clip)
deck_label, deck_color,           # optional render hints
team_ids: [],                     # reserved, empty for now
created_at
```

**Situations = editable ordered list.** `config["situations"]`: `[{id, name, order}]`. Seed
with: `walkup, mid_inning, foul_ball, dc_them, dc_us, coach_umpire, hype, celebrations`.
Drives the web situation filter **and** the deck filter pages.

**Player.** Replace `walkup_song_id` → `walkup_clip_id`; keep `announcement_file`,
`announcement_start_ms`, `announcement_end_ms`, `music_cue_ms`; add reserved `team_ids: []`.

**Pages/slots (structural, hand-edited).** Keep `config["pages"]`. Each page has 32 slots
(8×4 XL) keyed by index `0..31`: `{type, ref, label, color}` where
`type ∈ {player_walkup, clip, nav, action, blank}` and `ref` = pid / cid / page_id / action
name (`stop|fade|volume`). **Auto** music/filter pages are NOT stored here — the deck runtime
generates them from `situations` + `on_deck` clips.

**ConfigManager helpers to add:** `add_clip`, `clips_for_song`, `clips_by_situation`,
`clips_on_deck`, situations accessors, and migration in `_ensure_shape()`:
- backfill `clips: {}`, seed `situations`;
- for each player with legacy `walkup_song_id`, create a `walkup` clip from the referenced
  song (carry its `start_ms`/`end_ms`) and set `walkup_clip_id`.

## Web (Phase A — no play-call needed) — `web/app.py` + `web/templates/`

- **Button editor** (new nav "Stream Deck"): `GET /deck` (page list + 8×4 grid editor);
  `POST /deck/<page_id>/slot` (assign one slot: type+ref+label+color);
  `POST /deck/<page_id>/fill-players?order=jersey|alpha` (bulk-fill player_walkup buttons);
  page CRUD (add/rename/reorder/delete custom pages). New template `deck_editor.html`.
- **Library overhaul** (`library.html`, `library_edit.html`): song row shows metadata; the
  song edit page lists its **clips** + an "Add clip" flow. Clip editor reuses the
  `_trim_editor` macro for in/out, plus situation multi-select, category toggle, and the
  **push-to-deck** radio. New routes: `POST /clips`, `/clips/<cid>/save`, `/clips/<cid>/delete`,
  `/clips/<cid>/toggle-deck`.
- **Library filters** on `GET /library` (server-side query params): situation, category,
  artist, album, title, on-deck (team later).
- **Situations management**: `GET/POST /situations` (add/rename/reorder/delete); small
  `situations.html`.
- **Metadata extraction**: on upload/import prefill artist/album/title from ID3 (`mutagen`)
  or yt-dlp info; user can override. Add `mutagen` to `requirements.txt`.
- Update `players.html` / `player_edit.html` for `walkup_clip_id`; update `base.html` nav
  (add "Stream Deck", optionally "Situations").

**Reuse:** `_trim_editor.html` macro (in/out editor), `players_by_jersey()` (numeric fill; add
an alpha sort), `music_server.py` clip schema (`file,start_ms,end_ms,announcement,cue_ms`) as
the press→play payload, existing `/api/preview` for in-browser preview.

## Stream Deck runtime (Phase B — REQUIRES play-call)

New Coach-Pi `main.py` / `streamdeck_controller.py` that **mirrors play-call's structure**:
init the XL, render structural pages from `config["pages"]`, render **auto** music/filter
pages from `situations` + `on_deck` clips (with pagination + nav keys), handle key presses →
POST the clip to the Audio Pi `music_server.py` `/queue`. Wire the systemd service in
`install.sh`. See `CONTEXT_BRIDGE.md` for the exact play-call mirroring steps.

Everything reaches the Pi via the **existing `/sync/config`** (clips, situations, pages, and
`on_deck` all live in `config.json`) and `/sync/files`. No new sync endpoints needed. (Optional
later: only sync audio for `on_deck` clips.)

## Phasing

- **Phase A (no play-call):** data model + migration; library/clips/situations UI + filters +
  push-to-deck; button editor + add-players (numeric/alpha); metadata extraction.
- **Phase B (needs play-call):** deck runtime mirroring play-call.
- **Phase C (deferred):** Teams (player & clip `team_ids`, team filter, team management UI);
  sync-only-pushed-files optimization.

## Verification

- **Phase A:** run `python web/app.py` against a seeded `config.json`; upload a song, make
  2–3 clips with different situations/in-out, toggle push-to-deck; confirm `config["clips"]`,
  `situations`, per-clip `on_deck` shape; build a structural page; use "fill players" in both
  jersey and alpha order; confirm `GET /sync/config` returns the new fields; confirm legacy
  `walkup_song_id` players migrate to a `walkup` clip. Drive key flows with the bundled
  Chromium where useful.
- **Phase B:** with play-call available and an XL (or play-call's simulator), confirm pages
  render, paging works, and a key press queues the right clip on the Audio Pi.

## Critical files

- `config_manager.py` — songs metadata, `clips`, `situations`, slot schema, `walkup_clip_id`,
  `_ensure_shape()` migration, new helpers.
- `web/app.py` — deck/pages routes, clip routes, situations routes, library filters,
  fill-players, metadata extraction.
- `web/templates/` — new `deck_editor.html`, `situations.html`; overhaul `library.html`,
  `library_edit.html`; update `players.html`, `player_edit.html`, `base.html`.
- `requirements.txt` — add `mutagen`.
- **Phase B (play-call-based):** new `main.py` / `streamdeck_controller.py`, `install.sh`.
