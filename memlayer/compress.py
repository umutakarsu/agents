"""Phase 8: semantic compression with auditable lineage.

Take N raw ``working``-tier rows about the same entity and distill them into
one durable ``episodic`` row -- but do it *extractively* so every claim in
the summary is verbatim from a source row. The lineage is by construction:
each sentence in the summary maps to the exact memory row it came from.

Why extractive (not LLM-generative):
  * No hallucination is *possible*. The summary text is a subset of the
    input sentences, so "did the system invent this?" has a one-line proof.
  * No API key required; runs in the same offline/deterministic mode as
    embeddings.
  * Lineage falls out for free -- we don't have to align generated tokens
    back to source spans, we just remember which sentence we picked.

Scoring is word-overlap centrality (a poor person's TextRank): for each
sentence, sum across all other sentences the count of shared content words,
normalized by length. The most "central" sentences are most representative
of the group. Top-K are picked (K = min(5, ceil(0.25*N_sentences))) and
emitted in original order so the summary reads naturally.

Source rows aren't deleted -- they stay flagged ``compressed_into=<id>`` so
the existing eviction job can purge them later while the audit trail is
intact. If a source row gets superseded/updated, the dependent summaries
are flagged ``needs_recompression=true`` for a future re-run.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from memlayer.db import connect

# Stopwords -- inline because we're committing to "no external deps for the
# offline path". Just the most common English fillers that would otherwise
# dominate the overlap score without conveying meaning.
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "am", "i", "me", "my", "we", "our", "you", "your", "he", "she", "it",
    "its", "they", "them", "their", "this", "that", "these", "those",
    "and", "or", "but", "if", "then", "else", "so", "because", "as", "at",
    "by", "for", "from", "in", "into", "of", "on", "off", "to", "with",
    "without", "about", "over", "under", "up", "down", "out", "over",
    "do", "does", "did", "doing", "done", "have", "has", "had", "having",
    "will", "would", "could", "should", "can", "may", "might", "must",
    "not", "no", "nor", "than", "too", "very", "just", "also", "only",
    "any", "some", "all", "each", "every", "more", "most", "much", "many",
    "few", "such", "own", "same", "other", "another", "while",
})

_WORD_RE = re.compile(r"[a-z0-9]+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass
class CompressionResult:
    summary_id: int | None
    source_row_ids: list[int]
    num_sentences: int
    total_chars_in: int
    total_chars_out: int
    compression_ratio: float
    skipped_reason: str | None = None


@dataclass
class LineageRow:
    sentence_index: int
    sentence: str
    source_row_id: int


def _split_sentences(text: str) -> list[str]:
    """Trim into sentences. Empty fragments are dropped. We don't try to be
    clever about quotes/abbreviations -- memlayer content is short and
    structured enough that the naive regex is good enough."""
    parts = _SENTENCE_SPLIT_RE.split(text.strip())
    return [p.strip() for p in parts if p.strip()]


def _tokenize(sentence: str) -> set[str]:
    """Lowercase, strip punctuation, drop stopwords. Returns the set of
    content words used for centrality scoring."""
    return {
        w for w in _WORD_RE.findall(sentence.lower())
        if w not in _STOPWORDS and len(w) > 1
    }


def _centrality_score(idx: int, token_sets: list[set[str]]) -> float:
    """Sum over other sentences of (shared words / max(|a|, |b|))."""
    a = token_sets[idx]
    if not a:
        return 0.0
    score = 0.0
    for j, b in enumerate(token_sets):
        if j == idx or not b:
            continue
        denom = max(len(a), len(b))
        if denom:
            score += len(a & b) / denom
    return score


def _pick_top_sentences(
    sentences: list[tuple[int, str, int]],
) -> list[tuple[int, str, int]]:
    """Score by word-overlap centrality, pick top-K, return them in
    original order so the summary reads naturally.

    Items are ``(global_index, sentence_text, source_row_id)``."""
    if not sentences:
        return []
    token_sets = [_tokenize(s[1]) for s in sentences]
    scored = [
        (i, _centrality_score(i, token_sets))
        for i in range(len(sentences))
    ]
    # K = min(5, ceil(0.25 * total))
    k = min(5, max(1, math.ceil(0.25 * len(sentences))))
    # Highest score wins; ties broken by stable original order.
    top_idx = sorted(
        sorted(scored, key=lambda x: x[0]),  # stable by orig order
        key=lambda x: x[1],
        reverse=True,
    )[:k]
    picked = sorted(i for i, _ in top_idx)
    return [sentences[i] for i in picked]


def _fetch_working_rows(
    cur, workspace: str, entity_key: str
) -> list[tuple[int, str, float, list[str]]]:
    """Working rows for this entity not yet compressed. We deliberately
    exclude already-compressed rows (compressed_into IS NOT NULL) so a
    re-run is a no-op."""
    cur.execute(
        """
        SELECT id, content, confidence, allowed_principals
        FROM memory
        WHERE workspace = %s
          AND entity_key = %s
          AND tier = 'working'
          AND compressed_into IS NULL
        ORDER BY id
        """,
        (workspace, entity_key),
    )
    return list(cur.fetchall())


def _intersect_acls(acls: list[list[str]]) -> list[str]:
    """Take the intersection of all source ACL sets. Never *widen* the
    permission set during compression -- a summary derived from restricted
    rows must itself be at least as restricted as the most-restricted
    source. If the intersection is empty, fall back to the most-restricted
    individual set (rather than emitting an empty allowed_principals which
    would make the row unreadable to anyone)."""
    if not acls:
        return ["group:all"]
    sets = [set(a) for a in acls]
    inter = set.intersection(*sets) if sets else set()
    if inter:
        return sorted(inter)
    # No common principal across sources -- pick the smallest set so we
    # err on the side of restrictive.
    smallest = min(sets, key=len)
    return sorted(smallest)


def compress(
    workspace: str,
    entity_key: str,
    *,
    dry_run: bool = False,
    min_rows: int = 3,
) -> CompressionResult:
    """Compress all un-compressed working rows for one (workspace,entity).

    No-ops (returns a result with summary_id=None and skipped_reason set):
      * Fewer than ``min_rows`` working rows queued up.
      * No extractable sentences at all (e.g. all rows are empty).

    Otherwise writes a new ``episodic`` row whose ``content`` is the
    extracted summary, populates ``summary_lineage``, and marks each source
    row's ``compressed_into``."""
    with connect() as conn, conn.cursor() as cur:
        rows = _fetch_working_rows(cur, workspace, entity_key)
        if len(rows) < min_rows:
            return CompressionResult(
                summary_id=None,
                source_row_ids=[r[0] for r in rows],
                num_sentences=0,
                total_chars_in=sum(len(r[1]) for r in rows),
                total_chars_out=0,
                compression_ratio=1.0,
                skipped_reason=(
                    f"only {len(rows)} working row(s) < min_rows={min_rows}"
                ),
            )

        # Flatten into (global_idx, sentence, source_row_id).
        all_sentences: list[tuple[int, str, int]] = []
        for row_id, content, _conf, _acl in rows:
            for s in _split_sentences(content):
                all_sentences.append((len(all_sentences), s, row_id))

        if not all_sentences:
            return CompressionResult(
                summary_id=None,
                source_row_ids=[r[0] for r in rows],
                num_sentences=0,
                total_chars_in=sum(len(r[1]) for r in rows),
                total_chars_out=0,
                compression_ratio=1.0,
                skipped_reason="no extractable sentences",
            )

        picked = _pick_top_sentences(all_sentences)
        summary_text = " ".join(s for _, s, _ in picked)
        total_in = sum(len(r[1]) for r in rows)
        total_out = len(summary_text)
        ratio = (total_out / total_in) if total_in else 1.0

        if dry_run:
            return CompressionResult(
                summary_id=None,
                source_row_ids=[r[0] for r in rows],
                num_sentences=len(picked),
                total_chars_in=total_in,
                total_chars_out=total_out,
                compression_ratio=ratio,
                skipped_reason="dry_run",
            )

        # Aggregate provenance.
        source_ids = [r[0] for r in rows]
        confidences = [float(r[2]) for r in rows]
        avg_conf = sum(confidences) / len(confidences)
        # Clamp to [0, 1] -- defensive against any weird stored values.
        avg_conf = max(0.0, min(1.0, avg_conf))
        acls_in = [list(r[3]) if r[3] else ["group:all"] for r in rows]
        merged_acl = _intersect_acls(acls_in)

        # Write the episodic summary directly; we deliberately don't go
        # through remember() because the summary should not enter the
        # supersession ladder for entity_key -- it's a *new* kind of row,
        # an extracted summary, not a competing fact.
        cur.execute(
            """
            INSERT INTO memory
                (workspace, entity_key, content, source_type, source_id,
                 derived_from, confidence, allowed_principals, tier)
            VALUES (%s, %s, %s, 'system', 'compress',
                    %s, %s, %s, 'episodic')
            RETURNING id
            """,
            (
                workspace,
                entity_key,
                summary_text,
                source_ids,
                avg_conf,
                merged_acl,
            ),
        )
        summary_id = cur.fetchone()[0]

        # Per-sentence lineage. sentence_index is the position in the final
        # summary (0..K-1), not the global pre-pick index.
        for new_idx, (_global_idx, sentence, source_row_id) in enumerate(picked):
            cur.execute(
                """
                INSERT INTO summary_lineage
                    (summary_id, sentence_index, sentence, source_row_id)
                VALUES (%s, %s, %s, %s)
                """,
                (summary_id, new_idx, sentence, source_row_id),
            )

        # Flag source rows. They stay in the DB for audit but evict_stale
        # can now purge them once they're cold enough.
        cur.execute(
            "UPDATE memory SET compressed_into = %s "
            "WHERE id = ANY(%s)",
            (summary_id, source_ids),
        )
        conn.commit()

    return CompressionResult(
        summary_id=summary_id,
        source_row_ids=source_ids,
        num_sentences=len(picked),
        total_chars_in=total_in,
        total_chars_out=total_out,
        compression_ratio=ratio,
    )


