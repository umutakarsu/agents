"""Governance-modeled conflict resolution.

The old precedence ladder was a flat ``human > system > agent``. Real
organisations don't work that way: a security lead's "this is wrong" must
beat an engineering manager's "this is right" for a security incident, even
though both are humans; two engineering managers might tie on a general
topic and the system should *surface* the disagreement rather than silently
pick one.

This module turns precedence into a function of
``(role, entity_kind, confidence, recency, policy)``.

* A **role** is a per-workspace label such as ``security_lead`` or
  ``ic_engineer`` attached to an underlying ``source_type``.
* A **writer_role** maps a concrete writer (``source_type, source_id``) to a
  role within a workspace -- e.g. ``human:manager`` -> ``engineering_manager``.
* An **authority_policy** rule says "for entity_kind X in workspace W, role
  R has authority A". Higher authority wins. Multiple rules can match a
  given write (the catch-all ``*`` kind and the specific kind); the highest
  applicable authority is used.
* When two live rows for the same entity have authorities within
  ``CONFLICT_EPSILON`` of each other, ``writeback.remember()`` leaves both
  live and records a row in ``memory_conflict`` for human resolution.

Backward compatibility: ``authority_for()`` falls back to the original flat
ladder (human=3.0, system=2.0, agent=1.0) when no policy is configured.
That means existing workspaces and the Phase 0-6 demos continue to behave
identically until policies are explicitly seeded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from memlayer.db import connect

# Default authorities mirror the original flat ladder (human=3, system=2,
# agent=1) so a workspace with no policies set up behaves exactly like
# pre-Phase-9 memlayer.
DEFAULT_AUTHORITY: dict[str, float] = {
    "human": 3.0,
    "system": 2.0,
    "agent": 1.0,
}

# Two authorities within this tolerance are treated as a tie -> the system
# surfaces a conflict instead of silently picking a winner. Confidence /
# recency tiebreakers still feed _precedence() further down the chain, but
# the conflict ledger captures the disagreement either way.
CONFLICT_EPSILON: float = 0.05


@dataclass
class Conflict:
    id: int
    workspace: str
    entity_key: str
    row_a: int
    row_b: int
    authority_a: float
    authority_b: float
    detected_at: datetime
    status: str
    resolved_by: str | None = None
    winner_id: int | None = None
    resolved_at: datetime | None = None


# ---------------------------------------------------------------------------
# Entity classification
# ---------------------------------------------------------------------------
def classify_entity(entity_key: str) -> str:
    """Derive an ``entity_kind`` from an ``entity_key``.

    Rules (first match wins):

    * Prefix-based: ``security:...`` -> ``security``,
      ``engineering:...`` / ``eng:...`` -> ``engineering``,
      ``sales:...`` / ``account:...`` -> ``sales``,
      ``hr:...`` -> ``hr``, ``finance:...`` -> ``finance``,
      ``person:...`` -> ``people``.
    * Topic substring: a ``topic:`` key with ``layoff`` or ``hr`` in it
      classifies as ``hr``; with ``security`` / ``incident`` / ``breach``
      as ``security``; with ``revenue`` / ``pricing`` as ``finance``.
    * Otherwise ``general``.

    Heuristic on purpose -- the classification is just a hook for policy
    lookup. Workspaces can re-tag rows manually if the default mapping
    doesn't match their conventions.
    """
    if not entity_key:
        return "general"
    key = entity_key.lower()
    prefix, _, rest = key.partition(":")
    if prefix == "security":
        return "security"
    if prefix in ("engineering", "eng"):
        return "engineering"
    if prefix in ("sales", "account"):
        return "sales"
    if prefix == "hr":
        return "hr"
    if prefix == "finance":
        return "finance"
    if prefix == "person":
        return "people"
    if prefix == "topic":
        if any(tok in rest for tok in ("layoff", "hr", "hiring", "firing")):
            return "hr"
        if any(tok in rest for tok in ("security", "incident", "breach", "vuln")):
            return "security"
        if any(tok in rest for tok in ("revenue", "pricing", "finance", "budget")):
            return "finance"
    return "general"


# ---------------------------------------------------------------------------
# Authority lookup
# ---------------------------------------------------------------------------
def authority_for(
    workspace: str,
    source_type: str,
    source_id: str,
    entity_kind: str,
) -> float:
    """Effective authority for a writer in this workspace and entity_kind.

    Resolution order (each step short-circuits on success):

    1. Find the writer's role via ``writer_role`` (workspace, then 'default').
    2. If a role is found, look up ``authority_policy`` for that role with
       the specific ``entity_kind``; then with the catch-all ``'*'`` kind;
       in the workspace; then in the ``'default'`` workspace.
    3. If a role exists but no policy matched, return the role's
       ``base_authority``.
    4. Otherwise fall back to ``DEFAULT_AUTHORITY[source_type]`` (the flat
       ladder), or 0.0 if the source_type is unknown.

    This means a workspace with no governance rows behaves *identically* to
    pre-Phase-9 memlayer -- the fallback is the old ladder.
    """
    auth, _governed = _authority_with_origin(
        workspace, source_type, source_id, entity_kind
    )
    return auth


def _authority_with_origin(
    workspace: str,
    source_type: str,
    source_id: str,
    entity_kind: str,
) -> tuple[float, bool]:
    """Same as ``authority_for`` but also returns whether the answer came
    from an explicit policy/role row (``True``) or from the flat-ladder
    fallback (``False``).

    ``remember()`` uses this to decide whether a tie should surface a
    conflict: ties between two *governed* writers are interesting; ties
    between two pure-fallback writers (e.g. two agents in a workspace with
    no policies set up) are the same as old-memlayer "two agent rows" and
    should not produce surprise conflict rows.
    """
    with connect() as conn, conn.cursor() as cur:
        role_id = _lookup_writer_role(cur, workspace, source_type, source_id)
        if role_id is not None:
            auth = _lookup_policy_authority(
                cur, workspace, entity_kind, role_id
            )
            if auth is not None:
                return float(auth), True
            # Role exists but no policy match -> use the role's base_authority.
            cur.execute(
                "SELECT base_authority FROM role WHERE id = %s", (role_id,)
            )
            row = cur.fetchone()
            if row is not None:
                return float(row[0]), True
    return DEFAULT_AUTHORITY.get(source_type, 0.0), False


def _lookup_writer_role(
    cur,
    workspace: str,
    source_type: str,
    source_id: str,
) -> int | None:
    """Resolve a writer to a role_id, preferring workspace-specific
    assignments over the 'default' workspace fallback.

    Also: if ``source_id`` happens to *be* a role name in this workspace,
    treat that as an implicit assignment. This is the lightweight path the
    CLI uses (no need to call ``assign`` for every demo writer)."""
    # 1. Explicit assignment in this workspace.
    cur.execute(
        "SELECT role_id FROM writer_role "
        "WHERE workspace = %s AND source_type = %s AND source_id = %s",
        (workspace, source_type, source_id),
    )
    row = cur.fetchone()
    if row is not None:
        return int(row[0])
    # 2. Explicit assignment in the default workspace.
    cur.execute(
        "SELECT role_id FROM writer_role "
        "WHERE workspace = 'default' AND source_type = %s AND source_id = %s",
        (source_type, source_id),
    )
    row = cur.fetchone()
    if row is not None:
        return int(row[0])
    # 3. source_id matches a role name in this workspace.
    cur.execute(
        "SELECT id FROM role "
        "WHERE workspace = %s AND source_type = %s AND name = %s",
        (workspace, source_type, source_id),
    )
    row = cur.fetchone()
    if row is not None:
        return int(row[0])
    # 4. source_id matches a role name in the default workspace.
    cur.execute(
        "SELECT id FROM role "
        "WHERE workspace = 'default' AND source_type = %s AND name = %s",
        (source_type, source_id),
    )
    row = cur.fetchone()
    if row is not None:
        return int(row[0])
    return None


def _lookup_policy_authority(
    cur,
    workspace: str,
    entity_kind: str,
    role_id: int,
) -> float | None:
    """Highest applicable policy authority for (workspace, entity_kind, role).

    Falls back through the wildcard kind and the 'default' workspace so a
    single global rule (e.g. "human=3.0 for entity_kind='*'") covers every
    tenant without per-workspace setup.
    """
    # Prefer specific kind in this workspace, then '*' in this workspace,
    # then specific kind in default, then '*' in default. Within each
    # bucket pick the max in case multiple rows exist.
    cur.execute(
        """
        SELECT MAX(authority) FROM authority_policy
        WHERE role_id = %s
          AND (
              (workspace = %s AND entity_kind = %s)
           OR (workspace = %s AND entity_kind = '*')
           OR (workspace = 'default' AND entity_kind = %s)
           OR (workspace = 'default' AND entity_kind = '*')
          )
        """,
        (role_id, workspace, entity_kind, workspace, entity_kind),
    )
    row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


# ---------------------------------------------------------------------------
# Role + policy management
# ---------------------------------------------------------------------------
def upsert_role(
    workspace: str,
    source_type: str,
    name: str,
    base_authority: float = 1.0,
) -> int:
    """Create or update a role; return its id. Idempotent."""
    if source_type not in ("human", "system", "agent"):
        raise ValueError(f"invalid source_type {source_type!r}")
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO role (workspace, source_type, name, base_authority)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (workspace, source_type, name)
                DO UPDATE SET base_authority = EXCLUDED.base_authority
            RETURNING id
            """,
            (workspace, source_type, name, base_authority),
        )
        rid = int(cur.fetchone()[0])
        conn.commit()
    return rid


