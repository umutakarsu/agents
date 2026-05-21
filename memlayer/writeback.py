"""Write-back: agents and humans record durable memory derived from raw
events. Append-only -- content is never mutated or deleted; a losing memory
only gets a superseded_by pointer, so the full history stays auditable.

Conflict resolution when several memories describe the same entity_key:
precedence is (authority, confidence, recency) where ``authority`` comes
from the governance model (``memlayer.governance``). With no policies
configured the authority lookup falls back to the flat ladder (human=3.0,
system=2.0, agent=1.0), so pre-Phase-9 behaviour is preserved.

When the top two live rows have authorities that are effectively equal
(within ``governance.CONFLICT_EPSILON``), the system records a row in
``memory_conflict`` and leaves *both* live. A higher-authority role -- or
a human via the CLI -- can resolve the conflict later. This is what
prevents two equal-rank humans from silently overwriting each other.

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


def _precedence(
    source_type: str,
    source_id: str,
    confidence: float,
    mem_id: int,
    workspace: str,
    entity_kind: str,
) -> tuple:
    """Precedence tuple compared lexicographically; higher wins.

    Authority is governance-driven: ``authority_for`` looks up the writer's
    role and the matching policy, or falls back to the flat ladder. The
    ``mem_id`` tiebreaker keeps "newer wins" semantics when authority +
    confidence are identical.
    """
    # Local import to avoid an import cycle: governance imports db only,
    # but writeback is imported eagerly by demo / scripts and we want the
    # governance module to remain optional from a load-order perspective.
    from memlayer.governance import authority_for
    auth = authority_for(workspace, source_type, source_id, entity_kind)
    return (auth, confidence, mem_id)


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
    # Classify the entity so policy lookups have something to match on.
    # Done at write time and stored on the row so retrieval / debugging
    # doesn't have to re-derive it (and so re-tagging a row is just an
    # UPDATE -- no behaviour change needed in this function).
    from memlayer.governance import (
        CONFLICT_EPSILON,
        _authority_with_origin,
        classify_entity,
        record_conflict,
    )
    entity_kind = classify_entity(entity_key)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO memory
                (workspace, entity_key, content, source_type, source_id,
                 derived_from, confidence, allowed_principals, tier,
                 entity_kind)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                entity_kind,
            ),
        )
        new_id = cur.fetchone()[0]

        # All currently-live memories for this entity, including the new row.
        cur.execute(
            """
            SELECT id, source_type, source_id, confidence
            FROM memory
            WHERE workspace = %s AND entity_key = %s AND superseded_by IS NULL
            """,
            (workspace, entity_key),
        )
        live = cur.fetchall()

    # Compute authority + precedence tuple for each live row outside the
    # cursor loop -- ``authority_for`` opens its own connection, and nesting
    # psycopg connections in the same context-manager block deadlocks.
    # ``scored`` rows are (id, authority, confidence, source_type, source_id,
    # precedence_tuple, governed_flag).
    scored = []
    for row in live:
        mem_id, stype, sid, conf = row
        auth, governed = _authority_with_origin(
            workspace, stype, sid, entity_kind
        )
        # ``prec`` mirrors what _precedence() would return for this row; we
        # build it inline rather than calling _precedence so we don't make
        # the authority lookup twice (it does its own DB round-trip).
        prec = (auth, conf, mem_id)
        scored.append((mem_id, auth, conf, stype, sid, prec, governed))

    # Pick a winner with the governance-aware precedence tuple. Lexicographic
    # comparison: highest authority, then highest confidence, then newest id.
    winner = max(scored, key=lambda s: s[5])
    winner_id = winner[0]

    # Tie detection: any other live row whose authority is within epsilon
    # of the winner's authority is treated as an unresolved conflict -- but
    # only when at least one of the two rows is *governed* (i.e. the
    # authority came from an explicit role/policy rather than the
    # flat-ladder fallback). Two ungoverned rows resolve the pre-Phase-9
    # way: confidence + recency in _precedence break the tie cleanly and no
    # memory_conflict row is surfaced. This is what keeps demos / data
    # without policies set up behaving exactly like before.
    winner_auth = winner[1]
    winner_governed = winner[6]
    tie_ids: set[int] = {winner_id}
    for mem_id, auth, _conf, _stype, _sid, _prec, gov in scored:
        if mem_id == winner_id:
            continue
        if abs(auth - winner_auth) > CONFLICT_EPSILON:
            continue
        if not (gov or winner_governed):
            # Both ungoverned -> behave like the old flat ladder + confidence.
            continue
        tie_ids.add(mem_id)

    with connect() as conn, conn.cursor() as cur:
        # Supersede every non-tie loser. Append-only: we only ever set a
        # NULL superseded_by, never rewrite content.
        for mem_id, _auth, _conf, _stype, _sid, _prec, _gov in scored:
            if mem_id in tie_ids:
                continue
            cur.execute(
                "UPDATE memory SET superseded_by = %s "
                "WHERE id = %s AND superseded_by IS NULL",
                (winner_id, mem_id),
            )
        conn.commit()

    # Record a pending conflict for each tie row paired with the winner.
    # ``record_conflict`` is idempotent on (workspace, row_a, row_b) so a
    # repeated write that re-detects the same tie does not duplicate.
    if len(tie_ids) > 1:
        winner_auth_v = winner_auth
        # Map id -> authority for fast lookup.
        auth_by_id = {s[0]: s[1] for s in scored}
        for tid in tie_ids:
            if tid == winner_id:
                continue
            record_conflict(
                workspace,
                entity_key,
                winner_id,
                tid,
                winner_auth_v,
                auth_by_id[tid],
            )

    # is_current reflects whether the new row is *currently un-superseded*.
    # When the new row ties with an existing winner, both stay live -- so
    # is_current is True for the new row too.
    return new_id, new_id in tie_ids


def recall(
    workspace: str,
    entity_key: str,
    principals: list[str] | None = None,
) -> MemoryRow | None:
    """The single current memory for an entity, or None.

    Ranking respects (source_rank, effective_confidence) so a stale row only
    loses if a fresher, decay-adjusted row beats it -- raw ``confidence`` is
    still what supersession is decided on, but among the live row(s) we pick
    the strongest signal. Reading reinforces the row (bumps
    ``last_referenced_at`` to ``now()``) so durable facts stay sharp.

    If ``principals`` is given, the same ``allowed_principals && %s::text[]``
    pre-filter retrieval uses is applied here so callers can't recall a row
    they would not be allowed to surface via search. ``None`` skips the
    filter (backward-compatible default for trusted callers like the demo
    and CLI scripts)."""
    eff = effective_confidence_sql("memory")
    sql = f"""
        SELECT id, content, source_type, source_id, confidence,
               derived_from, tier, ({eff}) AS effective_confidence
        FROM memory
        WHERE workspace = %s AND entity_key = %s AND superseded_by IS NULL
    """
    params: tuple = (workspace, entity_key)
    if principals is not None:
        sql += " AND allowed_principals && %s::text[]"
        params = params + (principals,)
    sql += """
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
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
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


def history(
    workspace: str,
    entity_key: str,
    principals: list[str] | None = None,
) -> list[tuple]:
    """Full audit trail for an entity, oldest first: every memory ever
    written and what superseded it. History is a *passive* view -- looking
    at the trail does NOT reinforce the rows (otherwise the audit endpoint
    would defeat decay).

    If ``principals`` is given, only rows whose ``allowed_principals``
    overlap are returned -- same ACL pre-filter as retrieval. ``None``
    returns all rows (backward-compatible default for trusted callers)."""
    eff = effective_confidence_sql("memory")
    sql = f"""
        SELECT id, content, source_type, source_id, confidence,
               superseded_by, created_at, tier, last_referenced_at,
               ({eff}) AS effective_confidence
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
