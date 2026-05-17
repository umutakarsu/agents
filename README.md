# memlayer

A shared memory / context layer for AI agents — the Nexus-style architecture,
built from scratch to understand it. **Phases 0–2 built**: ingestion +
content-addressed dedup + embeddings with ACL + hybrid retrieval.

## What this proves

Three of the four hard problems in a real memory layer:

1. **Freshness vs. cost** — embeddings are keyed by `content_hash`. Re-ingesting
   unchanged content computes **zero** new embeddings. A one-paragraph doc edit
   re-embeds one chunk, not the whole document.
2. **Permissions, decoupled from embeddings** — ACLs live in `chunk_acl`, not in
   the vector. A permission change is a metadata patch, never a re-embed.
3. **Trustworthy retrieval** — vector + full-text fused with RRF, with the ACL
   filter applied as a **pre-filter inside both arms in SQL**. A caller can
   never rank, count, or leak a chunk they aren't allowed to see.

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

## Layout

| Path | Role |
|---|---|
| `schema.sql` | Full schema (incl. `memory` for later phases) |
| `memlayer/chunking.py` | Normalize + sha256 content addressing |
| `memlayer/embeddings.py` | Provider abstraction (local/voyage/openai) |
| `memlayer/ingest.py` | The pipeline + dedup gate |
| `memlayer/retrieval.py` | Hybrid vector+FTS, RRF fusion, ACL pre-filter |
| `memlayer/connectors/` | Thin source adapters (local files today) |

## Next phases

- **3** — Write-back: append-only `memory` with provenance + confidence.
- **4** — More connectors (GitHub, MCP), scheduling, multi-tenancy hardening.
