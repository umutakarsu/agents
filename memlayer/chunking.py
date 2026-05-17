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


_HEADING = re.compile(r"^#{1,6}\s", re.MULTILINE)


def _sections(text: str) -> list[str]:
    # Split on markdown headings so a repeated section is content-addressed
    # on its own. This is where real cross-source dedup pays off: boilerplate,
    # quoted threads and reply chains recur as identical *sections*, not
    # identical whole documents.
    bounds = [m.start() for m in _HEADING.finditer(text)]
    if not bounds:
        return [text]
    if bounds[0] != 0:
        bounds.insert(0, 0)
    bounds.append(len(text))
    return [text[bounds[i] : bounds[i + 1]] for i in range(len(bounds) - 1)]


def chunk(text: str, max_chars: int = 1200) -> list[str]:
    # Heading-aware, then paragraph-greedy within each section: pack paragraphs
    # up to max_chars so a one-paragraph edit invalidates one chunk, while a
    # repeated section dedupes across documents/sources.
    chunks: list[str] = []
    for section in _sections(text):
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", section) if p.strip()]
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
