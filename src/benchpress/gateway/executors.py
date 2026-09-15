"""The gateway's tool executor: `RealAppGateway` over the configured upstreams, or a 503 stand-in without any."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from benchpress.gateway.config import ConfigError, Settings
from benchpress.realapp import RealAppConfig, RealAppGateway, ScratchGuardError
from benchpress.tools import UNBOUNDED, ToolExecutor

__all__ = ["executor_from_settings", "unconfigured_executor"]


async def unconfigured_executor(tool_name: str, tool_input: dict[str, Any]) -> object:
    """Every provider call on a gateway without upstreams: a 503 result, never an exception."""
    provider = str(tool_input.get("provider") or "")
    return {"ok": False, "status_code": 503, "body": None, "error": f"no upstream configured for provider {provider!r}"}


def executor_from_settings(settings: Settings) -> tuple[ToolExecutor, Callable[[], Awaitable[None]]]:
    """The executor for `settings.upstreams`, and the coroutine function that closes it.

    `RealAppGateway` was built for one benchmark trial. The gateway lives for days, so both lifetime call caps are
    lifted and no trace is retained (the store's receipts are the record). An upstream `RealAppGateway` refuses
    (an unknown provider, a non-loopback URL without `BENCHPRESS_SCRATCH_OK=1`) is a `ConfigError`.
    """
    if not settings.upstreams:

        async def nothing() -> None:
            return None

        return unconfigured_executor, nothing
    try:
        gateway = RealAppGateway(
            [
                RealAppConfig.for_provider(u.provider, base_url=u.base_url, token=u.token, role=u.role)
                for u in settings.upstreams
            ],
            max_calls=UNBOUNDED,
            max_docs_calls=UNBOUNDED,
            trace_limit=0,
        )
    except (ValueError, ScratchGuardError) as exc:
        raise ConfigError(f"upstreams: {exc}") from exc
    return gateway.execute_tool, gateway.aclose
