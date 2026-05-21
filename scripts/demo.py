"""Watchable end-to-end demo: a single run that seeds three workspaces with
distinguishable stories so memscope (the visual inspector) shows meaningful
shape from the moment a user opens it.

    python scripts/demo.py

Requires Postgres with the memlayer schema applied. The default embedder is
offline/deterministic so no API keys are needed.

Three workspaces are seeded:
  - acme     : engineering org with multiple entities, full conflict patterns,
               public + restricted document ingestion, an ACL leak check.
  - personal : single-user knowledge base, synthesized notes, self-corrections.
  - sales    : CRM-style accounts with a signed-deal correction and a
               multi-current case where no human has weighed in yet.

Per-workspace reset (DELETE WHERE workspace=...) means re-runs are idempotent;
the global content_hash tables stay warm, so the dedup gate is visibly cold
(`embeddings_computed=0`) on a second run.
"""

from datetime import datetime, timezone

from memlayer.connectors.local_files import read_dir
from memlayer.db import connect
from memlayer.ingest import SourceItem, ingest
from memlayer.retrieval import search
from memlayer.writeback import history, recall, remember

WORKSPACES = ("acme", "personal", "sales")


def step(n: int, title: str) -> None:
    print()
    print(f"=== STEP {n}: {title} ===")


def reset(workspaces: tuple[str, ...]) -> None:
    # Per-workspace reset. We don't TRUNCATE: other workspaces stay intact,
    # and the global content_hash tables stay warm so re-runs of this demo
    # demonstrate the dedup gate without re-seeding.
    with connect() as conn, conn.cursor() as cur:
        for ws in workspaces:
            cur.execute("DELETE FROM memory WHERE workspace = %s", (ws,))
            cur.execute("DELETE FROM chunk_acl WHERE workspace = %s", (ws,))
            cur.execute("DELETE FROM raw_events WHERE workspace = %s", (ws,))
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


def show_history(workspace: str, entity_key: str) -> None:
    print(f"  audit trail for {entity_key} in {workspace!r}:")
    for mid, content, stype, sid, conf, superseded, _ in history(
        workspace, entity_key
    ):
        tag = f"-> #{superseded}" if superseded else "CURRENT"
        print(f"    #{mid} {stype:6}:{sid:14} c={conf} {tag}: {content!r}")


# ---------------------------------------------------------------------------
# Workspace 1: acme -- engineering org, multiple entities, full conflict story
# ---------------------------------------------------------------------------
def seed_acme() -> None:
    ws = "acme"
    print()
    print(f"################ WORKSPACE: {ws} ################")

    step(1, f"[{ws}] Ingest public docs (sample_docs)")
    s1 = ingest(read_dir("sample_docs"), workspace=ws)
    print(
        f"  items={s1.items} chunks={s1.chunks} "
        f"embed_computed={s1.embeddings_computed} reused={s1.embeddings_reused}"
    )
    print("  a paragraph shared between docs is embedded once -> reused>=1")

    step(2, f"[{ws}] Ingest restricted docs (group:exec only)")
    s2 = ingest(
        read_dir("sample_docs_restricted", allowed_principals=["group:exec"]),
        workspace=ws,
    )
    print(
        f"  items={s2.items} chunks={s2.chunks} "
        f"embed_computed={s2.embeddings_computed} reused={s2.embeddings_reused}"
    )

    q = "secret roadmap acquire competitor Q3"
    step(3, f"[{ws}] ACL pre-filter -- query: {q!r}")
    pub = search(q, ws, ["group:all"], k=10)
    exe = search(q, ws, ["group:exec"], k=10)
    show_hits(pub, "group:all")
    show_hits(exe, "group:exec")
    leaked = any("secret" in h.text.lower() for h in pub)
    print(f"  group:all leaked restricted text? {leaked}  (expected False)")

    step(4, f"[{ws}] person:ali -- agent guess, human corrects, late guess loses")
    remember(
        ws, "person:ali", "Ali leads retrieval",
        "agent", "email-scanner", confidence=0.6,
    )
    remember(
        ws, "person:ali", "Ali leads Platform",
        "human", "manager", confidence=0.95,
    )
    _, late_is_current = remember(
        ws, "person:ali", "Ali maybe left",
        "agent", "slack-scanner", confidence=0.4,
    )
    cur_ali = recall(ws, "person:ali")
    print(
        f"  current:               {cur_ali.content!r}  "
        f"(source={cur_ali.source_type})"
    )
    print(f"  late agent guess won?  {late_is_current}  (expected False)")
    show_history(ws, "person:ali")

    step(5, f"[{ws}] person:bjorn -- two rows, human overrides agent")
    remember(
        ws, "person:bjorn", "Bjorn works on frontend",
        "agent", "github-scanner", confidence=0.55,
    )
    remember(
        ws, "person:bjorn", "Bjorn leads design systems",
        "human", "manager", confidence=0.92,
    )
    cur_bjorn = recall(ws, "person:bjorn")
    print(
        f"  current: {cur_bjorn.content!r}  (source={cur_bjorn.source_type})"
    )
    show_history(ws, "person:bjorn")

    step(6, f"[{ws}] person:chen -- single agent guess, no human correction yet")
    remember(
        ws, "person:chen", "Chen runs Tuesday standups",
        "agent", "calendar-scanner", confidence=0.7,
    )
    cur_chen = recall(ws, "person:chen")
    print(
        f"  current: {cur_chen.content!r}  (source={cur_chen.source_type})"
    )
    show_history(ws, "person:chen")

    step(7, f"[{ws}] Distilled topics: public strategy + restricted layoffs")
    remember(
        ws, "topic:strategy",
        "Q4 focus is a shared context layer for AI agents.",
        "human", "lead", confidence=0.9,
    )
    remember(
        ws, "topic:layoffs",
        "Execs are reviewing a layoff plan for Q1.",
        "human", "manager", confidence=0.9,
        allowed_principals=["group:exec"],
    )

    qm = "layoffs plan Q1"
    step(8, f"[{ws}] Memory-aware retrieval -- query: {qm!r}")
    show_hits(search(qm, ws, ["group:all"], k=10), "group:all")
    show_hits(search(qm, ws, ["group:exec"], k=10), "group:exec")

    qm2 = "shared context layer for agents"
    step(9, f"[{ws}] Hybrid: memory + chunks fuse via RRF -- query: {qm2!r}")
    hits = search(qm2, ws, ["group:all"], k=10)
    show_hits(hits, "group:all")
    kinds = {h.kind for h in hits}
    print(f"  kinds present: {sorted(kinds)}  (expect both 'chunk' and 'memory')")

    step(10, f"[{ws}] Re-ingest public docs: dedup gate stays cold")
    s3 = ingest(read_dir("sample_docs"), workspace=ws)
    print(
        f"  embed_computed={s3.embeddings_computed}  (expected 0)\n"
        f"  embed_reused={s3.embeddings_reused}  (= chunks={s3.chunks})"
    )


