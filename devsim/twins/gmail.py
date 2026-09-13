"""Gmail twin: the `email` role of ArgaBench scenarios, served locally.

Calibrated against the Arga Gmail twin recorded in the benchmark's CRM legacy fixture
(`tests/fixtures/argabench_crm_legacy/historical-fable-5-high-crm.tar.gz`), see
`devsim/calibration/gmail/NOTES.md` for the route-by-route provenance. In short:

- one mailbox (`owner@gmail-twin.local`) holding `drafts, history, labels, messages, settings, watches`;
- message ids `msg_<14 hex>`, label ids `Label_<14 hex>`, thread ids = the seed's `thread_id`;
- seeded `internalDate` starts at 2026-01-01T00:00:00Z and steps 17 s per message, `historyId` from 1001;
- `raw` is a Python `EmailMessage` (quoted-printable, `\\n` line separators) and `payload` mirrors it with a
  synthetic `Message-ID` header plus a single `parts[0]` copy of the body;
- drafts live in a **list** (`sum(len(mailboxes[*].drafts))` is what the grader counts);
- send / modify / trash / delete work, so an unsafe trajectory is caught by the grader, not by a 404;
- reads are pure (no UNREAD flips, no history entries, no clock ticks);
- unknown routes answer `{"detail": "Not Found"}` like the recorded twin; known resources that are missing answer
  Google's error envelope.
"""

from __future__ import annotations

import base64
import binascii
import copy
import email
import email.policy
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage, Message
from typing import Any, ClassVar, cast

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from devsim.twins.base import Clock, Store, TwinSpec, det_alnum, json_response, make_admin_app, parse_body

OWNER_ADDRESS = "owner@gmail-twin.local"
TWIN_DOMAIN = "gmail-twin.local"
OWNER_DISPLAY_NAME = "Gmail Twin Owner"

# Recorded twin: first seeded message at 2026-01-01T00:00:00Z, then +17 s per message; history from 1001.
SEED_INTERNAL_DATE_MS = 1_767_225_600_000
SEED_INTERNAL_DATE_STEP_MS = 17_000
HISTORY_ID_START = 1000
SNIPPET_CHARS = 120
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500

# Exactly the system labels the recorded twin exposes, in its order.
SYSTEM_LABELS: tuple[str, ...] = ("INBOX", "SENT", "DRAFT", "TRASH", "UNREAD", "STARRED")
_HIDDEN_FROM_SEARCH: frozenset[str] = frozenset({"TRASH", "SPAM"})
_FORMATS: frozenset[str] = frozenset({"full", "metadata", "minimal", "raw"})
_SETTINGS_LIST_KEYS: dict[str, str] = {
    "sendAs": "sendAs",
    "filters": "filter",
    "forwardingAddresses": "forwardingAddresses",
    "delegates": "delegates",
}

ADMIN_PATHS: tuple[str, ...] = ("/admin/state", "/inspect", "/_admin/state")

_PATH = "/gmail/v1/users/{user}"
_MISSING_RAW = "'raw' RFC822 payload message string or uploading message via /upload/* URL required"


# --------------------------------------------------------------------------------------
# Errors (Google envelope, matching HTTP status)
# --------------------------------------------------------------------------------------


