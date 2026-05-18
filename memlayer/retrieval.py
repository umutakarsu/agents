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
"""

from dataclasses import dataclass

from memlayer.db import connect
from memlayer.embeddings import embed

RRF_K = 60  # standard RRF dampening constant


@dataclass
class Hit:
    id: str  # content_hash for chunks, "mem:<id>" for memory
    kind: str  # 'chunk' | 'memory'
    text: str
    score: float
    arms: tuple[str, ...]  # which arms surfaced it (explainability)
    provenance: str | None = None  # memory only: 'source_type:source_id'
    confidence: float | None = None  # memory only


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


def _memory_arm(cur, query, ws, principals, k) -> list[str]:
    # Only current memory (superseded_by IS NULL), ACL pre-filtered the same
    # way as chunks so distilled knowledge gets the identical trust guarantee.
    cur.execute(
        """
        SELECT 'mem:' || id
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
            confidence DESC
        LIMIT %(k)s
        """,
        {"ws": ws, "principals": principals, "query": query, "k": k},
    )
    return [r[0] for r in cur.fetchall()]


def rrf_fuse(
    rankings: dict[str, list[str]], k: int = RRF_K
) -> list[tuple[str, float, tuple[str, ...]]]:
    """Pure, offline-testable. rankings: arm_name -> ordered ids (best first).
    Returns (id, score, arms) sorted best first."""
    scores: dict[str, float] = {}
    arms: dict[str, list[str]] = {}
    for arm, ids in rankings.items():
        for rank, i in enumerate(ids):
            scores[i] = scores.get(i, 0.0) + 1.0 / (k + rank + 1)
            arms.setdefault(i, []).append(arm)
    fused = [(i, scores[i], tuple(arms[i])) for i in scores]
    fused.sort(key=lambda t: t[1], reverse=True)
    return fused


def _hydrate(cur, fused) -> dict[str, Hit]:
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
            )
    return out


def search(
    query: str, workspace: str, principals: list[str], k: int = 10
) -> list[Hit]:
    qvec = embed(query)
    arm_k = max(k * 2, 20)  # over-fetch so fusion has signal beyond the cut
    with connect() as conn, conn.cursor() as cur:
        rankings = {
            "vector": _vector_arm(cur, qvec, workspace, principals, arm_k),
            "fts": _fts_arm(cur, query, workspace, principals, arm_k),
            "memory": _memory_arm(cur, query, workspace, principals, arm_k),
        }
        fused = rrf_fuse(rankings)[:k]
        if not fused:
            return []
        hits = _hydrate(cur, fused)

    result: list[Hit] = []
    for i, score, arms in fused:
        h = hits.get(i)
        if h is None:  # row vanished between arm and hydrate; skip safely
            continue
        h.score, h.arms = score, arms
        result.append(h)
    return result
