-- Memory layer foundation schema (Postgres + pgvector)
-- Phases 0-1: raw ingestion, content-addressed chunks, embeddings with ACL.

CREATE EXTENSION IF NOT EXISTS vector;

-- Layer 1: append-only raw event log. Source of truth, never mutated.
CREATE TABLE IF NOT EXISTS raw_events (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT        NOT NULL,            -- 'local', 'github', 'slack', ...
    source_id    TEXT        NOT NULL,            -- stable id within the source
    workspace    TEXT        NOT NULL,            -- multi-tenant boundary
    payload      JSONB       NOT NULL,
    occurred_at  TIMESTAMPTZ NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, source_id, workspace)
);

-- Content-addressed chunks. The content_hash is the dedup key:
-- identical text across documents/sources is stored and embedded once.
CREATE TABLE IF NOT EXISTS chunks (
    content_hash TEXT        PRIMARY KEY,         -- sha256 of normalized text
    text         TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Maps a raw_event to its ordered chunks. A doc edit that changes one
-- paragraph re-points only the affected chunk_index to a new hash.
CREATE TABLE IF NOT EXISTS event_chunks (
    event_id     BIGINT  NOT NULL REFERENCES raw_events(id) ON DELETE CASCADE,
    chunk_index  INT     NOT NULL,
    content_hash TEXT    NOT NULL REFERENCES chunks(content_hash),
    PRIMARY KEY (event_id, chunk_index)
);

-- One embedding per unique chunk. Keyed by content_hash, so re-ingesting
-- unchanged content is a no-op (the freshness/cost solution).
CREATE TABLE IF NOT EXISTS embeddings (
    content_hash TEXT        PRIMARY KEY REFERENCES chunks(content_hash),
    model        TEXT        NOT NULL,
    embedding    VECTOR(256) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ACL principals attached to a chunk *in a workspace context*. Permission
-- changes patch this table only -- never re-embed. Retrieval pre-filters
-- on the requesting user's principal set.
CREATE TABLE IF NOT EXISTS chunk_acl (
    content_hash       TEXT   NOT NULL REFERENCES chunks(content_hash),
    workspace          TEXT   NOT NULL,
    allowed_principals TEXT[] NOT NULL,           -- e.g. {'user:ali','group:eng'}
    PRIMARY KEY (content_hash, workspace)
);

-- Layer for write-back. Append-only, versioned, provenance-carrying.
-- Not exercised in the foundation phase, defined here so the schema is whole.
-- source_type is constrained to the precedence enum at the schema level so
-- an agent caller can't claim source_type='human' and outrank a real human
-- via the conflict-resolution ladder. confidence is bounded to [0, 1] to
-- prevent inf/NaN from short-circuiting precedence comparisons.
CREATE TABLE IF NOT EXISTS memory (
    id            BIGSERIAL   PRIMARY KEY,
    workspace     TEXT        NOT NULL,
    entity_key    TEXT        NOT NULL,           -- what this memory is about
    content       TEXT        NOT NULL,
    source_type   TEXT        NOT NULL
                  CHECK (source_type IN ('human', 'system', 'agent')),
    source_id     TEXT        NOT NULL,
    derived_from  BIGINT[]    NOT NULL DEFAULT '{}',  -- raw_event ids
    confidence    REAL        NOT NULL DEFAULT 0.5
                  CHECK (confidence BETWEEN 0.0 AND 1.0),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_by BIGINT      REFERENCES memory(id)
);
-- Memory carries its own ACL so retrieval can pre-filter distilled knowledge
-- with the same trust guarantee as raw chunks. Idempotent migration for
-- databases created before this column existed.
ALTER TABLE memory
    ADD COLUMN IF NOT EXISTS allowed_principals TEXT[] NOT NULL DEFAULT '{group:all}';

CREATE INDEX IF NOT EXISTS idx_raw_events_workspace ON raw_events (workspace);
CREATE INDEX IF NOT EXISTS idx_memory_entity ON memory (workspace, entity_key);
-- HNSW for predictable p95 latency at the 1-10M vector scale.
CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw
    ON embeddings USING hnsw (embedding vector_cosine_ops);
-- GIN over the FTS arm. Expression index so chunks.text needs no extra column.
CREATE INDEX IF NOT EXISTS idx_chunks_fts
    ON chunks USING gin (to_tsvector('english', text));
-- FTS over distilled memory (the third retrieval arm).
CREATE INDEX IF NOT EXISTS idx_memory_fts
    ON memory USING gin (to_tsvector('english', content));
-- ACL pre-filter touches this on every query; index the lookup + array overlap.
CREATE INDEX IF NOT EXISTS idx_chunk_acl_lookup
    ON chunk_acl USING gin (allowed_principals);

-- Privacy filter audit log (phase 6)
CREATE TABLE IF NOT EXISTS redactions_log (
    id          BIGSERIAL PRIMARY KEY,
    workspace   TEXT NOT NULL,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,           -- 'aws_access_key', 'openai_api_key', etc.
    count       INT NOT NULL,            -- how many matches replaced
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS redactions_log_workspace_idx ON redactions_log (workspace, occurred_at DESC);

-- Phase 6: tiered memory + Ebbinghaus decay
-- A row's tier sets its half-life and floor; last_referenced_at is the
-- reinforcement clock (every recall/search read bumps it to now()).
-- Effective confidence (confidence * 0.5^(age/half_life), floored per tier)
-- is computed at read time, never stored. Auto-promotion between tiers is
-- future work; today remember() takes an explicit tier (default 'semantic'
-- so existing demo data keeps its meaning).
ALTER TABLE memory
    ADD COLUMN IF NOT EXISTS tier TEXT NOT NULL DEFAULT 'semantic';
DO $$ BEGIN
    ALTER TABLE memory ADD CONSTRAINT memory_tier_check
        CHECK (tier IN ('working','episodic','semantic','procedural'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
ALTER TABLE memory
    ADD COLUMN IF NOT EXISTS last_referenced_at TIMESTAMPTZ NOT NULL DEFAULT now();
-- Eviction sweeps filter on (tier, last_referenced_at); composite keeps the
-- low-tier scans cheap as the table grows.
CREATE INDEX IF NOT EXISTS idx_memory_tier_refd
    ON memory (tier, last_referenced_at);

-- Phase 10: federated cross-tenant concept ontology.
-- Privacy contract: this layer stores concept NAMES + AGGREGATE counts + SYNONYM
-- pairs derived from co-occurrence across tenants. No content, no entity_keys,
-- no per-tenant attribution is ever shared cross-tenant.

CREATE TABLE IF NOT EXISTS concept (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,                -- 'code review', 'customer churn'
    global_count INT NOT NULL DEFAULT 0,      -- summed across tenants, but NOT attributed
    tenant_count INT NOT NULL DEFAULT 0,      -- how many distinct tenants this appeared in
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS concept_name_idx ON concept (name);

-- Per-tenant. NEVER returned in cross-tenant queries. Used only to compute the
-- aggregates in `concept`.
CREATE TABLE IF NOT EXISTS tenant_concept (
    id BIGSERIAL PRIMARY KEY,
    workspace TEXT NOT NULL,
    concept_id BIGINT NOT NULL REFERENCES concept(id),
    local_count INT NOT NULL,
    last_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace, concept_id)
);
CREATE INDEX IF NOT EXISTS tenant_concept_workspace_idx ON tenant_concept (workspace);

-- Cross-tenant synonyms. A pair (a, b) is a synonym if both appear in the same
-- chunks/memory across N>=2 INDEPENDENT tenants. Stored once per pair.
CREATE TABLE IF NOT EXISTS concept_synonym (
    id BIGSERIAL PRIMARY KEY,
    concept_a BIGINT NOT NULL REFERENCES concept(id),
    concept_b BIGINT NOT NULL REFERENCES concept(id),
    cooccurrence_tenants INT NOT NULL,        -- how many distinct tenants saw both together
    confidence REAL NOT NULL,
    last_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (concept_a, concept_b),
    CHECK (concept_a < concept_b)
);