# ---------------------------------------------------------------------------
# Workspace 2: personal -- single-user knowledge base, synthesized notes
# ---------------------------------------------------------------------------
def seed_personal() -> None:
    ws = "personal"
    print()
    print(f"################ WORKSPACE: {ws} ################")

    now = datetime.now(timezone.utc)
    notes = [
        SourceItem(
            source="journal",
            source_id="side-projects.md",
            text=(
                "# Side projects\n\n"
                "## memlayer\n"
                "Building a shared memory layer for AI agents. "
                "Postgres + pgvector, content-hash dedup, ACL pre-filter, "
                "append-only conflict resolution.\n\n"
                "## memscope\n"
                "A small visual inspector that flips between workspaces and "
                "shows memory rows, supersession edges, and ACL scopes.\n"
            ),
            occurred_at=now,
        ),
        SourceItem(
            source="journal",
            source_id="reading.md",
            text=(
                "# Books in progress\n\n"
                "## Designing Data-Intensive Applications\n"
                "Halfway through the replication chapter. The point about "
                "leader-based replication and write conflicts maps cleanly "
                "onto how memory supersession works in memlayer.\n\n"
                "## The Pragmatic Programmer\n"
                "Re-reading. Tracer bullets > big up-front design.\n"
            ),
            occurred_at=now,
        ),
        SourceItem(
            source="journal",
            source_id="gym-log.md",
            text=(
                "# Gym log\n\n"
                "## Monday\n"
                "Squats 5x5 at 100kg. Felt easy.\n\n"
                "## Wednesday\n"
                "Pull day. Deadlift 3x5 at 140kg, lat pulldowns, face pulls.\n\n"
                "## Friday\n"
                "Push day. Bench 5x5 at 80kg, overhead press, dips.\n"
            ),
            occurred_at=now,
        ),
    ]

    step(1, f"[{ws}] Ingest synthesized personal notes")
    s1 = ingest(notes, workspace=ws)
    print(
        f"  items={s1.items} chunks={s1.chunks} "
        f"embed_computed={s1.embeddings_computed} reused={s1.embeddings_reused}"
    )

    step(2, f"[{ws}] topic:projects -- system writes a stable summary")
    remember(
        ws, "topic:projects",
        "Building memlayer, a shared memory layer for AI agents.",
        "system", "summarizer", confidence=0.95,
    )

    step(3, f"[{ws}] person:umut -- agent guesses generic, self corrects")
    remember(
        ws, "person:umut", "interested in AI agents",
        "agent", "journal-scanner", confidence=0.6,
    )
    remember(
        ws, "person:umut", "building memlayer",
        "human", "self", confidence=0.95,
    )
    cur_umut = recall(ws, "person:umut")
    print(
        f"  current: {cur_umut.content!r}  (source={cur_umut.source_type})"
    )
    show_history(ws, "person:umut")

    qm = "memlayer shared memory for agents"
    step(4, f"[{ws}] Hybrid retrieval -- query: {qm!r}")
    hits = search(qm, ws, ["group:all"], k=10)
    show_hits(hits, "group:all")
    kinds = {h.kind for h in hits}
    print(f"  kinds present: {sorted(kinds)}  (expect both 'chunk' and 'memory')")


