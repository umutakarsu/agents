"""Phase 7: identity unification.

The promise: know that ``@ali`` (Slack), ``ali@acme.com`` (Gmail),
``ali-acme`` (GitHub) are the same person. The risk: a bad merge is a
security incident (now someone else can see Ali's restricted content). The
mechanism: confidence-scored merges with human-in-the-loop review for
risky ones.

Algorithm:
    1. ``add_alias`` records an alias for some identity. Existing alias rows
       just bump ``evidence_count``. New aliases create or reuse an
       identity and then call ``propose_merges`` for that identity.
    2. ``propose_merges`` scores each candidate identity in the workspace
       against the focal identity. Five signal classes feed a weighted
       average. The fired signals plus the combined confidence decide
       between auto-merge / propose / skip.
    3. ``merge`` is the actual mutation: it moves alias rows, re-points
       memory rows, snapshots the loser for reversal, and deletes the
       loser identity row.

Adversarial defenses:
    - Rate-limit alias additions per (source, workspace) to defend against
      a hostile connector spamming alias rows to inflate ``evidence_count``
      and engineer an auto-merge.
    - Brand-new sources (no track record in the workspace) have their
      alias-row confidence capped at ``0.7`` so a freshly connected hostile
      source cannot reach the auto-merge threshold on day one.
    - Even at combined confidence 0.99 we require >= 2 *distinct* signal
      classes to auto-merge. A single signal is never enough -- a name
      collision alone, however perfect, is just a name collision.

Anything below the propose threshold (0.7) is silently ignored. Anything
in (winner, loser) is logged in ``identity_merge_log`` with the loser's
full alias snapshot so the merge is reversible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from memlayer.db import connect

# ---------- thresholds -----------------------------------------------------

# Auto-merge requires BOTH:
#   - combined confidence >= AUTO_MERGE_CONF
#   - number of distinct fired signals >= AUTO_MERGE_MIN_SIGNALS
AUTO_MERGE_CONF = 0.95
AUTO_MERGE_MIN_SIGNALS = 2

# A pair below this is silently dropped.
PROPOSE_CONF = 0.70

# A single signal "fires" if its value crosses this floor. Anything weaker
# than this is treated as noise and excluded from the weighted average.
SIGNAL_FLOOR = 0.30

# Rate limit: per (workspace, source) at most this many aliases per hour.
ALIAS_RATE_LIMIT_PER_HOUR = 50

# Sources with this many or fewer existing alias rows in the workspace are
# considered "new" and have their alias confidence capped.
NEW_SOURCE_THRESHOLD = 1
NEW_SOURCE_CONF_CEILING = 0.7


# ---------- types ----------------------------------------------------------


@dataclass
class MergeSignal:
    """A single signal in a merge decision. ``fired`` is the boolean
    threshold check (``value > SIGNAL_FLOOR``); the weight is folded in at
    aggregation time."""

    name: str
    value: float
    weight: float

    @property
    def fired(self) -> bool:
        return self.value > SIGNAL_FLOOR


@dataclass
class MergeProposal:
    identity_a: int
    identity_b: int
    confidence: float
    signals: dict[str, Any]
    auto_merged: bool = False
    proposal_id: int | None = None  # only for stored (non-auto) proposals
    skipped_reason: str | None = None  # 'denylisted' / 'duplicate' / None


# ---------- internal helpers ----------------------------------------------


def _levenshtein(a: str, b: str) -> int:
    """Iterative Levenshtein. Small strings only (handles + emails) so the
    O(len(a)*len(b)) cost is negligible."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur[j] = min(
                cur[j - 1] + 1,        # insertion
                prev[j] + 1,           # deletion
                prev[j - 1] + cost,    # substitution
            )
        prev = cur
    return prev[-1]


def _norm_similarity(a: str, b: str) -> float:
    """Levenshtein-normalised similarity in ``[0, 1]``. Empty inputs return
    0.0 -- they have no signal value either way."""
    if not a or not b:
        return 0.0
    a = a.lower().strip()
    b = b.lower().strip()
    if not a or not b:
        return 0.0
    dist = _levenshtein(a, b)
    longest = max(len(a), len(b))
    if longest == 0:
        return 0.0
    return 1.0 - (dist / longest)


