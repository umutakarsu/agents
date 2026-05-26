"""Hybrid retrieval over three arms:
  - vector  : pgvector cosine over raw chunks
  - fts     : Postgres full-text over raw chunks
  - memory  : Postgres full-text over distilled, conflict-resolved memory

Raw chunks are evidence; memory rows are the distilled knowledge an agent or
human wrote back (with provenance + confidence). Blending both means a query
gets the durable conclusion AND the source material behind it.

Correctness invariant: the ACL filter is a PRE-filter applied inside EVERY
arm, in SQL, before ranking. Post-filtering in app code would (a) leak the
existence of forbidden rows via result-count gaps and (b) silently shrink k.
Chunks pre-filter on chunk_acl; memory pre-filters on memory.allowed_principals.

Phase 6 (tiered memory + Ebbinghaus decay): the memory arm carries decay.
The arm sorts internally by ``ts_rank DESC, effective_confidence DESC``, and
each memory hit's RRF contribution is multiplied by its effective_confidence
so a faded row gets ranked lower than a freshly-reinforced one even when
both share the same text relevance. Reading reinforces: every memory id we
surface gets ``last_referenced_at = now()`` bumped in a single bulk UPDATE.
"""

from dataclasses import dataclass

from memlayer import federated
from memlayer.db import connect
from memlayer.decay import effective_confidence_sql
from memlayer.embeddings import embed

RRF_K = 60  # standard RRF dampening constant

# Cap on how many federated synonym terms we OR into the lexical query. More
# than a handful and the expansion starts pulling in loosely-related noise
# that drowns the original intent, so we keep it conservative.
_MAX_EXPANSIONS = 3


@dataclass
class Hit:
    id: str  # content_hash for chunks, "mem:<id>" for memory
    kind: str  # 'chunk' | 'memory'
    text: str
    score: float
    arms: tuple[str, ...]  # which arms surfaced it (explainability)
    provenance: str | None = None  # memory only: 'source_type:source_id'
    confidence: float | None = None  # memory only
    effective_confidence: float | None = None  # memory only; post-decay


# Shared ACL-scoped base for the chunk arms. Neither chunk arm can ever
# rank a chunk the caller is not allowed to see.
_VISIBLE_CHUNKS = """
WITH visible AS (
    SELECT c.content_hash, c.text
    FROM chunks c
    JOIN chunk_acl a ON a.content_hash = c.content_hash
    WHERE a.workspace = %(ws)s
      AND a.allowed_principals && %(principals)s::text[]
)
"""


def _vector_arm(cur, qvec, ws, principals, k) -> list[str]:
    cur.execute(
        _VISIBLE_CHUNKS
        + """
        SELECT v.content_hash
        FROM visible v
        JOIN embeddings e ON e.content_hash = v.content_hash
        ORDER BY e.embedding <=> %(q)s::vector
        LIMIT %(k)s
        """,
        {"ws": ws, "principals": principals, "q": qvec, "k": k},
    )
    return [r[0] for r in cur.fetchall()]


def _fts_arm(cur, query, ws, principals, k) -> list[str]:
    cur.execute(
        _VISIBLE_CHUNKS
        + """
        SELECT v.content_hash
        FROM visible v
        WHERE to_tsvector('english', v.text)
              @@ plainto_tsquery('english', %(query)s)
        ORDER BY ts_rank(
            to_tsvector('english', v.text),
            plainto_tsquery('english', %(query)s)
        ) DESC
        LIMIT %(k)s
        """,
        {"ws": ws, "principals": principals, "query": query, "k": k},
    )
    return [r[0] for r in cur.fetchall()]


def _memory_arm(
    cur, query, ws, principals, k
) -> tuple[list[str], dict[str, float]]:
    """Return (ordered ids, id -> effective_confidence).

    Only current memory (superseded_by IS NULL), ACL pre-filtered the same
    way as chunks so distilled knowledge gets the identical trust guarantee.
    Ordering uses raw text rank first (don't bury a perfect match because
    it's a little stale) then effective_confidence as the decay-aware
    tiebreaker. The effective_confidence map is returned so the fuser can
    weight each hit's RRF contribution."""
    eff = effective_confidence_sql("memory")
    cur.execute(
        f"""
        SELECT 'mem:' || id, ({eff}) AS effective_confidence
        FROM memory
        WHERE workspace = %(ws)s
          AND superseded_by IS NULL
          AND allowed_principals && %(principals)s::text[]
          AND to_tsvector('english', content)
              @@ plainto_tsquery('english', %(query)s)
        ORDER BY ts_rank(
            to_tsvector('english', content),
            plainto_tsquery('english', %(query)s)
        ) DESC,
            effective_confidence DESC
        LIMIT %(k)s
        """,
        {"ws": ws, "principals": principals, "query": query, "k": k},
    )
    rows = cur.fetchall()
    ids = [r[0] for r in rows]
    eff_map = {r[0]: float(r[1]) for r in rows}
    return ids, eff_map


