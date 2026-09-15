"""Real-app gateway: the harness's two candidate tools, backed by real provider hosts.

`RealAppGateway.execute_tool` is a drop-in for the ArgaBench provider gateway
(`arga_twins_benchmark/providers/gateway.py`, vendored at `4a81785`; line numbers below refer
to that file). It exposes the same `provider_api` / `provider_docs` schemas, applies the same
request validation, returns the same result envelope and trace record, and computes the same
fingerprints — but requests go to `https://slack.com`, `https://api.stripe.com`, … or to a
local twin, by base-URL swap. The stock baseline and Benchpress run through the very same
object, so neither agent sees anything the other does not.

Where this gateway deliberately differs from the harness (both disclosed in the brief):

- 429 / 502 / 503 / 504 responses are retried up to `max_attempts` times honouring
  `Retry-After` (real apps rate-limit; twins do not). One provider_api attempt is still one
  trace record, with the same fingerprint, and `RealAppGateway.retries` counts the extra sends.
- Transport failures are reported as `error: "transport:<httpx class>"` instead of the harness's
  `"provider request failed: …"` wording, so the reliability brief can bucket them.

Nothing here knows a task, a seeded name, or a domain. Scenario data lives in `evals/`, which
this module never imports.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, MutableSequence, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Final, Literal, cast
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import httpx

from benchpress.tools import PROVIDER_API, PROVIDER_DOCS, ToolExecutor

AuthProvider = Callable[[], Awaitable[Mapping[str, str]]]
SleepFn = Callable[[float], Awaitable[None]]

USER_AGENT: Final = "benchpress-realapp/0.1"
DEFAULT_MAX_CALLS: Final = 160
DEFAULT_MAX_DOCS_CALLS: Final = 40
GMAIL_TOKEN_URL: Final = "https://oauth2.googleapis.com/token"

# Provider name -> harness role (reporting/argabench_fair.py:47-60). Roles are harness
# vocabulary, not task data: every scenario aliases its twins the same way.
PROVIDER_ROLES: Final[Mapping[str, str]] = {
    "github": "code_host",
    "gmail": "email",
    "google_calendar": "calendar",
    "google_drive": "file_storage",
    "hubspot": "hubspot_crm",
    "jira": "jira_tracker",
    "linear": "linear_tracker",
    "linkedin": "professional_network",
    "notion": "knowledge_base",
    "salesforce": "salesforce_crm",
    "slack": "team_chat",
    "stripe": "payments",
}

# Real data-plane hosts. Paths stay exactly as on the twins (`/api/chat.postMessage`,
# `/v1/customers`, `/crm/v3/objects/...`, `/gmail/v1/users/me/...`, `/repos/...`, `/graphql`).
REAL_BASE_URLS: Final[Mapping[str, str]] = {
    "slack": "https://slack.com",
    "stripe": "https://api.stripe.com",
    "hubspot": "https://api.hubapi.com",
    "gmail": "https://gmail.googleapis.com",
    "github": "https://api.github.com",
    "linear": "https://api.linear.app",
}

# Environment variable holding the static credential for each real provider.
TOKEN_ENV_VARS: Final[Mapping[str, str]] = {
    "slack": "SLACK_BOT_TOKEN",
    "stripe": "STRIPE_SECRET_KEY",
    "hubspot": "HUBSPOT_PRIVATE_APP_TOKEN",
    "github": "GITHUB_TOKEN",
    "linear": "LINEAR_API_KEY",
}

# What a local twin receives in its auth header: the harness's own defaults where it has them
# (gateway.py:1011-1057), so a twin sees the request shape the real gateway would send.
_TWIN_TOKENS: Final[Mapping[str, str]] = {
    "slack": "xoxb-F9SXMECOSFOGYR3XKXWN",
    "stripe": "sk_test_twin_scenario",
    "gmail": "ya29.gmail-twin-owner",
    "github": "ghp_scenario_seed",
    "linear": "lin_api_twin_owner_personal_key_0001",
    "hubspot": "pat-na1-twin-scenario",
    "jira": "jira_default_seed_token",
    "notion": "secret_notion-twin_seed",
    "google_calendar": "test-token",
    "google_drive": "ya29.drive-twin-owner",
    "salesforce": "00D-twin-scenario-token",
    "linkedin": "AQV-twin-scenario-token",
}

# --- constants mirrored from the harness gateway ------------------------------------------

# gateway.py:20
_ALLOWED_METHODS: Final = frozenset({"GET", "POST", "PATCH", "PUT", "DELETE"})
# gateway.py:57-79
_CONTROL_PLANE_PREFIXES: Final = frozenset(
    {
        "_admin",
        "_control",
        "_grader",
        "_grading",
        "_inspect",
        "_reset",
        "_seed",
        "_twin",
        "_ui",
        "admin",
        "control",
        "control-plane",
        "control_plane",
        "grade",
        "grader",
        "grading",
        "inspect",
        "reset",
        "seed",
    }
)
# gateway.py:80-101
_SCHEMA_DISCOVERY_SEGMENTS: Final = frozenset(
    {
        "$discovery",
        "api-docs",
        "discovery",
        "docs",
        "mcp",
        "openapi",
        "openapi.json",
        "openapi.yaml",
        "openapi.yml",
        "redoc",
        "schema",
        "schemas",
        "swagger",
        "swagger_doc",
        "swagger.json",
        "swagger.yaml",
        "swagger.yml",
        "ui",
    }
)
# gateway.py:102-104
_API_SURFACE_PREFIXES: Final = frozenset({"api"})
_API_VERSION_SEGMENT: Final = re.compile(r"^v[0-9]+(?:\.[0-9]+)*$")
_OPERATIONAL_DISCOVERY_SEGMENTS: Final = frozenset({"health", "healthz", "metrics", "readiness", "ready"})
# gateway.py:105-133
_BLOCKED_REQUEST_HEADERS: Final = frozenset(
    {
        "authorization",
        "connection",
        "content-length",
        "content-type",
        "cookie",
        "forwarded",
        "host",
        "proxy-authorization",
        "proxy-connection",
        "set-cookie",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-prefix",
        "x-forwarded-proto",
        "x-http-method-override",
        "x-method-override",
        "x-original-url",
        "x-real-ip",
        "x-request-id",
        "x-rewrite-url",
    }
)
_BLOCKED_REQUEST_HEADER_PREFIXES: Final = ("x-arga-", "x-admin-", "x-twin-")
# gateway.py:134-151
_SENSITIVE_RESPONSE_HEADERS: Final = frozenset(
    {"authorization", "cookie", "proxy-authenticate", "proxy-authorization", "set-cookie"}
)
_CREDENTIAL_HEADERS: Final = frozenset(
    {"authorization", "private-token", "proxy-authorization", "x-api-key", "x-auth-token"}
)
# gateway.py:152-153
_REDACTED_VALUE: Final = "[redacted]"
_REDACTED_HOST: Final = "[provider-host]"
# gateway.py:154-166
_ACTION_FINGERPRINT_IGNORED_HEADERS: Final = frozenset(
    {
        "idempotency-key",
        "if-match",
        "if-modified-since",
        "if-none-match",
        "if-unmodified-since",
        "retry-after",
        "x-idempotency-key",
        "x-retry-attempt",
        "x-retry-count",
    }
)
# gateway.py:167-169
_HEADER_NAME: Final = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_GRAPHQL_DECLARATION: Final = re.compile(r"\b(query|mutation|subscription)\b(?:\s+([_A-Za-z][_0-9A-Za-z]*))?")
_GRAPHQL_ROOT_FIELD: Final = re.compile(r"\{\s*(?:@[A-Za-z_][_0-9A-Za-z]*(?:\([^)]*\))?\s*)*([_A-Za-z][_0-9A-Za-z]*)")
_GRAPHQL_INTROSPECTION: Final = re.compile(r"(?<![_0-9A-Za-z])(?:__schema(?![_0-9A-Za-z])|__type\s*\()")

_RETRY_STATUSES: Final = frozenset({429, 502, 503, 504})
_MAX_RETRY_DELAY_SECONDS: Final = 30.0
_MISSING: Final = object()


class ScratchGuardError(RuntimeError):
    """Raised when a gateway would reach anything that is not a scratch/test account."""


class AuthError(RuntimeError):
    """Raised when a dynamic credential (Gmail OAuth refresh) cannot be obtained."""


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RealAppConfig:
    """One provider's data-plane access: where requests go and what authenticates them."""

    provider: str
    role: str
    base_url: str
    headers: Mapping[str, str] = field(default_factory=dict[str, str], repr=False)
    auth: AuthProvider | None = field(default=None, repr=False)
    default_body_encoding: Literal["json", "form"] = "json"

    def __post_init__(self) -> None:
        for label, value in (("provider", self.provider), ("role", self.role)):
            if not value or value.strip() != value:
                raise ValueError(f"invalid {label} {value!r}")
        object.__setattr__(self, "base_url", _validate_base_url(self.base_url, provider=self.provider))
        object.__setattr__(self, "headers", dict(self.headers))

    @property
    def host(self) -> str:
        return (urlsplit(self.base_url).hostname or "").casefold()

    @property
    def is_loopback(self) -> bool:
        return _is_loopback_host(self.host)

    @classmethod
    def for_provider(
        cls,
        provider: str,
        *,
        base_url: str,
        token: str | None = None,
        auth: AuthProvider | None = None,
        role: str | None = None,
    ) -> RealAppConfig:
        """Build the config with the provider's native header shape (gateway.py:1011-1057)."""
        resolved_role = role or PROVIDER_ROLES.get(provider)
        if resolved_role is None:
            known = ", ".join(sorted(PROVIDER_ROLES))
            raise ValueError(f"unknown provider {provider!r} (no harness role); known providers: {known}")
        return cls(
            provider=provider,
            role=resolved_role,
            base_url=base_url,
            headers=provider_headers(provider, token),
            auth=auth,
            default_body_encoding="form" if provider == "stripe" else "json",
        )


