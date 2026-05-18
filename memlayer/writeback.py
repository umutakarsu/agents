"""Write-back: agents and humans record durable memory derived from raw
events. Append-only -- content is never mutated or deleted; a losing memory
only gets a superseded_by pointer, so the full history stays auditable.

Conflict resolution when several memories describe the same entity_key:
precedence is (source rank, confidence, recency). Human overrides system
overrides agent; then higher confidence; then newer. Exactly one memory per
entity is "current" (superseded_by IS NULL)."""

from dataclasses import dataclass

from memlayer.db import connect

_SOURCE_RANK = {"human": 3, "system": 2, "agent": 1}


@dataclass
class MemoryRow:
    id: int
    content: str
    source_type: str
    source_id: str
    confidence: float
    derived_from: list[int]


def _precedence(source_type: str, confidence: float, mem_id: int) -> tuple:
    # Higher tuple wins. mem_id is a monotonic tiebreaker == "newer".
    return (_SOURCE_RANK.get(source_type, 0), confidence, mem_id)


def remember(
    workspace: str,
    entity_key: str,
    content: str,
    source_type: str,
    source_id: str,
    derived_from: list[int] | None = None,
    confidence: float = 0.5,
    allowed_principals: list[str] | None = None,
) -> tuple[int, bool]:
    """Append a memory. Returns (memory_id, is_current). is_current is False
    when an existing memory outranks this one -- the new row is still stored
    for audit, but immediately superseded."""
    derived_from = derived_from or []
    allowed_principals = allowed_principals or ["group:all"]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO memory
                (workspace, entity_key, content, source_type, source_id,
                 derived_from, confidence, allowed_principals)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                workspace,
                entity_key,
                content,
                source_type,
                source_id,
                derived_from,
                confidence,
                allowed_principals,
            ),
        )
        new_id = cur.fetchone()[0]

        # All currently-live memories for this entity, including the new row.
        cur.execute(
            """
            SELECT id, source_type, confidence
            FROM memory
            WHERE workspace = %s AND entity_key = %s AND superseded_by IS NULL
            """,
            (workspace, entity_key),
        )
        live = cur.fetchall()

        winner_id = max(
            live, key=lambda r: _precedence(r[1], r[2], r[0])
        )[0]

        # Everyone live except the winner points at the winner. Append-only:
        # we only ever set a NULL superseded_by, never rewrite content.
        for mem_id, _, _ in live:
            if mem_id != winner_id:
                cur.execute(
                    "UPDATE memory SET superseded_by = %s "
                    "WHERE id = %s AND superseded_by IS NULL",
                    (winner_id, mem_id),
                )
        conn.commit()
    return new_id, winner_id == new_id


def recall(workspace: str, entity_key: str) -> MemoryRow | None:
    """The single current memory for an entity, or None."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, content, source_type, source_id, confidence, derived_from
            FROM memory
            WHERE workspace = %s AND entity_key = %s AND superseded_by IS NULL
            """,
            (workspace, entity_key),
        )
        row = cur.fetchone()
    return MemoryRow(*row) if row else None


def history(workspace: str, entity_key: str) -> list[tuple]:
    """Full audit trail for an entity, oldest first: every memory ever
    written and what superseded it."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, content, source_type, source_id, confidence,
                   superseded_by, created_at
            FROM memory
            WHERE workspace = %s AND entity_key = %s
            ORDER BY id
            """,
            (workspace, entity_key),
        )
        return cur.fetchall()
