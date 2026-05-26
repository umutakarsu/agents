/* memscope -- Alpine.js root component.
   Handles three views (DAG, Search, Pipeline) plus a small set of cross-cutting
   pieces (workspace list, error fallbacks, story-mode scenarios).

   The SVG in the DAG view is rendered as a string into x-html; node clicks
   call back through a window-level reference (memscope_app) because Alpine
   event bindings inside x-html'd content do not reach the component scope. */

// Bearer token storage key. We keep this out of the component state on
// purpose: localStorage is the source of truth so a page reload or another
// tab on the same origin picks up the token without re-prompting. The token
// is only ever sent in the Authorization header -- never logged, never put
// in a URL, never echoed back into the DOM.
const TOKEN_KEY = 'memscopeToken';

function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; }
  catch (_) { return ''; }
}

function setToken(t) {
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else   localStorage.removeItem(TOKEN_KEY);
  } catch (_) { /* private mode etc. */ }
}

// authFetch: thin wrapper around fetch() that adds the bearer header when a
// token is present. Used for EVERY API call so we never accidentally hit an
// endpoint anonymously when the user is signed in. If no token is stored,
// the request goes unauthenticated -- the server's anonymous-mode toggle
// decides whether that 401s or falls back to group:all.
function authFetch(url, options) {
  const opts = options ? { ...options } : {};
  const headers = new Headers(opts.headers || {});
  const tok = getToken();
  if (tok) headers.set('Authorization', `Bearer ${tok}`);
  opts.headers = headers;
  return fetch(url, opts);
}