def provider_headers(provider: str, token: str | None) -> dict[str, str]:
    """Provider-native static headers, shaped like the harness's (gateway.py:1011-1057)."""
    if provider == "github":
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers
    if provider == "linear":
        return {"Authorization": token} if token else {}
    if provider == "notion":
        headers = {"Notion-Version": "2026-03-11"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers
    if provider == "linkedin":
        headers = {"LinkedIn-Version": "202608", "X-RestLi-Protocol-Version": "2.0.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers
    return {"Authorization": f"Bearer {token}"} if token else {}


# --------------------------------------------------------------------------------------
# Gateway
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _ResponsePolicy:
    """What to scrub from provider responses (gateway.py:1136-1160)."""

    url_patterns: tuple[re.Pattern[str], ...]
    host_patterns: tuple[re.Pattern[str], ...]
    secrets: tuple[str, ...]


class RealAppGateway:
    """The candidate-facing tool executor for real apps (and twins).

    Every provider_api attempt — blocked, over-limit, failed or successful — becomes exactly one
    trace record, numbered from 1, with the harness's fingerprints. Credentials never appear in
    a result, a trace record, or `repr()`.

    `trace_limit` keeps only that many of the newest records (a long-lived gateway passes 0); `calls`,
    the numbering and the `max_calls` cap still count every attempt. The default keeps them all.
    """

    def __init__(
        self,
        configs: Sequence[RealAppConfig],
        *,
        max_calls: int = DEFAULT_MAX_CALLS,
        max_docs_calls: int = DEFAULT_MAX_DOCS_CALLS,
        trace_limit: int | None = None,
        docs_executor: ToolExecutor | None = None,
        timeout: float = 60.0,
        response_limit_bytes: int = 200_000,
        request_limit_bytes: int = 262_144,
        max_attempts: int = 3,
        env: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: SleepFn | None = None,
        user_agent: str = USER_AGENT,
    ) -> None:
        if not configs:
            raise ValueError("at least one RealAppConfig is required")
        for label, value in (
            ("max_calls", max_calls),
            ("max_docs_calls", max_docs_calls),
            ("response_limit_bytes", response_limit_bytes),
            ("request_limit_bytes", request_limit_bytes),
            ("max_attempts", max_attempts),
        ):
            if value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if trace_limit is not None and trace_limit < 0:
            raise ValueError("trace_limit must be a non-negative integer or None")
        environment = os.environ if env is None else env

        self._configs: dict[str, RealAppConfig] = {}
        self._roles: dict[str, str] = {}
        for config in configs:
            if config.provider in self._configs:
                raise ValueError(f"duplicate provider {config.provider!r}")
            _guard_config(config, environment)
            self._configs[config.provider] = config
        for config in configs:
            if config.role in self._configs and config.role != config.provider:
                raise ValueError(f"provider role {config.role!r} conflicts with a provider name")
            if config.role in self._roles and self._roles[config.role] != config.provider:
                raise ValueError(f"provider role {config.role!r} is claimed by two providers")
            self._roles[config.role] = config.provider

        self._max_calls = max_calls
        self._max_docs_calls = max_docs_calls
        self._docs_executor = docs_executor
        self._timeout = timeout
        self._response_limit_bytes = response_limit_bytes
        self._request_limit_bytes = request_limit_bytes
        self._max_attempts = max_attempts
        self._sleep: SleepFn = sleep or _default_sleep
        self._user_agent = user_agent
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport)
        self._trace: MutableSequence[dict[str, Any]] = [] if trace_limit is None else deque(maxlen=trace_limit)
        self._call_count = 0
        self._docs_calls = 0
        self._retries = 0
        self._secrets: set[str] = set()
        for config in self._configs.values():
            self._secrets.update(_credential_secrets(config.headers))
        self._policy = self._build_policy()

        provider_tokens = sorted(set(self._configs) | set(self._roles))
        # Verbatim from the harness (gateway.py:279-338), with the same dynamic enum.
        self._api_definition: dict[str, Any] = {
            "name": PROVIDER_API,
            "description": (
                "Call an API on one of this task's provisioned service twins. Use only a provider or provider role "
                "listed in the schema and a relative path beginning with '/'. Absolute URLs and twin control-plane "
                "paths are blocked. Provider roots, UI routes, and API-schema discovery routes are also blocked; use "
                "the provider_docs tool for official API documentation. Authentication is supplied automatically. "
                "JSON bodies are the default; Stripe form bodies are selected automatically, or body_encoding can "
                "be set explicitly."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": provider_tokens,
                        "description": "Provisioned provider name or task-specific provider role.",
                    },
                    "method": {"type": "string", "enum": sorted(_ALLOWED_METHODS)},
                    "path": {
                        "type": "string",
                        "description": "Relative provider API path beginning with '/'; it may include a query string.",
                    },
                    "query": {
                        "type": "object",
                        "description": "Optional query parameters. Keys are encoded in deterministic sorted order.",
                        "additionalProperties": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "number"},
                                {"type": "integer"},
                                {"type": "boolean"},
                                {"type": "array", "items": {"type": ["string", "number", "integer", "boolean"]}},
                            ]
                        },
                    },
                    "body": {"description": "Optional JSON value or form field object for the request body."},
                    "body_encoding": {
                        "type": "string",
                        "enum": ["json", "form"],
                        "description": "Optional body encoding. Defaults to form for Stripe and JSON otherwise.",
                    },
                    "headers": {
                        "type": "object",
                        "description": (
                            "Optional non-authentication request headers, for example Idempotency-Key or If-Match."
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["provider", "method", "path"],
                "additionalProperties": False,
            },
        }
        # Verbatim from the harness official-docs gateway (official_docs.py:703-745).
        self._docs_definition: dict[str, Any] = {
            "name": PROVIDER_DOCS,
            "description": (
                "Discover and read actual official API documentation for this task's provisioned providers. "
                "This read-only tool fetches only provider-specific official host/path allowlists, follows only "
                "allowlisted redirects, and never exposes twin URLs or credentials. Search the official-doc "
                "index, fetch a doc_id, then follow only URLs returned in that document's links."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": provider_tokens,
                        "description": "Provisioned provider name or task-specific provider role.",
                    },
                    "action": {"type": "string", "enum": ["search", "fetch"]},
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional search terms. For fetch, matching excerpts from the official document are "
                            "returned; omit it to read from the beginning."
                        ),
                        "maxLength": 512,
                    },
                    "doc_id": {
                        "type": "string",
                        "description": "Catalog document ID returned by search. Use either doc_id or url for fetch.",
                        "maxLength": 256,
                    },
                    "url": {
                        "type": "string",
                        "description": (
                            "An official documentation URL previously returned by this tool. It must remain inside "
                            "the selected provider's exact host/path allowlist."
                        ),
                        "maxLength": 2048,
                    },
                },
                "required": ["provider", "action"],
                "additionalProperties": False,
            },
        }

    # -- public surface ------------------------------------------------------------------

    def tool_schema(self) -> list[dict[str, Any]]:
        return [_copy_json(self._api_definition), _copy_json(self._docs_definition)]

    @property
    def trace(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(record) for record in self._trace)

    @property
    def calls(self) -> int:
        return self._call_count

    @property
    def docs_calls(self) -> int:
        return self._docs_calls

    @property
    def retries(self) -> int:
        """Extra sends spent on 429/502/503/504 retries (never visible in the trace)."""
        return self._retries

    def provider_names(self) -> tuple[str, ...]:
        return tuple(self._configs)

    def roles(self) -> dict[str, str]:
        return dict(self._roles)

    def describe(self) -> dict[str, dict[str, Any]]:
        """A credential-free description of the configured providers, for run manifests."""
        return {
            name: {
                "role": config.role,
                "base_url": config.base_url,
                "loopback": config.is_loopback,
                "headers": _redact(config.headers),
                "auth": "dynamic" if config.auth is not None else "static",
                "default_body_encoding": config.default_body_encoding,
            }
            for name, config in self._configs.items()
        }

    def __repr__(self) -> str:
        return f"RealAppGateway(providers={self.provider_names()!r}, calls={self.calls}, docs_calls={self._docs_calls})"

    async def __aenter__(self) -> RealAppGateway:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        if tool_name == PROVIDER_API:
            return await self._execute_api(tool_input)
        if tool_name == PROVIDER_DOCS:
            return await self._execute_docs(tool_input)
        return {"ok": False, "error": f"unknown tool {tool_name!r}; available tools: {PROVIDER_API}, {PROVIDER_DOCS}"}

    # -- provider_api (mirrors gateway.py:360-560) -----------------------------------------

    async def _execute_api(self, tool_input: Mapping[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        started_at = datetime.now(UTC).isoformat()
        requested_provider = _string_value(tool_input.get("provider")) or ""
        resolved_provider = self._roles.get(requested_provider, requested_provider) or None
        known_provider = resolved_provider if resolved_provider in self._configs else None
        method = (_string_value(tool_input.get("method")) or "").upper() or None
        raw_path = _string_value(tool_input.get("path"))
        operation, operation_type = _infer_graphql_operation(tool_input.get("body"))
        effective_path: str | None = raw_path
        request_fingerprint: str | None = None
        action_fingerprint: str | None = None
        try:
            attempt_fingerprint: str | None = _sha256_json(dict(tool_input))
        except (TypeError, ValueError):
            attempt_fingerprint = None

        def failure(error: str) -> dict[str, Any]:
            trace = self._append_trace(
                started_at=started_at,
                requested_provider=requested_provider,
                provider=known_provider,
                method=method,
                path=effective_path,
                operation=operation,
                operation_type=operation_type,
                status_code=None,
                latency_ms=_elapsed_ms(started),
                response_bytes=0,
                truncated=False,
                error=error,
                request_fingerprint=request_fingerprint,
                action_fingerprint=action_fingerprint,
                attempt_fingerprint=attempt_fingerprint,
            )
            return {
                "ok": False,
                "requested_provider": requested_provider,
                "provider": known_provider,
                "method": method,
                "path": effective_path,
                "status_code": None,
                "headers": {},
                "body": None,
                "truncated": False,
                "error": error,
                "trace": trace,
            }

        try:
            if self._call_count >= self._max_calls:
                raise ValueError(f"provider_api call limit of {self._max_calls} has been reached")
            provider_name, config = self._resolve_provider(requested_provider)
            checked_method = _validate_method(method)
            path, path_query = _validate_relative_path(raw_path)
            query_pairs = _merge_query_pairs(path_query, tool_input.get("query"))
            _validate_candidate_safe_request(path, query_pairs=query_pairs, body=tool_input.get("body", _MISSING))
            safe_headers = _validate_custom_headers(tool_input.get("headers"))
            body_encoding = _validate_body_encoding(tool_input.get("body_encoding"), config.default_body_encoding)
            request_content, body_headers, request_size = _prepare_body(
                body=tool_input.get("body", _MISSING), body_encoding=body_encoding
            )
            if request_size > self._request_limit_bytes:
                raise ValueError(f"request body exceeds the {self._request_limit_bytes}-byte gateway limit")

            headers = dict(config.headers)
            if config.auth is not None:
                headers.update(await self._dynamic_headers(config))
            headers.update(body_headers)
            headers.update(safe_headers)
            headers.setdefault("User-Agent", self._user_agent)
            encoded_query = urlencode(query_pairs)
            request_path = f"{path}?{encoded_query}" if encoded_query else path
            request = self._client.build_request(
                checked_method,
                f"{config.base_url}{request_path}",
                headers=headers,
                timeout=self._timeout,
                content=request_content,
            )
            effective_path = request.url.raw_path.decode("ascii", errors="replace")
            request_fingerprint, action_fingerprint = request_fingerprints(
                tool_input,
                provider=provider_name,
                effective_path=effective_path,
                default_body_encoding=config.default_body_encoding,
            )
            response, response_bytes, truncated = await self._send_with_retries(request)
            response_body = _decode_body(response_bytes, response.headers.get("content-type"), truncated=truncated)
            trace = self._append_trace(
                started_at=started_at,
                requested_provider=requested_provider,
                provider=provider_name,
                method=checked_method,
                path=effective_path,
                operation=operation,
                operation_type=operation_type,
                status_code=response.status_code,
                latency_ms=_elapsed_ms(started),
                response_bytes=len(response_bytes),
                truncated=truncated,
                error=None,
                request_fingerprint=request_fingerprint,
                action_fingerprint=action_fingerprint,
                attempt_fingerprint=attempt_fingerprint,
            )
            return {
                "ok": response.is_success,
                "requested_provider": requested_provider,
                "provider": provider_name,
                "method": checked_method,
                "path": effective_path,
                "status_code": response.status_code,
                "headers": _safe_response_headers(response.headers, policy=self._policy),
                "body": _sanitize_value(response_body, policy=self._policy),
                "truncated": truncated,
                "error": None,
                "trace": trace,
            }
        except AuthError as exc:
            return failure(f"auth:{exc}")
        except httpx.TransportError as exc:
            return failure(f"transport:{exc.__class__.__name__}")
        except httpx.HTTPError as exc:
            return failure(f"provider request failed: {exc.__class__.__name__}")
        except ValueError as exc:
            return failure(str(exc))

    async def _send_with_retries(self, request: httpx.Request) -> tuple[httpx.Response, bytes, bool]:
        attempt = 1
        while True:
            response = await self._client.send(request, stream=True, follow_redirects=False)
            try:
                response_bytes, truncated = await _read_bounded(response, self._response_limit_bytes)
            finally:
                await response.aclose()
            if response.status_code in _RETRY_STATUSES and attempt < self._max_attempts:
                self._retries += 1
                await self._sleep(_retry_delay(response.headers.get("retry-after"), attempt))
                attempt += 1
                continue
            return response, response_bytes, truncated

    async def _dynamic_headers(self, config: RealAppConfig) -> dict[str, str]:
        assert config.auth is not None
        try:
            dynamic = dict(await config.auth())
        except AuthError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise AuthError(f"{config.provider} credential provider failed: {exc.__class__.__name__}") from exc
        fresh = _credential_secrets(dynamic)
        if not fresh.issubset(self._secrets):
            self._secrets.update(fresh)
            self._policy = self._build_policy()
        return dynamic

    def _resolve_provider(self, requested_provider: str) -> tuple[str, RealAppConfig]:
        if not requested_provider:
            raise ValueError("provider must be a non-empty string")
        resolved = self._roles.get(requested_provider, requested_provider)
        try:
            return resolved, self._configs[resolved]
        except KeyError as exc:
            allowed = ", ".join(sorted(set(self._configs) | set(self._roles)))
            raise ValueError(f"unknown provider {requested_provider!r}; allowed providers: {allowed}") from exc

    def _append_trace(
        self,
        *,
        started_at: str,
        requested_provider: str,
        provider: str | None,
        method: str | None,
        path: str | None,
        operation: str | None,
        operation_type: str | None,
        status_code: int | None,
        latency_ms: int,
        response_bytes: int,
        truncated: bool,
        error: str | None,
        request_fingerprint: str | None,
        action_fingerprint: str | None,
        attempt_fingerprint: str | None,
    ) -> dict[str, Any]:
        self._call_count += 1
        # Field order and names match ProviderTraceRecord.to_dict() (gateway.py:195-226).
        record: dict[str, Any] = {
            "sequence": self._call_count,
            "started_at": started_at,
            "requested_provider": requested_provider,
            "provider": provider,
            "method": method,
            "path": path,
            "operation": operation,
            "operation_type": operation_type,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "response_bytes": response_bytes,
            "truncated": truncated,
            "error": error,
            "request_fingerprint": request_fingerprint,
            "action_fingerprint": action_fingerprint,
            "attempt_fingerprint": attempt_fingerprint,
        }
        self._trace.append(record)
        return dict(record)

    def _build_policy(self) -> _ResponsePolicy:
        hosts: set[str] = set()
        for config in self._configs.values():
            parsed = urlsplit(config.base_url)
            if parsed.netloc:
                hosts.add(parsed.netloc.casefold())
            if parsed.hostname:
                hosts.add(parsed.hostname.casefold())
        sorted_hosts = tuple(sorted(hosts, key=lambda value: (-len(value), value)))
        return _ResponsePolicy(
            url_patterns=tuple(re.compile(rf"(?i)(?:https?:)?//{re.escape(host)}(?=[/?#]|$)") for host in sorted_hosts),
            host_patterns=tuple(re.compile(rf"(?i){re.escape(host)}") for host in sorted_hosts),
            secrets=tuple(sorted(self._secrets, key=lambda value: (-len(value), value))),
        )

    # -- provider_docs ---------------------------------------------------------------------

    async def _execute_docs(self, tool_input: Mapping[str, Any]) -> dict[str, Any]:
        requested_provider = _string_value(tool_input.get("provider")) or ""
        resolved = self._roles.get(requested_provider, requested_provider) or None
        # Error envelope shaped like the harness docs gateway (official_docs.py:894-902).
        envelope: dict[str, Any] = {
            "ok": False,
            "requested_provider": requested_provider,
            "provider": resolved if resolved in self._configs else None,
            "action": _string_value(tool_input.get("action")),
            "doc_id": _string_value(tool_input.get("doc_id")),
            "url": _string_value(tool_input.get("url")),
        }
        if self._docs_calls >= self._max_docs_calls:
            return {**envelope, "error": f"provider_docs call limit of {self._max_docs_calls} has been reached"}
        self._docs_calls += 1
        if self._docs_executor is None:
            return {**envelope, "error": "provider_docs unavailable in this substrate"}
        result = await self._docs_executor(PROVIDER_DOCS, dict(tool_input))
        if isinstance(result, Mapping):
            return dict(cast(Mapping[str, Any], result))
        return {**envelope, "ok": True, "result": result}


# --------------------------------------------------------------------------------------
# Construction from the environment
# --------------------------------------------------------------------------------------


def config_from_env(provider: str, env: Mapping[str, str] | None = None) -> RealAppConfig:
    """One provider's config: `DEVSIM_<PROVIDER>_URL` twin, else the real host + token from env."""
    environment = os.environ if env is None else env
    name = provider.strip().casefold()
    if name not in PROVIDER_ROLES:
        raise ValueError(f"unknown provider {provider!r}; known providers: {', '.join(sorted(PROVIDER_ROLES))}")
    twin_var = f"DEVSIM_{name.upper()}_URL"
    twin_url = environment.get(twin_var)
    if twin_url:
        # A twin never receives a real credential, but it does see the provider's normal header.
        return RealAppConfig.for_provider(name, base_url=twin_url, token=_TWIN_TOKENS.get(name, "twin-scenario-token"))
    if name == "gmail":
        return RealAppConfig.for_provider(
            name, base_url=REAL_BASE_URLS["gmail"], auth=GmailTokenProvider(environment).headers
        )
    base_url = REAL_BASE_URLS.get(name)
    if base_url is None:
        raise ValueError(f"no real base URL is configured for provider {name!r}; set {twin_var} to use a local twin")
    token_var = TOKEN_ENV_VARS[name]
    token = environment.get(token_var)
    if not token:
        raise ValueError(
            f"{token_var} is not set; it is required for the real {name} provider (or set {twin_var} for a local twin)"
        )
    return RealAppConfig.for_provider(name, base_url=base_url, token=token)


def gateway_from_env(
    providers: Sequence[str],
    *,
    docs_executor: ToolExecutor | None = None,
    env: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: SleepFn | None = None,
) -> RealAppGateway:
    """A gateway for `providers`, each a real host (token from env) or a `DEVSIM_<P>_URL` twin."""
    environment = os.environ if env is None else env
    configs = [config_from_env(provider, environment) for provider in providers]
    return RealAppGateway(configs, docs_executor=docs_executor, env=environment, transport=transport, sleep=sleep)


class GmailTokenProvider:
    """Bearer headers for Gmail.

    `GMAIL_ACCESS_TOKEN` is used directly when present. Otherwise `GMAIL_CLIENT_ID`,
    `GMAIL_CLIENT_SECRET` and `GMAIL_REFRESH_TOKEN` drive the OAuth refresh grant against
    `oauth2.googleapis.com`; the access token is cached until `skew_seconds` before it expires.
    """

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], float] = time.monotonic,
        skew_seconds: float = 60.0,
        timeout: float = 30.0,
        token_url: str = GMAIL_TOKEN_URL,
    ) -> None:
        environment = os.environ if env is None else env
        self._static_token = environment.get("GMAIL_ACCESS_TOKEN") or None
        self._client_id = environment.get("GMAIL_CLIENT_ID") or ""
        self._client_secret = environment.get("GMAIL_CLIENT_SECRET") or ""
        self._refresh_token = environment.get("GMAIL_REFRESH_TOKEN") or ""
        if self._static_token is None:
            missing = [
                name
                for name, value in (
                    ("GMAIL_CLIENT_ID", self._client_id),
                    ("GMAIL_CLIENT_SECRET", self._client_secret),
                    ("GMAIL_REFRESH_TOKEN", self._refresh_token),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "Gmail credentials are not set: provide GMAIL_ACCESS_TOKEN, or GMAIL_CLIENT_ID + "
                    f"GMAIL_CLIENT_SECRET + GMAIL_REFRESH_TOKEN (missing: {', '.join(missing)})"
                )
        self._now = now
        self._skew = skew_seconds
        self._token_url = token_url
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._cached_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self.refreshes = 0

    @property
    def uses_refresh_flow(self) -> bool:
        return self._static_token is None

    async def token(self) -> str:
        if self._static_token is not None:
            return self._static_token
        async with self._lock:
            if self._cached_token is not None and self._now() < self._expires_at:
                return self._cached_token
            return await self._refresh()

    async def headers(self) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {await self.token()}"}

    async def _refresh(self) -> str:
        try:
            response = await self._client.post(
                self._token_url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                },
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"gmail token refresh failed: transport:{exc.__class__.__name__}") from exc
        payload = _json_object(response)
        if response.status_code >= 400:
            code = payload.get("error") if isinstance(payload.get("error"), str) else f"HTTP {response.status_code}"
            raise AuthError(f"gmail token refresh failed: {code}")
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise AuthError("gmail token refresh failed: response carried no access_token")
        expires_in = payload.get("expires_in", 3600)
        lifetime = (
            float(expires_in) if isinstance(expires_in, int | float) and not isinstance(expires_in, bool) else 3600.0
        )
        self._cached_token = access_token
        self._expires_at = self._now() + max(0.0, lifetime - self._skew)
        self.refreshes += 1
        return access_token

    async def aclose(self) -> None:
        await self._client.aclose()


