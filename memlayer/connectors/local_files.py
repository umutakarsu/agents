"""Local-files connector: the simplest possible source (zero auth) for proving
the foundation. Real connectors (GitHub, Slack, MCP) implement the same shape:
walk a source, yield SourceItems."""

from datetime import datetime, timezone
from pathlib import Path

from memlayer.ingest import SourceItem

_EXTS = {".md", ".txt"}


def read_dir(path: str, allowed_principals: list[str] | None = None) -> list[SourceItem]:
    # Resolve root once. rglob() follows symlinks by default, so a planted
    # symlink inside an allowed sample dir (e.g. sample_docs/leak.txt ->
    # /etc/passwd) would otherwise be slurped into the database. Two
    # defenses: (1) refuse anything that IS a symlink, (2) verify each
    # candidate's resolved path is still under the resolved root, so a
    # directory symlink that lands outside root is rejected too.
    # Skip on failure -- don't raise -- so a planted symlink can't DoS a
    # legitimate ingest.
    root = Path(path)
    root_resolved = root.resolve()
    items: list[SourceItem] = []
    for f in sorted(root.rglob("*")):
        if f.is_symlink():
            continue
        if f.suffix.lower() not in _EXTS or not f.is_file():
            continue
        try:
            resolved = f.resolve()
        except OSError:
            continue
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            # f resolves outside the root (e.g. via a parent symlink).
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
