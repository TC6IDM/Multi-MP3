"""What a finished track looks like on the iPhone: "(0001) Title.mp3", clean tags, even volume.

The files are copied to an iPhone with CopyTrans and played in Apple Music,
where two names matter:
  - the title (TIT2), shown while a song plays — just the song's name: no
    uploader in front, no "(Official Video)" or "[FREE DL]";
  - the sort title (TSOT), which Apple Music sorts by — "(0001) Title", so a
    playlist sorted by title plays in playlist order.
Before this app the owner named files "(0001) Song Name.mp3" by hand, and the
number ended up in the visible title as well. The file name keeps that scheme;
the number now lives in the sort title, so what's shown stays clean.

Three pieces:
  - naming: the clean title, file name and tags a track should have (`plan_for`);
  - tidy_playlist: renames files to match, moves songs that left the source
    playlist into _removed/, and puts back any that return;
  - Finisher: a background pool that writes tags and levels the volume
    (loudness.py), one atomic rewrite per file, without holding up downloads.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src import loudness
from src.state import DONE, PlaylistState

WIDTH = 4
REMOVED_DIR = "_removed"

# ── Clean titles ────────────────────────────────────────────────────────

# Bracketed bits that are about the upload rather than the song. Remix, edit,
# VIP, "feat.", "prod." and the like are part of the song and stay.
_JUNK_WORDS = (
    r"free\s*(?:dl|d/l|download)\b[^)\]}]*",
    r"(?:official\s+)?(?:hd\s+|hq\s+|4k\s+)?(?:music\s+|lyrics?\s+)?(?:video|audio|visuali[sz]er)"
    r"(?:\s+(?:clip|version))?",
    r"official", r"lyrics?", r"with\s+lyrics", r"hd", r"hq", r"4k", r"explicit",
    r"out\s+now\b[^)\]}]*", r"premiere", r"exclusive",
    r"clip\s+officiel", r"video\s+oficial", r"videoclip(?:\s+oficial)?",
)
_JUNK_BRACKET = re.compile(r"\s*[(\[{]\s*(?:" + "|".join(_JUNK_WORDS) + r")\s*[)\]}]", re.I)
_JUNK_PREFIX = re.compile(r"^\s*(?:premiere|exclusive|free\s*(?:dl|download)|out\s+now)\s*[:|\-–—]\s*", re.I)
# "| Free Download", "*FREE DL*", "- OUT NOW on …" at the end. "Out now" needs
# the separator: "Get Out Now" is a song.
_JUNK_SUFFIX = re.compile(r"(?:\s*[|*\-–—]+\s*\*?\s*(?:free\s*(?:dl|download)|out\s+now)|"
                          r"\s+\*?free\s*(?:dl|download))\b.*$", re.I)
# SoundCloud uploads often keep the producer's file name: "BE YOUR GIRL DUB.wav".
_FILE_EXTENSION = re.compile(r"\.(?:wav|mp3|flac|aiff?|m4a|aac|ogg)\s*$", re.I)
_SEPARATOR = re.compile(r"\s+[-–—]\s+")
_BRACKETED = re.compile(r"[(\[{][^)\]}]*[)\]}]")
# "Song - Extended Mix" / "Live Set - 2026": what follows the dash is a version
# or a date, not the song — so the part before it isn't an artist.
_VERSION_WORDS = {
    "extended", "radio", "club", "original", "vip", "dub", "instrumental", "acapella", "acoustic",
    "live", "demo", "remaster", "remastered", "bootleg", "flip", "rework", "edit", "mix", "remix",
    "version", "cover", "sped", "up", "slowed", "reverb", "intro", "outro", "clean", "mashup",
    "short", "long", "full", "unreleased", "pt", "part", "set", "and", "&", "+",
}
# "Song - Someone Remix": a remix credit after the dash, so again no artist
# before it. ("Dub" isn't one: in garage "RIKO DUB" is the track's name.)
_CREDIT_ENDINGS = {"remix", "edit", "mix", "bootleg", "flip", "vip", "rework", "version", "cover"}
# "A x B - Song", "A & B - Song", "A ft. B - Song": the left side lists artists.
_ARTIST_JOINERS = re.compile(r"\s(?:x|&|vs\.?|ft\.?|feat\.?|and)\s|,\s", re.I)


def _tidy_spaces(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" -–—|*").strip()


def _words(text: str) -> list[str]:
    return re.findall(r"[\w&+]+", _BRACKETED.sub(" ", text).lower())


def _is_version(text: str) -> bool:
    words = _words(text)
    return bool(words) and all(w in _VERSION_WORDS or w.isdigit() for w in words)


def _same_name(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"[^0-9a-z]+", "", s.lower())   # noqa: E731
    return bool(norm(a)) and norm(a) == norm(b)


def _names_artist(left: str, right: str, uploader: str) -> bool:
    """Is "left - right" an "Artist - Song" title?"""
    if _is_version(right):
        return False
    if _same_name(left, uploader) or _ARTIST_JOINERS.search(f" {left} "):
        return True
    words = _words(right)
    return not (words and words[-1] in _CREDIT_ENDINGS)


def clean_title(title: str, uploader: str = "") -> tuple[str, str | None]:
    """(song name, artist) from an upload's title.

    The artist comes from an "Artist - Song" title, and is None when the title
    doesn't name one (the uploader is then the best guess, and is left alone).
    """
    raw = _tidy_spaces(title or "")
    text = _JUNK_BRACKET.sub("", _FILE_EXTENSION.sub("", raw))
    text = _JUNK_PREFIX.sub("", text)
    text = _tidy_spaces(_JUNK_SUFFIX.sub("", text)) or _tidy_spaces(text)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”":
        text = text[1:-1].strip()
    artist: str | None = None
    parts = _SEPARATOR.split(text, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1] and _names_artist(parts[0], parts[1], uploader):
        artist, text = _tidy_spaces(parts[0]), _tidy_spaces(parts[1])
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”":
            text = text[1:-1].strip()
    return (text or raw), artist


def uploader_name(name: str) -> str:
    """YouTube's auto-generated "Artist - Topic" channels are just the artist."""
    return re.sub(r"\s+-\s+Topic$", "", name or "").strip()