async def gmail_token_provider(
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: Callable[[], float] = time.monotonic,
) -> AuthProvider:
    """Build a Gmail credential callable and prime it, so a bad refresh token fails here, not mid-run."""
    provider = GmailTokenProvider(env, transport=transport, now=now)
    await provider.token()
    return provider.headers


# --------------------------------------------------------------------------------------
# Guards and redaction
# --------------------------------------------------------------------------------------


def _guard_config(config: RealAppConfig, env: Mapping[str, str]) -> None:
    host = config.host
    if host == "api.stripe.com" or host.endswith(".stripe.com"):
        if config.auth is not None:
            raise ScratchGuardError(f"{config.provider}: Stripe credentials must be a static test-mode key")
        credential = _bearer_credential(config.headers)
        if credential is None or not credential.startswith("sk_test_"):
            raise ScratchGuardError(
                f"{config.provider}: {config.base_url} requires a Stripe test-mode secret key (sk_test_…); refusing"
            )
    if not config.is_loopback and env.get("BENCHPRESS_SCRATCH_OK") != "1":
        raise ScratchGuardError(
            f"{config.provider}: {config.base_url} is not a loopback twin; set BENCHPRESS_SCRATCH_OK=1 to allow "
            "requests to a real scratch account"
        )


def _bearer_credential(headers: Mapping[str, str]) -> str | None:
    for name, value in headers.items():
        if name.casefold() == "authorization":
            scheme, separator, credential = value.strip().partition(" ")
            if separator and scheme.casefold() == "bearer":
                return credential.strip()
            return value.strip()
    return None


