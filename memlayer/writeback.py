"""Write-back: agents and humans record durable memory derived from raw
events. Append-only -- content is never mutated or deleted; a losing memory
only gets a superseded_by pointer, so the full history stays auditable.

Conflict resolution when several memories describe the same entity_key:
precedence is (source rank, confidence, recency). Human overrides system
overrides agent; then higher confidence; then newer. Exactly one memory per
entity is "current" (superseded_by IS NULL)."""

import math
from dataclasses import dataclass

from memlayer.db import connect

_SOURCE_RANK = {"human": 3, "system": 2, "agent": 1}
_VALID_SOURCE_TYPES = frozenset(_SOURCE_RANK.keys())


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
    # Validate source_type and confidence here in addition to the schema
    # CHECK constraints so callers get a clear ValueError before the
    # database round-trip. source_type controls precedence (human > system
    # > agent), so an arbitrary string would either land at rank 0 or, if
    # the schema check were missing, let an attacker claim 'human'.
    # confidence=inf/NaN would short-circuit the (rank, conf, id) tuple
    # comparison in _precedence.
    if source_type not in _VALID_SOURCE_TYPES:
        raise ValueError(
            f"source_type must be one of {sorted(_VALID_SOURCE_TYPES)}, "
            f"got {source_type!r}"
        )
    if not math.isfinite(confidence) or not (0.0 <= confidence <= 1.0):
        raise ValueError(
            f"confidence must be a finite float in [0.0, 1.0], "
            f"got {confidence!r}"
        )
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


def recall(
    workspace: str,
    entity_key: str,
    principals: list[str] | None = None,
) -> MemoryRow | None:
    """The single current memory for an entity, or None.

    If `principals` is given, the same `allowed_principals && %s::text[]`
    pre-filter retrieval uses is applied here so callers can't recall a row
    they would not be allowed to surface via search. If `principals` is None,
    no ACL filter is applied (backward-compatible default for trusted callers
    like the demo and CLI scripts)."""
    sql = """
        SELECT id, content, source_type, source_id, confidence, derived_from
        FROM memory
        WHERE workspace = %s AND entity_key = %s AND superseded_by IS NULL
    """
    params: tuple = (workspace, entity_key)
    if principals is not None:
        sql += " AND allowed_principals && %s::text[]"
        params = params + (principals,)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return MemoryRow(*row) if row else None


def history(
    workspace: str,
    entity_key: str,
    principals: list[str] | None = None,
) -> list[tuple]:
    """Full audit trail for an entity, oldest first: every memory ever
    written and what superseded it.

    If `principals` is given, only rows whose `allowed_principals` overlap
    are returned -- same ACL pre-filter as retrieval. If None, returns all
    rows (backward-compatible)."""
    sql = """
        SELECT id, content, source_type, source_id, confidence,
               superseded_by, created_at
        FROM memory
        WHERE workspace = %s AND entity_key = %s
    """
    params: tuple = (workspace, entity_key)
    if principals is not None:
        sql += " AND allowed_principals && %s::text[]"
        params = params + (principals,)
    sql += " ORDER BY id"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()
