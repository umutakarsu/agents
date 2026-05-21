"""Boot memlayer's MCP server.

    python scripts/serve_mcp.py                 # stdio (default; Claude Code, Cursor)
    python scripts/serve_mcp.py --http 3111     # Streamable HTTP on port 3111

Stdio is the canonical transport for local MCP clients -- they spawn this
process and speak JSON-RPC over our stdin/stdout. ALL diagnostic output
must therefore go to stderr; a stray print() on stdout would corrupt the
protocol stream and the client would disconnect.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from memlayer.mcp_server import (
    SERVER_NAME,
    SERVER_VERSION,
    TOOLS,
    serve_http,
    serve_stdio,
)


def main() -> None:
    load_dotenv()  # picks up DATABASE_URL etc.

    ap = argparse.ArgumentParser(
        description="Run the memlayer MCP server (stdio or Streamable HTTP)."
    )
    ap.add_argument(
        "--http",
        type=int,
        metavar="PORT",
        help="Serve over Streamable HTTP on this port instead of stdio.",
    )
    ap.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address for --http (default 127.0.0.1).",
    )
    args = ap.parse_args()

    tool_names = ", ".join(t.name for t in TOOLS)
    transport = f"http://{args.host}:{args.http}/mcp" if args.http else "stdio"
    print(
        f"[{SERVER_NAME} v{SERVER_VERSION}] transport={transport} "
        f"tools={tool_names}",
        file=sys.stderr,
        flush=True,
    )

    try:
        if args.http is not None:
            asyncio.run(serve_http(host=args.host, port=args.http))
        else:
            asyncio.run(serve_stdio())
    except KeyboardInterrupt:
        print(f"[{SERVER_NAME}] shutting down", file=sys.stderr)


if __name__ == "__main__":
    main()
