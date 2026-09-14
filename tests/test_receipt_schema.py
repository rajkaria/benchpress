"""receipt.json is a public contract: the demo run must validate against schemas/receipt.v1.json."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from benchpress.demo import run_demo
from benchpress.schemas import receipt_schema, validate_receipt


async def test_demo_receipt_validates(tmp_path: Path) -> None:
    await run_demo(tmp_path)
    payload = json.loads((tmp_path / "receipt.json").read_text())
    validate_receipt(payload)  # raises on failure


def test_schema_is_draft_2020_and_pins_the_protocol() -> None:
    schema = receipt_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["protocol"]["const"] == "benchpress-receipt/1"
    for key in ("trial_id", "status", "request", "plan", "ledger", "refusals", "gate_decisions", "evidence"):
        assert key in schema["required"]


async def test_schema_rejects_a_status_from_a_2xx(tmp_path: Path) -> None:
    schema = receipt_schema()
    await run_demo(tmp_path)
    payload = json.loads((tmp_path / "receipt.json").read_text())
    payload["status"] = "http_200"
    with pytest.raises(jsonschema.ValidationError) as excinfo:
        jsonschema.validate(payload, schema)
    assert excinfo.value.validator == "enum"
    assert list(excinfo.value.path) == ["status"]
