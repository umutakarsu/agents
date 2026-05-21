"""End-to-end regression test for Phases 0-4 against a real Postgres.
No test framework -- standalone so it runs anywhere the app runs.

    python scripts/e2e_test.py        # exits non-zero on any failure

Resets the data (TRUNCATE, never drops the schema/extension), then asserts
the hard-problem behaviours hold across ingestion, ACL pre-filter,
conflict-resolved write-back, and memory-aware retrieval.
"""

import inspect
import sys
from datetime import datetime, timezone

from memlayer import federated
from memlayer.connectors.local_files import read_dir
from memlayer.db import connect
from memlayer.federated import (
    attribute_check,
    compute_synonyms,
    expand_query,
    index_workspace,
)
from memlayer.ingest import SourceItem, ingest
from memlayer.retrieval import search
from memlayer.scrub import scrub
from memlayer.writeback import recall, remember

WS = "e2e"
_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


def reset() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE memory, chunk_acl, embeddings, event_chunks, "
            "chunks, raw_events, redactions_log, concept_synonym, "
            "tenant_concept, concept RESTART IDENTITY CASCADE"
        )
        conn.commit()


def main() -> None:
    reset()

    print("Phase 0-1: ingestion + dedup")
    docs = read_dir("sample_docs")
    s1 = ingest(docs, workspace=WS)
    s2 = ingest(docs, workspace=WS)
    check("cold run computes embeddings", s1.embeddings_computed > 0)
    check(
        "shared section dedupes on cold run (>=1 reuse)",
        s1.embeddings_reused >= 1,
    )
    check("re-ingest computes zero embeddings", s2.embeddings_computed == 0)
    check(
        "re-ingest reuses every chunk",
        s2.embeddings_reused == s2.chunks and s2.chunks > 0,
    )

    print("Phase 2: ACL pre-filter")
    ingest(
        read_dir("sample_docs_restricted", allowed_principals=["group:exec"]),
        workspace=WS,
    )
    q = "secret roadmap acquire competitor Q3"
    public = search(q, WS, ["group:all"], k=10)
    execs = search(q, WS, ["group:exec"], k=10)
    leaked = any("secret roadmap" in h.text.lower() for h in public)
    visible = any("secret roadmap" in h.text.lower() for h in execs)
    check("group:all cannot see restricted content", not leaked)
    check("group:exec can see restricted content", visible)

    print("Phase 3: conflict-resolved write-back")
    remember(WS, "person:ali", "agent says retrieval lead",
             "agent", "scanner", confidence=0.6)
    remember(WS, "person:ali", "human says platform lead",
             "human", "mgr", confidence=0.95)
    _, is_cur = remember(WS, "person:ali", "agent guesses left",
                          "agent", "scanner2", confidence=0.4)
    cur = recall(WS, "person:ali")
    check("human memory is current", cur is not None
          and cur.source_type == "human")
    check("low-confidence agent guess is superseded", is_cur is False)

    print("Phase 4: memory-aware retrieval + memory ACL")
    # Distinct entity_keys so neither memory is superseded by the other.
    remember(WS, "topic:layoffs",
             "Execs are reviewing a layoff plan for Q1.",
             "human", "manager", confidence=0.9,
             allowed_principals=["group:exec"])
    remember(WS, "topic:strategy",
             "Q4 focus is a shared context layer for AI agents.",
             "human", "lead", confidence=0.9,
             allowed_principals=["group:all"])

    qm = "layoffs plan Q1"
    public_mem = search(qm, WS, ["group:all"], k=10)
    exec_mem = search(qm, WS, ["group:exec"], k=10)
    public_leaked_mem = any(
        h.kind == "memory" and "layoff" in h.text.lower() for h in public_mem
    )
    exec_visible_mem = any(
        h.kind == "memory" and "layoff" in h.text.lower() for h in exec_mem
    )
    check("memory ACL: group:all cannot see exec-only memory",
          not public_leaked_mem)
    check("memory ACL: group:exec can see exec-only memory",
          exec_visible_mem)

    # The strategy memory phrasing overlaps with the public docs, so this
    # query hits both raw chunks and the memory arm.
    blended = search("shared context layer", WS, ["group:all"], k=10)
    kinds = {h.kind for h in blended}
    check("retrieval surfaces memory as kind='memory'", "memory" in kinds)
    check("retrieval still surfaces raw chunks alongside memory",
          "chunk" in kinds)

    mem_hits = [h for h in blended if h.kind == "memory"]
    check("memory hits carry provenance",
          bool(mem_hits) and all(h.provenance for h in mem_hits))
    check("memory hits carry confidence",
          bool(mem_hits) and all(h.confidence is not None for h in mem_hits))

    print("Phase 6: privacy filter at ingest boundary")
    # Pure scrub() unit checks: no-secret text passes through unchanged.
    clean = "Just a normal sentence with nothing sensitive."
    clean_out, clean_reds = scrub(clean)
    check("scrub() leaves clean text identical", clean_out == clean)
    check("scrub() returns no redactions for clean text", clean_reds == [])

    # PII is OFF by default -- emails survive (legitimate signal).
    pii_text = "Contact me at alice@example.com about the project."
    default_out, default_reds = scrub(pii_text)
    check(
        "scrub() default leaves email in place (PII off by default)",
        "alice@example.com" in default_out and default_reds == [],
    )
    opt_out, opt_reds = scrub(pii_text, scrub_pii=True)
    check(
        "scrub(scrub_pii=True) opts into email scrubbing",
        "alice@example.com" not in opt_out
        and any(r.kind == "email" for r in opt_reds),
    )

    # End-to-end: a SourceItem with an AWS key + GitHub token is scrubbed
    # before it ever lands in chunks/embeddings/FTS.
    aws_key = "AKIAIOSFODNN7EXAMPLE"
    gh_token = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    leaky = SourceItem(
        source="slack",
        source_id="thread-leak-1",
        text=(
            f"# Outage post-mortem\n\nWe accidentally committed an AWS key: "
            f"{aws_key}. Also rotated the GitHub token {gh_token}.\n"
        ),
        occurred_at=datetime.now(timezone.utc),
    )
    leak_stats = ingest([leaky], workspace=WS)
    check("ingest reports scrubbed chars > 0", leak_stats.scrubbed_chars > 0)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM chunks WHERE text LIKE %s LIMIT 1", (f"%{aws_key}%",)
        )
        aws_in_chunks = cur.fetchone() is not None
        cur.execute(
            "SELECT 1 FROM chunks WHERE text LIKE %s LIMIT 1", (f"%{gh_token}%",)
        )
        gh_in_chunks = cur.fetchone() is not None
        cur.execute(
            "SELECT kind, count FROM redactions_log "
            "WHERE workspace = %s AND source_id = %s",
            (WS, "thread-leak-1"),
        )
        logged = {row[0]: row[1] for row in cur.fetchall()}

    check("AWS key never reaches chunks table", not aws_in_chunks)
    check("GitHub token never reaches chunks table", not gh_in_chunks)
    check("redactions_log records aws_access_key", "aws_access_key" in logged)
    check("redactions_log records github_token", "github_token" in logged)

    # Search for the secret returns nothing (it was never embedded).
    secret_hits = search(aws_key, WS, ["group:all"], k=10)
    check(
        "search for the AWS key returns no hits referencing the key",
        not any(aws_key in h.text for h in secret_hits),
    )

    # A clean SourceItem is identical pre- and post-scrub: no log rows.
    benign = SourceItem(
        source="slack",
        source_id="thread-benign-1",
        text="# Sprint plan\n\nGroom the backlog, ship the redactions filter.\n",
        occurred_at=datetime.now(timezone.utc),
    )
    benign_stats = ingest([benign], workspace=WS)
    check("benign ingest has zero scrubbed_chars", benign_stats.scrubbed_chars == 0)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM redactions_log "
            "WHERE workspace = %s AND source_id = %s",
            (WS, "thread-benign-1"),
        )
        benign_log_rows = cur.fetchone()[0]
    check("benign ingest writes no redaction log rows", benign_log_rows == 0)

    print("Phase 10: federated cross-tenant concept ontology")
    # Seed three synthetic workspaces with overlapping common vocabulary
    # ("code review", "pull request", "customer churn") and tenant-specific
    # vocabulary ("acme widget" only in tenant_a, "globex sprocket" only in
    # tenant_b). The common terms should rise as cross-tenant concepts; the
    # tenant-specific terms must stay tenant-private.
    now = datetime.now(timezone.utc)
    common_text = (
        "# Engineering\n\n"
        "## Code review\n"
        "Every pull request goes through code review. "
        "Code review is mandatory before merge. "
        "The code review process catches bugs early.\n\n"
        "## Customer churn\n"
        "We track customer churn weekly. "
        "Customer churn dropped 5% last quarter. "
        "Reducing customer churn is a top priority.\n\n"
        "## Pull request\n"
        "Open a pull request, request code review, then merge. "
        "A pull request without code review never lands.\n"
    )
    tenant_a_text = (
        common_text
        + "\n## Acme widget\n"
        "The acme widget ships next quarter. "
        "Our acme widget pipeline pairs with code review gates. "
        "Acme widget revenue offsets customer churn risk.\n"
    )
    tenant_b_text = (
        common_text
        + "\n## Globex sprocket\n"
        "Globex sprocket inventory is low. "
        "Order more globex sprocket before the customer churn review. "
        "The globex sprocket team also runs code review.\n"
    )
    tenant_c_text = common_text + (
        "\n## Onboarding\n"
        "New hires shadow code review for a week. "
        "They submit a pull request and watch customer churn dashboards.\n"
    )
    for ws, text in [
        ("tenant_a", tenant_a_text),
        ("tenant_b", tenant_b_text),
        ("tenant_c", tenant_c_text),
    ]:
        ingest(
            [
                SourceItem(
                    source="docs",
                    source_id=f"{ws}-handbook",
                    text=text,
                    occurred_at=now,
                )
            ],
            workspace=ws,
        )
        index_workspace(ws)

    n_syn = compute_synonyms(min_tenants=2, min_cooccurrence=2)

    with connect() as conn, conn.cursor() as cur:
        # 4. 'code review' is a cross-tenant concept seen by all three.
        cur.execute(
            "SELECT tenant_count, global_count FROM concept WHERE name = %s",
            ("code review",),
        )
        cr_row = cur.fetchone()
        check(
            "concept 'code review' present", cr_row is not None
        )
        check(
            "concept 'code review' tenant_count == 3",
            cr_row is not None and cr_row[0] == 3,
        )

        # 5. 'acme widget' is single-tenant only.
        cur.execute(
            "SELECT tenant_count FROM concept WHERE name = %s",
            ("acme widget",),
        )
        aw_row = cur.fetchone()
        check("concept 'acme widget' present", aw_row is not None)
        check(
            "concept 'acme widget' tenant_count == 1",
            aw_row is not None and aw_row[0] == 1,
        )

        # 6a. Per-tenant data stays per-tenant: tenant_a has 'acme widget' in
        # tenant_concept, but tenant_b does NOT.
        cur.execute(
            """
            SELECT 1 FROM tenant_concept tc
            JOIN concept c ON c.id = tc.concept_id
            WHERE tc.workspace = %s AND c.name = %s
            """,
            ("tenant_a", "acme widget"),
        )
        a_has_aw = cur.fetchone() is not None
        cur.execute(
            """
            SELECT 1 FROM tenant_concept tc
            JOIN concept c ON c.id = tc.concept_id
            WHERE tc.workspace = %s AND c.name = %s
            """,
            ("tenant_b", "acme widget"),
        )
        b_has_aw = cur.fetchone() is not None
        check(
            "tenant_concept: tenant_a HAS 'acme widget'", a_has_aw
        )
        check(
            "tenant_concept: tenant_b does NOT have 'acme widget'",
            not b_has_aw,
        )

        # 6b. concept_synonym never references a single-tenant concept.
        cur.execute(
            """
            SELECT COUNT(*) FROM concept_synonym s
            JOIN concept ca ON ca.id = s.concept_a
            JOIN concept cb ON cb.id = s.concept_b
            WHERE ca.tenant_count < 2 OR cb.tenant_count < 2
            """
        )
        bad_syn = cur.fetchone()[0]
        check(
            "concept_synonym never references a single-tenant concept",
            bad_syn == 0,
        )

        # 6b'. Specifically, 'acme widget' must not appear in any synonym
        # row -- it would betray tenant_a.
        cur.execute(
            """
            SELECT COUNT(*) FROM concept_synonym s
            WHERE s.concept_a = (SELECT id FROM concept WHERE name = 'acme widget')
               OR s.concept_b = (SELECT id FROM concept WHERE name = 'acme widget')
            """
        )
        aw_in_syn = cur.fetchone()[0]
        check(
            "'acme widget' appears in NO synonym row", aw_in_syn == 0
        )

    # 6c. Examine the SQL the federated module issues. Assert that none of
    # the cross-tenant aggregation paths (compute_synonyms, expand_query)
    # SELECT chunk text or memory content. We grep the source for the
    # SQL these functions actually use.
    src_compute = inspect.getsource(federated.compute_synonyms)
    src_expand = inspect.getsource(federated.expand_query)
    forbidden = ("chunks.text", "memory.content", "c.text", "raw_events")
    check(
        "compute_synonyms SQL never reads chunk/memory CONTENT",
        not any(tok in src_compute for tok in forbidden),
    )
    check(
        "expand_query SQL never reads chunk/memory CONTENT",
        not any(tok in src_expand for tok in forbidden),
    )

    # 6d. Privacy self-test passes for every tenant.
    for ws in ("tenant_a", "tenant_b", "tenant_c"):
        rep = attribute_check(ws)
        check(
            f"attribute_check ok for workspace={ws!r}",
            rep["ok"] and rep["single_tenant_synonyms"] == 0,
        )

    # 7. Query expansion: 'pull request feedback' shares the 'pull request'
    # concept with the cross-tenant corpus. Expect at least one synonym from
    # the cluster of co-occurring concepts ('code review', 'customer churn').
    expansions = expand_query("pull request feedback")
    check(
        "expand_query returns >=1 cross-tenant synonym for "
        "'pull request feedback'",
        len(expansions) >= 1,
    )
    print(f"  (expansion sample: {expansions[:5]})  synonyms_written={n_syn}")

    print()
    if _FAILS:
        print(f"FAILED ({len(_FAILS)}): {', '.join(_FAILS)}")
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
