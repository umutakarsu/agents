"""Hybrid retrieval: vector (pgvector cosine) + full-text (Postgres FTS),
fused with Reciprocal Rank Fusion.

Correctness invariant: the ACL filter is a PRE-filter applied inside BOTH
arms, in SQL, before ranking. Post-filtering in app code would (a) leak the
existence of forbidden chunks via result-count gaps and (b) silently shrink k.
The requesting principal set must overlap chunk_acl.allowed_principals."""

from dataclasses import dataclass

from memlayer.db import connect
from memlayer.embeddings import embed

RRF_K = 60  # standard RRF dampening constant


@dataclass
class Hit:
    content_hash: str
    text: str
    score: float
    arms: tuple[str, ...]  # which arms surfaced it (for explainability)


# Shared ACL-scoped base. Both arms read from this, so neither can ever
# rank a chunk the caller is not allowed to see.
_VISIBLE = """
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
        _VISIBLE
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
        _VISIBLE
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


def rrf_fuse(rankings: dict[str, list[str]], k: int = RRF_K) -> list[tuple[str, float, tuple[str, ...]]]:
    """Pure, offline-testable. rankings: arm_name -> ordered content_hashes
    (best first). Returns (content_hash, score, arms) sorted best first."""
    scores: dict[str, float] = {}
    arms: dict[str, list[str]] = {}
    for arm, hashes in rankings.items():
        for rank, h in enumerate(hashes):
            scores[h] = scores.get(h, 0.0) + 1.0 / (k + rank + 1)
            arms.setdefault(h, []).append(arm)
    fused = [(h, scores[h], tuple(arms[h])) for h in scores]
    fused.sort(key=lambda t: t[1], reverse=True)
    return fused


def search(query: str, workspace: str, principals: list[str], k: int = 10) -> list[Hit]:
    qvec = embed(query)
    # Over-fetch per arm so fusion has signal beyond the final cut.
    arm_k = max(k * 2, 20)
    with connect() as conn, conn.cursor() as cur:
        vec_ids = _vector_arm(cur, qvec, workspace, principals, arm_k)
        fts_ids = _fts_arm(cur, query, workspace, principals, arm_k)

        fused = rrf_fuse({"vector": vec_ids, "fts": fts_ids})[:k]
        if not fused:
            return []

        order = {h: i for i, (h, _, _) in enumerate(fused)}
        cur.execute(
            "SELECT content_hash, text FROM chunks WHERE content_hash = ANY(%s)",
            ([h for h, _, _ in fused],),
        )
        texts = {r[0]: r[1] for r in cur.fetchall()}

    return sorted(
        (Hit(h, texts[h], s, a) for h, s, a in fused),
        key=lambda hit: order[hit.content_hash],
    )
