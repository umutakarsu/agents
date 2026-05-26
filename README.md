# memlayer

A shared memory / context layer for AI agents — the Nexus-style architecture,
built from scratch to understand it. **Phases 0–3 built and verified
end-to-end on Postgres**: ingestion + content-addressed dedup + ACL +
hybrid retrieval + provenance-carrying write-back.

## What this proves

All four hard problems in a real memory layer:

1. **Freshness vs. cost** — embeddings are keyed by `content_hash`. Re-ingesting
   unchanged content computes **zero** new embeddings; an identical *section*
   recurring across documents is embedded once (heading-aware chunking).
2. **Permissions, decoupled from embeddings** — ACLs live in `chunk_acl`, not in
   the vector. A permission change is a metadata patch, never a re-embed.
3. **Trustworthy retrieval** — vector + full-text fused with RRF, with the ACL
   filter applied as a **pre-filter inside both arms in SQL**. A caller can
   never rank, count, or leak a chunk they aren't allowed to see.
4. **Write-back with provenance + conflict resolution** — memory is
   append-only; superseding sets a pointer, never deletes. Precedence is
   (source rank, confidence, recency): human > system > agent. A low-confidence
   agent guess can never overwrite a human correction, and the full trail
   stays auditable.

(Write-back with provenance and the agent loop are Phases 3–4 — not built yet.)

## Architecture (foundation)

```
connector ──► raw_events (append-only, source of truth)
                  │
                  ▼
              chunk + sha256(normalized text) = content_hash
                  │
       ┌──────────┼───────────────┐
       ▼          ▼               ▼
    chunks   embeddings       chunk_acl
            (only if hash    (permission patch
             is new = cost)   point, never re-embed)
```

## Run it

Requires Postgres with the `pgvector` extension. Runs fully offline — the
default embedder is deterministic, no API keys needed.

```bash
pip install -e .
cp .env.example .env          # set DATABASE_URL
python scripts/init_db.py
python scripts/ingest_local.py sample_docs --workspace acme
python scripts/ingest_local.py sample_docs --workspace acme   # run again
```

On the **second run**, `embeddings computed: 0` and `embeddings reused` equals
the chunk count — the dedup gate working. The two sample docs also share an
identical paragraph, so even on the first run it is embedded only once.

### Prove the ACL pre-filter (Phase 2)

```bash
# Ingest a restricted doc visible only to exec principals.
python scripts/ingest_local.py sample_docs_restricted \
    --workspace acme --principals user:ali,group:exec

# group:all CANNOT see the secret roadmap -> no restricted hit.
python scripts/search.py "secret roadmap acquire competitor" \
    --workspace acme --principals group:all

# An exec principal CAN.
python scripts/search.py "secret roadmap acquire competitor" \
    --workspace acme --principals group:exec
```

The filter is a SQL pre-filter inside both retrieval arms, so the forbidden
chunk is never ranked, counted, or returned for `group:all`.

### Prove conflict-resolved write-back (Phase 3)

```bash
# Agent infers something (low confidence).
python scripts/remember.py acme person:ali \
    "Ali leads retrieval" --source agent:email-scanner --confidence 0.6
# Human corrects it (high confidence) -> becomes current.
python scripts/remember.py acme person:ali \
    "Ali leads Platform" --source human:manager --confidence 0.95
# A later low-confidence agent guess CANNOT override the human.
python scripts/remember.py acme person:ali \
    "Ali maybe left" --source agent:slack-scanner --confidence 0.4

python scripts/recall.py acme person:ali             # the human's memory
python scripts/recall.py acme person:ali --history   # full audit trail
```

## Layout

| Path | Role |
|---|---|
| `schema.sql` | Full schema |
| `memlayer/chunking.py` | Heading-aware split + sha256 content addressing |
| `memlayer/embeddings.py` | Provider abstraction (local/voyage/openai) |
| `memlayer/ingest.py` | The pipeline + dedup gate |
| `memlayer/retrieval.py` | Hybrid vector+FTS, RRF fusion, ACL pre-filter |
| `memlayer/writeback.py` | Append-only memory + conflict resolution |
| `memlayer/connectors/` | Thin source adapters (local files, GitHub, MCP) |

### More connectors (Phase 4)

Same pipeline, same dedup gate, same ACL story -- just a different edge.

```bash
# Public GitHub repo (anonymous; set GITHUB_TOKEN for private / higher rate limits).
python scripts/ingest_github.py umutakarsu/agents --workspace acme

# An MCP server (Streamable HTTP). Pass auth via repeatable --header.
python scripts/ingest_mcp.py https://my-mcp.example/mcp \
    --workspace acme --header "Authorization: Bearer $MY_TOKEN"
```

