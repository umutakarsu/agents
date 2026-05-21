"""Tier-aware Ebbinghaus decay for memory rows.

Effective confidence is a *computed* value: the stored ``confidence`` column
is the value at last reinforcement, and ``last_referenced_at`` is the clock
that decay runs against. Reading a row reinforces it (writeback/retrieval
bump ``last_referenced_at``), so frequently-used facts stay sharp while
stale guesses fade.

Tiers (no auto-promotion yet -- promotion is future work):

==========  ==========  =====  ====================================
tier        half-life   floor  meaning
==========  ==========  =====  ====================================
working     6 hours     0.10   raw, ephemeral (tool obs, scratch)
episodic    14 days     0.30   compressed session summary
semantic    180 days    0.60   durable extracted fact
procedural  365 days    0.70   how-to / workflow
==========  ==========  =====  ====================================

Formula: ``effective = max(floor, confidence * 0.5 ** (age / half_life))``.

The Python implementation is the source of truth (used by writeback +
retrieval) and we mirror the formula in SQL where ranking happens, so the
database can sort by effective confidence without round-tripping every row
through Python.
"""

from __future__ import annotations

from memlayer.db import connect

# (half_life_seconds, floor). Order = tier name -> tuning.
TIERS: dict[str, tuple[float, float]] = {
    "working":     (6 * 3600,           0.10),
    "episodic":    (14 * 24 * 3600,     0.30),
    "semantic":    (180 * 24 * 3600,    0.60),
    "procedural":  (365 * 24 * 3600,    0.70),
}
VALID_TIERS = frozenset(TIERS.keys())

# Eviction thresholds: (max_age_seconds, effective_confidence_cutoff).
# Only low-tier rows get evicted; semantic + procedural are never auto-purged
# (those represent durable knowledge, only a human should remove them).
_EVICTION_RULES: dict[str, tuple[float, float]] = {
    "working":  (24 * 3600,         0.15),
    "episodic": (30 * 24 * 3600,    0.35),
}


def decay(confidence: float, tier: str, age_seconds: float) -> float:
    """Effective confidence after ``age_seconds`` for a row in ``tier``.

    Floors prevent a long-lived semantic/procedural fact from decaying to
    zero just because nobody read it for a year -- durable knowledge keeps a
    minimum signal even when cold."""
    if tier not in TIERS:
        raise ValueError(
            f"tier must be one of {sorted(VALID_TIERS)}, got {tier!r}"
        )
    half_life, floor = TIERS[tier]
    raw = confidence * (0.5 ** (max(age_seconds, 0.0) / half_life))
    return max(floor, raw)


# SQL fragment that mirrors ``decay()`` for in-database ranking. Plug it
# into a SELECT to compute effective_confidence without pulling rows into
# Python. ``memory_alias`` is the table alias used for the memory row.
def effective_confidence_sql(memory_alias: str = "memory") -> str:
    cases = []
    for tier, (half_life, floor) in TIERS.items():
        cases.append(
            f"WHEN {memory_alias}.tier = '{tier}' THEN GREATEST("
            f"{floor}::real, "
            f"{memory_alias}.confidence * "
            f"power(0.5, EXTRACT(EPOCH FROM (now() - "
            f"{memory_alias}.last_referenced_at)) / {half_life})"
            f")"
        )
    return "CASE " + " ".join(cases) + " END"


def evict_stale(dry_run: bool = False) -> dict[str, int]:
    """Delete stale low-tier rows; return per-tier counts.

    A row is stale when both (a) it has been untouched longer than the
    tier's max age AND (b) its decayed effective_confidence has fallen
    below the tier's cutoff. Semantic and procedural rows are never
    auto-evicted -- those represent durable knowledge that only a human
    should remove."""
    counts: dict[str, int] = {tier: 0 for tier in _EVICTION_RULES}
    eff_sql = effective_confidence_sql("memory")

    with connect() as conn, conn.cursor() as cur:
        for tier, (max_age, cutoff) in _EVICTION_RULES.items():
            # Count first (also the dry-run path), then delete in the
            # same transaction so the counts can't drift from the delete.
            cur.execute(
                f"""
                SELECT count(*) FROM memory
                WHERE tier = %s
                  AND now() - last_referenced_at > make_interval(secs => %s)
                  AND ({eff_sql}) < %s
                """,
                (tier, max_age, cutoff),
            )
            counts[tier] = cur.fetchone()[0]

            if not dry_run and counts[tier] > 0:
                cur.execute(
                    f"""
                    DELETE FROM memory
                    WHERE tier = %s
                      AND now() - last_referenced_at > make_interval(secs => %s)
                      AND ({eff_sql}) < %s
                    """,
                    (tier, max_age, cutoff),
                )
        if not dry_run:
            conn.commit()
    return counts
