"""GitHub connector: pulls markdown/text from a repo via the REST API. Same
shape as local_files -- walk the source, yield SourceItems. Stdlib only; auth
via GITHUB_TOKEN env var (anonymous works on public repos, rate-limited)."""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from datetime import datetime
from urllib.parse import quote

from memlayer.ingest import SourceItem

_API = "https://api.github.com"
_EXTS = (".md", ".txt")


def _get(url: str, token: str | None) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def read_repo(
    owner: str,
    repo: str,
    ref: str = "main",
    path_prefix: str = "",
    token: str | None = None,
    allowed_principals: list[str] | None = None,
) -> list[SourceItem]:
    token = token or os.environ.get("GITHUB_TOKEN")

    # Resolve ref -> commit once so source_id is stable and occurred_at is a
    # single snapshot time across the whole batch (cheaper than per-file).
    commit = _get(f"{_API}/repos/{owner}/{repo}/commits/{quote(ref)}", token)
    sha = commit["sha"]
    occurred_at = datetime.fromisoformat(
        commit["commit"]["committer"]["date"].replace("Z", "+00:00")
    )

    tree = _get(
        f"{_API}/repos/{owner}/{repo}/git/trees/{sha}?recursive=1", token
    )

    items: list[SourceItem] = []
    for entry in tree.get("tree", []):
        if entry.get("type") != "blob":
            continue
        path = entry["path"]
        if path_prefix and not path.startswith(path_prefix):
            continue
        if not path.lower().endswith(_EXTS):
            continue
        blob = _get(
            f"{_API}/repos/{owner}/{repo}/git/blobs/{entry['sha']}", token
        )
        if blob.get("encoding") != "base64":
            continue
        text = base64.b64decode(blob["content"]).decode(
            "utf-8", errors="replace"
        )
        items.append(
            SourceItem(
                source="github",
                source_id=f"{owner}/{repo}:{path}",
                text=text,
                occurred_at=occurred_at,
                allowed_principals=allowed_principals or ["group:all"],
                payload={
                    "owner": owner,
                    "repo": repo,
                    "ref": ref,
                    "commit": sha,
                    "path": path,
                    "blob_sha": entry["sha"],
                    "url": f"https://github.com/{owner}/{repo}/blob/{sha}/{path}",
                },
            )
        )
    return items
