"""CLI for Phase 8 extractive compression.

    python scripts/compress.py --workspace acme
        # compress every entity in 'acme' with >=3 un-compressed working rows
    python scripts/compress.py --workspace acme --entity topic:perf-issue
        # just that one entity
    python scripts/compress.py --workspace acme --dry-run
        # show what would happen, write nothing

Prints in/out chars, compression ratio, and the per-sentence lineage for
each summary written -- so a human can audit a run at a glance."""

from __future__ import annotations

import argparse
import sys

from memlayer.compress import (
    CompressionResult,
    compress,
    compress_all,
    lineage,
)


def _print_result(workspace: str, result: CompressionResult) -> None:
    if result.skipped_reason is not None and result.summary_id is None:
        print(
            f"  skipped: {result.skipped_reason}  "
            f"(sources={len(result.source_row_ids)})"
        )
        return
    print(
        f"  summary_id={result.summary_id}  "
        f"sources={result.source_row_ids}  "
        f"sentences={result.num_sentences}  "
        f"chars_in={result.total_chars_in} -> chars_out={result.total_chars_out}  "
        f"ratio={result.compression_ratio:.3f}"
    )
    if result.summary_id is None:
        # Dry run: we computed counts but didn't write, so no lineage to show.
        return
    rows = lineage(result.summary_id)
    print(f"  lineage ({len(rows)} sentences):")
    for r in rows:
        print(
            f"    [{r.sentence_index}] source=#{r.source_row_id}  "
            f"{r.sentence!r}"
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Extractive memory compression")
    p.add_argument("--workspace", required=True, help="Workspace to compress in")
    p.add_argument(
        "--entity",
        default=None,
        help="Just this entity_key; default: every entity in the workspace",
    )
    p.add_argument(
        "--min-rows",
        type=int,
        default=3,
        help="Minimum un-compressed working rows required to compress",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Score and report but don't write anything",
    )
    args = p.parse_args(argv)

    if args.entity is not None:
        print(f"Compressing {args.workspace}/{args.entity}")
        result = compress(
            args.workspace,
            args.entity,
            dry_run=args.dry_run,
            min_rows=args.min_rows,
        )
        _print_result(args.workspace, result)
        return 0

    print(f"Compressing all entities in {args.workspace}")
    if args.dry_run:
        # compress_all() has no dry_run plumbing -- we replicate it here so
        # the CLI flag stays useful in the "every entity" case too.
        from memlayer.db import connect

        with connect() as conn, conn.cursor() as cur:
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
                (args.workspace, args.min_rows),
            )
            entities = [row[0] for row in cur.fetchall()]
        results = [
            compress(args.workspace, ent, dry_run=True, min_rows=args.min_rows)
            for ent in entities
        ]
    else:
        results = compress_all(args.workspace, min_rows=args.min_rows)

    if not results:
        print("  no entities had enough working rows to compress.")
        return 0
    for r in results:
        print()
        _print_result(args.workspace, r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