function memscope() {
  return {
    // ---------- shared ----------
    tab: 'dag',
    wsList: [],

    // ---------- auth state ----------
    // currentUser is populated by /api/whoami on init(). null while loading.
    // For anonymous mode it carries the synthetic user; for signed-in users
    // it carries email + principals so the topbar can show the chip.
    currentUser: null,
    signinOpen: false,
    signinTokenInput: '',
    signinError: '',

    // ---------- view routing ----------
    // The app has three top-level views:
    //   'home'      -- the product landing page (what is memlayer?)
    //   'scenarios' -- the three big scenario cards
    //   'app'       -- the tab content (DAG / Search / Pipeline)
    // First-time visitors land on 'home'. The "Try a scenario" button takes
    // them to 'scenarios'. Running a scenario takes them to 'app'.
    view: 'home',

    // Back-compat shim: parts of the codebase still reference landingMode.
    // It is now derived from `view` and writes flip the view appropriately.
    get landingMode() { return this.view === 'scenarios'; },
    set landingMode(v) { this.view = v ? 'scenarios' : 'app'; },

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
    // DAG and Pipeline used to call the API without a principals string,
    // which let any caller enumerate restricted entities and read restricted
    // memory rows. The server now requires principals; the UI defaults to
    // group:all (least-privileged) and lets the user widen via the "Who's
    // asking" field.
    dagPrincipals: 'group:all',
    dag: null,
    selected: null,
    dagError: '',
    // Compression lineage for the currently-selected entity (moat feature).
    // Loaded alongside the DAG; degrades silently if the endpoint is absent.
    compression: null,

    // ---------- Search view ----------
    searchWorkspace: '',
    searchQuery: '',
    searchPrincipals: 'group:all',
    searchK: 10,
    searchHits: [],
    searchRan: false,
    searchLoading: false,
    searchError: '',
    // Federated query-expansion terms returned by /api/search (moat feature).
    searchExpansions: [],

    // ---------- Pipeline view ----------
    pipelineWorkspace: '',
    pipelinePrincipals: 'group:all',
    stats: null,
    pipelineError: '',
    ingestLoading: false,
    ingestResult: null,
    // Privacy-filter redaction log (moat feature). Loaded with stats.
    redactions: [],
    redactionsError: '',

    // ---------- Identities view (Phase 7) ----------
    // Identities are cross-source unifications: one canonical identity
    // gathers per-source aliases. ACL pre-filter is applied so an
    // identity whose memory rows you can't see is not enumerated.
    identitiesWorkspace: '',
    identitiesPrincipals: 'group:all',
    identitiesList: [],
    identitiesError: '',
    identitiesBannerDismissed: false,
    identitiesStoryActive: false,
    selectedIdentity: null,
    identityDetails: null,         // /api/identity/{id}
    mergeProposals: [],
    proposalBusy: null,            // proposal id while approve/reject is in-flight

    // ---------- Insights view (moat features) ----------
    // The Insights tab surfaces the deep "moat" properties: governance
    // conflict resolution, federated cross-tenant concepts, and Ebbinghaus
    // decay. Each section loads independently and degrades gracefully (an
    // inline error, never a crash) if its endpoint isn't available yet.
    insightsWorkspace: '',
    insightsBannerDismissed: false,
    // Governance conflicts
    conflicts: [],
    conflictsError: '',
    conflictBusy: null,            // conflict id while a resolve is in-flight
    // Federated concepts
    conceptsGlobal: [],
    conceptsLocal: [],
    conceptsSynonyms: [],
    conceptsError: '',
    // Decay preview
    decay: null,
    decayError: '',

    // =====================================================
    // init: identify the caller, then pull the workspace list.
    // =====================================================
    async init() {
      window.memscope_app = this;  // expose for SVG onclick callbacks
      // /api/whoami doubles as a 401 probe: if the user has no token AND
      // the server has auth enabled, we surface the sign-in chip prominently
      // by leaving currentUser=null. Once a token is pasted, init() is
      // re-run to pick up the new identity.
      await this.refreshWhoami();
      await this.refreshWorkspaces();
    },

    async refreshWhoami() {
      try {
        const r = await authFetch('/api/whoami').then(this._json);
        this.currentUser = r;
      } catch (e) {
        this.currentUser = null;
      }
    },

    async refreshWorkspaces() {
      try {
        const r = await authFetch('/api/workspaces').then(this._json);
        this.wsList = r.workspaces || [];
      } catch (e) {
        // Render in the dag-view banner (the user is most likely there on load).
        this.dagError = `loading workspaces: ${e.message}`;
      }
    },

    // =====================================================
    // Sign in / sign out -- minimal "paste a token" affordance.
    // =====================================================
    openSignin() {
      this.signinTokenInput = '';
      this.signinError = '';
      this.signinOpen = true;
    },
    closeSignin() {
      this.signinOpen = false;
      this.signinError = '';
      this.signinTokenInput = '';
    },
    async submitSignin() {
      const tok = (this.signinTokenInput || '').trim();
      if (!tok) {
        this.signinError = 'paste a bearer token';
        return;
      }
      setToken(tok);
      // Probe /api/whoami with the new token. If the server rejects it we
      // clear the token again so the user isn't stuck with a bad credential.
      try {
        const r = await authFetch('/api/whoami').then(this._json);
        this.currentUser = r;
        this.signinOpen = false;
        this.signinTokenInput = '';
        this.signinError = '';
        await this.refreshWorkspaces();
      } catch (e) {
        setToken('');
        this.currentUser = null;
        this.signinError = `sign-in failed: ${e.message}`;
      }
    },
    async signOut() {
      setToken('');
      this.currentUser = null;
      this.wsList = [];
      // Re-probe so anonymous mode (if enabled) reflects in the chip.
      await this.refreshWhoami();
      await this.refreshWorkspaces();
    },
    get signedIn() {
      return !!(this.currentUser && !this.currentUser.anonymous);
    },

    // =====================================================
    // View routing
    // =====================================================
    showHome() {
      this.view = 'home';
      this.helpOpen = false;
      // When returning to the top of the funnel, scroll back to the top so
      // the hero is the first thing the user sees.
      this.$nextTick(() => {
        if (typeof window !== 'undefined' && window.scrollTo) {
          window.scrollTo({ top: 0, behavior: 'smooth' });
        }
      });
    },

    showLanding() {
      // Legacy name; the "Try a scenario" button still calls this. Now means
      // "show the scenario picker".
      this.view = 'scenarios';
      this.helpOpen = false;
    },

    scrollToHowItWorks() {
      this.$nextTick(() => {
        const el = document.getElementById('how-it-works');
        if (el && el.scrollIntoView) {
          el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      });
    },

    jumpToTab(name) {
      this.tab = name;
      this.view = 'app';
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
      // person:ali is public; group:all is enough to load the full DAG.
      this.dagPrincipals = 'group:all';
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

    // ----- Scenario D: identities -- stitch the same person across sources -----
    async runScenarioD() {
      this.tab = 'identities';
      this.identitiesWorkspace = 'acme';
      this.identitiesPrincipals = 'group:all';
      this.identitiesStoryActive = true;
      this.identitiesBannerDismissed = true;
      this.landingMode = false;
      await this.loadIdentities();
      // Auto-select Ali Karsu so the cross-source aliases are visible.
      const ali = this.identitiesList.find(
        i => i.entity_key === 'person:ali'
      );
      if (ali) {
        await this.selectIdentity(ali.id);
      }
    },

    // ----- Scenario C: refuse to pay twice (Pipeline, acme, sample_docs) -----
    async runScenarioC() {
      this.tab = 'pipeline';
      this.pipelineWorkspace = 'acme';
      this.pipelinePrincipals = 'group:all';
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
    get dagPrincipalsCsv() {
      // Same CSV shape /api/search uses; default to group:all if blank so
      // a stray empty input doesn't break the call.
      const v = (this.dagPrincipals || '').split(',')
        .map(s => s.trim()).filter(Boolean).join(',');
      return v || 'group:all';
    },

    async loadEntities() {
      this.entityKey = '';
      this.entityList = [];
      this.dag = null;
      this.selected = null;
      this.compression = null;
      this.dagError = '';
      if (!this.workspace) return;
      try {
        // Principals come from the authenticated user now -- we don't pass
        // them in the URL. The "Who's asking" field is left in the UI as a
        // read-only hint about what the server will use (currentUser.principals).
        const r = await authFetch(
          `/api/entities?workspace=${encodeURIComponent(this.workspace)}`
        ).then(this._json);
        this.entityList = r.entities || [];
      } catch (e) {
        this.dagError = `loading entities: ${e.message}`;
      }
    },

    async loadDag() {
      this.selected = null;
      this.compression = null;
      this.dagError = '';
      if (!this.workspace || !this.entityKey) return;
      try {
        const r = await authFetch(
          `/api/memory/${encodeURIComponent(this.workspace)}/${encodeURIComponent(this.entityKey)}`
        ).then(this._json);
        this.dag = r;
      } catch (e) {
        this.dagError = `loading DAG: ${e.message}`;
      }
      // Compression lineage is a separate moat endpoint. Failure here is
      // non-fatal -- the DAG still renders; the summary panel just hides.
      await this.loadCompression();
    },

    // Distilled-summary lineage for the selected entity. Every summary
    // sentence carries the source row it was derived from -- visible proof
    // that compression never hallucinates. Degrades to null silently.
    async loadCompression() {
      this.compression = null;
      if (!this.workspace || !this.entityKey) return;
      try {
        const r = await authFetch(
          `/api/compression/${encodeURIComponent(this.workspace)}/${encodeURIComponent(this.entityKey)}`
        ).then(this._json);
        this.compression = r;
      } catch (e) {
        // No summary / endpoint unavailable: just show nothing.
        this.compression = null;
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
      this.searchExpansions = [];
      // Principals come from the authenticated user. The on-screen "Who's
      // asking" field stays editable because the demo scenarios mutate it
      // to tell the audit story -- but it is no longer passed to the
      // server, which trusts only what the bearer token says about us.
      const url = `/api/search?workspace=${encodeURIComponent(this.searchWorkspace)}`
        + `&q=${encodeURIComponent(this.searchQuery)}`
        + `&k=${encodeURIComponent(this.searchK)}`;
      try {
        const r = await authFetch(url).then(this._json);
        this.searchHits = r.hits || [];
        // Federated query expansion: related terms the cross-workspace concept
        // layer added to widen recall. Empty / absent -> render nothing.
        this.searchExpansions = Array.isArray(r.expansions) ? r.expansions : [];
      } catch (e) {
        this.searchError = `search failed: ${e.message} (API may not be available yet)`;
      } finally {
        this.searchLoading = false;
      }
    },

    // =====================================================
    // Pipeline view actions
    // =====================================================
    get pipelinePrincipalsCsv() {
      const v = (this.pipelinePrincipals || '').split(',')
        .map(s => s.trim()).filter(Boolean).join(',');
      return v || 'group:all';
    },

    async loadStats() {
      this.pipelineError = '';
      this.stats = null;
      if (!this.pipelineWorkspace) return;
      try {
        const r = await authFetch(
          `/api/pipeline/stats?workspace=${encodeURIComponent(this.pipelineWorkspace)}`
        ).then(this._json);
        this.stats = r;
      } catch (e) {
        this.pipelineError = `loading stats: ${e.message} (API may not be available yet)`;
      }
      // Redaction log is a separate moat endpoint; load it alongside stats
      // but keep its failure isolated so the rest of the pipeline view works.
      await this.loadRedactions();
    },

    // Privacy filter: what secrets were stripped at ingest for this workspace.
    async loadRedactions() {
      this.redactions = [];
      this.redactionsError = '';
      if (!this.pipelineWorkspace) return;
      try {
        const r = await authFetch(
          `/api/pipeline/redactions?workspace=${encodeURIComponent(this.pipelineWorkspace)}`
        ).then(this._json);
        this.redactions = r.redactions || [];
      } catch (e) {
        this.redactionsError = `redaction log (not available): ${e.message}`;
      }
    },

    async ingest(dir, allowedPrincipals) {
      this.pipelineError = '';
      if (!this.pipelineWorkspace) return;
      this.ingestLoading = true;
      this.ingestResult = null;
      try {
        const r = await authFetch('/api/pipeline/ingest', {
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
    // Identities view actions (Phase 7)
    // =====================================================
    get identitiesPrincipalsCsv() {
      const v = (this.identitiesPrincipals || '').split(',')
        .map(s => s.trim()).filter(Boolean).join(',');
      return v || 'group:all';
    },

    async loadIdentities() {
      this.identitiesError = '';
      this.identitiesList = [];
      this.mergeProposals = [];
      this.selectedIdentity = null;
      this.identityDetails = null;
      if (!this.identitiesWorkspace) return;
      try {
        const [list, props] = await Promise.all([
          authFetch(
            `/api/identities?workspace=${encodeURIComponent(this.identitiesWorkspace)}`
          ).then(this._json),
          authFetch(
            `/api/merge_proposals?workspace=${encodeURIComponent(this.identitiesWorkspace)}`
          ).then(this._json),
        ]);
        this.identitiesList = list.identities || [];
        this.mergeProposals = props.proposals || [];
      } catch (e) {
        this.identitiesError = `loading identities: ${e.message}`;
      }
    },

    async selectIdentity(id) {
      const ident = this.identitiesList.find(i => i.id === id);
      this.selectedIdentity = ident || null;
      this.identityDetails = null;
      if (!ident) return;
      try {
        const r = await authFetch(
          `/api/identity/${ident.id}`
        ).then(this._json);
        this.identityDetails = r;
      } catch (e) {
        // Details are non-critical -- still keep the basic identity row.
        this.identitiesError = `loading identity details: ${e.message}`;
      }
    },

    async approveProposal(id) {
      this.proposalBusy = id;
      try {
        await authFetch(`/api/merge_proposals/${id}/approve`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        }).then(this._json);
        // Re-fetch list and proposals so the merged identity disappears.
        await this.loadIdentities();
      } catch (e) {
        this.identitiesError = `approve failed: ${e.message}`;
      } finally {
        this.proposalBusy = null;
      }
    },

    async rejectProposal(id) {
      const reason = window.prompt(
        'Why reject this merge? (kept in the denylist so we won\'t propose it again)',
        'not the same person'
      );
      if (reason === null) return;  // cancelled
      this.proposalBusy = id;
      try {
        await authFetch(`/api/merge_proposals/${id}/reject`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason }),
        }).then(this._json);
        await this.loadIdentities();
      } catch (e) {
        this.identitiesError = `reject failed: ${e.message}`;
      } finally {
        this.proposalBusy = null;
      }
    },

    // =====================================================
    // Insights view actions (moat features)
    // =====================================================
    // Load all three Insights sections. Each is independent: one endpoint
    // 404ing (e.g. before the backend merge) must not blank the others.
    async loadInsights() {
      if (!this.insightsWorkspace) return;
      await Promise.all([
        this.loadConflicts(),
        this.loadConcepts(),
        this.loadDecay(),
      ]);
    },

    async loadConflicts() {
      this.conflictsError = '';
      this.conflicts = [];
      if (!this.insightsWorkspace) return;
      try {
        const r = await authFetch(
          `/api/governance/conflicts?workspace=${encodeURIComponent(this.insightsWorkspace)}`
        ).then(this._json);
        this.conflicts = r.conflicts || [];
      } catch (e) {
        this.conflictsError = `conflicts (not available): ${e.message}`;
      }
    },

    async resolveConflict(conflictId, winnerRowId) {
      this.conflictBusy = conflictId;
      try {
        await authFetch(`/api/governance/conflicts/${encodeURIComponent(conflictId)}/resolve`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ winner_row_id: winnerRowId }),
        }).then(this._json);
        await this.loadConflicts();
      } catch (e) {
        this.conflictsError = `resolve failed: ${e.message}`;
      } finally {
        this.conflictBusy = null;
      }
    },

    async loadConcepts() {
      this.conceptsError = '';
      this.conceptsGlobal = [];
      this.conceptsLocal = [];
      this.conceptsSynonyms = [];
      if (!this.insightsWorkspace) return;
      try {
        const r = await authFetch(
          `/api/concepts?workspace=${encodeURIComponent(this.insightsWorkspace)}`
        ).then(this._json);
        // Sort global concepts by how many tenants have seen them (descending)
        // so the most broadly-learned concept reads first / biggest.
        this.conceptsGlobal = (r.global || [])
          .slice()
          .sort((a, b) => (b.tenant_count || 0) - (a.tenant_count || 0));
        this.conceptsLocal = r.local || [];
        this.conceptsSynonyms = r.synonyms || [];
      } catch (e) {
        this.conceptsError = `concepts (not available): ${e.message}`;
      }
    },

    // Size a global-concept chip by how many tenants have seen it. Maps the
    // tenant count to a font size / weight band so cross-tenant breadth is
    // legible at a glance.
    conceptChipStyle(tenantCount) {
      const n = Math.max(1, tenantCount || 1);
      const size = Math.min(20, 12 + n);           // 13px..20px
      const weight = n >= 4 ? 700 : (n >= 2 ? 600 : 500);
      return `font-size: ${size}px; font-weight: ${weight};`;
    },

    async loadDecay() {
      this.decayError = '';
      this.decay = null;
      if (!this.insightsWorkspace) return;
      try {
        const r = await authFetch(
          `/api/decay/preview?workspace=${encodeURIComponent(this.insightsWorkspace)}`
        ).then(this._json);
        this.decay = r;
      } catch (e) {
        this.decayError = `decay preview (not available): ${e.message}`;
      }
    },

    // ----- Scenario E: the moat -- smarter and honest (Insights, acme) -----
    async runScenarioInsights() {
      this.tab = 'insights';
      this.insightsWorkspace = 'acme';
      this.insightsBannerDismissed = false;
      this.landingMode = false;
      await this.loadInsights();
    },

    // Hub-and-spoke SVG: the identity is a center node, each alias is a
    // leaf colored by source. Visually demonstrates "one identity, many
    // sources" without needing a real graph layout library.
    renderClusterGraph() {
      const ident = this.selectedIdentity;
      if (!ident || !ident.aliases || ident.aliases.length === 0) return '';
      const W = 420, H = 220;
      const cx = W / 2, cy = H / 2;
      const hubR = 38;
      const leafR = 26;
      const orbit = 75;
      const n = ident.aliases.length;
      const esc = s => String(s).replace(/[&<>"']/g,
        c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

      // Source color palette: matches the alias-chip CSS classes.
      const srcColor = src => ({
        slack:  '#7c6cff',
        gmail:  '#ef6b73',
        github: '#8c95a4',
        notion: '#e3e7ee',
        crm:    '#5ea8ff',
        journal:'#4cc38a',
      })[src] || '#aab2bf';

      const edges = ident.aliases.map((a, i) => {
        const angle = (2 * Math.PI * i) / n - Math.PI / 2;
        const lx = cx + Math.cos(angle) * orbit;
        const ly = cy + Math.sin(angle) * orbit;
        return `<line x1="${cx}" y1="${cy}" x2="${lx}" y2="${ly}"
                      stroke="#2a313c" stroke-width="1.5" />`;
      }).join('');

      const leaves = ident.aliases.map((a, i) => {
        const angle = (2 * Math.PI * i) / n - Math.PI / 2;
        const lx = cx + Math.cos(angle) * orbit;
        const ly = cy + Math.sin(angle) * orbit;
        const color = srcColor(a.source);
        const label = esc(a.external_id.length > 14
          ? a.external_id.slice(0, 13) + '…'
          : a.external_id);
        const srcLabel = esc(a.source.toUpperCase());
        return `
          <g>
            <circle cx="${lx}" cy="${ly}" r="${leafR}"
                    fill="#161c25" stroke="${color}" stroke-width="2" />
            <text x="${lx}" y="${ly - 2}" text-anchor="middle"
                  font-size="9" font-weight="700" fill="${color}"
                  letter-spacing="0.06em">${srcLabel}</text>
            <text x="${lx}" y="${ly + 10}" text-anchor="middle"
                  font-size="8.5" fill="#aab2bf"
                  font-family="ui-monospace,Menlo,monospace">${label}</text>
          </g>`;
      }).join('');

      const hubLabel = esc(ident.canonical_name.length > 14
        ? ident.canonical_name.slice(0, 13) + '…'
        : ident.canonical_name);

      return `
        <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}"
             xmlns="http://www.w3.org/2000/svg" class="cluster-svg">
          ${edges}
          <circle cx="${cx}" cy="${cy}" r="${hubR}"
                  fill="#1b1840" stroke="#7c6cff" stroke-width="2" />
          <text x="${cx}" y="${cy + 4}" text-anchor="middle"
                font-size="11" font-weight="700" fill="#e3e7ee">${hubLabel}</text>
          ${leaves}
        </svg>`;
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
      // Every string field that lands inside an HTML/SVG attribute is run
      // through esc(). source_type in particular is attacker-influenceable
      // (any writer can call remember() with an arbitrary string today),
      // and was previously interpolated *raw* into class="node ${...}" and
      // class="chip-bg src-${...}" -- a `class="agent" onclick="..."` payload
      // would have escaped attribute context and landed an XSS.
      const nodeSvg = dag.nodes.map(n => {
        const { x, y } = pos[n.id];
        const safeSrc = esc(n.source_type);
        const cls = `node ${safeSrc}${n.is_current ? ' current' : ' superseded'}`;
        // Badge layout (left to right): #id (mono) -- SOURCE chip -- confidence chip -- status
        // We draw everything as <text>/<rect> -- no <foreignObject> to keep export simple.
        const idText = `#${n.id}`;
        const srcText = String(n.source_type).toUpperCase();
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

            <rect class="chip-bg src-${safeSrc}" x="${srcChipX}" y="${y + 10}"
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
