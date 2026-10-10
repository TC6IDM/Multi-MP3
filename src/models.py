from dataclasses import dataclass

# Per-playlist outcome codes, shared by the coordinator, the engine and the UI.
CODE_OK = 0          # every track done (or unavailable at the source)
CODE_FAILED = 1      # nothing usable: the playlist couldn't even be resolved
CODE_PARTIAL = 2     # some tracks failed or are still to do — re-run to continue
CODE_CANCELLED = 3   # stopped by Cancel / Ctrl-C; the rest stays pending


@dataclass
class LinkResult:
    """Outcome of one playlist link."""
    provider: str
    link: str
    playlist_name: str
    code: int
    index: int = 0
    total: int = 0
