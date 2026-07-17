import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Tuple
import logging
import re
import os

from src.downloaders.base import BaseDownloader
from src.models import Song, Playlist


class SoundCloudDownloader(BaseDownloader):
    def download(self, link: str) -> Tuple[int, Path]:
        """Download SoundCloud link via scdl CLI."""
        # errors_file = self.errors_dir / f"scdl-{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"
        
        cmd = [
            "scdl",
            "-l", link,
            "--path", str(self.output_dir),
            "--no-playlist-folder",
            "--playlist-name-format", "%(playlist)s/%(playlist_index)04d %(uploader)s - %(title)s.%(ext)s",
            "--onlymp3",
            "--original-art",
            "-c",
            "--debug",
            "--yt-dlp-args", "--write-info-json --ignore-errors --no-abort-on-error --yes-playlist --embed-thumbnail --audio-quality 1",
        ]

        name = "scdl"
        return self._download(name, link, cmd)

        # self.logger.info(f"🔊 scdl: {link.split('?')[0]}")
        # self.logger.debug(f"📁 Output: {self.output_dir}")
        # self.logger.debug(f"Command: {' '.join(cmd)}")
        
        # env = os.environ.copy()
        # try:
        #     proc = subprocess.run(cmd, env=env, cwd=str(self.output_dir),
        #         capture_output=False, text=True, timeout=3600)
        #     if proc.returncode == 0:
        #         if proc.stdout:
        #             errors_file.write_text(proc.stdout)
        #         self.logger.info("✅ scdl complete")
        #     else:
        #         self.logger.warning(f"scdl exit code: {proc.returncode}")
        #     return proc.returncode, errors_file
        # except subprocess.TimeoutExpired:
        #     self.logger.warning("⏰ scdl timeout (1h)")
        #     return 1, errors_file
        # except Exception as e:
        #     self.logger.error(f"💥 scdl error: {e}")
        #     return 1, errors_file

    def cleanup(self, playlist_name: str) -> List[Song]:
        """Cleanup metadata/scan missing tracks across playlists."""
        # Delete root .info.json
        for infofile in self.output_dir.glob("*.info.json"):
            self.logger.info(f"🗑️ Deleting root {infofile.name}")
            infofile.unlink(missing_ok=True)
        
        metadata_root = self.output_dir / '.metadata'
        metadata_root.mkdir(exist_ok=True)
        all_missing_songs = []
        
        for playlist_dir in self.output_dir.iterdir():
            if not playlist_dir.is_dir() or playlist_dir.name.startswith('.'):
                continue

            missing_songs = self._cleanup_playlist(playlist_dir, metadata_root)
            all_missing_songs.extend(missing_songs)
        
        self.logger.info(f"🧹 Done! {len(all_missing_songs)} total missing")
        return all_missing_songs

    def fetch_metadata_image(self, url: str) -> str | None:
        return "Test"
    
    def _cleanup_playlist(self, playlist_dir: Path, metadata_root: Path) -> List[Song]:
        """Private: cleanup one playlist dir."""
        playlist_name = playlist_dir.name
        info_files = list(playlist_dir.glob('*.info.json'))
        if not info_files:
            return []
        
        playlist_json_path = metadata_root / f'{playlist_name}.json'
        first_info = info_files[0]
        first_info.rename(playlist_json_path)
        
        try:
            with playlist_json_path.open('r') as f:
                playlist_data = json.load(f)
        except Exception:
            playlist_data = {}
        expected_count = playlist_data.get('playlist_count', len(info_files))
        self.logger.info(f"{playlist_name}: {expected_count} expected")
        
        # Scan MP3s for numbers/padding
        numbers, padding = self._get_padding(playlist_dir)

        # numbers = []
        # padding = 0
        # for p in playlist_dir.iterdir():
        #     if p.suffix.lower() == '.mp3':
        #         match = re.match(r'(\d+)', p.stem)
        #         if match:
        #             num_str = match.group(1)
        #             numbers.append(int(num_str))
        #             padding = max(padding, len(num_str))
        # numbers.sort()
        missing_numbers = [n for n in range(1, expected_count + 1) if n not in numbers]
        
        missing_songs: List[Song] = []
        for num in missing_numbers:
            num_str = f"{num:0{padding}d}"
            missing_songs.append(Song(
                song_url='', playlist_url='', error=f'Missing {num_str}',
                playlist=Playlist('', playlist_name, expected_count),
                list_position=num_str
            ))
        
        if missing_songs:
            self.logger.info(f"⚠️ {len(missing_songs)} missing in {playlist_name}:")
            for song in missing_songs:
                self.logger.info(f"  🚫 {song.error}")
        else:
            self.logger.info(f"✅ All {expected_count} present: {playlist_name}")
        
        # Aggregate remaining .info.json to songs[]
        songs = []
        for info_file in info_files[1:]:
            try:
                with info_file.open('r') as f:
                    track_data = json.load(f)
                songs.append(track_data)
            except Exception:
                pass
        playlist_data['songs'] = songs
        
        with playlist_json_path.open('w', encoding='utf-8') as f:
            json.dump(playlist_data, f, indent=2, ensure_ascii=False)
        
        # Move description
        desc_file = playlist_dir / f'{playlist_name}.description'
        if desc_file.exists():
            desc_file.rename(metadata_root / f'{playlist_name}.txt')
        
        # Delete extras
        for info_file in info_files[1:]:
            info_file.unlink(missing_ok=True)
        
        self.logger.info(f"{playlist_name}.json: {len(songs)} songs")
        return missing_songs
