import json
import os
import re
import subprocess
import sys
import logging
from pathlib import Path
from datetime import datetime
from typing import List
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyClientCredentials
from spotdl.utils import spotify
import urllib.request


SPOTIFY_PREFIX = "https://open.spotify.com/"

# Load environment variables from .env file
load_dotenv()

# Access CLIENTID and CLIENTSECRET from environment variables
CLIENT_ID = os.getenv("CLIENTID")
CLIENT_SECRET = os.getenv("CLIENTSECRET")
os.environ['SPOTIFY_CLIENT_ID'] = CLIENT_ID or ''
os.environ['SPOTIFY_CLIENT_SECRET'] = CLIENT_SECRET or ''

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
    song_url: str
    playlist_url: str
    error: str
    playlist: Playlist
    list_position: str

    def __init__(self, song_url: str, playlist_url: str = None, error: str = "", title: str = "", artists: List[str] = [], playlist: Playlist = None, list_position: str = ""):
        self.title = title
        self.artists = artists
        self.song_url = song_url
        self.playlist_url = playlist_url
        self.error = error
        if playlist == None: self.playlist = Playlist(playlist_url)
        else: self.playlist = playlist
        self.list_position = list_position


def cleanupscdlmetadata(outputdir: Path, logger) -> List[Song]:
    for infofile in outputdir.glob("*.info.json"):
        if infofile.name.endswith(".info.json"):
            logger.info(f"🗑️ Deleting root {infofile.name}")
            infofile.unlink(missing_ok=True)

    metadataroot = outputdir / '.metadata'
    metadataroot.mkdir(exist_ok=True)
    allmissingsongs = []  # Collect ALL missing across playlists
    for playlistdir in outputdir.iterdir():
        if not playlistdir.is_dir():
            continue
        infofiles = list(playlistdir.glob('*.info.json'))
        if not infofiles:
            continue
        playlistname = playlistdir.name  # e.g., "soundcloud-music"
        playlistjsonpath = metadataroot / f'{playlistname}.json'
        firstinfo = infofiles[0]
        firstinfo.rename(playlistjsonpath)
        try:
            with playlistjsonpath.open('r') as f:
                playlistdata = json.load(f)
        except Exception:
            playlistdata = {}
        expectedcount = playlistdata.get('playlist_count', len(infofiles))
        logger.info(f"{playlistname}: {expectedcount} expected")
        numbers = []
        padding = 0
        for p in playlistdir.iterdir():
            if p.suffix.lower() == '.mp3':
                match = re.match(r'(\d+)', p.stem)
                if match:
                    numstr = match.group(1)
                    numbers.append(int(numstr))
                    padding = max(padding, len(numstr))
        numbers.sort()
        missingnumbers = [n for n in range(1, expectedcount + 1) if n not in numbers]
        missingsongs = []
        for num in missingnumbers:
            numstr = f"{num:0{padding}d}"
            missingsongs.append(Song(
                songurl='', playlisturl='', error=f'Missing {numstr}',
                playlist=Playlist('', playlistname, expectedcount),
                listposition=numstr
            ))
        if missingsongs:
            logger.info(f"{len(missingsongs)} missing in {playlistname}")
            for song in missingsongs:
                logger.info(f"  {song.error}")
        else:
            logger.info(f"All {expectedcount} present: {playlistname}")
        allmissingsongs.extend(missingsongs)
        # Aggregate tracks from other .info.json
        songs = []
        for i, infofile in enumerate(infofiles[1:], 1):
            try:
                with infofile.open('r') as f:
                    trackdata = json.load(f)
                songs.append(trackdata)
            except Exception:
                pass
        playlistdata['songs'] = songs  # Or 'entries'
        with playlistjsonpath.open('w', encoding='utf-8') as f:
            json.dump(playlistdata, f, indent=2, ensure_ascii=False)
        # Move description if exists
        descfile = playlistdir / f'{playlistname}.description'
        if descfile.exists():
            descfile.rename(metadataroot / f'{playlistname}.txt')
        # Delete extra .info.json
        for infofile in infofiles[1:]:
            infofile.unlink(missing_ok=True)
        logger.info(f"{playlistname}.json: {len(songs)} songs")
    logger.info(f"Done! {len(allmissingsongs)} total missing")
    return allmissingsongs