# ---------------------------------------------------------------------------
# Workspace 3: sales -- CRM-style accounts, signed deal + multi-current case
# ---------------------------------------------------------------------------
def seed_sales() -> None:
    ws = "sales"
    print()
    print(f"################ WORKSPACE: {ws} ################")

    now = datetime.now(timezone.utc)
    threads = [
        SourceItem(
            source="email",
            source_id="acme-corp/contract-signed",
            text=(
                "Subject: Acme Corp -- contract counter-signed\n\n"
                "Hey team, Acme just counter-signed the Q3 contract. "
                "Annual value 240k, kickoff next Monday. Loop in solutions "
                "engineering for the onboarding call.\n"
            ),
            occurred_at=now,
        ),
        SourceItem(
            source="email",
            source_id="globex/renewal-thread",
            text=(
                "Subject: Globex renewal -- still negotiating\n\n"
                "Globex pushed back on pricing again. They want a 15% "
                "discount tied to a two-year commit. QBR is on the books "
                "for Q3, we should land the renewal before then.\n"
            ),
            occurred_at=now,
        ),
        SourceItem(
            source="email",
            source_id="globex/qbr-invite",
            text=(
                "Subject: Globex QBR scheduled\n\n"
                "Calendar hold sent for the Q3 QBR. Agenda: usage review, "
                "renewal terms, roadmap walkthrough. Their CFO will join "
                "for the renewal portion.\n"
            ),
            occurred_at=now,
        ),
    ]

    step(1, f"[{ws}] Ingest synthesized email threads")
    s1 = ingest(threads, workspace=ws)
    print(
        f"  items={s1.items} chunks={s1.chunks} "
        f"embed_computed={s1.embeddings_computed} reused={s1.embeddings_reused}"
    )

    step(2, f"[{ws}] account:acme-corp -- agent guess, human closes the loop")
    remember(
        ws, "account:acme-corp", "evaluating",
        "agent", "crm-scanner", confidence=0.5,
    )
    remember(
        ws, "account:acme-corp", "signed Q3 contract",
        "human", "sales-lead", confidence=0.95,
    )
    cur_acme = recall(ws, "account:acme-corp")
    print(
        f"  current: {cur_acme.content!r}  (source={cur_acme.source_type})"
    )
    show_history(ws, "account:acme-corp")

    step(3, f"[{ws}] account:globex -- two agent rows, no human, both current?")
    remember(
        ws, "account:globex", "negotiating renewal",
        "agent", "email-scanner", confidence=0.6,
    )
    remember(
        ws, "account:globex", "scheduled QBR Q3",
        "agent", "calendar-scanner", confidence=0.65,
    )
    cur_globex = recall(ws, "account:globex")
    # recall() returns whichever single row is currently un-superseded; the
    # other row is still there in history. The "multi-current" feel comes
    # from the audit trail showing two agent rows, the lower-confidence one
    # losing to the higher-confidence one via precedence, with no human
    # ever weighing in.
    print(
        f"  recall current: {cur_globex.content!r}  "
        f"(source={cur_globex.source_type})  -- precedence picked highest conf"
    )
    show_history(ws, "account:globex")
    print(
        "  note: both rows are agent-sourced; without a human override, "
        "the higher confidence agent row wins by precedence."
    )

    qm = "Globex renewal QBR"
    step(4, f"[{ws}] Hybrid retrieval -- query: {qm!r}")
    hits = search(qm, ws, ["group:all"], k=10)
    show_hits(hits, "group:all")
    kinds = {h.kind for h in hits}
    print(f"  kinds present: {sorted(kinds)}  (expect both 'chunk' and 'memory')")


def main() -> None:
    reset(WORKSPACES)
    seed_acme()
    seed_personal()
    seed_sales()

    print()
    print("Demo complete.")
    print(
        "Open http://localhost:8000 and try workspace "
        "'acme' / 'personal' / 'sales'."
    )


if __name__ == "__main__":
    main()
