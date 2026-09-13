"""Typed working memory for a Benchpress run.

Every phase is a pure function of `(Context, model, tools) -> Context`. Nothing here is
task-specific: the *shape* is fixed, the *content* is discovered at runtime.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from benchpress.normalize import (
    canonical_email,
    canonical_text,
    casefold_text,
    contains_term,
    domain_of,
    significant_tokens,
)

PolicyKind = Literal[
    "communication_review",
    "approval",
    "embargo",
    "ownership",
    "containment",
    "quarantine",
    "suspicious",
    "other",
]

ActionKind = Literal["read", "create", "update", "comment", "draft", "message"]

DeliverableKind = Literal[
    "originating_channel_update",
    "unsent_customer_confirmation",
    "owner_review_record",
    "structured_result",
]

RunStatus = Literal["completed", "partial", "escalated"]

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]

# Action classes the gate refuses outright unless the definition of done authorizes them.
# Generic operational categories, never task ids.
ForbiddenClass = Literal[
    "send_email",
    "create_charge",
    "create_invoice",
    "update_subscription",
    "move_subscription",
    "merge_pr",
    "push_commit",
    "edit_source",
    "close_regression",
    "disable_workflow",
    "mass_rerun",
    "delete_any",
    "mutate_protected",
    "publish_external",
    "external_share",
    "calendar_invite_attendees",
]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Mutable(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskFrame(Frozen):
    """What the prompt asked for, parsed once at P0."""

    reporter: str | None = None
    originating_channel: str | None = None
    role: str | None = None
    subject_entities: tuple[str, ...] = ()
    requested_change: str = ""
    explicit_prohibitions: tuple[str, ...] = ()
    verify_required: bool = True
    inform_required: bool = True
    distractor_hint: str | None = None
    observed_identifiers: tuple[str, ...] = ()


class PolicyRecord(Frozen):
    """An operating rule discovered in a provisioned system before intent was fixed."""

    provider: str
    resource_ref: str
    quote: str
    kind: PolicyKind
    applies_to: tuple[str, ...] = ()

    @property
    def citation(self) -> str:
        return f"policy:{self.provider}:{self.resource_ref}"


class Candidate(Frozen):
    """One plausible target for the requested change."""

    provider: str
    resource_type: str
    resource_id: str
    display: str
    name: str | None = None
    domain: str | None = None
    email: str | None = None
    lifecycle: str | None = None
    notes: str = ""

    @property
    def ref(self) -> str:
        return f"{self.resource_type}:{self.resource_id}"

    def identifiers(self) -> frozenset[str]:
        raw = {self.resource_id, self.display}
        for optional in (self.name, self.domain, self.email):
            if optional:
                raw.add(optional)
        return frozenset(value for value in raw if value)


class ResolvedTarget(Frozen):
    """Exactly one chosen record per (provider, resource_type), with cited evidence."""

    provider: str
    resource_type: str
    resource_id: str
    display: str
    evidence: tuple[str, ...]
    confidence: Literal["high", "medium", "low"] = "high"

    @property
    def ref(self) -> str:
        return f"{self.resource_type}:{self.resource_id}"


class ProtectedSet(Mutable):
    """The deny-list. Every candidate that was *not* chosen lands here, plus every
    near-duplicate of a chosen target that exists anywhere in the workspace."""

    ids: set[str] = Field(default_factory=set[str])
    names: set[str] = Field(default_factory=set[str])
    domains: set[str] = Field(default_factory=set[str])
    emails: set[str] = Field(default_factory=set[str])

    def add_candidate(self, candidate: Candidate) -> None:
        self.ids.add(candidate.resource_id)
        self.names.add(candidate.display)
        if candidate.name:
            self.names.add(candidate.name)
        if candidate.domain:
            self.domains.add(candidate.domain.casefold())
        if candidate.email:
            self.emails.add(canonical_email(candidate.email))
            host = domain_of(candidate.email)
            if host:
                self.domains.add(host)

    def discard_target(self, target: ResolvedTarget, allowed: Iterable[str] = ()) -> None:
        """A chosen target must never sit in its own deny-list."""
        self.ids.discard(target.resource_id)
        self.names.discard(target.display)
        for value in allowed:
            self.ids.discard(value)
            self.names.discard(value)
            self.domains.discard(value.casefold())
            self.emails.discard(canonical_email(value))

    def all_terms(self) -> tuple[str, ...]:
        return tuple(sorted(self.ids | self.names | self.domains | self.emails))

    def hit(self, text: str) -> str | None:
        """Return the first protected term this text references, or None.

        Ids and emails match exactly; names match as canonical substrings; domains match
        as hostname tokens. The asymmetry is deliberate — a bare id like `42` must not
        false-positive on every payload that happens to contain `42`.
        """
        haystack_canonical = canonical_text(text)
        haystack_folded = casefold_text(text)
        for email in sorted(self.emails):
            if email and email in haystack_folded:
                return email
        for domain in sorted(self.domains):
            if domain and domain in haystack_folded:
                return domain
        for name in sorted(self.names, key=len, reverse=True):
            if len(canonical_text(name)) >= 4 and contains_term(text, name):
                return name
        for identifier in sorted(self.ids, key=len, reverse=True):
            folded = casefold_text(identifier)
            if len(folded) >= 6 and folded in haystack_canonical:
                return identifier
        return None


class EndStateItem(Frozen):
    """A fact that must be true in provider state before the run may claim success."""

    provider: str
    resource: str
    field: str
    expected: str
    comparison: Literal["email", "text", "contains"] = "contains"


class Deliverable(Frozen):
    kind: DeliverableKind
    provider: str | None = None
    channel: str | None = None
    because: str | None = None
    must_mention: tuple[str, ...] = ()


class DefinitionOfDone(Frozen):
    """The typed checklist. The agent may not return until each item is evidenced."""

    end_state: tuple[EndStateItem, ...] = ()
    deliverables: tuple[Deliverable, ...] = ()
    forbidden: tuple[str, ...] = ()
    write_scope: tuple[str, ...] = ()
    facts: dict[str, str] = Field(default_factory=dict[str, str])
    escalation: str | None = None

    def deliverable(self, kind: DeliverableKind) -> Deliverable | None:
        return next((item for item in self.deliverables if item.kind == kind), None)

    def forbids(self, action_class: str) -> bool:
        return action_class in self.forbidden


class ReadBack(Frozen):
    method: Literal["GET", "POST"] = "GET"
    path: str
    query: dict[str, str] = Field(default_factory=dict[str, str])
    body: object | None = None
    field_path: str | None = None


class Action(Frozen):
    """One planned step. Only these reach the gate."""

    id: str
    kind: ActionKind
    provider: str
    method: HttpMethod
    path: str
    query: dict[str, str] = Field(default_factory=dict[str, str])
    body: object | None = None
    body_encoding: Literal["json", "form"] = "json"
    headers: dict[str, str] = Field(default_factory=dict[str, str])
    satisfies: tuple[str, ...] = ()
    target_refs: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    readback: ReadBack | None = None
    rationale: str = ""

    @property
    def is_write(self) -> bool:
        return self.method != "GET"


class Plan(Frozen):
    actions: tuple[Action, ...] = ()

    def by_id(self, action_id: str) -> Action | None:
        return next((action for action in self.actions if action.id == action_id), None)


class Evidence(Frozen):
    """The only thing a final status may be computed from."""

    check: str
    provider: str
    resource: str
    expected: str
    observed: str
    match: bool
    detail: str = ""


class GateVerdict(Frozen):
    action_id: str
    allowed: bool
    rule: str = ""
    reason: str = ""


class LedgerEntry(Mutable):
    sequence: int
    phase: str
    provider: str
    method: str
    path: str
    fingerprint: str
    status_code: int | None = None
    ok: bool = False
    gate: GateVerdict | None = None
    action_id: str | None = None
    error: str | None = None
    response_digest: str = ""


class Context(Mutable):
    """The single mutable object threaded through P0..P7."""

    trial_id: str = "local"
    system_prompt: str = ""
    user_prompt: str = ""
    providers: tuple[str, ...] = ()
    provider_roles: dict[str, str] = Field(default_factory=dict[str, str])

    frame: TaskFrame = Field(default_factory=TaskFrame)
    policies: list[PolicyRecord] = Field(default_factory=list[PolicyRecord])
    candidates: list[Candidate] = Field(default_factory=list[Candidate])
    targets: list[ResolvedTarget] = Field(default_factory=list[ResolvedTarget])
    protected: ProtectedSet = Field(default_factory=ProtectedSet)
    dod: DefinitionOfDone = Field(default_factory=DefinitionOfDone)
    plan: Plan = Field(default_factory=Plan)

    evidence: list[Evidence] = Field(default_factory=list[Evidence])
    ledger: list[LedgerEntry] = Field(default_factory=list[LedgerEntry])
    refusals: list[GateVerdict] = Field(default_factory=list[GateVerdict])
    deliverable_refs: dict[str, str] = Field(default_factory=dict[str, str])

    ambiguous: bool = False
    notes: list[str] = Field(default_factory=list[str])

    # ---- derived views -------------------------------------------------------------

    def target_for(self, provider: str, resource_type: str | None = None) -> ResolvedTarget | None:
        for target in self.targets:
            if target.provider != provider:
                continue
            if resource_type is None or target.resource_type == resource_type:
                return target
        return None

    def target_refs(self) -> frozenset[str]:
        refs: set[str] = set()
        for target in self.targets:
            refs.update({target.ref, target.resource_id, target.display})
        return frozenset(refs)

    def known_domains(self) -> frozenset[str]:
        """Domains a write is allowed to address: the subject entities' own domains plus
        any domain already present in the resolved targets' evidence."""
        found: set[str] = set()
        for target in self.targets:
            for item in target.evidence:
                host = domain_of(item)
                if host:
                    found.add(host)
        for value in self.dod.facts.values():
            host = domain_of(value)
            if host:
                found.add(host)
        return frozenset(found - self.protected.domains)

    def policies_of(self, kind: PolicyKind) -> tuple[PolicyRecord, ...]:
        return tuple(record for record in self.policies if record.kind == kind)

    def near_duplicates(self, display: str) -> tuple[Candidate, ...]:
        """Candidates whose significant tokens collide with `display` — the look-alikes."""
        key = significant_tokens(display)
        if not key:
            return ()
        return tuple(
            candidate
            for candidate in self.candidates
            if candidate.display != display and significant_tokens(candidate.display) == key
        )

    def add_evidence(self, items: Sequence[Evidence]) -> None:
        self.evidence.extend(items)

    def status(self) -> RunStatus:
        if self.ambiguous:
            return "escalated"
        if not self.evidence:
            return "partial"
        return "completed" if all(item.match for item in self.evidence) else "partial"
