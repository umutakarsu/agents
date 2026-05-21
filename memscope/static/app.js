/* memscope -- Alpine.js component for the memory DAG view.
   The SVG is rendered as a string into x-html; node clicks call back through
   a window-level reference (memscope_app) which Alpine event bindings inside
   x-html'd content cannot reach directly. */

function memscope() {
  return {
    wsList: [],
    workspace: '',
    entityList: [],
    entityKey: '',
    dag: null,
    selected: null,
    error: '',

    async init() {
      window.memscope_app = this;  // expose for SVG onclick callbacks
      try {
        const r = await fetch('/api/workspaces').then(this._json);
        this.wsList = r.workspaces;
      } catch (e) {
        this.error = `loading workspaces: ${e.message}`;
      }
    },

    async loadEntities() {
      this.entityKey = '';
      this.entityList = [];
      this.dag = null;
      this.selected = null;
      if (!this.workspace) return;
      try {
        const r = await fetch(`/api/entities?workspace=${encodeURIComponent(this.workspace)}`)
          .then(this._json);
        this.entityList = r.entities;
      } catch (e) {
        this.error = `loading entities: ${e.message}`;
      }
    },

    async loadDag() {
      this.selected = null;
      this.error = '';
      if (!this.workspace || !this.entityKey) return;
      try {
        const r = await fetch(
          `/api/memory/${encodeURIComponent(this.workspace)}/${encodeURIComponent(this.entityKey)}`
        ).then(this._json);
        this.dag = r;
      } catch (e) {
        this.error = `loading DAG: ${e.message}`;
      }
    },

    select(id) {
      this.selected = this.dag.nodes.find(n => n.id === id) ?? null;
    },

    async _json(r) {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      return r.json();
    },

    renderSvg() {
      const dag = this.dag;
      if (!dag || dag.nodes.length === 0) return '';

      const NW = 360, NH = 78, GAP = 36, PAD = 20, EDGE_LANE = 80;
      const total = dag.nodes.length;
      const H = PAD * 2 + total * NH + (total - 1) * GAP;
      const W = PAD * 2 + NW + EDGE_LANE;

      // Vertical stack, oldest at top.
      const pos = {};
      dag.nodes.forEach((n, i) => {
        pos[n.id] = { x: PAD, y: PAD + i * (NH + GAP) };
      });

      const esc = s => String(s).replace(/[&<>"']/g,
        c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

      const clip = (s, n) => s.length > n ? s.slice(0, n - 1) + '…' : s;

      const nodeSvg = dag.nodes.map(n => {
        const { x, y } = pos[n.id];
        const cls = `node ${n.source_type}${n.is_current ? ' current' : ' superseded'}`;
        const status = n.is_current ? 'CURRENT' : 'superseded';
        return `
          <g class="${cls}" onclick="memscope_app.select(${n.id})">
            <rect x="${x}" y="${y}" width="${NW}" height="${NH}" rx="8" ry="8" />
            <text x="${x + 12}" y="${y + 20}" class="title">
              #${n.id} ${esc(n.source_type)}:${esc(clip(n.source_id, 22))} (c=${n.confidence})
            </text>
            <text x="${x + 12}" y="${y + 42}" class="body">${esc(clip(n.content, 56))}</text>
            <text x="${x + 12}" y="${y + 64}" class="meta">${status}</text>
          </g>`;
      }).join('');

      // Edges curve out to the right lane and into the target node's right side.
      const edgeSvg = dag.edges.map(e => {
        const a = pos[e.from], b = pos[e.to];
        if (!a || !b) return '';
        const ax = a.x + NW, ay = a.y + NH / 2;
        const bx = b.x + NW, by = b.y + NH / 2;
        const cx = Math.max(ax, bx) + EDGE_LANE - 20;
        return `<path class="edge"
                 d="M ${ax} ${ay} C ${cx} ${ay}, ${cx} ${by}, ${bx} ${by}"
                 marker-end="url(#arrow)" />`;
      }).join('');

      return `
        <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}"
             xmlns="http://www.w3.org/2000/svg">
          <defs>
            <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
                    markerWidth="6" markerHeight="6" orient="auto">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#888" />
            </marker>
          </defs>
          ${edgeSvg}
          ${nodeSvg}
        </svg>`;
    }
  };
}

document.addEventListener('alpine:init', () => {
  Alpine.data('memscope', memscope);
});
