"""Rich-based progress display for Multi-MP3."""

from __future__ import annotations

from typing import Any, Dict, List

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TaskID,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
    from rich.table import Table
    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False


class DownloadProgress:
    """Progress display using Rich. Falls back to no-op if rich is not installed."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and _RICH_AVAILABLE
        self._console: Console | None = None
        self._progress: Progress | None = None
        self._tasks: dict[str, TaskID] = {}

        if self.enabled:
            self._console = Console()
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                BarColumn(bar_width=40),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=self._console,
            )

    def start(self) -> None:
        if self._progress:
            self._progress.start()

    def stop(self) -> None:
        if self._progress:
            self._progress.stop()

    def link_started(self, provider: str, link: str, name: str, total_tracks: int = 0) -> None:
        """Called when a single link begins downloading. No-op in CLI TUI."""
        pass

    def link_complete(self, provider: str, link: str, name: str, code: int) -> None:
        """Called when a single link finishes downloading. No-op in CLI TUI."""
        pass

    def track_progress(self, provider: str, link: str, event: str, data: Dict[str, Any]) -> None:
        """Called for per-track progress events from subprocess output. No-op in CLI TUI."""
        pass

    def link_metadata(self, provider: str, link: str, tracks: List[Dict[str, Any]]) -> None:
        """Provide the full track list for a playlist (e.g. from Spotify API). No-op in CLI TUI."""
        pass

    def __enter__(self) -> "DownloadProgress":
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    def add_task(self, name: str, total: int = 1) -> str:
        """Add a task to the progress bar. Returns a task_id string."""
        if self._progress:
            task_id = self._progress.add_task(f"[cyan]{name}", total=total, completed=0)
            self._tasks[name] = task_id
            return name
        return name

    def advance(self, name: str) -> None:
        """Mark one item complete for the named task."""
        if self._progress and name in self._tasks:
            self._progress.advance(self._tasks[name])

    def update_description(self, name: str, description: str) -> None:
        """Update the display description of a task."""
        if self._progress and name in self._tasks:
            self._progress.update(self._tasks[name], description=description)

    def print_summary(self, title: str, items: List[str]) -> None:
        """Print a summary table to the console."""
        if not self._console:
            return

        table = Table(title=title, title_style="bold yellow")
        table.add_column("Status", style="bold")
        table.add_column("Playlist / Track")

        for item in items:
            table.add_row("✅", item)

        self._console.print(table)
