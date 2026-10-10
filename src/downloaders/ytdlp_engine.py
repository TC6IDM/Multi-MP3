"""yt-dlp as a library: SoundCloud and YouTube through one rate-limited worker pool per provider.

Before this, each playlist was a separate scdl / yt-dlp subprocess walking its
tracks one by one, pacing itself with fixed sleeps and blind to every other
playlist in the run. That design couldn't be both fast and safe: in parallel
the processes multiplied the request rate past SoundCloud's limit; serially the
fixed sleeps idled while a single track downloaded and converted.

Here each provider gets one queue fed by all of its playlists, N worker threads
draining it, and one AdaptiveRateLimiter deciding when the next track may
start. Downloads and ffmpeg conversions overlap while the request rate stays
inside a single shared budget, and the instant the source pushes back every
worker pauses together.

Per playlist:
  1. resolve   — one flat playlist extraction (no per-track requests)
  2. reconcile — the state record says which tracks are done; those cost nothing
  3. queue     — everything else goes to the provider's workers
  4. record    — each finished track is written to the record immediately
"""

from __future__ import annotations

import collections
import json
import logging
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import DownloadCancelled

from src.dedupe import DoneIndex, copy_track, renumber
from src.models import CODE_CANCELLED, CODE_FAILED, CODE_OK, CODE_PARTIAL, LinkResult
from src.ratelimit import AdaptiveRateLimiter
from src.state import DONE, FAILED, PENDING, UNAVAILABLE, PlaylistState, StateStore, index_files, read_legacy_archive
from src.tui import DownloadProgress

# ── Failure classification ──────────────────────────────────────────────

@dataclass
class Failure:
    # rate_limited — the source is pushing back; pause everything and retry
    # forbidden    — 401/403; on SoundCloud a stale client_id or a rate limit
    # unavailable  — gone at the source; retrying can't help
    # error        — anything else; retried a few times, then recorded failed
    # cancelled    — Cancel was pressed mid-track; the track stays pending
    kind: str
    message: str
    retry_after: float | None = None


_RATE_MARKERS = (
    "http error 429", "too many requests", "rate limit", "rate-limit",
    "not a bot",                                   # YouTube's bot wall is its rate limit
    "this content isn't available, try again later",
)
_UNAVAILABLE_MARKERS = (
    "http error 404", "http error 410", "video unavailable", "this video is unavailable",
    "private video", "has been removed", "not available in your country",
    "geo restrict", "geo-restrict", "blocked it in your country", "copyright",
    "account associated with this video has been terminated", "members-only",
    "join this channel", "confirm your age", "this track is not available",
    "track was not found", "is not available",
)


def _retry_after(headers: Any) -> float | None:
    try:
        raw = headers.get("Retry-After") if headers else None
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None   # an HTTP-date; the default cooldown schedule is fine


def classify(exc: BaseException) -> Failure:
    """Decide what a yt-dlp failure means for the run."""
    message = re.sub(r"^ERROR:\s*", "", str(exc)).strip() or exc.__class__.__name__
    status, headers = None, None
    # The HTTP status is the most reliable signal, but yt-dlp wraps it:
    # DownloadError(exc_info) → ExtractorError(.cause) → HTTPError.
    stack: list[Any] = [exc]
    seen = set()
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, DownloadCancelled):
            return Failure("cancelled", message)
        if isinstance(e, HTTPError):
            status = getattr(e, "status", None)
            headers = getattr(getattr(e, "response", None), "headers", None)
            break
        stack.extend([getattr(e, "cause", None), e.__cause__, e.__context__])
        exc_info = getattr(e, "exc_info", None)
        if isinstance(exc_info, tuple) and len(exc_info) > 1:
            stack.append(exc_info[1])

    low = message.lower()
    if status == 429 or any(m in low for m in _RATE_MARKERS):
        return Failure("rate_limited", message, _retry_after(headers))
    if status in (401, 403) or "http error 403" in low or "http error 401" in low:
        return Failure("forbidden", message)
    if "requested format" in low:
        return Failure("error", message)   # "Requested format is not available" isn't the track being gone
    if status in (404, 410) or any(m in low for m in _UNAVAILABLE_MARKERS):
        return Failure("unavailable", message)
    return Failure("error", message)


