"""Gmail scratch-account driver.

Seed: ensure the seeded labels exist, then import every seeded message as an RFC 5322 document through
`users.messages.import` (headers supply `From`, `Subject`, `Date` and threading; nothing is sent to anyone).
Gmail only stores mail for the authenticated mailbox, so the seeded recipient is rewritten to the scratch
account's own address; the rewrite is recorded in the seed notes and disclosed in the reliability brief.

Snapshot: every message (decoded headers, text body, labels, thread) and every draft, decoded the same way.
Reset: delete every draft, then batch-delete every message in the account. The mailbox exists only for these
rehearsals, and the driver refuses to run without `BENCHPRESS_SCRATCH_OK=1`.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Any, cast

import httpx

from evals.realapps.base import RealAppClient, SeedManifest, SeedResult, require_scratch_ok
from evals.realapps.gmail_auth import HeadersProvider

BASE_URL = "https://gmail.googleapis.com"
API = "/gmail/v1/users/me"
MESSAGE_SPACING = timedelta(minutes=5)
BATCH_DELETE_LIMIT = 1000
LIST_PAGE_SIZE = 500
MAX_PAGES = 200
SYSTEM_LABELS = frozenset(
    {
        "INBOX",
        "SENT",
        "DRAFT",
        "SPAM",
        "TRASH",
        "UNREAD",
        "STARRED",
        "IMPORTANT",
        "CHAT",
        "CATEGORY_PERSONAL",
        "CATEGORY_SOCIAL",
        "CATEGORY_PROMOTIONS",
        "CATEGORY_UPDATES",
        "CATEGORY_FORUMS",
    }
)
_SUBJECT_PREFIX = re.compile(r"^\s*(?:(?:re|fwd?|aw|wg)\s*:\s*)+", re.IGNORECASE)


class GmailSeedError(RuntimeError):
    def __init__(self, method: str, path: str, response: httpx.Response) -> None:
        super().__init__(f"gmail {method} {path} -> HTTP {response.status_code}: {response.text[:300]}")
        self.status_code = response.status_code


# ------------------------------------------------------------------------------------ RFC 5322


def build_raw_message(
    *,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    date: datetime,
    message_id: str,
    in_reply_to: str | None = None,
) -> str:
    """A base64url-encoded RFC 5322 message: plain text, UTF-8, CRLF line endings."""
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = format_datetime(date)
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    message.set_content(body, charset="utf-8")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def message_dates(seeded_at: float, count: int) -> list[datetime]:
    """Deterministic dates: `count` messages ending `MESSAGE_SPACING` before the seed minute, in seed order."""
    base = datetime.fromtimestamp(seeded_at, UTC).replace(second=0, microsecond=0)
    return [base - MESSAGE_SPACING * (count - index) for index in range(count)]


def message_id_for(seeded_at: float, index: int, sender: str, subject: str) -> str:
    digest = hashlib.sha256(f"{sender}\n{subject}".encode()).hexdigest()[:12]
    return f"<bp.{int(seeded_at)}.{index:03d}.{digest}@benchpress.seed>"


def normalized_subject(subject: str) -> str:
    return _SUBJECT_PREFIX.sub("", subject).strip().casefold()


def decode_base64url(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def _decode_text(data: str) -> str:
    """Body text with RFC 5322 CRLF line endings normalized, so assertions compare against seeded text."""
    return decode_base64url(data).decode("utf-8", errors="replace").replace("\r\n", "\n")


def _text_body(part: Mapping[str, Any]) -> str:
    """Prefer the first text/plain part, then text/html, then whatever body data the part carries."""
    mime = str(part.get("mimeType", ""))
    body = cast(Mapping[str, Any], part.get("body") or {})
    data = body.get("data")
    if isinstance(data, str) and data and mime.startswith("text/plain"):
        return _decode_text(data)
    parts = cast(list[Any], part.get("parts") or [])
    for wanted in ("text/plain", "text/html"):
        for child in parts:
            if isinstance(child, Mapping) and str(cast(Mapping[str, Any], child).get("mimeType", "")).startswith(
                wanted
            ):
                text = _text_body(cast(Mapping[str, Any], child))
                if text:
                    return text
    for child in parts:
        if isinstance(child, Mapping):
            text = _text_body(cast(Mapping[str, Any], child))
            if text:
                return text
    if isinstance(data, str) and data:
        return _decode_text(data)
    return ""


def decode_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a `format=full` message into the fields the assertions read."""
    payload = cast(Mapping[str, Any], message.get("payload") or {})
    headers: dict[str, str] = {}
    for header in cast(list[Any], payload.get("headers") or []):
        if isinstance(header, Mapping):
            typed = cast(Mapping[str, Any], header)
            headers.setdefault(str(typed.get("name", "")).lower(), str(typed.get("value", "")))
    return {
        "id": str(message.get("id", "")),
        "threadId": str(message.get("threadId", "")),
        "labelIds": [str(label) for label in cast(list[Any], message.get("labelIds") or [])],
        "internalDate": str(message.get("internalDate", "")),
        "snippet": str(message.get("snippet", "")),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "message_id": headers.get("message-id", ""),
        "in_reply_to": headers.get("in-reply-to", ""),
        "body": _text_body(payload),
    }


