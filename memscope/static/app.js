/* memscope -- Alpine.js root component.
   Handles three views (DAG, Search, Pipeline) plus a small set of cross-cutting
   pieces (workspace list, error fallbacks, story-mode scenarios).

   The SVG in the DAG view is rendered as a string into x-html; node clicks
   call back through a window-level reference (memscope_app) because Alpine
   event bindings inside x-html'd content do not reach the component scope. */

function memscope() {
  return {
    // ---------- shared ----------
    tab: 'dag',
    wsList: [],

    // ---------- landing / story mode ----------
    // The demo opens on a landing page with three big scenarios; the tabs
    // are only revealed once a scenario has been run (or the user clicks
    // a "jump straight to view" link). This keeps strangers from staring
    // at empty panels.
    landingMode: true,

    // Per-view info banners (dismissible).
    dagBannerDismissed: false,
    searchBannerDismissed: false,
    pipelineBannerDismissed: false,

    // Per-view scenario narrative banners (only on after a scenario fires).
    dagStoryActive: false,
    searchStoryActive: false,
    searchStoryStage: 1,   // 1 = asked as group:all, 2 = asked as group:exec
    pipelineStoryActive: false,

    // Help modal
    helpOpen: false,
    helpView: 'dag',

    // ---------- DAG view ----------
    workspace: '',
    entityList: [],
    entityKey: '',
    dag: null,
    selected: null,
    dagError: '',

    // ---------- Search view ----------
    searchWorkspace: '',
    searchQuery: '',
    searchPrincipals: 'group:all',
    searchK: 10,
    searchHits: [],
    searchRan: false,
    searchLoading: false,
    searchError: '',

    // ---------- Pipeline view ----------
    pipelineWorkspace: '',
    stats: null,
    pipelineError: '',
    ingestLoading: false,
    ingestResult: null,

    // =====================================================
    // init: pull workspace list once, share across all views
    // =====================================================
    async init() {
      window.memscope_app = this;  // expose for SVG onclick callbacks
      try {
        const r = await fetch('/api/workspaces').then(this._json);
        this.wsList = r.workspaces || [];
      } catch (e) {
        // Render in the dag-view banner (the user is most likely there on load).
        this.dagError = `loading workspaces: ${e.message}`;
      }
    },

    // =====================================================
    // Landing / scenario plumbing
    // =====================================================
    showLanding() {
      this.landingMode = true;
      // Close any open help modal.
      this.helpOpen = false;
    },

    jumpToTab(name) {
      this.tab = name;
      this.landingMode = false;
    },

    openHelp(view) {
      this.helpView = view;
      this.helpOpen = true;
    },

    armTip(arm) {
      const tips = {
        vector: 'Vector arm: dense-embedding similarity search (pgvector).',
        fts: 'Full-text arm: Postgres tsvector / GIN keyword search.',
        memory: 'Memory arm: distilled memory rows (separate from raw chunks).'
      };
      return tips[arm] || arm;
    },

    // ----- Scenario A: human correction wins (DAG, acme, person:ali) -----
    async runScenarioA() {
      this.tab = 'dag';
      this.workspace = 'acme';
      // loadEntities clears entityKey & dag; we then set entityKey and load.
      await this.loadEntities();
      this.entityKey = 'person:ali';
      await this.loadDag();
      this.dagStoryActive = true;
      this.dagBannerDismissed = true;  // story banner replaces the generic info banner
      this.landingMode = false;
    },

    // ----- Scenario B: same query, different answer (Search, acme) -----
    async runScenarioB() {
      this.tab = 'search';
      this.searchWorkspace = 'acme';
      this.searchQuery = 'secret roadmap acquire competitor';
      this.searchPrincipals = 'group:all';
      this.searchStoryActive = true;
      this.searchStoryStage = 1;
      this.searchBannerDismissed = true;
      this.landingMode = false;
      await this.runSearch();
    },

    // Second stage of scenario B -- re-run as group:exec.
    async runScenarioBStage2() {
      this.searchPrincipals = 'group:exec';
      this.searchStoryStage = 2;
      await this.runSearch();
    },

    // ----- Scenario C: refuse to pay twice (Pipeline, acme, sample_docs) -----
    async runScenarioC() {
      this.tab = 'pipeline';
      this.pipelineWorkspace = 'acme';
      this.pipelineStoryActive = true;
      this.pipelineBannerDismissed = true;
      this.landingMode = false;
      // Refresh stats first so the page isn't blank while ingest runs.
      await this.loadStats();
      await this.ingest('sample_docs', ['group:all']);
      // Scroll the result card into view so the dedup gate is the focal point.
      this.$nextTick(() => {
        const el = document.getElementById('reingest-panel');
        if (el && el.scrollIntoView) {
          el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      });
    },

    // =====================================================
    // DAG view actions
    // =====================================================
    async loadEntities() {
      this.entityKey = '';
      this.entityList = [];
      this.dag = null;
      this.selected = null;
      this.dagError = '';
      if (!this.workspace) return;
      try {
        const r = await fetch(`/api/entities?workspace=${encodeURIComponent(this.workspace)}`)
          .then(this._json);
        this.entityList = r.entities || [];
      } catch (e) {
        this.dagError = `loading entities: ${e.message}`;
      }
    },

    async loadDag() {
      this.selected = null;
      this.dagError = '';
      if (!this.workspace || !this.entityKey) return;
      try {
        const r = await fetch(
          `/api/memory/${encodeURIComponent(this.workspace)}/${encodeURIComponent(this.entityKey)}`
        ).then(this._json);
        this.dag = r;
      } catch (e) {
        this.dagError = `loading DAG: ${e.message}`;
      }
    },

    select(id) {
      if (!this.dag) return;
      this.selected = this.dag.nodes.find(n => n.id === id) ?? null;
    },

    // =====================================================
    // Search view actions
    // =====================================================
    get searchPrincipalsList() {
      return this.searchPrincipals
        .split(',')
        .map(s => s.trim())
        .filter(Boolean);
    },

    setExample(asGroup) {
      this.searchQuery = 'secret roadmap acquire competitor';
      this.searchPrincipals = asGroup;
      // Default a workspace if the user already loaded one or hasn't picked.
      if (!this.searchWorkspace && this.wsList.length > 0) {
        this.searchWorkspace = this.wsList[0];
      }
    },

    async runSearch() {
      this.searchError = '';
      if (!this.searchWorkspace || !this.searchQuery) return;
      this.searchLoading = true;
      this.searchRan = true;
      this.searchHits = [];
      const principalsCsv = this.searchPrincipalsList.join(',');
      const url = `/api/search?workspace=${encodeURIComponent(this.searchWorkspace)}`
        + `&q=${encodeURIComponent(this.searchQuery)}`
        + `&principals=${encodeURIComponent(principalsCsv)}`
        + `&k=${encodeURIComponent(this.searchK)}`;
      try {
        const r = await fetch(url).then(this._json);
        this.searchHits = r.hits || [];
      } catch (e) {
        this.searchError = `search failed: ${e.message} (API may not be available yet)`;
      } finally {
        this.searchLoading = false;
      }
    },

    // =====================================================
    // Pipeline view actions
    // =====================================================
    async loadStats() {
      this.pipelineError = '';
      this.stats = null;
      if (!this.pipelineWorkspace) return;
      try {
        const r = await fetch(
          `/api/pipeline/stats?workspace=${encodeURIComponent(this.pipelineWorkspace)}`
        ).then(this._json);
        this.stats = r;
      } catch (e) {
        this.pipelineError = `loading stats: ${e.message} (API may not be available yet)`;
      }
    },

    async ingest(dir, allowedPrincipals) {
      this.pipelineError = '';
      if (!this.pipelineWorkspace) return;
      this.ingestLoading = true;
      this.ingestResult = null;
      try {
        const r = await fetch('/api/pipeline/ingest', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            workspace: this.pipelineWorkspace,
            dir,
            allowed_principals: allowedPrincipals
          })
        }).then(this._json);
        // Attach a target label so the result card can label what just ran.
        r._target = dir;
        this.ingestResult = r;
        // Refresh stats so the counters reflect the new state.
        await this.loadStats();
      } catch (e) {
        this.pipelineError = `ingest failed: ${e.message} (API may not be available yet)`;
      } finally {
        this.ingestLoading = false;
      }
    },

    // =====================================================
    // Helpers
    // =====================================================
    async _json(r) {
      if (!r.ok) {
        // Try to surface a server-provided error message if available.
        let detail = '';
        try {
          const body = await r.json();
          if (body && body.detail) detail = ` -- ${body.detail}`;
        } catch (_) { /* not JSON, ignore */ }
        throw new Error(`${r.status} ${r.statusText}${detail}`);
      }
      return r.json();
    },

    // =====================================================
    // SVG renderer for the memory DAG
    // =====================================================
    renderSvg() {
      const dag = this.dag;
      if (!dag || dag.nodes.length === 0) return '';

      const NW = 380, NH = 96, GAP = 40, PAD = 24, EDGE_LANE = 96;
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

      // Nodes: layered headers with mono ID + sans badges, then mono content.
      const nodeSvg = dag.nodes.map(n => {
        const { x, y } = pos[n.id];
        const cls = `node ${n.source_type}${n.is_current ? ' current' : ' superseded'}`;
        // Badge layout (left to right): #id (mono) -- SOURCE chip -- confidence chip -- status
        // We draw everything as <text>/<rect> -- no <foreignObject> to keep export simple.
        const idText = `#${n.id}`;
        const srcText = n.source_type.toUpperCase();
        // Display confidence as a percentage in plain language.
        const confPct = Math.round((n.confidence || 0) * 100);
        const confText = `${confPct}%`;

        // Rough widths so chips don't overlap. Mono ID is left-aligned.
        const padInner = 14;
        const idX = x + padInner;
        const srcChipX = idX + 38;
        const srcChipW = 8 + srcText.length * 7.2;
        const confChipX = srcChipX + srcChipW + 8;
        const confChipW = 8 + confText.length * 7.2 + 6;  // a hair wider for the % glyph

        const statusText = n.is_current ? 'CURRENT' : 'corrected by';
        const statusX = x + NW - padInner;

        return `
          <g class="${cls}" onclick="memscope_app.select(${n.id})">
            <rect class="node-bg" x="${x}" y="${y}" width="${NW}" height="${NH}" rx="10" ry="10" />
            <rect class="node-stripe" x="${x}" y="${y}" width="4" height="${NH}" rx="2" ry="2" />

            <text x="${idX}" y="${y + 22}" class="id">${esc(idText)}</text>

            <rect class="chip-bg src-${n.source_type}" x="${srcChipX}" y="${y + 10}"
                  width="${srcChipW}" height="18" rx="9" ry="9" />
            <text x="${srcChipX + srcChipW / 2}" y="${y + 23}" class="chip-tx">${esc(srcText)}</text>

            <rect class="chip-bg chip-conf" x="${confChipX}" y="${y + 10}"
                  width="${confChipW}" height="18" rx="9" ry="9" />
            <text x="${confChipX + confChipW / 2}" y="${y + 23}" class="chip-tx mono-tx">${esc(confText)}</text>

            <text x="${statusX}" y="${y + 22}" class="status status-${n.is_current ? 'current' : 'superseded'}">${esc(statusText)}</text>

            <text x="${idX}" y="${y + 52}" class="content">${esc(clip(n.content, 58))}</text>
            <text x="${idX}" y="${y + 78}" class="src-id">source <tspan class="src-id-val">${esc(clip(n.source_id, 36))}</tspan></text>
          </g>`;
      }).join('');

      // Edges: solid, thicker, with a small "corrects" label near the curve midpoint.
      const edgeSvg = dag.edges.map(e => {
        const a = pos[e.from], b = pos[e.to];
        if (!a || !b) return '';
        const ax = a.x + NW, ay = a.y + NH / 2;
        const bx = b.x + NW, by = b.y + NH / 2;
        const cx = Math.max(ax, bx) + EDGE_LANE - 22;
        // Midpoint of a cubic Bezier with the chosen control points is roughly:
        const midX = (ax + bx) * 0.125 + cx * 0.75;
        const midY = (ay + by) / 2;
        return `
          <path class="edge"
                d="M ${ax} ${ay} C ${cx} ${ay}, ${cx} ${by}, ${bx} ${by}"
                marker-end="url(#arrow)" />
          <g class="edge-label">
            <rect x="${midX - 36}" y="${midY - 9}" width="72" height="18" rx="9" ry="9" />
            <text x="${midX}" y="${midY + 4}">corrects</text>
          </g>`;
      }).join('');

      return `
        <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}"
             xmlns="http://www.w3.org/2000/svg">
          <defs>
            <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
                    markerWidth="7" markerHeight="7" orient="auto">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#9aa3b2" />
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
