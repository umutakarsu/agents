"""Phase 11: automatic upward tier promotion.

The tier model has always had a downward path (``decay.evict_stale`` deletes
cold low-tier rows) but tier was set once at write time and never moved up.
This module closes the loop: raw memories that prove durable get promoted to
higher tiers automatically.

Two upward transitions (downward = eviction, already built in ``decay.py``):

1. **working -> episodic (via compression).** When an entity accumulates
   >= ``min_rows`` un-compressed ``working`` rows, ``compress.compress()``
   folds them into one ``episodic`` summary with verbatim lineage. We only
   orchestrate: find the eligible entities and call it. ``compress`` does the
   actual work (and flags the sources ``compressed_into``).

2. **episodic -> semantic (via reinforcement).** An ``episodic`` summary that
   has survived past a promotion-age threshold while staying above an
   effective-confidence cutoff -- AND that has been *referenced* enough times
   (``reference_count``) -- has proven it doesn't just decay away. We promote
   it to ``semantic`` (longer half-life, higher floor: it stops fading).
   semantic -> procedural is deliberately NOT automatic; procedural is
   hand-authored "how-to" knowledge, so it stays a manual step.

The reinforcement signal needs ``reference_count`` to actually move. The
column defaults to 0 and is incremented each time a row is reinforced. The
reinforcement sites (``writeback.recall`` / ``retrieval._reinforce_memory``)
are owned by other work this round, so wiring the ``reference_count + 1`` bump
into them is a pending one-line follow-up. Until then the count stays 0 and
nothing is promoted on the reinforcement path -- which is safe: a row that was
never used should not be promoted. (For tests, set ``reference_count`` directly
via SQL to simulate usage.)
"""

from __future__ import annotations

from memlayer.compress import compress
from memlayer.db import connect
from memlayer.decay import effective_confidence_sql


def promote_working_to_episodic(workspace: str, *, min_rows: int = 3) -> dict:
    """Promote every eligible entity's working rows to one episodic summary.

    An entity is eligible when it has >= ``min_rows`` un-compressed working
    rows (``tier='working' AND compressed_into IS NULL``). For each we call
    ``compress.compress()``, which writes the episodic summary and flags the
    sources ``compressed_into``.

    Returns ``{entities_compressed, summaries_created}`` where
    ``summaries_created`` is the list of new episodic summary ids."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT entity_key, count(*) AS n
            FROM memory
            WHERE workspace = %s
              AND tier = 'working'
              AND compressed_into IS NULL
            GROUP BY entity_key
            HAVING count(*) >= %s
            ORDER BY entity_key
            """,
            (workspace, min_rows),
        )
        entities = [row[0] for row in cur.fetchall()]

    summaries: list[int] = []
    for entity_key in entities:
        result = compress(workspace, entity_key, min_rows=min_rows)
        if result.summary_id is not None:
            summaries.append(result.summary_id)

    return {
        "entities_compressed": len(summaries),
        "summaries_created": summaries,
    }


def promote_episodic_to_semantic(
    workspace: str,
    *,
    min_age_days: int = 7,
    min_eff: float = 0.5,
    min_refs: int = 3,
) -> dict:
    """Promote durable episodic summaries to the semantic tier.

    A row qualifies when ALL hold:
      * ``tier = 'episodic'`` in ``workspace``,
      * it is at least ``min_age_days`` old (created_at), so it has had a
        chance to fade -- proving it did not just decay away,
      * its effective_confidence (per ``decay.effective_confidence_sql``) is
        still >= ``min_eff``,
      * ``reference_count >= min_refs`` -- it has actually been used.

    Promotion is ``UPDATE memory SET tier='semantic'``. Returns
    ``{promoted, ids}``."""
    eff_sql = effective_confidence_sql("memory")
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE memory
            SET tier = 'semantic'
            WHERE workspace = %s
              AND tier = 'episodic'
              AND now() - created_at >= make_interval(days => %s)
              AND ({eff_sql}) >= %s
              AND reference_count >= %s
            RETURNING id
            """,
            (workspace, min_age_days, min_eff, min_refs),
        )
        ids = [row[0] for row in cur.fetchall()]
        conn.commit()

    return {"promoted": len(ids), "ids": ids}


def promote_all(workspace: str) -> dict:
    """Run both upward transitions and return a combined summary.

    working -> episodic runs first so a freshly-created episodic summary is
    *not* eligible for semantic promotion in the same pass (it can't meet the
    age + use thresholds yet) -- promotion is a multi-pass, time-gated process
    by design."""
    w2e = promote_working_to_episodic(workspace)
    e2s = promote_episodic_to_semantic(workspace)
    return {
        "working_to_episodic": w2e,
        "episodic_to_semantic": e2s,
    }
