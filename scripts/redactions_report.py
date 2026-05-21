"""Audit report for the ingest-boundary privacy filter.

    python scripts/redactions_report.py
    python scripts/redactions_report.py --workspace acme
    python scripts/redactions_report.py --since 2026-01-01
    python scripts/redactions_report.py --workspace acme --since 2026-01-01

Reports rows from ``redactions_log``: WHAT kind of secret was found and
WHERE (workspace + source + source_id), grouped and aggregated. The log
never carries the matched content -- that's the whole point of scrubbing
at ingest. Use this to spot patterns ("this Slack channel keeps leaking
AWS keys") without ever seeing the keys themselves.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone

from memlayer.db import connect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="filter to a single workspace")
    parser.add_argument(
        "--since",
        help="ISO date (YYYY-MM-DD); default = 30 days ago",
    )
    args = parser.parse_args()

    if args.since:
        try:
            since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        except ValueError:
            since = datetime.combine(
                date.fromisoformat(args.since), datetime.min.time(), timezone.utc
            )
    else:
        since = datetime.now(timezone.utc) - timedelta(days=30)

    sql = [
        "SELECT kind, source, source_id, workspace, SUM(count)::int AS total",
        "FROM redactions_log",
        "WHERE occurred_at >= %s",
    ]
    params: list = [since]
    if args.workspace:
        sql.append("AND workspace = %s")
        params.append(args.workspace)
    sql.append("GROUP BY kind, source, source_id, workspace")
    sql.append("ORDER BY total DESC, kind, source, source_id")
    query = "\n".join(sql)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    if not rows:
        scope = f" workspace={args.workspace!r}" if args.workspace else ""
        print(f"No redactions logged since {since.date().isoformat()}{scope}.")
        return

    header = ("kind", "source", "source_id", "workspace", "count")
    widths = [len(h) for h in header]
    str_rows = [
        (str(k), str(s), str(sid), str(w), str(c)) for k, s, sid, w, c in rows
    ]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    print(f"Redactions since {since.date().isoformat()}"
          + (f" (workspace={args.workspace})" if args.workspace else ""))
    print()
    print(fmt(header))
    print(fmt(tuple("-" * w for w in widths)))
    for row in str_rows:
        print(fmt(row))
    print()
    print(f"{len(rows)} group(s); total matches: "
          f"{sum(int(r[-1]) for r in str_rows)}")


if __name__ == "__main__":
    main()
