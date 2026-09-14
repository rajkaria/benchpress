"""Policy packs: enforceable rules per business function, applied by the mutation gate in code.

A pack is YAML data (`benchpress/policy_packs/<name>.yaml`) declaring rules such as "outbound
email is draft-only" or "no refunds without an explicit approval fact". Each rule has a stable
id (`<pack>.<rule>`), a human reason, a request matcher, and optionally the approval facts that
lift it. Packs only ever *add* refusals: `Gate(ctx, policy_packs=...)` consults them right after
the control-plane rule, and a gate built without packs behaves exactly as before.

A pack refusal carries the rule id twice: as the verdict rule (`pack:<id>`, which the receipt
and the tool bus record verbatim) and inside the reason text.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from benchpress.context import Action, Context, ForbiddenClass
from benchpress.gate import body_text, classify

POLICY_PACKS_DIR = Path(__file__).resolve().parent / "policy_packs"
PACK_RULE_PREFIX = "pack:"
APPROVED_VALUES: frozenset[str] = frozenset({"approved", "granted", "true", "yes"})
_FORBIDDEN_CLASSES: frozenset[str] = frozenset(get_args(ForbiddenClass))

WriteMethod = Literal["POST", "PUT", "PATCH", "DELETE"]


class PolicyPackError(ValueError):
    """A pack that cannot be found, read, parsed, or validated."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


@cache
def _regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


class RuleMatch(_Model):
    """Which writes a rule applies to. Every given condition must hold; list members are alternatives.

    Rules never apply to reads (GET).
    """

    providers: tuple[str, ...] = ()
    methods: tuple[WriteMethod, ...] = ()
    path: str | None = None
    path_not: str | None = None
    body: str | None = None
    action_classes: tuple[str, ...] = ()

    @field_validator("path", "path_not", "body")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                _regex(value)
            except re.error as exc:
                raise ValueError(f"invalid regex {value!r}: {exc}") from exc
        return value

    @field_validator("action_classes")
    @classmethod
    def _known_classes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - _FORBIDDEN_CLASSES)
        if unknown:
            raise ValueError(f"unknown action class(es) {unknown}; known: {sorted(_FORBIDDEN_CLASSES)}")
        return value

    @model_validator(mode="after")
    def _not_empty(self) -> RuleMatch:
        if not (self.providers or self.methods or self.path or self.body or self.action_classes):
            raise ValueError("a rule must match on at least one of providers, methods, path, body, action_classes")
        return self

    def applies(self, action: Action) -> bool:
        if not action.is_write:
            return False
        if self.providers and action.provider.casefold() not in {name.casefold() for name in self.providers}:
            return False
        if self.methods and action.method not in self.methods:
            return False
        if self.path is not None and not _regex(self.path).search(action.path):
            return False
        if self.path_not is not None and _regex(self.path_not).search(action.path):
            return False
        if self.body is not None and not _regex(self.body).search(body_text(action.body)):
            return False
        if self.action_classes:
            labels = classify(action.provider, action.method, action.path, action.body)
            if not labels & set(self.action_classes):
                return False
        return True

    def summary(self) -> str:
        parts: list[str] = []
        if self.providers:
            parts.append(f"providers={','.join(self.providers)}")
        if self.methods:
            parts.append(f"methods={','.join(self.methods)}")
        if self.action_classes:
            parts.append(f"classes={','.join(self.action_classes)}")
        if self.path is not None:
            parts.append(f"path~{self.path}")
        if self.path_not is not None:
            parts.append(f"path!~{self.path_not}")
        if self.body is not None:
            parts.append(f"body~{self.body}")
        return " ".join(parts)


class PackRule(_Model):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*\.[a-z0-9]+(?:-[a-z0-9]+)*$")
    reason: str = Field(min_length=1)
    match: RuleMatch
    unless_approved: tuple[str, ...] = ()

    def approval(self, context: Context) -> str | None:
        """The approval fact that lifts this rule, if the definition of done carries one."""
        facts = {key.casefold(): value for key, value in context.dod.facts.items()}
        for key in self.unless_approved:
            if facts.get(key.casefold(), "").strip().casefold() in APPROVED_VALUES:
                return key
        return None

    def refusal(self, action: Action, context: Context) -> tuple[str, str] | None:
        if not self.match.applies(action):
            return None
        reason = f"{self.reason} [policy pack rule {self.id}]"
        if self.unless_approved:
            if self.approval(context) is not None:
                return None
            reason += f"; no approval fact ({', '.join(self.unless_approved)}) is present"
        return (f"{PACK_RULE_PREFIX}{self.id}", reason)