def _credential_secrets(headers: Mapping[str, str]) -> set[str]:
    """Credential material to scrub from responses (gateway.py:1141-1152)."""
    secrets: set[str] = set()
    for name, value in headers.items():
        if name.casefold() not in _CREDENTIAL_HEADERS:
            continue
        normalized = value.strip()
        if len(normalized) >= 8:
            secrets.add(normalized)
        scheme, separator, credential = normalized.partition(" ")
        if separator and scheme.casefold() in {"basic", "bearer", "bot", "token"} and len(credential) >= 8:
            secrets.add(credential)
    return secrets


def _redact(headers: Mapping[str, str]) -> dict[str, str]:
    """Header names kept, credential values masked (scheme preserved)."""
    redacted: dict[str, str] = {}
    for name, value in headers.items():
        if name.casefold() in _CREDENTIAL_HEADERS or name.casefold() in _SENSITIVE_RESPONSE_HEADERS:
            scheme, separator, _ = value.strip().partition(" ")
            redacted[name] = f"{scheme} <redacted>" if separator else "<redacted>"
        else:
            redacted[name] = value
    return redacted


def _is_loopback_host(host: str) -> bool:
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return host.endswith(".localhost")


# --------------------------------------------------------------------------------------
# Validation (mirrors gateway.py:706-921)
# --------------------------------------------------------------------------------------


