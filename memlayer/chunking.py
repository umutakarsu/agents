"""Chunking + content addressing. The content_hash is the dedup key for the
whole pipeline: same normalized text => same hash => stored & embedded once."""

import hashlib
import re

_WS = re.compile(r"[ \t]+")


def normalize(text: str) -> str:
    # Whitespace-normalize so trivial reformatting doesn't bust the hash.
    lines = [_WS.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def chunk(text: str, max_chars: int = 1200) -> list[str]:
    # Paragraph-greedy: pack paragraphs up to max_chars. Keeps semantically
    # related text together so a one-paragraph edit invalidates one chunk.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paragraphs:
        if buf and len(buf) + len(p) + 2 > max_chars:
            chunks.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks
