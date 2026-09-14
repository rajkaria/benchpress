"""ToolSpec: declared class beats the name heuristic; JSON-schema tools get a spec with target fields."""

from __future__ import annotations

from benchpress.toolspec import (
    ReadBackSpec,
    ToolSpec,
    _segment_name,  # pyright: ignore[reportPrivateUsage]
    classify,
    spec_from_schema,
)


def test_declared_class_wins_over_the_name() -> None:
    spec = ToolSpec(name="delete_draft", tool_class="write")
    assert classify(spec) == "write"


def test_name_heuristic_is_the_fallback() -> None:
    assert classify(ToolSpec(name="list_contacts")) == "read"
    assert classify(ToolSpec(name="update_contact")) == "write"
    assert classify(ToolSpec(name="delete_contact")) == "destructive"


def test_spec_from_json_schema_picks_id_like_target_fields() -> None:
    schema = {
        "type": "object",
        "properties": {
            "contact_id": {"type": "string"},
            "email": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["contact_id"],
    }
    spec = spec_from_schema("update_contact", schema, provider="hubspot")
    assert spec.provider == "hubspot"
    assert spec.target_fields == ("contact_id",)
    assert spec.readback is None


def test_readback_spec_maps_arguments() -> None:
    rb = ReadBackSpec(tool="get_contact", args={"contact_id": "$.contact_id"}, field_path="properties.email")
    spec = ToolSpec(name="update_contact", readback=rb)
    assert spec.readback is not None and spec.readback.args["contact_id"] == "$.contact_id"


def test_spec_from_schema_ignores_words_that_merely_end_in_id_or_key() -> None:
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "contactId": {"type": "string"},
            "valid": {"type": "boolean"},
            "paid": {"type": "boolean"},
            "monkey": {"type": "string"},
            "external_ref": {"type": "string"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["id"],
    }
    spec = spec_from_schema("update_contact", schema)
    assert spec.target_fields == ("id", "contactId", "external_ref", "idempotency_key")


def test_all_caps_runs_split_before_the_next_word() -> None:
    assert _segment_name("URLKey") == ["url", "key"]
    assert _segment_name("recordHTTPId") == ["record", "http", "id"]
    assert _segment_name("contactID") == ["contact", "id"]
    assert _segment_name("customer_id") == ["customer", "id"]


def test_an_all_caps_prefixed_key_is_a_target_field() -> None:
    spec = spec_from_schema("update_link", {"properties": {"URLKey": {}, "URLValue": {}}})
    assert spec.target_fields == ("URLKey",)
