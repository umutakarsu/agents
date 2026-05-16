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
CREATE TABLE IF NOT EXISTS memory (
    id            BIGSERIAL   PRIMARY KEY,
    workspace     TEXT        NOT NULL,
    entity_key    TEXT        NOT NULL,           -- what this memory is about
    content       TEXT        NOT NULL,
    source_type   TEXT        NOT NULL,           -- 'agent' | 'human' | 'system'
    source_id     TEXT        NOT NULL,
    derived_from  BIGINT[]    NOT NULL DEFAULT '{}',  -- raw_event ids
    confidence    REAL        NOT NULL DEFAULT 0.5,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_by BIGINT      REFERENCES memory(id)
);

CREATE INDEX IF NOT EXISTS idx_raw_events_workspace ON raw_events (workspace);
CREATE INDEX IF NOT EXISTS idx_memory_entity ON memory (workspace, entity_key);
-- HNSW for predictable p95 latency at the 1-10M vector scale.
CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw
    ON embeddings USING hnsw (embedding vector_cosine_ops);
