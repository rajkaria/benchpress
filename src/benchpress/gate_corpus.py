"""The gate-rule corpus: data cases that pin down what the mutation gate refuses and allows.

A case is a context (the facts the gate consults), one action, and the expected decision.
The bundled corpus ships as package data under `benchpress/corpus/`, and anyone can add a
YAML file. `benchpress gate check [PATH ...]` runs every case and exits non-zero when the
gate's decision differs from the one a case records.

The corpus documents what the gate *does*. A case the maintainers believe the gate gets
wrong carries an `xfail` note: it records the decision the gate *should* make, runs as a
strict expected failure, and turns into a failure the day the gate is fixed (so the note
gets removed with the fix).
"""

from __future__ import annotations

import base64
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from benchpress.context import (
    Action,
    ActionKind,
    Candidate,
    Context,
    DefinitionOfDone,
    ForbiddenClass,
    GateVerdict,
    HttpMethod,
    Plan,
    PolicyRecord,
    ProtectedSet,
    ResolvedTarget,
)
from benchpress.gate import Gate, fingerprint
from benchpress.packs import PACK_RULE_PREFIX, PolicyPack, PolicyPackError, load_policy_packs

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"

# The rule names `Gate` can refuse with, in its refusal priority order.
GATE_RULES: tuple[str, ...] = (
    "control_plane",
    "method",
    "action_class",
    "protected",
    "provider_scope",
    "plan_membership",
    "field_smuggling",
    "external_destination",
    "idempotency",
)
FORBIDDEN_CLASSES: frozenset[str] = frozenset(get_args(ForbiddenClass))
BASE64URL_KEY = "$base64url"

CaseStatus = Literal["pass", "fail", "xfail", "xpass"]


