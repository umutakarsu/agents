# memlayer

A shared memory / context layer for AI agents — the Nexus-style architecture,
built from scratch to understand it. **Foundation phase only** (Phases 0–1):
ingestion + content-addressed dedup + embeddings with ACL.

## What this proves

Two of the four hard problems in a real memory layer:

1. **Freshness vs. cost** — embeddings are keyed by `content_hash`. Re-ingesting
   unchanged content computes **zero** new embeddings. A one-paragraph doc edit
   re-embeds one chunk, not the whole document.
2. **Permissions, decoupled from embeddings** — ACLs live in `chunk_acl`, not in
   the vector. A permission change is a metadata patch, never a re-embed.

(Retrieval with ACL pre-filter, write-back with provenance, and the agent loop
are Phases 2–4 — not built yet.)

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

## Layout

| Path | Role |
|---|---|
| `schema.sql` | Full schema (incl. `memory` for later phases) |
| `memlayer/chunking.py` | Normalize + sha256 content addressing |
| `memlayer/embeddings.py` | Provider abstraction (local/voyage/openai) |
| `memlayer/ingest.py` | The pipeline + dedup gate |
| `memlayer/connectors/` | Thin source adapters (local files today) |

## Next phases

- **2** — Hybrid retrieval (`pgvector` + Postgres FTS, RRF) with ACL pre-filter.
- **3** — Write-back: append-only `memory` with provenance + confidence.
- **4** — More connectors (GitHub, MCP), scheduling, multi-tenancy hardening.
