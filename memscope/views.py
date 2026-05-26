"""View-model layer for memscope: cheap aggregate queries over the same
Postgres memlayer uses. Returns plain dicts ready to JSON-serialize."""

from dataclasses import asdict

from memlayer.connectors.local_files import read_dir
from memlayer.db import connect
from memlayer.ingest import ingest
from memlayer.retrieval import search_with_expansions


def workspaces() -> list[str]:
    """All distinct workspaces seen in raw_events or memory."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT workspace FROM raw_events "
            "UNION SELECT workspace FROM memory "
            "ORDER BY 1"
        )
        return [r[0] for r in cur.fetchall()]


def entities(workspace: str, principals: list[str]) -> list[dict]:
    """All entity_keys in a workspace plus version count, ACL-filtered. An
    entity only appears if at least one of its memory rows is visible to the
    caller's principals -- otherwise restricted entity_keys would be
    enumerable by anyone."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT entity_key,
                   count(*) AS versions,
                   bool_or(superseded_by IS NULL) AS has_current
            FROM memory
            WHERE workspace = %s
              AND allowed_principals && %s::text[]
            GROUP BY entity_key
            ORDER BY entity_key
            """,
            (workspace, principals),
        )
        return [
            {"entity_key": r[0], "versions": r[1], "has_current": r[2]}
            for r in cur.fetchall()
        ]


def memory_dag(
    workspace: str, entity_key: str, principals: list[str]
) -> dict:
    """Memory rows for an entity as graph nodes + supersede edges. Oldest
    first so a frontend layout maps id-order directly to top-down position.
    ACL-filtered against the caller's principals so restricted rows are
    never returned -- this endpoint applies the same pre-filter as the
    retrieval arm."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, content, source_type, source_id, confidence,
                   superseded_by, created_at,
                   superseded_by IS NULL AS is_current
            FROM memory
            WHERE workspace = %s
              AND entity_key = %s
              AND allowed_principals && %s::text[]
            ORDER BY id
            """,
            (workspace, entity_key, principals),
        )
        rows = cur.fetchall()

    nodes = [
        {
            "id": r[0],
            "content": r[1],
            "source_type": r[2],
            "source_id": r[3],
            "confidence": float(r[4]),
            "created_at": r[6].isoformat(),
            "is_current": r[7],
        }
        for r in rows
    ]
    edges = [{"from": r[0], "to": r[5]} for r in rows if r[5] is not None]
    return {
        "workspace": workspace,
        "entity_key": entity_key,
        "nodes": nodes,
        "edges": edges,
    }


def search_hits(
    workspace: str, query: str, principals: list[str], k: int = 10
) -> dict:
    """Run hybrid retrieval and shape Hits as JSON-friendly dicts. The arms
    tuple becomes a list so it serializes cleanly; everything else is already
    primitive.

    Also surfaces the federated query-expansion terms used to widen the
    lexical arms so the UI can show "also searched for: ..."."""
    hits, expansions = search_with_expansions(query, workspace, principals, k=k)
    return {
        "workspace": workspace,
        "query": query,
        "principals": principals,
        "expansions": expansions,
        "hits": [
            {
                "id": h.id,
                "kind": h.kind,
                "text": h.text,
                "score": h.score,
                "arms": list(h.arms),
                "provenance": h.provenance,
                "confidence": (
                    float(h.confidence) if h.confidence is not None else None
                ),
            }
            for h in hits
        ],
    }


def pipeline_stats(workspace: str, principals: list[str]) -> dict:
    """Workspace-wide counters that make the dedup gate visible: distinct
    content hashes vs total chunk_acl rows shows how much of the workspace's
    chunk volume is shared content, and total embeddings being a global count
    underscores that embeddings are content-addressed (not per-workspace).

    Principal-visible counts. The chunk and memory counters apply the same
    ACL pre-filter the retrieval arm uses, so a caller cannot infer the
    existence of restricted content from a count delta.

    raw_events and by_source are intentionally workspace-level not
    principal-level: raw events have no per-chunk ACL of their own, and
    ingestion is workspace-scoped, so a workspace member legitimately sees
    their own events. (A stricter pass would join through event_chunks ->
    chunk_acl, but the simpler workspace-level count is acceptable for now.)

    total_embeddings is global -- embeddings are content-addressed, not
    per-workspace -- and is renamed to total_embeddings_global so a caller
    doesn't read it as workspace-scoped."""
    with connect() as conn, conn.cursor() as cur:
        # workspace-level (intentionally not principal-filtered; see above).
        cur.execute(
            "SELECT count(*) FROM raw_events WHERE workspace = %s", (workspace,)
        )
        raw_events = cur.fetchone()[0]

        # ACL-filtered: counts visible chunks only.
        cur.execute(
            "SELECT count(*) FROM chunk_acl "
            "WHERE workspace = %s AND allowed_principals && %s::text[]",
            (workspace, principals),
        )
        chunks_in_workspace = cur.fetchone()[0]

        cur.execute(
            "SELECT count(DISTINCT content_hash) FROM chunk_acl "
            "WHERE workspace = %s AND allowed_principals && %s::text[]",
            (workspace, principals),
        )
        unique_content_hashes_in_workspace = cur.fetchone()[0]

        # Global counter: embeddings are content-addressed, no per-workspace
        # ACL exists. Renamed in the response to make that clear to callers.
        cur.execute("SELECT count(*) FROM embeddings")
        total_embeddings_global = cur.fetchone()[0]

        # ACL-filtered: counts visible memory rows only.
        cur.execute(
            "SELECT count(*) FROM memory "
            "WHERE workspace = %s AND allowed_principals && %s::text[]",
            (workspace, principals),
        )
        memory_rows = cur.fetchone()[0]

        # workspace-level (intentionally not principal-filtered; see above).
        cur.execute(
            """
            SELECT source, count(*)
            FROM raw_events
            WHERE workspace = %s
            GROUP BY source
            ORDER BY 2 DESC
            """,
            (workspace,),
        )
        by_source = [{"source": r[0], "items": r[1]} for r in cur.fetchall()]

    return {
        "workspace": workspace,
        "raw_events": raw_events,
        "chunks_in_workspace": chunks_in_workspace,
        "unique_content_hashes_in_workspace": unique_content_hashes_in_workspace,
        "total_embeddings_global": total_embeddings_global,
        "memory_rows": memory_rows,
        "by_source": by_source,
    }