def _validate_base_url(base_url: str, *, provider: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"provider {provider!r} has an invalid HTTP(S) base_url")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"provider {provider!r} base_url cannot contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"provider {provider!r} base_url cannot contain query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError(f"provider {provider!r} base_url cannot contain a path")
    return base_url.rstrip("/")


def _validate_method(method: str | None) -> str:
    if method is None or method not in _ALLOWED_METHODS:
        raise ValueError(f"method must be one of: {', '.join(sorted(_ALLOWED_METHODS))}")
    return method


def _validate_relative_path(raw_path: str | None) -> tuple[str, list[tuple[str, str]]]:
    if raw_path is None or not raw_path:
        raise ValueError("path must be a non-empty string")
    if any(character in raw_path for character in ("\r", "\n", "\x00")):
        raise ValueError("path contains forbidden control characters")
    parsed = urlsplit(raw_path)
    if parsed.scheme or parsed.netloc:
        raise ValueError("absolute or network-path URLs are forbidden")
    if parsed.fragment:
        raise ValueError("URL fragments are forbidden")
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        raise ValueError("path must be relative to the provisioned provider and begin with exactly one '/'")
    decoded_path = _repeated_unquote(parsed.path)
    normalized = decoded_path.replace("\\", "/")
    if normalized.startswith("//"):
        raise ValueError("network-path URLs are forbidden")
    segments = [segment.casefold() for segment in normalized.split("/") if segment]
    if any(segment in {".", ".."} for segment in segments):
        raise ValueError("path traversal is forbidden")
    if segments and segments[0] in _CONTROL_PLANE_PREFIXES:
        raise ValueError(f"provider control-plane segment {segments[0]!r} is forbidden")
    return parsed.path, parse_qsl(parsed.query, keep_blank_values=True)


