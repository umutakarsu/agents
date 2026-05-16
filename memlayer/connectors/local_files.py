"""Local-files connector: the simplest possible source (zero auth) for proving
the foundation. Real connectors (GitHub, Slack, MCP) implement the same shape:
walk a source, yield SourceItems."""

from datetime import datetime, timezone
from pathlib import Path

from memlayer.ingest import SourceItem

_EXTS = {".md", ".txt"}


def read_dir(path: str, allowed_principals: list[str] | None = None) -> list[SourceItem]:
    root = Path(path)
    items: list[SourceItem] = []
    for f in sorted(root.rglob("*")):
        if f.suffix.lower() not in _EXTS or not f.is_file():
            continue
        stat = f.stat()
        items.append(
            SourceItem(
                source="local",
                source_id=str(f.relative_to(root)),
                text=f.read_text(encoding="utf-8", errors="replace"),
                occurred_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                allowed_principals=allowed_principals or ["group:all"],
                payload={"path": str(f)},
            )
        )
    return items
