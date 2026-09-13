from __future__ import annotations

import re

import pytest

from benchpress.prompts import BENCHPRESS_ADDENDUM, PHASE_PROMPTS, render, system_text


def test_every_template_renders_without_leftover_slots() -> None:
    slot_names = {name: re.findall(r"<<(\w+)>>", template) for name, template in PHASE_PROMPTS.items()}
    for name, slots in slot_names.items():
        rendered = render(name, **{slot: {"k": "v"} for slot in slots})
        assert "<<" not in rendered
        assert '"k": "v"' in rendered or not slots


def test_missing_slot_is_an_error() -> None:
    with pytest.raises(KeyError):
        render("orient", providers=["slack"])


def test_system_text_keeps_harness_prompt_first() -> None:
    text = system_text("HARNESS PROMPT  \n")
    assert text.startswith("HARNESS PROMPT") and text.endswith(BENCHPRESS_ADDENDUM)


def test_prompts_are_task_agnostic() -> None:
    corpus = "\n".join(PHASE_PROMPTS.values()) + BENCHPRESS_ADDENDUM
    assert not re.search(r"(ECOM|CRM|DEV|IT|MKT)-0\d|northwind|acme\.example", corpus, re.IGNORECASE)