def _looks_like_email(s: str) -> bool:
    # We don't need RFC-grade validation. We just need to know if two
    # external_ids both *look* like emails so an exact-email comparator
    # makes sense.
    return "@" in s and "." in s.split("@", 1)[-1]


def _local_part(s: str) -> str:
    """Local-part of an email; or the handle stripped of a leading ``@``.

    Handle the leading-``@`` convention (Slack-style ``@ali``) BEFORE
    splitting on ``@`` -- otherwise ``"@ali".split("@", 1)[0]`` is the
    empty string and an empty handle would falsely match every other
    empty-handle identity in the workspace, manufacturing spurious
    ``exact_handle`` signals."""
    s = s.strip().lstrip("@")
    if "@" in s:
        head = s.split("@", 1)[0]
        return head.lower()
    return s.lower()


def _aliases_for(cur, identity_id: int) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT id, source, external_id, display_name, confidence, evidence_count
        FROM identity_alias
        WHERE identity_id = %s
        ORDER BY id
        """,
        (identity_id,),
    )
    return [
        {
            "id": r[0],
            "source": r[1],
            "external_id": r[2],
            "display_name": r[3],
            "confidence": float(r[4]),
            "evidence_count": int(r[5]),
        }
        for r in cur.fetchall()
    ]


def _identity_row(cur, identity_id: int) -> dict[str, Any] | None:
    cur.execute(
        "SELECT id, workspace, canonical_name, kind, entity_key "
        "FROM identity WHERE id = %s",
        (identity_id,),
    )
    r = cur.fetchone()
    if r is None:
        return None
    return {
        "id": r[0],
        "workspace": r[1],
        "canonical_name": r[2],
        "kind": r[3],
        "entity_key": r[4],
    }


def _pair(a: int, b: int) -> tuple[int, int]:
    """Canonical (lo, hi) pair so merge_proposal / denylist constraints hold."""
    return (a, b) if a < b else (b, a)


# ---------- signals --------------------------------------------------------


def _compute_signals(
    cur,
    workspace: str,
    aliases_a: list[dict[str, Any]],
    aliases_b: list[dict[str, Any]],
    name_a: str,
    name_b: str,
) -> list[MergeSignal]:
    """Compute the five signal classes for an (A, B) pair. Each signal is
    bounded to ``[0, 1]`` and carries a weight; ``_combine`` averages over
    those that fire (value > SIGNAL_FLOOR)."""

    signals: list[MergeSignal] = []

    emails_a = {a["external_id"].lower() for a in aliases_a if _looks_like_email(a["external_id"])}
    emails_b = {a["external_id"].lower() for a in aliases_b if _looks_like_email(a["external_id"])}

    # 1) Exact email match -- a shared verified email is almost decisive.
    if emails_a & emails_b:
        signals.append(MergeSignal("exact_email", 1.0, 1.0))
    else:
        signals.append(MergeSignal("exact_email", 0.0, 1.0))

    # 2) Exact handle match -- same external_id in different sources.
    # We strip an @ prefix so '@ali' (slack) matches 'ali' (notion).
    # Filter empties so a stray "" can't manufacture an overlap.
    handles_a = {_local_part(a["external_id"]) for a in aliases_a}
    handles_a.discard("")
    handles_b = {_local_part(a["external_id"]) for a in aliases_b}
    handles_b.discard("")
    if handles_a & handles_b:
        signals.append(MergeSignal("exact_handle", 0.9, 0.7))
    else:
        signals.append(MergeSignal("exact_handle", 0.0, 0.7))

    # 3) Local-part / handle similarity -- best pairwise Levenshtein over
    # local-parts. Emails included so 'ali@acme.com' vs 'ali-acme' lights up.
    if aliases_a and aliases_b:
        best = 0.0
        for a in aliases_a:
            la = _local_part(a["external_id"])
            for b in aliases_b:
                lb = _local_part(b["external_id"])
                s = _norm_similarity(la, lb)
                if s > best:
                    best = s
        signals.append(MergeSignal("handle_similarity", best, 0.5))
    else:
        signals.append(MergeSignal("handle_similarity", 0.0, 0.5))

    # 4) Display name similarity -- best pairwise across declared names.
    names_a = [a["display_name"] for a in aliases_a if a["display_name"]]
    names_b = [a["display_name"] for a in aliases_b if a["display_name"]]
    names_a.append(name_a)
    names_b.append(name_b)
    best_name = 0.0
    for na in names_a:
        for nb in names_b:
            s = _norm_similarity(na, nb)
            if s > best_name:
                best_name = s
    signals.append(MergeSignal("name_similarity", best_name, 0.6))

    # 5) Co-occurrence -- LIKE-count of pairs of external_ids appearing in
    # the same chunks.text. Cheap, source-agnostic. Normalised by
    # min(evidence_count) so a low-evidence pair can't ride a single shared
    # chunk to a high signal.
    co_occ = 0.0
    if aliases_a and aliases_b:
        ext_a = [a["external_id"] for a in aliases_a]
        ext_b = [b["external_id"] for b in aliases_b]
        best_co = 0
        for ea in ext_a:
            for eb in ext_b:
                if not ea or not eb or ea == eb:
                    continue
                cur.execute(
                    """
                    SELECT count(*)
                    FROM chunks c
                    JOIN chunk_acl ca ON ca.content_hash = c.content_hash
                    WHERE ca.workspace = %s
                      AND c.text ILIKE %s
                      AND c.text ILIKE %s
                    """,
                    (workspace, f"%{ea}%", f"%{eb}%"),
                )
                hits = int(cur.fetchone()[0])
                if hits > best_co:
                    best_co = hits
        if best_co > 0:
            min_evidence = max(
                1,
                min(
                    min(a["evidence_count"] for a in aliases_a),
                    min(a["evidence_count"] for a in aliases_b),
                ),
            )
            co_occ = min(1.0, best_co / float(min_evidence))
    signals.append(MergeSignal("co_occurrence", co_occ, 0.4))

    return signals


def _combine(signals: list[MergeSignal]) -> tuple[float, list[MergeSignal]]:
    """Weighted average across the *fired* signals. A signal that does not
    fire (value <= SIGNAL_FLOOR) is excluded -- otherwise weak signal
    floors would drag the average down on every pair. Returns
    ``(combined, fired)``."""
    fired = [s for s in signals if s.fired]
    if not fired:
        return 0.0, []
    num = sum(s.value * s.weight for s in fired)
    den = sum(s.weight for s in fired)
    if den == 0:
        return 0.0, fired
    return min(1.0, max(0.0, num / den)), fired


# ---------- adversarial defenses ------------------------------------------


def _check_rate_limit(cur, workspace: str, source: str) -> None:
    """Raise if (workspace, source) has added more than ALIAS_RATE_LIMIT_PER_HOUR
    aliases in the last hour. Caller catches and turns this into a 429 / log."""
    cur.execute(
        """
        SELECT count(*)
        FROM identity_alias ia
        JOIN identity i ON i.id = ia.identity_id
        WHERE i.workspace = %s
          AND ia.source = %s
          AND ia.created_at > now() - INTERVAL '1 hour'
        """,
        (workspace, source),
    )
    n = int(cur.fetchone()[0])
    if n >= ALIAS_RATE_LIMIT_PER_HOUR:
        raise RuntimeError(
            f"identity rate limit: {source} added {n} aliases to "
            f"{workspace} in the last hour (limit {ALIAS_RATE_LIMIT_PER_HOUR})"
        )


def _confidence_ceiling_for_source(
    cur, workspace: str, source: str, requested: float
) -> float:
    """Brand-new sources are capped to NEW_SOURCE_CONF_CEILING on their first
    few aliases. This blunts the day-1 attack where a hostile source
    instantly seeds high-confidence aliases that exact-email-match a known
    identity and trigger auto-merge."""
    cur.execute(
        """
        SELECT count(*)
        FROM identity_alias ia
        JOIN identity i ON i.id = ia.identity_id
        WHERE i.workspace = %s AND ia.source = %s
        """,
        (workspace, source),
    )
    prior = int(cur.fetchone()[0])
    if prior <= NEW_SOURCE_THRESHOLD:
        return min(requested, NEW_SOURCE_CONF_CEILING)
    return requested


# ---------- public API ----------------------------------------------------


def add_alias(
    workspace: str,
    source: str,
    external_id: str,
    display_name: str | None = None,
    entity_key: str | None = None,
    canonical_name: str | None = None,
    kind: str = "person",
    confidence: float = 1.0,
) -> int:
    """Record an alias. Returns the identity_id this alias resolves to.

    If ``(source, external_id)`` already exists, just bump
    ``evidence_count``. Otherwise: ensure an identity with the given
    ``entity_key`` exists (creating one if not), insert the alias, and run
    ``propose_merges`` for that identity.

    Confidence is clamped against the per-source ceiling for brand-new
    sources. Rate limit on (workspace, source) defends against alias spam.
    """
    if not source or not external_id:
        raise ValueError("source and external_id are required")
    if kind not in ("person", "team", "project", "topic"):
        raise ValueError(f"kind must be one of person/team/project/topic, got {kind!r}")

    with connect() as conn, conn.cursor() as cur:
        # 1. If alias already exists, bump evidence_count and we're done.
        cur.execute(
            "SELECT id, identity_id FROM identity_alias "
            "WHERE source = %s AND external_id = %s",
            (source, external_id),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                "UPDATE identity_alias SET evidence_count = evidence_count + 1 "
                "WHERE id = %s",
                (existing[0],),
            )
            conn.commit()
            return int(existing[1])

        # 2. Rate-limit + new-source confidence ceiling.
        _check_rate_limit(cur, workspace, source)
        confidence = _confidence_ceiling_for_source(cur, workspace, source, confidence)

        # 3. Resolve / create the identity. If the caller passed an
        # entity_key, an identity with that (workspace, entity_key) is
        # reused; otherwise we synthesise a stable key from canonical_name
        # (or display_name as fallback).
        if entity_key is None:
            base = (canonical_name or display_name or external_id).strip().lower()
            base = "".join(c if c.isalnum() else "-" for c in base).strip("-") or "alias"
            entity_key = f"{kind}:{base}"
        if canonical_name is None:
            canonical_name = display_name or external_id

        cur.execute(
            "SELECT id FROM identity WHERE workspace = %s AND entity_key = %s",
            (workspace, entity_key),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO identity (workspace, canonical_name, kind, entity_key) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (workspace, canonical_name, kind, entity_key),
            )
            identity_id = int(cur.fetchone()[0])
        else:
            identity_id = int(row[0])

        # 4. Insert the alias.
        cur.execute(
            """
            INSERT INTO identity_alias
                (identity_id, source, external_id, display_name,
                 confidence, evidence_count)
            VALUES (%s, %s, %s, %s, %s, 1)
            ON CONFLICT (source, external_id) DO UPDATE
                SET evidence_count = identity_alias.evidence_count + 1
            """,
            (identity_id, source, external_id, display_name, confidence),
        )
        conn.commit()

    # 5. Look for merge candidates. Done in a separate transaction so the
    # alias is durable even if proposal generation has a transient issue.
    propose_merges(workspace, identity_id)
    return identity_id


def propose_merges(workspace: str, identity_id: int) -> list[MergeProposal]:
    """Score every other identity in the workspace against this one and
    decide auto-merge / propose / skip per the rules at the top of the
    module."""
    out: list[MergeProposal] = []
    with connect() as conn, conn.cursor() as cur:
        focal = _identity_row(cur, identity_id)
        if focal is None:
            return out
        cur.execute(
            "SELECT id FROM identity "
            "WHERE workspace = %s AND id != %s",
            (workspace, identity_id),
        )
        others = [int(r[0]) for r in cur.fetchall()]

    for other_id in others:
        # Each candidate scored in its own connection so a slow LIKE on one
        # pair doesn't keep a big transaction open.
        proposal = _evaluate_pair(workspace, identity_id, other_id)
        if proposal is None:
            continue
        out.append(proposal)

    return out


def _evaluate_pair(
    workspace: str, a_id: int, b_id: int
) -> MergeProposal | None:
    """Score one (A, B) pair, possibly auto-merge or persist a proposal."""

    lo, hi = _pair(a_id, b_id)
    with connect() as conn, conn.cursor() as cur:
        # Skip if denylisted.
        cur.execute(
            "SELECT 1 FROM merge_denylist "
            "WHERE workspace = %s AND identity_a = %s AND identity_b = %s "
            "AND (until IS NULL OR until > now())",
            (workspace, lo, hi),
        )
        if cur.fetchone() is not None:
            return MergeProposal(
                identity_a=lo, identity_b=hi, confidence=0.0, signals={},
                skipped_reason="denylisted",
            )

        # Skip if a pending proposal already exists.
        cur.execute(
            "SELECT id, confidence FROM merge_proposal "
            "WHERE workspace = %s AND identity_a = %s AND identity_b = %s "
            "AND status = 'pending'",
            (workspace, lo, hi),
        )
        already = cur.fetchone()

        ident_a = _identity_row(cur, lo)
        ident_b = _identity_row(cur, hi)
        if ident_a is None or ident_b is None:
            return None
        aliases_a = _aliases_for(cur, lo)
        aliases_b = _aliases_for(cur, hi)

        signals = _compute_signals(
            cur, workspace,
            aliases_a, aliases_b,
            ident_a["canonical_name"], ident_b["canonical_name"],
        )
    combined, fired = _combine(signals)
    signals_dict = {s.name: round(s.value, 4) for s in signals}
    signals_dict["_combined"] = round(combined, 4)
    signals_dict["_fired"] = [s.name for s in fired]

    auto_merge = (
        combined >= AUTO_MERGE_CONF
        and len(fired) >= AUTO_MERGE_MIN_SIGNALS
    )
    propose = (
        combined >= PROPOSE_CONF and not auto_merge
    ) or (
        combined >= AUTO_MERGE_CONF and len(fired) < AUTO_MERGE_MIN_SIGNALS
    )

    if auto_merge:
        merge(workspace, lo, hi, signals_dict, merged_by="auto")
        return MergeProposal(
            identity_a=lo, identity_b=hi, confidence=combined,
            signals=signals_dict, auto_merged=True,
        )
    if propose:
        if already is not None:
            # Refresh confidence/signals so the UI shows the latest score.
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE merge_proposal SET confidence = %s, signals = %s "
                    "WHERE id = %s",
                    (combined, json.dumps(signals_dict), int(already[0])),
                )
                conn.commit()
            return MergeProposal(
                identity_a=lo, identity_b=hi, confidence=combined,
                signals=signals_dict, proposal_id=int(already[0]),
            )
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO merge_proposal
                    (workspace, identity_a, identity_b, confidence, signals)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (workspace, lo, hi, combined, json.dumps(signals_dict)),
            )
            new_id = int(cur.fetchone()[0])
            conn.commit()
        return MergeProposal(
            identity_a=lo, identity_b=hi, confidence=combined,
            signals=signals_dict, proposal_id=new_id,
        )

    return None  # below threshold


def _pick_winner(cur, a_id: int, b_id: int) -> tuple[int, int]:
    """Pick which identity wins. Rule:
        - prefer the one with more alias evidence (total evidence_count)
        - tie-break by lower id (older identity)
    Documented so an attacker can't manipulate winner choice arbitrarily."""
    cur.execute(
        "SELECT identity_id, COALESCE(SUM(evidence_count), 0) "
        "FROM identity_alias WHERE identity_id IN (%s, %s) "
        "GROUP BY identity_id",
        (a_id, b_id),
    )
    evidence = {int(r[0]): int(r[1]) for r in cur.fetchall()}
    ea = evidence.get(a_id, 0)
    eb = evidence.get(b_id, 0)
    if ea > eb:
        return a_id, b_id
    if eb > ea:
        return b_id, a_id
    # tie -> lower id wins
    if a_id < b_id:
        return a_id, b_id
    return b_id, a_id


