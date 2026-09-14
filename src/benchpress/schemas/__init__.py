"""Public JSON Schemas shipped with the package (receipt v1)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from importlib.resources import files
from typing import Any


@cache
def receipt_schema() -> dict[str, Any]:
    text = files("benchpress.schemas").joinpath("receipt.v1.json").read_text(encoding="utf-8")
    return json.loads(text)


def validate_receipt(payload: Mapping[str, Any]) -> None:
    """Raise `jsonschema.ValidationError` if `payload` is not a v1 receipt."""
    import jsonschema

    jsonschema.validate(dict(payload), receipt_schema())