def upsert_policy(
    workspace: str,
    entity_kind: str,
    role_id: int,
    authority: float,
) -> int:
    """Create or update a policy row; return its id. Idempotent."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO authority_policy
                (workspace, entity_kind, role_id, authority)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (workspace, entity_kind, role_id)
                DO UPDATE SET authority = EXCLUDED.authority
            RETURNING id
            """,
            (workspace, entity_kind, role_id, authority),
        )
        pid = int(cur.fetchone()[0])
        conn.commit()
    return pid


def assign_writer(
    workspace: str,
    source_type: str,
    source_id: str,
    role_id: int,
) -> None:
    """Map a concrete writer (source_type, source_id) to a role. Idempotent."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO writer_role
                (workspace, source_type, source_id, role_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (workspace, source_type, source_id)
                DO UPDATE SET role_id = EXCLUDED.role_id
            """,
            (workspace, source_type, source_id, role_id),
        )
        conn.commit()


def list_policies(workspace: str | None = None) -> list[dict]:
    """Return policy rows joined with role info for the CLI."""
    sql = """
        SELECT p.id, p.workspace, p.entity_kind, p.authority,
               r.id, r.source_type, r.name, r.base_authority
        FROM authority_policy p
        JOIN role r ON r.id = p.role_id
    """
    params: tuple = ()
    if workspace is not None:
        sql += " WHERE p.workspace = %s OR p.workspace = 'default'"
        params = (workspace,)
    sql += " ORDER BY p.workspace, p.entity_kind, p.authority DESC"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        out = []
        for row in cur.fetchall():
            out.append({
                "id": row[0],
                "workspace": row[1],
                "entity_kind": row[2],
                "authority": float(row[3]),
                "role_id": row[4],
                "source_type": row[5],
                "role_name": row[6],
                "base_authority": float(row[7]),
            })
    return out


