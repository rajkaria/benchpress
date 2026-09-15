"""Shared plumbing for the phases."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel

from benchpress.context import Context
from benchpress.model import ModelAPIError, ModelClient, ModelRefusal, SchemaFailure
from benchpress.playbooks import Playbook, for_provider
from benchpress.tools import ToolBus

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")


@dataclass(frozen=True)
class Ablations:
    """Switch a guarantee off to measure what it was worth. Never on by default."""

    no_policy_sweep: bool = False
    no_gate: bool = False
    no_readback: bool = False

    @classmethod
    def parse(cls, value: str | None) -> Ablations:
        names = {part.strip() for part in (value or "").split(",") if part.strip()}
        unknown = names - {"no_policy_sweep", "no_gate", "no_readback"}
        if unknown:
            raise ValueError(f"unknown ablations: {sorted(unknown)}")
        return cls(
            no_policy_sweep="no_policy_sweep" in names,
            no_gate="no_gate" in names,
            no_readback="no_readback" in names,
        )

    def active(self) -> tuple[str, ...]:
        return tuple(name for name, on in self.__dict__.items() if on)


@dataclass
class PhaseDeps:
    ctx: Context
    model: ModelClient
    bus: ToolBus
    ablations: Ablations = field(default_factory=Ablations)
    playbooks: dict[str, Playbook] = field(default_factory=dict[str, Playbook])
    completed: list[str] = field(default_factory=list[str])

    def playbook(self, provider: str) -> Playbook | None:
        return self.playbooks.get(provider)

    def note(self, text: str) -> None:
        self.ctx.notes.append(text)


def resolve_playbooks(providers: Sequence[str]) -> dict[str, Playbook]:
    found: dict[str, Playbook] = {}
    for provider in providers:
        playbook = for_provider(provider)
        if playbook is not None:
            found[provider] = playbook
    return found


T = TypeVar("T", bound=BaseModel)


async def safe_emit(deps: PhaseDeps, phase: str, schema: type[T], content: str, default: T) -> T:
    """A model failure is data: note it, fall back to the conservative default."""
    try:
        return await deps.model.emit(phase=phase, schema=schema, content=content)
    except SchemaFailure as exc:
        deps.note(f"{phase}: schema failure, using conservative default ({str(exc)[:160]})")
    except ModelRefusal as exc:
        deps.note(f"{phase}: model refused, using conservative default ({exc})")
    except ModelAPIError as exc:
        deps.note(f"{phase}: model API error, using conservative default ({exc})")
    return default


def dump(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [dump(item) for item in value]  # type: ignore[union-attr]
    if isinstance(value, dict):
        return {str(key): dump(item) for key, item in value.items()}  # type: ignore[union-attr]
    return value


def to_json(value: object) -> str:
    return json.dumps(dump(value), ensure_ascii=False, indent=1, default=str)


def sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text.replace("\n", " ")) if part.strip()]


def sentence_containing(text: str, needle_pattern: re.Pattern[str]) -> str:
    for sentence in sentences(text):
        if needle_pattern.search(sentence):
            return sentence
    return text[:300]