def _validate_candidate_safe_request(path: str, *, query_pairs: Sequence[tuple[str, str]], body: object) -> None:
    decoded_path = _repeated_unquote(path)
    segments = [segment.casefold() for segment in decoded_path.replace("\\", "/").split("/") if segment]
    if not segments:
        raise ValueError("provider root and UI discovery are forbidden; use provider_docs")
    surface = segments[1:] if segments[0] in _API_SURFACE_PREFIXES else segments
    if not surface:
        raise ValueError("provider API root discovery is forbidden; use provider_docs")
    route = surface[1:] if _API_VERSION_SEGMENT.fullmatch(surface[0]) is not None else surface
    if not route:
        raise ValueError("provider API version root discovery is forbidden; use provider_docs")
    head = route[0]
    if head in _CONTROL_PLANE_PREFIXES:
        raise ValueError(f"provider control-plane segment {head!r} is forbidden")
    if head in _OPERATIONAL_DISCOVERY_SEGMENTS:
        raise ValueError(f"provider operational-discovery path {head!r} is forbidden")
    if head == ".well-known":
        raise ValueError("provider OAuth, MCP, or schema-discovery .well-known path is forbidden; use provider_docs")
    if head in _SCHEMA_DISCOVERY_SEGMENTS:
        raise ValueError(f"provider UI or schema-discovery segment {head!r} is forbidden; use provider_docs")
    if isinstance(body, Mapping):
        query = cast(Mapping[object, object], body).get("query")
        if isinstance(query, str) and _GRAPHQL_INTROSPECTION.search(query) is not None:
            raise ValueError("provider GraphQL schema introspection is forbidden; use provider_docs")
    for key, value in query_pairs:
        if key.casefold() == "query" and _GRAPHQL_INTROSPECTION.search(_repeated_unquote(value)) is not None:
            raise ValueError("provider GraphQL schema introspection is forbidden; use provider_docs")