def compress_all(
    workspace: str,
    *,
    min_rows: int = 3,
) -> list[CompressionResult]:
    """Find every (entity_key) in ``workspace`` with >=``min_rows``
    un-compressed working rows and compress each one. Returns one
    CompressionResult per entity touched (including skipped ones)."""
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
    return [
        compress(workspace, ent, min_rows=min_rows) for ent in entities
    ]


def lineage(summary_id: int) -> list[LineageRow]:
    """The per-sentence lineage for one episodic summary, ordered by the
    sentence's position in the summary text."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT sentence_index, sentence, source_row_id
            FROM summary_lineage
            WHERE summary_id = %s
            ORDER BY sentence_index
            """,
            (summary_id,),
        )
        return [
            LineageRow(
                sentence_index=row[0],
                sentence=row[1],
                source_row_id=row[2],
            )
            for row in cur.fetchall()
        ]


def invalidate_summaries_for(source_row_id: int) -> list[int]:
    """Mark every summary that cited ``source_row_id`` as needing
    recompression. Memory is append-only, but a row *can* be superseded
    via the conflict-resolution ladder -- when that happens, any summary
    that quoted the now-superseded sentence is stale and should be
    regenerated next time compress() runs.

    Returns the list of summary IDs that were flagged."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE memory
            SET needs_recompression = true
            WHERE id IN (
                SELECT DISTINCT summary_id FROM summary_lineage
                WHERE source_row_id = %s
            )
            RETURNING id
            """,
            (source_row_id,),
        )
        flagged = [row[0] for row in cur.fetchall()]
        conn.commit()
    return flagged
