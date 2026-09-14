"""Request and response models shared by the gateway's HTTP routes and MCP tools.

Every model forbids unknown keys, so a misspelt field is a 422 rather than a setting silently ignored.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from benchpress.context import Action, Context, DefinitionOfDone, Evidence, GateVerdict, ResolvedTarget
from benchpress.normalize import canonical_email

__all__ = [
    "SESSION_ID_PATTERN",
    "ContextInput",
    "ExecuteRequest",
    "ExecuteResponse",
    "ExecuteStatus",
    "ProtectedInput",
    "ReceiptDetail",
    "ReceiptFilters",
    "ReceiptKind",
    "ReceiptPage",
    "ReceiptSummary",
    "SessionCreate",
    "SessionCreated",
]

SESSION_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,64}$"
_CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key", "api_key", "token", "secret", "password", "cookie"})


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProtectedInput(_Schema):
    """Records, names, domains and emails no write may touch. The caller decides them; the gateway never infers them."""

    ids: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()


class ContextInput(_Schema):
    """What the gate judges a session's writes against, as the caller states it."""

    user_prompt: str = ""
    providers: tuple[str, ...] = ()
    protected: ProtectedInput = Field(default_factory=ProtectedInput)
    targets: tuple[ResolvedTarget, ...] = ()
    write_scope: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()

    def to_context(self, session_id: str) -> Context:
        ctx = Context(trial_id=session_id, user_prompt=self.user_prompt, providers=self.providers)
        ctx.protected.ids.update(self.protected.ids)
        ctx.protected.names.update(self.protected.names)
        ctx.protected.domains.update(domain.casefold() for domain in self.protected.domains)
        ctx.protected.emails.update(canonical_email(email) for email in self.protected.emails)
        ctx.targets = list(self.targets)
        ctx.dod = DefinitionOfDone(write_scope=self.write_scope, forbidden=self.forbidden)
        return ctx


class SessionCreate(_Schema):
    session_id: str | None = Field(default=None, pattern=SESSION_ID_PATTERN)
    context: ContextInput
    idempotency_scope: Literal["session", "workspace"] = "session"


class SessionCreated(_Schema):
    session_id: str
    idempotency_scope: str


class ExecuteRequest(_Schema):
    """One write, run in an existing session (`session_id`) or in a new one built from an inline `context`."""

    action: Action
    session_id: str | None = None
    context: ContextInput | None = None

    @model_validator(mode="after")
    def _one_session_source_and_a_safe_write(self) -> ExecuteRequest:
        if (self.session_id is None) == (self.context is None):
            raise ValueError("send exactly one of session_id or context")
        if not self.action.is_write:
            raise ValueError("only writes are executed; reads go through read")
        leaked = sorted(key for key in self.action.headers if key.casefold() in _CREDENTIAL_HEADERS)
        if leaked:
            raise ValueError(f"credentials belong in upstream config, not action headers: {', '.join(leaked)}")
        return self


ExecuteStatus = Literal[
    "refused", "failed", "unverified", "verified", "mismatch", "needs_approval", "denied", "expired"
]


class ExecuteResponse(_Schema):
    status: ExecuteStatus
    session_id: str
    verdict: GateVerdict
    status_code: int | None
    evidence: list[Evidence]
    receipt_id: str | None
    approval_id: str | None = None


ReceiptKind = Literal["write", "guard", "run"]


class ReceiptSummary(_Schema):
    id: str
    kind: ReceiptKind
    at: str
    event: str
    session: str
    provider: str
    method: str
    path: str
    status: str | None
    rule: str
    resource: str


class ReceiptPage(_Schema):
    receipts: list[ReceiptSummary]
    next_before: str | None


class ReceiptDetail(_Schema):
    id: str
    kind: ReceiptKind
    payload: dict[str, Any]
    html: bool


class ReceiptFilters(_Schema):
    """`GET /v1/receipts` query: equality filters, a `customer` substring, and a newest-first page before an id."""

    customer: str | None = None
    provider: str | None = None
    rule: str | None = None
    status: str | None = None
    session: str | None = None
    event: str | None = None
    limit: int = Field(default=100, ge=1, le=500)
    before: str | None = None