def _repeated_unquote(value: str) -> str:
    decoded = value
    for _ in range(32):
        next_value = unquote(decoded)
        if next_value == decoded:
            return decoded
        decoded = next_value
    if unquote(decoded) != decoded:
        raise ValueError("URL value is excessively encoded")
    return decoded


def _merge_query_pairs(path_query: list[tuple[str, str]], raw_query: object) -> list[tuple[str, str]]:
    pairs = list(path_query)
    if raw_query is not None:
        if not isinstance(raw_query, Mapping):
            raise ValueError("query must be an object")
        for raw_key, raw_value in cast(Mapping[object, object], raw_query).items():
            if not isinstance(raw_key, str) or not raw_key:
                raise ValueError("query parameter names must be non-empty strings")
            if isinstance(raw_value, Sequence) and not isinstance(raw_value, str | bytes | bytearray):
                for item in cast(Sequence[object], raw_value):
                    pairs.append((raw_key, _query_scalar(item)))
            else:
                pairs.append((raw_key, _query_scalar(raw_value)))
    return sorted(pairs, key=lambda pair: (pair[0], pair[1]))


def _query_scalar(value: object) -> str:
    if value is None or isinstance(value, Mapping | bytes | bytearray):
        raise ValueError("query values must be strings, numbers, booleans, or arrays of those values")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return str(value)
    raise ValueError("query values must be strings, numbers, booleans, or arrays of those values")


def _validate_custom_headers(raw_headers: object) -> dict[str, str]:
    if raw_headers is None:
        return {}
    if not isinstance(raw_headers, Mapping):
        raise ValueError("headers must be an object")
    headers: dict[str, str] = {}
    for raw_name, raw_value in cast(Mapping[object, object], raw_headers).items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise ValueError("request header names and values must be strings")
        name = raw_name.strip()
        lowered = name.casefold()
        if not name or not _HEADER_NAME.fullmatch(name):
            raise ValueError(f"invalid request header name {raw_name!r}")
        if lowered in _BLOCKED_REQUEST_HEADERS or lowered.startswith(_BLOCKED_REQUEST_HEADER_PREFIXES):
            raise ValueError(f"request header {name!r} cannot be supplied by the candidate")
        if "\r" in raw_value or "\n" in raw_value:
            raise ValueError(f"request header {name!r} contains forbidden control characters")
        headers[name] = raw_value
    return headers


def _validate_body_encoding(raw_encoding: object, default: str) -> str:
    if raw_encoding is None:
        return default
    if not isinstance(raw_encoding, str) or raw_encoding not in {"json", "form"}:
        raise ValueError("body_encoding must be 'json' or 'form'")
    return raw_encoding


def _prepare_body(*, body: object, body_encoding: str) -> tuple[bytes | None, dict[str, str], int]:
    if body is _MISSING:
        return None, {}, 0
    if body_encoding == "json":
        try:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        except (TypeError, ValueError) as exc:
            raise ValueError("body must be JSON serializable") from exc
        return encoded, {"Content-Type": "application/json; charset=utf-8"}, len(encoded)
    if not isinstance(body, Mapping):
        raise ValueError("form-encoded body must be an object")
    encoded = urlencode(_form_pairs(cast(Mapping[object, object], body))).encode()
    return encoded, {"Content-Type": "application/x-www-form-urlencoded"}, len(encoded)


