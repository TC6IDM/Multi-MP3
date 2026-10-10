"""Versions, the one-click yt-dlp update, and housekeeping for the System tab."""

from __future__ import annotations

import functools
import json
import logging
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from src.utils import deno_path, spotdl_command, spotdl_python

# yt-dlp's extractors break whenever a site changes, and a fix usually ships
# within days — so this one package is allowed to move ahead of the lockfile.
YTDLP_SPEC = "yt-dlp[default,curl-cffi]"


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def _package_version(python: str, package: str) -> str | None:
    """A package's installed version as seen by a given interpreter (fresh
    process, so it reflects an update this server hasn't reloaded yet)."""
    try:
        r = _run([python, "-c", f"import importlib.metadata as m; print(m.version({package!r}))"], 60)
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


@functools.lru_cache(maxsize=1)
def _deno_version() -> str | None:
    deno = deno_path()
    if not deno:
        return None
    try:
        r = _run([deno, "--version"], 30)
        first = (r.stdout or "").splitlines()[:1]
        return first[0].replace("deno ", "").split(" ")[0] if first else None
    except (OSError, subprocess.SubprocessError):
        return None


def _norm_version(v: str | None) -> str:
    """'2026.08.19' (yt-dlp's own string) and '2026.8.19' (pip's) are the same."""
    return ".".join(str(int(p)) if p.isdigit() else p for p in (v or "").split("."))


def versions() -> dict[str, Any]:
    import yt_dlp.version

    spotdl_py = spotdl_python()
    loaded = _norm_version(yt_dlp.version.__version__)
    installed = _norm_version(_package_version(sys.executable, "yt-dlp")) or loaded
    return {
        "python": platform.python_version(),
        "yt_dlp": installed,
        "yt_dlp_loaded": loaded,
        # Updated on disk but this server still runs the old one in memory.
        "restart_required": installed != loaded,
        "spotdl": _package_version(spotdl_py, "spotdl") if spotdl_py else None,
        "spotdl_yt_dlp": _package_version(spotdl_py, "yt-dlp") if spotdl_py else None,
        "spotdl_found": bool(spotdl_command()),
        "deno": _deno_version(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
    }


def latest_ytdlp() -> str | None:
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/yt-dlp/json", timeout=10) as r:
            return str(json.load(r)["info"]["version"])
    except Exception:
        return None


def _install(python: str, spec: str) -> tuple[bool, str]:
    """pip install -U into the environment that owns `python`.

    uv-built environments have no pip, so fall back to `uv pip`."""
    r = _run([python, "-m", "pip", "install", "--upgrade", spec])
    if r.returncode == 0:
        return True, ""
    if "No module named pip" in (r.stderr + r.stdout):
        beside = Path(sys.executable).parent / ("uv.exe" if sys.platform == "win32" else "uv")
        uv = shutil.which("uv") or (str(beside) if beside.is_file() else None)
        if uv:
            r = _run([uv, "pip", "install", "--python", python, "--upgrade", spec])
            if r.returncode == 0:
                return True, ""
    return False, (r.stderr or r.stdout).strip()[-500:]


def update_ytdlp(logger: logging.Logger | None = None) -> dict[str, Any]:
    """Upgrade yt-dlp for the app and for spotdl's own environment."""
    result: dict[str, Any] = {"before": _package_version(sys.executable, "yt-dlp")}
    ok, err = _install(sys.executable, YTDLP_SPEC)
    result.update(ok=ok, error=err, after=_package_version(sys.executable, "yt-dlp"))
    spotdl_py = spotdl_python()
    if spotdl_py:
        before = _package_version(spotdl_py, "yt-dlp")
        sok, serr = _install(spotdl_py, "yt-dlp")
        result["spotdl"] = {"ok": sok, "error": serr, "before": before,
                            "after": _package_version(spotdl_py, "yt-dlp")}
    if logger:
        logger.info(f"⬆️ yt-dlp {result['before']} → {result['after']}")
    result["restart_required"] = result["after"] != result["before"]
    return result


def cleanup(output_dir: Path, busy: bool = False, error_age_days: int = 30) -> dict[str, int]:
    """Delete leftovers nobody needs: finished jobs' input files, old spotdl
    error lists, and (when nothing is running) interrupted downloads."""
    removed, freed = 0, 0

    def rm(p: Path) -> None:
        nonlocal removed, freed
        try:
            size = p.stat().st_size
            p.unlink()
            removed += 1
            freed += size
        except OSError:
            pass

    cutoff = time.time() - error_age_days * 86400
    if not busy:
        for p in (output_dir / ".web").glob("input_*.txt"):
            rm(p)
    for p in (output_dir / ".errors").glob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                rm(p)
        except OSError:
            pass
    partial = output_dir / ".partial"
    if not busy and partial.is_dir():
        for p in list(partial.rglob("*")):
            # Set-aside Spotify copies are the only copy of a file until their
            # playlist next runs — never deleted here.
            if p.is_file() and "spotify-set-aside" not in p.parts:
                rm(p)
    return {"removed": removed, "freed_bytes": freed}


def folder_stats(folder: Path) -> tuple[int, int]:
    """(mp3 count, total bytes) for one playlist folder."""
    count = size = 0
    try:
        for p in folder.iterdir():
            if p.suffix.lower() == ".mp3":
                try:
                    size += p.stat().st_size
                    count += 1
                except OSError:
                    pass
    except OSError:
        pass
    return count, size
