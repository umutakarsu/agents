from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector

from memlayer.config import DATABASE_URL


@contextmanager
def connect():
    with psycopg.connect(DATABASE_URL) as conn:
        register_vector(conn)
        yield conn
