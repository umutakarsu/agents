"""MCP server: exposes memlayer's core operations as MCP tools so any
MCP-speaking client (Claude Code, Cursor, OpenCode, ...) can read and
write to the memory layer.

Five tools, each a thin adapter over an existing memlayer function. No
business logic lives here -- this module's only job is to translate JSON
arguments into the right Python call and shape the result back.

Trust model
===========
Two transports, two threat models:

* **stdio** (the canonical MCP transport for local clients) -- the client
  spawns this process and speaks JSON-RPC over our stdin/stdout. There is
  no network attacker between the client and us; the OS already
  authenticated the local user. The client passes ``principals`` as a tool
  argument (MCP-native pattern), and we trust it the same way a CLI
  trusts its argv. If you don't trust the client to ask honestly, don't
  let it spawn the process.

* **Streamable HTTP** -- once we listen on a socket, ANY caller with
  network reach can hit us. We require a bearer token (``MEMSCOPE_TOKEN``
  env var, or the client sends ``Authorization: Bearer <t>``) and
  reject anything else. This is the same identity layer memscope's HTTP
  API uses, intentionally -- ``scripts/create_user.py`` mints tokens that
  work for both. The token's source_type also OVERRIDES whatever
  ``source_type`` an HTTP ``remember`` caller asks for, closing the audit
  P1: an agent service-account can't claim ``source_type="human"`` to
  outrank a real human via the supersede ladder.
"""

from __future__ import annotations

import json
import os
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import mcp.types as mcp_types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from memlayer.auth import User, authenticate
from memlayer.ingest import SourceItem, ingest
from memlayer.retrieval import search
from memlayer.writeback import history, recall, remember

SERVER_NAME = "memlayer"
SERVER_VERSION = "0.1.0"

# Hard cap on `k` to keep result payloads bounded regardless of caller input.
MAX_K = 100

# Per-request authenticated user (HTTP transport only). The stdio transport
# leaves this None and trusts tool arguments -- see module docstring.
_current_user: ContextVar[User | None] = ContextVar(
    "memlayer_mcp_user", default=None
)


# ---------------------------------------------------------------------------
# Tool definitions (explicit JSON schemas, not auto-derived).
#
# Keeping the schemas explicit (rather than letting Pydantic/FastMCP infer
# them) means the wire contract is reviewable in one place and unaffected by
# refactors to the underlying memlayer function signatures.
# ---------------------------------------------------------------------------