# ── Names and tags ──────────────────────────────────────────────────────

def safe_filename(text: str, limit: int = 120) -> str:
    """Text usable as a Windows file name (and on a phone, and in a zip)."""
    text = text.replace('"', "'")
    text = re.sub(r"[:/\\|]", "-", text)
    text = re.sub(r"[<>?*\x00-\x1f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip(" .")


def sort_title(number: int, title: str) -> str:
    return f"({number:0{WIDTH}d}) {title}"


def file_name(number: int, title: str) -> str:
    return f"({number:0{WIDTH}d}) {safe_filename(title) or 'Track'}.mp3"


@dataclass
class Plan:
    """The name and tags one track's file should have."""
    name: str | None                  # file name, or None to keep the one it has
    tags: dict[str, str] = field(default_factory=dict)   # ID3 frame → text

    @property
    def sig(self) -> str:
        """Changes whenever the plan does — the record keeps the last one
        applied, so an unchanged track costs nothing on the next run."""
        blob = json.dumps([self.name, sorted(self.tags.items())], ensure_ascii=False)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def known(text: str) -> str:
    """The text, or "" if it was mangled on the way in (U+FFFD): a title the
    record got wrong must never be written into the file."""
    return "" if "\ufffd" in (text or "") else (text or "")


def song_for(provider: str, t: dict[str, Any]) -> tuple[str, str | None]:
    """(title, artist-or-None) for a record's track."""
    if provider == "spotify":
        # spotdl's tags are already right: the song's own name and artists.
        return str(t.get("title") or ""), None
    if t.get("track_title"):
        # YouTube Music uploads say exactly which song they are.
        return str(t["track_title"]), t.get("track_artist") or None
    artists = t.get("artists") or []
    uploader = str(artists[0]) if artists else ""
    title, artist = clean_title(str(t.get("title") or ""), uploader_name(uploader))
    if artist is None:
        # The artist a label filled in on SoundCloud, or the artist behind an
        # auto-generated "Artist - Topic" YouTube channel.
        artist = t.get("track_artist") or (uploader_name(uploader) if uploader_name(uploader) != uploader
                                           else None)
    return title, artist


def plan_for(st: PlaylistState, t: dict[str, Any], fallback_title: str = "") -> Plan:
    provider = st.provider
    # Spotify albums are numbered by track number; artist pages and single
    # tracks have no number at all (file_index None) and keep their names.
    number = t.get("file_index") if provider == "spotify" else t.get("index")
    title, artist = song_for(provider, t)
    title = known(title) or known(fallback_title)
    artist = known(artist or "") or None
    tags: dict[str, str] = {}
    if title:
        tags["TIT2"] = title
    if artist:
        tags["TPE1"] = artist
    if provider != "spotify":
        # The playlist is the album (one album per playlist in Apple Music,
        # with a single album artist so it doesn't split by uploader).
        album = str(st.data.get("name") or "")
        if album:
            tags["TALB"] = album
        if st.data.get("album_artist"):
            tags["TPE2"] = str(st.data["album_artist"])
        if number:
            tags["TRCK"] = str(number)
    name = None
    if number and title:
        tags["TSOT"] = sort_title(int(number), title)
        name = file_name(int(number), title)
    return Plan(name, tags)


def _frame_text(frame: Any) -> str:
    return "/".join(str(x) for x in getattr(frame, "text", []) or [])


def read_tag(path: Path, frame_id: str) -> str:
    from mutagen.id3 import ID3

    try:
        frames = ID3(path, translate=False).getall(frame_id)
    except Exception:
        return ""
    return _frame_text(frames[0]) if frames else ""


def tags_differ(path: Path, tags: dict[str, str]) -> bool:
    from mutagen.id3 import ID3

    try:
        id3 = ID3(path, translate=False)
    except Exception:
        return bool(tags)
    for frame_id, value in tags.items():
        frames = id3.getall(frame_id)
        if (_frame_text(frames[0]) if frames else "") != value:
            return True
    return False


def write_tags(path: Path, tags: dict[str, str]) -> None:
    """Set these frames, leaving every other tag (artwork included) alone.

    Saved in the tag's own version. The library is ID3v2.3, which is what
    iTunes writes, and iTunes puts its sort title (TSOT) in v2.3 tags too —
    but mutagen's v2.3 conversion strips TSOT as v2.4-only, so tags are read
    untranslated and written back as they came.
    """
    from mutagen.id3 import ID3, TXXX, Frames, ID3NoHeaderError

    try:
        id3 = ID3(path, translate=False)
        version = 4 if id3.version >= (2, 4, 0) else 3
        if id3.version < (2, 3, 0):
            id3, version = ID3(path), 4      # v2.2 frames can't be written as v2.3
    except ID3NoHeaderError:
        id3, version = ID3(), 3
    for frame_id, value in tags.items():
        id3.delall(frame_id)
        if not value:
            continue
        if frame_id.startswith("TXXX:"):
            id3.add(TXXX(encoding=3, desc=frame_id[5:], text=[value]))
        else:
            id3.add(Frames[frame_id](encoding=3, text=[value]))
    id3.save(path, v2_version=version)


@dataclass
class Finished:
    gain: int | None        # 1.5 dB steps applied (None: volume not looked at)
    changed: bool


def finish_file(path: Path, tags: dict[str, str], *, level: bool,
                target: float = loudness.DEFAULT_TARGET_LUFS) -> Finished:
    """Bring one MP3's tags and volume up to date in a single atomic rewrite.

    The new version is built next to the file and swapped in only when it is
    complete — and, if the volume changed, only once ffmpeg confirms it
    decodes exactly as much louder or quieter as intended.
    """
    gain = loudness.read_gain(path)
    need_level = level and gain is None
    if not need_level and not tags_differ(path, tags):
        return Finished(gain, False)

    before = path.stat()
    measured = loudness.measure(path) if need_level else None
    buf = bytearray(path.read_bytes())
    applied = loudness.shift_gain(buf, loudness.plan_steps(measured, target)) if measured else 0
    all_tags = dict(tags)
    if need_level:
        all_tags[f"TXXX:{loudness.GAIN_TAG}"] = loudness.gain_text(applied, measured)
    tmp = path.with_name(f".{path.name}.finishing")
    try:
        tmp.write_bytes(buf)
        if applied and measured is not None:
            after = loudness.measure(tmp)
            expected = measured.lufs + applied * loudness.STEP_DB
            if after is None or abs(after.lufs - expected) > 1.0:
                # Never seen, but if it happens the file keeps its volume —
                # and is marked, so it isn't tried again every run.
                tmp.write_bytes(path.read_bytes())
                applied = 0
                all_tags[f"TXXX:{loudness.GAIN_TAG}"] = "+0 (left as is: level check failed)"
        write_tags(tmp, all_tags)
        now = path.stat()
        if (now.st_mtime_ns, now.st_size) != (before.st_mtime_ns, before.st_size):
            raise OSError("the file changed while it was being finished")
        loudness.replace_file(tmp, path)
        # Keep the download date: it's what Explorer and the phone sort by.
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    finally:
        tmp.unlink(missing_ok=True)
    return Finished(applied if need_level else gain, True)


# ── Tidying a playlist folder ───────────────────────────────────────────

@dataclass
class TidyReport:
    renamed: int = 0
    removed: int = 0       # songs that left the playlist, moved into _removed/
    restored: int = 0      # songs back in the playlist, moved out of _removed/
    swept: int = 0         # stray numbered files no track owns, into _removed/

    def note(self) -> str:
        parts = []
        if self.renamed:
            parts.append(f"renamed {self.renamed}")
        if self.removed or self.swept:
            parts.append(f"moved {self.removed + self.swept} no longer in the playlist to {REMOVED_DIR}/")
        if self.restored:
            parts.append(f"put back {self.restored} that returned")
        return ", ".join(parts)


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"no free name for {path.name}")


