"""Ingest-boundary privacy filter. Secrets (and optionally PII) are matched
with regexes and replaced with ``[REDACTED:<kind>]`` BEFORE the text is
chunked, embedded, or written to the chunks table. The system can never
surface in search what it never stored.

Each matcher is a (kind, compiled_pattern, post_filter) triple. ``post_filter``
runs over the regex's match to reject false positives (Luhn check for credit
cards, base64url shape check for JWTs, etc.). The order of matchers matters:
broader patterns must run after more specific ones (e.g. ``sk-ant-...`` is
matched as ``anthropic_api_key`` BEFORE the generic ``sk-...`` shape catches
it as ``openai_api_key``).

Cost story: this is pure stdlib regex, no network, no extra deps. It runs
once per SourceItem at ingest. Embeddings are computed from the scrubbed
text, so a secret pasted into a Slack thread is never embedded -- and
because we never embedded it, we can never search it back out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Redaction:
    """A summary record. Note: deliberately does NOT carry the matched text --
    the whole point of this module is that the scrubbed content never leaves
    this function. ``kind`` says WHAT class of secret was found, ``count``
    says how many, and ``chars`` is the total length of redacted text (so
    an admin can see roughly how much was stripped). That's everything
    needed to triage a leak source without seeing the leak itself."""

    kind: str
    count: int
    chars: int = 0


# ---------------------------------------------------------------------------
# Secret matchers (always on)
# ---------------------------------------------------------------------------
# Each entry is (kind, regex). The regex is run with re.findall semantics
# via re.sub; the iteration order here is the application order, which is
# load-bearing for the prefix-overlap cases below.

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)

# JWT: three dot-separated base64url segments, header starts with eyJ.
# The {10,} lower bounds keep us from matching random dotted-identifier-like
# strings; a real JWT header decodes to >=15 chars of JSON.
_JWT = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
)

_AWS_ACCESS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")

# AWS secret: a 40-char base64-like string that appears near an aws_secret-ish
# label OR near an AKIA access key id. Anchoring on context avoids the huge
# false-positive rate of an unanchored 40-char [A-Za-z0-9/+=] match.
_AWS_SECRET_LABELED = re.compile(
    r"(?i)(?:aws[_\-]?secret(?:[_\-]?access)?[_\-]?key|secret[_\-]?access[_\-]?key)"
    r"\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"
)

_ANTHROPIC_KEY = re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b")
# OpenAI: must run AFTER anthropic so sk-ant- doesn't get swallowed first.
_OPENAI_PROJ_KEY = re.compile(r"\bsk-proj-[A-Za-z0-9\-_]{20,}\b")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")

_GITHUB_TOKEN = re.compile(r"\bgh[posu]_[A-Za-z0-9]{36}\b")

_STRIPE_KEY = re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b")

# Order matters: most specific first.
_SECRET_MATCHERS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key_block", _PRIVATE_KEY),
    ("jwt", _JWT),
    ("aws_access_key", _AWS_ACCESS_KEY),
    ("aws_secret_key", _AWS_SECRET_LABELED),
    ("anthropic_api_key", _ANTHROPIC_KEY),
    ("openai_api_key", _OPENAI_PROJ_KEY),
    ("stripe_key", _STRIPE_KEY),
    ("openai_api_key", _OPENAI_KEY),
    ("github_token", _GITHUB_TOKEN),
]


# ---------------------------------------------------------------------------
# PII matchers (opt-in: scrub_pii=True)
# ---------------------------------------------------------------------------
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# 13-19 digit sequences, possibly with spaces or hyphens between groups.
# We Luhn-check after extraction.
_CREDIT_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn check. ``digits`` must be the digit-only string."""
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        n = ord(ch) - 48
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _scrub_credit_card(text: str) -> tuple[str, int, int]:
    count = 0
    chars = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal count, chars
        digits = re.sub(r"[ \-]", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            count += 1
            chars += len(m.group(0))
            return "[REDACTED:credit_card]"
        return m.group(0)

    return _CREDIT_CARD.sub(repl, text), count, chars


def scrub(text: str, *, scrub_pii: bool = False) -> tuple[str, list[Redaction]]:
    """Replace secrets in ``text`` with ``[REDACTED:<kind>]`` placeholders.

    Returns the scrubbed text and a list of ``Redaction(kind, count)``
    records, one per kind that fired. The records carry counts, not content
    -- the matched bytes never leave this function.

    Secrets (always on): AWS access keys, AWS secret keys (label-anchored),
    OpenAI / Anthropic / Stripe / GitHub tokens, JWTs, PEM-style private key
    blocks.

    PII (off by default; pass ``scrub_pii=True`` to enable): emails, US-style
    SSNs, credit-card numbers (Luhn-verified). PII is opt-in because emails
    and similar identifiers are often legitimate retrieval signal.
    """
    if not text:
        return text, []

    redactions: list[Redaction] = []
    scrubbed = text

    for kind, pattern in _SECRET_MATCHERS:
        # finditer first so we can count matches and total matched length
        # for IngestStats.scrubbed_chars accounting. We do the substitution
        # in a second pass via re.sub so capture-group-bearing patterns
        # (aws_secret_key) replace the whole match, not just the group.
        matches = list(pattern.finditer(scrubbed))
        if not matches:
            continue
        total_chars = sum(len(m.group(0)) for m in matches)
        scrubbed = pattern.sub(f"[REDACTED:{kind}]", scrubbed)
        redactions.append(Redaction(kind=kind, count=len(matches), chars=total_chars))

    if scrub_pii:
        # Credit cards: Luhn-checked, so we hand-roll the substitution.
        scrubbed, cc_count, cc_chars = _scrub_credit_card(scrubbed)
        if cc_count:
            redactions.append(
                Redaction(kind="credit_card", count=cc_count, chars=cc_chars)
            )

        ssn_matches = list(_SSN.finditer(scrubbed))
        if ssn_matches:
            scrubbed = _SSN.sub("[REDACTED:ssn]", scrubbed)
            redactions.append(
                Redaction(
                    kind="ssn",
                    count=len(ssn_matches),
                    chars=sum(len(m.group(0)) for m in ssn_matches),
                )
            )

        email_matches = list(_EMAIL.finditer(scrubbed))
        if email_matches:
            scrubbed = _EMAIL.sub("[REDACTED:email]", scrubbed)
            redactions.append(
                Redaction(
                    kind="email",
                    count=len(email_matches),
                    chars=sum(len(m.group(0)) for m in email_matches),
                )
            )

    return scrubbed, redactions
