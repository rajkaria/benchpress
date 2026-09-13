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


# File extensions that are NOT delegated top-level domains. A bare `summary.pdf` is a file name,
# never a host. Extensions that are also real TLDs (.zip, .mov, .md, .py, .sh, ...) are deliberately
# absent: `report.zip` may be a host, so it stays a destination and the gate fails closed.
FILE_EXTENSIONS_NOT_TLDS: frozenset[str] = frozenset(
    {
        "avi", "bak", "bmp", "cfg", "conf", "crt", "css", "csv", "dmg", "doc", "docx", "eml", "exe",
        "flac", "gif", "heic", "htm", "html", "ics", "ini", "ipynb", "jpeg", "jpg", "json", "jsx",
        "log", "mkv", "mpeg", "mpg", "msi", "odp", "ods", "odt", "ogg", "pdf", "pem", "php", "png",
        "ppt", "pptx", "rar", "rtf", "scss", "sql", "svg", "tar", "tgz", "tif", "tiff", "tmp",
        "toml", "tsv", "tsx", "txt", "vcf", "wav", "webm", "webp", "xhtml", "xls", "xlsx", "xml",
        "yaml", "yml",
    }
)  # fmt: skip


def url_hosts_in(text: str) -> frozenset[str]:
    """The host of every http(s) URL in `text`, without userinfo or port."""
    hosts: set[str] = set()
    for url in urls_in(text):
        authority = re.split(r"[/?#]", url.split("://", 1)[1], maxsplit=1)[0]
        host = authority.rsplit("@", 1)[-1].split(":", 1)[0].casefold()
        if host:
            hosts.add(host)
    return frozenset(hosts)


def destination_domains_in(text: str) -> frozenset[str]:
    """Domains that plausibly name a destination: `domains_in` minus bare file names.

    A token is kept when it is an email or URL host, starts with `www.`, or ends in anything
    other than a file extension that is not a TLD. So `summary.pdf` and `index.html` are dropped,
    `files.example/summary.pdf` keeps `files.example`, and `ap@summary.pdf` or `report.zip` stay.
    """
    hosts = url_hosts_in(text) | {email.split("@", 1)[1] for email in emails_in(text)}
    kept: set[str] = set()
    for token in domains_in(text):
        if token in hosts or token.startswith("www.") or token.rsplit(".", 1)[-1] not in FILE_EXTENSIONS_NOT_TLDS:
            kept.add(token)
    return frozenset(kept)


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