def _move(src: Path, dst: Path) -> Path:
    """Rename without ever replacing an existing file."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst = _unique(dst)
    os.rename(src, dst)        # unlike os.replace, refuses to overwrite on Windows
    return dst


_NUMBER_PREFIX = re.compile(r"^\s*\(?(\d+)\)?\s")


def numbered_files(folder: Path) -> list[Path]:
    """Every "(0001) …" / "0001 …" MP3 in a folder (not just one per number)."""
    try:
        return sorted(p for p in folder.iterdir()
                      if p.suffix.lower() == ".mp3" and not p.name.startswith(".")
                      and _NUMBER_PREFIX.match(p.name))
    except OSError:
        return []


def _norm(text: str) -> str:
    # Accents dropped: a title's "é" and a filename's "e" + combining accent
    # (or a plain "e" from an older sanitiser) are the same letter.
    decomposed = unicodedata.normalize("NFKD", text or "")
    return re.sub(r"[\W_]+", "", "".join(c for c in decomposed if not unicodedata.combining(c)).casefold())


def _stem_key(path: Path) -> str:
    return _norm(_NUMBER_PREFIX.sub("", path.stem + " ", count=1))


def _title_keys(title: str) -> tuple[str, str]:
    title = known(title)
    return _norm(title), _norm(clean_title(title)[0]) if title else ""


def _fits(stem: str, keys: tuple[str, str]) -> bool:
    want, clean = keys
    if not want:
        return True
    if not stem:
        return False
    return want in stem or (len(stem) >= 4 and stem in want) or (len(clean) >= 2 and clean in stem)


def name_matches(path: Path, title: str) -> bool:
    """Could this file be this song, going by its name? An unknown title can't
    rule anything out. Works for every naming this app has used: "(0005) Clean
    Title", "005 Title - Artist" (spotdl), "0005 Uploader - Raw Title"."""
    return _fits(_stem_key(path), _title_keys(title))


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def match_files(folder: Path, tracks: list[dict[str, Any]], number_key: str = "index",
                exclude: set[Path] | None = None) -> dict[str, Path]:
    """Which numbered file in `folder` belongs to which track.

    By number when the file's name also fits the track's title; then, for
    tracks still without one, by title alone where exactly one unclaimed file
    fits. Earlier versions matched on the number only, which hands a file to
    whatever song has since moved into its position — harmless while names
    were left alone, but tidying would now retitle that file as the wrong song.
    """
    files = [p for p in numbered_files(folder) if not exclude or p not in exclude]
    by_number: dict[int, list[Path]] = {}
    for p in files:
        m = _NUMBER_PREFIX.match(p.name)
        if m:
            by_number.setdefault(int(m.group(1)), []).append(p)
    claimed: set[Path] = set()
    found: dict[str, Path] = {}
    for t in tracks:
        n = t.get(number_key)
        if n is None:
            continue
        fits = [p for p in by_number.get(int(n), []) if p not in claimed
                and name_matches(p, str(t.get("title") or ""))]
        if fits:
            best = max(fits, key=_mtime)
            found[str(t["key"])] = best
            claimed.add(best)
    for t in tracks:
        key, title = str(t["key"]), str(t.get("title") or "")
        if key in found or t.get(number_key) is None or not _norm(title):
            continue
        fits = [p for p in files if p not in claimed and name_matches(p, title)]
        if len(fits) == 1:
            found[key] = fits[0]
            claimed.add(fits[0])
    return found