def cleanup_ytdlp_metadata(output_dir: Path, logger: logging.Logger) -> list[Song]:
    """
    Cleanup + return missing Song objects (like check_missing_tracks)
    """
    metadata_root = output_dir / ".metadata"
    metadata_root.mkdir(exist_ok=True)
    
    all_missing_songs = []  # Collect ALL missing across playlists

    for playlist_dir in output_dir.iterdir():
        if not playlist_dir.is_dir():
            continue

        info_files = list(playlist_dir.glob("*.info.json"))
        if not info_files:
            continue

        playlist_name = playlist_dir.name
        playlist_json_path = metadata_root / f"{playlist_name}.json"

        # Move FIRST .info.json
        first_info = info_files[0]
        first_info.rename(playlist_json_path)

        # Load playlist data
        try:
            with playlist_json_path.open("r") as f:
                playlist_data = json.load(f)
        except Exception:
            playlist_data = {}

        expected_count = playlist_data.get("playlist_count", len(info_files))
        logger.info(f"📊 {playlist_name}: {expected_count} expected")

        # Scan MP3s for numbers/padding (like check_missing_tracks)
        numbers = []
        padding = 0
        for p in playlist_dir.iterdir():
            if p.is_file() and p.suffix.lower() == '.mp3':
                match = re.match(r'^\s*(\d+)', p.stem)
                if match:
                    num_str = match.group(1)
                    numbers.append(int(num_str))
                    padding = max(padding, len(num_str))

        numbers.sort()
        missing_numbers = [n for n in range(1, expected_count + 1) if n not in numbers]

        # Create missing Song objects (like check_missing_tracks)
        missing_songs = []
        for num in missing_numbers:
            # Try to get track info from songs[] or playlist_data
            
            num_str = f"{num:0{padding}d}"
            missing_songs.append(Song(
                song_url="",  # YouTube: use playlist_data.webpage_url if needed
                playlist_url=playlist_data.get("webpage_url", ""),
                error=f"Missing {num_str}",
                playlist=Playlist(
                    playlist_url=playlist_data.get("webpage_url", ""), 
                    name=playlist_name, 
                    length=expected_count
                ),
                list_position=num_str
            ))

        # Log missing
        if missing_songs:
            logger.info(f"⚠️ {len(missing_songs)} missing in {playlist_name}:")
            for song in missing_songs:
                logger.info(f"  🚫 {song.error}")
        else:
            logger.info(f"✅ All {expected_count} present: {playlist_name}")

        all_missing_songs.extend(missing_songs)

        # Load songs array from remaining .info.json
        songs = []
        for i, info_file in enumerate(info_files[1:], 1):
            try:
                with info_file.open("r") as f:
                    track_data = json.load(f)
                songs.append(track_data)
            except Exception:
                pass

        playlist_data["songs"] = songs

        # Save final file
        with playlist_json_path.open("w", encoding="utf-8") as f:
            json.dump(playlist_data, f, indent=2, ensure_ascii=False)

        # Move description
        desc_file = playlist_dir / f"{playlist_name}.description"
        if desc_file.is_file():
            desc_file.rename(metadata_root / f"{playlist_name}.txt")

        # Cleanup
        for info_file in info_files[1:]:
            info_file.unlink(missing_ok=True)

        logger.info(f"✅ {playlist_name}.json: {len(songs)} songs")

    logger.info(f"🧹 Done! {len(all_missing_songs)} total missing")
    return all_missing_songs  # ✅ Return Song objects!


