"""Embedding providers behind one interface. The default 'local' provider is
offline and deterministic so the whole pipeline runs with no API keys -- it is
NOT semantically meaningful and is only for proving the dedup/ingest loop.
Swap EMBED_PROVIDER to 'voyage'/'openai' for real retrieval quality."""

import hashlib
import math
import re

from memlayer.config import EMBED_DIM, EMBED_PROVIDER

_TOKEN = re.compile(r"[a-z0-9]+")


def _local_embed(text: str) -> list[float]:
    # Hashed bag-of-words projected into EMBED_DIM, L2-normalized.
    vec = [0.0] * EMBED_DIM
    for tok in _TOKEN.findall(text.lower()):
        h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
        vec[h % EMBED_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _voyage_embed(text: str) -> list[float]:
    import voyageai

    client = voyageai.Client()
    return client.embed([text], model="voyage-3-lite").embeddings[0]


def _openai_embed(text: str) -> list[float]:
    from openai import OpenAI

    client = OpenAI()
    r = client.embeddings.create(
        model="text-embedding-3-small", input=text, dimensions=EMBED_DIM
    )
    return r.data[0].embedding


_PROVIDERS = {"local": _local_embed, "voyage": _voyage_embed, "openai": _openai_embed}


def embed(text: str) -> list[float]:
    return _PROVIDERS[EMBED_PROVIDER](text)


def model_name() -> str:
    return f"{EMBED_PROVIDER}:dim{EMBED_DIM}"
