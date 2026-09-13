"""Model-facing output schemas.

These are what `ModelClient.emit` forces the model to produce. They are deliberately
*drafts*: code post-processes each one (verifies evidence substrings, applies always-on
rules, converts intents into concrete requests through playbooks). Nothing here names a
task, an entity, or a domain.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from benchpress.context import Frozen, PolicyKind


class PolicyClassification(Frozen):
    """One operating rule found in a provisioned system."""

    source_index: int = Field(description="Index of the source in the list you were given.")
    kind: PolicyKind
    quote: str = Field(description="The exact sentence(s) from the source that state the rule, verbatim.")
    applies_to: tuple[str, ...] = Field(default=(), description="Entities or change types the rule governs.")
    rationale: str = ""


class PolicyBatch(Frozen):
    policies: tuple[PolicyClassification, ...] = ()


class TargetChoice(Frozen):
    provider: str
    resource_type: str
    resource_id: str
    display: str
    evidence: tuple[str, ...] = Field(
        description="Short strings copied verbatim from the chosen record that justify the choice.",
    )
    confidence: Literal["high", "medium", "low"] = "high"


class Resolution(Frozen):
    chosen: tuple[TargetChoice, ...] = ()
    ambiguous: bool = False
    ambiguity_reason: str = ""
    near_duplicate_ids: tuple[str, ...] = Field(
        default=(),
        description="resource_ids of every candidate that must NOT be touched (look-alikes, prospects, archives).",
    )


class EndStateDraft(Frozen):
    provider: str
    resource: str = Field(description="`resource_type:resource_id` of a chosen target.")
    field: str
    expected: str
    comparison: Literal["email", "text", "contains"] = "contains"


class DoDDraft(Frozen):
    """What 'done' means, as data."""

    summary: str = Field(description="One sentence: what will be true when the work is complete.")
    end_state: tuple[EndStateDraft, ...] = ()
    facts: dict[str, str] = Field(
        default_factory=dict,
        description="Every identifier the outcome depends on, named: entity, former value, verified value, ids…",
    )
    customer_contact_email: str = Field(default="", description="The customer-side address a confirmation would go to.")
    account_owner: str = Field(default="", description="The internal person accountable for the record, if known.")
    needs_customer_confirmation: bool = Field(
        default=False, description="True if a discovered policy requires a reviewed customer confirmation."
    )
    needs_owner_review: bool = Field(default=False, description="True if a discovered policy requires owner review.")
    forbidden: tuple[str, ...] = Field(
        default=(),
        description="Action classes the prompt or policies forbid, e.g. send_email, create_charge, merge_pr.",
    )
    escalate: bool = Field(default=False, description="True if no safe primary write can be identified.")
    escalation_reason: str = ""


class ActionIntent(Frozen):
    """A write expressed as intent. Code turns it into an exact request through a playbook."""

    id: str
    kind: Literal["update", "message", "draft", "comment"]
    provider: str
    ref: str = Field(default="", description="`resource_type:resource_id` for update/comment; must be a chosen target.")
    fields: dict[str, str] = Field(default_factory=dict, description="Field → new value for update.")
    channel: str = Field(default="", description="Channel name or id for message.")
    text: str = Field(default="", description="Message or comment text.")
    thread_ts: str = ""
    to: str = Field(default="", description="Recipient for draft (never sent).")
    subject: str = ""
    body: str = ""
    satisfies: tuple[str, ...] = Field(description="Definition-of-done items this write satisfies, e.g. end_state[0].")
    rationale: str = ""


class PlanDraft(Frozen):
    actions: tuple[ActionIntent, ...] = ()
    notes: str = ""


class RepairPlan(Frozen):
    actions: tuple[ActionIntent, ...] = ()
    reason: str = ""


class ChannelUpdateDraft(Frozen):
    """Human-readable update text; code appends the evidenced facts it must mention."""

    text: str