def pipeline_ingest(
    workspace: str, dir: str, allowed_principals: list[str]
) -> dict:
    """Read a local-files directory and run it through the ingest pipeline.
    Returns IngestStats as a dict so callers can see dedup behavior (computed
    vs reused embeddings)."""
    items = read_dir(dir, allowed_principals=allowed_principals)
    stats = ingest(items, workspace=workspace)
    return asdict(stats)


# ---------------------------------------------------------------------------
# Phase 7: identity unification view-models.
#
# Identities expose cross-source aliases for a workspace. Like all other
# memscope views, callers pass `principals` so the same ACL pre-filter is
# enforced -- an identity is only returned if at least one memory row for
# its entity_key is visible to the caller.
# ---------------------------------------------------------------------------


def identities_for_workspace(
    workspace: str, principals: list[str]
) -> list[dict]:
    """List identities in a workspace, ACL-filtered. An identity is
    *visible* if at least one memory row for its entity_key would survive
    the caller's principals filter -- otherwise the identity itself is a
    leak channel (its existence implies the existence of restricted
    memory). Identities with no memory rows at all are visible -- a fresh
    seed isn't a leak."""
    from memlayer.identity import list_identities  # local import to avoid cycle

    all_idents = list_identities(workspace)
    if not all_idents:
        return []
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT entity_key
            FROM memory
            WHERE workspace = %s
              AND allowed_principals && %s::text[]
            """,
            (workspace, principals),
        )
        visible_keys = {r[0] for r in cur.fetchall()}
        # entity_keys that exist in memory at all (visible or not)
        cur.execute(
            "SELECT DISTINCT entity_key FROM memory WHERE workspace = %s",
            (workspace,),
        )
        any_memory_keys = {r[0] for r in cur.fetchall()}

    out = []
    for ident in all_idents:
        key = ident["entity_key"]
        # Show identities with NO memory rows at all (fresh) and those
        # whose memory is at least partially visible.
        if key in any_memory_keys and key not in visible_keys:
            continue
        out.append(ident)
    return out


def identity_details(
    identity_id: int, principals: list[str]
) -> dict | None:
    """Full details for one identity: aliases + the memory rows for its
    entity_key that the caller may see + history."""
    from memlayer.identity import get_identity  # local import
    from memlayer.writeback import history, recall

    ident = get_identity(identity_id)
    if ident is None:
        return None

    # get_identity doesn't carry workspace in its return shape; fetch it
    # directly so we can call recall/history under ACL filter.
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT workspace FROM identity WHERE id = %s", (identity_id,))
        wrow = cur.fetchone()
        if wrow is None:
            return None
        workspace = wrow[0]

    current = recall(workspace, ident["entity_key"], principals=principals)
    rows = history(workspace, ident["entity_key"], principals=principals)
    hist = [
        {
            "id": int(r[0]),
            "content": r[1],
            "source_type": r[2],
            "source_id": r[3],
            "confidence": float(r[4]),
            "superseded_by": int(r[5]) if r[5] is not None else None,
            "created_at": r[6].isoformat() if r[6] else None,
        }
        for r in rows
    ]
    return {
        "identity": {**ident, "workspace": workspace},
        "current_memory": (
            {
                "id": current.id,
                "content": current.content,
                "source_type": current.source_type,
                "source_id": current.source_id,
                "confidence": current.confidence,
                "tier": current.tier,
            }
            if current is not None
            else None
        ),
        "history": hist,
    }


def merge_proposals_for_workspace(workspace: str) -> list[dict]:
    from memlayer.identity import list_proposals  # local import
    return list_proposals(workspace, status="pending")


def cluster_graph_view(identity_id: int) -> dict | None:
    from memlayer.identity import cluster_graph  # local import
    return cluster_graph(identity_id)


# ---------------------------------------------------------------------------
# Phase 8-10 inspector view-models: governance conflicts, compression lineage,
# federated concept ontology, privacy-filter redactions, decay preview.
#
# Like the rest of memscope, every read that touches per-row content applies
# the caller's ACL pre-filter (allowed_principals && principals) so restricted
# content never leaves the database. The federated `global` concept view is
# the one intentional cross-tenant read -- and it only exposes aggregate
# vocabulary (names + summed counts), never per-tenant attribution.
# ---------------------------------------------------------------------------


def governance_conflicts(workspace: str) -> list[dict]:
    """Pending governance conflicts for a workspace, each hydrated with the
    two memory rows in dispute so the UI can show what's actually being
    contested. The authority recorded on the conflict row is attached to the
    matching side."""
    from memlayer.governance import pending_conflicts  # local import

    conflicts = pending_conflicts(workspace)
    if not conflicts:
        return []

    # Collect every memory id we need to hydrate in one round trip.
    row_ids = sorted({c.row_a for c in conflicts} | {c.row_b for c in conflicts})
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, content, source_type, source_id
            FROM memory
            WHERE id = ANY(%s)
            """,
            (row_ids,),
        )
        by_id = {
            r[0]: {
                "id": r[0],
                "content": r[1],
                "source_type": r[2],
                "source_id": r[3],
            }
            for r in cur.fetchall()
        }

    out = []
    for c in conflicts:
        row_a = dict(by_id.get(c.row_a, {"id": c.row_a}))
        row_b = dict(by_id.get(c.row_b, {"id": c.row_b}))
        row_a["authority"] = c.authority_a
        row_b["authority"] = c.authority_b
        out.append(
            {
                "id": c.id,
                "entity_key": c.entity_key,
                "row_a": row_a,
                "row_b": row_b,
                "detected_at": c.detected_at.isoformat() if c.detected_at else None,
            }
        )
    return out


