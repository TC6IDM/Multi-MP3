import os
import re
import subprocess
import sys
import logging
from pathlib import Path
from datetime import datetime
from typing import List, Union
from dotenv import load_dotenv

SPOTIFY_PREFIX = "https://open.spotify.com/"

# Load environment variables from .env file
load_dotenv()

# Access CLIENTID and CLIENTSECRET from environment variables
CLIENT_ID = os.getenv("CLIENTID")
CLIENT_SECRET = os.getenv("CLIENTSECRET")

class Playlist:
    name: str
    playlist_urlplaylist_url: str
    length: int
    songs: List['Song']
    def __init__(self, playlist_url: str, name: str = "", length: int = 0, songs: List['Song'] = []):
        self.name = name
        self.playlist_url = playlist_url
        self.length = length
        self.songs = songs
        
class Song:
    title: str
    artists: List[str]
    spotify_url: str
    playlist_url: str
    error: str
    playlist: Playlist
    list_position: str

    def __init__(self, spotify_url: str, playlist_url: str, error: str, title: str = "", artists: List[str] = [], playlist: Playlist = None, list_position: str = ""):
        self.title = title
        self.artists = artists
        self.spotify_url = spotify_url
        self.playlist_url = playlist_url
        self.error = error
        if playlist == None: self.playlist = Playlist(playlist_url)
        else: self.playlist = playlist
        self.list_position = list_position

def setup_logging(output_dir: Path):
    """Setup logging to console + file"""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "spotdl.log"
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file)
        ]
    )
    logger = logging.getLogger(__name__)
    logger.info(f"🚀 Started - Logs: {log_file}")
    return logger

def clean_url(line: str) -> str:
    if line.startswith("#"):
        return ""
    line = line.strip()
    if not line:
        return ""
    # 1) Markdown [text](url) → capture url in ()
    m = re.match(r".*\((https?://open\.spotify\.com/[^\s)]+)\)\s*.*", line)
    if m:
        return m.group(1)
    # 2) Already plain spotify URL
    if line.startswith(SPOTIFY_PREFIX):
        return line
    return ""

def read_spotify_links(input_path: Path, logger: logging.Logger) -> list[str]:
    links = []
    try:
        with input_path.open("r", encoding="utf-8") as f:
            for raw in f:
                url = clean_url(raw)
                if url:
                    links.append(url)
        logger.info(f"✅ Parsed {len(links)} Spotify links")
        for i, link in enumerate(links, 1):
            logger.info(f"   {i}. {link.split('?')[0]}")
        return links
    except Exception as e:
        logger.info(f"❌ Failed to read links: {e}")
        return []