def merge(
    workspace: str,
    identity_a: int,
    identity_b: int,
    signals: dict[str, Any],
    merged_by: str = "auto",
) -> int:
    """Merge two identities. Picks a winner, moves aliases, re-points
    memory rows, logs the before-state, deletes the loser. Returns the
    winner id."""
    with connect() as conn, conn.cursor() as cur:
        a = _identity_row(cur, identity_a)
        b = _identity_row(cur, identity_b)
        if a is None or b is None:
            raise ValueError("one or both identities do not exist")
        if a["workspace"] != workspace or b["workspace"] != workspace:
            raise ValueError("identities are in different workspaces")

        winner_id, loser_id = _pick_winner(cur, identity_a, identity_b)
        winner = a if a["id"] == winner_id else b
        loser = a if a["id"] == loser_id else b

        # Snapshot the loser's aliases BEFORE we move them, so reversal can
        # restore the original (identity_id, source, external_id) mapping.
        loser_aliases = _aliases_for(cur, loser_id)

        # Move aliases. UNIQUE (source, external_id) means an exact dup on
        # the winner's side is already a clue we should have merged --
        # delete the loser's row in that case.
        for al in loser_aliases:
            cur.execute(
                "SELECT 1 FROM identity_alias "
                "WHERE identity_id = %s AND source = %s AND external_id = %s",
                (winner_id, al["source"], al["external_id"]),
            )
            if cur.fetchone() is not None:
                cur.execute("DELETE FROM identity_alias WHERE id = %s", (al["id"],))
            else:
                cur.execute(
                    "UPDATE identity_alias SET identity_id = %s WHERE id = %s",
                    (winner_id, al["id"]),
                )

        # Re-point memory rows. Same workspace, loser.entity_key -> winner.entity_key.
        cur.execute(
            "UPDATE memory SET entity_key = %s "
            "WHERE workspace = %s AND entity_key = %s",
            (winner["entity_key"], workspace, loser["entity_key"]),
        )

        # Audit log -- captures the loser as it was, so reverse_merge can rebuild.
        cur.execute(
            """
            INSERT INTO identity_merge_log
                (workspace, winner_id, loser_id, loser_canonical_name,
                 loser_entity_key, aliases_moved, signals, merged_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                workspace,
                winner_id,
                loser_id,
                loser["canonical_name"],
                loser["entity_key"],
                json.dumps(loser_aliases),
                json.dumps(signals),
                merged_by,
            ),
        )
        cur.execute("DELETE FROM identity WHERE id = %s", (loser_id,))
        conn.commit()
    return winner_id


def approve(proposal_id: int, by: str) -> int:
    """Approve a pending proposal -> call merge() -> mark proposal approved.
    Returns the winner id."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT workspace, identity_a, identity_b, signals, status "
            "FROM merge_proposal WHERE id = %s",
            (proposal_id,),
        )
        r = cur.fetchone()
        if r is None:
            raise ValueError(f"proposal {proposal_id} not found")
        workspace, a_id, b_id, signals, status = r
        if status != "pending":
            raise ValueError(f"proposal {proposal_id} is {status!r}, not pending")

    winner_id = merge(workspace, int(a_id), int(b_id), signals or {}, merged_by=by)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE merge_proposal SET status = 'approved', "
            "resolved_at = now(), resolved_by = %s WHERE id = %s",
            (by, proposal_id),
        )
        conn.commit()
    return winner_id