def lookup_conflict(conflict_id: int) -> dict | None:
    """Fetch the workspace + the two disputed row ids for a conflict so the
    resolve endpoint can authorize on the workspace and call into governance.
    Returns None if no such conflict exists."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT workspace, row_a, row_b, status "
            "FROM memory_conflict WHERE id = %s",
            (conflict_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "workspace": row[0],
        "row_a": int(row[1]),
        "row_b": int(row[2]),
        "status": row[3],
    }


def compression_view(
    workspace: str, entity_key: str, principals: list[str]
) -> dict:
    """Episodic summaries for (workspace, entity_key) that have per-sentence
    lineage, each with its source rows resolved.

    ACL-filtered: a summary is only returned if it is itself visible to the
    caller's principals. The summary row's allowed_principals is the
    intersection of its source rows' ACLs (see compress.py), so filtering on
    the summary row is the correct gate -- a caller who can see the summary
    can see at least the merged-down permission set. Source-row content is
    additionally ACL-filtered, so a source row the caller can't see shows up
    with null content rather than leaking text."""
    with connect() as conn, conn.cursor() as cur:
        # Episodic summaries for this entity that actually have lineage, and
        # are visible to the caller.
        cur.execute(
            """
            SELECT m.id, m.content, m.derived_from
            FROM memory m
            WHERE m.workspace = %s
              AND m.entity_key = %s
              AND m.tier = 'episodic'
              AND m.allowed_principals && %s::text[]
              AND EXISTS (
                  SELECT 1 FROM summary_lineage sl WHERE sl.summary_id = m.id
              )
            ORDER BY m.id
            """,
            (workspace, entity_key, principals),
        )
        summary_rows = cur.fetchall()
        if not summary_rows:
            return {"workspace": workspace, "entity_key": entity_key, "summaries": []}

        summary_ids = [r[0] for r in summary_rows]

        # All lineage rows for these summaries in one shot.
        cur.execute(
            """
            SELECT summary_id, sentence_index, sentence, source_row_id
            FROM summary_lineage
            WHERE summary_id = ANY(%s)
            ORDER BY summary_id, sentence_index
            """,
            (summary_ids,),
        )
        lineage_rows = cur.fetchall()

        # Resolve source-row content, ACL-filtered so restricted source text
        # is not leaked through the lineage view.
        source_ids = sorted({r[3] for r in lineage_rows})
        source_content: dict[int, str] = {}
        if source_ids:
            cur.execute(
                """
                SELECT id, content
                FROM memory
                WHERE id = ANY(%s)
                  AND allowed_principals && %s::text[]
                """,
                (source_ids, principals),
            )
            source_content = {r[0]: r[1] for r in cur.fetchall()}

    lineage_by_summary: dict[int, list[dict]] = {}
    for sid, sentence_index, sentence, source_row_id in lineage_rows:
        lineage_by_summary.setdefault(sid, []).append(
            {
                "sentence_index": sentence_index,
                "sentence": sentence,
                "source_row_id": source_row_id,
                "source_content": source_content.get(source_row_id),
            }
        )

    summaries = []
    for sid, content, derived_from in summary_rows:
        summaries.append(
            {
                "summary_id": sid,
                "content": content,
                "derived_from": list(derived_from) if derived_from else [],
                "sentences": lineage_by_summary.get(sid, []),
            }
        )
    return {
        "workspace": workspace,
        "entity_key": entity_key,
        "summaries": summaries,
    }


def concepts_view(workspace: str) -> dict:
    """Federated concept ontology view.

    `global` is the cross-tenant aggregate (top concepts by breadth then
    volume) -- this is safe to expose: it is vocabulary + summed counts, never
    per-tenant attribution. `local` is THIS workspace's vocabulary only (the
    one place we read tenant_concept, scoped to the caller's workspace -- we
    never return another tenant's tenant_concept rows). `synonyms` are the top
    cross-tenant synonym pairs resolved to concept names."""
    with connect() as conn, conn.cursor() as cur:
        # Global aggregate: cross-tenant, safe (names + summed counts only).
        cur.execute(
            """
            SELECT name, tenant_count, global_count
            FROM concept
            ORDER BY tenant_count DESC, global_count DESC, name ASC
            LIMIT 30
            """
        )
        global_concepts = [
            {"name": r[0], "tenant_count": r[1], "global_count": r[2]}
            for r in cur.fetchall()
        ]

        # Local: only this workspace's tenant_concept rows. NEVER another
        # tenant's.
        cur.execute(
            """
            SELECT c.name, tc.local_count
            FROM tenant_concept tc
            JOIN concept c ON c.id = tc.concept_id
            WHERE tc.workspace = %s
            ORDER BY tc.local_count DESC, c.name ASC
            LIMIT 30
            """,
            (workspace,),
        )
        local_concepts = [
            {"name": r[0], "local_count": r[1]} for r in cur.fetchall()
        ]

        # Synonyms: cross-tenant aggregate pairs resolved to names.
        cur.execute(
            """
            SELECT ca.name, cb.name, s.cooccurrence_tenants, s.confidence
            FROM concept_synonym s
            JOIN concept ca ON ca.id = s.concept_a
            JOIN concept cb ON cb.id = s.concept_b
            ORDER BY s.confidence DESC, s.cooccurrence_tenants DESC
            LIMIT 30
            """
        )
        synonyms = [
            {
                "a": r[0],
                "b": r[1],
                "cooccurrence_tenants": r[2],
                "confidence": float(r[3]),
            }
            for r in cur.fetchall()
        ]

    return {
        "workspace": workspace,
        "global": global_concepts,
        "local": local_concepts,
        "synonyms": synonyms,
    }


def redactions_view(workspace: str) -> dict:
    """Recent privacy-filter redactions for a workspace. The redactions_log
    stores only metadata about what was scrubbed (kind + match count + source),
    never the secret content itself, so this is safe to surface directly."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT kind, count, source, source_id, occurred_at
            FROM redactions_log
            WHERE workspace = %s
            ORDER BY occurred_at DESC
            LIMIT 50
            """,
            (workspace,),
        )
        redactions = [
            {
                "kind": r[0],
                "count": r[1],
                "source": r[2],
                "source_id": r[3],
                "occurred_at": r[4].isoformat() if r[4] else None,
            }
            for r in cur.fetchall()
        ]
    return {"workspace": workspace, "redactions": redactions}


def decay_preview(workspace: str) -> dict:
    """Per-workspace dry-run of what eviction WOULD remove. Mirrors the logic
    in decay.evict_stale (a row is evictable when it is both older than its
    tier's max age AND below the tier's effective-confidence cutoff), but
    scoped to a single workspace -- evict_stale itself is global. Nothing is
    deleted here."""
    from memlayer.decay import _EVICTION_RULES, effective_confidence_sql

    eff_sql = effective_confidence_sql("memory")
    evictable: dict[str, int] = {}
    examples: list[dict] = []

    with connect() as conn, conn.cursor() as cur:
        for tier, (max_age, cutoff) in _EVICTION_RULES.items():
            cur.execute(
                f"""
                SELECT count(*) FROM memory
                WHERE workspace = %s
                  AND tier = %s
                  AND now() - last_referenced_at > make_interval(secs => %s)
                  AND ({eff_sql}) < %s
                """,
                (workspace, tier, max_age, cutoff),
            )
            evictable[tier] = cur.fetchone()[0]

            # A few example rows so the UI can show *what* would go.
            cur.execute(
                f"""
                SELECT id, entity_key, tier, ({eff_sql}) AS effective_confidence,
                       last_referenced_at
                FROM memory
                WHERE workspace = %s
                  AND tier = %s
                  AND now() - last_referenced_at > make_interval(secs => %s)
                  AND ({eff_sql}) < %s
                ORDER BY last_referenced_at ASC
                LIMIT 5
                """,
                (workspace, tier, max_age, cutoff),
            )
            for r in cur.fetchall():
                examples.append(
                    {
                        "id": r[0],
                        "entity_key": r[1],
                        "tier": r[2],
                        "effective_confidence": float(r[3]),
                        "last_referenced_at": (
                            r[4].isoformat() if r[4] else None
                        ),
                    }
                )

    return {
        "workspace": workspace,
        "evictable": evictable,
        "examples": examples,
    }
