"""What a tool *is*, declared once and shared by every adapter.

Adapters fill a `ToolSpec` from framework metadata (JSON schema, decorators, MCP tool descriptions).
The gate and `VerifiedWrite` read it. A declared class always beats the name heuristic.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from benchpress.shims.guard_policy import ToolClass, classify_tool_name

_ID_SUFFIXES = ("_id", "id", "_ids", "ref", "key")


class ReadBackSpec(BaseModel):
    """How to observe the record a write touched: which tool, how its args derive from the write's args."""

    model_config = ConfigDict(frozen=True)

    tool: str
    # readback arg -> JSONPath into the write args/response
    args: dict[str, str] = Field(default_factory=dict[str, str])
    field_path: str | None = None


class ToolSpec(BaseModel):
    """Metadata about a tool: its name, class, the fields it targets, and how to observe its effects."""

    model_config = ConfigDict(frozen=True)

    name: str
    tool_class: ToolClass | None = None
    provider: str | None = None
    target_fields: tuple[str, ...] = ()
    readback: ReadBackSpec | None = None
    description: str = ""


def classify(spec: ToolSpec) -> ToolClass:
    """Return the tool's class: declared if set, else the name heuristic."""
    return spec.tool_class if spec.tool_class is not None else classify_tool_name(spec.name)


def spec_from_schema(name: str, schema: Mapping[str, Any], *, provider: str | None = None) -> ToolSpec:
    """Build a ToolSpec from a JSON schema, extracting target fields (those with id-like names)."""
    props = schema.get("properties")
    if isinstance(props, Mapping):
        props = cast(Mapping[str, Any], props)
        names = list(props.keys())
    else:
        names = []
    targets = tuple(n for n in names if n.lower().endswith(_ID_SUFFIXES))
    description = schema.get("description", "")
    return ToolSpec(name=name, provider=provider, target_fields=targets, description=str(description))