def _in_removed(path: Path, root: Path) -> bool:
    try:
        return path.resolve().relative_to(root.resolve()).parts[:1] == (REMOVED_DIR,)
    except ValueError:
        return False


def tidy_playlist(st: PlaylistState, *, since: float, shared_folder: bool = False,
                  on_move: Any = None, logger: logging.Logger | None = None) -> TidyReport:
    """Make a playlist's folder match its record. Fast: renames only.

      - done tracks get their "(0001) Title.mp3" name (and follow the playlist
        when tracks move position);
      - songs that left the source playlist move to _removed/<folder>/, and
        come back if they're added again;
      - numbered MP3s no track owns — songs removed before this existed — join
        them, so the folder holds exactly the playlist. Skipped for a folder
        another playlist shares, and never touches a file written since
        `since` (this run's own downloads).

    `on_move(old, new)` hears about every move (the duplicate index follows).
    """
    report = TidyReport()
    if not st.folder:
        return report
    root = st.root
    folder = root / st.folder
    removed_dir = root / REMOVED_DIR / Path(st.folder).name

    def moved(old: Path, new: Path) -> None:
        if on_move is not None:
            on_move(old, new)

    def warn(msg: str) -> None:
        if logger:
            logger.warning(msg)

    # Songs that left the playlist.
    for key, r in st.removed().items():
        path = st.file_path(r)
        if path is None or not path.is_file() or _in_removed(path, root):
            continue
        try:
            new = _move(path, removed_dir / path.name)
        except OSError as e:
            warn(f"⚠️ Couldn't move {path.name} to {REMOVED_DIR}/: {e}")
            continue
        st.note_removed(key, new)
        moved(path, new)
        report.removed += 1

    number_key = "file_index" if st.provider == "spotify" else "index"
    tracks = st.ordered()

    # Done tracks whose file went missing from the record: a rename recorded
    # just before a crash, or a SoundCloud track an old archive vouched for
    # without saying which file it was. Claimed back by number and title.
    owned = {p for t in tracks if (p := st.file_path(t)) is not None}
    orphans = [t for t in tracks if t.get("status") == DONE
               and not ((p := st.file_path(t)) is not None and p.is_file())]
    if orphans:
        for key, path in match_files(folder, orphans, number_key, exclude=owned).items():
            st.note(key, file=path)
        tracks = st.ordered()

    # Done tracks: the right name, in the right folder.
    moves: list[tuple[str, Path, Path]] = []
    for t in tracks:
        if t.get("status") != DONE:
            continue
        path = st.file_path(t)
        if path is None or not path.is_file():
            continue
        plan = plan_for(st, t)
        returning = _in_removed(path, root)
        if plan.name:
            target = folder / plan.name
        elif returning:
            target = folder / path.name
        else:
            continue
        if path.resolve() == target.resolve():
            continue
        if not returning and not name_matches(path, str(t.get("title") or "")):
            # The record pairs this track with a file named after some other
            # song. Retitling it would only make the mix-up permanent.
            if logger:
                logger.debug(f"Not renaming {path.name}: it doesn't look like {t.get('title')!r}")
            continue
        moves.append((t["key"], path, target))

    # A move into a name that another move is about to vacate (tracks that
    # swapped places) parks first under a name nothing else uses, and lands
    # after the others have moved out of its way.
    sources = {src.resolve() for _, src, _ in moves}
    direct: list[tuple[str, Path, Path]] = []
    parked: list[tuple[str, Path, Path]] = []
    for key, src, dst in moves:
        if dst.exists() and dst.resolve() in sources:
            try:
                spot = _move(src, src.with_name(f".tidy-{hashlib.sha1(key.encode()).hexdigest()[:10]}.mp3"))
            except OSError as e:
                warn(f"⚠️ Couldn't rename {src.name}: {e}")
                continue
            st.note(key, file=spot)
            parked.append((key, spot, dst))
        else:
            direct.append((key, src, dst))
    for key, src, dst in direct + parked:
        if dst.exists() and dst.resolve() != src.resolve():
            if st.owner_of(dst) is not None:
                continue           # another track's file: leave both alone
            try:                   # a stray copy: out of the way, not deleted
                stray = _move(dst, removed_dir / dst.name)
                moved(dst, stray)
                report.swept += 1
            except OSError as e:
                warn(f"⚠️ Couldn't move {dst.name} out of the way: {e}")
                continue
        try:
            new = _move(src, dst)
        except OSError as e:
            warn(f"⚠️ Couldn't rename {src.name}: {e}")
            continue
        st.note(key, file=new)
        moved(src, new)
        if _in_removed(src, root):
            report.restored += 1
        else:
            report.renamed += 1

    # Numbered files that aren't any track's, and don't even look like one:
    # songs that left the playlist before _removed/ existed.
    if not shared_folder and not st.is_new:
        owned = {p for t in st.ordered() if (p := st.file_path(t)) is not None}
        keys = [_title_keys(str(t.get("title") or "")) for t in tracks if _norm(str(t.get("title") or ""))]
        for path in numbered_files(folder):
            if path in owned or _mtime(path) >= since or _mtime(path) == 0.0:
                continue
            stem = _stem_key(path)
            if any(_fits(stem, k) for k in keys):
                continue
            try:
                new = _move(path, removed_dir / path.name)
            except OSError as e:
                warn(f"⚠️ Couldn't move {path.name} to {REMOVED_DIR}/: {e}")
                continue
            moved(path, new)
            report.swept += 1
    if report.renamed or report.removed or report.restored or report.swept:
        st.save()
    return report


