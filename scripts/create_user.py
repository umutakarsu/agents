"""Mint a memscope/memlayer bearer-token user.

    python scripts/create_user.py \
        --email ali@example.com \
        --principals group:exec,user:ali \
        --workspaces acme,personal \
        [--source-type human|system|agent]

Prints the plaintext token EXACTLY ONCE. Save it -- we only store
sha256(token) so the plaintext cannot be recovered. Lose it, rotate it
(by re-running this CLI with the same --email is a UniqueViolation; use a
new email or delete the row first).
"""

from __future__ import annotations

import argparse
import sys

from memlayer.auth import create_user


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--email", required=True)
    p.add_argument(
        "--principals",
        required=True,
        help="comma-separated, e.g. group:exec,user:ali",
    )
    p.add_argument(
        "--workspaces",
        required=True,
        help="comma-separated, e.g. acme,personal",
    )
    p.add_argument(
        "--source-type",
        default="human",
        choices=["human", "system", "agent"],
        help="Identity tier for write-back precedence (default: human).",
    )
    args = p.parse_args()

    principals = [s.strip() for s in args.principals.split(",") if s.strip()]
    workspaces = [s.strip() for s in args.workspaces.split(",") if s.strip()]

    try:
        user_id, token = create_user(
            email=args.email,
            principals=principals,
            workspaces=workspaces,
            source_type=args.source_type,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"created user id={user_id} email={args.email}")
    print(f"  principals  : {principals}")
    print(f"  workspaces  : {workspaces}")
    print(f"  source_type : {args.source_type}")
    print()
    print("Bearer token (shown ONCE -- save this now):")
    print(f"  {token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