class CorpusError(ValueError):
    """A corpus file that cannot be read, parsed, or validated."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def expand_body(value: object) -> object:
    """Replace every `{"$base64url": "text"}` mapping with the base64url encoding of `text`.

    Gmail carries whole RFC 2822 messages as unpadded base64url in `raw`. Writing the
    plain text in the case and letting the loader encode it keeps cases reviewable.
    """
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if len(mapping) == 1 and BASE64URL_KEY in mapping:
            text = mapping[BASE64URL_KEY]
            if not isinstance(text, str):
                raise CorpusError(f"{BASE64URL_KEY} takes a string, got {type(text).__name__}")
            return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")
        return {str(key): expand_body(item) for key, item in mapping.items()}
    if isinstance(value, list):
        return [expand_body(item) for item in cast(list[object], value)]
    return value


class CaseAction(_Model):
    """One request as the gate sees it. Mirrors `benchpress.context.Action`."""

    id: str = "a1"
    kind: ActionKind = "update"
    provider: str
    method: HttpMethod
    path: str
    query: dict[str, str] = Field(default_factory=dict[str, str])
    body: object | None = None
    fields: tuple[str, ...] = ()
    target_refs: tuple[str, ...] = ()
    satisfies: tuple[str, ...] = ("end_state[0]",)

    def to_action(self) -> Action:
        return Action.model_validate(
            {
                "id": self.id,
                "kind": self.kind,
                "provider": self.provider,
                "method": self.method,
                "path": self.path,
                "query": dict(self.query),
                "body": expand_body(self.body),
                "fields": self.fields,
                "target_refs": self.target_refs,
                "satisfies": self.satisfies,
            }
        )


class CaseProtected(_Model):
    """The deny-list: records the run resolved *away from* (look-alikes, unchosen candidates)."""

    ids: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()


class CaseContext(_Model):
    """Everything the gate consults, and nothing else. All fields are optional."""

    prompt: str = ""
    providers: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    write_scope: tuple[str, ...] = ()
    facts: dict[str, str] = Field(default_factory=dict[str, str])
    protected: CaseProtected = Field(default_factory=CaseProtected)
    targets: tuple[ResolvedTarget, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    policies: tuple[PolicyRecord, ...] = ()
    plan: tuple[CaseAction, ...] = ()
    enforce_plan: bool = False
    succeeded: tuple[CaseAction, ...] = ()

    @field_validator("forbidden")
    @classmethod
    def _known_classes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - FORBIDDEN_CLASSES)
        if unknown:
            raise ValueError(f"unknown action class(es) {unknown}; known: {sorted(FORBIDDEN_CLASSES)}")
        return value

    def build(self) -> Context:
        ctx = Context(trial_id="gate-corpus", user_prompt=self.prompt, providers=self.providers)
        ctx.candidates = list(self.candidates)
        ctx.targets = list(self.targets)
        ctx.policies = list(self.policies)
        ctx.protected = ProtectedSet(
            ids=set(self.protected.ids),
            names=set(self.protected.names),
            domains={domain.casefold() for domain in self.protected.domains},
            emails={email.casefold() for email in self.protected.emails},
        )
        ctx.dod = DefinitionOfDone(forbidden=self.forbidden, write_scope=self.write_scope, facts=dict(self.facts))
        ctx.plan = Plan(actions=tuple(action.to_action() for action in self.plan))
        return ctx


class CaseExpect(_Model):
    decision: Literal["allow", "refuse"]
    rule: str | None = None
    reason_contains: str | None = None

    @model_validator(mode="after")
    def _rule_iff_refuse(self) -> CaseExpect:
        if self.decision == "refuse":
            if not self.rule:
                raise ValueError("a refuse expectation must name the rule that refuses")
            if self.rule not in GATE_RULES and not self.rule.startswith(PACK_RULE_PREFIX):
                raise ValueError(
                    f"unknown gate rule {self.rule!r}; known: {list(GATE_RULES)} or '{PACK_RULE_PREFIX}<pack>.<rule>'"
                )
        elif self.rule or self.reason_contains:
            raise ValueError("an allow expectation takes no rule or reason")
        return self

    @property
    def label(self) -> str:
        return "allow" if self.decision == "allow" else f"refuse:{self.rule}"


class GateCase(_Model):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    description: str = ""
    tags: tuple[str, ...] = ()
    packs: tuple[str, ...] = ()
    context: CaseContext = Field(default_factory=CaseContext)
    action: CaseAction
    expect: CaseExpect
    xfail: str | None = Field(default=None, min_length=1)
    source: str = ""

    @model_validator(mode="after")
    def _pack_rule_needs_its_pack(self) -> GateCase:
        rule = self.expect.rule or ""
        if rule.startswith(PACK_RULE_PREFIX):
            pack_name = rule.removeprefix(PACK_RULE_PREFIX).split(".", 1)[0]
            if not any(Path(item).stem == pack_name for item in self.packs):
                raise ValueError(f"expected rule {rule!r} needs pack {pack_name!r} in `packs`")
        return self

    def policy_packs(self) -> tuple[PolicyPack, ...]:
        """Bundled packs by name; pack files by path, relative to the case file."""
        base = Path(self.source).parent if self.source else Path.cwd()
        resolved: list[str | Path] = []
        for item in self.packs:
            path = Path(item)
            if path.suffix in {".yaml", ".yml"} and not path.is_absolute():
                path = base / path
                resolved.append(path)
            else:
                resolved.append(item)
        return load_policy_packs(resolved)


class CorpusFile(_Model):
    """One YAML file. `shared` is free-form and exists to hold YAML anchors (`&ops`)."""

    shared: dict[str, object] = Field(default_factory=dict[str, object])
    cases: tuple[GateCase, ...] = ()


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------


def corpus_files(paths: Iterable[str | Path] | None = None) -> tuple[Path, ...]:
    """Resolve files and directories (searched recursively for *.yaml / *.yml)."""
    roots = [Path(path) for path in paths] if paths else [CORPUS_DIR]
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(sorted(path for path in root.rglob("*") if path.suffix in {".yaml", ".yml"}))
        elif root.is_file():
            files.append(root)
        else:
            raise CorpusError(f"no such corpus file or directory: {root}")
    return tuple(files)


def load_corpus_file(path: Path) -> tuple[GateCase, ...]:
    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CorpusError(f"{path}: cannot read YAML: {exc}") from exc
    if raw is None:
        return ()
    try:
        parsed = CorpusFile.model_validate(raw)
    except ValidationError as exc:
        raise CorpusError(f"{path}: {exc}") from exc
    cases = tuple(case.model_copy(update={"source": str(path)}) for case in parsed.cases)
    for case in cases:
        try:
            case.action.to_action()
            case.context.build()
            case.policy_packs()
        except (ValidationError, CorpusError, PolicyPackError) as exc:
            raise CorpusError(f"{path}: case {case.name!r}: {exc}") from exc
    return cases


def load_corpus(paths: Iterable[str | Path] | None = None) -> tuple[GateCase, ...]:
    """Every case under `paths` (default: the bundled corpus). Case names are unique."""
    cases: list[GateCase] = []
    seen: dict[str, str] = {}
    for path in corpus_files(paths):
        for case in load_corpus_file(path):
            if case.name in seen:
                raise CorpusError(f"duplicate case name {case.name!r} in {seen[case.name]} and {case.source}")
            seen[case.name] = case.source
            cases.append(case)
    return tuple(cases)


# --------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseResult:
    case: GateCase
    verdict: GateVerdict
    matched: bool
    status: CaseStatus

    @property
    def ok(self) -> bool:
        return self.status in ("pass", "xfail")

    @property
    def actual(self) -> str:
        return "allow" if self.verdict.allowed else f"refuse:{self.verdict.rule}"


def build_gate(case: GateCase) -> Gate:
    context = case.context.build()
    gate = Gate(context=context, allow_unplanned=not case.context.enforce_plan, policy_packs=case.policy_packs())
    for prior in case.context.succeeded:
        gate.record_success(fingerprint(prior.to_action()))
    return gate


def verdict_matches(expect: CaseExpect, verdict: GateVerdict) -> bool:
    if expect.decision == "allow":
        return verdict.allowed
    if verdict.allowed or verdict.rule != expect.rule:
        return False
    return expect.reason_contains is None or expect.reason_contains.casefold() in verdict.reason.casefold()


def run_case(case: GateCase) -> CaseResult:
    verdict = build_gate(case).evaluate(case.action.to_action())
    matched = verdict_matches(case.expect, verdict)
    status: CaseStatus = ("xpass" if matched else "xfail") if case.xfail else ("pass" if matched else "fail")
    return CaseResult(case=case, verdict=verdict, matched=matched, status=status)


def run_corpus(cases: Sequence[GateCase]) -> tuple[CaseResult, ...]:
    return tuple(run_case(case) for case in cases)


def render_results(results: Sequence[CaseResult], *, verbose: bool = False) -> str:
    headers = ("STATUS", "CASE", "EXPECTED", "ACTUAL")
    rows = [(result.status.upper(), result.case.name, result.case.expect.label, result.actual) for result in results]
    widths = [max([len(header), *(len(row[index]) for row in rows)]) for index, header in enumerate(headers)]
    lines = ["  ".join(cell.ljust(width) for cell, width in zip(headers, widths, strict=True)).rstrip()]
    for result, row in zip(results, rows, strict=True):
        lines.append("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
        if result.status in ("fail", "xpass") or verbose:
            if result.verdict.reason:
                lines.append(f"    reason: {result.verdict.reason}")
            if result.case.xfail:
                lines.append(f"    xfail: {result.case.xfail}")
            if result.status in ("fail", "xpass"):
                lines.append(f"    source: {result.case.source}")
    counts = {status: sum(1 for result in results if result.status == status) for status in get_args(CaseStatus)}
    lines.append(
        f"\n{len(results)} case(s): {counts['pass']} passed, {counts['xfail']} xfail, "
        f"{counts['fail']} failed, {counts['xpass']} xpass"
    )
    return "\n".join(lines)


def check_command(paths: Sequence[str], *, verbose: bool = False) -> int:
    """`benchpress gate check`: 0 when every case holds, 1 on a mismatch or xpass, 2 on a bad corpus."""
    try:
        cases = load_corpus(paths or None)
    except CorpusError as exc:
        print(f"gate check: {exc}", file=sys.stderr)
        return 2
    if not cases:
        print("gate check: no cases found", file=sys.stderr)
        return 2
    results = run_corpus(cases)
    print(render_results(results, verbose=verbose))
    return 0 if all(result.ok for result in results) else 1