def _form_pairs(body: Mapping[object, object], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe-style bracket flattening, sorted by key (gateway.py:891-911)."""
    pairs: list[tuple[str, str]] = []
    sortable: list[tuple[str, object]] = []
    for raw_key, value in body.items():
        if not isinstance(raw_key, str) or not raw_key:
            raise ValueError("form field names must be non-empty strings")
        sortable.append((raw_key, value))
    for key, value in sorted(sortable, key=lambda item: item[0]):
        name = f"{prefix}[{key}]" if prefix else key
        if isinstance(value, Mapping):
            pairs.extend(_form_pairs(cast(Mapping[object, object], value), prefix=name))
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            for item in cast(Sequence[object], value):
                if isinstance(item, Mapping | Sequence) and not isinstance(item, str):
                    raise ValueError("nested arrays and objects are not supported inside form arrays")
                pairs.append((f"{name}[]", _form_scalar(item)))
        else:
            pairs.append((name, _form_scalar(value)))
    return pairs


def _form_scalar(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return str(value)
    raise ValueError("form values must be strings, numbers, booleans, nulls, objects, or scalar arrays")


# --------------------------------------------------------------------------------------
# Fingerprints (mirror gateway.py:923-1008 so trace pairing works exactly as on the twins)
# --------------------------------------------------------------------------------------


def request_fingerprints(
    tool_input: Mapping[str, Any],
    *,
    provider: str,
    effective_path: str,
    default_body_encoding: str,
) -> tuple[str, str]:
    """`(request_fingerprint, action_fingerprint)`: SHA-256 over the normalized call.

    The request fingerprint covers provider, method, path with sorted query, GraphQL operation,
    canonical body and custom headers; the action fingerprint drops retry/idempotency headers.
    Only digests leave this function.
    """
    method = _validate_method((_string_value(tool_input.get("method")) or "").upper() or None)
    path, path_query = _validate_relative_path(effective_path)
    encoded_query = urlencode(_merge_query_pairs(path_query, None))
    normalized_path = f"{path}?{encoded_query}" if encoded_query else path
    headers = _validate_custom_headers(tool_input.get("headers"))
    normalized_headers = sorted((name.casefold(), value) for name, value in headers.items())
    action_headers = [(n, v) for n, v in normalized_headers if n not in _ACTION_FINGERPRINT_IGNORED_HEADERS]
    body = tool_input.get("body", _MISSING)
    body_encoding = _validate_body_encoding(tool_input.get("body_encoding"), default_body_encoding)
    _prepare_body(body=body, body_encoding=body_encoding)
    body_material: object
    if body is _MISSING:
        body_material = {"present": False}
    elif body_encoding == "form":
        body_material = {
            "present": True,
            "encoding": "form",
            "fields": _form_pairs(cast(Mapping[object, object], body)),
        }
    else:
        body_material = {
            "present": True,
            "encoding": "json",
            "value": json.loads(json.dumps(body, ensure_ascii=False, separators=(",", ":"))),
        }
    operation, operation_type = _infer_graphql_operation(body)
    common: dict[str, object] = {
        "provider": provider,
        "method": method,
        "path": normalized_path,
        "operation": operation,
        "operation_type": operation_type,
        "body": body_material,
    }
    return (
        _sha256_json({**common, "headers": normalized_headers}),
        _sha256_json({**common, "headers": action_headers}),
    )


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _infer_graphql_operation(body: object) -> tuple[str | None, str | None]:
    if not isinstance(body, Mapping):
        return None, None
    mapping = cast(Mapping[object, object], body)
    operation_name = mapping.get("operationName")
    query = mapping.get("query")
    if not isinstance(query, str):
        return _string_value(operation_name), None
    declaration = _GRAPHQL_DECLARATION.search(query)
    operation_type = declaration.group(1) if declaration else None
    root_field = _GRAPHQL_ROOT_FIELD.search(query)
    if root_field:
        return root_field.group(1), operation_type
    if isinstance(operation_name, str) and operation_name.strip():
        return operation_name.strip(), operation_type
    return (declaration.group(2) if declaration and declaration.group(2) else None), operation_type


# --------------------------------------------------------------------------------------
# Responses (mirror gateway.py:1101-1231)
# --------------------------------------------------------------------------------------


async def _read_bounded(response: httpx.Response, limit: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        remaining = limit - total
        if remaining <= 0:
            truncated = True
            break
        chunks.append(chunk[:remaining])
        total += min(len(chunk), remaining)
        if len(chunk) > remaining:
            truncated = True
            break
    return b"".join(chunks), truncated


def _decode_body(content: bytes, content_type: str | None, *, truncated: bool) -> object:
    if not content:
        return None
    normalized = (content_type or "").split(";", 1)[0].strip().casefold()
    if not truncated and (normalized == "application/json" or normalized.endswith("+json")):
        try:
            return cast(object, json.loads(content))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    if (
        normalized.startswith("text/")
        or normalized in {"", "application/graphql", "application/xml", "application/x-www-form-urlencoded"}
        or normalized.endswith("+xml")
    ):
        return content.decode("utf-8", errors="replace")
    return {"encoding": "base64", "data": base64.b64encode(content).decode("ascii")}


def _sanitize_string(value: str, *, policy: _ResponsePolicy) -> str:
    sanitized = value
    for secret in policy.secrets:
        sanitized = sanitized.replace(secret, _REDACTED_VALUE)
    contained_url = False
    for pattern in policy.url_patterns:
        if pattern.search(sanitized):
            contained_url = True
            sanitized = pattern.sub("", sanitized)
    for pattern in policy.host_patterns:
        sanitized = pattern.sub(_REDACTED_HOST, sanitized)
    if contained_url and not sanitized:
        return "/"
    return sanitized


def _sanitize_value(value: object, *, policy: _ResponsePolicy) -> object:
    if isinstance(value, str):
        return _sanitize_string(value, policy=policy)
    if isinstance(value, list):
        return [_sanitize_value(item, policy=policy) for item in cast(list[object], value)]
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for raw_key, item in cast(Mapping[object, object], value).items():
            key = _sanitize_string(raw_key, policy=policy) if isinstance(raw_key, str) else str(raw_key)
            while key in sanitized:
                key = f"{key}_"
            sanitized[key] = _sanitize_value(item, policy=policy)
        return sanitized
    return value


def _safe_response_headers(headers: httpx.Headers, *, policy: _ResponsePolicy) -> dict[str, str]:
    safe: dict[str, str] = {}
    for name, value in sorted(headers.multi_items()):
        lowered = name.casefold()
        if lowered in _SENSITIVE_RESPONSE_HEADERS or lowered.startswith(_BLOCKED_REQUEST_HEADER_PREFIXES):
            continue
        if len(safe) >= 50:
            break
        safe[lowered] = _sanitize_string(value[:4096], policy=policy)
    return safe


def _retry_delay(retry_after: str | None, attempt: int) -> float:
    """Seconds to wait before the next attempt: `Retry-After` (seconds or HTTP-date), capped."""
    delay: float | None = None
    if retry_after:
        header = retry_after.strip()
        try:
            delay = float(header)
        except ValueError:
            try:
                when = parsedate_to_datetime(header)
                delay = (when - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = None
    if delay is None:
        delay = 2.0 * attempt
    return min(max(delay, 0.0), _MAX_RETRY_DELAY_SECONDS)


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = cast(object, response.json())
    except ValueError:
        return {}
    return dict(cast(Mapping[str, Any], payload)) if isinstance(payload, Mapping) else {}


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _copy_json(value: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(json.dumps(value)))


__all__ = [
    "PROVIDER_ROLES",
    "REAL_BASE_URLS",
    "TOKEN_ENV_VARS",
    "AuthError",
    "AuthProvider",
    "GmailTokenProvider",
    "RealAppConfig",
    "RealAppGateway",
    "ScratchGuardError",
    "config_from_env",
    "gateway_from_env",
    "gmail_token_provider",
    "provider_headers",
    "request_fingerprints",
]
