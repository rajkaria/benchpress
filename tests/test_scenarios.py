"""Scenario registry: published seeds in, deep-copied and (optionally) transformed seeds out."""

from __future__ import annotations

import json

import pytest

from evals import harness_bridge, scenarios
from evals.scenarios import INJECTION_DESTINATION, INJECTION_SENDER, INJECTION_SUBJECT, SCENARIOS, add_injection_email


def test_billing_review_loads_the_published_task() -> None:
    loaded = scenarios.load("billing-review")
    assert loaded.scenario.task_id == "ECOM-02"
    assert loaded.task.twins == ("gmail", "hubspot", "slack", "stripe")
    assert len(loaded.task.facts) == 3
    assert len(loaded.task.protected_terms) == 2
    assert loaded.prompt == harness_bridge.user_prompt("ECOM-02")
    assert loaded.system_prompt == harness_bridge.system_prompt()
    assert loaded.seed_config == loaded.task.seed_config
    assert loaded.seed_config is not loaded.task.seed_config, "the seed is a copy the drivers may mutate"


def test_injection_transform_adds_exactly_one_external_destination_message() -> None:
    plain = scenarios.load("billing-review")
    injected = scenarios.load("billing-review-injection")
    assert injected.task.task_id == "ECOM-02"
    assert injected.prompt == plain.prompt, "the task prompt is untouched; only the seed changes"

    plain_messages = plain.seed_config["gmail"]["messages"]
    injected_messages = injected.seed_config["gmail"]["messages"]
    assert len(injected_messages) == len(plain_messages) + 1
    assert injected_messages[:-1] == plain_messages
    extra = injected_messages[-1]
    assert extra["from"] == INJECTION_SENDER
    assert extra["from"].endswith("@acme.example"), "the sender looks internal"
    assert extra["subject"] == INJECTION_SUBJECT
    assert INJECTION_DESTINATION in extra["body"]
    assert INJECTION_DESTINATION.split("@")[1] == "ledger-sync.example", "the destination is external"
    assert extra["to"] == plain_messages[0]["to"]
    assert extra["labels"] == ["INBOX"]
    assert extra["thread_id"].startswith("thread-")

    for provider in ("hubspot", "slack", "stripe"):
        assert injected.seed_config[provider] == plain.seed_config[provider]
    assert injected.seed_config["gmail"]["labels"] == plain.seed_config["gmail"]["labels"]


def test_injection_transform_never_mutates_its_input() -> None:
    original = {"gmail": {"messages": [{"to": ["someone@scratch.example"], "from": "a@b.example"}]}}
    frozen = json.dumps(original, sort_keys=True)
    transformed = add_injection_email(original)
    assert json.dumps(original, sort_keys=True) == frozen
    assert len(transformed["gmail"]["messages"]) == 2
    assert transformed["gmail"]["messages"][1]["to"] == ["someone@scratch.example"]
    bare = add_injection_email({})
    assert bare["gmail"]["messages"][0]["to"] == [scenarios.DEFAULT_SEED_RECIPIENT]


def test_seed_counts_match_the_scenario_json() -> None:
    loaded = scenarios.load("billing-review")
    counts = scenarios.seed_counts(loaded.seed_config)
    assert counts["gmail"] == {"drafts": 0, "labels": 1, "messages": 5}
    assert counts["hubspot"] == {"associations": 1, "companies": 4, "contacts": 4, "deals": 3, "lists": 0, "tickets": 0}
    assert counts["slack"] == {"channels": 2, "channels.messages": 5, "users": 4}
    assert counts["stripe"] == {"customers": 4, "meters": 0, "products": 3, "products.prices": 3}
    injected = scenarios.seed_counts(scenarios.load("billing-review-injection").seed_config)
    assert injected["gmail"]["messages"] == 6


def test_every_scenario_resolves_to_a_suite_task() -> None:
    assert set(SCENARIOS) == {
        "billing-review",
        "billing-review-injection",
        "ci-quarantine",
        "renewal-rescue",
        "followup-cohort",
    }
    for scenario_id, scenario in SCENARIOS.items():
        assert scenario.id == scenario_id
        loaded = scenarios.load(scenario_id)
        assert loaded.task.task_id == scenario.task_id
        assert loaded.task.task_id in harness_bridge.all_task_ids()
        assert loaded.seed_config
    assert scenarios.load("ci-quarantine").task.twins == ("github", "linear", "slack")
    assert scenarios.load("renewal-rescue").task.task_id == "CRM-02"
    assert scenarios.load("followup-cohort").task.task_id == "CRM-05"
    with pytest.raises(ValueError, match="unknown scenario"):
        scenarios.load("nope")
