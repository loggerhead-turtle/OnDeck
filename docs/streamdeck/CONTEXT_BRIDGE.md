# Context Bridge — Stream Deck build (read me first)

You are (probably) a fresh session that now has **both** `loggerhead-turtle/ondeck` and
`loggerhead-turtle/play-call`. This doc hands off everything needed to build the Stream Deck
feature. Read `docs/streamdeck/SPEC.md` (the approved spec) alongside this.

## TL;DR of what to do

1. Read `SPEC.md` fully.
2. **Phase A** (OnDeck-only, no play-call): data model + migration, then the web UI (library
   clips/situations/filters/push-to-deck, button editor, add-players). Start here — it unblocks
   everything and needs no play-call.
3. **Phase B** (needs play-call): build the Coach-Pi deck runtime by **mirroring play-call's**
   Stream Deck code. See "Phase B: how to mirror play-call" below.
4. Confirm the working branch with the user before pushing (this spec landed on
   `claude/streamdeck-spec`; the live/deployed branch has been `claude/ondeck-web-portal-trim-hvgzup`).

## Interview decisions (authoritative)

- One uploaded file = **immutable storage**; cut into many **clips**, each with its own
  **in/out points only** (no fades) via the existing `_trim_editor`.
- **Situations** are a **multi-tag, editable list**; they also serve as the **deck filter
  pages**, replacing the fixed built-in page kinds for music. Seed list: `walkup, mid_inning,
  foul_ball, dc_them, dc_us, coach_umpire, hype, celebrations`.
- **Push-to-deck is per clip**; a pushed clip shows up on the deck filter page(s) for its
  situation tag(s), auto-paginated.
- Metadata (artist/album/title): **auto from ID3 / YouTube + manual override**.
- Deck pages: **hand-edited structural pages + auto-generated music/filter pages**.
- Player walk-up becomes a **`walkup` clip** (migrate `walkup_song_id` → `walkup_clip_id`).
- Editor button types: **player_walkup, clip, nav, action**.
- Deck is **Stream Deck XL (32 keys, 8×4)**.
- **Teams deferred** (Phase C); keep `team_ids: []` on player + clip now so it's additive later.

## OnDeck file map (verified this session — line refs approximate)

- `config_manager.py`
  - `_default_pages()` (~L60–82): 9 built-in page kinds, each `{name, kind, order, deletable,
    slots:{}}`. **`slots` is never read/written today.**
  - `_default_config()` (~L28–57): top-level keys `system, players, songs, lineup,
    lineup_size, celebrations, mid_inning, pages`.
  - `add_player()` (~L166–180): player dict = `jersey, first_name, last_name,
    announcement_file, announcement_start_ms, announcement_end_ms, walkup_song_id,
    music_cue_ms`. (Login fields `player_username, player_password_hash, force_reset` are added
    on demand by `/players/<pid>/set-credentials`.)
  - `add_song()` (~L182–192): song dict = `filename, display_name, start_ms, end_ms`.
  - `players_by_jersey()` (~L194–199): sorted `[(pid, player), …]` — reuse for numeric fill;
    add an alphabetical variant for the alpha fill.
  - `save(mark_dirty=True)`, `_ensure_shape()` (upgrade/backfill) — put the migration here.
  - **No team concept anywhere** (confirmed by repo-wide search).
- `web/app.py` — routes for auth, `/` index, `/audio/<f>`, `/library*`, `/players*`,
  `/my-profile*`, `/settings*`, `/api/*` (proxy to Audio Pi), `/sync/*`. Helpers `_form_ms`,
  `_form_ms_or_none` (~L843). Library save at `/library/<sid>/save`; player save at
  `/players/<pid>/save` (~L443, persists `music_cue_ms`, ann/song trims).
- `web/templates/` — `base.html` (nav + `OnDeck` JS playback/demo/debug engine),
  `index.html` (quick-play), `library.html`, `library_edit.html`, `players.html`,
  `player_edit.html`, `my_profile.html`, `settings.html`, and the **`_trim_editor.html`**
  macro (WaveSurfer v7 in/out editor with optional cue marker) — reuse it for the clip editor.
- `music_server.py` (Audio Pi) — `POST /queue` accepts a clip dict:
  `{file, start_ms, end_ms, announcement, cue_ms}`. Also `/play /stop /fade /volume /status`.
  This is the press→play payload the deck runtime sends.
