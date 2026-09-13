"""A generic stub twin for providers that have no real twin module yet.

The data plane answers every request with a provider-neutral 404 JSON body, so a candidate's
calls are traced by the real `ProviderGateway` and paired by the graders, but nothing ever
succeeds. The admin plane serves a minimal JSON object so the harness's `TrustedStateCapturer`
can take baseline and final snapshots. `serve_twins` falls back to `make_stub_spec` for any
provider whose `devsim.twins.<provider>` module does not exist.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from devsim.twins import PROVIDER_ROLES
from devsim.twins.base import Store, TwinSpec, json_response

STUB_NOT_FOUND: dict[str, str] = {"error": "devsim stub: no route"}
_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


class StubStore(Store):
    """Holds nothing but the seed summary. Reads never change it."""

    provider = "stub"
    role = "stub"

    def __init__(self, seed_key: str) -> None:
        super().__init__(seed_key)
        self.seed_summary: dict[str, int] = {}

    def seed(self, seed_config: Mapping[str, Any]) -> None:
        """Record collection sizes from this provider's seed slice; nothing is materialised."""

        summary: dict[str, int] = {}
        for name, value in seed_config.items():
            if isinstance(value, list):
                summary[str(name)] = len(cast(list[object], value))
            elif isinstance(value, dict):
                summary[str(name)] = len(cast(dict[object, object], value))
        self.seed_summary = summary

    def admin_state(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "role": self.role,
            "stub": True,
            "records": {},
        }


def _stub_store_class(provider: str, role: str) -> type[StubStore]:
    namespace: dict[str, object] = {"provider": provider, "role": role}
    return cast(type[StubStore], type(f"Stub_{provider}_Store", (StubStore,), namespace))


def make_stub_data_app(store: Store) -> Starlette:
    """Every data-plane route → 404 `{"error": "devsim stub: no route"}`."""

    async def not_found(_: Request) -> Response:
        return json_response(STUB_NOT_FOUND, status=404, headers={"X-Devsim-Twin": f"stub:{store.provider}"})

    routes = [
        Route("/", not_found, methods=_ALL_METHODS),
        Route("/{path:path}", not_found, methods=_ALL_METHODS),
    ]
    return Starlette(routes=routes)


def make_stub_spec(provider: str, role: str | None = None) -> TwinSpec:
    """A `TwinSpec` for an arbitrary provider name (role defaults to the harness role map)."""

    resolved_role = role or PROVIDER_ROLES.get(provider, provider)
    store_class = _stub_store_class(provider, resolved_role)

    def make_store(seed_key: str) -> Store:
        return store_class(seed_key)

    return TwinSpec(
        provider=provider,
        role=resolved_role,
        make_store=make_store,
        make_data_app=make_stub_data_app,
        notes=("stub twin: data plane is 404-only; admin state is a minimal object",),
    )


SPEC: TwinSpec = make_stub_spec("stub", "stub")

__all__ = ["SPEC", "STUB_NOT_FOUND", "StubStore", "make_stub_data_app", "make_stub_spec"]