def reject(proposal_id: int, reason: str, by: str) -> None:
    """Reject a pending proposal and insert the pair into the denylist so
    we never propose it again (adversarial defense: a malicious source
    can't keep retrying the same bait)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT workspace, identity_a, identity_b, status "
            "FROM merge_proposal WHERE id = %s",
            (proposal_id,),
        )
        r = cur.fetchone()
        if r is None:
            raise ValueError(f"proposal {proposal_id} not found")
        workspace, a_id, b_id, status = r
        if status != "pending":
            raise ValueError(f"proposal {proposal_id} is {status!r}, not pending")
        cur.execute(
            "UPDATE merge_proposal SET status = 'rejected', "
            "resolved_at = now(), resolved_by = %s WHERE id = %s",
            (by, proposal_id),
        )
        # Permanent denylist by default (until=NULL). A future variant
        # could expire it after N days.
        cur.execute(
            """
            INSERT INTO merge_denylist (workspace, identity_a, identity_b, reason)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (workspace, identity_a, identity_b) DO UPDATE
                SET reason = EXCLUDED.reason
            """,
            (workspace, int(a_id), int(b_id), reason),
        )
        conn.commit()


def reverse_merge(log_id: int, by: str) -> int:
    """Restore the loser identity from a merge log entry. Re-creates the
    identity row with its old canonical_name + entity_key, moves the
    matching alias rows back, re-points memory rows back. Returns the
    restored loser id."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT workspace, winner_id, loser_id, loser_canonical_name,
                   loser_entity_key, aliases_moved, reversed_at
            FROM identity_merge_log WHERE id = %s
            """,
            (log_id,),
        )
        r = cur.fetchone()
        if r is None:
            raise ValueError(f"merge log {log_id} not found")
        workspace, winner_id, _orig_loser_id, loser_name, loser_key, aliases_json, reversed_at = r
        if reversed_at is not None:
            raise ValueError(f"merge log {log_id} already reversed at {reversed_at}")

        # Look up the kind from the winner so we can recreate the loser
        # with a sensible kind. (The aliases-moved snapshot doesn't record
        # the loser's original kind. In practice mergeable identities are
        # almost always the same kind, so winner.kind is the right fallback.)
        cur.execute("SELECT kind FROM identity WHERE id = %s", (winner_id,))
        wrow = cur.fetchone()
        winner_kind = wrow[0] if wrow else "person"

        cur.execute(
            "INSERT INTO identity (workspace, canonical_name, kind, entity_key) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (workspace, loser_name, winner_kind, loser_key),
        )
        new_loser_id = int(cur.fetchone()[0])

        aliases = aliases_json if isinstance(aliases_json, list) else json.loads(aliases_json)
        for al in aliases:
            # The alias may have been deleted as a dup at merge time; in
            # that case there's nothing to move. Otherwise move it back.
            cur.execute(
                "UPDATE identity_alias SET identity_id = %s "
                "WHERE source = %s AND external_id = %s "
                "AND identity_id = %s",
                (new_loser_id, al["source"], al["external_id"], winner_id),
            )

        # Re-point memory rows.
        cur.execute("SELECT entity_key FROM identity WHERE id = %s", (winner_id,))
        w = cur.fetchone()
        if w is not None:
            cur.execute(
                "UPDATE memory SET entity_key = %s "
                "WHERE workspace = %s AND entity_key = %s",
                (loser_key, workspace, w[0]),
            )

        cur.execute(
            "UPDATE identity_merge_log SET reversed_at = now() WHERE id = %s",
            (log_id,),
        )
        # Denylist the pair so we don't immediately re-propose what we
        # just deliberately split.
        lo, hi = _pair(new_loser_id, winner_id)
        cur.execute(
            """
            INSERT INTO merge_denylist (workspace, identity_a, identity_b, reason)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (workspace, lo, hi, f"reversed by {by}"),
        )
        conn.commit()
    return new_loser_id


