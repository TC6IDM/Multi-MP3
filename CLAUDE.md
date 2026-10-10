# CLAUDE.md — Multi-MP3

Keeps Spotify, YouTube and SoundCloud playlists downloaded as clean, tagged MP3s
under `downloads/`. The owner moves the files to their iPhone with CopyTrans and
plays them in Apple Music, so names and tags matter: `(0001) Song.mp3`, title =
the song's name only, sort title (TSOT) = `(0001) Song` so playlists sort in
order, and even volume (see "Apple Music output"). Two front ends share one
engine: a CLI (`main.py`) and a FastAPI web UI (`web_app.py`).

The owner runs it on Windows 11, natively (`.venv`) and through Docker
(`run.ps1`, `docker compose`). Their playlists are in `links.txt`, which they
edit constantly — never rewrite or reformat it except through the Links tab's
own save path (`src/links.py`, which preserves comments and blank lines).

## Working agreements

- **Never `git commit` or `git push` without asking.** Summarise the change, say
  what the commit would contain, and wait for a yes. Approval covers that one
  action only.
- Never start a long, full-library download run on your own initiative — it
  spends the owner's real SoundCloud/YouTube budget. Small targeted test runs
  are fine when verifying a change; say what you are about to run.
- Comments explain *why*, usually with the incident that forced the code
  ("a 460-track playlist died at ~102"). Keep that style; don't strip the
  history out of existing comments when editing near them.
- Match the surrounding code: type hints, `pathlib`, f-strings, emoji-prefixed
  log lines (`✅ ⚠️ 💥 🔁 ⏳`) in *logs* (never in UI chrome — see Design system).

## Running it

```powershell
# Native (Windows)
uv sync --extra web --extra dev                 # app env → .venv (pinned by uv.lock)
uv venv .venv-spotdl
uv pip install --python .venv-spotdl -r requirements-spotdl.txt
.venv\Scripts\uvicorn web_app:app --host 0.0.0.0 --port 8080
.venv\Scripts\python main.py links.txt downloads --speed balanced

# Docker (Python 3.13 + uv)
docker compose up -d --build web
.\run.ps1 -InputFile links.txt
```

