"""Query the memory layer as a specific principal set, to see the ACL
pre-filter in action.

    python scripts/search.py "permission model" --workspace acme \
        --principals group:all
    python scripts/search.py "secret roadmap" --workspace acme \
        --principals user:ali,group:exec
"""

import argparse

from memlayer.retrieval import search


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--workspace", default="default")
    ap.add_argument("--principals", default="group:all")
    ap.add_argument("-k", type=int, default=5)
    args = ap.parse_args()

    hits = search(
        args.query,
        workspace=args.workspace,
        principals=args.principals.split(","),
        k=args.k,
    )
    if not hits:
        print("(no visible results for these principals)")
        return
    for i, h in enumerate(hits, 1):
        head = h.text.splitlines()[0][:70]
        print(f"{i:>2}. score={h.score:.4f}  arms={'+'.join(h.arms):<11}  {head}")


if __name__ == "__main__":
    main()