# ---------------------------------------------------------------------------
# Conflict ledger
# ---------------------------------------------------------------------------
def record_conflict(
    workspace: str,
    entity_key: str,
    row_a_id: int,
    row_b_id: int,
    authority_a: float,
    authority_b: float,
) -> int:
    """Insert a pending conflict if one isn't already open for this pair.

    The CHECK(row_a < row_b) in the schema normalises ordering; we mirror
    that here so a re-detection of the same pair doesn't double-insert.
    """
    lo, hi = min(row_a_id, row_b_id), max(row_a_id, row_b_id)
    if lo == hi:
        raise ValueError("row_a_id and row_b_id must differ")
    # Preserve which authority belongs to which row after normalisation.
    if row_a_id < row_b_id:
        auth_lo, auth_hi = authority_a, authority_b
    else:
        auth_lo, auth_hi = authority_b, authority_a
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM memory_conflict
            WHERE workspace = %s AND row_a = %s AND row_b = %s
              AND status = 'pending'
            """,
            (workspace, lo, hi),
        )
        existing = cur.fetchone()
        if existing is not None:
            return int(existing[0])
        cur.execute(
            """
            INSERT INTO memory_conflict
                (workspace, entity_key, row_a, row_b,
                 authority_a, authority_b)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (workspace, entity_key, lo, hi, auth_lo, auth_hi),
        )
        cid = int(cur.fetchone()[0])
        conn.commit()
    return cid


def pending_conflicts(workspace: str | None = None) -> list[Conflict]:
    """Open conflicts, newest first. Used by the CLI / UI."""
    sql = """
        SELECT id, workspace, entity_key, row_a, row_b,
               authority_a, authority_b, detected_at, status,
               resolved_by, winner_id, resolved_at
        FROM memory_conflict
        WHERE status = 'pending'
    """
    params: tuple = ()
    if workspace is not None:
        sql += " AND workspace = %s"
        params = (workspace,)
    sql += " ORDER BY detected_at DESC"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        Conflict(
            id=r[0], workspace=r[1], entity_key=r[2],
            row_a=r[3], row_b=r[4],
            authority_a=float(r[5]), authority_b=float(r[6]),
            detected_at=r[7], status=r[8],
            resolved_by=r[9], winner_id=r[10], resolved_at=r[11],
        )
        for r in rows
    ]


