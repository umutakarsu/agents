"""Render the memscope page as a static SVG (server-side replica of what
the browser would draw). Sole purpose: produce a PNG screenshot in
environments where a headless browser isn't available.

Hits the live API on localhost:8000, lays out the same dark UI shell + DAG
graph that app.js renders, and writes screenshot.svg / .png next to itself.
"""

import html
import json
import subprocess
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:8000"
OUT_DIR = Path(__file__).parent / "screenshots"
OUT_DIR.mkdir(exist_ok=True)


def fetch(path: str) -> dict:
    with urllib.request.urlopen(API + path) as r:
        return json.load(r)


# ---- shared styling ----

CSS = """
.bg { fill: #0f1115; }
.panel { fill: #11151a; stroke: #1f2329; stroke-width: 1; }
.divider { stroke: #1f2329; stroke-width: 1; }
.title { fill: #e6e9ec; font-family: -apple-system, "Segoe UI", Inter, sans-serif; }
.subtitle { fill: #888; font-family: -apple-system, "Segoe UI", Inter, sans-serif; }
.label { fill: #9aa1a8; font-family: -apple-system, "Segoe UI", sans-serif; font-size: 12px; }
.value { fill: #d6dadf; font-family: -apple-system, "Segoe UI", sans-serif; font-size: 13px; }
.selectbox { fill: #161a20; stroke: #2a2f37; stroke-width: 1; }
.btn { fill: #2b6cb0; }
.btn-text { fill: #fff; font-family: -apple-system, "Segoe UI", sans-serif; font-size: 13px; font-weight: 500; }
.mono { font-family: ui-monospace, "DejaVu Sans Mono", "SF Mono", monospace; }
.node-rect { fill: #1a1f26; stroke-width: 1.5; }
.node-human   { stroke: #4c9a7a; }
.node-system  { stroke: #b88a37; }
.node-agent   { stroke: #6e7c8a; }
.node-current { stroke: #f0a83a; stroke-width: 2.5; }
.node-superseded { opacity: 0.55; }
.node-title { font-family: ui-monospace, "DejaVu Sans Mono", monospace; font-size: 13px; font-weight: 600; fill: #e6e9ec; }
.node-body  { font-family: ui-monospace, "DejaVu Sans Mono", monospace; font-size: 12px; fill: #c5cad0; }
.node-meta  { font-family: ui-monospace, "DejaVu Sans Mono", monospace; font-size: 11px; fill: #8b919a; letter-spacing: 0.5px; }
.edge { stroke: #888; stroke-width: 1.5; fill: none; stroke-dasharray: 4 3; }
.detail-key   { fill: #8b919a; font-family: -apple-system, sans-serif; font-size: 12px; }
.detail-val   { fill: #d6dadf; font-family: -apple-system, sans-serif; font-size: 12px; }
.detail-quote-bg { fill: #161a20; }
.detail-quote-bar { fill: #2b6cb0; }
.detail-quote-text { fill: #e6e9ec; font-family: ui-monospace, "DejaVu Sans Mono", monospace; font-size: 12px; }
.legend-text { fill: #9aa1a8; font-family: -apple-system, sans-serif; font-size: 12px; }
"""


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def render(workspace: str, entity_key: str, dag: dict) -> str:
    W, H = 1200, 760
    parts: list[str] = []

    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'width="{W}" height="{H}">'
    )
    parts.append(f"<style>{CSS}</style>")
    parts.append(f'<rect class="bg" width="{W}" height="{H}" />')

    # ---- header ----
    parts.append(
        f'<line class="divider" x1="0" y1="64" x2="{W}" y2="64" />'
    )
    parts.append(
        '<text class="title" x="28" y="36" font-size="22" font-weight="600">memscope</text>'
    )
    parts.append(
        '<text class="subtitle" x="200" y="36" font-size="14">'
        "memory DAG inspector — pick a workspace + entity, watch conflicts resolve</text>"
    )

    # ---- controls strip ----
    y0 = 96
    parts.append(f'<text class="label" x="28" y="{y0}">workspace</text>')
    parts.append(
        f'<rect class="selectbox" x="28" y="{y0 + 8}" width="240" height="34" rx="5" />'
    )
    parts.append(
        f'<text class="value" x="40" y="{y0 + 30}">{workspace}</text>'
    )
    parts.append(
        f'<text fill="#888" x="252" y="{y0 + 30}" font-size="11">▾</text>'
    )

    parts.append(f'<text class="label" x="288" y="{y0}">entity_key</text>')
    parts.append(
        f'<rect class="selectbox" x="288" y="{y0 + 8}" width="280" height="34" rx="5" />'
    )
    label = f"{entity_key} ({len(dag['nodes'])} versions)"
    parts.append(
        f'<text class="value" x="300" y="{y0 + 30}">{html.escape(label)}</text>'
    )
    parts.append(
        f'<text fill="#888" x="552" y="{y0 + 30}" font-size="11">▾</text>'
    )

    parts.append(
        f'<rect class="btn" x="588" y="{y0 + 8}" width="80" height="34" rx="5" />'
    )
    parts.append(
        f'<text class="btn-text" x="608" y="{y0 + 30}">reload</text>'
    )

    # ---- legend ----
    ly = 168
    legend_items = [
        ("human",   "#4c9a7a", False),
        ("system",  "#b88a37", False),
        ("agent",   "#6e7c8a", False),
        ("current (no outgoing supersede edge)", "#f0a83a", True),
    ]
    lx = 28
    for text, color, ring in legend_items:
        if ring:
            parts.append(
                f'<circle cx="{lx + 5}" cy="{ly - 4}" r="5" '
                f'fill="none" stroke="{color}" stroke-width="2" />'
            )
        else:
            parts.append(
                f'<circle cx="{lx + 5}" cy="{ly - 4}" r="5" fill="{color}" />'
            )
        parts.append(
            f'<text class="legend-text" x="{lx + 16}" y="{ly}">{text}</text>'
        )
        lx += 16 + len(text) * 6.5 + 22

    # ---- DAG layout ----
    NW, NH, GAP = 360, 78, 36
    pad_left = 28
    pad_top = 200
    pos = {}
    for i, n in enumerate(dag["nodes"]):
        pos[n["id"]] = (pad_left, pad_top + i * (NH + GAP))

    # arrow marker
    parts.append(
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#888" /></marker></defs>'
    )

    # edges first (so nodes overlap them where they meet)
    for e in dag["edges"]:
        a = pos.get(e["from"])
        b = pos.get(e["to"])
        if not a or not b:
            continue
        ax, ay = a[0] + NW, a[1] + NH / 2
        bx, by = b[0] + NW, b[1] + NH / 2
        cx = max(ax, bx) + 60
        parts.append(
            f'<path class="edge" d="M {ax} {ay} C {cx} {ay}, {cx} {by}, {bx} {by}" '
            f'marker-end="url(#arrow)" />'
        )

    # nodes
    for n in dag["nodes"]:
        x, y = pos[n["id"]]
        src_class = f"node-{n['source_type']}"
        cur_class = "node-current" if n["is_current"] else "node-superseded"
        rect_classes = f"node-rect {src_class}{' node-current' if n['is_current'] else ''}"
        group_class = "" if n["is_current"] else "node-superseded"
        parts.append(f'<g class="{group_class}">')
        parts.append(
            f'<rect class="{rect_classes}" x="{x}" y="{y}" '
            f'width="{NW}" height="{NH}" rx="8" ry="8" />'
        )
        parts.append(
            f'<text class="node-title" x="{x + 12}" y="{y + 22}">'
            f'#{n["id"]} {html.escape(n["source_type"])}:{html.escape(clip(n["source_id"], 22))} '
            f'(c={n["confidence"]})</text>'
        )
        parts.append(
            f'<text class="node-body" x="{x + 12}" y="{y + 44}">'
            f'{html.escape(clip(n["content"], 56))}</text>'
        )
        status = "CURRENT" if n["is_current"] else "superseded"
        parts.append(
            f'<text class="node-meta" x="{x + 12}" y="{y + 64}">{status}</text>'
        )
        parts.append("</g>")

    # ---- detail panel (right side) ----
    selected = next((n for n in dag["nodes"] if n["is_current"]), dag["nodes"][0])
    dx, dy, dw, dh = 800, 200, 372, 480
    parts.append(
        f'<rect class="panel" x="{dx}" y="{dy}" width="{dw}" height="{dh}" rx="8" />'
    )
    parts.append(
        f'<text x="{dx + 16}" y="{dy + 30}" '
        f'font-family="ui-monospace, DejaVu Sans Mono, monospace" '
        f'font-size="15" font-weight="600" fill="#e6e9ec">'
        f'#{selected["id"]}</text>'
    )
    rows = [
        ("source", f"{selected['source_type']}:{selected['source_id']}"),
        ("confidence", f"{selected['confidence']}"),
        ("created", selected["created_at"]),
        ("status", "CURRENT" if selected["is_current"] else "superseded"),
    ]
    ry = dy + 60
    for k, v in rows:
        parts.append(f'<text class="detail-key" x="{dx + 16}" y="{ry}">{k}</text>')
        parts.append(
            f'<text class="detail-val" x="{dx + dw - 16}" y="{ry}" '
            f'text-anchor="end">{html.escape(v)}</text>'
        )
        ry += 22

    # quote block
    qy = ry + 14
    qh = 160
    parts.append(
        f'<rect class="detail-quote-bg" x="{dx + 16}" y="{qy}" '
        f'width="{dw - 32}" height="{qh}" rx="4" />'
    )
    parts.append(
        f'<rect class="detail-quote-bar" x="{dx + 16}" y="{qy}" width="3" height="{qh}" />'
    )
    # wrap content
    content = selected["content"]
    line_chars = 38
    lines = []
    rest = content
    while rest and len(lines) < 7:
        if len(rest) <= line_chars:
            lines.append(rest)
            break
        cut = rest.rfind(" ", 0, line_chars)
        if cut == -1:
            cut = line_chars
        lines.append(rest[:cut])
        rest = rest[cut + 1:]
    if rest and len(lines) >= 7:
        lines[-1] = lines[-1][:line_chars - 1] + "…"
    for i, ln in enumerate(lines):
        parts.append(
            f'<text class="detail-quote-text" x="{dx + 28}" y="{qy + 22 + i * 18}">'
            f'{html.escape(ln)}</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


def main():
    workspaces = fetch("/api/workspaces")["workspaces"]
    print(f"workspaces: {workspaces}")
    for ws in workspaces:
        entities = fetch(f"/api/entities?workspace={ws}")["entities"]
        for ent in entities:
            ek = ent["entity_key"]
            dag = fetch(f"/api/memory/{ws}/{ek}")
            if not dag["nodes"]:
                continue
            svg = render(ws, ek, dag)
            stem = f"{ws}__{ek.replace(':', '_')}"
            svg_path = OUT_DIR / f"{stem}.svg"
            png_path = OUT_DIR / f"{stem}.png"
            svg_path.write_text(svg)
            subprocess.run(
                ["rsvg-convert", "-o", str(png_path), "-z", "2", str(svg_path)],
                check=True,
            )
            print(f"  wrote {png_path} ({len(dag['nodes'])} nodes, {len(dag['edges'])} edges)")


if __name__ == "__main__":
    main()