- `sync_agent.py` + `/sync/*` — Pi pulls `GET /sync/config` (full `config.json`),
  `GET /sync/files` (`{filename,size,md5}` manifest), `GET /sync/files/<name>`. **Clips,
  situations, pages, and `on_deck` all live in `config.json`, so they sync for free.** Sync is
  currently pull-only.

## Data model to implement (see SPEC for full field lists)

- `config["songs"][sid]` += `artist, album, duration_ms, source, added_at` (immutable storage).
- New `config["clips"][cid]` = `song_id, name, category, situations[], start_ms, end_ms,
  on_deck, deck_label, deck_color, team_ids[], created_at`.
- New `config["situations"]` = `[{id, name, order}]` (seeded).
- Player: `walkup_song_id` → `walkup_clip_id`; add `team_ids: []`.
- `config["pages"][kind].slots[i]` = `{type, ref, label, color}`,
  `type ∈ {player_walkup, clip, nav, action, blank}`, i in `0..31`.
- Migration in `_ensure_shape()`: backfill `clips:{}`, seed `situations`, convert each
  player's legacy `walkup_song_id` into a `walkup` clip and set `walkup_clip_id`.

## Phase A: suggested build order

1. `config_manager.py`: schema + helpers + `_ensure_shape()` migration (write a tiny script to
   round-trip a sample `config.json` and confirm migration is idempotent).
2. `requirements.txt`: add `mutagen`; metadata extraction on upload/import in `web/app.py`.
3. Library: clip CRUD routes + `library_edit.html` clip list + clip editor (reuse
   `_trim_editor`, situation multi-select, category, push-to-deck radio); `library.html`
   filters.
4. Situations management page.
5. Button editor (`/deck`, `deck_editor.html`, slot assignment, fill-players numeric/alpha,
   page CRUD); nav entry in `base.html`.
6. Update player editor/flows to `walkup_clip_id`.
7. Verify per SPEC (local Flask + seeded config + Chromium for key flows).

## Phase B: how to mirror play-call (do AFTER Phase A, needs play-call repo)

1. In `play-call`, locate the Stream Deck modules: the device init/open, the **page/grid
   render** (key image generation with Pillow), the **render loop**, **key-press handler**, and
   any **page navigation / pagination** logic. (Search for `StreamDeck`, `set_key_image`,
   `deck.open(`, `KEY_`, image/render helpers.)
2. Mirror that structure into a new OnDeck Coach-Pi `main.py` / `streamdeck_controller.py`:
   - Read `config.json` (same shape the web writes).
   - Render **structural** pages from `config["pages"][kind].slots` (player_walkup / clip / nav
     / action / blank).
   - Render **auto** music/filter pages: a "Filters" page listing `situations`; selecting a
     situation shows its `on_deck` clips, **paginated** across XL pages with prev/next nav keys.
   - On key press: resolve the slot/clip → POST `{file,start_ms,end_ms,announcement,cue_ms}` to
     the Audio Pi `music_server.py /queue` (then `/play` if needed — match existing behavior).
   - Keep OnDeck conventions (`Path.home()`, `ONDECK_HOME`, no hardcoded Pi username).
3. Wire the systemd service in `install.sh` (the README references a `streamdeck_controller.py`
   / coach service that does not exist yet).
4. Match play-call's button look/feel (fonts, colors, image sizes) so the decks are consistent.

## Gotchas learned this session (still relevant)

- **Never nest `<form>` tags.** The player/song editors had nested forms that silently dropped
  fields on submit; fixed via the HTML5 `form=` attribute (see `player_edit.html`,
  `library_edit.html`). Any new editor forms must avoid nesting.
- **Cloud mode has no Audio Pi.** `web/templates/base.html` `OnDeck` JS plays previews
  in-browser when `window.ONDECK_CLOUD` (cloud) or the Demo toggle (local). Deck press→play is
  Pi-only and is Phase B.
- **Recorded `.webm` announcements often report no/invalid duration**; clamp against a safe
  fallback (see `_maxTime()` in `_trim_editor.html`) rather than WaveSurfer's `getDuration()`.
- All times are **milliseconds (ints)** in config; config saves are atomic (tempfile +
  `os.replace`) under an `RLock`.

## Status at handoff

- Spec approved; this bridge + `SPEC.md` committed on `claude/streamdeck-spec`.
- No feature code written yet. Phase A is fully specified and unblocked.
- Phase B is blocked only on play-call access (now available in your session).
