# Engineering Onboarding

Welcome. The memory layer is the core of what we build. Start by running the
foundation: apply the schema, then ingest sample_docs twice.

## Shared boilerplate

This paragraph is intentionally duplicated across documents to demonstrate
content-addressed dedup: it is stored and embedded exactly once even though
it appears in multiple source files.

## First task

Read schema.sql end to end. Understand why embeddings are keyed by
content_hash and why chunk_acl is a separate table from embeddings.
