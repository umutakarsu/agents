"""End-to-end regression test for Phases 0-4 against a real Postgres.
No test framework -- standalone so it runs anywhere the app runs.

    python scripts/e2e_test.py        # exits non-zero on any failure

Resets the data (TRUNCATE, never drops the schema/extension), then asserts
the hard-problem behaviours hold across ingestion, ACL pre-filter,
conflict-resolved write-back, and memory-aware retrieval.
"""

import sys
from datetime import datetime, timezone

from memlayer.connectors.local_files import read_dir
from memlayer.db import connect
from memlayer.governance import (
    assign_writer,
    authority_for,
    classify_entity,
    pending_conflicts,
    resolve_conflict,
    seed_acme_policies,
    seed_default_policies,
    upsert_role,
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


def _is_superseded(memory_id: int) -> bool:
    """True iff memory row ``memory_id`` has a non-NULL superseded_by."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT superseded_by FROM memory WHERE id = %s", (memory_id,),
        )
        row = cur.fetchone()
    return row is not None and row[0] is not None


def reset() -> None:
    with connect() as conn, conn.cursor() as cur:
        # memory_conflict references memory rows, and writer_role /
        # authority_policy reference role rows -- CASCADE drops them in
        # the right order. We TRUNCATE rather than DROP so the schema
        # (and its constraints) stay intact across runs.
        cur.execute(
            "TRUNCATE memory_conflict, memory, chunk_acl, embeddings, "
            "event_chunks, chunks, raw_events, redactions_log, "
            "writer_role, authority_policy, role "
            "RESTART IDENTITY CASCADE"
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

    print("Phase 9: governance-modeled conflict resolution")
    # Seed defaults + acme policies. Both seeders are idempotent so calling
    # them twice (e.g. on a re-run) is harmless.
    seed_default_policies()
    seed_acme_policies()
    seed_default_policies()  # idempotent re-call

    # classify_entity heuristics: prefix-based routing.
    check(
        "classify_entity: security: prefix -> 'security'",
        classify_entity("security:incident-12") == "security",
    )
    check(
        "classify_entity: topic:layoffs -> 'hr'",
        classify_entity("topic:layoffs-q1") == "hr",
    )
    check(
        "classify_entity: unknown prefix -> 'general'",
        classify_entity("widget:42") == "general",
    )

    # Two writers in the acme workspace, each tied to a role.
    acme = "acme9"  # isolated workspace name so we don't collide w/ demo
    # Mirror acme policies onto the acme9 workspace via role assignment in
    # the acme workspace shared catalog. Simplest path: reuse seed_acme
    # logic by reseeding under acme9.
    sec_role = upsert_role(acme, "human", "security_lead", base_authority=4.5)
    eng_role = upsert_role(acme, "human", "engineering_manager", base_authority=4.0)
    from memlayer.governance import upsert_policy
    upsert_policy(acme, "security", sec_role, 5.0)
    upsert_policy(acme, "security", eng_role, 4.0)
    upsert_policy(acme, "engineering", eng_role, 5.0)
    upsert_policy(acme, "engineering", sec_role, 3.5)
    upsert_policy(acme, "general", sec_role, 3.0)
    upsert_policy(acme, "general", eng_role, 3.0)

    assign_writer(acme, "human", "eng_manager", eng_role)
    assign_writer(acme, "human", "security_lead", sec_role)

    # authority_for sanity: same human writers get different authorities
    # depending on the entity_kind.
    sec_on_sec = authority_for(acme, "human", "security_lead", "security")
    eng_on_sec = authority_for(acme, "human", "eng_manager", "security")
    check(
        "security_lead authority 5.0 on entity_kind=security",
        abs(sec_on_sec - 5.0) < 1e-6,
    )
    check(
        "engineering_manager authority 4.0 on entity_kind=security",
        abs(eng_on_sec - 4.0) < 1e-6,
    )
    sec_on_gen = authority_for(acme, "human", "security_lead", "general")
    eng_on_gen = authority_for(acme, "human", "eng_manager", "general")
    check(
        "security_lead and eng_manager both 3.0 on entity_kind=general (tie)",
        abs(sec_on_gen - 3.0) < 1e-6 and abs(eng_on_gen - 3.0) < 1e-6,
    )

    # Security entity: eng_manager writes first, security_lead overrides.
    sec_entity = "security:incident-12"
    eng_id, eng_is_cur = remember(
        acme, sec_entity, "the breach is contained",
        "human", "eng_manager", confidence=0.9,
    )
    check(
        "eng_manager's row is initially current (sole live row)",
        eng_is_cur is True,
    )
    sec_id, sec_is_cur = remember(
        acme, sec_entity,
        "the breach is NOT contained, ongoing investigation",
        "human", "security_lead", confidence=0.9,
    )
    check(
        "security_lead's row wins on entity_kind=security (5.0 > 4.0)",
        sec_is_cur is True,
    )
    cur_sec = recall(acme, sec_entity)
    check(
        "recall surfaces security_lead's wording on security entity",
        cur_sec is not None and "NOT contained" in cur_sec.content,
    )
    check(
        "eng_manager's row is now superseded (clean win, no conflict)",
        _is_superseded(eng_id),
    )

    # General entity: both leads tie at authority 3.0 -> conflict surfaced.
    gen_entity = "topic:framework-choice"
    eng_gid, _ = remember(
        acme, gen_entity, "use React for the rewrite",
        "human", "eng_manager", confidence=0.9,
    )
    sec_gid, sec_gid_is_cur = remember(
        acme, gen_entity, "use Vue for the rewrite",
        "human", "security_lead", confidence=0.9,
    )
    # On a tie, BOTH stay live -- so the new row IS current.
    check(
        "tie: new row stays current (not silently superseded)",
        sec_gid_is_cur is True,
    )
    check(
        "tie: previous row stays live (not superseded)",
        not _is_superseded(eng_gid),
    )
    open_confs = pending_conflicts(acme)
    matching = [
        c for c in open_confs
        if c.entity_key == gen_entity
        and {c.row_a, c.row_b} == {eng_gid, sec_gid}
    ]
    check(
        "memory_conflict row exists for the tied pair",
        len(matching) == 1,
    )

    # Resolve via governance.resolve_conflict picking eng_manager's row.
    cid = resolve_conflict(
        acme, eng_gid, sec_gid,
        resolved_by="role:security_lead",
        winner_id=eng_gid,
    )
    check("resolve_conflict returns a conflict id", isinstance(cid, int))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, winner_id, resolved_by FROM memory_conflict "
            "WHERE id = %s", (cid,),
        )
        status, winner_id, resolved_by = cur.fetchone()
    check("conflict status='resolved' after resolve_conflict",
          status == "resolved")
    check("conflict winner_id matches the chosen row",
          winner_id == eng_gid)
    check("conflict resolved_by recorded", resolved_by == "role:security_lead")
    check("loser row is now superseded after resolution",
          _is_superseded(sec_gid))
    check("winner row stays current after resolution",
          not _is_superseded(eng_gid))
    # And the conflict is no longer pending.
    still_pending = [
        c for c in pending_conflicts(acme) if c.id == cid
    ]
    check("resolved conflict drops out of pending list",
          still_pending == [])

    # Backward compat: a workspace with NO policies set up should still
    # work via the flat ladder. Pre-Phase-9 semantics: human (auth 3.0)
    # beats agent (auth 1.0) cleanly, no conflict surfaced.
    bare = "bare-ws"
    bare_eid = "person:rae"
    a_id, _ = remember(
        bare, bare_eid, "rae works on infra",
        "agent", "scanner", confidence=0.7,
    )
    h_id, h_is_cur = remember(
        bare, bare_eid, "Rae leads infra",
        "human", "boss", confidence=0.95,
    )
    check(
        "flat ladder fallback: human wins over agent without any policies",
        h_is_cur is True,
    )
    check(
        "flat ladder fallback: agent row is superseded",
        _is_superseded(a_id),
    )
    bare_open = [
        c for c in pending_conflicts(bare) if c.entity_key == bare_eid
    ]
    check(
        "flat ladder fallback: no conflict surfaced (delta > epsilon)",
        bare_open == [],
    )

    print()
    if _FAILS:
        print(f"FAILED ({len(_FAILS)}): {', '.join(_FAILS)}")
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
