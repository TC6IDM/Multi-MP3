import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from src.dedupe import DoneIndex, copy_track, renumber
from src.downloaders.soundcloud import SoundCloudDownloader
from src.downloaders.spotify import SpotifyDownloader, link_kind
from src.downloaders.youtube import YouTubeDownloader
from src.downloaders.ytdlp_engine import YtDlpEngine
from src.models import CODE_CANCELLED, CODE_FAILED, CODE_OK, CODE_PARTIAL, LinkResult
from src.ratelimit import AdaptiveRateLimiter
from src.settings import DEFAULT_SPEED, env_rate_override, lane
from src.state import DONE, FAILED, UNAVAILABLE, PlaylistState, StateStore
from src.tui import DownloadProgress
from src.utils import read_links

ENGINE_PROVIDERS = ("soundcloud", "youtube")


class Coordinator:
    """Orchestrates downloading across Spotify, YouTube, and SoundCloud.

    Every provider runs in its own lane, all at the same time. They hit
    different services, so there's no reason for one to wait on another — the
    old single "parallel" switch made SoundCloud's rate limit everyone's
    problem. Each lane sets its own pace: SoundCloud and YouTube through a
    shared adaptive rate limiter, Spotify through spotdl's thread count.
    """

    def __init__(self, output_dir: Path, logger: logging.Logger,
                 spotify_client_id: str = "", spotify_client_secret: str = "",
                 speed: str = DEFAULT_SPEED, max_workers: int | None = None,
                 use_tui: bool = False,
                 progress: DownloadProgress | None = None,
                 cancel_event: threading.Event | None = None):
        self.output_dir = output_dir.resolve()
        self.logger = logger
        self.spotify_client_id = spotify_client_id
        self.spotify_client_secret = spotify_client_secret
        self.speed = speed
        self.max_workers = max_workers
        self.tui = progress or DownloadProgress(enabled=use_tui)
        self.cancel = cancel_event or threading.Event()
        self.store = StateStore(self.output_dir)
        # Every track already downloaded anywhere, so a song that's in several
        # playlists is fetched once and copied into the rest.
        self.done_index = DoneIndex(self.store)
        self._spotify: SpotifyDownloader | None = None
        self._spotify_lock = threading.Lock()

    def _get_spotify(self) -> SpotifyDownloader:
        with self._spotify_lock:
            if self._spotify is None:
                self._spotify = SpotifyDownloader(self.output_dir, self.logger,
                                                  self.spotify_client_id, self.spotify_client_secret)
                self._spotify.cancel = self.cancel
                self._spotify.threads = lane("spotify", self.speed).threads or 6
            return self._spotify

    # ── SoundCloud / YouTube ─────────────────────────────────────────

    def _limiter(self, provider: str) -> AdaptiveRateLimiter:
        cfg = lane(provider, self.speed, self.max_workers)
        return AdaptiveRateLimiter(
            provider, rate=cfg.rate, min_rate=cfg.min_rate, max_rate=cfg.max_rate,
            state_file=self.output_dir / ".cache" / f"ratelimit-{provider}.json",
            # An explicitly configured rate beats the one learned last run.
            use_learned=env_rate_override(provider) is None,
            on_event=lambda kind, data: self.tui.provider_status(provider, kind, data),
        )

    def _run_engine_lane(self, provider: str, links: list[str]) -> list[LinkResult]:
        cfg = lane(provider, self.speed, self.max_workers)
        limiter = self._limiter(provider)
        downloader = (SoundCloudDownloader(self.output_dir, self.logger) if provider == "soundcloud"
                      else YouTubeDownloader(self.output_dir, self.logger))
        profile = downloader.profile(cfg, limiter)
        self.logger.info(f"🎯 {provider}: {len(links)} playlist(s) · {cfg.workers} workers · "
                         f"starting at {limiter.rate:.0f} tracks/min (max {cfg.max_rate:.0f})")
        self.tui.provider_status(provider, "rate", {"rate": round(limiter.rate, 1),
                                                    "workers": cfg.workers})
        engine = YtDlpEngine(profile, self.output_dir, self.logger, self.store, self.tui, self.cancel,
                             done_index=self.done_index)
        return engine.run(links)

    # ── Spotify ──────────────────────────────────────────────────────

    def _run_spotify_lane(self, links: list[str]) -> list[LinkResult]:
        cfg = lane("spotify", self.speed, self.max_workers)
        total = len(links)
        task_name = f"spotify ({total} links)"
        self.tui.add_task(task_name, total=total)
        self.logger.info(f"🎯 spotify: {total} playlist(s) · {cfg.workers} at a time · "
                         f"{cfg.threads} spotdl threads")
        results: list[LinkResult] = []
        with ThreadPoolExecutor(max_workers=max(1, min(cfg.workers, total)),
                                thread_name_prefix="spotify") as executor:
            futures = {executor.submit(self._download_spotify_link, link, i, total): link
                       for i, link in enumerate(links, 1)}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    link = futures[future]
                    self.logger.error(f"💥 spotify crashed on {link}: {e}")
                    results.append(LinkResult("spotify", link, link, CODE_FAILED))
                self.tui.advance(task_name)
        return results

    def _download_spotify_link(self, link: str, index: int, total: int) -> LinkResult:
        """One Spotify link, entirely through spotdl.

        spotdl reads the playlist and hands its song list over mid-run (see
        spotdl_runner.py). That's the moment the record is synced, tracks
        already downloaded for another playlist are copied in, old copies of
        tracks being re-downloaded are set aside, and spotdl is told which
        songs to skip. After the run the record is reconciled against the disk.
        """
        provider = "spotify"
        downloader = self._get_spotify()
        kind = link_kind(link)
        short = link.split("?")[0].rstrip("/").split("/")[-1] or link

        def forward(evt: str, data: dict) -> None:
            self.tui.track_progress(provider, link, evt, data)

        if self.cancel.is_set():
            self.tui.link_complete(provider, link, short, CODE_CANCELLED)
            return LinkResult(provider, link, short, CODE_CANCELLED, index, total)

        self.logger.info(f"\n{'=' * 60}")
        self.logger.info(f"[SPOTIFY {index}/{total}] {link.split('?')[0]}")
        self.tui.link_started(provider, link, short, 0)

        ctx: dict[str, Any] = {}

        def on_songs(songs: list[dict[str, Any]]) -> list[str]:
            tracks = downloader.tracks_from_songs(songs, kind)
            name = downloader.list_name(songs, kind, short)
            folder = downloader.folder_for(songs, kind)
            st = downloader.prepare_state(self.store, link, name, folder, tracks)
            copied = self._copy_spotify_duplicates(st, folder, tracks, kind)
            moved = downloader.set_aside_stale(st, folder, tracks)
            ctx.update(st=st, folder=folder, tracks=tracks, name=name)
            downloader.save_cover(songs[0].get("cover_url") if songs else None, folder)

            s = st.summary()
            note = f" ({copied} copied from other playlists)" if copied else ""
            self.logger.info(f"📋 Spotify: {name} — {s['total']} tracks, {s['done']} done, "
                             f"{s['left']} to fetch{note}")
            if moved:
                self.logger.info(f"📦 Set aside {moved} old cop{'y' if moved == 1 else 'ies'} "
                                 f"until their re-downloads finish")
            self.tui.link_started(provider, link, name, len(tracks))
            self.tui.link_metadata(provider, link, self._ui_tracks(st))
            return downloader.skip_urls(st)

        run_started = time.time()
        code, errors_file = 1, None
        try:
            code, errors_file = downloader.download(link, on_progress=forward, on_songs=on_songs)
            if code not in (0, 130):
                self.logger.warning(f"spotdl exited with code {code}")
            if ctx and not self.cancel.is_set():
                # spotdl commits to one YouTube candidate per track and gives up
                # if it fails, so retry those by searching YouTube ourselves.
                self._retry_failed_spotify(downloader, ctx["folder"], ctx["tracks"], kind,
                                           errors_file, forward)
        except Exception as e:
            self.logger.error(f"💥 {provider} error on {link}: {e}")
            code = 1

        st: PlaylistState | None = ctx.get("st")
        name = ctx.get("name", short)
        if st is None:
            # spotdl never got as far as listing the songs (or ran without the
            # handshake): no record to reconcile, only its exit code to go on.
            final = CODE_CANCELLED if self.cancel.is_set() else (CODE_OK if code == 0 else CODE_FAILED)
        else:
            folder, tracks = ctx["folder"], ctx["tracks"]
            clean_exit = code == 0 and not self.cancel.is_set()
            downloader.reconcile(st, folder, tracks, run_started, errors_file,
                                 completed=not self.cancel.is_set() and code != 130,
                                 check_truncation=not clean_exit)
            restored = downloader.restore_stale(st, folder, tracks)
            if restored:
                self.logger.info(f"📦 Put back {restored} old cop{'y' if restored == 1 else 'ies'} "
                                 f"whose re-download didn't finish")
            for t in st.ordered():
                if t.get("status") == DONE and t.get("id"):
                    path = st.file_path(t)
                    if path is not None:
                        self.done_index.add(provider, t["id"], path)
            s = st.summary()
            if s["left"] == 0:
                final = CODE_OK
                self.logger.info(f"✅ Spotify: {name} — all {s['done']} tracks downloaded")
            elif self.cancel.is_set():
                final = CODE_CANCELLED
            else:
                final = CODE_PARTIAL
                self.logger.warning(f"⚠️ Spotify: {name} — {s['done']}/{s['total']} done · "
                                    f"{s['failed']} failed. Re-run to retry those.")
            # The record is exact now; let the track list show exactly that.
            self.tui.link_metadata(provider, link, self._ui_tracks(st))

        self.tui.link_complete(provider, link, name, final)
        return LinkResult(provider, link, name, final, index, total)

    def _copy_spotify_duplicates(self, st: PlaylistState, folder: str,
                                 tracks: list[dict[str, Any]], kind: str) -> int:
        """Copy in tracks another playlist already downloaded, so spotdl skips
        them. spotdl tags songs with their real album, so the copy needs no
        retagging — only the position at the front of the filename changes."""
        if not folder:
            return 0
        target_dir = self.output_dir / folder
        width = len(str(max((t["index"] for t in tracks), default=0))) if kind == "playlist" else 2
        copied = 0
        for t in st.ordered():
            if t.get("status") in (DONE, UNAVAILABLE) or not t.get("id"):
                continue
            src = self.done_index.find("spotify", t["id"], not_in=target_dir)
            if src is None:
                continue
            number = t.get("file_index")
            name = renumber(src.name, number, width if number is not None else 0)
            try:
                dst = copy_track(src, target_dir / name)
            except OSError as e:
                self.logger.debug(f"Couldn't copy {src.name}: {e}")
                continue
            st.mark(t["key"], DONE, file=dst, save=False)
            copied += 1
        if copied:
            st.save()
        return copied

    @staticmethod
    def _ui_tracks(st: PlaylistState) -> list[dict[str, Any]]:
        return [{
            "num": t.get("index"), "title": t.get("title") or f"Track {t.get('index')}",
            "artists": t.get("artists") or [], "id": t.get("id") or "",
            "status": {DONE: "downloaded", UNAVAILABLE: "unavailable",
                       FAILED: "failed"}.get(t.get("status", ""), "pending"),
            "error": t.get("error") or "", "archived": t.get("status") == DONE,
        } for t in st.ordered()]

    # ── Recovery pass ────────────────────────────────────────────────

    def _retry_failed_spotify(self, downloader: SpotifyDownloader, playlist_dir: str,
                              tracks: list[dict[str, Any]], kind: str, errors_file: Path | None,
                              emit: Any) -> None:
        """Re-attempt tracks spotdl couldn't fetch, via a direct YouTube search.

        spotdl resolves each track to a single YouTube URL and reports failure
        if that one is unavailable (age-gated, region-locked, taken down). A
        plain search usually turns up a working upload of the same song.
        """
        if errors_file is None:
            return
        failed_ids = downloader._parse_spotdl_errors(errors_file)
        if not failed_ids:
            return

        by_id = {t["id"]: t for t in tracks if t.get("id")}
        # Same numbering as spotdl's {list-position} / {track-number}.
        padding = len(str(max((t["index"] for t in tracks), default=0))) if kind == "playlist" else 2
        targets = [(tid, by_id[tid]) for tid in failed_ids if tid in by_id]
        if not targets:
            return

        # Route retry output to the owning playlist's log pane rather than the
        # global one, matching how subprocess output is handled.
        def say(msg: str) -> None:
            if emit:
                emit("log", {"line": msg})
            else:
                self.logger.info(msg)

        say(f"🔁 {len(targets)} track(s) failed — retrying via YouTube search...")

        recovered = 0
        for tid, t in targets:
            if self.cancel.is_set():
                break
            title = t.get("title") or ""
            artists = t.get("artists") or []
            if not title:
                continue
            if emit:
                emit("track_retry", {"track_id": tid, "title": title})
            try:
                ok = downloader.search_and_download(
                    title=title, artists=artists, playlist_dir=playlist_dir,
                    position=t.get("file_index"), padding=padding, on_log=say,
                )
            except Exception as e:
                say(f"Retry crashed for {title}: {e}")
                ok = False

            if ok:
                recovered += 1
                if emit:
                    emit("track_complete", {"title": title, "recovered": True})
            elif emit:
                emit("track_failed", {
                    "track_id": tid, "title": title,
                    "error": "No working source found on spotdl's match or a YouTube search",
                })

        say(f"🔁 Recovered {recovered}/{len(targets)} previously failed track(s)")
        # One line in the global log so the run summary still reflects it
        self.logger.info(f"🔁 recovered {recovered}/{len(targets)} failed track(s)")

    # ── Cross-provider orchestration ─────────────────────────────────

    def _run_lane(self, provider: str, links: list[str]) -> list[LinkResult]:
        self.logger.info(f"🎯 Starting {len(links)} {provider} link(s)...")
        if provider in ENGINE_PROVIDERS:
            results = self._run_engine_lane(provider, links)
        else:
            results = self._run_spotify_lane(links)
        worst = max((r.code for r in results), default=0)
        self.logger.info(f"🏁 {provider} lane done (worst code: {worst})")
        return results

    def process_all(self, input_file: Path, providers: list[str] | None = None) -> int:
        """Run every provider lane concurrently. Returns the worst playlist code."""
        if providers is None:
            providers = ["soundcloud", "youtube", "spotify"]

        links_by_provider = read_links(input_file, self.logger)
        active = [(p, links_by_provider.get(p, [])) for p in providers if links_by_provider.get(p)]
        if not active:
            self.logger.info("ℹ️ No links found for any provider")
            return 0

        self.logger.info(f"⚙️ Speed: {self.speed} · {len(self.done_index)} tracks already on disk")
        exit_code = 0
        with self.tui:
            with ThreadPoolExecutor(max_workers=len(active), thread_name_prefix="lane") as executor:
                futures = {executor.submit(self._run_lane, p, links): p for p, links in active}
                pending = set(futures)
                while pending:
                    try:
                        # Waited in 1s slices: a wait with no timeout can't be
                        # interrupted by Ctrl-C on Windows.
                        for future in as_completed(set(pending), timeout=1.0):
                            pending.discard(future)
                            provider = futures[future]
                            try:
                                results = future.result()
                                exit_code = max([exit_code] + [r.code for r in results])
                            except Exception as e:
                                self.logger.error(f"💥 {provider} lane crashed: {e}")
                                exit_code = max(exit_code, 1)
                    except TimeoutError:
                        continue
                    except KeyboardInterrupt:
                        # Ctrl-C in the CLI: stop cleanly, so whatever finished
                        # is recorded and everything else stays "to do".
                        self.logger.warning("⏹️ Stopping — finishing up and saving progress...")
                        self.cancel.set()

        self._log_summary()
        self.logger.info(f"🏁 All providers complete (exit: {exit_code})")
        return exit_code

    def totals(self) -> dict[str, int]:
        """Track counts across every playlist this run touched."""
        totals = {"done": 0, "failed": 0, "pending": 0, "unavailable": 0, "total": 0}
        for st in self.store.opened():
            s = st.summary()
            for k in totals:
                totals[k] += int(s.get(k, 0))
        return totals

    def _log_summary(self) -> None:
        t = self.totals()
        if not t["total"]:
            return
        self.logger.info(
            f"📊 {t['done']}/{t['total']} tracks done · {t['failed']} failed · "
            f"{t['pending']} still to do · {t['unavailable']} unavailable")
