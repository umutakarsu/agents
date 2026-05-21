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

Permissions: the MCP server currently trusts the caller's principals as
supplied. Production deployments need an auth layer (see note in
`memscope/app.py`).

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

> **No auth — bind to localhost only.** memscope's HTTP layer does not
> authenticate callers and trusts whatever `principals` they pass. The
> retrieval-side ACL pre-filter is still applied in SQL, but anyone with
> network reach to the listening port can claim `group:exec` and read
> restricted content. Always pass `--host 127.0.0.1` (or run behind a
> trusted reverse proxy that adds auth) until a real identity layer ships.

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