def rrf_fuse(
    rankings: dict[str, list[str]],
    k: int = RRF_K,
    weights: dict[str, float] | None = None,
) -> list[tuple[str, float, tuple[str, ...]]]:
    """Pure, offline-testable. rankings: arm_name -> ordered ids (best first).

    ``weights`` is an optional per-id post-RRF multiplier: a hit's
    contribution to the final score is multiplied by ``weights[id]`` if
    present (default 1.0). This is how the memory arm injects decay --
    text relevance still drives the in-arm rank, but a faded memory row
    can't ride on the back of a strong ts_rank.

    Returns (id, score, arms) sorted best first."""
    weights = weights or {}
    scores: dict[str, float] = {}
    arms: dict[str, list[str]] = {}
    for arm, ids in rankings.items():
        for rank, i in enumerate(ids):
            contribution = (1.0 / (k + rank + 1)) * weights.get(i, 1.0)
            scores[i] = scores.get(i, 0.0) + contribution
            arms.setdefault(i, []).append(arm)
    fused = [(i, scores[i], tuple(arms[i])) for i in scores]
    fused.sort(key=lambda t: t[1], reverse=True)
    return fused


def _hydrate(cur, fused, eff_map: dict[str, float]) -> dict[str, Hit]:
    chunk_hashes = [i for i, _, _ in fused if not i.startswith("mem:")]
    mem_ids = [int(i[4:]) for i, _, _ in fused if i.startswith("mem:")]
    out: dict[str, Hit] = {}

    if chunk_hashes:
        cur.execute(
            "SELECT content_hash, text FROM chunks WHERE content_hash = ANY(%s)",
            (chunk_hashes,),
        )
        for h, text in cur.fetchall():
            out[h] = Hit(id=h, kind="chunk", text=text, score=0.0, arms=())

    if mem_ids:
        cur.execute(
            """
            SELECT id, content, source_type, source_id, confidence
            FROM memory WHERE id = ANY(%s)
            """,
            (mem_ids,),
        )
        for mid, content, stype, sid, conf in cur.fetchall():
            key = f"mem:{mid}"
            out[key] = Hit(
                id=key,
                kind="memory",
                text=content,
                score=0.0,
                arms=(),
                provenance=f"{stype}:{sid}",
                confidence=conf,
                effective_confidence=eff_map.get(key),
            )
    return out


def _reinforce_memory(cur, mem_ids: list[int]) -> None:
    """Bump ``last_referenced_at`` for every memory row surfaced. A single
    UPDATE keeps reinforcement O(1) round-trips no matter how many hits."""
    if not mem_ids:
        return
    cur.execute(
        "UPDATE memory SET last_referenced_at = now() WHERE id = ANY(%s)",
        (mem_ids,),
    )


def search(
    query: str, workspace: str, principals: list[str], k: int = 10
) -> list[Hit]:
    """Backward-compatible entry point: returns only the Hits.

    Kept stable for the CLI (scripts/search.py) and any other caller that
    only wants results. Internally delegates to ``search_with_expansions``
    and discards the expansion list."""
    hits, _expansions = search_with_expansions(query, workspace, principals, k=k)
    return hits


def search_with_expansions(
    query: str, workspace: str, principals: list[str], k: int = 10
) -> tuple[list[Hit], list[str]]:
    """Hybrid retrieval plus the federated query-expansion terms used.

    The lexical arms (fts, and the memory arm) are widened with cross-tenant
    synonym concepts so a query for "pull request feedback" also matches
    documents phrased as "code review". The expansion is purely lexical, so
    the vector arm stays on the *original* query (its embedding already
    captures semantic neighbours; re-embedding a term-soup would only blur
    intent).

    Safety: expansions are capped at ``_MAX_EXPANSIONS`` terms, and when
    ``expand_query`` returns nothing this behaves byte-for-byte like the old
    single-query path. Returns ``(hits, expansions)``."""
    qvec = embed(query)

    # Federated lexical expansion. Reads only cross-tenant aggregate tables
    # (concept / concept_synonym) -- workspace-agnostic and privacy-safe.
    expansions = federated.expand_query(query)[:_MAX_EXPANSIONS]
    # Widen the lexical query only when we actually have expansions; otherwise
    # the expanded string IS the original query and behaviour is unchanged.
    lexical_query = query
    if expansions:
        lexical_query = query + " " + " ".join(expansions)

    arm_k = max(k * 2, 20)  # over-fetch so fusion has signal beyond the cut
    with connect() as conn, conn.cursor() as cur:
        vec_ids = _vector_arm(cur, qvec, workspace, principals, arm_k)
        # Lexical arms run on the expanded query so synonyms widen recall.
        fts_ids = _fts_arm(cur, lexical_query, workspace, principals, arm_k)
        mem_ids_ranked, eff_map = _memory_arm(
            cur, lexical_query, workspace, principals, arm_k
        )
        rankings = {
            "vector": vec_ids,
            "fts": fts_ids,
            "memory": mem_ids_ranked,
        }
        # Post-RRF weighting: each memory id's RRF contribution is scaled
        # by its effective_confidence so a decayed row fades from the
        # fused ranking even if it scored high on ts_rank.
        fused = rrf_fuse(rankings, weights=eff_map)[:k]
        if not fused:
            return [], expansions
        hits = _hydrate(cur, fused, eff_map)
        # Reinforce every memory row that actually surfaced in the result.
        # Single bulk UPDATE -- one round-trip regardless of result count.
        surfaced_mem_ids = [
            int(i[4:]) for i, _, _ in fused if i.startswith("mem:")
        ]
        _reinforce_memory(cur, surfaced_mem_ids)
        conn.commit()

    result: list[Hit] = []
    for i, score, arms in fused:
        h = hits.get(i)
        if h is None:  # row vanished between arm and hydrate; skip safely
            continue
        h.score, h.arms = score, arms
        result.append(h)
    return result, expansions
