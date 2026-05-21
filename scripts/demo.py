"""Watchable end-to-end demo: one run that exercises every hard problem the
memory layer solves -- dedup, ACL on chunks AND memory, conflict resolution,
memory-aware retrieval -- with the result of each step printed inline.

    python scripts/demo.py

Requires Postgres with the memlayer schema applied. The default embedder is
offline/deterministic so no API keys are needed.

Uses workspace 'demo' and resets only that workspace on each run; orphan
chunks/embeddings stay, so the global dedup gate is visibly warm on re-run.
"""

from memlayer.connectors.local_files import read_dir
from memlayer.db import connect
from memlayer.ingest import ingest
from memlayer.retrieval import search
from memlayer.writeback import history, recall, remember

WS = "demo"


def step(n: int, title: str) -> None:
    print()
    print(f"=== STEP {n}: {title} ===")


def reset() -> None:
    # Per-workspace reset. We don't TRUNCATE: other workspaces stay intact,
    # and the global content_hash tables stay warm so re-runs of this demo
    # demonstrate the dedup gate without re-seeding.
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM memory WHERE workspace = %s", (WS,))
        cur.execute("DELETE FROM chunk_acl WHERE workspace = %s", (WS,))
        cur.execute("DELETE FROM raw_events WHERE workspace = %s", (WS,))
        conn.commit()


def show_hits(hits, label: str) -> None:
    print(f"  [{label}] -> {len(hits)} hits")
    for h in hits[:5]:
        first = h.text.strip().splitlines()[0][:70]
        extra = (
            f"  prov={h.provenance} conf={h.confidence}"
            if h.kind == "memory"
            else ""
        )
        arms = ",".join(h.arms)
        print(f"    {h.kind:6} arms={arms:20}{extra}  {first!r}")


def main() -> None:
    reset()

    step(1, "Ingest public docs (sample_docs)")
    s1 = ingest(read_dir("sample_docs"), workspace=WS)
    print(
        f"  items={s1.items} chunks={s1.chunks} "
        f"embed_computed={s1.embeddings_computed} reused={s1.embeddings_reused}"
    )
    print("  a paragraph shared between docs is embedded once -> reused>=1")

    step(2, "Ingest restricted docs (sample_docs_restricted, group:exec only)")
    s2 = ingest(
        read_dir("sample_docs_restricted", allowed_principals=["group:exec"]),
        workspace=WS,
    )
    print(
        f"  items={s2.items} chunks={s2.chunks} "
        f"embed_computed={s2.embeddings_computed} reused={s2.embeddings_reused}"
    )

    q = "secret roadmap acquire competitor Q3"
    step(3, f"ACL pre-filter -- query: {q!r}")
    pub = search(q, WS, ["group:all"], k=10)
    exe = search(q, WS, ["group:exec"], k=10)
    show_hits(pub, "group:all")
    show_hits(exe, "group:exec")
    leaked = any("secret" in h.text.lower() for h in pub)
    print(f"  group:all leaked restricted text? {leaked}  (expected False)")

    step(4, "Write-back: agent guesses, human corrects, late guess loses")
    remember(
        WS, "person:ali", "Ali leads retrieval",
        "agent", "email-scanner", confidence=0.6,
    )
    remember(
        WS, "person:ali", "Ali leads Platform",
        "human", "manager", confidence=0.95,
    )
    _, late_is_current = remember(
        WS, "person:ali", "Ali maybe left",
        "agent", "slack-scanner", confidence=0.4,
    )
    cur = recall(WS, "person:ali")
    print(f"  current:               {cur.content!r}  (source={cur.source_type})")
    print(f"  late agent guess won?  {late_is_current}  (expected False)")
    print("  audit trail:")
    for mid, content, stype, sid, conf, superseded, _ in history(WS, "person:ali"):
        tag = f"-> #{superseded}" if superseded else "CURRENT"
        print(f"    #{mid} {stype:6}:{sid:14} c={conf} {tag}: {content!r}")

    step(5, "Write a distilled memory restricted to group:exec")
    remember(
        WS, "topic:layoffs",
        "Execs are reviewing a layoff plan for Q1.",
        "human", "manager", confidence=0.9,
        allowed_principals=["group:exec"],
    )
    remember(
        WS, "topic:strategy",
        "Q4 focus is a shared context layer for AI agents.",
        "human", "lead", confidence=0.9,
    )

    qm = "layoffs plan Q1"
    step(6, f"Memory-aware retrieval -- query: {qm!r}")
    show_hits(search(qm, WS, ["group:all"], k=10), "group:all")
    show_hits(search(qm, WS, ["group:exec"], k=10), "group:exec")

    qm2 = "shared context layer for agents"
    step(7, f"Hybrid: memory + chunks fuse via RRF -- query: {qm2!r}")
    hits = search(qm2, WS, ["group:all"], k=10)
    show_hits(hits, "group:all")
    kinds = {h.kind for h in hits}
    print(f"  kinds present: {sorted(kinds)}  (expect both 'chunk' and 'memory')")

    step(8, "Re-ingest public docs: dedup gate stays cold")
    s3 = ingest(read_dir("sample_docs"), workspace=WS)
    print(
        f"  embed_computed={s3.embeddings_computed}  (expected 0)\n"
        f"  embed_reused={s3.embeddings_reused}  (= chunks={s3.chunks})"
    )

    print()
    print("Demo complete.")


if __name__ == "__main__":
    main()
