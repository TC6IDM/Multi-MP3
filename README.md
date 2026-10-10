# Multi-MP3 — Playlist Downloader (Spotify / YouTube / SoundCloud)

Keep **Spotify** (playlists, albums, tracks, artists), **YouTube** (playlists, channels) and **SoundCloud** (playlists) downloaded as clean, tagged MP3s in a local `downloads/` folder.

- All three services download at the same time.
- A rate limiter knows when to back off.
- A per-track record means every run only fetches what's missing.
- A track that's in several playlists is downloaded once.

Two ways to run it:

| Interface | Best for | Quick start |
|-----------|----------|-------------|
| 🌐 **Web UI** | Managing links, watching progress, browsing your library | `docker compose up -d --build web` → <http://localhost:8080> |
| ⌨️ **CLI** | Scripting, scheduled runs, headless servers | `./run.sh links.txt` |

---

## Quick Start

**1. Add your links** to `links.txt` (one per line), or paste them into the web UI's **Links** tab:

```text
# Comments and blank lines are kept as they are
https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M Speed Garage
[My Mix](https://www.youtube.com/playlist?list=PLxxxxxx)
https://soundcloud.com/user/sets/my-playlist gym
#https://soundcloud.com/user/sets/old-one   ← a leading # switches a link off
```

**2. Run it** with the Web UI or the CLI below.

No accounts or API keys are needed: spotdl reads Spotify without a developer app. Everything in `.env` is optional; see [`.env-example`](.env-example).

---

## 🌐 Running the Web UI

```bash
docker compose up -d --build web     # then open http://localhost:8080
docker compose logs -f web           # follow the server log
docker compose down                  # stop
```

### Without Docker

