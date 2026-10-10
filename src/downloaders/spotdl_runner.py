"""Run spotdl, pausing once to tell Multi-MP3 what the playlist contains.

This file is executed by the *spotdl* virtualenv's Python, not the app's, so it
must not import anything from `src`. It is spotdl's own console entry point
with one hook: right after spotdl has read the playlist from Spotify, the full
song list is printed as a single marker line, and the app answers on stdin
with the URLs it already has. spotdl then downloads only the rest.

That gives the app an exact per-track record for Spotify playlists without
Spotify's developer API (which now refuses apps whose owner lacks Premium),
and without a second, slow `spotdl save` pass to learn the track list.

Protocol:
    stdout  @@MULTIMP3_SONGS@@ [{"url": ..., "name": ..., ...}, ...]
    stdin   {"skip": ["https://open.spotify.com/track/...", ...]}
"""

import json
import os
import sys

# This file sits next to the app's soundcloud.py, youtube.py, … — and spotdl
# imports a library called `soundcloud`. Never let this folder shadow it, even
# if someone runs the script without -I.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]

MARKER = "@@MULTIMP3_SONGS@@"
FIELDS = ("url", "song_id", "name", "artists", "duration", "album_name", "track_number",
          "disc_number", "list_name", "list_url", "list_position", "list_length", "cover_url")


def main() -> None:
    import spotdl.console.download as download_module
    from spotdl.console.entry_point import console_entry_point

    original = download_module.get_simple_songs

    def get_simple_songs_and_ask(*args, **kwargs):
        songs = original(*args, **kwargs)
        listing = [{f: getattr(s, f, None) for f in FIELDS} for s in songs]
        sys.stdout.write(MARKER + " " + json.dumps(listing, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        try:
            reply = json.loads(sys.stdin.readline() or "{}")
            skip = set(reply.get("skip") or [])
        except (ValueError, AttributeError):
            skip = set()   # no answer: behave exactly like plain spotdl
        return [s for s in songs if s.url not in skip]

    download_module.get_simple_songs = get_simple_songs_and_ask
    sys.argv = ["spotdl", *sys.argv[1:]]
    console_entry_point()


if __name__ == "__main__":
    main()
