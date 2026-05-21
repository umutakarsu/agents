"""MCP connector: reads resources from an MCP server over Streamable HTTP /
JSON-RPC. Same shape as the other connectors -- walk the source, yield
SourceItems. Stdlib only; auth headers (e.g. Authorization) pass through.
Stdio transport is out of scope for this thin adapter."""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from itertools import count

from memlayer.ingest import SourceItem


def _rpc(
    url: str,
    method: str,
    params: dict | None,
    headers: dict[str, str],
    rid: int,
) -> dict:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read().decode("utf-8"))
    if "error" in payload:
        raise RuntimeError(f"MCP error on {method}: {payload['error']}")
    return payload.get("result", {})


def read_mcp_resources(
    server_url: str,
    headers: dict[str, str] | None = None,
    allowed_principals: list[str] | None = None,
) -> list[SourceItem]:
    headers = headers or {}
    rid = count(1)
    occurred_at = datetime.now(timezone.utc)
    items: list[SourceItem] = []

    cursor: str | None = None
    while True:
        params = {"cursor": cursor} if cursor else {}
        result = _rpc(server_url, "resources/list", params, headers, next(rid))
        for r in result.get("resources", []):
            uri = r["uri"]
            read_res = _rpc(
                server_url, "resources/read", {"uri": uri}, headers, next(rid)
            )
            for content in read_res.get("contents", []):
                text = content.get("text")
                if not text:
                    continue  # blob/binary resources are skipped
                items.append(
                    SourceItem(
                        source="mcp",
                        source_id=uri,
                        text=text,
                        occurred_at=occurred_at,
                        allowed_principals=allowed_principals or ["group:all"],
                        payload={
                            "uri": uri,
                            "name": r.get("name"),
                            "mimeType": r.get("mimeType")
                            or content.get("mimeType"),
                            "server": server_url,
                        },
                    )
                )
        cursor = result.get("nextCursor")
        if not cursor:
            break
    return items
