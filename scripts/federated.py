"""CLI for the federated cross-tenant concept ontology (Phase 10).

    python scripts/federated.py index --workspace WS    # index one workspace
    python scripts/federated.py index-all               # index every workspace
    python scripts/federated.py synonyms                # recompute synonym pairs
    python scripts/federated.py expand "query terms"    # show query expansion
    python scripts/federated.py privacy-check           # privacy self-test
    python scripts/federated.py top-concepts [N=20]     # show top global concepts

The ``top-concepts`` view is the demo moment: it shows concepts ordered by
how many DISTINCT tenants have them. A high tenant_count is the cross-tenant
proof that a concept name is real vocabulary, not a quirk of one workspace.
No tenant's specific content is revealed.
"""

import argparse
import sys

from memlayer.federated import (
    attribute_check,
    compute_synonyms,
    expand_query,
    index_workspace,
    list_workspaces,
    top_concepts,
)


def _cmd_index(args: argparse.Namespace) -> None:
    summary = index_workspace(args.workspace)
    print(f"indexed workspace={args.workspace!r}")
    for k, v in summary.items():
        print(f"  {k}: {v}")


def _cmd_index_all(_args: argparse.Namespace) -> None:
    workspaces = list_workspaces()
    if not workspaces:
        print("(no workspaces found)")
        return
    for ws in workspaces:
        summary = index_workspace(ws)
        print(
            f"  {ws:>16}  vocab={summary['vocab_size']:>4}  "
            f"added={summary['concepts_added']:>4}  "
            f"updated={summary['concepts_updated']:>4}  "
            f"extractions={summary['total_extractions']}"
        )


def _cmd_synonyms(args: argparse.Namespace) -> None:
    n = compute_synonyms(
        min_tenants=args.min_tenants, min_cooccurrence=args.min_cooccurrence
    )
    print(f"wrote {n} synonym pair(s)")
    print(
        f"  (min_tenants={args.min_tenants}, "
        f"min_cooccurrence={args.min_cooccurrence})"
    )


def _cmd_expand(args: argparse.Namespace) -> None:
    expansions = expand_query(args.query)
    print(f"query: {args.query!r}")
    if not expansions:
        print("  (no cross-tenant synonyms found)")
        return
    for e in expansions:
        print(f"  + {e}")


def _cmd_privacy_check(_args: argparse.Namespace) -> None:
    workspaces = list_workspaces()
    if not workspaces:
        print("(no workspaces to check)")
        return
    any_fail = False
    for ws in workspaces:
        rep = attribute_check(ws)
        status = "OK" if rep["ok"] else "FAIL"
        print(f"[{status}] workspace={ws!r}")
        print(f"  concept_total              : {rep['concept_total']}")
        print(f"  concept_single_tenant      : {rep['concept_single_tenant']}")
        print(f"  synonym_total              : {rep['synonym_total']}")
        print(
            f"  single_tenant_synonyms     : {rep['single_tenant_synonyms']} "
            "(must be 0)"
        )
        if rep["review_needed"]:
            sample = ", ".join(rep["review_needed"][:5])
            extra = (
                f" (+{len(rep['review_needed']) - 5} more)"
                if len(rep["review_needed"]) > 5
                else ""
            )
            print(
                f"  unique-to-this-workspace   : "
                f"{len(rep['review_needed'])}  e.g. [{sample}]{extra}"
            )
        if not rep["ok"]:
            any_fail = True
    if any_fail:
        sys.exit(1)


def _cmd_top_concepts(args: argparse.Namespace) -> None:
    rows = top_concepts(limit=args.n)
    if not rows:
        print("(no concepts indexed yet -- run `index-all` first)")
        return
    print(
        f"{'tenant_count':>13}  {'global_count':>13}  concept"
    )
    print(f"{'-' * 13}  {'-' * 13}  {'-' * 30}")
    for name, tcount, gcount in rows:
        print(f"{tcount:>13}  {gcount:>13}  {name}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="index one workspace")
    p.add_argument("--workspace", required=True)
    p.set_defaults(func=_cmd_index)

    p = sub.add_parser("index-all", help="index every workspace")
    p.set_defaults(func=_cmd_index_all)

    p = sub.add_parser("synonyms", help="recompute cross-tenant synonym pairs")
    p.add_argument("--min-tenants", type=int, default=2)
    p.add_argument("--min-cooccurrence", type=int, default=3)
    p.set_defaults(func=_cmd_synonyms)

    p = sub.add_parser("expand", help="show query expansion")
    p.add_argument("query")
    p.set_defaults(func=_cmd_expand)

    p = sub.add_parser("privacy-check", help="run the privacy self-test")
    p.set_defaults(func=_cmd_privacy_check)

    p = sub.add_parser("top-concepts", help="show top global concepts")
    p.add_argument("n", type=int, nargs="?", default=20)
    p.set_defaults(func=_cmd_top_concepts)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
