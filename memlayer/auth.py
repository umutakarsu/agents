"""HTTP-side identity for memscope (and the MCP server's HTTP transport).

The retrieval-side ACL pre-filter is correct as-is: it trusts the
``principals`` list it is handed. The problem this module closes is that
the HTTP boundary used to hand it whatever the *caller* claimed. Now the
caller proves who they are with a bearer token; we look up the user row
and hand retrieval the principals the database says they own.

Tokens never round-trip back out: we store ``sha256(token)`` and compare
hashes. ``create_user`` returns the plaintext exactly once -- the caller
(usually ``scripts/create_user.py``) prints it; lose it and you rotate by
issuing a new token.

Why sha256 and not bcrypt/argon2? Bearer tokens here are high-entropy
random strings (32 bytes from ``secrets.token_urlsafe``), not user-chosen
passwords. A slow KDF defends against brute force of low-entropy inputs;
that threat doesn't apply here, and bcrypt/argon2 would add a per-request
hashing cost on every authenticated call. sha256 is the right primitive
for this shape of secret -- same as how API gateways store API keys.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from memlayer.db import connect

# 32 bytes -> 43 base64 chars after token_urlsafe. ~256 bits of entropy;
# brute force is infeasible regardless of how the hash is stored.
_TOKEN_BYTES = 32

_VALID_SOURCE_TYPES = frozenset({"human", "system", "agent"})


@dataclass
class User:
    """The authenticated caller. The fields here are what we ARE willing to
    trust about an HTTP request -- distinct from anything the client claimed
    in the URL or body."""

    id: int
    email: str
    principals: list[str]
    workspaces: list[str]
    source_type: str


def _hash_token(token: str) -> str:
    """Single round of sha256 over the utf-8 bytes. Tokens are high-entropy
    by construction (see module docstring), so a slow KDF would add cost
    without changing the security model."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_user(
    email: str,
    principals: list[str],
    workspaces: list[str],
    source_type: str = "human",
) -> tuple[int, str]:
    """Generate a fresh bearer token, store ONLY its hash, return
    ``(user_id, plaintext_token)``. The plaintext is shown to the operator
    exactly once -- it cannot be recovered later because we never stored it.

    ``principals`` is the set the user is *authorized to claim* (e.g.
    ``["group:exec", "user:ali"]``). ``workspaces`` is the set of tenant
    workspaces they may touch. ``source_type`` flows into writeback so an
    agent service-account can't masquerade as a human via the HTTP API.
    """
    if source_type not in _VALID_SOURCE_TYPES:
        raise ValueError(
            f"source_type must be one of {sorted(_VALID_SOURCE_TYPES)}, "
            f"got {source_type!r}"
        )
    if not email:
        raise ValueError("email is required")
    if not principals:
        raise ValueError("principals must be non-empty")
    if not workspaces:
        raise ValueError("workspaces must be non-empty")

    token = secrets.token_urlsafe(_TOKEN_BYTES)
    token_hash = _hash_token(token)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (email, token_hash, principals, workspaces, source_type)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (email, token_hash, principals, workspaces, source_type),
        )
        user_id = cur.fetchone()[0]
        conn.commit()
    return user_id, token


def authenticate(token: str) -> User | None:
    """Look up the user by ``sha256(token)``. Returns ``None`` for unknown
    tokens; the caller is responsible for turning that into a 401. We do
    NOT log the token (or its hash) -- a leak in our logs would be just as
    bad as a leak in the database."""
    if not token:
        return None
    token_hash = _hash_token(token)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, email, principals, workspaces, source_type
            FROM users
            WHERE token_hash = %s
            """,
            (token_hash,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return User(
            id=row[0],
            email=row[1],
            principals=list(row[2]),
            workspaces=list(row[3]),
            source_type=row[4],
        )


__all__ = ["User", "create_user", "authenticate"]
