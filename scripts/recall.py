"""Read the current memory for an entity, or the full audit trail.

    python scripts/recall.py acme "person:ali"
    python scripts/recall.py acme "person:ali" --history
"""

import argparse

from memlayer.writeback import history, recall


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace")
    ap.add_argument("entity_key")
    ap.add_argument("--history", action="store_true")
    args = ap.parse_args()

    if args.history:
        rows = history(args.workspace, args.entity_key)
        if not rows:
            print("(no memory for this entity)")
            return
        for mid, content, stype, sid, conf, superseded, _, tier, _lref, eff in rows:
            tag = f"-> superseded by #{superseded}" if superseded else "CURRENT"
            print(f"#{mid:<3} {stype}:{sid:<14} c={conf:<4} eff={eff:.3f} tier={tier:<10} {tag}")
            print(f"     {content}")
        return

    m = recall(args.workspace, args.entity_key)
    if m is None:
        print("(no current memory for this entity)")
        return
    print(f"#{m.id} [{m.source_type}:{m.source_id}] confidence={m.confidence}")
    print(f"derived_from raw_events={m.derived_from}")
    print(m.content)


if __name__ == "__main__":
    main()