### Expose memlayer as an MCP server

Let MCP-speaking clients (Claude Code, Cursor, OpenCode) read and write to your
memlayer through five tools: `search_memory`, `remember`, `recall`, `history`,
`ingest_text`.

```bash
pip install -e ".[mcp_server]"
python scripts/serve_mcp.py                  # stdio (for Claude Code)
python scripts/serve_mcp.py --http 3111      # HTTP transport
```

Claude Code config snippet:

```json
{
  "mcpServers": {
    "memlayer": {
      "command": "python",
      "args": ["scripts/serve_mcp.py"],
      "cwd": "/path/to/agents"
    }
  }
}
```

Permissions: the **stdio** transport trusts the caller's `principals`
verbatim -- the client spawned the process locally; the OS already
authenticated the user. The **HTTP** transport requires the same bearer
token memscope uses (see "Auth" section below) and will additionally
override `source_type` with the token owner's identity, so an agent
service-account can't claim `source_type="human"` over the wire.

## memscope -- visual inspector

A small FastAPI + Alpine.js UI for *seeing* what the memory layer does.
First slice ships the **memory DAG view**: pick a workspace + entity, get
an SVG graph of every memory row ever written for that entity, with
`superseded_by` edges drawn out so conflict resolution is visible. Click
a node for full content, provenance, confidence, and status.

```bash
pip install -e ".[memscope]"
uvicorn memscope.app:app --host 127.0.0.1 --reload
# open http://localhost:8000
```

### Auth

