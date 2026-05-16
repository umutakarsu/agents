"""Ingest a local folder. Run it twice on unchanged files to see the dedup
gate work: the second run computes 0 embeddings, reuses all.

    python scripts/ingest_local.py sample_docs --workspace acme
"""

import argparse

from memlayer.connectors.local_files import read_dir
from memlayer.ingest import ingest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--workspace", default="default")
    ap.add_argument(
        "--principals",
        default="group:all",
        help="comma-separated, e.g. user:ali,group:eng",
    )
    args = ap.parse_args()

    items = read_dir(args.path, allowed_principals=args.principals.split(","))
    stats = ingest(items, workspace=args.workspace)

    print(f"items:               {stats.items}")
    print(f"chunks:              {stats.chunks}")
    print(f"embeddings computed: {stats.embeddings_computed}  (cost)")
    print(f"embeddings reused:   {stats.embeddings_reused}  (dedup saved)")


if __name__ == "__main__":
    main()
