import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple
import logging
from urllib.request import urlopen

import spotipy as spotipy_lib
from spotipy.oauth2 import SpotifyClientCredentials
from spotdl.utils import spotify

from src.downloaders.base import BaseDownloader
from src.models import Song, Playlist


class SpotifyDownloader(BaseDownloader):

    def __init__(self, output_dir: Path, logger: logging.Logger, client_id: str, client_secret: str):
        super().__init__(output_dir, logger)
        self.client_credentials_manager = SpotifyClientCredentials(client_id, client_secret)
        os.environ['SPOTIFY_CLIENT_ID'] = client_id
        os.environ['SPOTIFY_CLIENT_SECRET'] = client_secret

    def download(self, link: str,
                 on_progress: Callable[[str, Dict[str, Any]], None] | None = None
                 ) -> Tuple[int, Path]:
        """Implement BaseDownloader.download: run spotdl."""
        errors_file = self.errors_dir / f"errors-spotdl-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.txt"
        
        output_template = self._use_correct_config(link)

        cmd = [
            "spotdl",
            "--save-errors", str(errors_file),
            "--client-id", self.client_credentials_manager.client_id,
            "--client-secret", self.client_credentials_manager.client_secret,
            "--output", output_template,
            # Rich's TUI wraps long lines inside a bordered box, which split
            # `Downloaded "Some Title"` across two lines and broke progress
            # parsing entirely. --simple-tui emits plain, unwrapped lines.
            "--simple-tui",
            "--log-level", "INFO",
            # spotdl drives its own bundled yt-dlp, which needs a JS runtime for
            # YouTube's challenge — without it every track fails with HTTP 403.
            "--yt-dlp-args", "--js-runtimes deno",
            "download",
            link,
        ]
        name = "SpotDL"
        return self._download(name, link, cmd, _errors_file=errors_file,
                              on_progress=on_progress)

    def cleanup(self, playlist_name: str) -> List[Song]:
        """Scan for missing tracks using Spotify metadata."""
        playlist_dir = self.output_dir / playlist_name

        if not playlist_dir.is_dir():
            self.logger.info(f"No playlist dir for cleanup: {playlist_name}")
            return []

        return self._find_missing_in_playlist(playlist_dir)

    def fetch_metadata_image(self, url: str) -> str | None:
        """Fetch playlist/album image and metadata."""
        session = spotify.Spotify(client_credentials_manager=self.client_credentials_manager)

        if "playlist" in url:
            out = session.playlist(url)
            self._paginate_tracks(out, url)
        elif "album" in url:
            out = session.album(url)
            self._paginate_tracks(out, url)
        elif "artist" in url:
            out = session.artist(url)
        elif "track" in url:
            out = session.track(url)
        else:
            self.logger.warning(f"Unknown Spotify type in {url}")
            return None

        # Save metadata JSON and image
        icons_dir = self.output_dir / ".icons"
        icons_dir.mkdir(parents=True, exist_ok=True)
        metadata_dir = self.output_dir / ".metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)

        safe_name = "".join(c for c in out['name'] if c.isalnum() or c in (' ', '-', '_')).rstrip()
        json_path = metadata_dir / f"{safe_name}.json"
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        self.logger.info(f"📄 Spotify metadata: {json_path}")

        try:
            image_path = icons_dir / f"{safe_name}.jpg"
            with urlopen(out['images'][0]['url']) as resp, open(image_path, 'wb') as f:
                f.write(resp.read())
            self.logger.info(f"🏾 Image saved: {image_path}")
        except Exception as e:
            self.logger.warning(f"Image fetch failed: {e}")

        return out['name']

    def _paginate_tracks(self, out: dict, url: str) -> None:
        """Handle Spotify API pagination — fetches all tracks beyond the 100-item limit."""
        tracks = out.get("tracks")
        if not tracks:
            return

        total = tracks.get("total", 0)
        items = tracks.get("items", [])
        if total <= len(items):
            return  # Already have all tracks

        self.logger.info(f"📋 Paginating {total} tracks (have {len(items)})...")

        # Extract the Spotify ID from the URL
        sp_id = url.rstrip("/").split("/")[-1].split("?")[0]
        sp = spotipy_lib.Spotify(client_credentials_manager=self.client_credentials_manager)

        # `market` is deliberately omitted: "from_token" requires a user token,
        # and with the client-credentials flow Spotify rejects it with HTTP 400.
        # That error used to abort the whole metadata fetch, leaving the UI with
        # no track list at all for any playlist over 100 tracks.
        fields = ("items(is_local,track(id,name,artists(name),track_number,"
                  "duration_ms,external_urls))," "total")

        is_playlist = "playlist" in url
        offset = len(items)
        while offset < total:
            try:
                if is_playlist:
                    page = sp.playlist_items(
                        sp_id, limit=100, offset=offset,
                        fields=fields, additional_types=("track",),
                    )
                else:
                    page = sp.album_tracks(sp_id, limit=50, offset=offset)
            except Exception as e:
                # Keep whatever we already have rather than losing everything
                self.logger.warning(
                    f"Pagination stopped at {len(items)}/{total}: {e}")
                break

            page_items = page.get("items", [])
            if not page_items:
                break
            items.extend(page_items)
            offset += len(page_items)

        tracks["items"] = items
        out["tracks"] = tracks
        if len(items) >= total:
            self.logger.info(f"📊 All {len(items)} tracks retrieved")
        else:
            self.logger.warning(f"📊 Retrieved {len(items)} of {total} tracks")

    def _use_correct_config(self, link: str) -> str:
        """
        Detect link type (playlist/album/track/artist) and return the
        appropriate spotdl output template.
        """
        if "playlist" in link:
            return "{list-name}/{list-position} {title} - {artists}.{output-ext}"
        elif "album" in link:
            return "{list-name}/{track-number} {title} - {artists}.{output-ext}"
        elif "artist" in link:
            return "{list-name}/{title} - {artists}.{output-ext}"
        elif "track" in link:
            return "{title}/{title} - {artists}.{output-ext}"
        else:
            raise ValueError(f"Unknown Spotify link type — cannot determine output template: {link}")

    def _find_missing_in_playlist(self, playlist_dir: Path) -> List[Song]:
        """Private: your check_missing_tracks_with_metadata_spotify."""
        playlist_name = playlist_dir.name
        metadata_path = self.output_dir / ".metadata" / f"{playlist_name}.json"
        
        if not metadata_path.is_file():
            self.logger.info(f"No metadata: {playlist_name}")
            return []
        
        try:
            with metadata_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            
            if meta.get("type") == "playlist" or meta.get("type") == "album":
                tracks = meta.get("tracks", {}).get("items", [])
            elif meta.get("type") == "artist":
                tracks = []
            elif meta.get("type") == "track":
                tracks = [meta]

            expected_count = len(tracks)
            self.logger.info(f"📊 Metadata shows {expected_count} tracks expected")
        except Exception as e:
            self.logger.error(f"Metadata load failed {playlist_name}: {e}")
            return []
        
        # Scan files for numbers/padding
        numbers, padding = self._get_padding(playlist_dir)
        missing_nums = [n for n in range(1, expected_count + 1) if n not in numbers]
        if not missing_nums:
            self.logger.info(f"✅ All {expected_count} tracks present in: {playlist_name}")
            return []
        
        missing_songs: list[Song] = []
        for num in missing_nums:
            if num - 1 < len(tracks):
                track = tracks[num - 1].get("track") or tracks[num - 1]
                title = track.get("name", "").strip()
                artists = [a.get("name", "") for a in track.get("artists", [])]
                song_url = track.get("external_urls", {}).get("spotify", "")
                num_str = f"{num:0{padding}d}"
                missing_songs.append(Song(
                    song_url=song_url, playlist_url="", error=f"Missing {num_str}",
                    title=title, artists=artists,
                    playlist=Playlist(playlist_url="", name=playlist_name, length=expected_count),
                    list_position=num_str
                ))
        
        self.logger.info(f"⚠️ {len(missing_songs)} missing tracks in {playlist_name} (expected {expected_count}, padding={padding}):")
        for song in missing_songs:
            self.logger.info(f"{song.song_url}  🚫 {song.error} {song.title} - {', '.join(song.artists)}")
        return missing_songs