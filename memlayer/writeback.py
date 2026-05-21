"""Write-back: agents and humans record durable memory derived from raw
events. Append-only -- content is never mutated or deleted; a losing memory
only gets a superseded_by pointer, so the full history stays auditable.

Conflict resolution when several memories describe the same entity_key:
precedence is (source rank, confidence, recency). Human overrides system
overrides agent; then higher confidence; then newer. Exactly one memory per
entity is "current" (superseded_by IS NULL).

Phase 6 (tiered memory + Ebbinghaus decay): each memory row carries a
``tier`` (working / episodic / semantic / procedural) and a
``last_referenced_at`` timestamp. Effective confidence decays with age on a
tier-specific half-life and is reinforced (timestamp bumped to ``now()``) on
every read. Conflict resolution still uses raw stored confidence so a fresh
human correction always beats a stale agent guess regardless of decay -- the
decay signal feeds ranking and eviction, not the supersede ladder."""

import math
from dataclasses import dataclass

from memlayer.db import connect
from memlayer.decay import VALID_TIERS, effective_confidence_sql

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
    tier: str = "semantic"
    effective_confidence: float | None = None


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
    tier: str = "semantic",
) -> tuple[int, bool]:
    """Append a memory. Returns (memory_id, is_current). is_current is False
    when an existing memory outranks this one -- the new row is still stored
    for audit, but immediately superseded.

    ``tier`` controls decay behaviour; default 'semantic' matches the
    existing "extracted durable fact" assumption of pre-Phase-6 data."""
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
    if tier not in VALID_TIERS:
        raise ValueError(
            f"tier must be one of {sorted(VALID_TIERS)}, got {tier!r}"
        )
    derived_from = derived_from or []
    allowed_principals = allowed_principals or ["group:all"]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO memory
                (workspace, entity_key, content, source_type, source_id,
                 derived_from, confidence, allowed_principals, tier)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                tier,
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
    """The single current memory for an entity, or None.

    Ranking respects (source_rank, effective_confidence) so a stale row only
    loses if a fresher, decay-adjusted row beats it -- raw ``confidence`` is
    still what supersession is decided on, but among the live row(s) we pick
    the strongest signal. Reading reinforces the row (bumps
    ``last_referenced_at`` to ``now()``) so durable facts stay sharp."""
    eff = effective_confidence_sql("memory")
    with connect() as conn, conn.cursor() as cur:
        # Pick the best live row for this entity. Source rank dominates so
        # human > system > agent is still inviolable; effective_confidence
        # breaks ties at the same rank.
        cur.execute(
            f"""
            SELECT id, content, source_type, source_id, confidence,
                   derived_from, tier, ({eff}) AS effective_confidence
            FROM memory
            WHERE workspace = %s AND entity_key = %s AND superseded_by IS NULL
            ORDER BY
                CASE source_type
                    WHEN 'human' THEN 3
                    WHEN 'system' THEN 2
                    WHEN 'agent' THEN 1
                    ELSE 0
                END DESC,
                effective_confidence DESC,
                id DESC
            LIMIT 1
            """,
            (workspace, entity_key),
        )
        row = cur.fetchone()
        if row is None:
            return None
        # Reinforcement: reading a memory bumps its decay clock. Same
        # transaction so a concurrent eviction can't see a half-state.
        cur.execute(
            "UPDATE memory SET last_referenced_at = now() WHERE id = %s",
            (row[0],),
        )
        conn.commit()
    return MemoryRow(
        id=row[0],
        content=row[1],
        source_type=row[2],
        source_id=row[3],
        confidence=row[4],
        derived_from=row[5],
        tier=row[6],
        effective_confidence=float(row[7]) if row[7] is not None else None,
    )


def history(workspace: str, entity_key: str) -> list[tuple]:
    """Full audit trail for an entity, oldest first: every memory ever
    written and what superseded it. History is a *passive* view -- looking
    at the trail does NOT reinforce the rows (otherwise the audit endpoint
    would defeat decay)."""
    eff = effective_confidence_sql("memory")
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, content, source_type, source_id, confidence,
                   superseded_by, created_at, tier, last_referenced_at,
                   ({eff}) AS effective_confidence
            FROM memory
            WHERE workspace = %s AND entity_key = %s
            ORDER BY id
            """,
            (workspace, entity_key),
        )
        return cur.fetchall()