# ── Provider profile ────────────────────────────────────────────────────

@dataclass
class EngineProfile:
    """Everything provider-specific the engine needs."""
    provider: str
    label: str
    ydl_args: list[str]                 # yt-dlp CLI flags, parsed by yt-dlp itself
    outtmpl: str                        # relative to the output dir; first segment is the folder
    workers: int
    limiter: AdaptiveRateLimiter
    max_attempts: int = 3
    # SoundCloud answers a rate limit with 403 as often as 429.
    forbidden_is_rate_limit: bool = False
    # Import pre-records yt-dlp download archives (.archive/<key>.txt).
    legacy_archive: bool = False
    # Give up on the lane after this many back-to-back cooldowns (~35 min with
    # the default 60/180/600s schedule) instead of waiting forever.
    give_up_after: int = 5
    # The files carry the playlist in their tags (album = playlist, track =
    # position), so a copy into another playlist needs retagging.
    tags_from_playlist: bool = False
    before_run: Callable[[YtDlpEngine], None] | None = None
    refresh_auth: Callable[[YtDlpEngine], bool] | None = None
    backfill: Callable[[YtDlpEngine, list[dict[str, Any]]], None] | None = None


# ── Internals ───────────────────────────────────────────────────────────

class _AnyEvent:
    """Quacks like threading.Event; set when any of its events is."""

    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(e.is_set() for e in self._events)

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(0.25, left))
        return True


class _WorkQueue:
    """Deque + condition. Requeues go to the front so a playlist finishes in order.

    Closing doesn't end it while a worker still holds an item, because that
    item may come back (a rate-limited track is requeued, not dropped).
    """

    def __init__(self) -> None:
        self._items: collections.deque[Any] = collections.deque()
        self._cond = threading.Condition()
        self._closed = False
        self._in_flight = 0

    def put(self, item: Any, front: bool = False) -> None:
        with self._cond:
            if front:
                self._items.appendleft(item)
            else:
                self._items.append(item)
            self._cond.notify()

    def get(self, stopping: Callable[[], bool]) -> Any | None:
        with self._cond:
            while True:
                if stopping():
                    return None
                if self._items:
                    self._in_flight += 1
                    return self._items.popleft()
                if self._closed and self._in_flight == 0:
                    return None
                self._cond.wait(0.5)

    def done(self) -> None:
        with self._cond:
            self._in_flight -= 1
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def drain(self) -> list[Any]:
        with self._cond:
            items = list(self._items)
            self._items.clear()
            return items


@dataclass
class _Playlist:
    link: str
    position: int
    state: PlaylistState | None = None
    name: str = ""
    folder: str = ""
    total: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)
    remaining: int = 0
    enqueued_all: bool = False
    started: bool = False
    finished: bool = False
    code: int = CODE_OK
    error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class _Track:
    pl: _Playlist
    key: str
    index: int
    entry: dict[str, Any]
    attempts: int = 0


class _YdlLogger:
    """Sends yt-dlp's chatter to the log pane of the playlist being worked on."""

    _SKIP = re.compile(r"^\[debug\]|^\[download\]\s+\d+(\.\d+)?%|ETA\s+\d")

    def __init__(self, engine: YtDlpEngine) -> None:
        self._engine = engine

    def debug(self, msg: str) -> None:
        self._line(msg)

    def info(self, msg: str) -> None:
        self._line(msg)

    def warning(self, msg: str) -> None:
        self._line(f"WARNING: {msg}")

    def error(self, msg: str) -> None:
        self._line(msg)

    def _line(self, msg: str) -> None:
        if not msg or self._SKIP.search(msg):
            return
        pl = getattr(self._engine._tls, "playlist", None)
        if pl is not None:
            self._engine._log(pl, msg)
        else:
            self._engine.logger.debug(msg)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


# ── Engine ──────────────────────────────────────────────────────────────

