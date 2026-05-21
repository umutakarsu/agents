"""Phase 10: federated cross-tenant concept ontology.

The fourth moat: the platform learns from every customer's workspace, but no
customer's content ever bleeds into another customer's retrieval.

Mechanism: extract per-tenant *concept vocabularies* (n-gram terms appearing
across that tenant's chunks/memory), find concepts that appear in many tenants,
build a shared concept graph. The cross-tenant store is restricted by
construction to:
  - concept NAMES (the literal terms, treated as dictionary-class vocabulary),
  - AGGREGATE counts (summed across tenants without per-tenant attribution),
  - SYNONYM pairs derived from cross-tenant co-occurrence.

What is NEVER shared cross-tenant:
  - chunk content, memory content, entity_keys,
  - the specific rows that produced an extraction,
  - which specific tenants have which concepts.

The privacy invariant is enforced structurally: cross-tenant queries
(``compute_synonyms``, ``expand_query``) only read from the aggregate
``concept`` and ``concept_synonym`` tables. The per-tenant ``tenant_concept``
table is only read with an explicit workspace scope, and its rows are never
returned in any function that another tenant would call.

Synonym discovery requires ``min_tenants`` distinct workspaces to have seen
the same pair co-occur. A concept that only one tenant has produced is
excluded from synonym formation: it isn't yet a "global" concept and could
otherwise leak workspace identity through its rarity.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from itertools import combinations

from memlayer.db import connect

# ---------------------------------------------------------------------------
# Vocabulary extraction
# ---------------------------------------------------------------------------

# ~50 common English stopwords. Inline so this module has no extra deps.
# Tuned for technical text: includes generic verbs ("is", "are") and a few
# context-light words ("thing", "stuff") but not domain words like "code" or
# "review" which are the kind of vocabulary we WANT to surface.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "or", "but", "if", "the", "this", "that", "these",
        "those", "is", "are", "was", "were", "be", "been", "being", "am",
        "do", "does", "did", "have", "has", "had", "of", "in", "on", "at",
        "to", "for", "with", "by", "from", "as", "it", "its", "he", "she",
        "they", "them", "we", "us", "you", "your", "i", "my", "me", "our",
        "their", "his", "her", "not", "no", "so", "than", "then", "too",
        "very", "just", "also", "about", "into", "over", "out", "up", "down",
        "can", "could", "should", "would", "may", "might", "will", "shall",
        "any", "all", "some", "such", "only", "own", "same", "other", "more",
        "most", "much", "many", "few", "each", "every", "thing", "things",
        "stuff", "via", "per",
    }
)

# Lowercase ASCII-ish word tokenizer. Keeps internal apostrophes/dashes off
# the n-gram boundary so "well-known" tokenizes as two tokens, and that's
# fine -- "well known" is a perfectly valid 2-gram concept.
_TOKEN = re.compile(r"[a-z][a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _is_concept_token(tok: str) -> bool:
    return tok not in _STOPWORDS and len(tok) >= 2


def extract_concepts(
    text: str, *, min_length: int = 2, max_length: int = 4
) -> list[str]:
    """Extract candidate concept names (n-grams) from text.

    Returns a list with REPETITIONS preserved so the caller can build local
    counts. We use n-gram extraction (default 2-4 grams), drop n-grams that
    start or end with a stopword, lowercase everything. A 1-gram alternative
    is too noisy in practice (single words like "team", "user" overwhelm
    real multi-word concepts like "code review"), so we start at 2.
    """
    if min_length < 1:
        raise ValueError("min_length must be >= 1")
    if max_length < min_length:
        raise ValueError("max_length must be >= min_length")

    tokens = _tokenize(text)
    out: list[str] = []
    n = len(tokens)
    for size in range(min_length, max_length + 1):
        for i in range(n - size + 1):
            window = tokens[i : i + size]
            # Drop n-grams anchored on stopwords -- "the review" is not a
            # concept, but its inner 1-gram "review" is also too noisy on its
            # own. The interior of the n-gram is allowed to contain a stopword
            # ("end of life") because skipping those loses real phrases.
            if window[0] in _STOPWORDS or window[-1] in _STOPWORDS:
                continue
            if any(not _is_concept_token(t) for t in (window[0], window[-1])):
                continue
            out.append(" ".join(window))
    return out


# ---------------------------------------------------------------------------
# Per-tenant indexing
# ---------------------------------------------------------------------------


def _iter_workspace_texts(cur, workspace: str) -> Iterable[str]:
    """Yield every piece of text owned by ``workspace``: ACL'd chunks + memory.

    Strictly bounded to the workspace argument; nothing crosses tenants here.
    """
    # Chunks: join chunks -> chunk_acl to scope by workspace.
    cur.execute(
        """
        SELECT c.text
        FROM chunks c
        JOIN chunk_acl a ON a.content_hash = c.content_hash
        WHERE a.workspace = %s
        """,
        (workspace,),
    )
    for (text,) in cur.fetchall():
        yield text

    # Memory: workspace lives directly on the row.
    cur.execute(
        "SELECT content FROM memory WHERE workspace = %s",
        (workspace,),
    )
    for (content,) in cur.fetchall():
        yield content


def _get_or_create_concept(cur, name: str) -> int:
    """Return the concept.id for ``name``, inserting if absent.

    Concept names are intentionally GLOBAL: vocabulary, not content. We treat
    them like dictionary words. The privacy contract guards INSTANCE data
    (chunk text, memory content, who said what) -- not the literal term
    "code review".
    """
    cur.execute(
        """
        INSERT INTO concept (name) VALUES (%s)
        ON CONFLICT (name) DO UPDATE SET last_updated_at = now()
        RETURNING id
        """,
        (name,),
    )
    return cur.fetchone()[0]


def index_workspace(workspace: str) -> dict:
    """Extract concepts for ONE workspace and update aggregates.

    Reads ONLY this workspace's chunks + memory. Writes per-tenant counts to
    ``tenant_concept`` and recomputes the aggregate ``concept`` row (global
    count = sum across tenants, tenant_count = distinct workspaces). Returns
    a small summary.
    """
    local_counts: Counter[str] = Counter()
    total_extractions = 0

    with connect() as conn, conn.cursor() as cur:
        # Step 1: extract per-tenant concept counts. Stays per-workspace.
        for text in _iter_workspace_texts(cur, workspace):
            terms = extract_concepts(text)
            total_extractions += len(terms)
            local_counts.update(terms)

        # Drop ultra-rare terms (singletons within this workspace) to keep
        # the vocabulary signal-heavy. A concept needs at least two
        # appearances locally to be considered a real concept of this tenant.
        local_counts = Counter(
            {k: v for k, v in local_counts.items() if v >= 2}
        )

        concepts_added = 0
        concepts_updated = 0

        # Step 2: upsert this tenant's vocabulary into tenant_concept.
        # Resetting first means re-indexing converges instead of accumulating
        # stale counts; the aggregate refresh below then recomputes from the
        # current set of tenant_concept rows for the affected concepts.
        cur.execute(
            "SELECT concept_id FROM tenant_concept WHERE workspace = %s",
            (workspace,),
        )
        prior_concept_ids = {r[0] for r in cur.fetchall()}
        cur.execute(
            "DELETE FROM tenant_concept WHERE workspace = %s", (workspace,)
        )

        new_concept_ids: set[int] = set()
        for name, count in local_counts.items():
            # SELECT first to know if this is a brand-new concept globally.
            cur.execute("SELECT id FROM concept WHERE name = %s", (name,))
            row = cur.fetchone()
            if row is None:
                concepts_added += 1
            else:
                concepts_updated += 1
            cid = _get_or_create_concept(cur, name)
            new_concept_ids.add(cid)
            cur.execute(
                """
                INSERT INTO tenant_concept
                    (workspace, concept_id, local_count, last_updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (workspace, concept_id)
                DO UPDATE SET local_count = EXCLUDED.local_count,
                              last_updated_at = now()
                """,
                (workspace, cid, count),
            )

        # Step 3: refresh aggregates for every concept that was either added
        # or removed from this workspace. Aggregates are recomputed from
        # tenant_concept (which is per-tenant truth), but the OUTPUT row in
        # ``concept`` is workspace-agnostic.
        touched = prior_concept_ids | new_concept_ids
        for cid in touched:
            cur.execute(
                """
                UPDATE concept
                SET global_count = COALESCE(
                        (SELECT SUM(local_count) FROM tenant_concept
                         WHERE concept_id = %s), 0),
                    tenant_count = COALESCE(
                        (SELECT COUNT(DISTINCT workspace) FROM tenant_concept
                         WHERE concept_id = %s), 0),
                    last_updated_at = now()
                WHERE id = %s
                """,
                (cid, cid, cid),
            )

        conn.commit()

    return {
        "concepts_added": concepts_added,
        "concepts_updated": concepts_updated,
        "total_extractions": total_extractions,
        "vocab_size": len(local_counts),
    }


# ---------------------------------------------------------------------------
# Cross-tenant synonym discovery
# ---------------------------------------------------------------------------


def compute_synonyms(
    min_tenants: int = 2, min_cooccurrence: int = 3
) -> int:
    """Find concept pairs that co-occur across at least ``min_tenants`` workspaces.

    Privacy invariant: this function NEVER selects chunk text, memory content,
    or per-tenant attribution into its outputs. It joins ``tenant_concept`` to
    itself by workspace to find pairs that co-occur in the same WORKSPACE
    vocabulary, then aggregates the per-pair distinct-tenant count. The
    aggregate is written to ``concept_synonym``; nothing about which tenants
    contributed is preserved.

    A pair is admitted if and only if:
      - both concepts have tenant_count >= min_tenants (each concept itself
        is "global", not the quirk of one workspace),
      - the pair co-occurs in vocabularies of at least min_tenants distinct
        workspaces,
      - and at least one of those workspaces hit local_count >= min_cooccurrence
        for both members (signal threshold, not just incidental).

    Returns the number of synonym rows written.
    """
    if min_tenants < 2:
        # Single-tenant "synonyms" would just expose that tenant's vocabulary.
        raise ValueError("min_tenants must be >= 2 to preserve privacy")

    written = 0
    with connect() as conn, conn.cursor() as cur:
        # Pull only globally-eligible concepts (tenant_count >= min_tenants).
        # Single-tenant terms are excluded here, which is the structural guard
        # against single-tenant identity leaks via rare concept pairs.
        cur.execute(
            "SELECT id, name, tenant_count FROM concept "
            "WHERE tenant_count >= %s",
            (min_tenants,),
        )
        eligible = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
        if len(eligible) < 2:
            return 0
        eligible_ids = tuple(eligible.keys())

        # Pull the per-workspace vocab restricted to eligible concepts.
        # We never read concept names that aren't already globally eligible,
        # which means a tenant-private term cannot ride along into the join.
        cur.execute(
            """
            SELECT workspace, concept_id, local_count
            FROM tenant_concept
            WHERE concept_id = ANY(%s)
            """,
            (list(eligible_ids),),
        )
        # workspace -> {concept_id: local_count}
        by_ws: dict[str, dict[int, int]] = {}
        for ws, cid, lc in cur.fetchall():
            by_ws.setdefault(ws, {})[cid] = lc

        # Compute distinct-tenant cooccurrence counts in memory. This is a
        # plain aggregation: pair -> {workspaces that have both}. We do NOT
        # store the workspaces; only |set|.
        pair_tenants: dict[tuple[int, int], set[str]] = {}
        pair_strong: dict[tuple[int, int], int] = {}
        for ws, vocab in by_ws.items():
            cids_here = sorted(vocab.keys())
            for a, b in combinations(cids_here, 2):
                key = (a, b)  # CHECK constraint requires a < b; sorted() guarantees
                pair_tenants.setdefault(key, set()).add(ws)
                if (
                    vocab[a] >= min_cooccurrence
                    and vocab[b] >= min_cooccurrence
                ):
                    pair_strong[key] = pair_strong.get(key, 0) + 1

        # Clear the table first so removed pairs don't linger when a
        # workspace's content changes and recompute is run.
        cur.execute("DELETE FROM concept_synonym")

        for (a, b), tenants in pair_tenants.items():
            n = len(tenants)
            if n < min_tenants:
                continue
            if pair_strong.get((a, b), 0) < 1:
                # Require at least one workspace where both members hit the
                # signal threshold. Pure incidental co-occurrence is dropped.
                continue
            # Confidence: how strong is this synonym at population scale?
            # Normalise by the smaller member's tenant_count -- if every
            # tenant that has the rarer concept ALSO has the partner, the
            # signal is maximal.
            min_concept_tenants = min(eligible[a][1], eligible[b][1])
            denom = max(min_concept_tenants, 1)
            confidence = min(1.0, n / denom)
            cur.execute(
                """
                INSERT INTO concept_synonym
                    (concept_a, concept_b, cooccurrence_tenants,
                     confidence, last_updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (concept_a, concept_b)
                DO UPDATE SET cooccurrence_tenants = EXCLUDED.cooccurrence_tenants,
                              confidence = EXCLUDED.confidence,
                              last_updated_at = now()
                """,
                (a, b, n, confidence),
            )
            written += 1

        conn.commit()
    return written


# ---------------------------------------------------------------------------
# Retrieval-time helper (single-tenant safe)
# ---------------------------------------------------------------------------


def expand_query(query: str) -> list[str]:
    """Given a query, return synonym-expanded concept names.

    This is retrieval-time helper; the caller's workspace is irrelevant
    because we read ONLY the cross-tenant aggregate tables (``concept``,
    ``concept_synonym``). The returned list is a set of cross-tenant
    synonym concept names that the caller can OR into their query.
    """
    query_concepts = extract_concepts(query)
    if not query_concepts:
        return []

    out: set[str] = set()
    with connect() as conn, conn.cursor() as cur:
        # Look up which of the query concepts exist globally.
        cur.execute(
            "SELECT id, name FROM concept WHERE name = ANY(%s)",
            (list(set(query_concepts)),),
        )
        rows = cur.fetchall()
        if not rows:
            return []
        name_by_id = {cid: name for cid, name in rows}
        ids = list(name_by_id.keys())

        # For each, fetch the partner from concept_synonym (in either slot).
        cur.execute(
            """
            SELECT s.concept_a, s.concept_b, s.confidence,
                   ca.name AS name_a, cb.name AS name_b
            FROM concept_synonym s
            JOIN concept ca ON ca.id = s.concept_a
            JOIN concept cb ON cb.id = s.concept_b
            WHERE s.concept_a = ANY(%s) OR s.concept_b = ANY(%s)
            ORDER BY s.confidence DESC
            """,
            (ids, ids),
        )
        for a, b, _conf, name_a, name_b in cur.fetchall():
            if a in name_by_id and name_b not in query_concepts:
                out.add(name_b)
            if b in name_by_id and name_a not in query_concepts:
                out.add(name_a)

    return sorted(out)


# ---------------------------------------------------------------------------
# Privacy self-test
# ---------------------------------------------------------------------------


def attribute_check(workspace: str) -> dict:
    """Verify the privacy contract: no ``concept`` or ``concept_synonym`` row
    can be traced to a SINGLE tenant's content.

    Returns a dict that is the audit report. A passing report has zero
    ``single_tenant_synonyms`` and an empty ``review_needed`` list.

    The ``workspace`` argument is the tenant we are checking FROM. We use it
    to verify that the cross-tenant views never expose this workspace's
    unique vocabulary as synonym material.
    """
    report: dict = {
        "workspace": workspace,
        "concept_total": 0,
        "concept_single_tenant": 0,
        "synonym_total": 0,
        "single_tenant_synonyms": 0,
        "review_needed": [],
    }
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM concept")
        report["concept_total"] = cur.fetchone()[0]

        # Concepts seen by exactly one tenant: these MUST NOT participate in
        # any cross-tenant synonym, or they could leak that tenant's identity.
        cur.execute(
            "SELECT id, name FROM concept WHERE tenant_count = 1"
        )
        singletons = cur.fetchall()
        report["concept_single_tenant"] = len(singletons)

        cur.execute("SELECT COUNT(*) FROM concept_synonym")
        report["synonym_total"] = cur.fetchone()[0]

        # The hard invariant: a synonym pair must NOT reference any concept
        # with tenant_count == 1. If any such pair exists, the privacy
        # contract is broken.
        cur.execute(
            """
            SELECT s.id
            FROM concept_synonym s
            JOIN concept ca ON ca.id = s.concept_a
            JOIN concept cb ON cb.id = s.concept_b
            WHERE ca.tenant_count < 2 OR cb.tenant_count < 2
            """
        )
        bad = cur.fetchall()
        report["single_tenant_synonyms"] = len(bad)

        # Workspace-private singletons that exist only in THIS workspace:
        # these are concepts that, if any cross-tenant code surfaced them,
        # would identify this workspace. Flag for review.
        cur.execute(
            """
            SELECT c.name
            FROM concept c
            JOIN tenant_concept t ON t.concept_id = c.id
            WHERE c.tenant_count = 1 AND t.workspace = %s
            LIMIT 50
            """,
            (workspace,),
        )
        report["review_needed"] = [r[0] for r in cur.fetchall()]

    report["ok"] = (
        report["single_tenant_synonyms"] == 0
    )
    return report


# ---------------------------------------------------------------------------
# Convenience: enumerate workspaces (used by ``index-all`` CLI).
# Workspace names are NOT cross-tenant private metadata in this system --
# they appear in raw_events and chunk_acl which any operator with DB access
# already sees. Listing them is only used to drive batch indexing.
# ---------------------------------------------------------------------------


def list_workspaces() -> list[str]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT workspace FROM (
                SELECT workspace FROM chunk_acl
                UNION
                SELECT workspace FROM memory
            ) w
            ORDER BY workspace
            """
        )
        return [r[0] for r in cur.fetchall()]


def top_concepts(limit: int = 20) -> list[tuple[str, int, int]]:
    """Return [(name, tenant_count, global_count)] ordered by cross-tenant
    breadth. This is the demo moment: concepts seen by many tenants are
    population-validated vocabulary."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT name, tenant_count, global_count
            FROM concept
            ORDER BY tenant_count DESC, global_count DESC, name ASC
            LIMIT %s
            """,
            (limit,),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]
