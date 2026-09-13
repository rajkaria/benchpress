"""P0 — Orient: parse the request, map providers, read the originating channel."""

from __future__ import annotations

import re

from benchpress.context import TaskFrame
from benchpress.normalize import domains_in, emails_in
from benchpress.phases.common import PhaseDeps, safe_emit
from benchpress.prompts import render
from benchpress.tools import BudgetExhausted

_CHANNEL = re.compile(r"#([A-Za-z0-9][\w.-]*)")
_PROHIBITION = re.compile(r"\b(?:do not|don't|never|must not)\b\s+([^.;]+)", re.IGNORECASE)
_SPLIT_LIST = re.compile(r",\s*|\s+or\s+|\s+and\s+")


async def orient(deps: PhaseDeps) -> None:
    ctx = deps.ctx
    deps.bus.enter("P0")
    frame = await safe_emit(
        deps,
        "orient",
        TaskFrame,
        render("orient", providers=list(ctx.providers), prompt=ctx.user_prompt),
        TaskFrame(),
    )
    channel = channel_from_prompt(ctx.user_prompt) or clean_channel(frame.originating_channel)
    prohibitions = tuple(dict.fromkeys([*frame.explicit_prohibitions, *prohibitions_in(ctx.user_prompt)]))
    identifiers = set(frame.observed_identifiers) | set(emails_in(ctx.user_prompt)) | set(domains_in(ctx.user_prompt))
    entities = tuple(dict.fromkeys(entity.strip() for entity in frame.subject_entities if looks_like_name(entity)))
    ctx.frame = frame.model_copy(
        update={
            "originating_channel": channel,
            "explicit_prohibitions": prohibitions,
            "observed_identifiers": tuple(sorted(value for value in identifiers if value)),
            "subject_entities": entities,
        }
    )
    slack = deps.playbook("slack")
    if slack is None or not channel:
        return
    try:
        channel_id = await slack.resolve_channel(deps.bus, channel)
        if not channel_id:
            deps.note(f"P0: originating channel {channel!r} not found")
            return
        ctx.originating_channel_id = channel_id
        history = await slack.channel_history(deps.bus, channel_id, 50)
    except BudgetExhausted:
        deps.note("P0: budget exhausted while reading the originating channel")
        return
    found: set[str] = set()
    latest = ""
    for message in history:
        text = str(message.get("text", ""))
        found |= set(emails_in(text))
        ts = str(message.get("ts", ""))
        latest = max(latest, ts)
    ctx.channel_baseline_ts = latest or None
    if found:
        merged = set(ctx.frame.observed_identifiers) | found
        ctx.frame = ctx.frame.model_copy(update={"observed_identifiers": tuple(sorted(merged))})


def channel_from_prompt(prompt: str) -> str | None:
    match = _CHANNEL.search(prompt)
    return match.group(1) if match else None


def clean_channel(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lstrip("#").split()[0] if value.strip() else ""
    return cleaned.rstrip(".,;:") or None


_NOT_A_PROHIBITION = re.compile(r"^(been|was|were|had|has|is|are)\b", re.IGNORECASE)


def prohibitions_in(prompt: str) -> list[str]:
    found: list[str] = []
    for match in _PROHIBITION.finditer(prompt):
        clause = match.group(1).strip()
        if _NOT_A_PROHIBITION.match(clause):
            continue  # "has never been a customer" describes a fact, not a rule
        clause = re.split(r"\bunless\b", clause, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        for part in _SPLIT_LIST.split(clause):
            part = re.sub(r"^(or|and)\s+", "", part.strip(" ,."), flags=re.IGNORECASE)
            if len(part) > 3:
                found.append(part)
    return found


def looks_like_name(value: str) -> bool:
    text = value.strip()
    if not text or "@" in text or len(text.split()) > 6:
        return False
    if text[0].islower():
        return False
    return any(character.isalpha() for character in text)
