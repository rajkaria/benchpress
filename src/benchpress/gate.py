"""The mutation gate — authority enforced in code, not in a prompt.

Every non-GET call passes through `Gate.check` before it can reach the tool bus. A rule
here is a thing that *cannot happen*, as opposed to a thing the model was asked not to do.
The gate is pure: same (action, context, ledger) in, same verdict out, no I/O.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, cast

from benchpress.context import Action, Context, GateVerdict
from benchpress.normalize import casefold_text, domains_in, emails_in

# --------------------------------------------------------------------------------------
# Control plane. The gateway blocks these too; we reject them earlier so a blocked attempt
# never leaves the process and never pollutes the trace.
# --------------------------------------------------------------------------------------

BLOCKED_PATH_PREFIXES: tuple[str, ...] = (
    "/admin",
    "/_admin",
    "/_twin",
    "/inspect",
    "/reset",
    "/.well-known",
    "/openapi.json",
    "/swagger",
    "/redoc",
    "/schema",
    "/health",
    "/healthz",
    "/ready",
    "/readyz",
    "/metrics",
    "/docs",
    "/api-docs",
)

BLOCKED_EXACT_PATHS: frozenset[str] = frozenset({"/", "/api"})

_GRAPHQL_INTROSPECTION = re.compile(r"__schema|__type\b", re.IGNORECASE)


class GateRefusal(RuntimeError):
    """Raised when a write may not proceed. Carries the rule that refused it."""

    def __init__(self, rule: str, reason: str) -> None:
        super().__init__(f"{rule}: {reason}")
        self.rule = rule
        self.reason = reason


# --------------------------------------------------------------------------------------
# Action classification. Generic API shapes per provider — no task facts, no ids.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassRule:
    action_class: str
    provider: str | None
    methods: frozenset[str]
    path_pattern: re.Pattern[str]
    body_pattern: re.Pattern[str] | None = None
    description: str = ""


def _rule(
    action_class: str,
    provider: str | None,
    methods: Iterable[str],
    path_regex: str,
    *,
    body_regex: str | None = None,
    description: str = "",
) -> ClassRule:
    return ClassRule(
        action_class=action_class,
        provider=provider,
        methods=frozenset(method.upper() for method in methods),
        path_pattern=re.compile(path_regex, re.IGNORECASE),
        body_pattern=re.compile(body_regex, re.IGNORECASE) if body_regex else None,
        description=description,
    )


CLASS_RULES: tuple[ClassRule, ...] = (
    # --- email -----------------------------------------------------------------------
    _rule("send_email", "gmail", ["POST"], r"/messages/send\b", description="Gmail send"),
    _rule("send_email", "gmail", ["POST"], r"/drafts/send\b", description="Gmail draft send"),
    _rule("send_email", "gmail", ["POST"], r"/drafts/[^/]+/send\b", description="Gmail draft send"),
    _rule(
        "send_email",
        "gmail",
        ["POST", "PUT", "PATCH"],
        r"/messages/[^/]+/(modify|trash|untrash)\b",
        body_regex=r'"SENT"',
        description="Gmail label smuggling to SENT",
    ),
    _rule("send_email", None, ["POST"], r"/(send|sendMail|messages\.send)\b", description="generic send"),
    # --- money -----------------------------------------------------------------------
    _rule("create_charge", "stripe", ["POST"], r"/v1/charges\b", description="Stripe charge"),
    _rule("create_charge", "stripe", ["POST"], r"/v1/payment_intents\b", description="Stripe payment intent"),
    _rule("create_invoice", "stripe", ["POST"], r"/v1/invoices\b", description="Stripe invoice"),
    _rule(
        "create_invoice", "stripe", ["POST"], r"/v1/invoices/[^/]+/(pay|send|finalize)\b", description="invoice send"
    ),
    _rule("update_subscription", "stripe", ["POST", "PUT", "PATCH", "DELETE"], r"/v1/subscriptions\b"),
    _rule("update_subscription", "stripe", ["POST", "PUT", "PATCH", "DELETE"], r"/v1/subscription_items\b"),
    _rule("move_subscription", "stripe", ["POST"], r"/v1/subscriptions/[^/]+\b", body_regex=r"\bitems?\b|\bprice\b"),
    _rule("create_charge", "stripe", ["POST"], r"/v1/refunds\b", description="Stripe refund"),
    # --- source control ---------------------------------------------------------------
    _rule("merge_pr", "github", ["PUT", "POST"], r"/pulls/\d+/merge\b", description="GitHub merge"),
    _rule("merge_pr", "github", ["POST"], r"/merges\b"),
    _rule("push_commit", "github", ["POST", "PATCH"], r"/git/refs\b"),
    _rule("push_commit", "github", ["PUT", "POST", "PATCH", "DELETE"], r"/contents/"),
    _rule("edit_source", "github", ["PUT", "POST", "PATCH"], r"/git/(blobs|trees|commits)\b"),
    _rule("disable_workflow", "github", ["PUT", "POST"], r"/actions/workflows/[^/]+/(disable|enable)\b"),
    _rule("mass_rerun", "github", ["POST"], r"/actions/runs/\d+/(rerun|rerun-failed-jobs)\b"),
    _rule("disable_workflow", "github", ["POST", "PUT", "PATCH", "DELETE"], r"/branches/[^/]+/protection\b"),
    _rule("publish_external", "github", ["POST", "PATCH"], r"/releases\b"),
    # --- issue trackers ---------------------------------------------------------------
    _rule(
        "close_regression",
        "jira",
        ["POST"],
        r"/issue/[^/]+/transitions\b",
        body_regex=r"close|done|resolve|won.?t.?fix",
    ),
    _rule("close_regression", "linear", ["POST"], r"/graphql\b", body_regex=r"issueArchive|archiveIssue"),
    # --- calendar / sharing -----------------------------------------------------------
    _rule(
        "calendar_invite_attendees",
        "google_calendar",
        ["POST", "PUT", "PATCH"],
        r"/events\b",
        body_regex=r'"attendees"\s*:\s*\[\s*\{',
        description="calendar hold with attendees",
    ),
    _rule("external_share", "google_drive", ["POST", "PATCH"], r"/permissions\b"),
    _rule("external_share", "google_drive", ["POST"], r"/files/[^/]+/copy\b"),
    # --- publication ------------------------------------------------------------------
    _rule("publish_external", "linkedin", ["POST", "PUT"], r"/(ugcPosts|posts|shares)\b"),
)


def classify(provider: str, method: str, path: str, body: object) -> frozenset[str]:
    """Every forbidden-class label this request would carry. Provider-generic."""
    method_upper = method.upper()
    body_text = _body_text(body)
    labels: set[str] = set()
    if method_upper == "DELETE":
        labels.add("delete_any")
    for rule in CLASS_RULES:
        if rule.provider is not None and rule.provider != provider:
            continue
        if method_upper not in rule.methods:
            continue
        if not rule.path_pattern.search(path):
            continue
        if rule.body_pattern is not None and not rule.body_pattern.search(body_text):
            continue
        labels.add(rule.action_class)
    return frozenset(labels)


def _body_text(body: object) -> str:
    """Everything a body says, including text hidden inside base64url `raw` fields.

    Gmail drafts carry the recipient and the message inside an RFC 2822 blob encoded as
    base64url. A deny-list that only read the JSON envelope would be blind to it, so the
    decoded text is appended to the searchable body.
    """
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    try:
        rendered = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(body)
    decoded = "\n".join(_decoded_raw_fields(body))
    return f"{rendered}\n{decoded}" if decoded else rendered


def body_text(body: object) -> str:
    """Public form of the searchable body text (JSON rendering plus decoded base64url `raw`)."""
    return _body_text(body)


def _decoded_raw_fields(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, item in cast(Mapping[object, object], value).items():
            if str(raw_key) == "raw" and isinstance(item, str):
                text = decode_base64url_text(item)
                if text:
                    found.append(text)
            else:
                found.extend(_decoded_raw_fields(item))
    elif isinstance(value, (list, tuple)):
        for item in cast(Sequence[object], value):
            found.extend(_decoded_raw_fields(item))
    return found


def decode_base64url_text(value: str) -> str:
    """Decode a base64url (or base64) string to text, or return '' if it is not one."""
    import base64
    import binascii

    stripped = value.strip()
    if len(stripped) < 8:
        return ""
    padded = stripped + "=" * (-len(stripped) % 4)
    try:
        data = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        try:
            data = base64.b64decode(padded.encode("ascii"), validate=False)
        except (binascii.Error, ValueError, UnicodeEncodeError):
            return ""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    return text if text and printable / len(text) > 0.95 else ""


def request_text(action_provider: str, path: str, query: Mapping[str, str], body: object) -> str:
    """Everything a request carries, as one searchable string."""
    parts = [action_provider, path, json.dumps(dict(query), sort_keys=True), _body_text(body)]
    return "\n".join(part for part in parts if part)


def _leaf_field_names(body: object, *, prefix: str = "") -> set[str]:
    """Field names a write touches, flattened. `properties.email` and `email` both count."""
    names: set[str] = set()
    if isinstance(body, Mapping):
        for raw_key, value in cast(Mapping[object, object], body).items():
            key = str(raw_key)
            names.add(key)
            if prefix:
                names.add(f"{prefix}.{key}")
            names |= _leaf_field_names(value, prefix=key)
    elif isinstance(body, (list, tuple)):
        for item in cast(Sequence[object], body):
            names |= _leaf_field_names(item, prefix=prefix)
    return names


# --------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------

# Wrapper keys that carry no authority of their own — a body of {"properties": {...}} is
# a write to the inner fields, so these never count as smuggled fields.
_TRANSPARENT_FIELD_KEYS: frozenset[str] = frozenset(
    {
        "properties",
        "fields",
        "data",
        "payload",
        "attributes",
        "input",
        "variables",
        "metadata",
        "object",
        "record",
        "resource",
        "value",
        "values",
        "update",
        "patch",
        "message",
        "raw",
    }
)


class PolicyRuleSet(Protocol):
    """Extra refusals layered onto the gate, e.g. a `benchpress.packs.PolicyPack`. Can only add refusals."""

    def refusal(self, action: Action, context: Context) -> tuple[str, str] | None: ...


@dataclass
class Gate:
    """Stateful only in the sense that it remembers fingerprints it has already allowed."""

    context: Context
    allow_unplanned: bool = False
    policy_packs: Sequence[PolicyRuleSet] = ()
    _succeeded: set[str] = field(default_factory=set[str])

    # -- public ------------------------------------------------------------------------

    def check(self, action: Action) -> GateVerdict:
        """Return an allowing verdict, or raise `GateRefusal`."""
        rule, reason = self._first_refusal(action)
        if rule:
            verdict = GateVerdict(action_id=action.id, allowed=False, rule=rule, reason=reason)
            self.context.refusals.append(verdict)
            raise GateRefusal(rule, reason)
        return GateVerdict(action_id=action.id, allowed=True, rule="allowed")

    def evaluate(self, action: Action) -> GateVerdict:
        """Non-raising form, for tests and for the receipt."""
        rule, reason = self._first_refusal(action)
        if rule:
            return GateVerdict(action_id=action.id, allowed=False, rule=rule, reason=reason)
        return GateVerdict(action_id=action.id, allowed=True, rule="allowed")

    def record_success(self, fingerprint: str) -> None:
        self._succeeded.add(fingerprint)

    def has_succeeded(self, fingerprint: str) -> bool:
        return fingerprint in self._succeeded

    # -- rules, in refusal priority order ----------------------------------------------

    def _first_refusal(self, action: Action) -> tuple[str, str]:
        for rule in (
            self._rule_control_plane,
            self._rule_policy_packs,
            self._rule_method,
            self._rule_action_class,
            self._rule_protected,
            self._rule_provider_scope,
            self._rule_plan_membership,
            self._rule_field_smuggling,
            self._rule_external_destination,
            self._rule_idempotency,
        ):
            refusal = rule(action)
            if refusal is not None:
                return refusal
        return ("", "")

    def _rule_control_plane(self, action: Action) -> tuple[str, str] | None:
        path = action.path if action.path.startswith("/") else f"/{action.path}"
        lowered = path.casefold()
        if lowered in BLOCKED_EXACT_PATHS:
            return ("control_plane", f"path {action.path!r} is a control-plane root")
        for prefix in BLOCKED_PATH_PREFIXES:
            if lowered == prefix or lowered.startswith(f"{prefix}/") or lowered.startswith(f"{prefix}?"):
                return ("control_plane", f"path {action.path!r} matches blocked prefix {prefix!r}")
        if "://" in action.path:
            return ("control_plane", "absolute URLs are not a data-plane path")
        if _GRAPHQL_INTROSPECTION.search(_body_text(action.body)):
            return ("control_plane", "GraphQL introspection is forbidden")
        return None

    def _rule_policy_packs(self, action: Action) -> tuple[str, str] | None:
        """Pack rules run before the generic rules so a refusal names the specific pack rule id."""
        if not action.is_write:
            return None
        for pack in self.policy_packs:
            refusal = pack.refusal(action, self.context)
            if refusal is not None:
                return refusal
        return None

    def _rule_method(self, action: Action) -> tuple[str, str] | None:
        if action.method == "DELETE":
            return ("method", "DELETE is never permitted")
        return None

    def _rule_action_class(self, action: Action) -> tuple[str, str] | None:
        if not action.is_write:
            return None
        labels = classify(action.provider, action.method, action.path, action.body)
        forbidden = sorted(label for label in labels if self.context.dod.forbids(label))
        if forbidden:
            return ("action_class", f"action class {forbidden[0]!r} is forbidden by the definition of done")
        return None

    def _rule_protected(self, action: Action) -> tuple[str, str] | None:
        if not action.is_write:
            return None
        chosen = self.context.target_refs()
        # Identity match first: a path segment or declared target ref that *is* a protected
        # id or name, whatever its length. Short numeric ids never slip through here.
        protected_ids = self.context.protected.ids
        protected_names = {casefold_text(name) for name in self.context.protected.names}
        segments = [segment for segment in action.path.split("?", 1)[0].split("/") if segment]
        for ref in action.target_refs:
            segments.append(ref)
            segments.append(ref.split(":", 1)[-1])
        for segment in segments:
            if segment in chosen:
                continue
            if segment in protected_ids or casefold_text(segment) in protected_names:
                return ("protected", f"request targets protected record {segment!r}")
        text = request_text(action.provider, action.path, action.query, action.body)
        hit = self.context.protected.hit(text)
        if hit is None:
            return None
        # A protected term may legitimately appear when the write's own target is what the
        # deny-list would otherwise catch — but only if that exact term is a chosen target.
        if hit in chosen:
            return None
        return ("protected", f"request references protected term {hit!r}")

    def _rule_provider_scope(self, action: Action) -> tuple[str, str] | None:
        if not action.is_write:
            return None
        scope = self.context.dod.write_scope
        if scope and action.provider not in scope:
            return ("provider_scope", f"provider {action.provider!r} is outside the authorized write scope")
        return None

    def _rule_plan_membership(self, action: Action) -> tuple[str, str] | None:
        if not action.is_write or self.allow_unplanned:
            return None
        planned = self.context.plan.by_id(action.id)
        if planned is None:
            return ("plan_membership", f"action {action.id!r} is not on the approved plan")
        if (planned.provider, planned.method, _path_shape(planned.path)) != (
            action.provider,
            action.method,
            _path_shape(action.path),
        ):
            return ("plan_membership", f"action {action.id!r} does not match its planned shape")
        if not planned.satisfies:
            return ("plan_membership", f"action {action.id!r} satisfies no definition-of-done item")
        return None

    def _rule_field_smuggling(self, action: Action) -> tuple[str, str] | None:
        if not action.is_write or action.body is None or not action.fields:
            return None
        declared = {name.casefold() for name in action.fields} | _TRANSPARENT_FIELD_KEYS
        touched = {name.casefold() for name in _leaf_field_names(action.body)}
        smuggled = sorted(name for name in touched if name not in declared and "." not in name)
        if smuggled:
            return ("field_smuggling", f"body writes undeclared field(s): {', '.join(smuggled[:4])}")
        return None

    def _rule_external_destination(self, action: Action) -> tuple[str, str] | None:
        if not action.is_write:
            return None
        body_text = _body_text(action.body)
        if not body_text:
            return None
        allowed = set(self.context.known_domains()) | _internal_domains(self.context)
        for address in sorted(emails_in(body_text)):
            host = address.split("@", 1)[1]
            if not _domain_allowed(host, allowed):
                return ("external_destination", f"body addresses external recipient {address!r}")
        for host in sorted(domains_in(body_text) - set(emails_in(body_text))):
            if not _domain_allowed(host, allowed):
                return ("external_destination", f"body references external destination {host!r}")
        return None

    def _rule_idempotency(self, action: Action) -> tuple[str, str] | None:
        if not action.is_write:
            return None
        if self.has_succeeded(fingerprint(action)):
            return ("idempotency", f"action {action.id!r} already succeeded; replay would duplicate")
        return None


def _domain_allowed(host: str, allowed: Iterable[str]) -> bool:
    host = host.casefold()
    for candidate in allowed:
        candidate = candidate.casefold()
        if host == candidate or host.endswith(f".{candidate}"):
            return True
    return False


def _internal_domains(context: Context) -> set[str]:
    """Domains that are structurally internal: every domain seen in the prompt, in a
    discovered policy, or in the provider hostnames the twins expose."""
    found: set[str] = set()
    found |= set(domains_in(context.user_prompt))
    for policy in context.policies:
        found |= set(domains_in(policy.quote))
        found |= set(domains_in(policy.resource_ref))
    for candidate in context.candidates:
        if candidate.domain:
            found.add(candidate.domain.casefold())
        if candidate.email:
            found.add(candidate.email.split("@", 1)[-1].casefold())
    return found - set(context.protected.domains)


def _path_shape(path: str) -> str:
    """Path with identifier-looking segments collapsed, so a planned shape survives a
    concrete id substitution but not a change of endpoint."""
    segments = path.split("?", 1)[0].strip("/").split("/")
    shaped = [("{id}" if _looks_like_id(segment) else segment.casefold()) for segment in segments]
    return "/" + "/".join(shaped)


def _looks_like_id(segment: str) -> bool:
    if not segment:
        return False
    if segment.isdigit():
        return True
    folded = casefold_text(segment)
    has_digit = any(char.isdigit() for char in folded)
    return has_digit and len(folded) >= 4


def fingerprint(action: Action) -> str:
    """Stable identity of a mutation.

    Uses the *concrete* path, never the collapsed shape: two writes to two different
    records are two different mutations, and conflating them would let the idempotency
    rule silently suppress a legitimate second write.
    """
    import hashlib

    payload = json.dumps(
        {
            "provider": action.provider,
            "method": action.method,
            "path": action.path.split("?", 1)[0].rstrip("/").casefold(),
            "query": dict(sorted(action.query.items())),
            "body": _canonical_body(action.body),
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _canonical_body(body: object) -> object:
    if isinstance(body, Mapping):
        items = sorted(cast(Mapping[object, object], body).items(), key=lambda kv: str(kv[0]))
        return {str(key): _canonical_body(value) for key, value in items}
    if isinstance(body, (list, tuple)):
        return [_canonical_body(item) for item in cast(Sequence[object], body)]
    if isinstance(body, str):
        return casefold_text(body)
    return body


def planned_actions_only(actions: Sequence[Action]) -> tuple[Action, ...]:
    """Drop any action that satisfies no definition-of-done item (minimum mutation)."""
    return tuple(action for action in actions if not action.is_write or action.satisfies)
