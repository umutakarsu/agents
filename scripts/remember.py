"""Record a memory about an entity. Conflict resolution is automatic.

    python scripts/remember.py acme "person:ali" \
        "Ali leads the retrieval workstream" \
        --source agent:email-scanner --confidence 0.6
    python scripts/remember.py acme "person:ali" \
        "Ali leads Platform, not retrieval" \
        --source human:manager --confidence 0.95
"""

import argparse

from memlayer.writeback import remember


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace")
    ap.add_argument("entity_key")
    ap.add_argument("content")
    ap.add_argument(
        "--source",
        required=True,
        help="<type>:<id>, type in {human,system,agent}, e.g. human:manager",
    )
    ap.add_argument("--confidence", type=float, default=0.5)
    ap.add_argument(
        "--derived-from",
        default="",
        help="comma-separated raw_event ids this was inferred from",
    )
    args = ap.parse_args()

    source_type, _, source_id = args.source.partition(":")
    derived = [int(x) for x in args.derived_from.split(",") if x.strip()]

    mem_id, is_current = remember(
        workspace=args.workspace,
        entity_key=args.entity_key,
        content=args.content,
        source_type=source_type,
        source_id=source_id,
        derived_from=derived,
        confidence=args.confidence,
    )
    state = "CURRENT" if is_current else "stored but superseded by a stronger memory"
    print(f"memory #{mem_id}: {state}")


if __name__ == "__main__":
    main()