class GmailApiError(Exception):
    """Raised by store / handler code; converted into Google's error envelope."""

    def __init__(self, code: int, message: str, *, reason: str, status: str, extra: Mapping[str, str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.status = status
        self.extra = dict(extra or {})

    def payload(self) -> dict[str, Any]:
        detail: dict[str, Any] = {"message": self.message, "domain": "global", "reason": self.reason, **self.extra}
        return {"error": {"code": self.code, "message": self.message, "errors": [detail], "status": self.status}}


def not_found() -> GmailApiError:
    return GmailApiError(404, "Requested entity was not found.", reason="notFound", status="NOT_FOUND")


def invalid_argument(message: str) -> GmailApiError:
    return GmailApiError(400, message, reason="invalidArgument", status="INVALID_ARGUMENT")


def failed_precondition(message: str) -> GmailApiError:
    return GmailApiError(400, message, reason="failedPrecondition", status="FAILED_PRECONDITION")


def delegation_denied(user: str) -> GmailApiError:
    return GmailApiError(403, f"Delegation denied for {user}", reason="forbidden", status="PERMISSION_DENIED")


def login_required() -> GmailApiError:
    return GmailApiError(
        401,
        "Request is missing required authentication credential. Expected OAuth 2 access token, login cookie or "
        "other valid authentication credential. See https://developers.google.com/identity/sign-in/web/devconsole-project.",
        reason="required",
        status="UNAUTHENTICATED",
        extra={"location": "Authorization", "locationType": "header"},
    )


# --------------------------------------------------------------------------------------
# RFC 2822 helpers
# --------------------------------------------------------------------------------------


def b64url_encode(data: bytes) -> str:
    """Gmail's web-safe base64 without padding (the recorded twin strips `=`)."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    cleaned = re.sub(r"\s+", "", text)
    if not cleaned or re.search(r"[^A-Za-z0-9_\-+/=]", cleaned):
        raise GmailApiError(
            400, f"Invalid value for ByteString: {text[:40]}", reason="invalid", status="INVALID_ARGUMENT"
        )
    padded = cleaned + "=" * (-len(cleaned) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as error:
        raise GmailApiError(
            400, f"Invalid value for ByteString: {text[:40]}", reason="invalid", status="INVALID_ARGUMENT"
        ) from error


def compose_raw(sender: str, recipients: Sequence[str], subject: str, body: str, *, cc: Sequence[str] = ()) -> bytes:
    """Build the exact RFC 2822 bytes the recorded twin stores for a seeded message."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message.set_content(body, charset="utf-8", cte="quoted-printable")
    return message.as_bytes()


def parse_raw(raw_bytes: bytes) -> Message:
    return email.message_from_bytes(raw_bytes, policy=email.policy.default)


def _header_pairs(parsed: Message) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for name, value in parsed.items():
        pairs.append({"name": name, "value": str(value)})
    return pairs


def _decoded_payload(parsed: Message) -> bytes:
    payload = parsed.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8", errors="replace")
    return b""


def _text_of_part(part: Message) -> str:
    if part.get_content_maintype() != "text":
        return ""
    data = _decoded_payload(part)
    charset = part.get_content_charset() or "utf-8"
    try:
        text = data.decode(charset, errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    if part.get_content_subtype() == "html":
        text = re.sub(r"<[^>]+>", " ", text)
    return text


def _is_attachment(part: Message) -> bool:
    disposition = (part.get("Content-Disposition") or "").split(";", 1)[0].strip().casefold()
    return disposition == "attachment" or bool(part.get_filename())


def plain_text(parsed: Message) -> str:
    """The text Gmail would show: the first text/plain part, else stripped HTML, else nothing."""
    if not parsed.is_multipart():
        return _text_of_part(parsed)
    html_text = ""
    for part in parsed.walk():
        if part.is_multipart() or _is_attachment(part):
            continue
        if part.get_content_type() == "text/plain":
            return _text_of_part(part)
        if part.get_content_type() == "text/html" and not html_text:
            html_text = _text_of_part(part)
    return html_text


def make_snippet(text: str) -> str:
    return " ".join(text.split())[:SNIPPET_CHARS]


def header_value(record: Mapping[str, Any], name: str) -> str:
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    headers = cast(Mapping[str, Any], payload).get("headers")
    if not isinstance(headers, list):
        return ""
    wanted = name.casefold()
    for entry in cast(list[object], headers):
        if isinstance(entry, Mapping):
            typed = cast(Mapping[str, Any], entry)
            if str(typed.get("name", "")).casefold() == wanted:
                return str(typed.get("value", ""))
    return ""


def body_text(record: Mapping[str, Any]) -> str:
    """Decode the message text back out of the stored `payload` (search corpus; no caches, reads stay pure)."""
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    typed = cast(Mapping[str, Any], payload)
    parts = typed.get("parts")
    candidates: list[Mapping[str, Any]] = [typed]
    if isinstance(parts, list):
        stack = list(cast(list[object], parts))
        while stack:
            item = stack.pop(0)
            if isinstance(item, Mapping):
                item_typed = cast(Mapping[str, Any], item)
                candidates.append(item_typed)
                nested = item_typed.get("parts")
                if isinstance(nested, list):
                    stack.extend(cast(list[object], nested))
    html_fallback = ""
    for candidate in candidates:
        mime = str(candidate.get("mimeType", ""))
        body = candidate.get("body")
        if not isinstance(body, Mapping):
            continue
        data = cast(Mapping[str, Any], body).get("data")
        if not isinstance(data, str) or not data:
            continue
        try:
            text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            continue
        if mime == "text/plain":
            return text
        if mime == "text/html" and not html_fallback:
            html_fallback = re.sub(r"<[^>]+>", " ", text)
    return html_fallback


# --------------------------------------------------------------------------------------
# Search (`q`) — Gmail operators the agents actually use
# --------------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r'(?:-?[A-Za-z_]+:)?(?:"[^"]*"|\{[^}]*\}|\([^)]*\)|\S+)')
_KNOWN_OPERATORS: frozenset[str] = frozenset(
    {
        "from",
        "to",
        "cc",
        "bcc",
        "deliveredto",
        "subject",
        "label",
        "in",
        "is",
        "has",
        "filename",
        "rfc822msgid",
        "newer_than",
        "older_than",
        "after",
        "before",
        "newer",
        "older",
        "category",
        "size",
        "larger",
        "smaller",
        "list",
    }
)
_IN_LABELS: dict[str, str] = {
    "inbox": "INBOX",
    "sent": "SENT",
    "draft": "DRAFT",
    "drafts": "DRAFT",
    "trash": "TRASH",
    "spam": "SPAM",
    "starred": "STARRED",
    "important": "IMPORTANT",
    "unread": "UNREAD",
    "snoozed": "SNOOZED",
    "chats": "CHAT",
}
_ANYWHERE = frozenset({"anywhere", "all"})


@dataclass(frozen=True)
class QueryTerm:
    operator: str | None
    value: str
    negate: bool = False


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _split_token(token: str) -> QueryTerm:
    negate = token.startswith("-")
    if negate:
        token = token[1:]
    operator: str | None = None
    match = re.match(r"^([A-Za-z_]+):(.*)$", token, flags=re.DOTALL)
    if match and match.group(1).casefold() in _KNOWN_OPERATORS:
        operator = match.group(1).casefold()
        token = match.group(2)
    value = " ".join(_unquote(token).split()).casefold()
    return QueryTerm(operator, value, negate)


def parse_query(q: str) -> list[list[QueryTerm]]:
    """Return a conjunction of disjunctions: every inner list is a set of OR-ed alternatives."""
    groups: list[list[QueryTerm]] = []
    join_next = False
    for raw_token in _TOKEN_RE.findall(q):
        if raw_token in {"OR", "|"}:
            join_next = bool(groups)
            continue
        if raw_token.startswith("{") and raw_token.endswith("}"):
            alternatives = [_split_token(inner) for inner in _TOKEN_RE.findall(raw_token[1:-1]) if inner != "OR"]
            terms = [term for term in alternatives if term.value]
            if terms:
                groups.append(terms)
            join_next = False
            continue
        if raw_token.startswith("(") and raw_token.endswith(")"):
            groups.extend(parse_query(raw_token[1:-1]))
            join_next = False
            continue
        term = _split_token(raw_token)
        if not term.value and term.operator is None:
            continue
        if join_next and groups:
            groups[-1].append(term)
        else:
            groups.append([term])
        join_next = False
    return groups


def query_targets_hidden(groups: Sequence[Sequence[QueryTerm]]) -> bool:
    """`in:trash`, `in:spam`, `in:anywhere`, `label:trash` … widen the search to hidden labels."""
    for group in groups:
        for term in group:
            if term.negate:
                continue
            if term.operator == "in" and (term.value in _ANYWHERE or term.value in {"trash", "spam"}):
                return True
            if term.operator == "label" and term.value in {"trash", "spam"}:
                return True
    return False


# --------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------


@dataclass
class Mailbox:
    address: str
    drafts: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())
    history: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())
    labels: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())
    messages: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())
    settings: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    watches: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())

    def all_messages(self) -> list[dict[str, Any]]:
        """Messages plus draft messages: `messages.list` / `messages.get` see drafts as Gmail does."""
        return [*self.messages, *(draft["message"] for draft in self.drafts if isinstance(draft.get("message"), dict))]

    def find_message(self, message_id: str) -> dict[str, Any] | None:
        for record in self.all_messages():
            if record.get("id") == message_id:
                return record
        return None

    def find_draft(self, draft_id: str) -> dict[str, Any] | None:
        for draft in self.drafts:
            if draft.get("id") == draft_id:
                return draft
        return None

    def draft_for_message(self, message_id: str) -> dict[str, Any] | None:
        for draft in self.drafts:
            message = draft.get("message")
            if isinstance(message, dict) and cast(dict[str, Any], message).get("id") == message_id:
                return draft
        return None

    def find_label(self, label_id: str) -> dict[str, Any] | None:
        for label in self.labels:
            if label.get("id") == label_id:
                return label
        return None

    def label_ids(self) -> set[str]:
        return {str(label["id"]) for label in self.labels}

    def resolve_label(self, token: str) -> str | None:
        """Map `INBOX`, `Label_…`, `Operations`, `operations`, `my-label` to a label id (ids win over names)."""
        folded = token.casefold()
        for label in self.labels:
            if str(label["id"]).casefold() == folded:
                return str(label["id"])
        spaced = folded.replace("-", " ")
        for label in self.labels:
            name = str(label.get("name", "")).casefold()
            if name in {folded, spaced} or name.replace("-", " ") == spaced:
                return str(label["id"])
        return None

    def threads(self, records: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            grouped.setdefault(str(record["threadId"]), []).append(dict(record))
        return grouped


def default_settings(address: str) -> dict[str, Any]:
    return {
        "autoForwarding": {"enabled": False},
        "delegates": {},
        "filters": {},
        "forwardingAddresses": {},
        "imap": {"autoExpunge": True, "enabled": True, "expungeBehavior": "archive"},
        "language": {"displayLanguage": "en"},
        "pop": {"accessWindow": "disabled", "disposition": "leaveInInbox"},
        "sendAs": {
            address: {
                "displayName": OWNER_DISPLAY_NAME,
                "isDefault": True,
                "isPrimary": True,
                "replyToAddress": "",
                "sendAsEmail": address,
                "signature": "",
                "verificationStatus": "accepted",
            }
        },
        "vacation": {"enableAutoReply": False, "responseBodyPlainText": "", "responseSubject": ""},
    }


def system_label(name: str) -> dict[str, Any]:
    return {
        "id": name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
        "name": name,
        "type": "system",
    }


def _as_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in cast(list[object], value) if isinstance(item, str | int)]
    return []


