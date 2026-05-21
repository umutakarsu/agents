"""Ingest a GitHub repo's markdown/text files.

    python scripts/ingest_github.py umutakarsu/agents --workspace acme
    GITHUB_TOKEN=ghp_xxx python scripts/ingest_github.py private/repo --ref main

Re-running on the same ref reuses every embedding via the content-hash gate.
"""

import argparse

from memlayer.connectors.github import read_repo
from memlayer.ingest import ingest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("slug", help="owner/repo, e.g. umutakarsu/agents")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--path-prefix", default="")
    ap.add_argument("--workspace", default="default")
    ap.add_argument(
        "--principals",
        default="group:all",
        help="comma-separated, e.g. user:ali,group:eng",
    )
    args = ap.parse_args()

    owner, repo = args.slug.split("/", 1)
    items = read_repo(
        owner,
        repo,
        ref=args.ref,
        path_prefix=args.path_prefix,
        allowed_principals=args.principals.split(","),
    )
    stats = ingest(items, workspace=args.workspace)

    print(f"items:               {stats.items}")
    print(f"chunks:              {stats.chunks}")
    print(f"embeddings computed: {stats.embeddings_computed}  (cost)")
    print(f"embeddings reused:   {stats.embeddings_reused}  (dedup saved)")


if __name__ == "__main__":
    main()