def resolve_conflict(
    workspace: str,
    row_a_id: int,
    row_b_id: int,
    resolved_by: str,
    winner_id: int,
) -> int:
    """Resolve the pending conflict between (row_a_id, row_b_id): mark the
    winner current, supersede the loser, flip status='resolved'.

    Returns the conflict id. Raises if no pending conflict exists, or if
    ``winner_id`` isn't one of the two rows.
    """
    lo, hi = min(row_a_id, row_b_id), max(row_a_id, row_b_id)
    if winner_id not in (lo, hi):
        raise ValueError(
            f"winner_id {winner_id} must be one of "
            f"{lo} or {hi}"
        )
    loser_id = lo if winner_id == hi else hi
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM memory_conflict
            WHERE workspace = %s AND row_a = %s AND row_b = %s
              AND status = 'pending'
            """,
            (workspace, lo, hi),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(
                f"no pending conflict for rows ({lo},{hi}) in {workspace!r}"
            )
        conflict_id = int(row[0])
        # Supersede the loser (only if it's still live -- a concurrent write
        # might already have superseded it).
        cur.execute(
            "UPDATE memory SET superseded_by = %s "
            "WHERE id = %s AND superseded_by IS NULL",
            (winner_id, loser_id),
        )
        cur.execute(
            """
            UPDATE memory_conflict
            SET status = 'resolved',
                resolved_at = now(),
                resolved_by = %s,
                winner_id = %s
            WHERE id = %s
            """,
            (resolved_by, winner_id, conflict_id),
        )
        conn.commit()
    return conflict_id


# ---------------------------------------------------------------------------
# Seeders
# ---------------------------------------------------------------------------
def seed_default_policies() -> None:
    """Populate the ``default`` workspace with the flat-ladder equivalent
    policies. Idempotent -- calling it repeatedly is a no-op.

    These rows make the new path explicit (`human` role at authority 3.0
    for entity_kind '*', system at 2.0, agent at 1.0). Even without them
    the fallback in ``authority_for()`` returns the same numbers, but
    seeding makes the behaviour visible in the policy table.
    """
    # Catch-all human / system / agent roles in the 'default' workspace.
    human_role = upsert_role("default", "human", "human", base_authority=3.0)
    system_role = upsert_role("default", "system", "system", base_authority=2.0)
    agent_role = upsert_role("default", "agent", "agent", base_authority=1.0)
    upsert_policy("default", "*", human_role, 3.0)
    upsert_policy("default", "*", system_role, 2.0)
    upsert_policy("default", "*", agent_role, 1.0)
    # 'general' kind gets the same flat ladder explicitly, so a policy
    # listing for a workspace inheriting from default shows something sane.
    upsert_policy("default", "general", human_role, 3.0)
    upsert_policy("default", "general", system_role, 2.0)
    upsert_policy("default", "general", agent_role, 1.0)


def seed_acme_policies() -> None:
    """Seed the example policy set for the ``acme`` workspace.

    Demonstrates role-based override: a ``security_lead`` outranks an
    ``engineering_manager`` for security-tagged entities, the reverse for
    engineering entities, and both tie on general topics so the system
    surfaces the conflict instead of silently picking.
    """
    sec = upsert_role("acme", "human", "security_lead", base_authority=4.5)
    eng = upsert_role("acme", "human", "engineering_manager", base_authority=4.0)
    ic = upsert_role("acme", "human", "ic_engineer", base_authority=3.0)
    ingest = upsert_role("acme", "system", "ingest", base_authority=2.0)
    watcher = upsert_role("acme", "agent", "watcher", base_authority=1.0)

    # Security: security_lead 5.0 > engineering_manager 4.0 > ic 2.5.
    upsert_policy("acme", "security", sec, 5.0)
    upsert_policy("acme", "security", eng, 4.0)
    upsert_policy("acme", "security", ic, 2.5)

    # Engineering: engineering_manager 5.0 > security_lead 3.5 > ic 3.0.
    upsert_policy("acme", "engineering", eng, 5.0)
    upsert_policy("acme", "engineering", sec, 3.5)
    upsert_policy("acme", "engineering", ic, 3.0)

    # General: both leads tie at 3.0 -> conflicts get surfaced.
    upsert_policy("acme", "general", sec, 3.0)
    upsert_policy("acme", "general", eng, 3.0)
    upsert_policy("acme", "general", ic, 2.0)

    # System / agent baselines so they don't accidentally tie with humans.
    upsert_policy("acme", "*", ingest, 2.0)
    upsert_policy("acme", "*", watcher, 1.0)
