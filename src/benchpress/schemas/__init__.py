"""Public JSON Schemas shipped with the package (receipt v1, write v1).

`receipt_schema()` and `write_schema()` need only the standard library. `validate_receipt()` and
`validate_write_line()` need `jsonschema`, which ships as the optional `[schema]` extra:
`pip install "benchpress-agent[schema]"`.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from functools import cache
from importlib.resources import files
from typing import Any

SCHEMA_EXTRA_HINT = 'validate_receipt needs jsonschema: install the extra with `pip install "benchpress-agent[schema]"`'


@cache
def _receipt_schema() -> dict[str, Any]:
    text = files("benchpress.schemas").joinpath("receipt.v1.json").read_text(encoding="utf-8")
    return json.loads(text)


def receipt_schema() -> dict[str, Any]:
    """The receipt v1 JSON Schema, as a fresh copy the caller may mutate."""
    return copy.deepcopy(_receipt_schema())


def validate_receipt(payload: Mapping[str, Any]) -> None:
    """Raise `jsonschema.ValidationError` if `payload` is not a v1 receipt.

    Raises `ImportError` naming the `[schema]` extra when `jsonschema` is not installed.
    """
    try:
        import jsonschema
    except ImportError as exc:
        raise ImportError(SCHEMA_EXTRA_HINT) from exc

    jsonschema.validate(dict(payload), _receipt_schema())


@cache
def _write_schema() -> dict[str, Any]:
    text = files("benchpress.schemas").joinpath("write.v1.json").read_text(encoding="utf-8")
    return json.loads(text)


def write_schema() -> dict[str, Any]:
    """The write v1 JSON Schema, as a fresh copy the caller may mutate."""
    return copy.deepcopy(_write_schema())


def validate_write_line(line: str | Mapping[str, Any]) -> None:
    """Raise `jsonschema.ValidationError` if `line` is not a v1 `benchpress-write/1` line.

    `line` is either the JSON text produced by `benchpress.write_receipts.receipt_line` / `write_line`, or an
    already-parsed mapping. Raises `ImportError` naming the `[schema]` extra when `jsonschema` is not installed.
    """
    try:
        import jsonschema
    except ImportError as exc:
        raise ImportError(SCHEMA_EXTRA_HINT) from exc

    payload = json.loads(line) if isinstance(line, str) else dict(line)
    jsonschema.validate(payload, _write_schema())
