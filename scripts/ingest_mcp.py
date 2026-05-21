"""Ingest text resources from an MCP server (Streamable HTTP transport).

    python scripts/ingest_mcp.py https://my-mcp.example/mcp \\
        --workspace acme --header "Authorization: Bearer $MY_TOKEN"
"""

import argparse

from memlayer.connectors.mcp import read_mcp_resources
from memlayer.ingest import ingest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("server_url")
    ap.add_argument("--workspace", default="default")
    ap.add_argument(
        "--principals",
        default="group:all",
        help="comma-separated, e.g. user:ali,group:eng",
    )
    ap.add_argument(
        "--header",
        action="append",
        default=[],
        help='repeatable. Format: "Name: value"',
    )
    args = ap.parse_args()

    headers: dict[str, str] = {}
    for h in args.header:
        name, _, value = h.partition(":")
        headers[name.strip()] = value.strip()

    items = read_mcp_resources(
        args.server_url,
        headers=headers,
        allowed_principals=args.principals.split(","),
    )
    stats = ingest(items, workspace=args.workspace)

    print(f"items:               {stats.items}")
    print(f"chunks:              {stats.chunks}")
    print(f"embeddings computed: {stats.embeddings_computed}  (cost)")
    print(f"embeddings reused:   {stats.embeddings_reused}  (dedup saved)")


if __name__ == "__main__":
    main()
