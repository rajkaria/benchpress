"""Gmail playbook (role `email`) — official Gmail REST shapes.

Reads list and decode messages and drafts (`format=full`, base64url bodies, multipart parts).
The only write this playbook constructs is the creation of an *unsent* draft
(`POST /gmail/v1/users/me/drafts`, RFC 2822 `raw`). It never constructs a message-send call
and never touches labels; the gate refuses those classes anyway, but a playbook must not even
offer them.
"""

from __future__ import annotations

import base64
import binascii
import html
import re
from collections.abc import Mapping, Sequence
from email import policy
from email.header import Header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, parseaddr
from typing import Any, cast

from benchpress.context import Action, ReadBack, TaskFrame
from benchpress.playbooks import (
    CREATED_ID_PLACEHOLDER,
    BasePlaybook,
    PolicySource,
    ProviderRecord,
    as_records,
    as_str,
    can_read,
    flat_fields,
    parse_ref,
    read_json,
)
from benchpress.tools import ToolBus, as_mapping

USERS_ME = "/gmail/v1/users/me"
MESSAGES_PATH = f"{USERS_ME}/messages"
DRAFTS_PATH = f"{USERS_ME}/drafts"

LIST_MAX_RESULTS = 50
MAX_POLICY_READS = 25
MAX_SEARCH_READS = 10
MAX_DRAFT_READS = 10

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")
_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


# --------------------------------------------------------------------------------------
# Pure codecs
# --------------------------------------------------------------------------------------