class PolicyPack(_Model):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str = Field(min_length=1)
    description: str = ""
    rules: tuple[PackRule, ...] = Field(min_length=1)
    source: str = ""

    @model_validator(mode="after")
    def _rule_ids(self) -> PolicyPack:
        seen: set[str] = set()
        for rule in self.rules:
            if not rule.id.startswith(f"{self.name}."):
                raise ValueError(f"rule id {rule.id!r} must start with the pack name {self.name!r}")
            if rule.id in seen:
                raise ValueError(f"duplicate rule id {rule.id!r}")
            seen.add(rule.id)
        return self

    def refusal(self, action: Action, context: Context) -> tuple[str, str] | None:
        """The first rule of this pack that refuses `action`, as `(rule, reason)`."""
        for rule in self.rules:
            refusal = rule.refusal(action, context)
            if refusal is not None:
                return refusal
        return None


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------


def available_policy_packs() -> tuple[str, ...]:
    return tuple(sorted(path.stem for path in POLICY_PACKS_DIR.glob("*.yaml")))


def _pack_path(name_or_path: str | Path) -> Path:
    path = Path(name_or_path)
    if path.suffix in {".yaml", ".yml"} or path.is_file():
        if not path.is_file():
            raise PolicyPackError(f"no such policy pack file: {path}")
        return path
    bundled = POLICY_PACKS_DIR / f"{name_or_path}.yaml"
    if not bundled.is_file():
        raise PolicyPackError(
            f"unknown policy pack {str(name_or_path)!r}; bundled packs: {', '.join(available_policy_packs())}"
        )
    return bundled


def policy_pack_from_yaml(text: str, *, source: str) -> PolicyPack:
    """A pack from YAML text. `source` says where the text came from, in every error and on the pack."""
    try:
        raw: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyPackError(f"{source}: cannot read YAML: {exc}") from exc
    try:
        pack = PolicyPack.model_validate(raw)
    except ValidationError as exc:
        raise PolicyPackError(f"{source}: {exc}") from exc
    return pack.model_copy(update={"source": source})


def load_policy_pack(name_or_path: str | Path) -> PolicyPack:
    """A bundled pack by name (`billing`) or a pack file by path (`./my-pack.yaml`)."""
    path = _pack_path(name_or_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyPackError(f"{path}: cannot read YAML: {exc}") from exc
    pack = policy_pack_from_yaml(text, source=str(path))
    if path.parent == POLICY_PACKS_DIR and pack.name != path.stem:
        raise PolicyPackError(f"{path}: bundled pack name {pack.name!r} must match its file name")
    return pack


def load_policy_packs(names_or_paths: Iterable[str | Path]) -> tuple[PolicyPack, ...]:
    packs: list[PolicyPack] = []
    for item in names_or_paths:
        pack = load_policy_pack(item)
        if any(existing.name == pack.name for existing in packs):
            raise PolicyPackError(f"policy pack {pack.name!r} given twice")
        packs.append(pack)
    return tuple(packs)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def list_command() -> int:
    """`benchpress policy list`."""
    try:
        packs = [load_policy_pack(name) for name in available_policy_packs()]
    except PolicyPackError as exc:
        print(f"policy list: {exc}", file=sys.stderr)
        return 2
    width = max([len("NAME"), *(len(pack.name) for pack in packs)])
    print(f"{'NAME'.ljust(width)}  RULES  TITLE")
    for pack in packs:
        print(f"{pack.name.ljust(width)}  {str(len(pack.rules)).rjust(5)}  {pack.title}")
    return 0


def show_command(name_or_path: str) -> int:
    """`benchpress policy show NAME`."""
    try:
        pack = load_policy_pack(name_or_path)
    except PolicyPackError as exc:
        print(f"policy show: {exc}", file=sys.stderr)
        return 2
    print(f"{pack.name}: {pack.title}")
    if pack.description:
        print(pack.description.strip())
    print(f"source: {pack.source}")
    print(f"\nrules ({len(pack.rules)}):")
    for rule in pack.rules:
        print(f"  {rule.id}")
        print(f"    refuses: {rule.reason}")
        print(f"    matches: {rule.match.summary()}")
        if rule.unless_approved:
            print(f"    unless approved by fact: {', '.join(rule.unless_approved)}")
    return 0