TOOLS: list[mcp_types.Tool] = [
    mcp_types.Tool(
        name="search_memory",
        description=(
            "Search a workspace's memory + chunks with a hybrid retrieval "
            "system (vector + full-text). ACL pre-filter applied in SQL -- "
            "only returns content the caller's principals can see."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "query": {"type": "string"},
                "principals": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "e.g. ['user:ali', 'group:exec']",
                },
                "k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_K,
                    "default": 10,
                },
            },
            "required": ["workspace", "query", "principals"],
        },
    ),
    mcp_types.Tool(
        name="remember",
        description=(
            "Write a memory row about an entity. Append-only with provenance "
            "and conflict resolution: human > system > agent. A low-confidence "
            "row never overwrites a higher-confidence one with greater source "
            "precedence."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "entity_key": {"type": "string"},
                "content": {"type": "string"},
                "source_type": {
                    "type": "string",
                    "enum": ["human", "system", "agent"],
                },
                "source_id": {
                    "type": "string",
                    "description": "e.g. 'manager', 'email-scanner'",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "allowed_principals": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["group:all"],
                },
            },
            "required": [
                "workspace",
                "entity_key",
                "content",
                "source_type",
                "source_id",
                "confidence",
            ],
        },
    ),
    mcp_types.Tool(
        name="recall",
        description=(
            "Get the current (winning) memory row for a workspace + entity. "
            "Returns null if no rows match the caller's principals."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "entity_key": {"type": "string"},
                "principals": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["workspace", "entity_key", "principals"],
        },
    ),
    mcp_types.Tool(
        name="history",
        description=(
            "Get the full audit trail of memory rows for an entity (all "
            "versions, including superseded). Filtered by the caller's "
            "principals."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "entity_key": {"type": "string"},
                "principals": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["workspace", "entity_key", "principals"],
        },
    ),
    mcp_types.Tool(
        name="ingest_text",
        description=(
            "Ingest one or more text documents into a workspace's memory "
            "layer. Each document becomes chunks; identical content reused "
            "(content-addressed dedup)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "source_id": {"type": "string"},
                            "text": {"type": "string"},
                            "allowed_principals": {
                                "type": "array",
                                "items": {"type": "string"},
                                "default": ["group:all"],
                            },
                        },
                        "required": ["source", "source_id", "text"],
                    },
                },
            },
            "required": ["workspace", "items"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Tool implementations: pure adapters. Business logic stays in memlayer.*.
# ---------------------------------------------------------------------------


def _hit_to_dict(hit) -> dict[str, Any]:
    """Convert a retrieval.Hit dataclass to a JSON-serializable dict.
    `arms` is a tuple in the dataclass; JSON has no tuple type, list it."""
    d = asdict(hit)
    d["arms"] = list(hit.arms)
    # provenance and confidence are already None for chunks; leave shape.
    return d


def _do_search_memory(args: dict[str, Any]) -> list[dict[str, Any]]:
    workspace = args["workspace"]
    query = args["query"]
    principals = list(args["principals"])
    k = min(int(args.get("k", 10)), MAX_K)
    hits = search(query, workspace=workspace, principals=principals, k=k)
    return [_hit_to_dict(h) for h in hits]


def _do_remember(args: dict[str, Any]) -> dict[str, Any]:
    # HTTP transport: if an authenticated user is in scope, their
    # source_type wins over whatever the caller asked for. This closes the
    # audit P1: an agent service-account can't claim source_type='human'
    # via the wire to outrank a real human in the supersede ladder.
    # Stdio transport leaves _current_user=None; the local client's arg is
    # honoured because it isn't a network attacker (see module docstring).
    user = _current_user.get()
    source_type = user.source_type if user is not None else args["source_type"]
    new_id, is_current = remember(
        workspace=args["workspace"],
        entity_key=args["entity_key"],
        content=args["content"],
        source_type=source_type,
        source_id=args["source_id"],
        confidence=float(args["confidence"]),
        allowed_principals=list(
            args.get("allowed_principals") or ["group:all"]
        ),
    )
    return {"memory_id": new_id, "is_current": is_current}


def _do_recall(args: dict[str, Any]) -> dict[str, Any] | None:
    row = recall(
        workspace=args["workspace"],
        entity_key=args["entity_key"],
        principals=list(args["principals"]),
    )
    if row is None:
        return None
    return {
        "id": row.id,
        "content": row.content,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "confidence": row.confidence,
        # MemoryRow doesn't currently carry created_at; we don't need a second
        # DB round-trip to fetch it here -- callers wanting timestamps can use
        # `history` which returns created_at for every row.
        "created_at": None,
    }


def _do_history(args: dict[str, Any]) -> list[dict[str, Any]]:
    rows = history(
        workspace=args["workspace"],
        entity_key=args["entity_key"],
        principals=list(args["principals"]),
    )
    out: list[dict[str, Any]] = []
    for mid, content, stype, sid, conf, superseded_by, created_at in rows:
        out.append(
            {
                "id": mid,
                "content": content,
                "source_type": stype,
                "source_id": sid,
                "confidence": conf,
                "superseded_by": superseded_by,
                "created_at": (
                    created_at.isoformat()
                    if isinstance(created_at, datetime)
                    else created_at
                ),
                "is_current": superseded_by is None,
            }
        )
    return out


def _do_ingest_text(args: dict[str, Any]) -> dict[str, Any]:
    workspace = args["workspace"]
    raw_items = args["items"]
    occurred_at = datetime.now(timezone.utc)
    source_items: list[SourceItem] = []
    for it in raw_items:
        source_items.append(
            SourceItem(
                source=it["source"],
                source_id=it["source_id"],
                text=it["text"],
                occurred_at=occurred_at,
                allowed_principals=list(
                    it.get("allowed_principals") or ["group:all"]
                ),
            )
        )
    stats = ingest(source_items, workspace)
    return {
        "items": stats.items,
        "chunks": stats.chunks,
        "embeddings_computed": stats.embeddings_computed,
        "embeddings_reused": stats.embeddings_reused,
    }


# Dispatch table keeps the call_tool handler trivial -- one lookup + call.
_DISPATCH = {
    "search_memory": _do_search_memory,
    "remember": _do_remember,
    "recall": _do_recall,
    "history": _do_history,
    "ingest_text": _do_ingest_text,
}


# ---------------------------------------------------------------------------
# Lowlevel Server wiring. Exported `server` so transports can `await
# server.run(...)`. Stdio + Streamable HTTP runners live below.
# ---------------------------------------------------------------------------

server: Server = Server(SERVER_NAME)


@server.list_tools()
async def _list_tools() -> list[mcp_types.Tool]:
    return TOOLS


@server.call_tool()
async def _call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[mcp_types.TextContent]:
    args = arguments or {}
    fn = _DISPATCH.get(name)
    if fn is None:
        # The framework also rejects unknown tools at the protocol layer,
        # but a clean error message helps when this is poked directly.
        return [
            mcp_types.TextContent(
                type="text", text=json.dumps({"error": f"unknown tool: {name}"})
            )
        ]
    # memlayer functions are synchronous (psycopg) -- calling them from the
    # event loop will block briefly. That's fine for an interactive memory
    # layer; if it ever isn't, wrap with anyio.to_thread.run_sync here.
    try:
        result = fn(args)
    except Exception as exc:
        return [
            mcp_types.TextContent(
                type="text",
                text=json.dumps({"error": str(exc), "type": type(exc).__name__}),
            )
        ]
    return [
        mcp_types.TextContent(
            type="text", text=json.dumps(result, default=str, indent=2)
        )
    ]


def _init_options() -> InitializationOptions:
    return InitializationOptions(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )


async def serve_stdio() -> None:
    """Run the MCP server over stdio. Canonical transport for local clients
    like Claude Code -- the client spawns this process and speaks JSON-RPC
    over our stdin/stdout."""
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, _init_options())


