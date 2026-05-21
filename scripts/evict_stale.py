"""Evict stale low-tier memory rows.

Working rows older than 24h and decayed below 0.15 effective_confidence,
and episodic rows older than 30d and decayed below 0.35, get hard-deleted.
Semantic and procedural rows are never auto-evicted -- only humans remove
those (a future endpoint).

Run on a cron or by hand; this script intentionally does NOT install
itself into a scheduler (scheduling is a separate feature).

    python scripts/evict_stale.py            # delete stale rows
    python scripts/evict_stale.py --dry-run  # report only, change nothing
"""

import argparse

from memlayer.decay import evict_stale


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report the counts that would be evicted, change nothing",
    )
    args = ap.parse_args()

    counts = evict_stale(dry_run=args.dry_run)
    verb = "Would evict" if args.dry_run else "Evicted"
    working = counts.get("working", 0)
    episodic = counts.get("episodic", 0)
    print(f"{verb} {working} working rows, {episodic} episodic rows.")


if __name__ == "__main__":
    main()
