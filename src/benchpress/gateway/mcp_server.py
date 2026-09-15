"""`build_mcp_server`: the gateway's MCP tools, `verified_write`, `read` and `explain_refusal`.

Each tool authenticates the caller from the `Authorization` header of the HTTP request that carried the call (None
in process), then calls the same `GatewayService` method as the HTTP surface. An MCP write is therefore gated,
executed, read back, receipted and counted exactly like `POST /v1/execute`. `create_app` mounts the server's
streamable-HTTP app at `/mcp/`.

A refused write is not a tool error: `verified_write` returns its outcome with `status: "refused"`, and
`explain_refusal` returns the verdict. Tool errors are for calls that cannot be judged: a refused key, invalid input,
or a `RequestRejected` such as an unknown session or a control-plane read. The error text is the message, rendered
without the rejected input, because an action's headers may carry a credential.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator, Mapping
from contextlib import contextmanager
from types import MappingProxyType
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from benchpress import __version__
from benchpress.context import Action
from benchpress.gateway.schemas import ContextInput, ExecuteRequest
from benchpress.gateway.service import GatewayService, RequestRejected
from benchpress.gateway.store import WorkspaceRow
from benchpress.packs import PACK_RULE_PREFIX

__all__ = ["INSTRUCTIONS", "RULE_HELP", "Authenticator", "authorization_header", "build_mcp_server", "rule_help"]

Authenticator = Callable[[str | None], Awaitable[WorkspaceRow]]
"""Takes the raw `Authorization` header value (None without one) and returns the caller's workspace.

Raises `PermissionError` when the caller is refused. The gateway's is `benchpress.gateway.deps.header_authenticator`.
"""

RULE_HELP: Mapping[str, str] = MappingProxyType(
    {
        "control_plane": "The path reaches the provider's control plane (an API root, an admin, reset or schema "
        "route, an absolute URL, or GraphQL introspection), which no agent write may touch.",
        "method": "DELETE is never permitted, so archive or update the record instead, or leave the deletion to a "
        "person.",
        "action_class": "The write belongs to an action class, such as sending email or moving money, that the "
        "session's definition of done forbids.",
        "protected": "The write targets a record, name, domain or email that the caller marked protected for this "
        "session.",
        "provider_scope": "The write goes to a provider outside the session's authorized write scope.",
        "plan_membership": "The action is not on the session's approved plan, does not match its planned provider, "
        "method and path, or satisfies no definition-of-done item.",
        "field_smuggling": "The body writes fields that the action does not declare in `fields`, and every field a "
        "write changes must be declared.",
        "external_destination": "The body addresses a recipient or destination whose domain the session does not "
        "allow.",
        "idempotency": "An identical write already succeeded or is still in flight, so running it again would "
        "duplicate it.",
        "policy_pack": "A policy pack enforced on this workspace refuses the write, and the reason names the pack "
        "rule and what it would take, such as an approval.",
        "allowed": "The gate allows this write, so verified_write would run it and read it back, unless an approval "
        "rule parks it for a person first.",
    }
)
"""One plain sentence per verdict rule for `explain_refusal`'s `help`. Any `pack:<pack>.<rule>` id uses
`policy_pack`."""

INSTRUCTIONS = (
    "Benchpress gateway tools for agents that write to external systems. verified_write gates one write against the "
    "session's context, executes it only if the gate allows it, and reads the result back; its status is verified, "
    "mismatch, unverified, failed, refused or needs_approval. read runs one provider GET. explain_refusal asks the "
    "gate about a write and never executes anything. A refusal is data, not an error: read its rule, reason and "
    "help, then change the write or ask a person. Send exactly one of session_id or context with each write."
)


def rule_help(rule: str) -> str:
    """The help sentence for a verdict rule: `policy_pack` for any pack rule, else the rule's own entry, else ''."""
    if rule.startswith(PACK_RULE_PREFIX):
        return RULE_HELP["policy_pack"]
    return RULE_HELP.get(rule, "")


def authorization_header(ctx: Context[Any, Any]) -> str | None:
    """The `Authorization` header of the HTTP request that carried this call, or None (in process, or no header)."""
    headers = ctx.headers
    if headers is None:
        return None
    return next((value for name, value in headers.items() if name.lower() == "authorization"), None)


def _validation_message(exc: ValidationError) -> str:
    """pydantic's errors without the rejected input, which may carry a credential from an action's headers."""
    problems: list[str] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"])
        problems.append(f"{location}: {error['msg']}" if location else error["msg"])
    return f"invalid {exc.title}: {'; '.join(problems)}"


@contextmanager
def _tool_errors() -> Generator[None]:
    """Turn invalid input or a rejected request into a tool error that carries its message."""
    try:
        yield
    except ValidationError as exc:
        raise ToolError(_validation_message(exc)) from None
    except RequestRejected as exc:
        raise ToolError(str(exc)) from None


def build_mcp_server(service: GatewayService, authenticate: Authenticator) -> MCPServer:
    """The gateway's MCP server over `service`, authenticating every tool call with `authenticate`."""
    server = MCPServer(name="benchpress-gateway", instructions=INSTRUCTIONS, version=__version__)

    async def caller(ctx: Context[Any, Any]) -> WorkspaceRow:
        # Only the authenticator's refusal is reported as a PermissionError tool error; any other PermissionError
        # (a file the store cannot open, say) stays an unexpected failure whose text never reaches the client.
        try:
            return await authenticate(authorization_header(ctx))
        except PermissionError as exc:
            raise ToolError(str(exc)) from None

    # Not additive-only: a verified write can overwrite a field, send an email or void an invoice, so it is marked
    # destructive and a client that auto-approves non-destructive tools never waves it through.
    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True))
    async def verified_write(
        action: dict[str, Any],
        ctx: Context[Any, Any],
        session_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Gate, execute and read back one write (a non-GET action).

        Run it in an existing session (session_id) or in a one-shot session built from an inline context; send
        exactly one. Returns the outcome: status (verified, mismatch, unverified, failed, refused, needs_approval),
        the gate's verdict, the read-back evidence and the receipt id. A refusal is a result, not an error.
        """
        with _tool_errors():
            workspace = await caller(ctx)
            request = ExecuteRequest.model_validate({"action": action, "session_id": session_id, "context": context})
            response = await service.execute(workspace, request)
        return response.model_dump(mode="json")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def read(
        provider: str, path: str, ctx: Context[Any, Any], query: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Run one GET against a provider through the gateway's executor.

        Reads are not gated, but a control-plane path is rejected. Returns ok, status_code, body and error.
        """
        with _tool_errors():
            workspace = await caller(ctx)
            return await service.read(workspace, provider, path, query or {})

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def explain_refusal(
        action: dict[str, Any],
        ctx: Context[Any, Any],
        session_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ask the gate whether a write would be allowed, and why, without executing anything.

        Takes the same action and session_id or context as verified_write. Returns allowed, rule, reason, and help:
        a plain sentence on what the rule protects.
        """
        with _tool_errors():
            workspace = await caller(ctx)
            parsed = Action.model_validate(action)
            inline = ContextInput.model_validate(context) if context is not None else None
            verdict = await service.explain(workspace, parsed, session_id=session_id, context=inline)
        return {**verdict, "help": rule_help(str(verdict["rule"]))}

    return server