def run_spotdl_for_link(link: str, output_dir: Path, logger: logging.Logger) -> tuple[int, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    
    errors_dir = output_dir / "errors"
    errors_dir = Path(errors_dir)  # Ensure errors_dir is a Path object
    errors_dir.mkdir(parents=True, exist_ok=True)
    errors_file = f"{errors_dir}/errors-{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"

    cmd = [
        "spotdl",
        "--save-errors", str(errors_file), # Save errors (wrong songs, failed downloads etc) to a file
        "--client-id", CLIENT_ID, #The client id to use when logging in to Spotify.
        "--client-secret", CLIENT_SECRET, #The client secret to use when logging in to Spotify.
        "download",
        link,
    ]
    
    logger.info(f"🎵 Downloading: {link.split('?')[0]}")
    logger.info(f"Command: {' '.join(cmd)}")
    
    env = os.environ.copy()
    try:
        proc = subprocess.run(cmd, env=env, cwd=str(output_dir), 
                            capture_output=False, text=True, timeout=3600)
        if proc.returncode == 0:
            logger.info(f"✅ Playlist complete: {output_dir}")
        else:
            logger.info(f"⚠️  Playlist finished with code {proc.returncode}")
        return proc.returncode, Path(errors_file)
    except subprocess.TimeoutExpired:
        logger.info("⏰ Download timed out after 1 hour")
        return 1, Path(errors_file)
    except Exception as e:
        logger.info(f"💥 SpotDL error: {e}")
        return 1, Path(errors_file)

def parse_errors(errors_file: Path, logger: logging.Logger, playlist_url: str) -> List[Song]:
    """Parse spotdl errors file for failed songs."""
    failed_songs = []
        
    try:
        with errors_file.open("r", encoding="utf-8") as ef:
            for line in ef:
                line = line.strip()
                if not line or not line.startswith('https://open.spotify.com/track/'):
                    continue
                
                #https://open.spotify.com/track/6bFeIzkzsU45auYW1UUa47 - LookupError: No results found for song: NOTION - Dreams
                if ' - LookupError: No results found for song:' in line:
                    song_link = line.split(' - LookupError: No results found for song:', 1)[0]
                    artists = line.split(' - LookupError: No results found for song:', 1)[1].split(' - ')[0]
                    title = line.split(' - LookupError: No results found for song:', 1)[1].split(' - ')[1]
                    failed_songs.append(Song(title.strip(), [a.strip() for a in artists.split(',')], song_link.strip(), playlist_url, "LookupError: No results found"))
                    continue
                
                #https://open.spotify.com/track/2ZXsTQ8d1c75zMEJH0uj1R - KeyError: 'webCommandMetadata'
                if " - KeyError: 'webCommandMetadata'" in line:
                    song_link = line.split(' - KeyError:', 1)[0]
                    failed_songs.append(Song("Unknown Title", ["Unknown Artist"], song_link.strip(), playlist_url, f"KeyError: 'webCommandMetadata'"))
                    continue

                #https://open.spotify.com/track/0PBQS0GycsYJ4yJJRjAIXU - AudioProviderError: YT-DLP download error - https://music.youtube.com/watch?v=ceXJTfuie6k
                if " - AudioProviderError: YT-DLP download error - " in line:
                    song_link = line.split(' - AudioProviderError: YT-DLP download error - ', 1)[0]
                    failed_songs.append(Song("Unknown Title", ["Unknown Artist"], song_link.strip(), playlist_url, "AudioProviderError: YT-DLP download error"))
                    continue
    
    except Exception as e:
        logger.error(f"Failed to parse errors: {e}")
    
    return failed_songs

def main():
    if len(sys.argv) < 3:
        print("Usage: python main.py <input_file> <output_dir>")
        sys.exit(1)

    input_file = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])
    
    logger = setup_logging(output_dir)
    
    if not input_file.is_file():
        logger.info(f"❌ Input file not found: {input_file}")
        sys.exit(1)

    links = read_spotify_links(input_file, logger)
    if not links:
        logger.info("❌ No Spotify playlist links found")
        sys.exit(1)

    logger.info(f"🎯 Starting {len(links)} playlists...")
    
    exit_code = 0
    for i, link in enumerate(links, 1):
        logger.info(f"\n{'='*60}")
        logger.info(f"[{i}/{len(links)}] Processing playlist...")
        code, errors_file = run_spotdl_for_link(link, output_dir, logger)
        if code != 0:
            exit_code = code
            logger.info(f"Playlist {i} failed (code {code})")
        
        logger.info(errors_file)

        if errors_file.is_file():
            failed_songs = parse_errors(errors_file, logger, link)
            if failed_songs:
                logger.info(f"🔍 {len(failed_songs)} failed songs found in playlist - {link}:")
                for song in failed_songs:
                    logger.info(f"  ❌ {song.spotify_url} - {song.error} - {song.title} - {', '.join(song.artists)}")
            else:
                logger.info("✅ No lookup errors found")

    logger.info(f"\n🎉 Complete! Final exit code: {exit_code}")
    logger.info(f"📁 Files in: {output_dir}")
    sys.exit(exit_code)

if __name__ == "__main__":
    main()


