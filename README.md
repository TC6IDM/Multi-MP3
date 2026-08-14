# Multi-MP3 — Playlist Downloader (Spotify / YouTube / SoundCloud)

Download music from **Spotify** (playlists, albums, tracks, artists), **YouTube** (playlists, channels), and **SoundCloud** (playlists) into a local `downloads/` folder — with consistent naming, parallel downloads, and missing-track reporting.

Two ways to run it:

| Interface | Best for | Quick start |
|-----------|----------|-------------|
| 🌐 **Web UI** | Watching progress live, browsing your library | `docker compose up -d --build web` → <http://localhost:8080> |
| ⌨️ **CLI** | Scripting, cron jobs, headless servers | `./run.sh links.txt` |

---

## Quick Start

**1. Add Spotify credentials** — create a `.env` file in the project root:

```env
CLIENTID=your_spotify_client_id
CLIENTSECRET=your_spotify_client_secret
```

Get these from the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) (create an app; any redirect URI works).

**2. Add your links** to `links.txt` (one per line):

```text
# Comments are ignored
https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M
[My Mix](https://open.spotify.com/playlist/...)
https://www.youtube.com/playlist?list=PLxxxxxx
https://soundcloud.com/user/sets/my-playlist
```

**3. Run it** — pick the Web UI or CLI below.

---

## 🌐 Running the Web UI

The web interface gives you live per-playlist progress, expandable track lists, a file browser, and run history.

### Start it

```bash
docker compose up -d --build web
```

Then open **<http://localhost:8080>**.

To stop it:

```bash
docker compose down
```

To follow server logs:

```bash
docker compose logs -f web
```

### Without Docker

```bash
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1
# Linux/Mac: source .venv/bin/activate

pip install -e ".[web]"
uvicorn web_app:app --host 0.0.0.0 --port 8080
```

