"""View-model layer for memscope: cheap aggregate queries over the same
Postgres memlayer uses. Returns plain dicts ready to JSON-serialize."""

from dataclasses import asdict

from memlayer.connectors.local_files import read_dir
from memlayer.db import connect
from memlayer.ingest import ingest
from memlayer.retrieval import search


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
    primitive."""
    hits = search(query, workspace, principals, k=k)
    return {
        "workspace": workspace,
        "query": query,
        "principals": principals,
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