def check_missing_tracks_with_metadata_spotify(playlist_url: str, playlist_name: str, output_dir: Path, logger: logging.Logger):
    """
    Use METADATA total count (not files) as expected_count.
    """
    playlist_dir = output_dir / playlist_name
    metadata_path = output_dir / ".metadata" / f"{playlist_name}.json"
    
    if not metadata_path.is_file():
        logger.info(f"📄 No metadata for: {playlist_name}")
        return []
    
    if not playlist_dir.is_dir():
        logger.info(f"📁 No playlist dir: {playlist_name}")
        return []

    # Load metadata FIRST for expected_count
    try:
        with metadata_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)
        tracks = meta.get("tracks", {}).get("items", [])
        expected_count = len(tracks)  # Use metadata total!
        logger.info(f"📊 Metadata shows {expected_count} tracks expected")
    except Exception as e:
        logger.error(f"❌ Failed to load metadata: {e}")
        return []

    # Extract numbers + padding from EXISTING files
    numbers = []
    padding = 0
    for p in playlist_dir.iterdir():
        if p.is_file() and p.suffix.lower() in ('.mp3', '.flac', '.m4a'):
            match = re.match(r'^\s*(\d+)', p.stem)
            if match:
                num_str = match.group(1)
                numbers.append(int(num_str))
                padding = max(padding, len(num_str))

    if not numbers:
        logger.info(f"ℹ️ No numbered files in: {playlist_name}")
        return []

    numbers.sort()
    missing_numbers = [n for n in range(1, expected_count + 1) if n not in numbers]

    if not missing_numbers:
        logger.info(f"✅ All {expected_count} tracks present in: {playlist_name}")
        return []

    # Create Song objects for missing tracks
    missing_songs = []
    for num in missing_numbers:
        if num - 1 < len(tracks):
            track_item = tracks[num - 1]
            track = track_item.get("track") or track_item
            title = track.get("name", "").strip()
            artists = [a.get("name", "") for a in track.get("artists", [])]
            song_url = track.get("external_urls", {}).get("spotify", "")
            
            num_str = f"{num:0{padding}d}"
            
            missing_songs.append(Song(
                song_url=song_url,
                playlist_url="",
                error=f"Missing {num_str}",
                title=title,
                artists=artists,
                playlist=Playlist(playlist_url=playlist_url, name=playlist_name, length=expected_count),
                list_position=num_str
            ))

    logger.info(f"⚠️ {len(missing_songs)} missing tracks in {playlist_name} (expected {expected_count}, padding={padding}):")
    for song in missing_songs:
        logger.info(f"  🚫 {song.error} {song.title} - {', '.join(song.artists)}")
        logger.info(f"     {song.song_url}")

    return missing_songs


def getImage(url: str, output_dir: Path, logger: logging.Logger):  # Add output_dir param
    client_credentials_manager = SpotifyClientCredentials(
        client_id=CLIENT_ID, 
        client_secret=CLIENT_SECRET
    )
    session = spotify.Spotify(client_credentials_manager=client_credentials_manager)
    
    # Dynamic .icons folder inside output_dir (e.g. /app/music/.icons)
    icons_dir = output_dir / ".icons"
    icons_dir.mkdir(parents=True, exist_ok=True)
    
    
    if "album" in url: 
        out = session.album(url)
        type = "album"
    elif "playlist" in url: 
        out = session.playlist(url)
        type = "playlist"
    elif "artist" in url: 
        out = session.artist(url)
        type = "artist"
    elif "track" in url:
        out = session.track(url)
        type = "track"
    else:
        logger.info(f"❌ Unknown type: {type}")
        return None
    
    metadata_dir = output_dir / ".metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    # Save FULL metadata as JSON
    safe_name = "".join(c for c in out['name'] if c.isalnum() or c in (' ', '-', '_')).rstrip()
    json_path = metadata_dir / f"{safe_name}.json"
    
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    
    logger.info(f"📄 Metadata JSON saved: {json_path}")
    
    try:
        # Sanitize filename (no invalid chars)
        safe_name = "".join(c for c in out['name'] if c.isalnum() or c in (' ', '-', '_')).rstrip()
        image_path = icons_dir / f"{safe_name}.jpg"
        
        logger.info(f"🖼️  Downloading: {out['images'][0]['url']} → {image_path}")
        urllib.request.urlretrieve(out['images'][0]["url"], image_path)
        logger.info(f"✅ Saved: {image_path}")
        
    except Exception as e:
        logger.info(f"❌ Image failed: {e}")
    
    return out['name']

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
    """Spotify + SoundCloud + YouTube (markdown + plain)."""
    if line.startswith("#"):
        return ""
    line = line.strip()
    if not line:
        return ""
    
    # YouTube markdown [text](youtube_url)
    m_yt_md = re.search(r"\((https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s\)\]]+)\)", line)
    if m_yt_md:
        return m_yt_md.group(1)
    
    # YouTube plain URLs
    m_yt = re.search(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s\)\]]+", line)
    if m_yt:
        return m_yt.group(0)
    
    # SoundCloud
    m_sc = re.search(r"https?://(?:www\.)?soundcloud\.com/[^\s\)\]]+", line)
    if m_sc:
        return m_sc.group(0)
    
    # Spotify markdown [text](song_url)
    m_sp_md = re.search(r"\((https?://(?:open\.)?spotify\.com/[^\s\)\]]+)\)", line)
    if m_sp_md:
        return m_sp_md.group(1)
    
    # Spotify plain
    if line.startswith(SPOTIFY_PREFIX):
        return line
    
    return ""