> Requires `ffmpeg` and [Deno](https://deno.land) on your PATH. Deno is needed by `yt-dlp` for YouTube's JavaScript challenge — without it you get HTTP 403 errors.

### Using the interface

The dashboard has four tabs:

| Tab | What it does |
|-----|--------------|
| **Dashboard** | Paste links, pick providers, toggle parallel mode, start a run |
| **Live** | Real-time progress — expandable provider sections with per-playlist cards |
| **Library** | Browse downloaded playlists, play or download individual MP3s |
| **History** | Past runs with status, exit codes, and link counts |

**On the Dashboard tab:**

- Under the box, a **live count** of what was recognised — `Parsed: 8 links (▶ 1 ☁ 4 ● 3)` — updates as you type or paste, without saving first.
- Unticking a provider dims and strikes through its tally and drops it from the total, so the number always matches what pressing Start would actually download.

**On the Live tab:**

- Each provider (YouTube / SoundCloud / Spotify, shown with its own logo) is a collapsible section — click the header to expand or collapse.
- Inside are **playlist cards** showing the playlist name, a track progress bar, and current status.
- **Click a card** to expand its full track list — every track shows ✅ downloaded, 🔄 downloading, or ⏳ pending.
- Click **📜 Log** on a card to see the subprocess output for *that playlist only*, instead of one giant merged log.
- All playlists appear immediately as ⏸ Queued when the run starts, so you can see the whole plan up front.

### Configuration

Set via environment variables (or in `docker-compose.yml`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUTPUT_DIR` | `downloads` | Where MP3s are written |
| `LINKS_FILE` | `links.txt` | Default links file loaded into the editor |
| `CLIENTID` | — | Spotify client ID (required) |
| `CLIENTSECRET` | — | Spotify client secret (required) |

Change the port by editing the `ports` mapping in `docker-compose.yml` (e.g. `"9000:8080"`).

### REST API

The web UI is backed by a JSON API you can drive directly:

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/api/status` | Current job state and credential check |
| `GET` / `PUT` | `/api/links` | Read or save `links.txt` |
| `POST` | `/api/links/parse` | Count the links in unsaved text — `{text}` → per-provider tallies |
| `POST` | `/api/jobs` | Start a run — `{links, providers, parallel}` |
| `GET` | `/api/jobs` | Run history |
| `GET` | `/api/jobs/{id}` | Single run detail |
| `DELETE` | `/api/jobs/{id}` | Cancel the running job |
| `GET` | `/api/jobs/{id}/events` | SSE stream of live progress |
| `GET` | `/api/files` | Browse the output directory |
| `GET` | `/api/files/{path}/download` | Stream an MP3 |

Interactive docs are served at `/docs`.

---

## ⌨️ Running from the Command Line

### With the wrapper scripts (builds the image for you)

**Bash:**

```bash
chmod +x run.sh
./run.sh                      # uses links.txt
./run.sh links.txt            # explicit input file
./run.sh links.txt --parallel # parallel downloads
```

**PowerShell:**

```powershell
./run.ps1                              # uses links.txt
./run.ps1 -InputFile "links.txt"
./run.ps1 -InputFile "links.txt" -Parallel
```

Both scripts build the image, create `./downloads`, mount your config and links, and run the CLI.

### With docker compose

```bash
docker compose --profile cli run --rm cli
```

### With plain Docker

```bash
docker build -t playlist-downloader .

docker run --rm \
  -v "$(pwd)/.spotdl:/root/.config/spotdl" \
  -v "$(pwd)/links.txt:/app/input_links.txt:ro" \
  -v "$(pwd)/downloads:/app/music" \
  -v "$(pwd)/.env:/app/.env:ro" \
  playlist-downloader \
  input_links.txt /app/music --parallel
```

### Without Docker

```bash
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1
# Linux/Mac: source .venv/bin/activate

pip install -e .
python main.py links.txt downloads/ --parallel
```

### CLI reference

```
python main.py [input] [output] [options]
```

| Argument / Flag | Default | Description |
|-----------------|---------|-------------|
| `input` (positional) | `links.txt` | File containing links |
| `output` (positional) | `downloads` | Output directory |
| `-i`, `--input-file` | — | Override the input file |
| `-o`, `--output-dir` | — | Override the output directory |
| `-p`, `--parallel` | off | Run providers and links concurrently (off = fully sequential) |
| `--max-workers N` | `4` | Concurrency limit when `--parallel` is set |
| `--providers ...` | `all` | Any of `spotify`, `youtube`, `soundcloud`, `all` |
| `--tui` | off | Rich progress bars in the terminal |

**Examples:**

```bash
# Everything in links.txt, sequentially
python main.py

# Parallel, 8 workers, with progress bars
python main.py links.txt downloads/ --parallel --max-workers 8 --tui

# Only Spotify and YouTube
python main.py --providers spotify youtube

# Custom paths via flags
python main.py -i my-links.txt -o /media/music --parallel
```

---

## Input File Format

One link per line. Blank lines and `#` comments are ignored. Both plain URLs and Markdown links work.

| Provider | Plain URL | Markdown |
|----------|-----------|----------|
| Spotify | `https://open.spotify.com/playlist/...` | `[name](https://open.spotify.com/playlist/...)` |
| YouTube | `https://www.youtube.com/playlist?list=...` | `[name](https://youtube.com/...)` |
| SoundCloud | `https://soundcloud.com/user/sets/...` | `[name](https://soundcloud.com/...)` |

Spotify albums, tracks, and artist pages are supported alongside playlists.

---

## Output Layout

```
downloads/
├── My Spotify Playlist/
│   ├── 01 Artist - Song.mp3
│   └── 02 Artist - Another Song.mp3
├── My YouTube Playlist/
│   └── 01 Uploader - Video Title.mp3
├── .metadata/          # Aggregated playlist JSON
├── .icons/             # Playlist cover art
├── .errors/            # Per-run error files
├── .archive/           # Per-playlist SoundCloud resume state
├── .web/               # Web UI job history
└── spotdl.log          # Full run log
```

| Provider | Naming template |
|----------|-----------------|
| Spotify | `{list-name}/{list-position} {title} - {artists}.mp3` |
| YouTube | `%(playlist_title)s/%(playlist_index)02d %(uploader)s - %(title)s.mp3` |
| SoundCloud | `%(playlist)s/%(playlist_index)04d %(uploader)s - %(title)s.mp3` |

---

## Parallelism

With `--parallel` set (the Web UI's **Parallel** checkbox, on by default), downloads run concurrently at three levels:

1. **Across providers** — Spotify, YouTube, and SoundCloud run at the same time.
2. **Within a provider** — multiple playlists download concurrently, bounded by `--max-workers`.
3. **Within a playlist** — `spotdl` and `yt-dlp` use their own internal worker pools for individual tracks.

**Turn it off and the entire run is single-threaded** — one provider, then the next; one playlist at a time; one track at a time. That is the setting to use for large SoundCloud runs: SoundCloud rate-limits by IP, and once tripped it rejects the cached `client_id` *and* blocks the refresh request, so every remaining track fails with 403. SoundCloud requests are additionally paced within a playlist (`--sleep-requests`, `--concurrent-fragments 1`, exponential retry backoff), so a serial run is slower but actually finishes.

Cleanup is deliberately deferred until all downloads finish. Running it earlier would let one playlist's metadata scan race against another's in-flight writes.

Logs include thread names, so you can confirm parallel execution:

```
2026-08-08 12:00:01 | INFO | ThreadPoolExecutor-0_1 | 🎵 yt-dlp: https://...
2026-08-08 12:00:01 | INFO | ThreadPoolExecutor-0_2 | 🎵 SpotDL: https://...
```

---

## Resuming a Failed SoundCloud Playlist

SoundCloud rate-limits by IP, and a long playlist can hit the wall partway through — the run doesn't crash, it just fails every remaining track with 403 while still exiting 0.

Each SoundCloud playlist gets a **download archive** at `downloads/.archive/<playlist>-<hash>.txt` recording the track IDs it has successfully downloaded. Only successful downloads are recorded, so failures are always retried.

When a run ends with failed tracks, the playlist is **automatically re-run from where it stopped**, after a wait for the rate limit to clear:

```
⏳ 358 track(s) failed after 102 new this pass — waiting 60s for the rate limit to clear, then resuming.
▶️ Resuming — 102 track(s) already done, skipping those (attempt 2/3)
✅ Resume complete — 460 track(s) downloaded in total
```

The archive is what makes this cheap. Without it, a resumed run would call the API once per track it already has, just to work out the filename and find the file already exists — burning the same rate limit that caused the failure. yt-dlp checks the archive against the playlist entry, which already carries the track ID from the single playlist call, so skipped tracks cost no requests at all.

Archived tracks are reported to the UI as they're skipped, so a resumed playlist starts with a ✅ against everything the previous run got and its counter opens at `102/460` rather than `0/460`:

```
⏭️ scdl: 102 track(s) already downloaded on an earlier run — skipped
```

If it still can't finish, it stops and tells you where to pick up; just re-run the same link later and it resumes from there. Two passes in a row that download nothing new also stop it early, rather than waiting out attempts that aren't helping.

### Failing before the first track

A separate failure kills the run at startup instead of partway: scdl derives an API `client_id` by downloading soundcloud.com and its JS bundles under a fixed 30-second curl timeout, and a slow response there takes the whole playlist with it — `curl: (28) Operation timed out ... with 557020 bytes received`, before a single track.

So the `client_id` is resolved here first, with a generous timeout and an on-disk cache at `downloads/.cache/soundcloud-client-id.txt`, and handed to scdl with `--client-id`; the usual run never touches soundcloud.com at startup at all. A run that still dies before its first track is retried on a short clock (10s, 30s, 60s) with a freshly fetched id, since nothing was rate-limited and there's nothing to wait out.

| Variable | Default | Purpose |
|----------|---------|---------|
| `SOUNDCLOUD_MAX_ATTEMPTS` | `3` | How many times to resume a partially-failed playlist |
| `SOUNDCLOUD_RETRY_BACKOFF` | `60,300,900` | Seconds to wait before each resume |
| `SOUNDCLOUD_AUTH_TOKEN` | — | Optional; an authenticated session gets a much higher rate limit |
| `SOUNDCLOUD_CLIENT_ID` | — | Optional; skips the `client_id` lookup and cache entirely |
| `SOUNDCLOUD_CLIENT_ID_TIMEOUT` | `90` | Seconds allowed for that lookup |

> Deleting a playlist's file in `.archive/` forces a full re-check on the next run. Do that if you delete MP3s by hand and want them fetched again — the archive would otherwise consider them done.

---

## Missing Track Detection

After downloading, each provider's `cleanup()`:

1. Aggregates metadata JSON into `.metadata/<playlist>.json`
2. Scans the playlist folder for numbered files (`01.mp3`, `02.mp3`, …)
3. Compares against the expected track count from metadata
4. Logs anything absent:

```
⚠️ 2 missing in My Playlist:
  🚫 Missing 03
  🚫 Missing 07
```

Spotify playlists longer than 100 tracks are paginated automatically — the API caps each page at 100, so the full list is fetched across multiple requests.

---

## Configuration Files

### `.env` (required)

```env
CLIENTID=your_spotify_client_id
CLIENTSECRET=your_spotify_client_secret
```

### `.spotdl/config.json` (optional)

Copy `.spotdl/config.example.json` → `.spotdl/config.json` to get started:

```json
{
  "bitrate": "256k",
  "format": "mp3",
  "overwrite": "skip",
  "threads": 4,
  "log_level": "INFO"
}
```

Mounted as a volume, so it persists between runs.

> **Note:** `.spotdl/config.json` is gitignored — it can hold API tokens. Never commit it.

---

## Project Structure

```
├── main.py                 # CLI entrypoint (argparse)
├── web_app.py              # FastAPI web app
├── Dockerfile              # CLI image
├── Dockerfile.web          # Web UI image
├── docker-compose.yml      # web + cli services
├── run.sh / run.ps1        # CLI wrapper scripts
├── links.txt               # Your links
├── src/
│   ├── coordinator.py      # Orchestrates providers, manages threading
│   ├── models.py           # Song, Playlist
│   ├── utils.py            # Logging, link parsing, credentials
│   ├── tui.py              # Rich progress display
│   ├── event_bus.py        # Thread-safe bridge to SSE streaming
│   ├── web_progress.py     # Progress events for the web UI
│   ├── web_handler.py      # Log handler feeding the SSE stream
│   ├── job_manager.py      # Job lifecycle and history
│   └── downloaders/
│       ├── base.py         # Shared download/cleanup logic
│       ├── spotify.py      # spotdl + spotipy
│       ├── youtube.py      # yt-dlp
│       └── soundcloud.py   # scdl
├── templates/              # Jinja2 templates
└── static/                 # CSS
```

---

## Requirements

**Host:** Docker Desktop or Docker Engine. (Python 3.12+ if running without Docker.)

**In the image:** Python 3.12-slim, `ffmpeg`, [Deno](https://deno.land), and the Python packages `spotdl`, `spotipy`, `yt-dlp`, `scdl`, `psutil`, `rich`, `python-dotenv` — plus `fastapi`, `uvicorn`, and `jinja2` for the web image.

---

## Troubleshooting

**YouTube downloads fail with HTTP 403**
`yt-dlp` needs a JavaScript runtime for YouTube's challenge. The Docker images install Deno automatically; if running natively, install Deno and make sure it's on your PATH.

**`Missing Spotify credentials`**
Check that `.env` exists in the project root with `CLIENTID` and `CLIENTSECRET`, and that it's mounted into the container.

**Spotify playlist stops at 100 tracks**
Fixed — the API paginates at 100 items per page and the downloader now fetches all pages. If you still see this, confirm you're on a rebuilt image.

**Web UI shows no progress**
Hard-refresh the browser (Ctrl+Shift+R) to clear cached JS/CSS after rebuilding.

**Port 8080 already in use**
Edit the `ports` mapping in `docker-compose.yml`, e.g. `"9000:8080"`.

---

## Development

```bash
pip install -e ".[dev]"

ruff check src/ main.py web_app.py     # lint
mypy src/ main.py web_app.py           # type check
pytest                                  # tests
```

CI runs lint, type checks, and builds both Docker images on push.

---

## License

MIT — see [LICENSE](LICENSE).
