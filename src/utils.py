import functools
import logging
import os
import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@functools.lru_cache(maxsize=1)
def deno_path() -> str | None:
    """The deno binary yt-dlp needs for YouTube's JS challenge.

    Prefers the copy the `deno` wheel installs into this venv, so a native run
    works without a separate install; falls back to whatever is on PATH.
    """
    try:
        import deno
        path = deno.find_deno_bin()
        if path and Path(path).is_file():
            return str(path)
    except Exception:
        pass
    return shutil.which("deno")


def subprocess_env() -> dict[str, str]:
    """Environment for child tools, with deno's directory on PATH.

    spotdl drives its own yt-dlp with `--js-runtimes deno`, which looks deno up
    on PATH — and a venv's Scripts directory is only on PATH when activated.
    """
    env = os.environ.copy()
    deno = deno_path()
    if deno:
        env["PATH"] = str(Path(deno).parent) + os.pathsep + env.get("PATH", "")
    return env


def spotdl_command() -> str | None:
    """Path to the spotdl executable, which lives in its own virtualenv.

    Every spotdl release pins fastapi<0.104, uvicorn<0.24 and rich<14, so it
    can't share the app's environment without freezing the web stack. Order:
    SPOTDL_BIN, then the project's .venv-spotdl, then PATH.
    """
    env_bin = os.getenv("SPOTDL_BIN", "").strip()
    if env_bin:
        return env_bin
    for candidate in (PROJECT_ROOT / ".venv-spotdl" / "Scripts" / "spotdl.exe",
                      PROJECT_ROOT / ".venv-spotdl" / "bin" / "spotdl"):
        if candidate.is_file():
            return str(candidate)
    return shutil.which("spotdl")


def spotdl_python() -> str | None:
    """The Python interpreter of spotdl's virtualenv, for spotdl_runner.py.

    Found next to the spotdl executable (Scripts\\python.exe / bin/python).
    SPOTDL_PYTHON overrides. None when spotdl wasn't installed into a venv of
    its own (pipx, a system package) — then spotdl runs plain, without the
    track-list handshake.
    """
    env_py = os.getenv("SPOTDL_PYTHON", "").strip()
    if env_py:
        return env_py
    spotdl = spotdl_command()
    if not spotdl:
        return None
    folder = Path(spotdl).parent
    for name in ("python.exe", "python3", "python"):
        candidate = folder / name
        if candidate.is_file():
            return str(candidate)
    return None


def setup_logging(output_dir: Path) -> logging.Logger:
    """Setup logging to console + file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "spotdl.log"

    # Piped or redirected on Windows, stdout falls back to cp1252, and every
    # emoji in the log ("✅", "⏳") became a logging error instead of a line.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(threadName)s | %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8")
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


def provider_for(url: str) -> str | None:
    """Which provider owns a URL, or None if it belongs to none of them.

    The single source of truth for this mapping — the web API needs the same
    answer as the CLI when it decides which links a job should even include.
    """
    if "spotify.com" in url:
        return "spotify"
    if "soundcloud.com" in url:
        return "soundcloud"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    return None


def read_links(input_path: Path, logger: logging.Logger) -> dict[str, list[str]]:
    """Read and categorize all links from the input file by provider."""
    spotify_links: list[str] = []
    soundcloud_links: list[str] = []
    youtube_links: list[str] = []
    buckets = {
        "spotify": spotify_links,
        "soundcloud": soundcloud_links,
        "youtube": youtube_links,
    }

    try:
        with input_path.open("r", encoding="utf-8") as f:
            for raw in f:
                url = clean_url(raw)
                if url:
                    bucket = buckets.get(provider_for(url) or "")
                    if bucket is not None:
                        bucket.append(url)

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


def get_spotify_creds(logger: logging.Logger) -> tuple[str, str]:
    """Spotify CLIENTID/CLIENTSECRET from .env or env vars — optional.

    spotdl is the primary Spotify source and reads Spotify without the
    developer API by default, so credentials are only passed through when
    present. (Spotify now refuses developer apps whose owner lacks Premium.)
    """
    client_id = os.getenv("CLIENTID", "").strip()
    client_secret = os.getenv("CLIENTSECRET", "").strip()
    if client_id and client_secret:
        logger.info("🔑 Spotify creds loaded (passed to spotdl)")
        return client_id, client_secret
    return "", ""
