import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Tuple
import logging
import urllib.request

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

    def download(self, link: str) -> Tuple[int, Path]:
        """Implement BaseDownloader.download: run spotdl."""
        errors_file = self.errors_dir / f"errors-spotdl-{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"
        
        output_template = self._use_correct_config(link)

        threads = os.getenv("SPOTDL_THREADS", "4")
        try:
            threads = str(max(1, int(threads)))
        except ValueError:
            threads = "4"

        self.logger.info(f"🧵 Using spotdl threads: {threads}")

        cmd = [
            "spotdl",
            "--save-errors", str(errors_file),
            "--client-id", self.client_credentials_manager.client_id,
            "--client-secret", self.client_credentials_manager.client_secret,
            "--threads", threads,
            "--output", output_template,
            # "--yt-dlp-args", "--write-info-json --write-playlist-metafiles --no-abort-on-error --ignore-errors",
            "download",
            link,
        ]
        name = "SpotDL"
        return self._download(name, link, cmd, _errors_file=errors_file)

        # self.logger.info(f"🎵 spotdl: {link.split('?')[0]}")
        # self.logger.debug(f"Command: {' '.join(cmd)}")
        
        # env = os.environ.copy()
        # try:
        #     proc = subprocess.run(cmd, env=env, cwd=str(self.output_dir), 
        #                           capture_output=False, text=True, timeout=3600)
        #     if proc.returncode == 0:
        #         self.logger.info("✅ spotdl complete")
        #     else:
        #         self.logger.warning(f"spotdl exit code: {proc.returncode}")
        #     return proc.returncode, errors_file
        # except subprocess.TimeoutExpired:
        #     self.logger.warning("⏰ spotdl timeout (1h)")
        #     return 1, errors_file
        # except Exception as e:
        #     self.logger.error(f"💥 spotdl error: {e}")
        #     return 1, errors_file

    def cleanup(self, playlist_name: str) -> List[Song]:
        """Scan for missing tracks using metadata (your check_missing_tracks logic)."""
        # Aggregate all missing across playlists in output_dir
        all_missing = []

        playlist_dir = self.output_dir / playlist_name

        if not playlist_dir.is_dir():
            self.logger.info(f"No playlist dir for cleanup: {playlist_name}")
            return []

        missing = self._find_missing_in_playlist(playlist_dir)
        all_missing.extend(missing)
        # if all_missing:
        #     self.logger.info(f"⚠️ Total {len(all_missing)} missing Spotify tracks")
        return all_missing

    def fetch_metadata_image(self, url: str) -> str | None:
        """Your getImage: fetch playlist/album image/metadata."""
        session = spotify.Spotify(client_credentials_manager=self.client_credentials_manager)
        
        if "playlist" in url:
            out = session.playlist(url)
        elif "album" in url:
            out = session.album(url)
        elif "artist" in url:
            out = session.artist(url)
        elif "track" in url:
            out = session.track(url)
        else:
            self.logger.warning(f"Unknown Spotify type in {url}")
            return None
        
        # Save metadata JSON and image (your logic)
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
            urllib.request.urlretrieve(out['images'][0]['url'], image_path)
            self.logger.info(f"🖼️ Image saved: {image_path}")
        except Exception as e:
            self.logger.warning(f"Image fetch failed: {e}")
        
        return out['name']

    def _use_correct_config(self, link: str) -> None:
        """
        Detect link type (playlist/album/track/artist) and update spotdl config.json
        output template accordingly.
        """

        # Determine type from URL
        if "playlist" in link:
            output_template = "{list-name}/{list-position} {title} - {artists}.{output-ext}"
        elif "album" in link:
            output_template = "{list-name}/{track-number} {title} - {artists}.{output-ext}"
        elif "artist" in link:
            output_template = "{list-name}/{title} - {artists}.{output-ext}"
        elif "track" in link:
            output_template = "{title}/{title} - {artists}.{output-ext}"
        else:
            self.logger.info(f"ℹ️ Unknown Spotify type for link: {link}")
            return  # do not touch config if type is unknown
        
        return output_template

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
        # numbers = []
        # padding = 0
        # for p in playlist_dir.iterdir():
        #     if p.suffix.lower() in ('.mp3', '.flac', '.m4a'):
        #         match = re.match(r'^\s*(\d+)', p.stem)
        #         if match:
        #             num = int(match.group(1))
        #             numbers.append(num)
        #             padding = max(padding, len(match.group(1)))
        
        # if not numbers:
        #     self.logger.info(f"ℹ️ No numbered files in: {playlist_name}")
        #     return []
    
        # numbers.sort()
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