memscope (and the MCP server's HTTP transport) require a bearer token.
The user record stores the ONLY principals the caller is authorized to
claim, plus the ONLY workspaces they can touch -- so the HTTP boundary
is now an identity boundary, not just a parameter pass-through.

```bash
# Mint a token (prints the plaintext ONCE -- save it).
python scripts/create_user.py \
    --email ali@example.com \
    --principals group:exec,user:ali \
    --workspaces acme,personal

# Use it.
curl -H "Authorization: Bearer <token>" 'http://localhost:8000/api/search?workspace=acme&q=...&k=5'
```

The `principals` query string the UI used to send is now ignored: the
server only trusts what the user row says. Cross-workspace requests get
a 403; bad tokens get a 401.

**Demo / dev mode**: set `MEMSCOPE_AUTH_DISABLED=1` to bypass auth -- every
request acts as a synthetic anonymous user with `principals=["group:all"]`
and access to every workspace in the DB. The bundled scenarios run in
this mode. Production deploys must leave the variable unset.

Read-only -- it inspects whatever's already in your Postgres. Pipeline,
Search, and Ingest views from the design are not built yet.

## Privacy filter

Secrets and (optionally) PII are scrubbed at ingest, before chunking and
embedding. The system can never surface in search what it never stored.

Detected by default: AWS keys, OpenAI / Anthropic / Stripe / GitHub tokens,
JWTs, private-key blocks.

Opt-in (often legitimate signal otherwise): emails, SSNs, credit-card
numbers — pass `scrub_pii=True` to `scrub()` (or to `ingest()`) if you
want them stripped.

Each redaction is logged (KIND + WHERE, never the secret) to `redactions_log`:

```bash
python scripts/redactions_report.py
```

Cleanup is more expensive than prevention — wiping a secret from `chunks`
after the fact would also force re-embedding. Scrubbing at the ingest
boundary side-steps that whole class of cleanup.

## Phase 9: governance-modeled conflict resolution

The Phase 3 precedence ladder was a flat `human > system > agent`. Real
organisations need finer answers: a security lead's "this is wrong"
should override an engineering manager's "this is right" on a security
incident, even though both are humans; two equal-rank humans on a general
topic should *surface* a conflict instead of one silently overwriting
the other.

Precedence is now a function of `(role, entity_kind, confidence, recency,
policy)`. The pieces:

- **`role`** — a per-workspace label (e.g. `security_lead`,
  `engineering_manager`, `ic_engineer`) attached to a `source_type`.
- **`writer_role`** — maps a concrete `(source_type, source_id)` to a role.
- **`authority_policy`** — `(workspace, entity_kind, role) -> authority`
  rows. Higher authority wins. The wildcard kind `*` and the `default`
  workspace act as fallbacks.
- **`memory.entity_kind`** — derived at write time by
  `governance.classify_entity(entity_key)` (e.g. `security:incident-12` →
  `security`; `topic:layoffs-q1` → `hr`).
- **`memory_conflict`** — when the top two live rows' authorities differ
  by less than `CONFLICT_EPSILON` (0.05), both stay live and a pending
  conflict row is recorded for human resolution.

A workspace with no policies configured falls back to the old flat
ladder (human=3.0, system=2.0, agent=1.0), so existing data and demos
behave exactly as before until you opt in.

```bash
# Seed the system-wide defaults + an example acme policy set.
PYTHONPATH=$PWD python -c "
from memlayer.governance import seed_default_policies, seed_acme_policies
seed_default_policies(); seed_acme_policies()
"

# What's the role catalog and policy table for acme?
python scripts/govern.py list-policies --workspace acme

# A security incident -- security_lead authority 5.0 > eng_manager 4.0.
python scripts/remember.py acme "security:incident-12" \
    "the breach is contained" --source human:eng_manager --confidence 0.9
python scripts/remember.py acme "security:incident-12" \
    "the breach is NOT contained" --source human:security_lead --confidence 0.9
python scripts/recall.py acme "security:incident-12"
# -> security_lead's wording, eng_manager's row superseded.

# A general topic -- both tied at 3.0. Both rows stay live; a conflict
# row is surfaced for a higher-authority role to resolve.
python scripts/remember.py acme "topic:framework" \
    "use React" --source human:eng_manager --confidence 0.9
python scripts/remember.py acme "topic:framework" \
    "use Vue" --source human:security_lead --confidence 0.9
python scripts/govern.py conflicts --workspace acme

# Resolve. The chosen row stays current, the other gets superseded.
python scripts/govern.py resolve --conflict-id 1 --winner-row 2 \
    --by 'role:security_lead'
```

Custom org charts are just rows -- no schema change required:

```bash
python scripts/govern.py add-role --workspace acme --type human \
    --name vp_engineering --authority 4.7
python scripts/govern.py add-policy --workspace acme \
    --kind engineering --role vp_engineering --authority 6.0
python scripts/govern.py assign --workspace acme --type human \
    --source-id alice --role vp_engineering
```

## Phase 6: tiered memory + Ebbinghaus decay

Memory rows now carry a `tier` (`working` / `episodic` / `semantic` / `procedural`)
and an effective confidence that decays over time unless the row is reinforced
by being read. Stale rows in low tiers auto-evict:

```bash
python scripts/evict_stale.py            # delete stale rows
python scripts/evict_stale.py --dry-run  # report only
```

Half-lives: working=6h, episodic=14d, semantic=180d, procedural=365d.
Promotion between tiers is future work.

## Next phases

- **5 (remaining)** — memscope Pipeline + Search + Ingest views;
  scheduling (periodic connector runs); multi-tenancy hardening.

### Watch it all run

A single watchable script that walks through every property -- dedup,
ACL on chunks AND distilled memory, conflict-resolved write-back,
memory-aware retrieval -- against a real Postgres, on a `demo` workspace
that resets per run.

```bash
python scripts/demo.py
```

## Phase 7: identity unification

One real person can be `@ali` in Slack, `ali@acme.com` in Gmail, and
`ali-acme` on GitHub. Without unification every connector sees a
stranger. With unification — and **only** with a human-in-the-loop for
risky merges — the memory layer can join cross-source signals into one
canonical identity per workspace.

The risk asymmetry shapes the design: a *missed* merge is mild (you'll
see duplicates in the UI), a *bad* merge is a security incident (someone
now sees restricted memory that wasn't addressed to them). So the
algorithm errs on caution.

**Tables (see `schema.sql`):**

| Table | Role |
|---|---|
| `identity` | Canonical (workspace, entity_key) -> canonical_name + kind |
| `identity_alias` | Per-source aliases, `(source, external_id)` unique globally |
| `merge_proposal` | Pending pairs the system wants a human to approve |
| `merge_denylist` | Pairs a human rejected — never propose again |
| `identity_merge_log` | Reversal snapshot (winner/loser, aliases moved) |

**Signals (five classes, see `memlayer/identity.py`):**

1. Exact email match — weight 1.0
2. Exact handle match (`@ali` slack == `ali` notion) — weight 0.7
3. Local-part / handle Levenshtein similarity — weight 0.5
4. Display-name Levenshtein similarity — weight 0.6
5. Co-occurrence in chunk text — weight 0.4

Combined confidence is a weighted average over signals that **fire**
(value > 0.3). Rules:

- combined >= 0.95 **and** >= 2 distinct signals fired -> **auto-merge**
- 0.70 <= combined < 0.95 (or 0.95 with only 1 signal) -> **propose**
- otherwise -> **ignore**

**Adversarial defenses:**

- Rate limit: a single source can add at most 50 aliases per workspace
  per hour. Spamming aliases to inflate `evidence_count` and engineer an
  auto-merge is throttled.
- Brand-new source ceiling: an alias from a source with <= 1 prior
  alias rows in the workspace is capped at confidence 0.7. A freshly
  connected hostile source cannot reach auto-merge on day one.
- Two-signal requirement: even at combined confidence 0.99, a single
  signal class is never enough. A perfect name collision alone is just
  a name collision.

**Demo seeding (acme workspace):**

- `person:ali` — three aliases (`@ali` slack, `ali@acme.com` gmail,
  `ali-acme` github) that **do** unify into one identity.
- `person:bjorn` — three aliases across slack / gmail / notion.
- `person:chen` — one alias only (`chen@acme.com` gmail). Unresolved.
- `person:ali-karsi` — looks like Ali Karsu but isn't. Name and handle
  similarity push it above the propose threshold; the lack of an exact
  email match (different domain) keeps it below auto-merge. A pending
  merge proposal is created for human review.

**memscope UI:**

A new **Identities** tab shows the unified identities for a workspace,
a per-identity cluster graph (hub-and-spoke of source-colored alias
chips), pending merge proposals with signal breakdowns, and approve /
reject actions. Approving a proposal calls `merge()` and the identities
collapse in the list; rejecting writes to `merge_denylist` so the same
pair is never proposed again.

## Phase 8: Semantic Compression with Lineage

N raw `working` rows about the same entity get folded into one durable
`episodic` summary -- **and every sentence in that summary is traceable
back to the exact source row it came from**. The lineage is by
construction, not by post-hoc alignment: the summary is *extractive*
(verbatim sentences picked from the input), so there is no model output
to attribute and no hallucination is possible.

The pipeline (`memlayer/compress.py`):

1. Group working rows by `(workspace, entity_key)`.
2. Split each row's content into sentences, score by word-overlap
   centrality (poor man's TextRank), pick the top
   `min(5, ceil(0.25 * total_sentences))`.
3. Write an `episodic` row whose `content` is the picked sentences in
   original order, with `derived_from` listing every source row.
4. Insert one row per sentence into `summary_lineage` -- exact text +
   exact source row id.
5. Flag source rows `compressed_into=<summary_id>` so the existing
   `evict_stale` job can purge them later. The audit trail stays whole.

ACL handling is *restrictive*: the summary's `allowed_principals` is the
intersection of its sources' principals. Compression never widens
access.

```bash
# Compress one entity:
python scripts/compress.py --workspace acme --entity topic:perf-issue
# Compress every entity in a workspace with >=3 un-compressed working rows:
python scripts/compress.py --workspace acme
# Score-and-report without writing:
python scripts/compress.py --workspace acme --dry-run
```

When a source row is superseded or updated, call
`invalidate_summaries_for(source_row_id)` -- every summary that cited the
row is flagged `needs_recompression=true` and a future `compress()` run
will regenerate it.

The e2e test asserts the killer invariant directly: **every sentence in
the summary appears verbatim in its cited source row**. No hallucination
by construction.

## Phase 10: Federated Cross-Tenant Concept Ontology

The fourth moat: the platform learns from every customer's workspace, but
**no customer's content ever bleeds into another customer's retrieval**.

The mechanism is deliberately simple and provably safe: extract a per-tenant
**concept vocabulary** (n-gram terms that appear in that tenant's chunks /
memory), aggregate the vocabulary cross-tenant, and discover synonym pairs
that co-occur in many tenants' vocabularies. Concepts are the *names of
things* an org talks about; instances are the actual conversations.

```bash
# Build the per-tenant vocab and aggregate cross-tenant counts.
python scripts/federated.py index-all

# Discover synonym pairs (co-occurring concepts across >= N tenants).
python scripts/federated.py synonyms

# The demo moment: top concepts by how many tenants share them.
python scripts/federated.py top-concepts

# Privacy self-test.
python scripts/federated.py privacy-check

# Show what a query would expand to via cross-tenant synonyms.
python scripts/federated.py expand "pull request feedback"
```

### What IS shared cross-tenant

- **Concept names** -- literal terms like `"code review"`, `"customer churn"`.
  Treated as dictionary-class vocabulary, not workspace-private content.
- **Aggregate counts** -- `global_count` (sum) and `tenant_count` (distinct
  workspaces that have a concept). Never attributed.
- **Synonym pairs** -- two concept names that co-occur in the vocabularies of
  at least `min_tenants` (default 2) distinct workspaces. Stored once per
  pair, with a cooccurrence-tenant count and a confidence in `[0, 1]`.

### What is NEVER shared cross-tenant

- chunk content, memory content, entity keys,
- the actual rows that produced the concept extraction,
- which specific tenants have which concepts,
- any concept whose `tenant_count == 1` is excluded from synonym formation
  (a singleton concept would otherwise leak workspace identity).

The privacy contract is enforced **structurally**, not by policy:
`compute_synonyms` and `expand_query` only read from the aggregate `concept`
and `concept_synonym` tables. The per-tenant `tenant_concept` table is only
read with an explicit workspace scope, by `index_workspace` (single tenant),
or aggregated to `(workspace, concept_id, local_count)` triples whose
workspace identity is dropped before storage. The e2e test verifies the
literal SQL: cross-tenant aggregation paths never SELECT `chunks.text` or
`memory.content`.

### How it lifts retrieval (integration point)

`memlayer.federated.expand_query(query)` returns a list of synonym concepts
discovered cross-tenant. A query for `"PR feedback"` learns from the
population that `"code review"` is a strongly related concept, even if the
caller's own workspace never used that exact phrase. This widens FTS recall
without any other tenant's content ever crossing the boundary.

The expansion helper is a clearly-labelled hook today; wiring it into
`memlayer/retrieval.py` is intentionally deferred so the ontology layer can
land independently.

### Known weaknesses (audit notes)

- **Concept-name surface area**: concept names are themselves text. A
  pathological vocabulary (e.g. an internal codename that's also unique to
  one tenant) is technically dictionary-class but practically a unique
  identifier. Today we exclude `tenant_count == 1` from synonym formation,
  which kills the leak vector through synonyms, but if downstream callers
  ever surface raw `concept.name` rows directly, single-tenant names are
  still exposed. Mitigation: filter callers to `tenant_count >= 2`.
- **Frequency-based identification**: `global_count` is a simple sum across
  tenants. A concept with `tenant_count == 2` but `global_count == 9000`
  effectively reveals that one of those two tenants has thousands of
  occurrences -- a fingerprint, not an identity, but information leakage at
  the margins. A k-anonymity or differential-privacy noise layer on counts
  would close this.

## Phase 11: tier auto-promotion

The tier model (`working` / `episodic` / `semantic` / `procedural`) always had
a *downward* path -- `decay.evict_stale` deletes cold low-tier rows -- but a
row's tier was set once at write time and never moved up. Phase 11 closes the
loop with the *upward* path: memories that prove durable get promoted to higher
tiers automatically.

### The two upward transitions

1. **working -> episodic (accumulation).** When an entity has accumulated
   `>= min_rows` (default 3) un-compressed `working` rows, they get folded into
   one `episodic` summary via the existing extractive `compress()` (verbatim
   sentences + per-sentence lineage; see Phase 8). `promote.py` only
   orchestrates -- it finds the eligible entities and calls `compress()`, which
   writes the summary and flags each source `compressed_into`.

2. **episodic -> semantic (age + use).** An `episodic` summary that has *proven
   durable* gets promoted to `semantic` (longer half-life, higher floor: it
   stops fading). "Proven durable" is two signals together:
   - **age**: at least `min_age_days` (default 7) old -- it has had time to
     decay and didn't vanish;
   - **confidence**: its decayed `effective_confidence` is still `>= min_eff`
     (default 0.5);
   - **use**: `reference_count >= min_refs` (default 3) -- it has actually been
     read, not just sat there.

   `semantic -> procedural` is **NOT** automatic. Procedural is hand-authored
   "how-to" knowledge, so it stays a manual step.

### The reference_count signal

`reference_count` (added to `memory` in this phase, defaults to 0) complements
`last_referenced_at`: the timestamp says *when last used*, the count says *how
proven*. Promotion to `semantic` needs both age (survived decay) and use
(count).

Reinforcement happens in `writeback.recall()` and
`retrieval._reinforce_memory()` (they bump `last_referenced_at` on every read).
Wiring the `reference_count = reference_count + 1` bump into those two sites is
a **pending one-liner** -- it's deliberately not done here because those files
are owned by other work this round. Until it lands, the count stays 0 and the
reinforcement path simply promotes nothing, which is safe: a row that was never
used should not be promoted.

### CLI

```bash
# Run both promotions for a workspace.
python scripts/promote.py --workspace acme

# Report what would happen, write nothing.
python scripts/promote.py --workspace acme --dry-run
```

The thresholds are tunable: `--min-rows`, `--min-age-days`, `--min-eff`,
`--min-refs`. `promote_all(workspace)` runs both transitions in one call;
working->episodic runs first, and a freshly-created episodic summary can't be
promoted to semantic in the same pass (it can't meet the age + use gates yet) --
promotion is a multi-pass, time-gated process by design.
