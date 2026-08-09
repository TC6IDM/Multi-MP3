import logging
import os
from pathlib import Path
import re
import sys
from typing import Dict, List, Tuple



def setup_logging(output_dir: Path) -> logging.Logger:
    """Setup logging to console + file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "spotdl.log"

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(threadName)s | %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file)
        ]
    )
    logger = logging.getLogger(__name__)
    logger.info(f"🚀 Started - Logs: {log_file}")
    return logger


def clean_url(line: str) -> str:
    """Extract a Spotify, SoundCloud, or YouTube URL from a plain or markdown line."""
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

    # Spotify plain URLs
    m_sp_plain = re.search(r"https?://(?:open\.)?spotify\.com/[^\s\)\]]+", line)
    if m_sp_plain:
        return m_sp_plain.group(0)

    return ""


def read_links(input_path: Path, logger: logging.Logger) -> Dict[str, List[str]]:
    """Read and categorize all links from the input file by provider."""
    spotify_links: List[str] = []
    soundcloud_links: List[str] = []
    youtube_links: List[str] = []

    try:
        with input_path.open("r", encoding="utf-8") as f:
            for raw in f:
                url = clean_url(raw)
                if url:
                    if "spotify.com" in url:
                        spotify_links.append(url)
                    elif "soundcloud.com" in url:
                        soundcloud_links.append(url)
                    elif "youtube.com" in url or "youtu.be" in url:
                        youtube_links.append(url)

        total = len(spotify_links) + len(soundcloud_links) + len(youtube_links)
        logger.info(f"✅ Parsed {total} total links:")
        logger.info(f"   📀 Spotify: {len(spotify_links)}")
        logger.info(f"   🔊 SoundCloud: {len(soundcloud_links)}")
        logger.info(f"   📺 YouTube: {len(youtube_links)}")

        all_links = spotify_links + soundcloud_links + youtube_links
        for i, link in enumerate(all_links, 1):
            if "spotify.com" in link:
                kind = "📀 Spotify"
            elif "soundcloud.com" in link:
                kind = "🔊 SoundCloud"
            else:
                kind = "📺 YouTube"
            logger.info(f"   {i}. {kind} {link.split('?')[0]}")

        return {
            "spotify": spotify_links,
            "soundcloud": soundcloud_links,
            "youtube": youtube_links,
        }
    except Exception as e:
        logger.error(f"❌ Failed to read links: {e}")
        return {"spotify": [], "soundcloud": [], "youtube": []}


def get_spotify_creds(logger: logging.Logger) -> Tuple[str, str]:
    """Load and validate Spotify CLIENTID/CLIENTSECRET from .env or env vars."""
    client_id = os.getenv("CLIENTID")
    client_secret = os.getenv("CLIENTSECRET")

    if not client_id or not client_secret:
        logger.error("❌ Missing CLIENTID or CLIENTSECRET in .env")
        raise ValueError("Spotify credentials required")

    os.environ["SPOTIFY_CLIENT_ID"] = client_id
    os.environ["SPOTIFY_CLIENT_SECRET"] = client_secret
    logger.info("🔑 Spotify creds loaded")
    return client_id, client_secret