The app and spotdl live in **two separate environments**. Every spotdl release pins old versions of `fastapi`, `uvicorn` and `rich`, which would hold the web app back if they shared one. The app only ever runs spotdl as a separate program. [uv](https://docs.astral.sh/uv/) installs exactly the versions pinned in `uv.lock` and `requirements-spotdl.txt`:

```powershell
uv sync --extra web                                        # the app → .venv
uv venv .venv-spotdl
uv pip install --python .venv-spotdl -r requirements-spotdl.txt
.venv\Scripts\uvicorn web_app:app --host 0.0.0.0 --port 8080
```

(No uv? Plain pip works too: `pip install -e ".[web]"` into one venv, and `pip install -r requirements-spotdl.txt` into `.venv-spotdl`.)

The app finds `.venv-spotdl` by itself; set `SPOTDL_BIN` if spotdl lives elsewhere. `ffmpeg` must be on your PATH. **Deno**, which yt-dlp needs for YouTube's JavaScript challenge, comes as a pip package, so there's nothing else to install.

### The tabs

| Tab | What it's for |
|-----|---------------|
| **Links** | Your playlists. Add by pasting a link, rename with a label, switch one off without deleting it, and see at a glance which are up to date (`Up to date · 182`, `14 left of 182`, `New`). Pick providers and a **Speed**, then **Download**. *Edit links.txt as text* is there for bulk changes. |
| **Live** | The run in progress: one section per provider with its download rate or rate-limit countdown, one row per playlist, and every track's state (downloaded, downloading, to do, failed with the reason, unavailable). **Refreshing the page mid-run rebuilds all of it**, and other tabs or devices can watch the same run. |
| **Library** | Every playlist with its cover, progress, size and failures. **Resume**, **Retry failed**, **Reset**, search, and **Failed tracks**: everything that couldn't be downloaded, why, and YouTube/SoundCloud search links to find it by hand. Click a playlist to play it in the browser. |
| **History** | Past runs: result, tracks done/failed/left, speed, and how long they took. |
| **System** | Versions, a one-click **yt-dlp update**, notifications, and leftover cleanup. |

**Starting a run while one is going queues it.** It starts automatically when the current one finishes, and you can drop it from *Up next* on the Live tab. Only one run downloads at a time on purpose: two at once would double the request rate the limiters manage.

**Stop run really stops.** Tracks that are mid-download are dropped, everything finished is saved, and the rest stays recorded as still to do.

### Notifications

- **Browser:** allow notifications (System tab, or when you first press Download). You'll get one when a run finishes while the tab is in the background.
- **Phone:** set `NOTIFY_URL` to an [ntfy](https://ntfy.sh) topic (`https://ntfy.sh/<your-topic>`, then subscribe to it in the ntfy app) or a Discord webhook URL. *System → Send a test* checks it works.

### Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUTPUT_DIR` | `downloads` | Where MP3s are written |
| `LINKS_FILE` | `links.txt` | The links file the Links tab edits |
| `NOTIFY_URL` | — | ntfy topic or Discord webhook for "run finished" pings |
| `WEB_USERNAME` / `WEB_PASSWORD` | — | Require a login. The server listens on your whole network |
| `SPOTDL_BIN` / `SPOTDL_PYTHON` | auto | Where spotdl and its environment's Python are |
| `CLIENTID` / `CLIENTSECRET` | — | Optional Spotify developer app, passed to spotdl (its owner needs Premium) |

Speed tuning is covered under [Speed and rate limiting](#speed-and-rate-limiting). Change the port with the `ports` mapping in `docker-compose.yml`.

### REST API

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/api/status` | Running job, queue, tooling checks |
| `GET` / `PUT` | `/api/links` | links.txt as text and as entries (with each playlist's sync state). `PUT` takes `{text}` or `{entries}` |
| `POST` | `/api/links/parse` | Count the links in unsaved text |
| `POST` | `/api/jobs` | Start (or queue) a run: `{links, providers, speed}` |
| `GET` | `/api/jobs`, `/api/jobs/{id}` | History / one run |
| `DELETE` | `/api/jobs/{id}` | Stop the running job, or remove a queued one |
| `GET` | `/api/jobs/{id}/events` | SSE stream, replayable from the start; honours `Last-Event-ID` |
| `GET` | `/api/library` | Playlists with progress, size and cover |
| `GET` | `/api/state`, `/api/state/{provider}/{key}` | Progress records |
| `POST` | `/api/state/reset` | Forget progress: `{targets: [{provider, key}]}`, or `{}` for all |
| `POST` | `/api/state/retry` | Give failed and unavailable tracks another go |
| `GET` | `/api/report/failed`, `/api/report/failed.txt` | Everything that couldn't be downloaded, with search links |
| `GET` / `POST` | `/api/system`, `/api/system/update-ytdlp`, `/api/system/cleanup`, `/api/system/test-notification` | Maintenance |
| `GET` | `/api/files`, `/api/files/{path}/download`, `/api/covers/{name}` | File browser, MP3 streaming, cover art |

Interactive docs are served at `/docs`.

---

## ⌨️ Running from the Command Line

```bash
./run.sh                        # Docker: builds the image, uses links.txt
./run.sh links.txt --speed fast
./run.ps1 -InputFile "links.txt"
docker compose --profile cli run --rm cli
python main.py links.txt downloads/        # without Docker (see setup above)
```

```
python main.py [input] [output] [options]
```

| Argument / Flag | Default | Description |
|-----------------|---------|-------------|
| `input` / `output` | `links.txt` / `downloads` | Links file / output directory (`-i` / `-o` also work) |
| `-s`, `--speed` | `balanced` | `careful`, `balanced` or `fast` |
| `-p`, `--parallel` | — | Kept for old scripts; same as `--speed balanced` |
| `--max-workers N` | per speed | Tracks (SoundCloud/YouTube) or playlists (Spotify) each provider works on at once |
| `--providers ...` | `all` | Any of `spotify`, `youtube`, `soundcloud`, `all` |
| `--tui` | off | Rich progress bars in the terminal |

Ctrl-C stops cleanly: finished tracks are saved and the rest stay to do.

---

## Output Layout

```
downloads/
├── My Spotify Playlist/01 Song - Artist.mp3
├── My YouTube Playlist/01 Uploader - Video Title.mp3
├── My SoundCloud Set/0001 Uploader - Track.mp3
├── .state/       # progress records: one JSON file per playlist
├── .metadata/    # playlist info
├── .icons/       # cover art
├── .cache/       # SoundCloud client id, learned rate limits, yt-dlp cache
├── .partial/     # downloads in progress; old copies kept safe during a re-download
├── .errors/      # spotdl error lists
├── .web/         # web UI job history
└── spotdl.log    # CLI log
```

| Provider | Naming template |
|----------|-----------------|
| Spotify playlist | `{list-name}/{list-position} {title} - {artists}.mp3` |
| Spotify album | `{list-name}/{track-number} {title} - {artists}.mp3` |
| YouTube | `%(playlist_title)s/%(playlist_index)02d %(uploader)s - %(title)s.mp3` |
| SoundCloud | `%(playlist)s/%(playlist_index)04d %(uploader)s - %(title)s.mp3` |

SoundCloud files are tagged with the playlist as album and the position as track number, so a playlist stays together when you import it into Apple Music (for example with CopyTrans).

---

## Speed and rate limiting

Spotify, YouTube and SoundCloud always run **at the same time**. They're different services, so there's no reason for one to wait. Each then sets its own pace.

**SoundCloud and YouTube** run through yt-dlp inside the app, with a pool of workers per provider downloading in parallel. **One shared rate limiter** decides when the next track may start, across every playlist in the run:

- After each stretch of clean downloads it raises the rate a little, so it finds the real limit instead of guessing.
- When the service refuses (HTTP 429/403, or YouTube's "confirm you're not a bot"), it **halves the rate and pauses every worker at once**. It waits as long as the service asks, or else 60 seconds, then 180, then 600.
- It remembers the rate it settled on, and the next run starts just below it.
- If the service is still refusing after five cooldowns in a row, it stops. Everything not yet downloaded stays recorded as still to do.

**Spotify** runs through spotdl, with several playlists at once and spotdl's own threads. spotdl hands the app the playlist's song list as soon as it has read it. The app answers with the songs already done, so spotdl only works on what's missing. A 135-track playlist that's already downloaded is checked in about 20 seconds.

| Speed | SoundCloud | YouTube | Spotify |
|-------|-----------|---------|---------|
| `careful` | 1 worker, 8 tracks/min | 1 worker, 15/min | 1 playlist, 3 threads |
| `balanced` (default) | 3 workers, starts at 20/min (max 40) | 3 workers, 30/min (max 60) | 2 playlists, 6 threads |
| `fast` | 4 workers, 30/min (max 60) | 4 workers, 45/min (max 90) | 3 playlists, 8 threads |

Fine-tune the balanced preset (`fast` is derived from it; `careful` ignores these):

| Variable | Default | Purpose |
|----------|---------|---------|
| `SOUNDCLOUD_WORKERS` / `YOUTUBE_WORKERS` | 3 | Tracks in flight at once |
| `SOUNDCLOUD_RATE` / `YOUTUBE_RATE` | 20 / 30 | Starting tracks per minute (overrides the learned rate) |
| `SOUNDCLOUD_MAX_RATE` / `YOUTUBE_MAX_RATE` | 40 / 60 | Ceiling for the adaptive rate |
| `SOUNDCLOUD_MAX_ATTEMPTS` / `YOUTUBE_MAX_ATTEMPTS` | 3 | Tries per track before it's recorded as failed |
| `SPOTIFY_WORKERS` / `SPOTIFY_THREADS` | 2 / 6 | Playlists at once / spotdl `--threads` |
| `SOUNDCLOUD_AUTH_TOKEN` | — | Optional OAuth token. A signed-in session gets much higher limits |
| `SOUNDCLOUD_CLIENT_ID` / `SOUNDCLOUD_CLIENT_ID_TIMEOUT` | — / 90 | Skip, or allow more time for, the client-id lookup |

---

## Progress records, resume, reset

Every playlist has a record at `downloads/.state/<provider>/<playlist>.json` listing each track as **done**, **failed**, **still to do**, or **unavailable** (removed, private or region-locked). Each track is written to the record the moment it finishes, so a run that dies partway leaves an exact list of what's left.

- **Resume** by running the same link again, or with **Resume** in the Library. Finished tracks cost nothing.
- **Delete an MP3** and the next run fetches it again.
- **The same song in two playlists** is downloaded once and copied into the second, re-tagged for that playlist.
- **Retry failed** gives failed and unavailable tracks another go. Runs otherwise skip unavailable ones so they don't cost a request every time.
- **Reset** (per playlist, or **Reset all**) makes the next run **re-download every track and overwrite the existing MP3s**. Old files stay in place until each replacement has *completely* downloaded, so a reset run that's stopped or rate-limited never leaves you with fewer files.
- **Upgrading:** the first time a playlist is seen, files already on disk (and the old `.archive/` files) count as done, so nothing you have is fetched again.

---

## Maintenance

- **yt-dlp** breaks whenever a site changes, and fixes ship within days. *System → Update yt-dlp* upgrades it for the app and for spotdl's environment; restart the app afterwards. Everything else is pinned exactly by `uv.lock` (app) and `requirements-spotdl.txt` (spotdl).
- **Cleanup** (*System → Clean up leftovers*, also run at startup) removes finished jobs' input files, old spotdl error lists and interrupted downloads. Music and records are never touched.

---

## Project Structure

```
├── main.py / web_app.py          # CLI / FastAPI app
├── Dockerfile / Dockerfile.web   # Python 3.13 + uv images
├── pyproject.toml / uv.lock      # app dependencies, pinned
├── requirements-spotdl.txt       # spotdl's environment, pinned
├── src/
│   ├── coordinator.py            # one lane per provider, all at once; Spotify flow
│   ├── state.py                  # per-playlist progress records
│   ├── ratelimit.py              # shared adaptive rate limiter
│   ├── dedupe.py                 # download each track once, copy into other playlists
│   ├── links.py                  # links.txt ↔ the Links tab, keeping its shape
│   ├── settings.py               # speed presets + environment overrides
│   ├── event_bus.py              # replayable event log behind the live stream
│   ├── job_manager.py            # job lifecycle, queue, history
│   ├── notify.py                 # ntfy / Discord notifications
│   ├── maintenance.py            # versions, yt-dlp update, cleanup
│   └── downloaders/
│       ├── ytdlp_engine.py       # in-app yt-dlp worker pool (SoundCloud + YouTube)
│       ├── soundcloud.py / youtube.py
│       ├── spotify.py            # spotdl + records against disk
│       ├── spotdl_runner.py      # spotdl with the track-list handshake
│       └── base.py               # subprocess runner, YouTube-search recovery
├── templates/ static/            # UI (fonts self-hosted in static/fonts)
├── tests/                        # pytest, offline
└── docs/design-audit.md          # UI audit and what was fixed
```

---

## Troubleshooting

**YouTube downloads fail with HTTP 403**
yt-dlp needs Deno. It's installed with the app's packages, so check the app's environment is the one you start it from. The Links tab warns if Deno can't be found.

**SoundCloud keeps cooling down**
Try **Careful**, lower `SOUNDCLOUD_RATE`, or set `SOUNDCLOUD_AUTH_TOKEN`. Progress is never lost; a later run continues from exactly where this one stopped.

**Something stopped working after a site change**
*System → Update yt-dlp*, then restart.

**Web UI looks stale after updating**
Hard-refresh the browser (Ctrl+Shift+R).

---

## Development

```bash
uv sync --extra web --extra dev
uv run pytest -q                                      # ~3s, offline
uv run ruff check src/ main.py web_app.py tests/
uv run mypy src/ main.py web_app.py --ignore-missing-imports
```

CI runs lint, type checks and tests on Python 3.13, and builds both Docker images. `CLAUDE.md` has the architecture notes and the problems each provider has caused so far.

---

## License

MIT, see [LICENSE](LICENSE). Bundled fonts (Bricolage Grotesque, JetBrains Mono) are under the SIL Open Font License; see `static/fonts/`.