def replace_superseded(st: PlaylistState, key: str, new: Path) -> None:
    """After a re-download landed under a new name, delete the old copy it
    replaces — unless some other track has since claimed that file."""
    t = st.track(key)
    old = st.file_path(t) if t else None
    if old is None or not old.is_file():
        return
    try:
        if old.resolve() == new.resolve():
            return
    except OSError:
        return
    owner = st.owner_of(old)
    if owner is not None and owner != key:
        return
    try:
        old.unlink()
    except OSError:
        pass


# ── Finishing in the background ─────────────────────────────────────────

def _default_workers() -> int:
    try:
        n = int(os.getenv("LEVEL_WORKERS", "") or 0)
    except ValueError:
        n = 0
    return n if n > 0 else max(1, min(4, (os.cpu_count() or 2) // 2))


class Finisher:
    """Writes tags and levels volume for finished tracks, off the download path.

    Downloads don't wait for it, and a run only waits at the very end. The
    first run after this was added has the whole existing library to get
    through once (well under a second a song); Stop leaves the rest for the
    next run, since each track records what was last applied to it.
    """

    def __init__(self, logger: logging.Logger, cancel: threading.Event | None = None,
                 level: bool | None = None, workers: int | None = None) -> None:
        self.logger = logger
        self.cancel = cancel or threading.Event()
        self.level = (loudness.enabled() if level is None else level) and bool(shutil.which("ffmpeg"))
        self.target = loudness.target_lufs()
        self._pool = ThreadPoolExecutor(max_workers=workers or _default_workers(),
                                        thread_name_prefix="finish")
        self._lock = threading.Lock()
        self._queued: set[tuple[str, str, str]] = set()
        self._futures: list[Future[None]] = []
        self.done = 0
        self.levelled = 0
        self.failed = 0
        self._announced = False
        self._last_report = time.monotonic()

    def needs(self, st: PlaylistState, t: dict[str, Any]) -> bool:
        if t.get("status") != DONE or not t.get("file"):
            return False
        if self.level and t.get("gain") is None:
            return True
        return t.get("tagged") != plan_for(st, t).sig

    def submit(self, st: PlaylistState, key: str) -> None:
        if self.cancel.is_set():
            return
        ident = (st.provider, st.key, key)
        with self._lock:
            if ident in self._queued:
                return
            self._queued.add(ident)
            self._futures.append(self._pool.submit(self._run, st, key))

    def submit_playlist(self, st: PlaylistState) -> int:
        todo = [t for t in st.ordered() if self.needs(st, t)]
        if len(todo) > 20 and not self._announced:
            self._announced = True
            what = "tagging and levelling the volume of" if self.level else "tagging"
            self.logger.info(f"🎚️ {what.capitalize()} existing files in the background — "
                             f"a one-off for the library, new downloads after that")
        for t in todo:
            self.submit(st, t["key"])
        return len(todo)

    def _run(self, st: PlaylistState, key: str) -> None:
        if self.cancel.is_set():
            return
        t = st.track(key)
        if not t or t.get("status") != DONE:
            return
        path = st.file_path(t)
        if path is None or not path.is_file():
            return
        fallback = "" if known(str(t.get("title") or "")) else read_tag(path, "TIT2")
        plan = plan_for(st, t, fallback_title=fallback)
        try:
            result = finish_file(path, plan.tags, level=self.level, target=self.target)
        except Exception as e:
            with self._lock:
                self.failed += 1
            self.logger.debug(f"Couldn't finish {path.name}: {e}")
            return
        st.note(key, tagged=plan.sig, gain=result.gain)
        with self._lock:
            self.done += 1
            if result.gain:
                self.levelled += 1
            report = time.monotonic() - self._last_report > 30
            if report:
                self._last_report = time.monotonic()
            left = len(self._queued) - self.done - self.failed
        if report and left > 0:
            self.logger.info(f"🎚️ {self.done} files tagged/levelled, {left} to go")

    def close(self) -> None:
        """Wait for everything queued (or, after Stop, only what's running)."""
        if self.cancel.is_set():
            self._pool.shutdown(wait=True, cancel_futures=True)
        else:
            while True:
                with self._lock:
                    pending = [f for f in self._futures if not f.done()]
                if not pending or self.cancel.is_set():
                    break
                time.sleep(0.25)
            self._pool.shutdown(wait=True, cancel_futures=self.cancel.is_set())
        left = len(self._queued) - self.done - self.failed
        if self.done or self.failed:
            msg = f"🎚️ Tags and volume brought up to date on {self.done} file(s)"
            if self.level:
                msg += f" ({self.levelled} turned up or down)"
            if self.failed:
                msg += f" · {self.failed} couldn't be finished"
            if left > 0:
                msg += f" · {left} left for next run"
            self.logger.info(msg)
