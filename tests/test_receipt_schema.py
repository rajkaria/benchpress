"""receipt.json is a public contract: the demo run must validate against schemas/receipt.v1.json."""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from benchpress import schemas
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


def test_receipt_schema_returns_a_private_copy() -> None:
    first = receipt_schema()
    first["required"].clear()
    first["properties"]["protocol"]["const"] = "tampered"
    second = receipt_schema()
    assert "trial_id" in second["required"]
    assert second["properties"]["protocol"]["const"] == "benchpress-receipt/1"
    assert second is not first


async def test_validate_receipt_is_unaffected_by_a_mutated_copy(tmp_path: Path) -> None:
    receipt_schema()["properties"]["status"]["enum"] = ["http_200"]
    await run_demo(tmp_path)
    validate_receipt(json.loads((tmp_path / "receipt.json").read_text()))


def test_schema_id_resolves_to_the_repository_file_and_states_the_v1_rule() -> None:
    schema = receipt_schema()
    assert schema["$id"] == (
        "https://raw.githubusercontent.com/rajkaria/benchpress/main/src/benchpress/schemas/receipt.v1.json"
    )
    assert "additive" in schema["description"]
    assert "ignore unknown keys" in schema["description"]


def test_validate_receipt_names_the_extra_when_jsonschema_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def no_jsonschema(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "jsonschema" or name.startswith("jsonschema."):
            raise ModuleNotFoundError("No module named 'jsonschema'", name="jsonschema")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_jsonschema)
    with pytest.raises(ImportError) as excinfo:
        schemas.validate_receipt({"protocol": "benchpress-receipt/1"})
    assert 'pip install "benchpress-agent[schema]"' in str(excinfo.value)