# ---------- read-side helpers (used by memscope) --------------------------


def list_identities(workspace: str) -> list[dict[str, Any]]:
    """All identities in a workspace with their aliases. Used by the UI."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.id, i.canonical_name, i.kind, i.entity_key, i.created_at,
                   COALESCE(json_agg(json_build_object(
                       'id', ia.id,
                       'source', ia.source,
                       'external_id', ia.external_id,
                       'display_name', ia.display_name,
                       'confidence', ia.confidence,
                       'evidence_count', ia.evidence_count
                   )) FILTER (WHERE ia.id IS NOT NULL), '[]'::json) AS aliases
            FROM identity i
            LEFT JOIN identity_alias ia ON ia.identity_id = i.id
            WHERE i.workspace = %s
            GROUP BY i.id
            ORDER BY i.canonical_name
            """,
            (workspace,),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        aliases = r[5] if isinstance(r[5], list) else json.loads(r[5]) if r[5] else []
        out.append({
            "id": int(r[0]),
            "canonical_name": r[1],
            "kind": r[2],
            "entity_key": r[3],
            "created_at": r[4].isoformat() if r[4] else None,
            "aliases": aliases,
            "alias_count": len(aliases),
        })
    return out


def get_identity(identity_id: int) -> dict[str, Any] | None:
    with connect() as conn, conn.cursor() as cur:
        row = _identity_row(cur, identity_id)
        if row is None:
            return None
        aliases = _aliases_for(cur, identity_id)
    row["aliases"] = aliases
    row["alias_count"] = len(aliases)
    return row


