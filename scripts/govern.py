"""Governance CLI: roles, policies, writer assignments, conflicts.

Subcommands::

    python scripts/govern.py list-policies [--workspace WS]
    python scripts/govern.py add-role --workspace WS --type human \
        --name security_lead --authority 4.5
    python scripts/govern.py add-policy --workspace WS --kind security \
        --role security_lead --authority 5.0
    python scripts/govern.py assign --workspace WS --type human \
        --source-id manager --role engineering_manager
    python scripts/govern.py conflicts [--workspace WS]
    python scripts/govern.py resolve --conflict-id N --winner-row M \
        --by 'role:security_lead'

The CLI is a thin wrapper around ``memlayer.governance``. Operations are
idempotent so it's safe to script.
"""

import argparse
import sys

from memlayer.db import connect
from memlayer.governance import (
    assign_writer,
    list_policies,
    pending_conflicts,
    resolve_conflict,
    upsert_policy,
    upsert_role,
)


def _role_id(workspace: str, source_type: str | None, name: str) -> int:
    """Look up an existing role by name in the workspace (or default).

    Used when add-policy / assign refer to a role by name instead of id.
    Falls back to the 'default' workspace so a global role catalog works.
    """
    with connect() as conn, conn.cursor() as cur:
        if source_type is not None:
            cur.execute(
                "SELECT id FROM role "
                "WHERE workspace IN (%s, 'default') AND source_type = %s "
                "AND name = %s ORDER BY (workspace = %s) DESC LIMIT 1",
                (workspace, source_type, name, workspace),
            )
        else:
            cur.execute(
                "SELECT id FROM role "
                "WHERE workspace IN (%s, 'default') AND name = %s "
                "ORDER BY (workspace = %s) DESC LIMIT 1",
                (workspace, name, workspace),
            )
        row = cur.fetchone()
    if row is None:
        raise SystemExit(
            f"role {name!r} not found in workspace {workspace!r} or 'default'"
        )
    return int(row[0])


def cmd_list_policies(args: argparse.Namespace) -> None:
    rows = list_policies(args.workspace)
    if not rows:
        print("(no policies)")
        return
    print(
        f"{'workspace':12} {'entity_kind':14} {'role':24} "
        f"{'auth':>5}  base"
    )
    for r in rows:
        role_label = f"{r['source_type']}:{r['role_name']}"
        print(
            f"{r['workspace']:12} {r['entity_kind']:14} {role_label:24} "
            f"{r['authority']:5.2f}  {r['base_authority']:.2f}"
        )


def cmd_add_role(args: argparse.Namespace) -> None:
    rid = upsert_role(args.workspace, args.type, args.name, args.authority)
    print(
        f"role #{rid}: {args.type}:{args.name} in {args.workspace!r} "
        f"(base_authority={args.authority})"
    )


def cmd_add_policy(args: argparse.Namespace) -> None:
    # Roles can be referred to by name across the (workspace, default) pair.
    rid = _role_id(args.workspace, None, args.role)
    pid = upsert_policy(args.workspace, args.kind, rid, args.authority)
    print(
        f"policy #{pid}: kind={args.kind!r} role={args.role!r} "
        f"authority={args.authority} in {args.workspace!r}"
    )


def cmd_assign(args: argparse.Namespace) -> None:
    rid = _role_id(args.workspace, args.type, args.role)
    assign_writer(args.workspace, args.type, args.source_id, rid)
    print(
        f"assigned {args.type}:{args.source_id} -> role {args.role!r} "
        f"in {args.workspace!r}"
    )


def cmd_conflicts(args: argparse.Namespace) -> None:
    rows = pending_conflicts(args.workspace)
    if not rows:
        print("(no pending conflicts)")
        return
    print(
        f"{'id':>5} {'workspace':12} {'entity_key':30} "
        f"{'row_a':>6} {'row_b':>6} {'auth_a':>6} {'auth_b':>6}"
    )
    for c in rows:
        print(
            f"{c.id:>5} {c.workspace:12} {c.entity_key:30} "
            f"{c.row_a:>6} {c.row_b:>6} "
            f"{c.authority_a:>6.2f} {c.authority_b:>6.2f}"
        )


def cmd_resolve(args: argparse.Namespace) -> None:
    # Look up the conflict to discover the row pair and workspace.
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT workspace, row_a, row_b, status "
            "FROM memory_conflict WHERE id = %s",
            (args.conflict_id,),
        )
        row = cur.fetchone()
    if row is None:
        print(f"no conflict with id {args.conflict_id}", file=sys.stderr)
        sys.exit(1)
    ws, row_a, row_b, status = row
    if status != "pending":
        print(
            f"conflict {args.conflict_id} already {status}",
            file=sys.stderr,
        )
        sys.exit(1)
    cid = resolve_conflict(ws, row_a, row_b, args.by, args.winner_row)
    print(
        f"resolved conflict #{cid}: winner=#{args.winner_row} by {args.by!r}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list-policies")
    p.add_argument("--workspace")
    p.set_defaults(func=cmd_list_policies)

    p = sub.add_parser("add-role")
    p.add_argument("--workspace", required=True)
    p.add_argument(
        "--type", required=True, choices=("human", "system", "agent"),
        help="source_type",
    )
    p.add_argument("--name", required=True)
    p.add_argument("--authority", type=float, default=1.0)
    p.set_defaults(func=cmd_add_role)

    p = sub.add_parser("add-policy")
    p.add_argument("--workspace", required=True)
    p.add_argument(
        "--kind", required=True,
        help="entity_kind, e.g. 'security', 'engineering', '*' for all",
    )
    p.add_argument(
        "--role", required=True,
        help="role name (matched within the workspace or 'default')",
    )
    p.add_argument("--authority", type=float, required=True)
    p.set_defaults(func=cmd_add_policy)

    p = sub.add_parser("assign")
    p.add_argument("--workspace", required=True)
    p.add_argument(
        "--type", required=True, choices=("human", "system", "agent"),
    )
    p.add_argument(
        "--source-id", required=True,
        help="the source_id passed to remember()",
    )
    p.add_argument("--role", required=True)
    p.set_defaults(func=cmd_assign)

    p = sub.add_parser("conflicts")
    p.add_argument("--workspace")
    p.set_defaults(func=cmd_conflicts)

    p = sub.add_parser("resolve")
    p.add_argument("--conflict-id", type=int, required=True)
    p.add_argument(
        "--winner-row", type=int, required=True,
        help="memory.id to keep current",
    )
    p.add_argument(
        "--by", required=True,
        help="how the conflict was resolved, e.g. 'role:security_lead'",
    )
    p.set_defaults(func=cmd_resolve)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