class YtDlpEngine:
    def __init__(self, profile: EngineProfile, output_dir: Path, logger: logging.Logger,
                 store: StateStore, progress: DownloadProgress,
                 cancel: threading.Event | None = None,
                 done_index: DoneIndex | None = None) -> None:
        self.p = profile
        # Tracks already downloaded for another playlist are copied, not fetched.
        self.done_index = done_index
        self.output_dir = output_dir.resolve()
        self.cachedir = self.output_dir / ".cache" / "yt-dlp"
        # Workers download here and only move finished files into place (see
        # _promote) — never straight over an existing copy.
        self.staging = self.output_dir / ".partial" / profile.provider
        self.logger = logger
        self.store = store
        self.progress = progress
        self.cancel = cancel or threading.Event()
        # Set when the lane gives up (still rate-limited after repeated cooldowns).
        self._halt = threading.Event()
        self._stop = _AnyEvent(self.cancel, self._halt)
        self._tls = threading.local()
        self._generation = 0
        self._auth_refreshed = False
        self._lock = threading.Lock()
        self._params = self._build_params()
        self._task = ""

    @property
    def provider(self) -> str:
        return self.p.provider

    def _stopping(self) -> bool:
        return self._stop.is_set()

    # ── yt-dlp setup ─────────────────────────────────────────────────

    def _build_params(self) -> dict[str, Any]:
        """Options from yt-dlp's own CLI parser, so flags mean exactly what they
        mean on the command line, then the hooks a library caller needs."""
        opts: dict[str, Any] = dict(
            yt_dlp.parse_options(list(self.p.ydl_args) + ["-o", self.p.outtmpl]).ydl_opts)
        opts.update({
            "paths": {"home": str(self.staging)},
            "cachedir": str(self.cachedir),
            "logger": _YdlLogger(self),
            "progress_hooks": [self._progress_hook],
            "noprogress": True,
            # parse_options defaults to "only_download", which swallows download
            # errors — the engine has to see every failure to classify it.
            "ignoreerrors": False,
            "extract_flat": False,
            # Only tracks the record says aren't done reach a worker, so an
            # existing file there is stale — a reset, or a broken earlier
            # attempt — and must be replaced rather than skipped.
            "overwrites": True,
            # scdl used "" here; yt-dlp's default "NA" would rename tracks
            # whose uploader is missing.
            "outtmpl_na_placeholder": "",
        })
        return opts

    def _resolve_params(self) -> dict[str, Any]:
        params: dict[str, Any] = dict(self._params)
        params.update({"extract_flat": "in_playlist", "skip_download": True,
                       # Folders are computed against the real output dir.
                       "paths": {"home": str(self.output_dir)},
                       "postprocessors": [], "progress_hooks": []})
        return params

    def _ydl(self) -> yt_dlp.YoutubeDL:
        """This thread's YoutubeDL. Instances aren't thread-safe, so one each;
        rebuilt after an auth refresh so no extractor keeps a stale client id."""
        tls = self._tls
        if getattr(tls, "generation", None) != self._generation:
            self._close_ydl()
            tls.ydl = yt_dlp.YoutubeDL(dict(self._params))
            tls.generation = self._generation
        ydl: yt_dlp.YoutubeDL = tls.ydl
        return ydl

    def _close_ydl(self) -> None:
        old = getattr(self._tls, "ydl", None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
            self._tls.ydl = None

    def _progress_hook(self, d: dict[str, Any]) -> None:
        # Raising here is how yt-dlp lets a caller abort mid-file.
        if self.cancel.is_set():
            raise DownloadCancelled("Cancelled")

    # ── Events ───────────────────────────────────────────────────────

    def _emit(self, pl: _Playlist, event: str, data: dict[str, Any]) -> None:
        self.progress.track_progress(self.provider, pl.link, event, data)

    def _log(self, pl: _Playlist, line: str) -> None:
        self.progress.track_progress(self.provider, pl.link, "log", {"line": line[:400]})

    # ── Run ──────────────────────────────────────────────────────────

    def run(self, links: list[str]) -> list[LinkResult]:
        if not links:
            return []
        if self.p.before_run:
            try:
                self.p.before_run(self)
            except Exception as e:
                self.logger.warning(f"{self.p.label} setup step failed: {e}")

        self._task = f"{self.provider} ({len(links)} playlists)"
        self.progress.add_task(self._task, total=len(links))
        # Only one job runs at a time, so anything in staging is debris from an
        # interrupted run (.part files, unembedded thumbnails).
        shutil.rmtree(self.staging, ignore_errors=True)

        queue = _WorkQueue()
        playlists = [_Playlist(link=link, position=i) for i, link in enumerate(links, 1)]
        producer = threading.Thread(target=self._produce, args=(playlists, queue),
                                    name=f"{self.provider}-resolve", daemon=True)
        workers = [threading.Thread(target=self._work, args=(queue,),
                                    name=f"{self.provider}-dl-{n + 1}", daemon=True)
                   for n in range(max(1, self.p.workers))]
        producer.start()
        for w in workers:
            w.start()
        producer.join()
        for w in workers:
            w.join()

        # Anything still queued — Cancel, or the lane gave up — simply stays
        # pending in its record; that's what the next run picks up.
        queue.drain()
        for pl in playlists:
            self._finish(pl)
        shutil.rmtree(self.staging, ignore_errors=True)
        return [LinkResult(self.provider, pl.link, pl.name or pl.link, pl.code, pl.position, len(links))
                for pl in playlists]

    def _produce(self, playlists: list[_Playlist], queue: _WorkQueue) -> None:
        """Resolve playlists one after another, feeding the queue as each lands,
        so the workers start on playlist 1 while playlist 2 is still resolving."""
        try:
            for pl in playlists:
                if self._stopping():
                    break
                self._tls.playlist = pl
                try:
                    items = self._prepare(pl)
                except Exception as e:
                    self.logger.error(f"💥 {self.p.label}: couldn't prepare {pl.link}: {e}")
                    pl.error = str(e)
                    items = None
                finally:
                    self._tls.playlist = None
                if items is None:
                    self._finish(pl)
                    continue
                with pl.lock:
                    pl.remaining = len(items)
                for item in items:
                    queue.put(item)
                with pl.lock:
                    pl.enqueued_all = True
                    finish_now = pl.remaining <= 0
                if finish_now:
                    self._finish(pl)
        finally:
            queue.close()

    # ── Resolve + reconcile ──────────────────────────────────────────

    def _resolve(self, pl: _Playlist) -> tuple[dict[str, Any], str] | None:
        attempts = 0
        while not self._stopping():
            attempts += 1
            if not self.p.limiter.acquire(self._stop):
                return None
            issued = self.p.limiter.stamp()
            try:
                with yt_dlp.YoutubeDL(self._resolve_params()) as ydl:
                    info = ydl.extract_info(pl.link, download=False)
                    if not info:
                        raise RuntimeError("yt-dlp returned nothing for this link")
                    info = self._as_playlist(info, pl.link)
                    folder = self._folder_for(ydl, info)
                return info, folder
            except Exception as e:
                f = classify(e)
                if f.kind == "cancelled":
                    return None
                if f.kind == "forbidden" and self.p.forbidden_is_rate_limit:
                    if self._try_refresh_auth(pl):
                        continue
                    f = Failure("rate_limited", f.message)
                if f.kind == "rate_limited":
                    self._rate_limited(pl, f, issued)
                    continue
                if f.kind == "error" and attempts < self.p.max_attempts:
                    self._log(pl, f"🔁 Couldn't read the playlist ({f.message[:150]}) — retrying")
                    self._stop.wait(5 * attempts)
                    continue
                pl.error = f.message
                self.logger.error(f"❌ {self.p.label}: couldn't read {pl.link}: {f.message[:300]}")
                self._log(pl, f"❌ Couldn't read the playlist: {f.message[:300]}")
                return None
        return None

    @staticmethod
    def _as_playlist(info: dict[str, Any], link: str) -> dict[str, Any]:
        """A single-track link is handled as a one-track playlist named after it."""
        if info.get("_type") in ("playlist", "multi_video"):
            return info
        entry = {
            "_type": "url", "url": info.get("webpage_url") or link,
            "id": info.get("id"), "title": info.get("title"),
            "uploader": info.get("uploader"), "ie_key": info.get("extractor_key"),
        }
        return {**info, "_type": "playlist", "entries": [entry]}

    def _folder_for(self, ydl: yt_dlp.YoutubeDL, info: dict[str, Any]) -> str:
        """The playlist's folder, computed by yt-dlp itself from the same output
        template — so it is byte-identical to what scdl and the old yt-dlp
        command produced, and existing downloads are found rather than refetched."""
        extra = yt_dlp.YoutubeDL._playlist_infodict(info, n_entries=len(info.get("entries") or []))
        sample = {**extra, "playlist_index": 1, "playlist_autonumber": 1,
                  "id": "x", "title": "x", "uploader": "x", "ext": "mp3"}
        path = Path(ydl.prepare_filename(sample)).resolve()
        try:
            rel = path.parent.relative_to(self.output_dir)
        except ValueError:
            return ""
        return "" if str(rel) == "." else rel.as_posix()

    def _prepare(self, pl: _Playlist) -> list[_Track] | None:
        resolved = self._resolve(pl)
        if resolved is None:
            return None
        info, folder = resolved

        entries = [e for e in (info.get("entries") or []) if e]
        indices = info.get("requested_entries") or list(range(1, len(entries) + 1))
        pl.extra = yt_dlp.YoutubeDL._playlist_infodict(info, n_entries=len(entries))
        pl.name = str(info.get("title") or info.get("id") or pl.link)
        pl.folder = folder
        pl.total = len(entries)
        pl.info = {k: v for k, v in info.items()
                   if k not in ("entries", "requested_entries", "thumbnails") and not k.startswith("_")}
        self._save_cover(info, folder)

        st = self.store.open(self.provider, pl.link)
        pl.state = st
        first_sight = st.is_new

        tracks: list[dict[str, Any]] = []
        seen = set()
        for index, entry in zip(indices, entries):
            key = str(entry.get("id") or entry.get("url") or index)
            if key in seen:            # the same track twice in one playlist
                key = f"{key}#{index}"
            seen.add(key)
            artist = entry.get("uploader") or entry.get("channel") or ""
            known = st.track(key) or {}
            tracks.append({
                "key": key, "index": int(index), "id": entry.get("id"),
                # Titles a previous run learned beat a stub with none.
                "title": entry.get("title") or known.get("title") or "",
                "artists": [artist] if artist else (known.get("artists") or []),
                "entry": entry,
            })
        if self.p.backfill and any(not t["title"] for t in tracks):
            try:
                self.p.backfill(self, tracks)
            except Exception as e:
                self.logger.debug(f"Title backfill failed: {e}")

        st.update_meta(name=pl.name, folder=pl.folder, url=pl.link)
        st.sync_tracks(tracks)
        imported = self._seed(st, tracks, folder) if first_sight else 0
        healed = st.heal()
        st.save()

        by_key = {t["key"]: t for t in tracks}
        ui_tracks, items = [], []
        for t in st.ordered():
            status = t.get("status", PENDING)
            ui_tracks.append({
                "num": t.get("index"), "title": t.get("title") or f"Track {t.get('index')}",
                "artists": t.get("artists") or [], "id": t["key"],
                "status": {DONE: "downloaded", UNAVAILABLE: "unavailable"}.get(status, "pending"),
                "error": t.get("error") or "", "archived": status == DONE,
            })
            if status in (PENDING, FAILED):
                items.append(_Track(pl=pl, key=t["key"], index=int(t.get("index") or 0),
                                    entry=by_key[t["key"]]["entry"]))

        s = st.summary()
        note = f" (found {imported} already downloaded)" if imported else ""
        if healed:
            note += f" ({healed} deleted since — fetching again)"
        self.logger.info(f"📋 {self.p.label}: {pl.name} — {s['total']} tracks, {s['done']} done, "
                         f"{len(items)} to fetch{note}")
        self.progress.link_started(self.provider, pl.link, pl.name, pl.total, queued=True)
        self.progress.link_metadata(self.provider, pl.link, ui_tracks)
        return items

    def _seed(self, st: PlaylistState, tracks: list[dict[str, Any]], folder: str) -> int:
        """First sight of a playlist: count what earlier versions already
        downloaded as done, so switching to records doesn't refetch a library.
        Old SoundCloud archives say exactly which ids finished; otherwise a file
        whose number matches the track's playlist position is taken as it."""
        archived = read_legacy_archive(self.store.legacy_archive(st.key)) if self.p.legacy_archive else set()
        files = index_files(self.output_dir / folder) if folder else {}
        n = 0
        for t in tracks:
            path = files.get(t["index"])
            if path is not None or (t.get("id") and str(t["id"]) in archived):
                st.mark(t["key"], DONE, file=path, save=False)
                n += 1
        return n

    # ── Workers ──────────────────────────────────────────────────────

    def _work(self, queue: _WorkQueue) -> None:
        try:
            while True:
                item = queue.get(self._stopping)
                if item is None:
                    break
                try:
                    self._process(item, queue)
                except Exception as e:
                    self.logger.error(f"💥 {self.p.label} worker crashed on {item.pl.name} #{item.index}: {e}")
                    self._record_failure(item, f"Internal error: {e}")
                finally:
                    queue.done()
        finally:
            self._close_ydl()

    def _start_playlist(self, pl: _Playlist) -> None:
        with pl.lock:
            if pl.started:
                return
            pl.started = True
        self.logger.info(f"\n{'=' * 60}")
        self.logger.info(f"[{self.provider.upper()} {pl.position}] {pl.name}")
        self.progress.link_started(self.provider, pl.link, pl.name, pl.total)

    def _process(self, item: _Track, queue: _WorkQueue) -> None:
        pl = item.pl
        if self._copy_duplicate(item):
            return
        if not self.p.limiter.acquire(self._stop):
            queue.put(item, front=True)
            return
        issued = self.p.limiter.stamp()
        self._start_playlist(pl)
        self._emit(pl, "track", {"current": item.index, "total": pl.total})
        self._tls.playlist = pl
        try:
            info = self._ydl().process_ie_result(
                dict(item.entry), download=True,
                # Exactly the context yt-dlp gives each entry when it walks a
                # playlist itself, so %(playlist)s / %(playlist_index)s in the
                # template come out as they always have.
                extra_info={**pl.extra, "playlist_index": item.index, "playlist_autonumber": item.index})
        except Exception as e:
            self._handle_failure(item, classify(e), issued, queue)
            return
        finally:
            self._tls.playlist = None

        staged = self._final_path(info)
        if staged is None or not staged.is_file():
            self._handle_failure(item, Failure("error", "yt-dlp finished without producing a file"),
                                 issued, queue)
            return
        try:
            path = self._promote(staged)
        except OSError as e:
            # Typically the old copy is open in a player (Windows locks it).
            self._handle_failure(item, Failure("error", f"Couldn't replace the existing file: {e}"),
                                 issued, queue)
            return
        assert pl.state is not None
        pl.state.mark(item.key, DONE, file=path, title=(info or {}).get("title"))
        if self.done_index is not None:
            self.done_index.add(self.provider, item.entry.get("id"), path)
        self.p.limiter.on_success()
        self._emit(pl, "track_complete", {"index": item.index, "title": path.stem})
        self._track_finished(pl)

    def _copy_duplicate(self, item: _Track) -> bool:
        """Copy the track from another playlist that already has it. No request,
        so no rate-limit budget spent. True if the track is now done."""
        pl = item.pl
        if self.done_index is None or pl.state is None or not pl.folder:
            return False
        target_dir = self.output_dir / pl.folder
        src = self.done_index.find(self.provider, item.entry.get("id"), not_in=target_dir)
        if src is None:
            return False
        m = re.search(r"%\(playlist_index\)0?(\d+)d", self.p.outtmpl)
        width = int(m.group(1)) if m else 0
        tags: dict[str, Any] = {}
        if self.p.tags_from_playlist:
            tags = {"album": pl.extra.get("playlist"), "album_artist": pl.extra.get("playlist_uploader"),
                    "track": item.index}
        try:
            path = copy_track(src, target_dir / renumber(src.name, item.index, width), **tags)
        except OSError as e:
            self.logger.debug(f"Couldn't copy {src.name}: {e}")
            return False
        self._start_playlist(pl)
        pl.state.mark(item.key, DONE, file=path)
        self._log(pl, f"📎 #{item.index} already downloaded in {src.parent.name} — copied")
        self._emit(pl, "track_complete", {"index": item.index, "title": path.stem, "copied": True})
        self._track_finished(pl)
        return True

    def _save_cover(self, info: dict[str, Any], folder: str) -> None:
        """The playlist's artwork, for the Library. One CDN request, not an API call."""
        if not folder:
            return
        target = self.output_dir / ".icons" / f"{Path(folder).name}.jpg"
        if target.exists():
            return
        thumbs = [t for t in (info.get("thumbnails") or []) if isinstance(t, dict) and t.get("url")]
        url = info.get("thumbnail") or (thumbs[-1]["url"] if thumbs else None)
        if not url:
            return
        try:
            from urllib.request import Request, urlopen
            target.parent.mkdir(parents=True, exist_ok=True)
            with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as resp:
                target.write_bytes(resp.read())
        except Exception as e:
            self.logger.debug(f"Cover fetch failed: {e}")

    def _promote(self, staged: Path) -> Path:
        """Move a finished file from staging over its real location.

        Downloads land in a staging folder first because yt-dlp, told to
        overwrite, deletes the existing file *before* it starts downloading —
        so a cancelled or rate-limited re-download after a reset destroyed the
        old copy and left a .part in its place. Now the old file is only ever
        replaced, atomically, by a complete new one.
        """
        try:
            rel = staged.resolve().relative_to(self.staging.resolve())
        except ValueError:
            return staged   # already outside staging; leave it where it is
        target = self.output_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(5):
            try:
                staged.replace(target)
                return target
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.3 * (attempt + 1))
        return target

    @staticmethod
    def _final_path(info: dict[str, Any] | None) -> Path | None:
        if not info:
            return None
        for d in reversed(info.get("requested_downloads") or []):
            fp = d.get("filepath") or d.get("_filename")
            if fp:
                return Path(fp)
        fp = info.get("filepath") or info.get("_filename")
        return Path(fp) if fp else None

    def _handle_failure(self, item: _Track, f: Failure, issued: float, queue: _WorkQueue) -> None:
        pl = item.pl
        if f.kind == "cancelled":
            queue.put(item, front=True)
            return
        if f.kind == "forbidden" and self.p.forbidden_is_rate_limit:
            if self._try_refresh_auth(pl):
                queue.put(item, front=True)
                return
            f = Failure("rate_limited", f.message)
        if f.kind == "rate_limited":
            self._rate_limited(pl, f, issued)
            self._emit(pl, "track_retry", {"index": item.index})
            queue.put(item, front=True)
            return
        if f.kind == "unavailable":
            assert pl.state is not None
            pl.state.mark(item.key, UNAVAILABLE, error=f.message)
            self._emit(pl, "track_failed", {"index": item.index, "error": f.message[:200],
                                            "unavailable": True})
            self._track_finished(pl)
            return
        item.attempts += 1
        if item.attempts < self.p.max_attempts and not self._stopping():
            self._log(pl, f"🔁 #{item.index} failed ({f.message[:150]}) — "
                          f"retry {item.attempts}/{self.p.max_attempts - 1} later")
            queue.put(item)   # to the back: give whatever it was time to clear
            return
        self._record_failure(item, f.message)

    def _record_failure(self, item: _Track, message: str) -> None:
        pl = item.pl
        if pl.state is not None:
            pl.state.mark(item.key, FAILED, error=message)
        self._emit(pl, "track_failed", {"index": item.index, "error": message[:200]})
        self._track_finished(pl)

    def _rate_limited(self, pl: _Playlist, f: Failure, issued: float) -> None:
        if not self.p.limiter.on_rate_limited(f.retry_after, issued_at=issued, reason=f.message):
            return   # another worker already started this cooldown
        snap = self.p.limiter.snapshot()
        msg = (f"⏳ {self.p.label} is rate-limiting — pausing all {self.p.label} downloads for "
               f"{snap['paused_for']:.0f}s, then continuing at {snap['rate']:.0f} tracks/min")
        self.logger.warning(msg)
        self._log(pl, msg)
        if snap["hits"] > self.p.give_up_after:
            self._halt.set()
            self.logger.error(
                f"🛑 {self.p.label} is still refusing after {snap['hits']} cooldowns — stopping. "
                f"Everything not downloaded yet is recorded as still to do; re-run later to "
                f"continue from exactly here.")

    def _try_refresh_auth(self, pl: _Playlist) -> bool:
        """One fresh credential per run: a 403 is often just a stale client id."""
        with self._lock:
            if self._auth_refreshed or not self.p.refresh_auth:
                return False
            self._auth_refreshed = True
        self._log(pl, f"🔑 403 from {self.p.label} — retrying with a fresh client id")
        try:
            ok = bool(self.p.refresh_auth(self))
        except Exception as e:
            self.logger.warning(f"{self.p.label} credential refresh failed: {e}")
            ok = False
        if ok:
            with self._lock:
                self._generation += 1
        return ok

    # ── Completion ───────────────────────────────────────────────────

    def _track_finished(self, pl: _Playlist) -> None:
        with pl.lock:
            pl.remaining -= 1
            finish = pl.enqueued_all and pl.remaining <= 0 and not pl.finished
        if finish:
            self._finish(pl)

    def _finish(self, pl: _Playlist) -> None:
        with pl.lock:
            if pl.finished:
                return
            pl.finished = True

        label = self.p.label
        if pl.state is None:
            # Never resolved: Cancel, the lane giving up before reaching it
            # (still to do — not a failure of this playlist), or a dead link.
            pl.code = (CODE_CANCELLED if self.cancel.is_set()
                       else CODE_PARTIAL if self._halt.is_set() else CODE_FAILED)
            pl.name = pl.name or pl.link.split("?")[0].rstrip("/").split("/")[-1]
        else:
            pl.state.save()
            s = pl.state.summary()
            extra = f" · {s['unavailable']} unavailable" if s["unavailable"] else ""
            if s["left"] == 0:
                pl.code = CODE_OK
                self.logger.info(f"✅ {label}: {pl.name} — all {s['done']} tracks downloaded{extra}")
            elif self.cancel.is_set():
                pl.code = CODE_CANCELLED
                self.logger.info(f"⏹️ {label}: {pl.name} — stopped at {s['done']}/{s['total']}, "
                                 f"{s['left']} still to do")
            else:
                pl.code = CODE_PARTIAL
                self.logger.warning(
                    f"⚠️ {label}: {pl.name} — {s['done']}/{s['total']} done · {s['failed']} failed · "
                    f"{s['pending']} still to do{extra}. Re-run to continue from here.")
            self._write_metadata(pl)
        self.progress.link_complete(self.provider, pl.link, pl.name or pl.link, pl.code)
        if self._task:
            self.progress.advance(self._task)

    def _write_metadata(self, pl: _Playlist) -> None:
        """Playlist info + the per-track record, in .metadata/ as before."""
        if pl.state is None:
            return
        name = Path(pl.folder).name if pl.folder else pl.name
        if not name:
            return
        meta_dir = self.output_dir / ".metadata"
        try:
            meta_dir.mkdir(parents=True, exist_ok=True)
            data = _jsonable({**pl.info, "tracks": pl.state.ordered()})
            (meta_dir / f"{name}.json").write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError) as e:
            self.logger.debug(f"Could not write metadata for {name}: {e}")