def read_links(input_path: Path, logger: logging.Logger) -> list[str]:
    """Read ALL links (Spotify + SoundCloud) from input file."""
    links = []
    spotify_links = []
    soundcloud_links = []
    youtube_links = []
    
    try:
        with input_path.open("r", encoding="utf-8") as f:
            for raw in f:
                url = clean_url(raw)
                if url:
                    links.append(url)
                    
                    if "spotify.com" in url:
                        spotify_links.append(url)
                    elif "soundcloud.com" in url:
                        soundcloud_links.append(url)
                    elif "youtube.com" in url or "youtu.be" in url:
                        youtube_links.append(url)

        
        logger.info(f"✅ Parsed {len(links)} total links:")
        logger.info(f"   📀 Spotify: {len(spotify_links)}")
        logger.info(f"   🔊 SoundCloud: {len(soundcloud_links)}")
        logger.info(f"   📺 YouTube: {len(youtube_links)}")
        
        for i, link in enumerate(links, 1):
            kind = "📀 Spotify" if "spotify.com" in link else "🔊 SoundCloud" if "soundcloud.com" in link else "📺 YouTube"
            logger.info(f"   {i}. {kind} {link.split('?')[0]}")
        
        return {
            "all": links,
            "spotify": spotify_links,
            "soundcloud": soundcloud_links,
            "youtube": youtube_links
        }
    except Exception as e:
        logger.error(f"❌ Failed to read links: {e}")
        return {"all": [], "spotify": [], "soundcloud": [], "youtube": []}