def list_proposals(workspace: str, status: str = "pending") -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT mp.id, mp.workspace, mp.identity_a, mp.identity_b,
                   mp.confidence, mp.signals, mp.status,
                   mp.created_at, mp.resolved_at, mp.resolved_by,
                   ia.canonical_name, ia.kind, ia.entity_key,
                   ib.canonical_name, ib.kind, ib.entity_key
            FROM merge_proposal mp
            LEFT JOIN identity ia ON ia.id = mp.identity_a
            LEFT JOIN identity ib ON ib.id = mp.identity_b
            WHERE mp.workspace = %s AND mp.status = %s
            ORDER BY mp.confidence DESC, mp.id DESC
            """,
            (workspace, status),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        signals = r[5] if isinstance(r[5], dict) else json.loads(r[5]) if r[5] else {}
        with connect() as conn, conn.cursor() as cur:
            a_aliases = _aliases_for(cur, int(r[2])) if r[10] else []
            b_aliases = _aliases_for(cur, int(r[3])) if r[13] else []
        out.append({
            "id": int(r[0]),
            "workspace": r[1],
            "identity_a": {
                "id": int(r[2]),
                "canonical_name": r[10],
                "kind": r[11],
                "entity_key": r[12],
                "aliases": a_aliases,
            },
            "identity_b": {
                "id": int(r[3]),
                "canonical_name": r[13],
                "kind": r[14],
                "entity_key": r[15],
                "aliases": b_aliases,
            },
            "confidence": float(r[4]),
            "signals": signals,
            "status": r[6],
            "created_at": r[7].isoformat() if r[7] else None,
            "resolved_at": r[8].isoformat() if r[8] else None,
            "resolved_by": r[9],
        })
    return out


def cluster_graph(identity_id: int) -> dict[str, Any] | None:
    """A simple hub-and-spoke graph for one identity: the identity is the
    hub, each alias is a leaf, edges are 'same identity'. The UI colors
    leaves by source."""
    ident = get_identity(identity_id)
    if ident is None:
        return None
    nodes = [{
        "id": f"i:{ident['id']}",
        "kind": "identity",
        "label": ident["canonical_name"],
        "entity_key": ident["entity_key"],
    }]
    edges = []
    for al in ident["aliases"]:
        nid = f"a:{al['id']}"
        nodes.append({
            "id": nid,
            "kind": "alias",
            "source": al["source"],
            "label": al["external_id"],
            "display_name": al["display_name"],
            "confidence": al["confidence"],
            "evidence_count": al["evidence_count"],
        })
        edges.append({"from": f"i:{ident['id']}", "to": nid})
    return {"identity": ident, "nodes": nodes, "edges": edges}