def b64url_decode(data: object) -> bytes:
    """Decode base64url with or without padding; anything undecodable is ``b""``."""
    if not isinstance(data, str) or not data.strip():
        return b""
    cleaned = "".join(data.split())
    padded = cleaned + "=" * (-len(cleaned) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return b""


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def strip_html(markup: str) -> str:
    text = html.unescape(_TAG.sub(" ", markup))
    text = _WHITESPACE.sub(" ", text)
    return _BLANK_LINES.sub("\n\n", "\n".join(line.strip() for line in text.split("\n"))).strip()


def _headers(part: Mapping[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header in as_records(part.get("headers")):
        name = as_str(header.get("name")).strip().casefold()
        if name and name not in headers:
            headers[name] = as_str(header.get("value")).strip()
    return headers


def _collect_text(part: Mapping[str, Any], plain: list[str], rich: list[str]) -> None:
    mime_type = as_str(part.get("mimeType")).casefold()
    data = b64url_decode(as_mapping(part.get("body")).get("data")).decode("utf-8", errors="replace")
    is_text = mime_type.startswith("text/") or not mime_type
    if data and is_text:
        bucket = rich if mime_type.startswith("text/html") else plain
        if data not in bucket:
            bucket.append(data)
    for sub_part in as_records(part.get("parts")):
        _collect_text(sub_part, plain, rich)


def _parse_raw(raw: object) -> tuple[dict[str, str], str]:
    data = b64url_decode(raw)
    if not data:
        return {}, ""
    message = BytesParser(policy=policy.default).parsebytes(data)
    headers = {str(key).casefold(): str(value) for key, value in message.items()}
    body = ""
    try:
        chosen = message.get_body(preferencelist=("plain", "html"))
    except (AttributeError, KeyError, TypeError):
        chosen = None
    if chosen is not None:
        content: object = chosen.get_content()
        body = content if isinstance(content, str) else as_str(content)
        if chosen.get_content_type() == "text/html":
            body = strip_html(body)
    elif not message.is_multipart():
        payload: object = message.get_payload(decode=True)
        body = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else as_str(payload)
    return headers, body


def decode_message(payload: Mapping[str, Any]) -> dict[str, object]:
    """Decode a `users.messages.get?format=full` resource (or a bare MIME payload) to flat text.

    Prefers `text/plain` parts, falls back to tag-stripped `text/html`, then to the RFC 2822
    `raw` field, then to the `snippet`.
    """
    message = payload if ("payload" in payload or "raw" in payload or "id" in payload) else {"payload": payload}
    mime = as_mapping(message.get("payload"))
    headers = _headers(mime)
    plain: list[str] = []
    rich: list[str] = []
    _collect_text(mime, plain, rich)
    body = "\n".join(plain).strip() or strip_html("\n".join(rich))
    if not body or not headers:
        raw_headers, raw_body = _parse_raw(message.get("raw"))
        headers = headers or raw_headers
        body = body or raw_body
    snippet = as_str(message.get("snippet")).strip()
    labels = [as_str(label) for label in cast(Sequence[object], message.get("labelIds") or [])]
    return {
        "id": as_str(message.get("id")),
        "thread_id": as_str(message.get("threadId")),
        "labels": labels,
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": snippet,
        "body": (body or snippet).strip(),
        "mime_type": as_str(mime.get("mimeType")),
        "headers": headers,
    }


def decode_draft(payload: Mapping[str, Any]) -> dict[str, object]:
    """Decode a `users.drafts.get?format=full` resource to `{id, to, subject, body, labels, …}`."""
    message = decode_message(as_mapping(payload.get("message")))
    return {
        "id": as_str(payload.get("id")),
        "message_id": message["id"],
        "thread_id": message["thread_id"],
        "to": message["to"],
        "cc": message["cc"],
        "subject": message["subject"],
        "body": message["body"],
        "labels": message["labels"],
        "snippet": message["snippet"],
    }


def _clean_header(value: str) -> str:
    """Collapse whitespace and strip line breaks so a value can never smuggle a second header."""
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def _encode_header(value: str) -> str:
    cleaned = _clean_header(value)
    return cleaned if cleaned.isascii() else Header(cleaned, "utf-8").encode()


def format_recipients(value: str) -> str:
    """Normalize a recipient list: one `Name <addr>` or bare `addr` per entry, RFC 2047 names.

    Line breaks are collapsed first, so a value can never smuggle a second header; an entry that
    is not a well-formed mailbox keeps only its first address-shaped token.
    """
    formatted: list[str] = []
    for entry in _clean_header(value).split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        name, address = parseaddr(candidate)
        if not _ADDRESS.fullmatch(address):
            match = _ADDRESS.search(candidate)
            if match is None:
                continue
            name, address = "", match.group(0)
        formatted.append(formataddr((name, address)) if name else address)
    return ", ".join(formatted)


def encode_rfc2822(to: str, subject: str, body: str) -> str:
    """Build a `text/plain; charset=utf-8` RFC 2822 message and return it base64url-encoded.

    The body is carried verbatim (7bit/8bit, no transfer encoding) so that whatever stores the
    draft sees the exact text the plan promised.
    """
    recipients = format_recipients(to)
    lines = [
        f"To: {recipients}",
        f"Subject: {_encode_header(subject)}",
        "MIME-Version: 1.0",
        'Content-Type: text/plain; charset="utf-8"',
        f"Content-Transfer-Encoding: {'7bit' if body.isascii() else '8bit'}",
    ]
    normalized_body = "\r\n".join(body.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    raw = "\r\n".join(lines) + "\r\n\r\n" + normalized_body
    return b64url_encode(raw.encode("utf-8"))


def parse_rfc2822(raw: str) -> EmailMessage:
    """Inverse of `encode_rfc2822` for tests and receipts."""
    return BytesParser(policy=policy.default).parsebytes(b64url_decode(raw))


def policy_source_from(message: Mapping[str, object]) -> PolicySource:
    subject = as_str(message.get("subject"))
    body = as_str(message.get("body"))
    message_id = as_str(message.get("id"))
    return PolicySource(
        provider="gmail",
        resource_ref=f"message:{message_id}",
        title=subject,
        text=f"{subject}\n{body}".strip(),
        author=as_str(message.get("from")),
        path=f"{MESSAGES_PATH}/{message_id}",
    )


def _record(decoded: Mapping[str, object], resource_type: str, raw: Mapping[str, Any]) -> ProviderRecord:
    fields = flat_fields({key: value for key, value in decoded.items() if key != "headers"})
    return ProviderRecord(
        provider="gmail",
        resource_type=resource_type,
        resource_id=as_str(decoded.get("id")),
        fields=fields,
        raw=dict(raw),
    )


# --------------------------------------------------------------------------------------
# The playbook
# --------------------------------------------------------------------------------------


class GmailPlaybook(BasePlaybook):
    provider: str = "gmail"
    role: str = "email"
    identity_fields: tuple[str, ...] = ("from", "to", "subject")

    # -- reads -------------------------------------------------------------------------

    async def list_message_ids(
        self, bus: ToolBus, *, query: str | None = None, max_results: int = LIST_MAX_RESULTS
    ) -> list[str]:
        params: dict[str, str] = {"maxResults": str(max(1, max_results))}
        if query and query.strip():
            params["q"] = query.strip()
        payload = await read_json(bus, self.provider, MESSAGES_PATH, query=params)
        if payload is None:
            return []
        return [as_str(entry.get("id")) for entry in as_records(payload.get("messages")) if as_str(entry.get("id"))]

    async def get_message(self, bus: ToolBus, message_id: str) -> dict[str, object] | None:
        if not message_id.strip():
            return None
        payload = await read_json(bus, self.provider, f"{MESSAGES_PATH}/{message_id.strip()}", query={"format": "full"})
        return decode_message(payload) if payload is not None else None

    async def read_messages(self, bus: ToolBus, message_ids: Sequence[str], cap: int) -> list[dict[str, object]]:
        decoded: list[dict[str, object]] = []
        for message_id in message_ids[: max(0, cap)]:
            if not can_read(bus):
                break
            message = await self.get_message(bus, message_id)
            if message is not None:
                decoded.append(message)
        return decoded

    async def search_messages(self, bus: ToolBus, query: str) -> list[dict[str, object]]:
        """Gmail search (`q=`), decoded. Bounded to `MAX_SEARCH_READS` full reads."""
        if not query.strip():
            return []
        message_ids = await self.list_message_ids(bus, query=query, max_results=MAX_SEARCH_READS)
        return await self.read_messages(bus, message_ids, MAX_SEARCH_READS)

    async def policy_sources(self, bus: ToolBus, frame: TaskFrame) -> list[PolicySource]:
        message_ids = await self.list_message_ids(bus)
        messages = await self.read_messages(bus, message_ids, MAX_POLICY_READS)
        return [policy_source_from(message) for message in messages]

    async def get_draft(self, bus: ToolBus, draft_id: str) -> dict[str, object] | None:
        if not draft_id.strip():
            return None
        payload = await read_json(bus, self.provider, f"{DRAFTS_PATH}/{draft_id.strip()}", query={"format": "full"})
        return decode_draft(payload) if payload is not None else None

    async def list_drafts(self, bus: ToolBus) -> list[dict[str, object]]:
        payload = await read_json(bus, self.provider, DRAFTS_PATH, query={"maxResults": str(LIST_MAX_RESULTS)})
        if payload is None:
            return []
        drafts: list[dict[str, object]] = []
        for entry in as_records(payload.get("drafts"))[:MAX_DRAFT_READS]:
            draft_id = as_str(entry.get("id"))
            if not draft_id:
                continue
            if not can_read(bus):
                break
            draft = await self.get_draft(bus, draft_id)
            if draft is not None:
                drafts.append(draft)
        return drafts

    async def read_record(self, bus: ToolBus, ref: str) -> ProviderRecord | None:
        parsed = parse_ref(ref)
        if parsed is None:
            return None
        resource_type, resource_id = parsed
        if resource_type in {"message", "messages"}:
            payload = await read_json(bus, self.provider, f"{MESSAGES_PATH}/{resource_id}", query={"format": "full"})
            return _record(decode_message(payload), "message", payload) if payload is not None else None
        if resource_type in {"draft", "drafts"}:
            payload = await read_json(bus, self.provider, f"{DRAFTS_PATH}/{resource_id}", query={"format": "full"})
            return _record(decode_draft(payload), "draft", payload) if payload is not None else None
        return None

    # -- writes ------------------------------------------------------------------------

    def draft_action(
        self,
        action_id: str,
        to: str,
        subject: str,
        body: str,
        satisfies: Sequence[str],
        *,
        target_refs: Sequence[str] = (),
    ) -> Action | None:
        recipients = format_recipients(to)
        if not action_id or not recipients or (not subject.strip() and not body.strip()):
            return None
        refs = tuple(dict.fromkeys([*target_refs, *(f"recipient:{address}" for address in _addresses(recipients))]))
        return Action(
            id=action_id,
            kind="draft",
            provider=self.provider,
            method="POST",
            path=DRAFTS_PATH,
            body={"message": {"raw": encode_rfc2822(recipients, subject, body)}},
            body_encoding="json",
            fields=("message", "raw"),
            satisfies=tuple(satisfies),
            target_refs=refs,
            readback=ReadBack(
                method="GET",
                path=f"{DRAFTS_PATH}/{CREATED_ID_PLACEHOLDER}",
                query={"format": "full"},
                field_path="message.snippet",
                unobserved=("raw",),
            ),
            rationale=f"save an unsent draft addressed to {recipients}",
        )


def _addresses(recipients: str) -> list[str]:
    return [match.group(0).casefold() for match in _ADDRESS.finditer(recipients)]


__all__ = [
    "DRAFTS_PATH",
    "MAX_POLICY_READS",
    "MESSAGES_PATH",
    "GmailPlaybook",
    "b64url_decode",
    "b64url_encode",
    "decode_draft",
    "decode_message",
    "encode_rfc2822",
    "format_recipients",
    "parse_rfc2822",
    "policy_source_from",
    "strip_html",
]