def run_spotdl_for_link(link: str, output_dir: Path, logger: logging.Logger) -> tuple[int, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    
    errors_dir = output_dir / ".errors"
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
    

def run_scdl_for_link(link: str, output_dir: Path, logger: logging.Logger) -> tuple[int, Path]:
    """
    Download SoundCloud link using scdl CLI (like spotdl).
    Files: "01 Artist - Title.mp3" in playlist folder.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    errors_dir = output_dir / ".errors"
    errors_dir.mkdir(parents=True, exist_ok=True)
    errors_file = errors_dir / f"scdl-{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"
    
    cmd = [
        "scdl",
        "-l", link,  # URL (playlist/user/track)
        "--path", str(output_dir),  # Use a custom path for downloaded files
        "--no-playlist-folder",  # Download playlist tracks into main directory, instead of making a playlist subfolder
        "--playlist-name-format", "%(playlist)s/%(playlist_index)04d %(uploader)s - %(title)s.%(ext)s",  # Specify the downloaded file name format, if it is being downloaded as part of a playlist
        "--onlymp3",  # Download only mp3 files
        "--original-art",  # Download original cover art, not just 500x500 JPEG
        "-c",  # Continue if a downloaded file already exists
        "--debug",  # Set log level to DEBUG
        '--yt-dlp-args', '--write-info-json --ignore-errors --no-abort-on-error',  # Pass additional arguments to yt-dlp
    ]
    
    logger.info(f"🔊 scdl Downloading: {link.split('?')[0]}")
    logger.info(f"📁 Output: {output_dir}")
    logger.info(f"Command: {' '.join(cmd)}")
    
    env = os.environ.copy()
    try:
        proc = subprocess.run(
            cmd, 
            env=env, 
            cwd=str(output_dir),
            capture_output=False, 
            text=True, 
            timeout=3600
        )
        if proc.returncode == 0 and proc.stdout:
            errors_file.write_text(proc.stdout)
            logger.info("✅ scdl complete")
        else:
            logger.info(f"⚠️ scdl exit code: {proc.returncode}")
        return proc.returncode, errors_file
        
    except subprocess.TimeoutExpired:
        logger.info("⏰ scdl timed out (1h)")
        return 1, errors_file
    except Exception as e:
        logger.error(f"💥 scdl error: {e}")
        return 1, errors_file


def run_ytdlp_for_link(link: str, output_dir: Path, logger: logging.Logger) -> tuple[int, Path]:
    """
    Download YouTube playlist/channel using yt-dlp (like spotdl/scdl).
    Files: "01 Artist - Title.mp3" in playlist folder.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    errors_dir = output_dir / ".errors"
    errors_dir.mkdir(parents=True, exist_ok=True)
    errors_file = errors_dir / f"ytdlp-{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"
    
    cmd = [
        "yt-dlp",
        "-t", "mp3",           # Audio → MP3
        "--yes-playlist",                         # Full playlist
        "--ignore-errors",                        # Skip bad videos
        "--no-abort-on-error",                    # Continue on errors
        "--embed-thumbnail",                      # Embed cover
        "--write-info-json",                      # Save metadata JSON
        "--add-metadata",                         # Title/uploader metadata
        "--audio-quality", "1",                   # Best audio
        # "--no-overwrites",                      # Don't overwrite existing files
        #need to find a way to skip downloaded song
        f"--output", "%(playlist_title)s/%(playlist_index)02d %(uploader)s - %(title)s.%(ext)s",             # "Playlist/01 Uploader - Title.mp3"
        link,
    ]

    logger.info(f"📺 yt-dlp: {link.split('?')[0]}")
    logger.info(f"📁 → {output_dir}")
    logger.info(f"Command: {' '.join(cmd)}")
    
    env = os.environ.copy()
    try:
        proc = subprocess.run(cmd, env=env, cwd=str(output_dir), 
                            capture_output=False, text=True, timeout=3600*2)
        if proc.returncode == 0 and proc.stdout:
            errors_file.write_text(proc.stdout)
            logger.info("✅ yt-dlp complete")
        else:
            logger.info(f"⚠️ yt-dlp exit: {proc.returncode}")
        return proc.returncode, errors_file
    except subprocess.TimeoutExpired:
        logger.error("⏰ yt-dlp timeout (2h)")
        return 1, errors_file
    except Exception as e:
        logger.error(f"💥 yt-dlp: {e}")
        return 1, errors_file


def parse_errors(errors_file: Path, logger: logging.Logger, playlist_url: str) -> List[Song]:
    """Parse spotdl errors file for failed songs."""
    failed_songs = []
        
    try:
        with errors_file.open("r", encoding="utf-8") as ef:
            for line in ef:
                line = line.strip()
                if not line or not line.startswith('https://open.spotify.com/track/'):
                    continue
                
                #    def __init__(self, song_url: str, playlist_url: str = None, error: str = "", title: str = "", artists: List[str] = [], playlist: Playlist = None, list_position: str = ""):
                #https://open.spotify.com/track/6bFeIzkzsU45auYW1UUa47 - LookupError: No results found for song: NOTION - Dreams
                if ' - LookupError: No results found for song:' in line:
                    song_link = line.split(' - LookupError: No results found for song:', 1)[0]
                    artists = line.split(' - LookupError: No results found for song:', 1)[1].split(' - ')[0]
                    title = line.split(' - LookupError: No results found for song:', 1)[1].split(' - ')[1]
                    failed_songs.append(Song(song_url=song_link.strip(), playlist_url=playlist_url, error = "LookupError: No results found", title=title.strip(), artists=[a.strip() for a in artists.split(',')]))
                    continue
                
                #https://open.spotify.com/track/2ZXsTQ8d1c75zMEJH0uj1R - KeyError: 'webCommandMetadata'
                if " - KeyError: 'webCommandMetadata'" in line:
                    song_link = line.split(' - KeyError:', 1)[0]
                    failed_songs.append(Song(song_url=song_link.strip(), playlist_url=playlist_url, error = f"KeyError: 'webCommandMetadata'"))
                    continue

                #https://open.spotify.com/track/0PBQS0GycsYJ4yJJRjAIXU - AudioProviderError: YT-DLP download error - https://music.youtube.com/watch?v=ceXJTfuie6k
                if " - AudioProviderError: YT-DLP download error - " in line:
                    song_link = line.split(' - AudioProviderError: YT-DLP download error - ', 1)[0]
                    failed_songs.append(Song(song_url=song_link.strip(), playlist_url=playlist_url, error = "AudioProviderError: YT-DLP download error"))
                    continue
    
    except Exception as e:
        logger.error(f"Failed to parse errors: {e}")
    
    return failed_songs

def spotify_main(input_file: Path, output_dir: Path, logger: logging.Logger):
    logger.info(f"🔑 Exported SPOTIFY_CLIENT_ID: {CLIENT_ID[:8]}...")
    logger.info(f"🔑 Exported SPOTIFY_CLIENT_SECRET: {CLIENT_SECRET[:8]}...")

    links = read_links(input_file, logger).get("spotify", [])
    if not links:
        logger.info("❌ No Spotify playlist links found")
        return 0

    logger.info(f"🎯 Starting {len(links)} playlists...")
    
    exit_code = 0
    for i, link in enumerate(links, 1):
        logger.info(f"\n{'='*60}")
        name = getImage(link, output_dir, logger)
        logger.info(f"Spotify [{i}/{len(links)}] Processing playlist... {name}")
        code, errors_file = run_spotdl_for_link(link, output_dir, logger)
        if code != 0:
            exit_code = code
            logger.info(f"Playlist {i} failed (code {code})")

        check_missing_tracks_with_metadata_spotify(link, name, output_dir, logger)

        # logger.info(errors_file)
        # if errors_file.is_file():
        #     failed_songs = parse_errors(errors_file, logger, link)
        #     if failed_songs:
        #         logger.info(f"🔍 {len(failed_songs)} errors found in playlist - {link}:")
        #         for song in failed_songs:
        #             logger.info(f"  ❌ {song.song_url} - {song.error} - {song.title} - {', '.join(song.artists)}")
        #     else:
        #         logger.info("✅ No lookup errors found")

    logger.info(f"\n🎉 Complete! Final exit code: {exit_code}")
    logger.info(f"📁 Files in: {output_dir}")
    return exit_code

def soundcloud_main(input_file: Path, output_dir: Path, logger: logging.Logger):
    # Placeholder for SoundCloud processing logic
    logger.info("🔊 SoundCloud processing is not implemented yet.")

    links = read_links(input_file, logger).get("soundcloud", [])
    if not links:
        logger.info("❌ No SoundCloud playlist links found")
        return 0
    
    logger.info(f"🎯 Starting {len(links)} playlists...")
    
    exit_code = 0
    for i, link in enumerate(links, 1):
        logger.info(f"\n{'='*60}")
        # name = getImage(link, output_dir, logger)
        logger.info(f"[SC {i}/{len(links)}] Processing playlist... ") #{name}
        code, errors_file = run_scdl_for_link(link, output_dir, logger)
        if code != 0:
            exit_code = code
            logger.info(f"Playlist {i} failed (code {code})")
        
        cleanupscdlmetadata(output_dir, logger)
        
        logger.info(errors_file)


    logger.info(f"\n🎉 Complete! Final exit code: {exit_code}")
    logger.info(f"📁 Files in: {output_dir}")
    return exit_code

def youtube_main(input_file: Path, output_dir: Path, logger: logging.Logger):
    # Placeholder for YouTube processing logic
    logger.info("📺 YouTube processing is not implemented yet.")
    
    links = read_links(input_file, logger).get("youtube", [])
    
    if not links:
        logger.info("ℹ️ No YouTube links found")
        return 0
    
    logger.info(f"📺 Starting {len(links)} YouTube playlists/channels...")
    
    exit_code = 0
    for i, link in enumerate(links, 1):
        logger.info(f"\n{'='*60}")
        # name = getImage(link, output_dir, logger)
        logger.info(f"[YT {i}/{len(links)}] Processing playlist... ") #{name}
        code, errors_file = run_ytdlp_for_link(link, output_dir, logger)
        if code != 0:
            exit_code = code
            
            logger.info(f"Playlist {i} failed (code {code})")
        
        cleanup_ytdlp_metadata(output_dir, logger)

        logger.info(f"Errors: {errors_file}")
        
    logger.info(f"\n🎉 YouTube complete! Exit: {exit_code}")
    return exit_code

    return 0

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
        
    soundcloud_exitcode = soundcloud_main(input_file, output_dir, logger)
    if soundcloud_exitcode != 0:
        logger.info(f"❌ Exiting with code {soundcloud_exitcode}")
        sys.exit(soundcloud_exitcode)

    youtube_exitcode = youtube_main(input_file, output_dir, logger)
    if youtube_exitcode != 0:
        logger.info(f"❌ Exiting with code {youtube_exitcode}")
        sys.exit(youtube_exitcode)

    spotify_exitcode = spotify_main(input_file, output_dir, logger)
    if spotify_exitcode != 0:
        logger.info(f"❌ Exiting with code {spotify_exitcode}")
        sys.exit(spotify_exitcode)

    sys.exit(0)

if __name__ == "__main__":
    main()


