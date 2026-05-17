from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector

from memlayer.config import DATABASE_URL


@contextmanager
def connect():
    with psycopg.connect(DATABASE_URL) as conn:
        # Bootstrap: the pgvector type must exist before register_vector can
        # introspect it. Idempotent, so safe on every connection.
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()
        register_vector(conn)
        yield conn
