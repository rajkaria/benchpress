"""Twin registry. Each provider module exposes a module-level `SPEC: TwinSpec`."""

from __future__ import annotations

import importlib

from devsim.twins.base import Clock, Store, TwinSpec, make_admin_app

# Provider name -> harness role (docs/RESEARCH.md "Provider roles").
PROVIDER_ROLES: dict[str, str] = {
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


def load_spec(provider: str) -> TwinSpec:
    """Import `devsim.twins.<provider>` and return its SPEC. Raises ImportError if absent."""
    module = importlib.import_module(f"devsim.twins.{provider}")
    spec = getattr(module, "SPEC", None)
    if not isinstance(spec, TwinSpec):
        raise ImportError(f"devsim.twins.{provider} does not define SPEC: TwinSpec")
    return spec


def available() -> list[str]:
    found: list[str] = []
    for provider in sorted(PROVIDER_ROLES):
        try:
            load_spec(provider)
        except ImportError:
            continue
        found.append(provider)
    return found


__all__ = ["PROVIDER_ROLES", "Clock", "Store", "TwinSpec", "available", "load_spec", "make_admin_app"]
