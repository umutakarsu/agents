import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/memlayer")
EMBED_PROVIDER = os.environ.get("EMBED_PROVIDER", "local")
EMBED_DIM = 256  # must match VECTOR(n) in schema.sql
