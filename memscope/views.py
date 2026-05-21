"""View-model layer for memscope: cheap aggregate queries over the same
Postgres memlayer uses. Returns plain dicts ready to JSON-serialize."""

from memlayer.db import connect


def workspaces() -> list[str]:
    """All distinct workspaces seen in raw_events or memory."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT workspace FROM raw_events "
            "UNION SELECT workspace FROM memory "
            "ORDER BY 1"
        )
        return [r[0] for r in cur.fetchall()]


def entities(workspace: str) -> list[dict]:
    """All entity_keys in a workspace plus version count."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT entity_key,
                   count(*) AS versions,
                   bool_or(superseded_by IS NULL) AS has_current
            FROM memory
            WHERE workspace = %s
            GROUP BY entity_key
            ORDER BY entity_key
            """,
            (workspace,),
        )
        return [
            {"entity_key": r[0], "versions": r[1], "has_current": r[2]}
            for r in cur.fetchall()
        ]


def memory_dag(workspace: str, entity_key: str) -> dict:
    """Memory rows for an entity as graph nodes + supersede edges. Oldest
    first so a frontend layout maps id-order directly to top-down position."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, content, source_type, source_id, confidence,
                   superseded_by, created_at,
                   superseded_by IS NULL AS is_current
            FROM memory
            WHERE workspace = %s AND entity_key = %s
            ORDER BY id
            """,
            (workspace, entity_key),
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
