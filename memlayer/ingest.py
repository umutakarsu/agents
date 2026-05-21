"""Connector-agnostic ingestion. A connector only has to yield SourceItems;
this pipeline handles raw-event logging, chunking, content-hash dedup, ACL,
and embed-only-when-new. (This mirrors Nexus's approach: connectors are a thin
edge, the pipeline is the substance.)"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from memlayer.chunking import chunk, content_hash
from memlayer.db import connect
from memlayer.embeddings import embed, model_name
from memlayer.scrub import scrub


@dataclass
class SourceItem:
    source: str
    source_id: str
    text: str
    occurred_at: datetime
    allowed_principals: list[str] = field(default_factory=lambda: ["group:all"])
    payload: dict | None = None


@dataclass
class IngestStats:
    items: int = 0
    chunks: int = 0
    embeddings_computed: int = 0  # actual embedding calls (cost)
    embeddings_reused: int = 0  # dedup hits (saved cost)
    scrubbed_chars: int = 0  # total chars replaced by the privacy filter


def ingest(
    items: list[SourceItem], workspace: str, *, scrub_pii: bool = False
) -> IngestStats:
    """Run the ingest pipeline. ``scrub_pii`` opts into emails / SSNs /
    credit-card scrubbing on top of the always-on secret matchers; see
    ``memlayer.scrub`` for the matcher list."""
    stats = IngestStats()
    with connect() as conn, conn.cursor() as cur:
        for item in items:
            stats.items += 1

            # Privacy filter: scrub BEFORE chunking. The scrubbed text is
            # what gets hashed, embedded, indexed, and returned by search.
            # The raw_events payload still carries whatever the connector
            # handed us -- that's the source of truth log and the place an
            # admin can audit, but it's not what we embed. The substantive
            # leak surface (chunks + embeddings + FTS index) sees only the
            # scrubbed text.
            scrubbed_text, redactions = scrub(item.text, scrub_pii=scrub_pii)
            if redactions:
                for r in redactions:
                    stats.scrubbed_chars += r.chars
                    cur.execute(
                        """
                        INSERT INTO redactions_log
                            (workspace, source, source_id, kind, count)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (workspace, item.source, item.source_id, r.kind, r.count),
                    )
                item.text = scrubbed_text

            cur.execute(
                """
                INSERT INTO raw_events
                    (source, source_id, workspace, payload, occurred_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (source, source_id, workspace)
                DO UPDATE SET payload = EXCLUDED.payload,
                              occurred_at = EXCLUDED.occurred_at
                RETURNING id
                """,
                (
                    item.source,
                    item.source_id,
                    workspace,
                    psycopg_json(item.payload or {"text": item.text}),
                    item.occurred_at.astimezone(timezone.utc),
                ),
            )
            event_id = cur.fetchone()[0]

            for idx, piece in enumerate(chunk(item.text)):
                h = content_hash(piece)

                cur.execute(
                    "INSERT INTO chunks (content_hash, text) VALUES (%s, %s) "
                    "ON CONFLICT (content_hash) DO NOTHING",
                    (h, piece),
                )

                # The dedup gate: embed only if this exact content is new.
                cur.execute(
                    "SELECT 1 FROM embeddings WHERE content_hash = %s", (h,)
                )
                if cur.fetchone() is None:
                    cur.execute(
                        "INSERT INTO embeddings (content_hash, model, embedding) "
                        "VALUES (%s, %s, %s) ON CONFLICT (content_hash) DO NOTHING",
                        (h, model_name(), embed(piece)),
                    )
                    stats.embeddings_computed += 1
                else:
                    stats.embeddings_reused += 1

                # Permission changes patch this row only -- never re-embed.
                # Re-ingestion is a NO-OP on an existing chunk's ACL: it can
                # neither widen nor narrow. Any ACL change must come through
                # an explicit, authenticated endpoint (not yet built). Without
                # this guard, an unauthenticated caller can re-ingest the
                # same content with permissive principals to either demote
                # restricted content or grant themselves access.
                cur.execute(
                    """
                    INSERT INTO chunk_acl (content_hash, workspace, allowed_principals)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (content_hash, workspace) DO NOTHING
                    """,
                    (h, workspace, item.allowed_principals),
                )

                cur.execute(
                    """
                    INSERT INTO event_chunks (event_id, chunk_index, content_hash)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (event_id, chunk_index)
                    DO UPDATE SET content_hash = EXCLUDED.content_hash
                    """,
                    (event_id, idx, h),
                )
                stats.chunks += 1
        conn.commit()
    return stats


def psycopg_json(obj: dict):
    from psycopg.types.json import Jsonb

    return Jsonb(obj)
