"""Multi-MP3: Download playlists from Spotify, YouTube, and SoundCloud."""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.coordinator import Coordinator
from src.utils import get_spotify_creds, setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-MP3 — Download playlists from Spotify, YouTube, and SoundCloud"
    )
    parser.add_argument(
        "input", nargs="?", type=Path, default=Path("links.txt"),
        help="Input file with links (default: links.txt)"
    )
    parser.add_argument(
        "output", nargs="?", type=Path, default=Path("downloads"),
        help="Output directory for downloads (default: downloads/)"
    )
    parser.add_argument(
        "-i", "--input-file", dest="input_file_override", type=Path,
        help="Override input file (alternative to positional arg)"
    )
    parser.add_argument(
        "-o", "--output-dir", dest="output_dir_override", type=Path,
        help="Override output directory (alternative to positional arg)"
    )
    parser.add_argument(
        "-p", "--parallel",
        action="store_true",
        help="Run providers and their links concurrently. Without it the whole "
             "run is sequential — one provider, one playlist and one track at "
             "a time, which is what keeps SoundCloud under its per-IP rate limit"
    )
    parser.add_argument(
        "--max-workers",
        type=int, default=4,
        help="Max parallel downloads when --parallel is set (default: 4)"
    )
    parser.add_argument(
        "--providers",
        nargs="+", choices=["spotify", "youtube", "soundcloud", "all"],
        default=["all"],
        help="Which providers to run (default: all)"
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Enable rich-based progress display"
    )
    parsed = parser.parse_args()

    # Allow -i/-o to override positional args
    if parsed.input_file_override:
        parsed.input = parsed.input_file_override
    if parsed.output_dir_override:
        parsed.output = parsed.output_dir_override

    return parsed


def main() -> None:
    args = parse_args()

    load_dotenv()

    logger = setup_logging(args.output)

    if not args.input.is_file():
        logger.error(f"❌ Input file not found: {args.input}")
        sys.exit(1)

    try:
        client_id, client_secret = get_spotify_creds(logger)
    except ValueError:
        sys.exit(1)

    providers = ["soundcloud", "youtube", "spotify"] if "all" in args.providers else args.providers

    coord = Coordinator(
        args.output, logger, client_id, client_secret,
        parallel=args.parallel, max_workers=args.max_workers,
        use_tui=args.tui,
    )
    exit_code = coord.process_all(args.input, providers)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