async def serve_http(host: str = "127.0.0.1", port: int = 3111) -> None:
    """Run the MCP server over Streamable HTTP. Useful for remote clients
    and for testing with curl. Single-session manager per process, as the
    mcp library mandates.

    Auth: every request must carry ``Authorization: Bearer <token>``. The
    token is the same shape memscope uses (issued by
    ``scripts/create_user.py``). The authenticated user is pinned to the
    request via a ContextVar so ``_do_remember`` can override source_type
    without changing the tool-call signature. ``MEMSCOPE_AUTH_DISABLED=1``
    bypasses this for local dev -- DO NOT set in production."""
    import contextlib

    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.types import Receive, Scope, Send

    session_manager = StreamableHTTPSessionManager(app=server, stateless=False)

    auth_disabled = os.environ.get("MEMSCOPE_AUTH_DISABLED", "").strip() in {
        "1",
        "true",
        "yes",
    }

    def _read_bearer(scope: Scope) -> str | None:
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                s = v.decode("latin-1")
                if s.startswith("Bearer "):
                    return s[len("Bearer "):]
        return None

    async def _send_401(send: Send, detail: str) -> None:
        body = json.dumps({"error": detail}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="memlayer"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def handle_streamable_http(
        scope: Scope, receive: Receive, send: Send
    ) -> None:
        if auth_disabled:
            await session_manager.handle_request(scope, receive, send)
            return
        token = _read_bearer(scope)
        if token is None:
            await _send_401(send, "Missing Authorization: Bearer <token>")
            return
        user = authenticate(token)
        if user is None:
            await _send_401(send, "Invalid token")
            return
        # Pin the user for the lifetime of this request; _do_remember reads
        # it to override source_type.
        tok = _current_user.set(user)
        try:
            await session_manager.handle_request(scope, receive, send)
        finally:
            _current_user.reset(tok)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with session_manager.run():
            yield

    app = Starlette(
        debug=False,
        routes=[Mount("/mcp", app=handle_streamable_http)],
        lifespan=lifespan,
    )

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    await uvicorn.Server(config).serve()


__all__ = [
    "server",
    "TOOLS",
    "serve_stdio",
    "serve_http",
    "SERVER_NAME",
    "SERVER_VERSION",
]