def _chunks(items: Sequence[str], size: int) -> list[list[str]]:
    return [list(items[start : start + size]) for start in range(0, len(items), size)]


# ---------------------------------------------------------------------------------------- driver


class GmailApp:
    app = "gmail"

    def __init__(self, headers_provider: HeadersProvider, *, address: str, base_url: str = BASE_URL) -> None:
        if "@" not in address:
            raise ValueError("address must be the scratch account's email address")
        self.address = address
        self._headers_provider = headers_provider
        self._client = RealAppClient(base_url, {})

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: object | None = None,
        expect: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        self._client.set_headers(dict(await self._headers_provider()))
        response = await self._client.request(method, path, params=params, json_body=json_body)
        if response.status_code not in expect:
            raise GmailSeedError(method, path, response)
        return response

    # -- seed

    async def seed(self, seed_config: Mapping[str, Any], manifest: SeedManifest) -> SeedResult:
        require_scratch_ok()
        section = cast(Mapping[str, Any], seed_config.get("gmail") or {})
        messages = [cast(Mapping[str, Any], item) for item in cast(list[Any], section.get("messages") or [])]
        drafts = [cast(Mapping[str, Any], item) for item in cast(list[Any], section.get("drafts") or [])]

        wanted_labels: list[str] = []
        for label in cast(list[Any], section.get("labels") or []):
            name = str(cast(Mapping[str, Any], label).get("name", "")) if isinstance(label, Mapping) else str(label)
            if name:
                wanted_labels.append(name)
        for spec in messages:
            for name in cast(list[Any], spec.get("labels") or []):
                if str(name) not in SYSTEM_LABELS:
                    wanted_labels.append(str(name))
        label_ids = await self._ensure_labels(list(dict.fromkeys(wanted_labels)), manifest)

        dates = message_dates(manifest.seeded_at, len(messages) + len(drafts))
        anchors: dict[str, tuple[str, str, str]] = {}  # seed thread -> (Message-ID, Gmail threadId, subject key)
        original_recipients: set[str] = set()
        for index, spec in enumerate(messages):
            sender = str(spec.get("from", ""))
            subject = str(spec.get("subject", ""))
            original_recipients.update(str(item) for item in cast(list[Any], spec.get("to") or []))
            message_id = message_id_for(manifest.seeded_at, index, sender, subject)
            thread_key = str(spec.get("thread_id") or message_id)
            anchor = anchors.get(thread_key)
            raw = build_raw_message(
                sender=sender,
                recipient=self.address,
                subject=subject,
                body=str(spec.get("body", "")),
                date=dates[index],
                message_id=message_id,
                in_reply_to=anchor[0] if anchor else None,
            )
            body: dict[str, Any] = {"raw": raw, "labelIds": self._label_ids_for(spec, label_ids)}
            if anchor and anchor[2] == normalized_subject(subject):
                body["threadId"] = anchor[1]
            response = await self._request(
                "POST",
                f"{API}/messages/import",
                params={"internalDateSource": "dateHeader", "neverMarkSpam": "true"},
                json_body=body,
            )
            created = cast(Mapping[str, Any], response.json())
            gmail_id = str(created["id"])
            gmail_thread = str(created.get("threadId", ""))
            manifest.add("gmail", "messages", gmail_id)
            if anchor is None:
                anchors[thread_key] = (message_id, gmail_thread, normalized_subject(subject))
                manifest.alias("gmail", f"thread:{thread_key}", gmail_thread)

        for offset, spec in enumerate(drafts):
            index = len(messages) + offset
            recipients = cast(list[Any], spec.get("to") or [])
            raw = build_raw_message(
                sender=str(spec.get("from", self.address)),
                recipient=", ".join(str(item) for item in recipients) or self.address,
                subject=str(spec.get("subject", "")),
                body=str(spec.get("body", "")),
                date=dates[index],
                message_id=message_id_for(manifest.seeded_at, index, self.address, str(spec.get("subject", ""))),
            )
            response = await self._request("POST", f"{API}/drafts", json_body={"message": {"raw": raw}})
            manifest.add("gmail", "drafts", str(cast(Mapping[str, Any], response.json())["id"]))

        notes: list[str] = []
        if messages:
            notes.append(
                f"recipient rewritten to {self.address} on {len(messages)} imported messages "
                f"(seed recipients: {', '.join(sorted(original_recipients)) or 'none'})"
            )
        return SeedResult(
            app="gmail",
            counts={"labels": len(label_ids), "messages": len(messages), "drafts": len(drafts)},
            notes=tuple(notes),
        )

    @staticmethod
    def _label_ids_for(spec: Mapping[str, Any], label_ids: Mapping[str, str]) -> list[str]:
        names = [str(name) for name in cast(list[Any], spec.get("labels") or ["INBOX"])]
        resolved: list[str] = []
        for name in names:
            if name in SYSTEM_LABELS:
                resolved.append(name)
            elif name in label_ids:
                resolved.append(label_ids[name])
            else:
                raise GmailSeedError("seed", "labels", httpx.Response(400, text=f"label {name!r} was not ensured"))
        return list(dict.fromkeys(resolved))

    async def _labels(self) -> list[dict[str, Any]]:
        payload = cast(Mapping[str, Any], (await self._request("GET", f"{API}/labels")).json())
        labels: list[dict[str, Any]] = []
        for item in cast(list[Any], payload.get("labels") or []):
            if isinstance(item, Mapping):
                typed = cast(Mapping[str, Any], item)
                labels.append(
                    {
                        "id": str(typed.get("id", "")),
                        "name": str(typed.get("name", "")),
                        "type": str(typed.get("type", "")),
                    }
                )
        return labels

    async def _ensure_labels(self, names: Sequence[str], manifest: SeedManifest) -> dict[str, str]:
        existing = {label["name"]: label["id"] for label in await self._labels()}
        ensured: dict[str, str] = {}
        for name in names:
            if name in existing:
                ensured[name] = existing[name]
            else:
                response = await self._request(
                    "POST",
                    f"{API}/labels",
                    json_body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
                    expect=(200, 201),
                )
                ensured[name] = str(cast(Mapping[str, Any], response.json())["id"])
            manifest.add("gmail", "labels", ensured[name])
            manifest.alias("gmail", f"label:{name}", ensured[name])
        return ensured

    # -- snapshot

    async def _page_ids(self, collection: str) -> list[str]:
        ids: list[str] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            params = {"maxResults": str(LIST_PAGE_SIZE)}
            if collection == "messages":
                params["includeSpamTrash"] = "true"
            if token:
                params["pageToken"] = token
            payload = cast(Mapping[str, Any], (await self._request("GET", f"{API}/{collection}", params=params)).json())
            for item in cast(list[Any], payload.get(collection) or []):
                if isinstance(item, Mapping):
                    ids.append(str(cast(Mapping[str, Any], item).get("id", "")))
            next_token = payload.get("nextPageToken")
            token = str(next_token) if next_token else None
            if not token:
                break
        return ids

    async def _message(self, message_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"{API}/messages/{message_id}", params={"format": "full"})
        return decode_message(cast(Mapping[str, Any], response.json()))

    async def _draft(self, draft_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"{API}/drafts/{draft_id}", params={"format": "full"})
        payload = cast(Mapping[str, Any], response.json())
        return {
            "id": str(payload.get("id", "")),
            "message": decode_message(cast(Mapping[str, Any], payload.get("message") or {})),
        }

    async def snapshot(self) -> dict[str, Any]:
        labels = await self._labels()
        messages = [await self._message(message_id) for message_id in await self._page_ids("messages")]
        drafts = [await self._draft(draft_id) for draft_id in await self._page_ids("drafts")]
        return {"address": self.address, "labels": labels, "messages": messages, "drafts": drafts}

    # -- reset

    async def reset(self, manifest: SeedManifest) -> None:
        """Wipe the scratch mailbox: every draft, then every message (the manifest is a subset of both)."""
        require_scratch_ok()
        del manifest
        for draft_id in await self._page_ids("drafts"):
            await self._request("DELETE", f"{API}/drafts/{draft_id}", expect=(200, 204, 404))
        for chunk in _chunks(await self._page_ids("messages"), BATCH_DELETE_LIMIT):
            await self._request("POST", f"{API}/messages/batchDelete", json_body={"ids": chunk}, expect=(200, 204))

    async def verify_clean(self) -> list[str]:
        residue: list[str] = []
        drafts = await self._page_ids("drafts")
        if drafts:
            residue.append(f"gmail drafts: {len(drafts)} remaining")
        messages = await self._page_ids("messages")
        if messages:
            residue.append(f"gmail messages: {len(messages)} remaining")
        return residue

    async def aclose(self) -> None:
        await self._client.aclose()