Requires `ffmpeg` on PATH. Deno (yt-dlp's JS runtime for YouTube) is the
`deno` pip wheel — `utils.deno_path()` finds it, `utils.subprocess_env()` puts
it on PATH for child tools.

**spotdl lives in its own virtualenv** (`.venv-spotdl/`, `/opt/spotdl` in
Docker): every spotdl release pins `fastapi<0.104`, `uvicorn<0.24`, `rich<14`.
The app never imports spotdl; it runs `src/downloaders/spotdl_runner.py` with
spotdl's Python (`utils.spotdl_python()`), falling back to the plain `spotdl`
executable (`SPOTDL_BIN`) without the handshake.

## Verification

```powershell
.venv\Scripts\python -m pytest -q                 # tests/ — ~10s, offline
.venv\Scripts\python -m ruff check src/ main.py web_app.py tests/
.venv\Scripts\python -m mypy src/ main.py web_app.py --ignore-missing-imports
.claude\skills\impeccable\scripts\impeccable.cmd detect --json templates/index.html templates/playlist.html static/style.css
```

CI runs the same on Python 3.13 via `uv sync --frozen`. The loudness tests
encode real MP3s with ffmpeg and skip where it's missing (CI, currently). UI changes: check in a
browser at 1280px and 390px, dark and light (`prefers-color-scheme`), and the
detector must stay at 0 findings. Network-touching checks (a real resolve or a
small download) are manual, never in the test suite.

## Architecture

```
main.py / web_app.py
  └─ Coordinator (src/coordinator.py)       one lane per provider, all concurrent
       ├─ Spotify    → SpotifyDownloader → spotdl_runner.py (spotdl's venv)
       │                 handshake: spotdl prints the song list, we answer with
       │                 URLs to skip, it downloads only the rest
       ├─ YouTube    ─┐
       └─ SoundCloud ─┴─ YtDlpEngine (in-process yt-dlp, worker pool)
                              └─ AdaptiveRateLimiter (one per provider)
       DoneIndex (src/dedupe.py): a track in several playlists is fetched once,
       then copied (and retagged) into the others
       tidy_playlist (src/tidy.py): each playlist's files renamed to "(0001) Song",
       leavers moved to _removed/ — on resolve, and for Spotify after reconcile
       Finisher (src/tidy.py): background pool writing tags + levelling volume
       (src/loudness.py); the run waits for it at the end
       health (src/health.py): per-provider failure rate → "update yt-dlp" advice
  progress → WebProgress → EventBus (replayable, seq-numbered) → SSE → index.html
  records  → StateStore (src/state.py)      downloads/.state/<provider>/*.json
  jobs     → JobManager: one running, the rest queued; history in .web/jobs.json
```

| File | Role |
|---|---|
| `src/coordinator.py` | Lanes, Spotify flow (handshake → record → tidy → copy duplicates → set aside → spotdl → recovery → reconcile → restore → tidy → finish), cancel, the Finisher, health advice. |
| `src/state.py` | Per-playlist records — the source of truth for "done / still to do". Reset lives here. |
| `src/ratelimit.py` | Adaptive token bucket shared by all workers of a provider; AIMD + global cooldown. |
| `src/dedupe.py` | Done-track index across all records; copy + retag into another playlist. |
| `src/tidy.py` | Clean titles, `(0001) Song.mp3` names, tags (`plan_for`), folder tidying + `_removed/`, background `Finisher`. |
| `src/loudness.py` | Lossless levelling: ffmpeg ebur128 measure, MP3 global_gain shift (mp3gain's method). |
| `src/health.py` | Per-provider failure history in `.cache/health.json` → "update yt-dlp" advice. |
| `src/links.py` | links.txt ⇄ entries (label, on/off, markdown) preserving raw lines. |
| `src/event_bus.py` | Replayable log: every event has a seq; readers keep cursors. |
| `src/job_manager.py` | Singleton: current job, FIFO queue, cancel event, history. |
| `src/notify.py` | ntfy / Discord ping on run end (`NOTIFY_URL`). |
| `src/maintenance.py` | Versions, one-click yt-dlp update (pip, or uv in uv-built envs), cleanup. |
| `src/downloaders/ytdlp_engine.py` | yt-dlp as a library: resolve → reconcile → queue → workers → staging → promote. |
| `src/downloaders/spotify.py` | spotdl command, track list from songs, file matching, set-aside/restore, reconcile. |
| `src/downloaders/spotdl_runner.py` | Runs inside spotdl's venv; must not import `src`. |
| `src/downloaders/soundcloud.py` | client_id resolution/cache, title backfill, engine profile. |
| `src/downloaders/base.py` | Subprocess runner (spotdl) with handshake, YouTube-search recovery, kill-tree. |
| `web_app.py` | REST + SSE + pages. User paths go through `_safe_path()`. Optional `BasicAuth` (pure ASGI). |
| `templates/index.html` / `static/style.css` | The SPA (vanilla JS) and its single token-based stylesheet. |

### Event protocol (backend → UI)

SSE at `/api/jobs/{id}/events`. Every message has `id: <seq>`; a fresh
connection replays from 0 (that's how a refreshed page rebuilds the Live tab),
`Last-Event-ID` resumes. The bus stays readable 15 min after the job ends.

- `job.started {links: {provider: [urls]}, speed}` — lays out every playlist.
- `link.started {provider, link, name, total_tracks, queued}` — `queued: true`
  when resolved, again without it when the first track starts.
- `link.metadata {provider, link, tracks:[{num,title,artists,id,status,error}]}`
  — full list with saved statuses; Spotify re-sends it after reconciling.
- `link.complete {provider, link, name, code}` — 0 done, 1 failed, 2 partial,
  3 cancelled (`src/models.py`).
- `track.progress {provider, link, event, ...}` — `track`, `track_active`,
  `track_complete` (engine: exact `index`; may carry `copied`/`recovered`),
  `track_failed` (`unavailable: true` for gone-at-source), `track_retry`,
  `searching`, `tally`, `error`.
- `track.log {provider, link, line}` — per-playlist output. Sheddable.
- `provider.cooldown {provider, seconds, rate, reason}` / `provider.rate`.
- `log {message}` — global log.
- `job.completed|partial|failed|cancelled {status, title, summary, advice}`, then `done`.

`EventBus` only keeps every event for types in `_CONTROL_MARKERS` (log lines
are capped); a new state-carrying type must be added there.

### UI rules (`index.html`)

- **Performance:** nodes are built once (`mk()`, `icon()`) and patched with
  `setText` / `setAttr` / `setIcon` / `setWidth`. Per-event work is O(1) (title
  and id indexes, `_pendingCursor`, `_dirty` sets); track rows are only built
  when a playlist is expanded. No `innerHTML` with data — ever.
- **Accessibility:** expanders are `<button aria-expanded aria-controls>`, tabs
  are a real `tablist` with arrow keys, status changes go through
  `#run-status` / `announce()`, failure reasons are visible text (not
  tooltips), touch targets ≥44px on coarse pointers. Lighthouse a11y is 100.

### Design system (`static/style.css`)

Committed direction, verified with the Impeccable and Hallmark audits
(`docs/design-audit.md`):

- **Tokens only.** Every colour is a `:root` variable, redefined for light mode
  under `prefers-color-scheme: light`. Warm graphite neutrals, one ice-cyan
  accent (deliberately none of the provider brand colours), `--ok` / `--warn` /
  `--bad` only for state. All pairs ≥4.5:1 in both themes.
- **Type:** Bricolage Grotesque (headings, controls), system UI (dense lists),
  JetBrains Mono only for numbers/logs. Fonts self-hosted in `static/fonts`.
- **Icons:** the inline SVG sprite (Lucide geometry) + real provider logos.
  No emoji or Unicode glyphs as icons in the UI.
- **Banned (Hallmark/Impeccable tells):** coloured side stripes, gradients,
  coloured glow shadows, hover lifts, `transition: all`, nested bordered
  cards, uppercase letter-spaced labels, eyebrows, "..." instead of "…".
- Motion: only the in-flight track/playlist spins; `prefers-reduced-motion`
  stops it (state stays readable from colour + word).

## downloads/ layout

```
downloads/
├── <Playlist>/(0001) Song.mp3          every provider (Spotify albums: track number)
├── _removed/<Playlist>/                songs that left the source playlist
├── .state/<provider>/<slug>-<hash>.json  progress records
├── .archive/      legacy SoundCloud archives — imported into .state once
├── .cache/        soundcloud-client-id.txt, ratelimit-<provider>.json, yt-dlp/
├── .partial/      engine staging (<provider>/) + spotify-set-aside/<key>/
├── .metadata/     playlist JSON
├── .icons/        cover art (Spotify: first song's art; engine: playlist thumbnail)
├── .errors/       spotdl --save-errors files
└── .web/          jobs.json + per-job input files (deleted when the job ends)
```

Folder names must stay byte-identical to what earlier versions produced, or a
run re-downloads the whole playlist into a new folder. The engine computes them
with yt-dlp's own `%(playlist)s` / `%(playlist_title)s` templates; a dry run
against the real library confirmed every folder matches.

File names are free to change. The engine still downloads with the old outtmpl
(that template is what computes the folder), spotdl still writes
`NNN Title - Artist.mp3`, and `tidy` renames to the final name. Files from
earlier versions are renamed the first time their playlist runs.

### Apple Music output (`src/tidy.py`, `src/loudness.py`)

- **Plan per track** (`plan_for`): TIT2 = clean title (`clean_title` strips
  "(Official Video)", "[FREE DL]", ".wav", splits "Artist - Song"), TSOT =
  `(0001) Song`, engine providers also TALB = playlist, TPE2 = playlist
  uploader, TRCK = position. Spotify keeps spotdl's album/track tags. The plan's
  `sig` is stored on the track (`tagged`) so unchanged tracks cost nothing.
- **Tags stay ID3v2.3** (what the library and iTunes use). mutagen's v2.3
  conversion deletes TSOT, so `write_tags` loads with `translate=False` and
  saves in the tag's own version. Never use `update_to_v23()`.
- **Levelling** shifts every granule's global_gain (1.5 dB/step) toward
  −14 LUFS, capped by sample peak when turning up; granules with gain < 56 are
  inaudible and clamped. Every change is re-measured before the swap, and the
  original mtime is kept. `TXXX:MULTIMP3_GAIN` marks a file as done (record:
  `gain`). New engine downloads are finished in staging before `_promote`;
  everything else goes through the `Finisher` pool, which the run waits for at
  the end (Stop leaves the rest for next run).
- **Matching files to tracks** (`match_files`) needs the number *and* a name
  that fits the title. Number-only matching handed files to whatever song had
  moved into that position, and tidying would retitle them as the wrong song.
- **Removed songs**: `sync_tracks` moves dropped tracks (with files) to the
  record's `removed`; `tidy_playlist` moves their files to `_removed/` and
  back if they return. It also sweeps numbered files that fit no track, but
  never on first sight, never in a folder another record shares, and never a
  file newer than the run's start.
- **The spotdl runner runs with `-X utf8`** and prints the song list
  ASCII-escaped: under `-I` on Windows it printed cp1252, and "Inéz" was
  recorded as "In\ufffdz". Titles containing U+FFFD are never written to files
  (`known()`).

### Never lose an existing file

Reset means "re-download over the existing MP3s", and the old copy must survive
until its replacement is complete. This bit us once:

- **Engine:** yt-dlp with `overwrites=True` deletes the existing file *before*
  downloading. Workers download into `.partial/<provider>/`;
  `YtDlpEngine._promote()` atomically replaces the real file when done.
- **Spotify:** spotdl's ffmpeg writes in place and deletes the output if
  conversion fails; a hard-link backup would be truncated too (same inode). So
  `set_aside_stale()` *moves* old copies of to-do tracks into
  `.partial/spotify-set-aside/<key>/` (with a manifest) and `restore_stale()`
  moves back any without a fresh replacement. After an unclean exit,
  `reconcile(check_truncation=True)` deletes files much shorter than the song.
- **Copies between playlists** (`dedupe.copy_track`) and **recovery searches**
  write to a temp name and swap in only when complete.
- **Tagging and levelling** (`finish_file`) build the new file beside the old
  one (`.<name>.finishing`), re-measure it, check the original didn't change
  meanwhile, then `os.replace`. Cleanup removes leftover `.finishing` /
  `.copying` temp files.
- **Renames** (`tidy._move`) use `os.rename`, which refuses to overwrite on
  Windows, plus a unique-name fallback. Nothing tidy does deletes a file:
  strays go to `_removed/`. The one delete is `replace_superseded`, the old copy
  of a re-downloaded track under its old name, only after the new file is in
  place and only if no other track owns it.
- **Reset keeps each track's `file`**, so a re-download under a new name
  replaces the old copy instead of sitting next to it.

## Provider gotchas (each one cost a debugging session)

**SoundCloud**
- Rate-limits hard. Once tripped, *every* further request fails (403/429) and
  continuing extends the block — stop fast and cool down; never keep going.
  yt-dlp's source estimates ~600 API requests / 10 min; a track costs ~2–3.
- `client_id` is scraped from soundcloud.com's JS; that page is heavy. We
  resolve it ourselves (90s timeout), cache it daily in `.cache/`, and seed
  yt-dlp's cache with it.
- The playlist API returns full data for only the first few tracks; titles are
  backfilled via `/tracks?ids=` in batches of 50.
- Files are tagged album = playlist, album artist = playlist owner, track =
  position (`plan_for`); copies into another playlist are retagged.
- yt-dlp's SoundCloud `track` field is just the upload title again; only its
  `artists` (a label's publisher metadata) adds anything. YouTube's `track` is
  the real song (YouTube Music / "Music in this video") and is used as the title.

**YouTube**
- yt-dlp prints `Destination:` twice per track; needs Deno + `yt-dlp-ejs`.
- Playlist URLs are usually `watch?v=…&list=…` — the key is the `list` id.

**Spotify**
- **spotdl is the primary source** — no Spotify developer app needed. Spotify
  refuses development-mode apps whose owner lacks Premium (`403 … Active
  premium subscription required`), so the app no longer calls Spotify's API
  itself; `CLIENTID`/`CLIENTSECRET` are optional pass-throughs.
- The track list comes from `spotdl_runner.py`'s handshake inside the same
  spotdl run. A separate `spotdl save` took 25+ min for 137 songs — never add a
  second metadata pass.
- **Run the runner with `-I -X utf8`.** As a plain script, its folder
  (`src/downloaders/`) goes first on `sys.path`, and spotdl's
  `import soundcloud` found our `soundcloud.py` — spotdl died on import. The
  runner also strips its own folder from `sys.path` as a second guard. `-X utf8`
  because `-I` ignores `PYTHONIOENCODING` (see Apple Music output).
- Must run spotdl with `--simple-tui` (the Rich TUI wraps lines and breaks
  parsing).
- Old flow: spotdl searched YouTube for every song *before* noticing the file
  existed (~10s/song). With the skip list, a fully-downloaded 135-track
  playlist re-checks in ~20s.
- spotdl only writes its own archive at the end of a run; the files on disk are
  the truth (`reconcile`).

## Configuration (env / `.env`)

| Variable | Default | Purpose |
|---|---|---|
| `OUTPUT_DIR` / `LINKS_FILE` | `downloads` / `links.txt` | Web UI paths |
| `NOTIFY_URL` | — | ntfy topic or Discord webhook |
| `WEB_USERNAME` / `WEB_PASSWORD` | — | Optional login |
| `SPOTDL_BIN` / `SPOTDL_PYTHON` | auto | spotdl executable / its venv's Python |
| `CLIENTID` / `CLIENTSECRET` | — | Optional, passed to spotdl |
| `SPOTIFY_WORKERS` / `SPOTIFY_THREADS` | 2 / 6 | Playlists at once / spotdl `--threads` |
| `YOUTUBE_WORKERS` / `_RATE` / `_MAX_RATE` | 3 / 30 / 60 | Engine workers, start + max tracks/min |
| `SOUNDCLOUD_WORKERS` / `_RATE` / `_MAX_RATE` | 3 / 20 / 40 | Same for SoundCloud |
| `SOUNDCLOUD_MAX_ATTEMPTS` / `YOUTUBE_MAX_ATTEMPTS` | 3 | Tries per track before failed |
| `SOUNDCLOUD_CLIENT_ID` / `_TIMEOUT` / `_AUTH_TOKEN` | — / 90 / — | client-id override, lookup timeout, OAuth |
| `LEVEL_VOLUME` / `LEVEL_TARGET_LUFS` / `LEVEL_WORKERS` | 1 / −14 / ≤4 | Lossless volume levelling on/off, target, parallel ffmpeg |

**Format:** MP3 only for now. The owner has only seen MP3 work through
CopyTrans → Apple Music. AAC/.m4a (SoundCloud's and YouTube's native codec, no
transcode) is a possible future option once tested on the phone.

## Windows notes

- Shell is PowerShell 5.1: no `&&`; use `;` or `if ($?) { … }`. The Monitor
  tool runs **bash**, not PowerShell.
- **Never rewrite source files with `Get-Content`/`Set-Content`** — PowerShell
  5.1 reads UTF-8 as cp1252 and writes it back mangled. Use the editor tools.
- Python printing emoji to a pipe needs `-X utf8` (isolated `-I` ignores
  `PYTHONIOENCODING`).
- A folder whose name ends in a space (`downloads/Summer better sound like
  this `) can't be opened by PowerShell, and `Path.is_dir()` says False, though
  `os.listdir` works. Windows trims trailing spaces and dots from paths.
- `npx skills add … --copy` (symlinks need Developer Mode).

## Known issues and next steps

Logged 2026-10-10, after the `(0001) Song` / levelling / `_removed` work. Fix,
verify, or strike through as they're dealt with.

**Not verified yet (needs a real run or the phone)**
- The first real run with renaming and finishing. It was rehearsed only on
  copies of three playlists (152 files, all correct). The whole 16 GB library
  takes ~15–20 min to tag and level once.
- That CopyTrans → Apple Music on the iPhone reads TSOT from ID3v2.3 and sorts
  by it. ffprobe reads it as `title-sort`; iTunes writes it the same way.
- The spotdl handshake under `-X utf8` was checked only with `--version`. Look
  for correct accents in the record after the next Spotify run.

**Bugs or risks to fix**
- `SpotifyDownloader.find_folder` relies on `Path.is_dir()`, which is False for
  folder names ending in a space or dot. Such a playlist could get a second
  folder. Fall back to `os.listdir` of the parent.
- Two playlists whose folder names are the same (two YouTube playlists both
  titled "Music") share a folder, and their numbers collide. Tidy's sweep is
  off for shared folders, but the files still mix. Give the second one a
  suffixed folder.
- After a Reset, `_copy_duplicate` / `_copy_spotify_duplicates` copy the track
  from another playlist instead of downloading it again. So a reset doesn't
  refresh a song that also lives elsewhere. Skip copying from playlists that
  were reset too, or from all playlists when a reset is meant to refresh.
- Spotify artist and single-track links have no position, so their files keep
  spotdl's `Song - Artist.mp3` and get no sort title.
- Leftovers of interrupted spotdl runs (`*.webm`, `*.webp` beside the MP3s,
  e.g. in Speed Garage 2026) are never cleaned up. Cleanup could remove ones
  older than a day.
- CI has no ffmpeg, so `tests/test_loudness.py` is skipped there. Add
  `sudo apt-get install -y ffmpeg` to the workflow.

**Ideas the owner has heard and not ruled out**
- Scheduled sync (e.g. daily) so additions and removals happen without
  pressing Download.
- AAC/.m4a output, once tested through CopyTrans on the phone (see Format).
- Library housekeeping in the owner's `downloads/`: `- backup` folders,
  `run-20260717-…`, `testing`, the trailing-space folder. That's the owner's
  call; never delete without asking.
- The Finisher's MP3 frame walk is pure Python and GIL-bound (~1 s per hour of
  audio). Fine for now, but it's the place to optimise if levelling gets slow.

## Installed skills (`.claude/skills/`)

| Skill | Use it for |
|---|---|
| `impeccable` | `audit`, `polish`, … and the `detect` command above; read `reference/craft-floor.md` before UI edits |
| `hallmark` | `hallmark audit <target>` — anti-AI-slop punch list |
| `frontend-design` | General frontend design guidance |
| `fastapi` | FastAPI idioms for `web_app.py` |
| `python-testing-patterns` | pytest patterns for `tests/` |
| `yt-dlp` | User-facing download helper; triggers on video URLs — not a dev reference |
| `find-skills` | `npx skills find <query>` |
