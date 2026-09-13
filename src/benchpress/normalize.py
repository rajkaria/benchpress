"""Canonicalization helpers.

Every comparison Benchpress makes between a *claimed* value and an *observed* value goes
through this module, so that "done" never hinges on incidental formatting.
"""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w@.\-+ ]+", re.UNICODE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
_DOMAIN = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\b", re.IGNORECASE)

# Corporate suffixes and workflow qualifiers that must NOT make two records look alike.
# These are generic English business tokens, not task facts.
LOOKALIKE_QUALIFIERS: frozenset[str] = frozenset(
    {
        "archive",
        "archived",
        "backup",
        "copy",
        "demo",
        "deprecated",
        "draft",
        "eu",
        "emea",
        "former",
        "legacy",
        "old",
        "operations",
        "ops",
        "prospect",
        "replica",
        "sandbox",
        "staging",
        "test",
        "trial",
        "uat",
    }
)


def casefold_text(value: str) -> str:
    """Lowercase, strip accents, collapse whitespace. Used for human-readable comparison."""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WHITESPACE.sub(" ", stripped.casefold()).strip()


def canonical_text(value: str) -> str:
    """Casefold plus punctuation removal. The loosest comparison Benchpress will accept."""
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", casefold_text(value))).strip()


def canonical_email(value: str) -> str:
    """Emails compare exactly (after casefolding) — never fuzzily."""
    return value.strip().casefold()


def tokens(value: str) -> frozenset[str]:
    return frozenset(part for part in canonical_text(value).split(" ") if part)


def _singular(token: str) -> str:
    """Fold trivial English plurals so `studios` and `studio` collide.

    Deliberately crude: near-duplicate detection wants over-collision (which only ever adds
    a record to the protected set) rather than under-collision (which risks a wrong write).
    """
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("ses"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def significant_tokens(value: str) -> frozenset[str]:
    """Tokens with generic lookalike qualifiers removed.

    Two records whose *significant* tokens are equal but whose raw names differ are exactly
    the near-duplicate pairs that cause wrong-target writes, so this is how Benchpress finds
    them — without knowing anything about the specific entity.
    """
    return frozenset(_singular(token) for token in tokens(value) if token not in LOOKALIKE_QUALIFIERS)


def emails_in(text: str) -> frozenset[str]:
    return frozenset(match.group(0).casefold() for match in _EMAIL.finditer(text))


def urls_in(text: str) -> frozenset[str]:
    return frozenset(match.group(0) for match in _URL.finditer(text))


def domains_in(text: str) -> frozenset[str]:
    """Every domain-shaped token, including the host part of any email or URL."""
    found: set[str] = set()
    for match in _DOMAIN.finditer(text):
        found.add(match.group(0).casefold())
    for email in emails_in(text):
        found.add(email.split("@", 1)[1])
    return frozenset(found)


def domain_of(value: str) -> str | None:
    if "@" in value:
        return value.split("@", 1)[1].strip().casefold() or None
    match = _DOMAIN.search(value)
    return match.group(0).casefold() if match else None


def contains_term(haystack: str, needle: str) -> bool:
    """Substring containment under canonical text, used for protected-term detection."""
    needle_canonical = canonical_text(needle)
    if not needle_canonical:
        return False
    return needle_canonical in canonical_text(haystack)
