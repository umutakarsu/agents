"""CLI for Phase 11 tier auto-promotion.

    python scripts/promote.py --workspace acme            # run both promotions
    python scripts/promote.py --workspace acme --dry-run  # report only, no writes

Two upward transitions:
  * working -> episodic: compress entities with >= --min-rows un-compressed
    working rows into one episodic summary each (compress() does the work).
  * episodic -> semantic: promote episodic summaries that are old enough,
    still confident enough, and referenced enough to have proven durable.

semantic -> procedural is intentionally manual and not touched here.
"""

from __future__ import annotations

import argparse
import sys

from memlayer.compress import compress
from memlayer.db import connect
from memlayer.decay import effective_confidence_sql
from memlayer.promote import (
    promote_episodic_to_semantic,
    promote_working_to_episodic,
)


def _dry_run(
    workspace: str,
    *,
    min_rows: int,
    min_age_days: int,
    min_eff: float,
    min_refs: int,
) -> None:
    """Report what each transition would do, without writing anything.

    working -> episodic uses compress()'s own dry_run path so the reported
    counts come from the same scoring code the real run uses. episodic ->
    semantic counts the rows the UPDATE would match (same predicate, no
    UPDATE)."""
    eff_sql = effective_confidence_sql("memory")
    with connect() as conn, conn.cursor() as cur:
        # working -> episodic: eligible entities.
        cur.execute(
            """
            SELECT entity_key, count(*)
            FROM memory
            WHERE workspace = %s
              AND tier = 'working'
              AND compressed_into IS NULL
            GROUP BY entity_key
            HAVING count(*) >= %s
            ORDER BY entity_key
            """,
            (workspace, min_rows),
        )
        eligible = [(row[0], row[1]) for row in cur.fetchall()]

        # episodic -> semantic: rows the UPDATE would match.
        cur.execute(
            f"""
            SELECT id
            FROM memory
            WHERE workspace = %s
              AND tier = 'episodic'
              AND now() - created_at >= make_interval(days => %s)
              AND ({eff_sql}) >= %s
              AND reference_count >= %s
            ORDER BY id
            """,
            (workspace, min_age_days, min_eff, min_refs),
        )
        promotable = [row[0] for row in cur.fetchall()]

    print(f"[dry-run] workspace={workspace!r}")
    print("  working -> episodic:")
    if not eligible:
        print("    no entities have enough un-compressed working rows.")
    for ent, n in eligible:
        # compress()'s own dry-run path scores without writing, so the
        # projected sentence count comes from the same code the real run uses.
        proj = compress(workspace, ent, dry_run=True, min_rows=min_rows)
        print(
            f"    would compress {ent!r} ({n} working rows) -> "
            f"1 episodic (~{proj.num_sentences} sentences)"
        )
    print(f"    entities_eligible={len(eligible)}")

    print("  episodic -> semantic:")
    if not promotable:
        print("    no episodic rows meet age + confidence + reference cutoffs.")
    for mid in promotable:
        print(f"    would promote memory #{mid} -> semantic")
    print(f"    rows_promotable={len(promotable)}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Tier auto-promotion")
    p.add_argument("--workspace", required=True, help="Workspace to promote in")
    p.add_argument(
        "--min-rows",
        type=int,
        default=3,
        help="working->episodic: min un-compressed working rows per entity",
    )
    p.add_argument(
        "--min-age-days",
        type=int,
        default=7,
        help="episodic->semantic: min age in days to be eligible",
    )
    p.add_argument(
        "--min-eff",
        type=float,
        default=0.5,
        help="episodic->semantic: min effective_confidence to be eligible",
    )
    p.add_argument(
        "--min-refs",
        type=int,
        default=3,
        help="episodic->semantic: min reference_count to be eligible",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would happen, write nothing",
    )
    args = p.parse_args(argv)

    if args.dry_run:
        _dry_run(
            args.workspace,
            min_rows=args.min_rows,
            min_age_days=args.min_age_days,
            min_eff=args.min_eff,
            min_refs=args.min_refs,
        )
        return 0

    w2e = promote_working_to_episodic(args.workspace, min_rows=args.min_rows)
    e2s = promote_episodic_to_semantic(
        args.workspace,
        min_age_days=args.min_age_days,
        min_eff=args.min_eff,
        min_refs=args.min_refs,
    )
    print(f"workspace={args.workspace!r}")
    print(
        f"  working -> episodic: compressed {w2e['entities_compressed']} "
        f"entit(ies), summaries={w2e['summaries_created']}"
    )
    print(
        f"  episodic -> semantic: promoted {e2s['promoted']} row(s), "
        f"ids={e2s['ids']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
