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

## Next phases

- **4 (remaining)** — Phase 4 e2e assertions on memory-aware retrieval;
  scheduling; multi-tenancy hardening.

### Watch it all run

A single watchable script that walks through every property -- dedup,
ACL on chunks AND distilled memory, conflict-resolved write-back,
memory-aware retrieval -- against a real Postgres, on a `demo` workspace
that resets per run.

```bash
python scripts/demo.py
```
