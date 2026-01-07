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

class Song:
    def __init__(self, title: str, artists: List[str], spotify_url: str, playlist_url: str, error: str):
        self.title = title
        self.artists = artists
        self.spotify_url = spotify_url
        self.playlist_url = playlist_url
        self.error = error

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
        # 🎵 AUDIO FALLBACKS (tries in order until success)
        "--audio", "youtube-music", "youtube", "bandcamp", # [{youtube,youtube-music,slider-kz,soundcloud,bandcamp,piped} ...] The audio provider to use. You can provide more than one for fallback.

        # 📝 LYRICS FALLBACKS (always tries all, embeds best)
        # "--lyrics", "genius", "musixmatch", "azlyrics", "synced", #[{genius,musixmatch,azlyrics,synced} ...] The lyrics provider to use. You can provide more than one for fallback. Synced lyrics might not work correctly with some music players. For such cases it's better to use `--generate-lrc` option.
        # "--lyrics", "",  # EMPTY = disables all lyrics
        "--lyrics", "genius", #[{genius,musixmatch,azlyrics,synced} ...] The lyrics provider to use. You can provide more than one for fallback. Synced lyrics might not work correctly with some music players. For such cases it's better to use `--generate-lrc` option.

        # 🎛️ Core settings
        #lower bitrate to reduce file size
        "--bitrate", "256k", #{auto,disable,8k,16k,24k,32k,40k,48k,64k,80k,96k,112k,128k,160k,192k,224k,256k,320k,0,1,2,3,4,5,6,7,8,9} The constant/variable bitrate to use for the output file. Values from 0 to 9 are variable bitrates. Auto will use the bitrate of the original file. Disable will disable the bitrate option. (In case of m4a and opus files, auto and disable will skip the conversion)
        "--format", "mp3", #{mp3,flac,ogg,opus,m4a,wav} The format to download the song in.
        "--threads", "4", #The number of threads to use when downloading songs.
        "--overwrite", "skip", #{skip,metadata,force} How to handle existing/duplicate files. (When combined with --scan-for-songs force will remove all duplicates, and metadata will only apply metadata to the latest song and will remove the rest. )
        "--print-errors", #Print errors (wrong songs, failed downloads etc) on exit, useful for long playlist
        "--save-errors", str(errors_file), # Save errors (wrong songs, failed downloads etc) to a file
        "--max-retries", "5", #The maximum number of retries to perform when getting metadata.
        "--preload", #Preload the download url to speed up the download process.
        "--scan-for-songs", #Scan the output directory for existing files. This option should be combined with the --overwrite option to control how existing files are handled. (Output directory is the last directory that is not a template variable in the output template)


        # 📁 Clean output
        "--output", "{list-name}/{list-position} {title} - {artists}.{output-ext}", #Specify the downloaded file name format, available variables: {title}, {artists}, {artist}, {album}, {album-artist}, {genre}, {disc-number}, {disc-count}, {duration}, {year}, {original-date}, {track-number}, {tracks-count}, {isrc}, {track-id}, {publisher}, {list-length}, {list-position}, {list-name}, {output-ext}
        
        #🐛 Debugging
        "--log-level", "DEBUG",  #{CRITICAL,FATAL,ERROR,WARN,WARNING,INFO,MATCH,DEBUG,NOTSET} Select log level.

        #other
        # "--genius-access-token", "GENIUS_TOKEN", #Lets you choose your own Genius access token.
        # "--config", #Use the config file to download songs. It's located under C:\Users\user\.spotdl\config.json or ~/.spotdl/config.json under linux
        # "--search-query", "SEARCH_QUERY", #The search query to use, available variables: {title}, {artists}, {artist}, {album}, {album-artist}, {genre}, {disc-number}, {disc-count}, {duration}, {year}, {original-date}, {track-number}, {tracks-count}, {isrc}, {track-id}, {publisher}, {list-length}, {list-position}, {list-name}, {output-ext}
        # "--dont-filter-results", #Disable filtering results.
        # "--album-type", "{single,album,compilation}", #Type of the album to search for. (album, single, compilation)
        # "--only-verified-results", #Use only verified results. (Not all providers support this)
        # "--user-auth", #Login to Spotify using OAuth.
        "--client-id", CLIENT_ID, #The client id to use when logging in to Spotify.
        "--client-secret", CLIENT_SECRET, #The client secret to use when logging in to Spotify.
        # "--auth-token", "AUTH_TOKEN", #The authorization token to use directly to log in to Spotify.
        # "--cache-path", "CACHE_PATH", #The path where spotipy cache file will be stored.
        # "--no-cache", #Disable caching (both requests and token).
        # "--headless", #Run in headless mode.
        # "--use-cache-file", #Use the cache file to get metadata. It's located under C:\Users\user\.spotdl\.spotify_cache or ~/.spotdl/.spotify_cache under linux. It only caches tracks and gets updated whenever spotDL gets metadata from Spotify. (It may provide outdated metadata use with caution)
        # "--ffmpeg", "FFMPEG", #The ffmpeg executable to use.
        # "--ffmpeg-args", "FFMPEG_ARGS", #Additional ffmpeg arguments passed as a string.
        # "--save-file", "SAVE_FILE", #The file to save/load the songs data from/to. It has to end with .spotdl. If combined with the download operation, it will save the songs data to the file. Required for save/sync (use - to print to stdout when using save).
        # "--m3u", "M3U", #Name of the m3u file to save the songs to. Defaults to {list[0]}.m3u8 If you want to generate a m3u for each list in the query use {list}, If you want to generate a m3u file based on the first list in the query use {list[0]}, (0 is the first list in the query, 1 is the second, etc. songs don't count towards the list number)
        # "--cookie-file", "COOKIE_FILE", #Path to cookies file.
        # "--restrict", "{strict,ascii,none}", #[{strict,ascii,none}] Restrict filenames to a sanitized set of characters for better compatibility
        # "--sponsor-block", #Use the sponsor block to download songs from yt/ytm.
        # "--archive", "ARCHIVE", #Specify the file name for an archive of already downloaded songs
        # "--playlist-numbering", #Sets each track in a playlist to have the playlist's name as its album, and album art as the playlist's icon
        # "--playlist-retain-track-cover", #Sets each track in a playlist to have the playlist's name as its album, while retaining album art of each track
        # "--fetch-albums", #Fetch all albums from songs in query
        # "--id3-separator ID3_SEPARATOR", #Change the separator used in the id3 tags. Only supported for mp3 files.
        # "--ytm-data", #Use ytm data instead of spotify data when downloading using ytm link.
        # "--add-unavailable", #Add unavailable songs to the m3u/archive files when downloading
        # "--generate-lrc", #Generate lrc files for downloaded songs. Requires `synced` provider to be present in the lyrics providers list.
        # "--force-update-metadata", #Force update metadata for songs that already have metadata.
        # "--sync-without-deleting", #Sync without deleting songs that are not in the query.
        # "--max-filename-length", "MAX_FILENAME_LENGTH", #Max file name length. (This won't override the max file name length enforced by the OS)
        # "--yt-dlp-args", "YT_DLP_ARGS", #Arguments to pass to yt-dlp
        # "--detect-formats", "[{mp3,flac,ogg,opus,m4a,wav} ...]", #Detect already downloaded songs with file format different from the --format option (When combined with --m3u option, only first detected format will be added to m3u file)
        # "--redownload", #to redownload the local song in diffrent format using --format for meta operation
        # "--skip-album-art", #skip downloading album art for meta operation
        # "--ignore-albums", "[IGNORE_ALBUMS ...]", #ignores the song of the given albums
        # "--skip-explicit", #Skip explicit songs
        # "--proxy", "PROXY", #Http(s) proxy server for download song. Example: http://host:port
        # "--create-skip-file", #Create skip file for successfully downloaded file
        # "--respect-skip-file", #If a file with the extension .skip exists, skip download
        # "--sync-remove-lrc", #Remove lrc files when using sync operation when downloading songs
        # "--host", "HOST", #The host to use for the web server.
        # "--port", "PORT", #The port to run the web server on.
        # "--keep-alive", #Keep the web server alive even when no clients are connected.
        # "--allowed-origins", "[ALLOWED_ORIGINS ...]", #The allowed origins for the web server.
        # "--web-use-output-dir", #Use the output directory instead of the session directory for downloads. (This might cause issues if you have multiple users using the web-ui at the same time)
        # "--keep-sessions", #Keep the session directory after the web server is closed.
        # "--force-update-gui", #Refresh the web server directory with a fresh git checkout
        # "--web-gui-repo", "WEB_GUI_REPO", #Custom web gui repo to use for the web server. Example: https://github.com/spotdl/web-ui/tree/master/dist
        # "--web-gui-location", "WEB_GUI_LOCATION", #Path to the web gui directory to use for the web server.
        # "--enable-tls", #Enable TLS on the web server.
        # "--cert-file", "CERT_FILE", #File Path to the TLS Certificate (PEM format).
        # "--key-file", "KEY_FILE", #File Path to the TLS Private Key (PEM format).
        # "--ca-file", "CA_FILE", #File Path to the TLS Certificate Authority File (PEM format).
        # "--simple-tui", #Use a simple tui.
        # "--log-format", "LOG_FORMAT", #Custom logging format to use. More info: https://docs.python.org/3/library/logging.html#logrecord-attributes
        # "--download-ffmpeg", #Download ffmpeg to spotdl directory.
        # "--download-config", #Generate a config file. This will overwrite current config if present.
        # "--check-for-updates", #Check for new version.
        # "--profile", #Run in profile mode. Useful for debugging.
        # "--version", #Show the version number and exit.

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
    
    spotify_url_pattern = re.compile(r'https://open\.spotify\.com/track/[^\s\)]+')
    
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
                logger.info(f"🔍 {len(failed_songs)} failed songs found in playlist - {failed_songs[0].playlist_url}:")
                for song in failed_songs:
                    logger.info(f"  ❌ {song.spotify_url} - {song.error} - {song.title} - {', '.join(song.artists)}")
            else:
                logger.info("✅ No lookup errors found")

    logger.info(f"\n🎉 Complete! Final exit code: {exit_code}")
    logger.info(f"📁 Files in: {output_dir}")
    sys.exit(exit_code)

if __name__ == "__main__":
    main()


