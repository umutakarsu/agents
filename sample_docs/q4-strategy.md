# Q4 Strategy

Our Q4 focus is the shared context layer. Every AI tool the team uses should
read and write the same memory instead of working in isolation.

## Shared boilerplate

This paragraph is intentionally duplicated across documents to demonstrate
content-addressed dedup: it is stored and embedded exactly once even though
it appears in multiple source files.

## Goals

- Ship the ingestion pipeline with content-hash dedup.
- Enforce source-system permissions at retrieval time.
- Keep retrieval under the chat latency budget.