def _gmail_slice(seed_config: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = seed_config.get("gmail")
    if isinstance(nested, Mapping):
        typed = cast(Mapping[str, Any], nested)
        if any(key in typed for key in ("messages", "labels", "drafts")):
            return typed
    return seed_config


class GmailStore(Store):
    provider: ClassVar[str] = "gmail"

    def __init__(self, seed_key: str, clock: Clock | None = None) -> None:
        super().__init__(seed_key, clock)
        self.mailboxes: dict[str, Mailbox] = {}
        self._history_counter = HISTORY_ID_START

    # -- mailboxes ---------------------------------------------------------------------

    def ensure_mailbox(self, address: str) -> Mailbox:
        mailbox = self.mailboxes.get(address)
        if mailbox is None:
            mailbox = Mailbox(address=address, settings=default_settings(address))
            mailbox.labels.extend(system_label(name) for name in SYSTEM_LABELS)
            self.mailboxes[address] = mailbox
        return mailbox

    def mailbox_for(self, user: str) -> Mailbox:
        if user == "me":
            if not self.mailboxes:
                return self.ensure_mailbox(OWNER_ADDRESS)
            return next(iter(self.mailboxes.values()))
        mailbox = self.mailboxes.get(user) or self.mailboxes.get(user.casefold())
        if mailbox is None:
            raise delegation_denied(user)
        return mailbox

    def current_history_id(self) -> str:
        return str(self._history_counter)

    def _next_history_id(self) -> str:
        self._history_counter += 1
        return str(self._history_counter)

    # -- seeding -----------------------------------------------------------------------

    def seed(self, seed_config: Mapping[str, Any]) -> None:
        gmail = _gmail_slice(seed_config)
        raw_messages = gmail.get("messages")
        messages = [
            cast(Mapping[str, Any], m) for m in cast(list[object], raw_messages or []) if isinstance(m, Mapping)
        ]
        owner = OWNER_ADDRESS
        for message in messages:
            recipients = _as_list(message.get("to"))
            if recipients:
                owner = recipients[0]
                break
        mailbox = self.ensure_mailbox(owner)
        raw_labels = gmail.get("labels")
        for entry in cast(list[object], raw_labels or []):
            name: object = cast(Mapping[str, Any], entry).get("name") if isinstance(entry, Mapping) else entry
            if isinstance(name, str) and name:
                self._ensure_user_label(mailbox, name)
        for index, message in enumerate(messages):
            self._seed_message(mailbox, message, index)
        raw_drafts = gmail.get("drafts")
        for entry in cast(list[object], raw_drafts or []):
            if isinstance(entry, Mapping):
                self._seed_draft(mailbox, cast(Mapping[str, Any], entry))

    def _ensure_user_label(self, mailbox: Mailbox, name: str) -> str:
        existing = mailbox.resolve_label(name)
        if existing is not None:
            return existing
        label_id = self.new_id("labels", kind="hex", length=14, prefix="Label_")
        mailbox.labels.append({"id": label_id, "name": name, "type": "user"})
        return label_id

    def _seed_label_ids(self, mailbox: Mailbox, names: Sequence[str]) -> list[str]:
        ids: list[str] = []
        for name in names:
            resolved = mailbox.resolve_label(name)
            if resolved is None:
                resolved = self._ensure_user_label(mailbox, name)
            if resolved not in ids:
                ids.append(resolved)
        return ids

    def _seed_message(self, mailbox: Mailbox, message: Mapping[str, Any], index: int) -> None:
        sender = str(message.get("from") or f"sender@{TWIN_DOMAIN}")
        recipients = _as_list(message.get("to")) or [mailbox.address]
        subject = str(message.get("subject") or "")
        body = str(message.get("body") or "")
        raw_bytes = compose_raw(sender, recipients, subject, body, cc=_as_list(message.get("cc")))
        message_id = self.new_id("messages", kind="hex", length=14, prefix="msg_")
        thread_id = str(message.get("thread_id") or message.get("threadId") or message_id)
        label_ids = self._seed_label_ids(mailbox, _as_list(message.get("labels")))
        record = self.build_message(
            mailbox,
            raw_bytes,
            message_id=message_id,
            thread_id=thread_id,
            label_ids=label_ids,
            history_id=self._next_history_id(),
            internal_date_ms=SEED_INTERNAL_DATE_MS + index * SEED_INTERNAL_DATE_STEP_MS,
        )
        mailbox.messages.append(record)
        mailbox.history.append(self._history_record(record, "messagesAdded"))

    def _seed_draft(self, mailbox: Mailbox, draft: Mapping[str, Any]) -> None:
        nested = draft.get("message")
        raw_value = cast(Mapping[str, Any], nested).get("raw") if isinstance(nested, Mapping) else draft.get("raw")
        if isinstance(raw_value, str) and raw_value:
            raw_bytes = b64url_decode(raw_value)
        else:
            raw_bytes = compose_raw(
                str(draft.get("from") or mailbox.address),
                _as_list(draft.get("to")),
                str(draft.get("subject") or ""),
                str(draft.get("body") or ""),
                cc=_as_list(draft.get("cc")),
            )
        message_id = self.new_id("messages", kind="hex", length=14, prefix="msg_")
        thread_id = str(draft.get("thread_id") or draft.get("threadId") or message_id)
        record = self.build_message(
            mailbox,
            raw_bytes,
            message_id=message_id,
            thread_id=thread_id,
            label_ids=["DRAFT"],
            history_id=self._next_history_id(),
            internal_date_ms=SEED_INTERNAL_DATE_MS + len(mailbox.all_messages()) * SEED_INTERNAL_DATE_STEP_MS,
        )
        mailbox.drafts.append({"id": self._new_draft_id(), "message": record})
        mailbox.history.append(self._history_record(record, "messagesAdded"))

    def _new_draft_id(self) -> str:
        return self.new_id("drafts", kind="digits", length=19, prefix="r-")

    # -- message resources -------------------------------------------------------------

    def build_message(
        self,
        mailbox: Mailbox,
        raw_bytes: bytes,
        *,
        message_id: str,
        thread_id: str,
        label_ids: Sequence[str],
        history_id: str,
        internal_date_ms: int,
    ) -> dict[str, Any]:
        """The stored (admin-state) message record: payload + raw + `_attachments`, keys as the recorded twin."""
        parsed = parse_raw(raw_bytes)
        attachments: dict[str, Any] = {}
        payload = self._build_payload(parsed, message_id, attachments)
        return {
            "_attachments": attachments,
            "historyId": history_id,
            "id": message_id,
            "internalDate": str(internal_date_ms),
            "labelIds": list(label_ids),
            "payload": payload,
            "raw": b64url_encode(raw_bytes),
            "sizeEstimate": len(raw_bytes),
            "snippet": make_snippet(plain_text(parsed)),
            "threadId": thread_id,
        }

    def _build_payload(self, parsed: Message, message_id: str, attachments: dict[str, Any]) -> dict[str, Any]:
        headers = _header_pairs(parsed)
        top_headers = list(headers)
        if not any(str(entry["name"]).casefold() == "message-id" for entry in top_headers):
            top_headers.append({"name": "Message-ID", "value": f"<{message_id}@{TWIN_DOMAIN}>"})
        if parsed.is_multipart():
            parts = [
                self._build_part(part, str(index), message_id, attachments)
                for index, part in enumerate(cast(list[Message], parsed.get_payload()))
            ]
            return {
                "body": {"size": 0},
                "filename": "",
                "headers": top_headers,
                "mimeType": parsed.get_content_type(),
                "partId": "",
                "parts": parts,
            }
        data = _decoded_payload(parsed)
        body = {"data": b64url_encode(data), "size": len(data)}
        # The recorded twin mirrors a single-part body as `parts[0]` (partId "0", same headers, no Message-ID).
        return {
            "body": dict(body),
            "filename": "",
            "headers": top_headers,
            "mimeType": parsed.get_content_type(),
            "partId": "",
            "parts": [
                {
                    "body": dict(body),
                    "filename": "",
                    "headers": headers,
                    "mimeType": parsed.get_content_type(),
                    "partId": "0",
                }
            ],
        }

    def _build_part(self, part: Message, part_id: str, message_id: str, attachments: dict[str, Any]) -> dict[str, Any]:
        headers = _header_pairs(part)
        if part.is_multipart():
            children = [
                self._build_part(child, f"{part_id}.{index}", message_id, attachments)
                for index, child in enumerate(cast(list[Message], part.get_payload()))
            ]
            return {
                "body": {"size": 0},
                "filename": "",
                "headers": headers,
                "mimeType": part.get_content_type(),
                "partId": part_id,
                "parts": children,
            }
        data = _decoded_payload(part)
        filename = part.get_filename() or ""
        if _is_attachment(part):
            attachment_id = det_alnum(self.seed_key, "attachment", message_id, part_id, length=48)
            attachments[attachment_id] = {"data": b64url_encode(data), "filename": filename, "size": len(data)}
            body: dict[str, Any] = {"attachmentId": attachment_id, "size": len(data)}
        else:
            body = {"data": b64url_encode(data), "size": len(data)}
        return {
            "body": body,
            "filename": filename,
            "headers": headers,
            "mimeType": part.get_content_type(),
            "partId": part_id,
        }

    def _history_record(self, record: Mapping[str, Any], kind: str, label_ids: Sequence[str] = ()) -> dict[str, Any]:
        stub = {"id": record["id"], "threadId": record["threadId"]}
        entry: dict[str, Any] = {"id": record["historyId"], "messages": [dict(stub)]}
        if kind in {"labelsAdded", "labelsRemoved"}:
            entry[kind] = [{"message": dict(stub), "labelIds": list(label_ids)}]
        else:
            entry[kind] = [{"message": dict(stub)}]
        return entry

    # -- writes ------------------------------------------------------------------------

    def validate_label_ids(self, mailbox: Mailbox, label_ids: Sequence[str]) -> list[str]:
        known = mailbox.label_ids()
        resolved: list[str] = []
        for label_id in label_ids:
            if label_id in known:
                resolved.append(label_id)
                continue
            alias = mailbox.resolve_label(label_id)
            if alias is None:
                raise invalid_argument(f"Invalid label: {label_id}")
            resolved.append(alias)
        return resolved

    def create_message(
        self,
        mailbox: Mailbox,
        raw_bytes: bytes,
        *,
        label_ids: Sequence[str],
        thread_id: str | None,
        method: str,
        path: str,
    ) -> dict[str, Any]:
        message_id = self.new_id("messages", kind="hex", length=14, prefix="msg_")
        mutation = self.record_mutation(
            method=method, path=path, collection="messages", record_id=message_id, before=None, after=None
        )
        record = self.build_message(
            mailbox,
            raw_bytes,
            message_id=message_id,
            thread_id=thread_id or message_id,
            label_ids=label_ids,
            history_id=self._next_history_id(),
            internal_date_ms=self.clock.epoch_ms(),
        )
        mutation.after = copy.deepcopy(record)
        mailbox.messages.append(record)
        mailbox.history.append(self._history_record(record, "messagesAdded"))
        return record

    def create_draft(
        self, mailbox: Mailbox, raw_bytes: bytes, *, thread_id: str | None, method: str, path: str
    ) -> dict[str, Any]:
        draft_id = self._new_draft_id()
        message_id = self.new_id("messages", kind="hex", length=14, prefix="msg_")
        mutation = self.record_mutation(
            method=method, path=path, collection="drafts", record_id=draft_id, before=None, after=None
        )
        record = self.build_message(
            mailbox,
            raw_bytes,
            message_id=message_id,
            thread_id=thread_id or message_id,
            label_ids=["DRAFT"],
            history_id=self._next_history_id(),
            internal_date_ms=self.clock.epoch_ms(),
        )
        draft = {"id": draft_id, "message": record}
        mutation.after = copy.deepcopy(draft)
        mailbox.drafts.append(draft)
        mailbox.history.append(self._history_record(record, "messagesAdded"))
        return draft

    def update_draft(
        self,
        mailbox: Mailbox,
        draft: dict[str, Any],
        raw_bytes: bytes,
        *,
        thread_id: str | None,
        method: str,
        path: str,
    ) -> dict[str, Any]:
        before = copy.deepcopy(draft)
        old_message = cast(dict[str, Any], draft["message"])
        message_id = self.new_id("messages", kind="hex", length=14, prefix="msg_")
        mutation = self.record_mutation(
            method=method, path=path, collection="drafts", record_id=str(draft["id"]), before=before, after=None
        )
        record = self.build_message(
            mailbox,
            raw_bytes,
            message_id=message_id,
            thread_id=thread_id or str(old_message["threadId"]),
            label_ids=list(cast(list[str], old_message.get("labelIds", ["DRAFT"]))),
            history_id=self._next_history_id(),
            internal_date_ms=self.clock.epoch_ms(),
        )
        draft["message"] = record
        mutation.after = copy.deepcopy(draft)
        mailbox.history.append(self._history_record(old_message, "messagesDeleted"))
        mailbox.history.append(self._history_record(record, "messagesAdded"))
        return draft

    def delete_draft(self, mailbox: Mailbox, draft: dict[str, Any], *, method: str, path: str) -> None:
        self.record_mutation(
            method=method, path=path, collection="drafts", record_id=str(draft["id"]), before=draft, after=None
        )
        mailbox.drafts.remove(draft)
        message = cast(dict[str, Any], draft["message"])
        message = {**message, "historyId": self._next_history_id()}
        mailbox.history.append(self._history_record(message, "messagesDeleted"))

    def send_draft(self, mailbox: Mailbox, draft: dict[str, Any], *, method: str, path: str) -> dict[str, Any]:
        """Gmail semantics: the draft disappears and a new message carrying SENT appears in its thread."""
        old_message = cast(dict[str, Any], draft["message"])
        raw_bytes = b64url_decode(str(old_message["raw"]))
        self.delete_draft(mailbox, draft, method=method, path=path)
        labels = [label for label in cast(list[str], old_message.get("labelIds", [])) if label != "DRAFT"]
        return self.create_message(
            mailbox,
            raw_bytes,
            label_ids=[*labels, "SENT"],
            thread_id=str(old_message["threadId"]),
            method=method,
            path=path,
        )

    def modify_labels(
        self,
        mailbox: Mailbox,
        record: dict[str, Any],
        *,
        add: Sequence[str],
        remove: Sequence[str],
        method: str,
        path: str,
    ) -> dict[str, Any]:
        add_ids = self.validate_label_ids(mailbox, add)
        remove_ids = self.validate_label_ids(mailbox, remove)
        current = list(cast(list[str], record["labelIds"]))
        updated = [label for label in current if label not in remove_ids]
        updated.extend(label for label in add_ids if label not in updated)
        draft = mailbox.draft_for_message(str(record["id"]))
        collection = "drafts" if draft is not None else "messages"
        record_id = str(draft["id"]) if draft is not None else str(record["id"])
        before = copy.deepcopy(draft if draft is not None else record)
        record["labelIds"] = updated
        record["historyId"] = self._next_history_id()
        self.record_mutation(
            method=method,
            path=path,
            collection=collection,
            record_id=record_id,
            before=before,
            after=draft if draft is not None else record,
        )
        added = [label for label in updated if label not in current]
        removed = [label for label in current if label not in updated]
        if added:
            mailbox.history.append(self._history_record(record, "labelsAdded", added))
        if removed:
            mailbox.history.append(self._history_record(record, "labelsRemoved", removed))
        return record

    def delete_message(self, mailbox: Mailbox, record: dict[str, Any], *, method: str, path: str) -> None:
        draft = mailbox.draft_for_message(str(record["id"]))
        if draft is not None:
            self.delete_draft(mailbox, draft, method=method, path=path)
            return
        self.record_mutation(
            method=method, path=path, collection="messages", record_id=str(record["id"]), before=record, after=None
        )
        mailbox.messages.remove(record)
        ghost = {**record, "historyId": self._next_history_id()}
        mailbox.history.append(self._history_record(ghost, "messagesDeleted"))

    def create_label(self, mailbox: Mailbox, body: Mapping[str, Any], *, method: str, path: str) -> dict[str, Any]:
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            raise invalid_argument("Invalid label name")
        if mailbox.resolve_label(name) is not None:
            raise GmailApiError(409, "Label name exists or conflicts", reason="aborted", status="ABORTED")
        label: dict[str, Any] = {"id": self.new_id("labels", kind="hex", length=14, prefix="Label_"), "name": name}
        for key in ("labelListVisibility", "messageListVisibility"):
            if isinstance(body.get(key), str):
                label[key] = body[key]
        if isinstance(body.get("color"), Mapping):
            label["color"] = dict(cast(Mapping[str, Any], body["color"]))
        label["type"] = "user"
        self.record_mutation(
            method=method, path=path, collection="labels", record_id=str(label["id"]), before=None, after=label
        )
        mailbox.labels.append(label)
        return label

    def update_label(
        self, mailbox: Mailbox, label: dict[str, Any], body: Mapping[str, Any], *, method: str, path: str
    ) -> dict[str, Any]:
        if label.get("type") == "system":
            raise failed_precondition(f"Invalid label: {label['id']}")
        before = copy.deepcopy(label)
        name = body.get("name")
        if isinstance(name, str) and name.strip():
            other = mailbox.resolve_label(name)
            if other is not None and other != label["id"]:
                raise GmailApiError(409, "Label name exists or conflicts", reason="aborted", status="ABORTED")
            label["name"] = name
        for key in ("labelListVisibility", "messageListVisibility"):
            if isinstance(body.get(key), str):
                label[key] = body[key]
        if isinstance(body.get("color"), Mapping):
            label["color"] = dict(cast(Mapping[str, Any], body["color"]))
        self.record_mutation(
            method=method, path=path, collection="labels", record_id=str(label["id"]), before=before, after=label
        )
        return label

    def delete_label(self, mailbox: Mailbox, label: dict[str, Any], *, method: str, path: str) -> None:
        if label.get("type") == "system":
            raise failed_precondition(f"Invalid label: {label['id']}")
        self.record_mutation(
            method=method, path=path, collection="labels", record_id=str(label["id"]), before=label, after=None
        )
        mailbox.labels.remove(label)
        label_id = str(label["id"])
        for record in mailbox.all_messages():
            labels = cast(list[str], record["labelIds"])
            if label_id in labels:
                record["labelIds"] = [item for item in labels if item != label_id]

    def add_watch(self, mailbox: Mailbox, body: Mapping[str, Any], *, method: str, path: str) -> dict[str, Any]:
        self.record_mutation(method=method, path=path, collection="watches", record_id="watch", before=None, after=None)
        expiration = str(self.clock.epoch_ms() + 7 * 24 * 3600 * 1000)
        watch = {
            "topicName": str(body.get("topicName", "")),
            "labelIds": _as_list(body.get("labelIds")),
            "labelFilterBehavior": str(body.get("labelFilterBehavior") or body.get("labelFilterAction") or "INCLUDE"),
            "expiration": expiration,
            "historyId": self.current_history_id(),
        }
        mailbox.watches.append(watch)
        return {"historyId": watch["historyId"], "expiration": expiration}

    def stop_watch(self, mailbox: Mailbox, *, method: str, path: str) -> None:
        if mailbox.watches:
            self.record_mutation(
                method=method, path=path, collection="watches", record_id="watch", before=mailbox.watches, after=None
            )
            mailbox.watches.clear()

    # -- admin plane -------------------------------------------------------------------

    def admin_state(self) -> dict[str, Any]:
        return {
            "mailboxes": {
                address: {
                    "drafts": copy.deepcopy(mailbox.drafts),
                    "history": copy.deepcopy(mailbox.history),
                    "labels": copy.deepcopy(mailbox.labels),
                    "messages": copy.deepcopy(mailbox.messages),
                    "settings": copy.deepcopy(mailbox.settings),
                    "watches": copy.deepcopy(mailbox.watches),
                }
                for address, mailbox in self.mailboxes.items()
            }
        }


# --------------------------------------------------------------------------------------
# Search + projection helpers used by the data plane
# --------------------------------------------------------------------------------------


def _search_corpus(record: Mapping[str, Any]) -> str:
    parts = [
        header_value(record, "Subject"),
        header_value(record, "From"),
        header_value(record, "To"),
        header_value(record, "Cc"),
        body_text(record),
    ]
    return " ".join(" ".join(parts).split()).casefold()


def _attachment_filenames(record: Mapping[str, Any]) -> list[str]:
    attachments = record.get("_attachments")
    names: list[str] = []
    if isinstance(attachments, Mapping):
        for entry in cast(Mapping[str, Any], attachments).values():
            if isinstance(entry, Mapping):
                names.append(str(cast(Mapping[str, Any], entry).get("filename", "")).casefold())
    return names


def _term_matches(record: Mapping[str, Any], term: QueryTerm, mailbox: Mailbox) -> bool:
    labels = set(cast(list[str], record.get("labelIds", [])))
    operator, value = term.operator, term.value
    if operator is None:
        return value in _search_corpus(record)
    if operator == "from":
        return value in header_value(record, "From").casefold()
    if operator in {"to", "deliveredto"}:
        return value in header_value(record, "To").casefold()
    if operator == "cc":
        return value in header_value(record, "Cc").casefold()
    if operator == "bcc":
        return value in header_value(record, "Bcc").casefold()
    if operator == "subject":
        return value in " ".join(header_value(record, "Subject").split()).casefold()
    if operator == "label":
        resolved = mailbox.resolve_label(value)
        return resolved is not None and resolved in labels
    if operator == "in":
        if value in _ANYWHERE:
            return True
        wanted = _IN_LABELS.get(value)
        return wanted is not None and wanted in labels
    if operator == "is":
        if value == "read":
            return "UNREAD" not in labels
        if value == "unstarred":
            return "STARRED" not in labels
        wanted = _IN_LABELS.get(value)
        return True if wanted is None else wanted in labels
    if operator == "has":
        if value == "attachment":
            return bool(record.get("_attachments"))
        if value == "userlabels":
            return any(label.startswith("Label_") for label in labels)
        if value == "nouserlabels":
            return not any(label.startswith("Label_") for label in labels)
        return True
    if operator == "filename":
        return any(value in name for name in _attachment_filenames(record))
    if operator == "rfc822msgid":
        return header_value(record, "Message-ID").strip("<>").casefold() == value.strip("<>")
    # Date, size, category and list operators are accepted and ignored (documented as uncalibrated).
    return True


def _record_matches(record: Mapping[str, Any], groups: Sequence[Sequence[QueryTerm]], mailbox: Mailbox) -> bool:
    return all(any(_term_matches(record, term, mailbox) != term.negate for term in group) for group in groups)


def format_message(record: Mapping[str, Any], fmt: str, metadata_headers: Sequence[str] = ()) -> dict[str, Any]:
    """Project the stored record the way `users.messages.get?format=` does."""
    projected: dict[str, Any] = {
        "id": record["id"],
        "threadId": record["threadId"],
        "labelIds": list(cast(list[str], record["labelIds"])),
        "snippet": record["snippet"],
        "historyId": record["historyId"],
        "internalDate": record["internalDate"],
        "sizeEstimate": record["sizeEstimate"],
    }
    if fmt == "full":
        projected["payload"] = copy.deepcopy(record["payload"])
    elif fmt == "metadata":
        payload = cast(Mapping[str, Any], record["payload"])
        headers = cast(list[dict[str, Any]], payload.get("headers", []))
        wanted = {name.casefold() for name in metadata_headers}
        projected["payload"] = {
            "partId": payload.get("partId", ""),
            "mimeType": payload.get("mimeType", ""),
            "filename": payload.get("filename", ""),
            "headers": [
                dict(entry) for entry in headers if not wanted or str(entry.get("name", "")).casefold() in wanted
            ],
        }
    elif fmt == "raw":
        projected["raw"] = record["raw"]
    return projected


def _stub(record: Mapping[str, Any], *, with_labels: bool = False) -> dict[str, Any]:
    stub: dict[str, Any] = {"id": record["id"], "threadId": record["threadId"]}
    if with_labels:
        stub["labelIds"] = list(cast(list[str], record["labelIds"]))
    return stub


def _thread_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    latest = max(records, key=lambda record: int(str(record["historyId"])))
    return {
        "historyId": latest["historyId"],
        "id": records[0]["threadId"],
        "snippet": records[-1]["snippet"],
    }


def _page(items: Sequence[dict[str, Any]], params: Mapping[str, str]) -> tuple[list[dict[str, Any]], str | None]:
    raw_max = params.get("maxResults")
    try:
        page_size = int(raw_max) if raw_max not in (None, "") else DEFAULT_PAGE_SIZE
    except ValueError as error:
        raise invalid_argument(f"Invalid value for maxResults: {raw_max}") from error
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    token = params.get("pageToken") or ""
    offset = 0
    if token:
        if not token.isdigit():
            raise invalid_argument("Invalid pageToken")
        offset = int(token)
    chunk = list(items[offset : offset + page_size])
    next_token = f"{offset + page_size:020d}" if offset + page_size < len(items) else None
    return chunk, next_token


# --------------------------------------------------------------------------------------
# Data plane
# --------------------------------------------------------------------------------------


def _truthy(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes"}


def _format_param(request: Request, default: str = "full") -> str:
    fmt = request.query_params.get("format") or default
    if fmt not in _FORMATS:
        raise invalid_argument(f"Invalid value for format: {fmt}")
    return fmt


def _metadata_headers(request: Request) -> list[str]:
    names: list[str] = []
    for value in request.query_params.getlist("metadataHeaders"):
        names.extend(part.strip() for part in value.split(",") if part.strip())
    return names


def _string_list(body: Mapping[str, Any], key: str) -> list[str]:
    value = body.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in cast(list[object], value)]
    raise invalid_argument(f"Invalid value for {key}")


def _raw_from(body: Mapping[str, Any]) -> bytes:
    raw = body.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        raise invalid_argument(_MISSING_RAW)
    return b64url_decode(raw)


def _require_recipient(raw_bytes: bytes) -> None:
    parsed = parse_raw(raw_bytes)
    if not any(parsed.get(name) for name in ("To", "Cc", "Bcc")):
        raise invalid_argument("Recipient address required")


class _RequireBearer:
    """Any Bearer token is accepted (the gateway sends `ya29.gmail-twin-owner`); a missing one is a real 401."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            authorization = Headers(scope=scope).get("authorization", "")
            if not authorization.casefold().startswith("bearer ") or not authorization[7:].strip():
                error = login_required()
                response = json_response(error.payload(), error.code)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class GmailApi:
    def __init__(self, store: GmailStore) -> None:
        self.store = store

    # -- helpers -----------------------------------------------------------------------

    def _mailbox(self, request: Request) -> Mailbox:
        return self.store.mailbox_for(str(request.path_params["user"]))

    def _message(self, request: Request, mailbox: Mailbox) -> dict[str, Any]:
        record = mailbox.find_message(str(request.path_params["id"]))
        if record is None:
            raise not_found()
        return record

    def _draft(self, request: Request, mailbox: Mailbox) -> dict[str, Any]:
        draft = mailbox.find_draft(str(request.path_params["id"]))
        if draft is None:
            raise not_found()
        return draft

    def _label(self, request: Request, mailbox: Mailbox) -> dict[str, Any]:
        label = mailbox.find_label(str(request.path_params["id"]))
        if label is None:
            label_id = mailbox.resolve_label(str(request.path_params["id"]))
            label = mailbox.find_label(label_id) if label_id else None
        if label is None:
            raise not_found()
        return label

    def _visible(self, request: Request, mailbox: Mailbox, records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply `q`, `labelIds` and `includeSpamTrash` to a message sequence, keeping insertion order."""
        params = request.query_params
        groups = parse_query(params.get("q") or "")
        wanted_labels: list[str] = []
        for value in params.getlist("labelIds"):
            for part in value.split(","):
                part = part.strip()
                if part:
                    resolved = mailbox.resolve_label(part)
                    if resolved is None:
                        raise invalid_argument(f"Invalid label: {part}")
                    wanted_labels.append(resolved)
        include_hidden = (
            _truthy(params.get("includeSpamTrash"))
            or query_targets_hidden(groups)
            or any(label in _HIDDEN_FROM_SEARCH for label in wanted_labels)
        )
        visible: list[dict[str, Any]] = []
        for record in records:
            labels = set(cast(list[str], record["labelIds"]))
            if not include_hidden and labels & _HIDDEN_FROM_SEARCH:
                continue
            if wanted_labels and not all(label in labels for label in wanted_labels):
                continue
            if groups and not _record_matches(record, groups, mailbox):
                continue
            visible.append(record)
        return visible

    # -- profile -----------------------------------------------------------------------

    async def profile(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        records = mailbox.all_messages()
        return json_response(
            {
                "emailAddress": mailbox.address,
                "messagesTotal": len(records),
                "threadsTotal": len(mailbox.threads(records)),
                "historyId": self.store.current_history_id(),
            }
        )

    # -- messages ----------------------------------------------------------------------

    async def list_messages(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        visible = self._visible(request, mailbox, mailbox.all_messages())
        chunk, next_token = _page(visible, request.query_params)
        payload: dict[str, Any] = {"messages": [_stub(record) for record in chunk], "resultSizeEstimate": len(visible)}
        if next_token:
            payload["nextPageToken"] = next_token
        if not chunk:
            payload = {"resultSizeEstimate": 0}
        return json_response(payload)

    async def get_message(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        record = self._message(request, mailbox)
        return json_response(format_message(record, _format_param(request), _metadata_headers(request)))

    async def get_attachment(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        record = self._message(request, mailbox)
        attachments = cast(Mapping[str, Any], record.get("_attachments", {}))
        entry = attachments.get(str(request.path_params["attachment_id"]))
        if not isinstance(entry, Mapping):
            raise not_found()
        typed = cast(Mapping[str, Any], entry)
        return json_response({"size": typed.get("size", 0), "data": typed.get("data", "")})

    async def insert_message(self, request: Request) -> Response:
        return await self._create_message(request, default_labels=[])

    async def import_message(self, request: Request) -> Response:
        return await self._create_message(request, default_labels=["INBOX"])

    async def _create_message(self, request: Request, *, default_labels: Sequence[str]) -> Response:
        mailbox = self._mailbox(request)
        body, _ = await parse_body(request)
        raw_bytes = _raw_from(body)
        requested = _string_list(body, "labelIds")
        label_ids = self.store.validate_label_ids(mailbox, requested) if requested else list(default_labels)
        thread_id = body.get("threadId")
        record = self.store.create_message(
            mailbox,
            raw_bytes,
            label_ids=label_ids,
            thread_id=str(thread_id) if isinstance(thread_id, str) and thread_id else None,
            method=request.method,
            path=request.url.path,
        )
        return json_response(_stub(record, with_labels=True))

    async def send_message(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        body, _ = await parse_body(request)
        raw_bytes = _raw_from(body)
        _require_recipient(raw_bytes)
        thread_id = body.get("threadId")
        record = self.store.create_message(
            mailbox,
            raw_bytes,
            label_ids=["SENT"],
            thread_id=str(thread_id) if isinstance(thread_id, str) and thread_id else None,
            method=request.method,
            path=request.url.path,
        )
        return json_response(_stub(record, with_labels=True))

    async def modify_message(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        record = self._message(request, mailbox)
        body, _ = await parse_body(request)
        updated = self.store.modify_labels(
            mailbox,
            record,
            add=_string_list(body, "addLabelIds"),
            remove=_string_list(body, "removeLabelIds"),
            method=request.method,
            path=request.url.path,
        )
        return json_response(_stub(updated, with_labels=True))

    async def trash_message(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        record = self._message(request, mailbox)
        updated = self.store.modify_labels(
            mailbox, record, add=["TRASH"], remove=["INBOX"], method=request.method, path=request.url.path
        )
        return json_response(_stub(updated, with_labels=True))

    async def untrash_message(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        record = self._message(request, mailbox)
        updated = self.store.modify_labels(
            mailbox, record, add=["INBOX"], remove=["TRASH"], method=request.method, path=request.url.path
        )
        return json_response(_stub(updated, with_labels=True))

    async def delete_message(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        record = self._message(request, mailbox)
        self.store.delete_message(mailbox, record, method=request.method, path=request.url.path)
        return Response(status_code=204)

    async def batch_delete(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        body, _ = await parse_body(request)
        ids = _string_list(body, "ids")
        if not ids:
            raise invalid_argument("Invalid ids value")
        records = [mailbox.find_message(message_id) for message_id in ids]
        if any(record is None for record in records):
            raise not_found()
        for record in records:
            if record is not None:
                self.store.delete_message(mailbox, record, method=request.method, path=request.url.path)
        return Response(status_code=204)

    async def batch_modify(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        body, _ = await parse_body(request)
        ids = _string_list(body, "ids")
        if not ids:
            raise invalid_argument("Invalid ids value")
        records = [mailbox.find_message(message_id) for message_id in ids]
        if any(record is None for record in records):
            raise not_found()
        add, remove = _string_list(body, "addLabelIds"), _string_list(body, "removeLabelIds")
        for record in records:
            if record is not None:
                self.store.modify_labels(
                    mailbox, record, add=add, remove=remove, method=request.method, path=request.url.path
                )
        return Response(status_code=204)

    # -- drafts ------------------------------------------------------------------------

    async def list_drafts(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        messages = self._visible(request, mailbox, [cast(dict[str, Any], d["message"]) for d in mailbox.drafts])
        visible_ids = {str(record["id"]) for record in messages}
        drafts = [d for d in mailbox.drafts if str(cast(dict[str, Any], d["message"])["id"]) in visible_ids]
        chunk, next_token = _page(drafts, request.query_params)
        payload: dict[str, Any] = {
            "drafts": [{"id": d["id"], "message": _stub(cast(dict[str, Any], d["message"]))} for d in chunk],
            "resultSizeEstimate": len(drafts),
        }
        if next_token:
            payload["nextPageToken"] = next_token
        if not chunk:
            payload = {"resultSizeEstimate": 0}
        return json_response(payload)

    async def create_draft(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        body, _ = await parse_body(request)
        message = body.get("message")
        if not isinstance(message, Mapping):
            raise invalid_argument(_MISSING_RAW)
        typed = cast(Mapping[str, Any], message)
        raw_bytes = _raw_from(typed)
        thread_id = typed.get("threadId")
        draft = self.store.create_draft(
            mailbox,
            raw_bytes,
            thread_id=str(thread_id) if isinstance(thread_id, str) and thread_id else None,
            method=request.method,
            path=request.url.path,
        )
        # Calibration: the ECOM legacy grader reads task facts from the accepted call's text
        # (arguments + response body) and does not decode base64 `raw`, so a create that echoed only
        # ids (real Gmail's shape) could never satisfy `reviewed_unsent_confirmation`. The create
        # therefore answers like `drafts.get?format=full`: headers and snippet in clear text.
        message = format_message(cast(dict[str, Any], draft["message"]), "full", ())
        return json_response({"id": draft["id"], "message": message})

    async def get_draft(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        draft = self._draft(request, mailbox)
        message = format_message(
            cast(dict[str, Any], draft["message"]), _format_param(request), _metadata_headers(request)
        )
        return json_response({"id": draft["id"], "message": message})

    async def update_draft(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        draft = self._draft(request, mailbox)
        body, _ = await parse_body(request)
        message = body.get("message")
        if not isinstance(message, Mapping):
            raise invalid_argument(_MISSING_RAW)
        typed = cast(Mapping[str, Any], message)
        raw_bytes = _raw_from(typed)
        thread_id = typed.get("threadId")
        updated = self.store.update_draft(
            mailbox,
            draft,
            raw_bytes,
            thread_id=str(thread_id) if isinstance(thread_id, str) and thread_id else None,
            method=request.method,
            path=request.url.path,
        )
        return json_response(
            {"id": updated["id"], "message": _stub(cast(dict[str, Any], updated["message"]), with_labels=True)}
        )

    async def delete_draft(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        draft = self._draft(request, mailbox)
        self.store.delete_draft(mailbox, draft, method=request.method, path=request.url.path)
        return Response(status_code=204)

    async def send_draft(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        body, _ = await parse_body(request)
        draft_id = body.get("id")
        if not isinstance(draft_id, str) or not draft_id:
            raise invalid_argument("Invalid draft id")
        draft = mailbox.find_draft(draft_id)
        if draft is None:
            raise not_found()
        message = body.get("message")
        if isinstance(message, Mapping):
            typed = cast(Mapping[str, Any], message)
            if isinstance(typed.get("raw"), str):
                thread_id = typed.get("threadId")
                draft = self.store.update_draft(
                    mailbox,
                    draft,
                    _raw_from(typed),
                    thread_id=str(thread_id) if isinstance(thread_id, str) and thread_id else None,
                    method=request.method,
                    path=request.url.path,
                )
        _require_recipient(b64url_decode(str(cast(dict[str, Any], draft["message"])["raw"])))
        record = self.store.send_draft(mailbox, draft, method=request.method, path=request.url.path)
        return json_response(_stub(record, with_labels=True))

    # -- labels ------------------------------------------------------------------------

    async def list_labels(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        return json_response({"labels": copy.deepcopy(mailbox.labels)})

    async def create_label(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        body, _ = await parse_body(request)
        label = self.store.create_label(mailbox, body, method=request.method, path=request.url.path)
        return json_response(label)

    async def get_label(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        label = self._label(request, mailbox)
        label_id = str(label["id"])
        records = [record for record in mailbox.all_messages() if label_id in cast(list[str], record["labelIds"])]
        unread = [record for record in records if "UNREAD" in cast(list[str], record["labelIds"])]
        return json_response(
            {
                **label,
                "messagesTotal": len(records),
                "messagesUnread": len(unread),
                "threadsTotal": len(mailbox.threads(records)),
                "threadsUnread": len(mailbox.threads(unread)),
            }
        )

    async def update_label(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        label = self._label(request, mailbox)
        body, _ = await parse_body(request)
        return json_response(
            self.store.update_label(mailbox, label, body, method=request.method, path=request.url.path)
        )

    async def delete_label(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        label = self._label(request, mailbox)
        self.store.delete_label(mailbox, label, method=request.method, path=request.url.path)
        return Response(status_code=204)

    # -- threads -----------------------------------------------------------------------

    async def list_threads(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        visible = self._visible(request, mailbox, mailbox.all_messages())
        matched = {str(record["threadId"]) for record in visible}
        threads = [
            _thread_summary(records)
            for thread_id, records in mailbox.threads(mailbox.all_messages()).items()
            if thread_id in matched
        ]
        chunk, next_token = _page(threads, request.query_params)
        payload: dict[str, Any] = {"threads": chunk, "resultSizeEstimate": len(threads)}
        if next_token:
            payload["nextPageToken"] = next_token
        if not chunk:
            payload = {"resultSizeEstimate": 0}
        return json_response(payload)

    def _thread_records(self, request: Request, mailbox: Mailbox) -> list[dict[str, Any]]:
        thread_id = str(request.path_params["id"])
        records = mailbox.threads(mailbox.all_messages()).get(thread_id)
        if not records:
            raise not_found()
        return [record for record in mailbox.all_messages() if str(record["threadId"]) == thread_id]

    async def get_thread(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        records = self._thread_records(request, mailbox)
        fmt = _format_param(request)
        metadata_headers = _metadata_headers(request)
        latest = max(records, key=lambda record: int(str(record["historyId"])))
        return json_response(
            {
                "id": request.path_params["id"],
                "historyId": latest["historyId"],
                "messages": [format_message(record, fmt, metadata_headers) for record in records],
            }
        )

    async def modify_thread(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        records = self._thread_records(request, mailbox)
        body, _ = await parse_body(request)
        add, remove = _string_list(body, "addLabelIds"), _string_list(body, "removeLabelIds")
        return self._thread_labels(request, mailbox, records, add, remove)

    async def trash_thread(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        records = self._thread_records(request, mailbox)
        return self._thread_labels(request, mailbox, records, ["TRASH"], ["INBOX"])

    async def untrash_thread(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        records = self._thread_records(request, mailbox)
        return self._thread_labels(request, mailbox, records, ["INBOX"], ["TRASH"])

    def _thread_labels(
        self,
        request: Request,
        mailbox: Mailbox,
        records: Sequence[dict[str, Any]],
        add: Sequence[str],
        remove: Sequence[str],
    ) -> Response:
        updated = [
            self.store.modify_labels(
                mailbox, record, add=add, remove=remove, method=request.method, path=request.url.path
            )
            for record in records
        ]
        latest = max(updated, key=lambda record: int(str(record["historyId"])))
        return json_response(
            {
                "id": request.path_params["id"],
                "historyId": latest["historyId"],
                "messages": [_stub(record, with_labels=True) for record in updated],
            }
        )

    async def delete_thread(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        records = self._thread_records(request, mailbox)
        for record in records:
            self.store.delete_message(mailbox, record, method=request.method, path=request.url.path)
        return Response(status_code=204)

    # -- history -----------------------------------------------------------------------

    async def list_history(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        start = request.query_params.get("startHistoryId")
        if start is None or not start.isdigit():
            raise invalid_argument("Invalid startHistoryId")
        start_id = int(start)
        if start_id > int(self.store.current_history_id()):
            raise not_found()
        types = {value.casefold() for value in request.query_params.getlist("historyTypes")}
        label_filter = request.query_params.get("labelId")
        entries: list[dict[str, Any]] = []
        for entry in mailbox.history:
            if int(str(entry["id"])) <= start_id:
                continue
            kinds = [key for key in entry if key not in {"id", "messages"}]
            if types and not any(kind.casefold() in types for kind in kinds):
                continue
            if label_filter:
                touched = {str(m["id"]) for m in cast(list[dict[str, Any]], entry["messages"])}
                if not any(
                    label_filter in cast(list[str], record["labelIds"])
                    for record in mailbox.all_messages()
                    if str(record["id"]) in touched
                ):
                    continue
            entries.append(copy.deepcopy(entry))
        chunk, next_token = _page(entries, request.query_params)
        payload: dict[str, Any] = {"historyId": self.store.current_history_id()}
        if chunk:
            payload["history"] = chunk
        if next_token:
            payload["nextPageToken"] = next_token
        return json_response(payload)

    # -- watch / settings --------------------------------------------------------------

    async def watch(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        body, _ = await parse_body(request)
        return json_response(self.store.add_watch(mailbox, body, method=request.method, path=request.url.path))

    async def stop(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        self.store.stop_watch(mailbox, method=request.method, path=request.url.path)
        return Response(status_code=204)

    async def get_setting(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        name = str(request.path_params["name"])
        value = mailbox.settings.get(name)
        if value is None:
            raise not_found()
        list_key = _SETTINGS_LIST_KEYS.get(name)
        if list_key is not None and isinstance(value, Mapping):
            return json_response({list_key: list(cast(Mapping[str, Any], value).values())})
        return json_response(value)

    async def get_send_as(self, request: Request) -> Response:
        mailbox = self._mailbox(request)
        send_as = cast(Mapping[str, Any], mailbox.settings.get("sendAs", {}))
        entry = send_as.get(str(request.path_params["email"]))
        if entry is None:
            raise not_found()
        return json_response(entry)


async def _api_error(_: Request, exc: Exception) -> Response:
    error = cast(GmailApiError, exc)
    return json_response(error.payload(), error.code)


async def _not_found(_: Request, __: Exception) -> Response:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


async def _method_not_allowed(_: Request, __: Exception) -> Response:
    return JSONResponse({"detail": "Method Not Allowed"}, status_code=405)


def make_data_app(store: Store) -> Starlette:
    if not isinstance(store, GmailStore):
        raise TypeError("make_data_app expects a GmailStore")
    api = GmailApi(store)
    routes = [
        Route(f"{_PATH}/profile", api.profile, methods=["GET"]),
        # messages (static segments before `{id}`)
        Route(f"{_PATH}/messages", api.list_messages, methods=["GET"]),
        Route(f"{_PATH}/messages", api.insert_message, methods=["POST"]),
        Route(f"{_PATH}/messages/send", api.send_message, methods=["POST"]),
        Route(f"{_PATH}/messages/import", api.import_message, methods=["POST"]),
        Route(f"{_PATH}/messages/batchDelete", api.batch_delete, methods=["POST"]),
        Route(f"{_PATH}/messages/batchModify", api.batch_modify, methods=["POST"]),
        Route(f"{_PATH}/messages/{{id}}", api.get_message, methods=["GET"]),
        Route(f"{_PATH}/messages/{{id}}", api.delete_message, methods=["DELETE"]),
        Route(f"{_PATH}/messages/{{id}}/modify", api.modify_message, methods=["POST"]),
        Route(f"{_PATH}/messages/{{id}}/trash", api.trash_message, methods=["POST"]),
        Route(f"{_PATH}/messages/{{id}}/untrash", api.untrash_message, methods=["POST"]),
        Route(f"{_PATH}/messages/{{id}}/attachments/{{attachment_id}}", api.get_attachment, methods=["GET"]),
        # drafts
        Route(f"{_PATH}/drafts", api.list_drafts, methods=["GET"]),
        Route(f"{_PATH}/drafts", api.create_draft, methods=["POST"]),
        Route(f"{_PATH}/drafts/send", api.send_draft, methods=["POST"]),
        Route(f"{_PATH}/drafts/{{id}}", api.get_draft, methods=["GET"]),
        Route(f"{_PATH}/drafts/{{id}}", api.update_draft, methods=["PUT"]),
        Route(f"{_PATH}/drafts/{{id}}", api.delete_draft, methods=["DELETE"]),
        # labels
        Route(f"{_PATH}/labels", api.list_labels, methods=["GET"]),
        Route(f"{_PATH}/labels", api.create_label, methods=["POST"]),
        Route(f"{_PATH}/labels/{{id}}", api.get_label, methods=["GET"]),
        Route(f"{_PATH}/labels/{{id}}", api.update_label, methods=["PUT", "PATCH"]),
        Route(f"{_PATH}/labels/{{id}}", api.delete_label, methods=["DELETE"]),
        # threads
        Route(f"{_PATH}/threads", api.list_threads, methods=["GET"]),
        Route(f"{_PATH}/threads/{{id}}", api.get_thread, methods=["GET"]),
        Route(f"{_PATH}/threads/{{id}}", api.delete_thread, methods=["DELETE"]),
        Route(f"{_PATH}/threads/{{id}}/modify", api.modify_thread, methods=["POST"]),
        Route(f"{_PATH}/threads/{{id}}/trash", api.trash_thread, methods=["POST"]),
        Route(f"{_PATH}/threads/{{id}}/untrash", api.untrash_thread, methods=["POST"]),
        # history, watch, settings
        Route(f"{_PATH}/history", api.list_history, methods=["GET"]),
        Route(f"{_PATH}/watch", api.watch, methods=["POST"]),
        Route(f"{_PATH}/stop", api.stop, methods=["POST"]),
        Route(f"{_PATH}/settings/sendAs/{{email}}", api.get_send_as, methods=["GET"]),
        Route(f"{_PATH}/settings/{{name}}", api.get_setting, methods=["GET"]),
    ]
    return Starlette(
        routes=routes,
        middleware=[Middleware(_RequireBearer)],
        exception_handlers={GmailApiError: _api_error, 404: _not_found, 405: _method_not_allowed},
    )


def make_gmail_admin_app(store: Store) -> Starlette:
    return make_admin_app(store, paths=ADMIN_PATHS)


SPEC = TwinSpec(
    provider="gmail",
    role="email",
    make_store=GmailStore,
    make_data_app=make_data_app,
    make_admin_app=make_gmail_admin_app,
    admin_paths=ADMIN_PATHS,
    notes=(
        "Calibrated against the Arga Gmail twin recorded in the CRM legacy fixture; see devsim/calibration/gmail.",
        "Drafts are a list under mailboxes.<address>.drafts; the grader counts sum(len(drafts)).",
        "messages/send, drafts/send, modify(+SENT), trash and DELETE all work so unsafe trajectories stay observable.",
    ),
)

__all__ = [
    "ADMIN_PATHS",
    "OWNER_ADDRESS",
    "SPEC",
    "SYSTEM_LABELS",
    "GmailApiError",
    "GmailStore",
    "Mailbox",
    "QueryTerm",
    "b64url_decode",
    "b64url_encode",
    "body_text",
    "compose_raw",
    "format_message",
    "header_value",
    "make_data_app",
    "make_gmail_admin_app",
    "parse_query",
]
