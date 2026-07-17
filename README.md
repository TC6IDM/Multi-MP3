# Playlist Downloader (Spotify / YouTube / SoundCloud)

A Dockerized CLI tool to download music from **Spotify playlists/albums/tracks/artists**, **YouTube playlists/channels**, and **SoundCloud playlists** into a local `downloads/` folder, with consistent naming and basic missing-track reporting.

## Features

- **Spotify**
  - Supports playlists, albums, tracks, and artists
  - Uses `spotdl` + `spotipy` for metadata and downloading
  - Per-playlist folders with auto-numbered filenames
  - Detects missing tracks via metadata and logs them

- **YouTube**
  - Uses `yt-dlp` to download playlists/channels to MP3
  - Per-playlist folders and `.info.json` metadata
  - Reports missing/failed videos

- **SoundCloud**
  - Uses `scdl` + `yt-dlp` for playlist downloading
  - Per-playlist folders with consistent naming
  - Aggregates `.info.json` into playlist metadata and logs missing items

- **Unified CLI**
  - Single `main.py` entrypoint
  - One **input file** (`links.txt`) for all providers
  - One **output directory** (`downloads/`) with subfolders per playlist/album

## Project Structure
├── main.py # CLI entrypoint
├── Dockerfile
├── run.sh # Bash wrapper
├── run.ps1 # PowerShell wrapper
├── links.txt # Example input file (Spotify/YouTube/SoundCloud links)
├── .spotdl/ # spotdl cache + config (mounted into container)
├── downloads/ # Output folder (created at runtime, gitignored)
└── src/
  ├── init.py
  ├── models.py # Song, Playlist model classes
  ├── utils.py # logging, link parsing, Spotify creds, error parsing
  ├── base.py # BaseDownloader interface
  ├── coordinator.py # Orchestrates all providers
  └── downloaders/
    ├── init.py
    ├── spotify.py # SpotifyDownloader
    ├── youtube.py # YouTubeDownloader
    └── soundcloud.py # SoundCloudDownloader



## Requirements

### Host (outside Docker)

- Docker Desktop or Docker Engine
- (Optional) Python 3.12 + virtualenv if you want to run without Docker
- A `.env` file with Spotify credentials:
  ```env
  CLIENTID=your_spotify_client_id
  CLIENTSECRET=your_spotify_client_secret

### Inside Docker (handled by Dockerfile)
- Python 3.12-slim base image
- ffmpeg
- Python Packages:
  - Spotdl
  - spotipy
  - python-dotenv
  - yt-dlp
  - scdl
 
## Input File Format (`links.txt`)

The tool reads an input file (default `links.txt` or `links.example.txt`) containing links, one per line:

```text
# Comments are ignored
https://open.spotify.com/playlist/...
[My playlist](https://open.spotify.com/playlist/...)
https://www.youtube.com/playlist?list=...
https://soundcloud.com/user/sets/...
```
## Supported formats:
| Provider   | Plain URL                                 | Markdown                                      |
| ---------- | ----------------------------------------- | --------------------------------------------- |
| Spotify    | https://open.spotify.com/playlist/...     | [name](https://open.spotify.com/playlist/...) |
| YouTube    | https://www.youtube.com/playlist?list=... | [name](https://youtube.com/...)               |
| SoundCloud | https://soundcloud.com/user/sets/...      | [name](https://soundcloud.com/...)            |


## Running with Docker

### 1. Build the image

From the project root:

**PowerShell:**
- `docker build -t playlist-downloader .`

**Bash:**
- `docker build -t playlist-downloader .`

### 2. Use the provided scripts

#### PowerShell (`run.ps1`)
- `./run.ps1` (default: `links.example.txt`)
- `./run.ps1 -InputFile "links.txt"`

#### Bash (`run.sh`)
- `chmod +x run.sh`
- `./run.sh` (default: `links.example.txt`)
- `./run.sh links.txt`

**What the scripts do:**
- Build `playlist-downloader` image
- Ensure `./downloads` exists  
- Volume mounts:
  - `./.spotdl` → `/root/.config/spotdl` (spotdl config/cache)
  - `./<InputFile>` → `/app/input_links.txt` (read-only)
  - `./downloads` → `/app/music` (output)
  - `./.env` → `/app/.env` (read-only)
- Runs: `python main.py input_links.txt /app/music`

## Running Without Docker (Optional)

- `python -m venv .venv`
- Activate: `source .venv/bin/activate` (Linux/Mac) or `.venv\Scripts\Activate.ps1` (Windows)
- `pip install spotdl spotipy python-dotenv yt-dlp scdl ffmpeg`
- `python main.py links.txt downloads/`

**Requires `.env` with Spotify credentials in project root.**

## How It Works

**1. Entry Point (main.py)**
- Parses CLI args: input file + output dir
- Loads `.env` credentials
- Sets up logging (console + spotdl.log)
- Creates Coordinator → calls `process_all()`

**2. Coordinator**
- `read_links()` → groups by provider (spotify/youtube/soundcloud)
- Processes in order: soundcloud → youtube → spotify
- For each link: download → cleanup → log missing tracks

**3. Downloaders**
| Provider | Tool | Output Template |
|----------|------|-----------------|
| Spotify  | spotdl | `{playlist}/{playlist_index} {title} - {artist}.mp3` |
| YouTube  | yt-dlp | `%(playlist_title)s/%(playlist_index)02d %(uploader)s - %(title)s.mp3` |
| SoundCloud | scdl | `%(playlist)s/%(playlist_index)04d %(uploader)s - %(title)s.mp3`

## Missing Track Detection

Each provider's `cleanup()` method:
- Collects metadata JSONs into `.metadata/<playlist>.json`
- Scans playlist directory for numbered files (01.mp3, 02.mp3...)
- Compares against metadata total_tracks/playlist_count
- Logs missing tracks:
  ⚠️ 2 missing in My Playlist
  Missing 03 Some Track
  Missing 07 Another Track

## Configuration

### .env (Required)
CLIENTID=your_spotify_client_id
CLIENTSECRET=your_spotify_client_secret


### .spotdl/config.json (Optional)
{
"bitrate": "256k",
"format": "mp3",
"overwrite": "skip",
"log_level": "INFO"
}


**Volume mounted** - config persists between runs

## Logging & Errors

**Written to:**
- Console (real-time progress)
- `<output>/spotdl.log` (full logs)

**Per-provider error files:**
| Provider | Error File |
|----------|------------|
| Spotify  | `.errors/errors-YYYYMMDDHHMMSS.txt` |
| YouTube  | `.errors/ytdlp-YYYYMMDDHHMMSS.txt` |
| SoundCloud | `.errors/scdl-YYYYMMDDHHMMSS.txt` |

## Development

**Optional tools:**
- `black` (formatter)
- `ruff` (linter)
- `mypy` (type checker)

**Run tests:**
pytest

## Roadmap

- Parallel downloads
- Progress bars/TUI
- CSV/JSON export of missing tracks
- Geo-restricted content handling

## License

MIT License - see LICENSE file for details.


