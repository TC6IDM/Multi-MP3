"""links.txt as a list of entries the Dashboard can edit — without losing the file's shape.

The file stays plain text you can still edit by hand: one link per line, an
optional label after it (or a Markdown `[label](url)`), and a leading `#` to
switch a link off. Comments and blank lines are kept exactly where they were,
so the grouping you've given the file survives the Dashboard editing it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from src.state import playlist_key
from src.utils import clean_url, provider_for

_MD_LINK = re.compile(r"^\[(?P<label>[^\]]*)\]\((?P<url>[^)\s]+)\)\s*(?P<rest>.*)$")


@dataclass
class LinkEntry:
    line: int            # position in the file (entries and raw lines together)
    url: str
    provider: str
    label: str
    enabled: bool
    markdown: bool       # written back as [label](url)
    key: str             # the playlist's record key

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def parse(text: str) -> list[LinkEntry | str]:
    """Each line → a LinkEntry, or the raw line itself (comments, blanks)."""
    out: list[LinkEntry | str] = []
    for i, raw in enumerate(text.splitlines()):
        stripped = raw.strip()
        enabled = not stripped.startswith("#")
        body = stripped.lstrip("#").strip()
        url = clean_url(body) if body else ""
        provider = provider_for(url) if url else None
        if not url or provider is None:
            out.append(raw)
            continue
        m = _MD_LINK.match(body)
        if m and m.group("url") == url:
            label, markdown = (m.group("label") + " " + m.group("rest")).strip(), True
        else:
            label = body.replace(url, "", 1).strip()
            markdown = False
        out.append(LinkEntry(line=i, url=url, provider=provider, label=label, enabled=enabled,
                             markdown=markdown, key=playlist_key(provider, url)))
    return out


def entries(text: str) -> list[LinkEntry]:
    return [e for e in parse(text) if isinstance(e, LinkEntry)]


def render_entry(e: LinkEntry) -> str:
    prefix = "" if e.enabled else "#"
    if e.markdown:
        return f"{prefix}[{e.label}]({e.url})"
    return f"{prefix}{e.url}" + (f" {e.label}" if e.label else "")


def apply_edits(text: str, edited: list[dict[str, object]]) -> str:
    """Rewrite the file from the Dashboard's list, keeping raw lines in place.

    `edited` is the complete list of links, in order. Entries that still exist
    keep their line; removed ones disappear; new ones go at the end.
    """
    parsed = parse(text)
    by_line = {e.line: e for e in parsed if isinstance(e, LinkEntry)}
    wanted: dict[int, LinkEntry] = {}
    added: list[LinkEntry] = []
    for item in edited:
        url = clean_url(str(item.get("url", "")).strip())
        provider = provider_for(url) if url else None
        if not url or provider is None:
            continue
        entry = LinkEntry(
            line=-1, url=url, provider=provider,
            label=" ".join(str(item.get("label", "")).split()),   # one line, no stray breaks
            enabled=bool(item.get("enabled", True)),
            markdown=bool(item.get("markdown", False)),
            key=playlist_key(provider, url),
        )
        line = item.get("line")
        if isinstance(line, int) and line in by_line:
            entry.line = line
            wanted[line] = entry
        else:
            added.append(entry)

    lines: list[str] = []
    for i, e in enumerate(parsed):
        if isinstance(e, LinkEntry):
            if i in wanted:
                lines.append(render_entry(wanted[i]))
        else:
            lines.append(e)
    lines.extend(render_entry(e) for e in added)
    return "\n".join(lines) + ("\n" if lines else "")
