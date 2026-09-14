"""Rehearse: the model never decides a production write live.

A request is run N times, each time against a fresh, isolated copy of the relevant state (a
`Stage`). Every run goes through the full controller (`run_trial`: gate, read-back, receipts).
The rehearsal converges only when every run ends in the same normalized final state, the gate
refused nothing, and every run executed the same ordered writes. The converged, typed plan is
what `replay` (stage 2) later executes against production — with no model in the loop.

Normalization is explicit (`Normalizer`): timestamps collapse to `<time>`, and ids a run
*generated* (present in a write response or the final state, absent from the baseline) are
renumbered `<id:1>`, `<id:2>`, … by first appearance. Nothing here is task-specific.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from benchpress.context import Action, Context, GateVerdict, LedgerEntry, Plan
from benchpress.controller import run_trial
from benchpress.gate import Gate, fingerprint
from benchpress.model import ModelClient, ModelConfig
from benchpress.phases.execute import field_checks, fill_placeholders
from benchpress.playbooks import Playbook, extract_field
from benchpress.tools import PROVIDER_API, BudgetExhausted, ToolBus, ToolExecutor

JsonState = dict[str, Any]

DEFAULT_REHEARSAL_SYSTEM_PROMPT = (
    "You are an operations agent working across the provisioned business systems. Complete the request "
    "through the provider_api tool using ordinary data-plane routes only. Minimize mutations, respect every "
    "explicit prohibition, verify the final state through reads, and return exactly the output requested."
)

# The Context fields the gate reads. Replay rebuilds a gate from exactly these.
GATE_CONTEXT_FIELDS: frozenset[str] = frozenset(
    {"trial_id", "user_prompt", "providers", "frame", "policies", "candidates", "targets", "protected", "dod"}
)


# --------------------------------------------------------------------------------------
# Stage contract
# --------------------------------------------------------------------------------------


class Stage(Protocol):
    """A fresh, isolated copy of the state a request touches."""

    async def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> object: ...

    async def snapshot(self) -> JsonState: ...

    async def aclose(self) -> None: ...


StageFactory = Callable[[], Awaitable[Stage]]
ModelFactory = Callable[[int], ModelClient]


# --------------------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------------------

PLACEHOLDER = re.compile(r"^<id:\d+>$")
TIME_PLACEHOLDER = "<time>"
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")


@dataclass(frozen=True)
class Normalizer:
    """The volatile-field rules, in one place.

    - A *time key* (`created`, `updated_at`, `createdAt`, …) holding a scalar becomes `<time>`;
      so does any string that is a full ISO-8601 datetime, wherever it sits.
    - An *id* is a scalar under an id key (`id`, `ts`, `*_id`, `*Id`, `*_ts`), or a mapping key
      that looks like an identifier (has a digit, at least four characters).
    - Generated ids are renumbered by first appearance and replaced wherever they occur: as a
      whole value, as a mapping key, or as a delimited token inside a longer string.

    Business dates written by a plan also collapse to `<time>` in the state hash; the write
    fingerprints (which are not time-normalized) still tell two such runs apart.
    """

    id_keys: frozenset[str] = frozenset({"id", "ts", "thread_ts", "threadid", "client_msg_id", "event_ts"})
    id_suffixes: tuple[str, ...] = ("_id", "Id", "_ts")
    time_keys: frozenset[str] = frozenset(
        {
            "created",
            "updated",
            "timestamp",
            "internaldate",
            "createdate",
            "lastmodifieddate",
            "hs_lastmodifieddate",
            "hs_createdate",
            "date_created",
            "date_updated",
            "latency_ms",
            "elapsed_s",
        }
    )
    time_suffixes: tuple[str, ...] = ("_at", "At", "_time", "_timestamp", "Timestamp")

    def is_id_key(self, key: str) -> bool:
        return key.casefold() in self.id_keys or key.endswith(self.id_suffixes)

    def is_time_key(self, key: str) -> bool:
        return key.casefold() in self.time_keys or key.endswith(self.time_suffixes)

    @staticmethod
    def looks_like_id(value: str) -> bool:
        return len(value) >= 4 and any(char.isdigit() for char in value) and " " not in value

    def ids_in(self, value: object) -> list[str]:
        """Id-like values in canonical traversal order (sorted keys, list order), unique."""
        seen: dict[str, None] = {}
        for found in self._walk_ids(value, under_id_key=False):
            seen.setdefault(found, None)
        return list(seen)

    def _walk_ids(self, value: object, *, under_id_key: bool) -> Iterator[str]:
        if isinstance(value, Mapping):
            mapping = cast(Mapping[object, object], value)
            for key in sorted(mapping, key=str):
                name = str(key)
                child = mapping[key]
                if _is_mapping(child) and self.looks_like_id(name):
                    yield name
                if self.is_time_key(name):
                    continue
                yield from self._walk_ids(child, under_id_key=self.is_id_key(name))
        elif isinstance(value, (list, tuple)):
            for item in cast(Sequence[object], value):
                yield from self._walk_ids(item, under_id_key=under_id_key)
        elif under_id_key and isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value)
            if text:
                yield text

    def normalize(self, value: object, generated: Mapping[str, str]) -> Any:
        """Replace timestamps and generated ids (`generated`: concrete value → placeholder)."""
        pattern = _token_pattern(generated)
        return self._normalize(value, generated, pattern)

    def _normalize(self, value: object, generated: Mapping[str, str], pattern: re.Pattern[str] | None) -> Any:
        if isinstance(value, Mapping):
            mapping = cast(Mapping[object, object], value)
            out: dict[str, Any] = {}
            for key in sorted(mapping, key=str):
                name = str(key)
                child = mapping[key]
                new_key = generated.get(name, name)
                if self.is_time_key(name) and not _is_container(child) and child is not None:
                    out[new_key] = TIME_PLACEHOLDER
                else:
                    out[new_key] = self._normalize(child, generated, pattern)
            return out
        if isinstance(value, (list, tuple)):
            return [self._normalize(item, generated, pattern) for item in cast(Sequence[object], value)]
        if isinstance(value, bool) or value is None or isinstance(value, float):
            return value
        if isinstance(value, int):
            return generated.get(str(value), value)
        if isinstance(value, str):
            if value in generated:
                return generated[value]
            if _ISO_DATETIME.match(value):
                return TIME_PLACEHOLDER
            if pattern is None:
                return value
            return pattern.sub(lambda match: generated[match.group(0)], value)
        return str(value)

    def generated_ids(self, baseline: object, *sources: object) -> dict[str, str]:
        """Ids present in `sources` (in order) but not in `baseline`, numbered by first appearance.

        Returns concrete value → placeholder.
        """
        known = set(self.ids_in(baseline))
        out: dict[str, str] = {}
        for source in sources:
            for value in self.ids_in(source):
                if value not in known and value not in out:
                    out[value] = f"<id:{len(out) + 1}>"
        return out


DEFAULT_NORMALIZER = Normalizer()


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


def _is_container(value: object) -> bool:
    return isinstance(value, (Mapping, list, tuple))


def _token_pattern(generated: Mapping[str, str]) -> re.Pattern[str] | None:
    if not generated:
        return None
    alternatives = "|".join(re.escape(value) for value in sorted(generated, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9_])(?:{alternatives})(?![A-Za-z0-9_])")


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def state_hash(normalized_state: object) -> str:
    return hashlib.sha256(canonical_json(normalized_state).encode("utf-8")).hexdigest()


def first_difference(reference: object, candidate: object, path: str = "") -> str | None:
    """Dotted path of the first difference (sorted keys), or None when equal.

    A reference leaf that is an id placeholder matches any string placeholder or value on the
    candidate side only when they are equal; callers that need wildcard semantics pre-map ids.
    """
    if isinstance(reference, Mapping) and isinstance(candidate, Mapping):
        left = cast(Mapping[str, object], reference)
        right = cast(Mapping[str, object], candidate)
        for key in sorted(set(left) | set(right)):
            child_path = f"{path}.{key}" if path else key
            if key not in left or key not in right:
                return child_path
            found = first_difference(left[key], right[key], child_path)
            if found is not None:
                return found
        return None
    if isinstance(reference, list) and isinstance(candidate, list):
        left_items = cast(list[object], reference)
        right_items = cast(list[object], candidate)
        for index in range(max(len(left_items), len(right_items))):
            child_path = f"{path}.{index}" if path else str(index)
            if index >= len(left_items) or index >= len(right_items):
                return child_path
            found = first_difference(left_items[index], right_items[index], child_path)
            if found is not None:
                return found
        return None
    return None if reference == candidate else (path or "<root>")


# --------------------------------------------------------------------------------------
# Typed records
# --------------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PlannedWrite(_Model):
    """One executed write, in execution order, exactly as it went through the gate."""

    index: int
    action_id: str
    phase: str
    action: Action
    ok: bool
    status_code: int | None = None
    fingerprint: str
    normalized_fingerprint: str
    created: dict[str, str] = Field(default_factory=dict[str, str])  # response field path -> generated id
    readback: Any = None  # normalized read-back response body, when the controller read the write back

    def describe(self) -> str:
        return f"#{self.index} {self.action_id} {self.action.provider} {self.action.method} {self.action.path}"


class RefusalNote(_Model):
    run: int
    action_id: str
    rule: str
    reason: str = ""


class RunRecord(_Model):
    index: int
    trial_id: str
    status: str
    error: str | None = None
    state_hash: str
    refusals: tuple[GateVerdict, ...] = ()
    writes: tuple[PlannedWrite, ...] = ()
    failed_writes: tuple[str, ...] = ()
    generated_ids: dict[str, str] = Field(default_factory=dict[str, str])  # placeholder -> concrete
    trace_consistent: bool = True
    final_text: str = ""

    @property
    def fingerprints(self) -> tuple[str, ...]:
        return tuple(write.normalized_fingerprint for write in self.writes)


class WriteDiff(_Model):
    index: int
    reference: str | None = None
    candidate: str | None = None
    reference_fingerprint: str | None = None
    candidate_fingerprint: str | None = None


class RunDiff(_Model):
    run: int
    state_path: str | None = None
    first_write: WriteDiff | None = None


class Divergence(_Model):
    reference_run: int = 0
    differing_runs: tuple[int, ...] = ()
    refusals: tuple[RefusalNote, ...] = ()
    failed_writes: dict[str, tuple[str, ...]] = Field(default_factory=dict[str, tuple[str, ...]])
    diffs: tuple[RunDiff, ...] = ()

    def report(self) -> str:
        lines: list[str] = []
        for diff in self.diffs:
            if diff.state_path is not None:
                lines.append(f"run {diff.run} vs run {self.reference_run}: final state differs at {diff.state_path}")
            if diff.first_write is not None:
                write = diff.first_write
                lines.append(
                    f"run {diff.run} vs run {self.reference_run}: first differing write #{write.index}: "
                    f"{write.reference or '<none>'} != {write.candidate or '<none>'}"
                )
        for note in self.refusals:
            lines.append(f"run {note.run}: gate refused {note.action_id} ({note.rule}: {note.reason})")
        for run, action_ids in sorted(self.failed_writes.items()):
            lines.append(f"run {run}: writes failed: {', '.join(action_ids)}")
        return "\n".join(lines)


class Rehearsal(_Model):
    request: str
    providers: tuple[str, ...]
    n: int
    converged: bool
    reasons: tuple[str, ...] = ()
    state_hash: str | None = None
    plan: tuple[PlannedWrite, ...] = ()
    generated_ids: dict[str, str] = Field(default_factory=dict[str, str])  # placeholder -> concrete (run 0)
    gate_context: dict[str, Any] = Field(default_factory=dict[str, Any])
    runs: tuple[RunRecord, ...] = ()
    divergence: Divergence | None = None

    def summary(self) -> str:
        head = (
            f"rehearsal: {'CONVERGED' if self.converged else 'DIVERGED'} over {self.n} runs; "
            f"{len(self.plan)} planned writes"
        )
        if self.state_hash:
            head += f"; state {self.state_hash[:16]}"
        lines = [head, *(f"- {reason}" for reason in self.reasons)]
        if self.divergence is not None:
            lines.append(self.divergence.report())
        return "\n".join(line for line in lines if line)

    def write_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=1))
        return path

    @classmethod
    def read_json(cls, path: Path) -> Rehearsal:
        return cls.model_validate_json(path.read_text())


# --------------------------------------------------------------------------------------
# Recording executor
# --------------------------------------------------------------------------------------


@dataclass
class RecordedCall:
    tool_name: str
    tool_input: dict[str, Any]
    output: object


@dataclass
class RecordingExecutor:
    """Forwards every call to the stage and keeps the provider_api calls in order."""

    execute: Callable[[str, dict[str, Any]], Awaitable[object]]
    calls: list[RecordedCall] = field(default_factory=list[RecordedCall])

    async def __call__(self, tool_name: str, tool_input: dict[str, Any]) -> object:
        try:
            output = await self.execute(tool_name, tool_input)
        except Exception as exc:
            if tool_name == PROVIDER_API:
                self.calls.append(RecordedCall(tool_name, json.loads(canonical_json(tool_input)), None))
            raise exc
        if tool_name == PROVIDER_API:
            self.calls.append(RecordedCall(tool_name, json.loads(canonical_json(tool_input)), output))
        return output


def response_body(output: object) -> object:
    """The upstream body inside a gateway result (same unwrapping as the tool bus)."""
    if not isinstance(output, Mapping):
        return output
    payload = cast(Mapping[str, object], output)
    return payload.get("body", payload.get("data", payload))


def _executed(entry: LedgerEntry) -> bool:
    if entry.gate is not None and not entry.gate.allowed:
        return False
    return not (entry.error or "").startswith("blocked_path:")


def _field_paths(value: object, wanted: set[str], normalizer: Normalizer, prefix: str = "") -> dict[str, str]:
    """Response field path -> id, for every generated id found under an id key or as a mapping key."""
    found: dict[str, str] = {}
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        for key in sorted(mapping, key=str):
            name = str(key)
            child = mapping[key]
            path = f"{prefix}.{name}" if prefix else name
            if normalizer.is_id_key(name) and isinstance(child, (str, int)) and not isinstance(child, bool):
                if str(child) in wanted and str(child) not in found.values():
                    found[path] = str(child)
            else:
                for sub_path, sub_value in _field_paths(child, wanted, normalizer, path).items():
                    if sub_value not in found.values():
                        found[sub_path] = sub_value
    elif isinstance(value, list):
        for index, item in enumerate(cast(list[object], value)):
            path = f"{prefix}.{index}" if prefix else str(index)
            for sub_path, sub_value in _field_paths(item, wanted, normalizer, path).items():
                if sub_value not in found.values():
                    found[sub_path] = sub_value
    return found


def normalize_action(action: Action, generated: Mapping[str, str], normalizer: Normalizer) -> Action:
    return action.model_copy(
        update={
            "path": normalizer.normalize(action.path, generated),
            "query": normalizer.normalize(dict(action.query), generated),
            "body": normalizer.normalize(action.body, generated),
        }
    )


@dataclass(frozen=True)
class _Extracted:
    writes: tuple[PlannedWrite, ...]
    failed: tuple[str, ...]
    generated: dict[str, str]  # concrete -> placeholder
    normalized_state: Any
    consistent: bool


def extract_run(
    ctx: Context,
    calls: Sequence[RecordedCall],
    baseline: JsonState,
    final: JsonState,
    normalizer: Normalizer = DEFAULT_NORMALIZER,
) -> _Extracted:
    """Turn a finished run's ledger + recorded calls into the typed, ordered write list."""
    executed = [entry for entry in ctx.ledger if _executed(entry)]
    consistent = len(executed) == len(calls)
    pairs = list(zip(executed, calls, strict=False))
    raw_writes: list[tuple[int, LedgerEntry, Action, object]] = []
    for position, (entry, call) in enumerate(pairs):
        if entry.action_id is None:
            continue
        action = _planned_action(ctx, entry)
        if action is None:
            consistent = False
            continue
        raw_writes.append((position, entry, action, response_body(call.output)))

    ok_responses = [body for _, entry, _, body in raw_writes if entry.ok]
    generated = normalizer.generated_ids(baseline, *ok_responses, final)
    writes: list[PlannedWrite] = []
    failed: list[str] = []
    for order, (position, entry, action, body) in enumerate(raw_writes):
        if not entry.ok:
            failed.append(entry.action_id or action.id)
        readback = _find_readback(action, body, pairs, position, raw_writes, order)
        writes.append(
            PlannedWrite(
                index=len(writes),
                action_id=action.id,
                phase=entry.phase,
                action=action,
                ok=entry.ok,
                status_code=entry.status_code,
                fingerprint=fingerprint(action),
                normalized_fingerprint=fingerprint(normalize_action(action, generated, normalizer)),
                created=_field_paths(body, set(generated), normalizer) if entry.ok else {},
                readback=None if readback is None else normalizer.normalize(readback, generated),
            )
        )
    return _Extracted(
        writes=tuple(writes),
        failed=tuple(failed),
        generated=generated,
        normalized_state=normalizer.normalize(final, generated),
        consistent=consistent,
    )


def _planned_action(ctx: Context, entry: LedgerEntry) -> Action | None:
    for action in ctx.plan.actions:
        if action.id == entry.action_id and action.method == entry.method and action.path == entry.path:
            return action
    return None


def _find_readback(
    action: Action,
    body: object,
    pairs: Sequence[tuple[LedgerEntry, RecordedCall]],
    position: int,
    raw_writes: Sequence[tuple[int, LedgerEntry, Action, object]],
    order: int,
) -> object | None:
    spec = action.readback
    if spec is None:
        return None
    response: Mapping[str, object] = cast(Mapping[str, object], body) if _is_mapping(body) else {}
    path = fill_placeholders(spec.path, response)
    query = {key: fill_placeholders(value, response) for key, value in spec.query.items()}
    stop = raw_writes[order + 1][0] if order + 1 < len(raw_writes) else len(pairs)
    for entry, call in pairs[position + 1 : stop]:
        if entry.action_id is not None:
            continue
        payload = call.tool_input
        if payload.get("path") == path and dict(cast(Mapping[str, str], payload.get("query") or {})) == query:
            return response_body(call.output)
    return None


# --------------------------------------------------------------------------------------
# rehearse()
# --------------------------------------------------------------------------------------


async def rehearse(
    request: str,
    providers: Sequence[str],
    stage_factory: StageFactory,
    *,
    n: int = 3,
    system_prompt: str = DEFAULT_REHEARSAL_SYSTEM_PROMPT,
    config: ModelConfig | None = None,
    model_factory: ModelFactory | None = None,
    playbooks: Mapping[str, Playbook] | None = None,
    trace_dir: Path | None = None,
    normalizer: Normalizer = DEFAULT_NORMALIZER,
) -> Rehearsal:
    """Run the request `n` times, each on a fresh stage, and decide whether the plan converged."""
    if n < 2:
        raise ValueError("a rehearsal needs at least two runs to show convergence")
    runs: list[RunRecord] = []
    states: list[Any] = []
    contexts: list[Context] = []
    for index in range(n):
        stage = await stage_factory()
        try:
            baseline = await stage.snapshot()
            recorder = RecordingExecutor(stage.execute_tool)
            result = await run_trial(
                system_prompt=system_prompt,
                user_prompt=request,
                providers=providers,
                execute_tool=recorder,
                config=config or (ModelConfig(model="rehearsal") if model_factory is not None else None),
                trace_dir=None if trace_dir is None else trace_dir / f"run-{index}",
                trial_id=f"rehearsal-{index}",
                model_client=None if model_factory is None else model_factory(index),
                playbooks=playbooks,
            )
            final = await stage.snapshot()
        finally:
            await stage.aclose()
        extracted = extract_run(result.context, recorder.calls, baseline, final, normalizer)
        states.append(extracted.normalized_state)
        contexts.append(result.context)
        runs.append(
            RunRecord(
                index=index,
                trial_id=result.context.trial_id,
                status=result.status,
                error=result.error,
                state_hash=state_hash(extracted.normalized_state),
                refusals=tuple(result.context.refusals),
                writes=extracted.writes,
                failed_writes=extracted.failed,
                generated_ids={placeholder: value for value, placeholder in extracted.generated.items()},
                trace_consistent=extracted.consistent,
                final_text=result.final_text,
            )
        )
    return verdict(request, providers, runs, states, contexts[0])


def verdict(
    request: str,
    providers: Sequence[str],
    runs: Sequence[RunRecord],
    states: Sequence[Any],
    reference_context: Context,
) -> Rehearsal:
    """The convergence rule: equal state hashes, zero refusals, identical write fingerprints."""
    reference = runs[0]
    reasons: list[str] = []
    differing = [run.index for run in runs[1:] if run.state_hash != reference.state_hash]
    if differing:
        reasons.append(f"final state hashes differ from run 0 in runs {differing}")
    fingerprint_diff = [run.index for run in runs[1:] if run.fingerprints != reference.fingerprints]
    if fingerprint_diff:
        reasons.append(f"write sequences differ from run 0 in runs {fingerprint_diff}")
    refusing = [run.index for run in runs if run.refusals]
    if refusing:
        reasons.append(f"the gate refused writes in runs {refusing}")
    failing = [run.index for run in runs if run.failed_writes]
    if failing:
        reasons.append(f"writes failed in runs {failing}")
    inconsistent = [run.index for run in runs if not run.trace_consistent]
    if inconsistent:
        reasons.append(f"ledger and recorded calls disagree in runs {inconsistent}")
    converged = not reasons

    divergence: Divergence | None = None
    if not converged:
        diffs: list[RunDiff] = []
        for run in runs[1:]:
            state_path = (
                first_difference(states[0], states[run.index]) if run.state_hash != reference.state_hash else None
            )
            write_diff = _first_write_diff(reference, run)
            if state_path is not None or write_diff is not None:
                diffs.append(RunDiff(run=run.index, state_path=state_path, first_write=write_diff))
        divergence = Divergence(
            differing_runs=tuple(sorted(set(differing) | set(fingerprint_diff))),
            refusals=tuple(
                RefusalNote(run=run.index, action_id=item.action_id, rule=item.rule, reason=item.reason)
                for run in runs
                for item in run.refusals
            ),
            failed_writes={str(run.index): run.failed_writes for run in runs if run.failed_writes},
            diffs=tuple(diffs),
        )
    return Rehearsal(
        request=request,
        providers=tuple(providers),
        n=len(runs),
        converged=converged,
        reasons=tuple(reasons),
        state_hash=reference.state_hash if converged else None,
        plan=reference.writes if converged else (),
        generated_ids=dict(reference.generated_ids) if converged else {},
        gate_context=reference_context.model_dump(mode="json", include=set(GATE_CONTEXT_FIELDS)),
        runs=tuple(runs),
        divergence=divergence,
    )


def _first_write_diff(reference: RunRecord, run: RunRecord) -> WriteDiff | None:
    for index in range(max(len(reference.writes), len(run.writes))):
        left = reference.writes[index] if index < len(reference.writes) else None
        right = run.writes[index] if index < len(run.writes) else None
        if left is not None and right is not None and left.normalized_fingerprint == right.normalized_fingerprint:
            continue
        return WriteDiff(
            index=index,
            reference=None if left is None else _describe_write(left),
            candidate=None if right is None else _describe_write(right),
            reference_fingerprint=None if left is None else left.normalized_fingerprint,
            candidate_fingerprint=None if right is None else right.normalized_fingerprint,
        )
    return None


def _describe_write(write: PlannedWrite) -> str:
    body = canonical_json(write.action.body)
    if len(body) > 160:
        body = body[:157] + "..."
    return f"{write.describe()} {body}"


# --------------------------------------------------------------------------------------
# replay()
# --------------------------------------------------------------------------------------

ReplayStatus = Literal["completed", "stopped", "refused"]
Snapshot = Callable[[], Awaitable[JsonState]]


class ReplayStep(_Model):
    index: int
    action_id: str
    provider: str
    method: str
    path: str
    fingerprint: str
    allowed: bool
    rule: str = ""
    reached_provider: bool = False
    ok: bool = False
    status_code: int | None = None
    substituted: dict[str, str] = Field(default_factory=dict[str, str])  # rehearsal id -> replay id
    created: dict[str, str] = Field(default_factory=dict[str, str])  # response field path -> replay id
    readback_checked: bool = False
    mismatches: tuple[str, ...] = ()


class ReplayReceipt(_Model):
    """What replay actually did. `executed` counts writes that reached the provider."""

    status: ReplayStatus
    reason: str = ""
    rehearsal_state_hash: str | None = None
    planned: int = 0
    executed: int = 0
    stopped_at: int | None = None
    steps: tuple[ReplayStep, ...] = ()
    id_map: dict[str, str] = Field(default_factory=dict[str, str])  # rehearsal concrete id -> replay concrete id
    final_state_hash: str | None = None
    state_matches_rehearsal: bool | None = None

    def summary(self) -> str:
        lines = [f"replay: {self.status.upper()}; {self.executed}/{self.planned} planned writes reached the provider"]
        if self.reason:
            lines.append(f"- {self.reason}")
        for step in self.steps:
            if not step.allowed:
                outcome = f"refused by the gate ({step.rule})"
            elif not step.ok:
                outcome = f"failed ({step.status_code})"
            elif step.mismatches:
                outcome = f"landed, read-back mismatch: {step.mismatches[0]}"
            else:
                outcome = "ok, read back" if step.readback_checked else "ok (no read-back defined)"
            lines.append(f"  #{step.index} {step.action_id} {step.provider} {step.method} {step.path}: {outcome}")
        if self.stopped_at is not None and self.stopped_at + 1 < self.planned:
            lines.append(f"- writes #{self.stopped_at + 1}..#{self.planned - 1} were not attempted")
        if self.state_matches_rehearsal is not None:
            verdict_text = "matches" if self.state_matches_rehearsal else "does NOT match"
            lines.append(
                f"- final normalized state {verdict_text} the rehearsal ({(self.final_state_hash or '')[:16]})"
            )
        return "\n".join(lines)

    def write_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=1))
        return path


def substitute_ids(value: object, id_map: Mapping[str, str], used: dict[str, str] | None = None) -> Any:
    """Replace rehearsal-generated ids with this replay's ids (whole values and delimited tokens)."""
    if not id_map:
        return value
    pattern = _token_pattern(id_map)
    return _substitute(value, id_map, pattern, used if used is not None else {})


def _substitute(value: object, id_map: Mapping[str, str], pattern: re.Pattern[str] | None, used: dict[str, str]) -> Any:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {
            _substitute(str(key), id_map, pattern, used): _substitute(child, id_map, pattern, used)
            for key, child in mapping.items()
        }
    if isinstance(value, list):
        return [_substitute(item, id_map, pattern, used) for item in cast(list[object], value)]
    if isinstance(value, tuple):
        return tuple(_substitute(item, id_map, pattern, used) for item in cast(tuple[object, ...], value))
    if isinstance(value, str) and pattern is not None:

        def swap(match: re.Match[str]) -> str:
            used[match.group(0)] = id_map[match.group(0)]
            return id_map[match.group(0)]

        return pattern.sub(swap, value)
    return value


def substitute_action(action: Action, id_map: Mapping[str, str]) -> tuple[Action, dict[str, str]]:
    used: dict[str, str] = {}
    if not id_map:
        return action, used
    readback = action.readback
    if readback is not None:
        readback = readback.model_copy(
            update={
                "path": substitute_ids(readback.path, id_map, used),
                "query": substitute_ids(dict(readback.query), id_map, used),
                "body": substitute_ids(readback.body, id_map, used),
            }
        )
    updated = action.model_copy(
        update={
            "path": substitute_ids(action.path, id_map, used),
            "query": substitute_ids(dict(action.query), id_map, used),
            "body": substitute_ids(action.body, id_map, used),
            "headers": substitute_ids(dict(action.headers), id_map, used),
            "readback": readback,
        }
    )
    return updated, used


def record_difference(reference: object, candidate: object, bound: frozenset[str], path: str = "") -> str | None:
    """`first_difference`, except an id placeholder this replay has not bound matches any scalar."""
    if isinstance(reference, str) and PLACEHOLDER.match(reference) and reference not in bound:
        return None if not _is_container(candidate) else (path or "<root>")
    if isinstance(reference, Mapping) and isinstance(candidate, Mapping):
        left = cast(Mapping[str, object], reference)
        right = cast(Mapping[str, object], candidate)
        for key in sorted(set(left) | set(right)):
            child_path = f"{path}.{key}" if path else key
            if key not in left or key not in right:
                return child_path
            found = record_difference(left[key], right[key], bound, child_path)
            if found is not None:
                return found
        return None
    if isinstance(reference, list) and isinstance(candidate, list):
        left_items = cast(list[object], reference)
        right_items = cast(list[object], candidate)
        if len(left_items) != len(right_items):
            shorter = min(len(left_items), len(right_items))
            return f"{path}.{shorter}" if path else str(shorter)
        for index, (left_item, right_item) in enumerate(zip(left_items, right_items, strict=True)):
            found = record_difference(left_item, right_item, bound, f"{path}.{index}" if path else str(index))
            if found is not None:
                return found
        return None
    return None if reference == candidate else (path or "<root>")


async def replay(
    rehearsal: Rehearsal,
    execute_tool: ToolExecutor,
    *,
    snapshot: Snapshot | None = None,
    normalizer: Normalizer = DEFAULT_NORMALIZER,
    trace_path: str | None = None,
) -> ReplayReceipt:
    """Execute a converged rehearsal's writes, in order, with no model in the loop.

    Every write goes through the tool bus and a gate rebuilt from the rehearsal's gate context.
    Ids generated earlier in the plan are substituted with the ids this replay generated. Every
    write is read back: its declared fields must hold the written values, and the whole record
    must match the rehearsed record (normalized). Replay stops at the first refusal, failure or
    mismatch. `snapshot` (optional; twins only) adds a final normalized state-hash comparison.
    """
    planned = len(rehearsal.plan)
    if not rehearsal.converged or rehearsal.state_hash is None:
        why = "; ".join(rehearsal.reasons) or "no converged state"
        return ReplayReceipt(status="refused", reason=f"rehearsal did not converge: {why}", planned=planned)
    if not rehearsal.gate_context:
        return ReplayReceipt(status="refused", reason="rehearsal carries no gate context", planned=planned)

    ctx = Context.model_validate({**rehearsal.gate_context, "trial_id": f"replay-{rehearsal.state_hash[:8]}"})
    ctx.plan = Plan(actions=tuple(write.action for write in rehearsal.plan))
    rehearsal_ids = {concrete: placeholder for placeholder, concrete in rehearsal.generated_ids.items()}
    baseline = await snapshot() if snapshot is not None else None
    recorder = RecordingExecutor(execute_tool)
    bus = ToolBus(context=ctx, execute=recorder, gate=Gate(ctx), trace_path=trace_path)
    bus.enter("P5")

    id_map: dict[str, str] = {}
    steps: list[ReplayStep] = []
    responses: list[object] = []
    status: ReplayStatus = "completed"
    reason = ""
    stopped_at: int | None = None

    for write in rehearsal.plan:
        action, used = substitute_action(write.action, id_map)
        ctx.plan = Plan(actions=tuple(action if item.id == action.id else item for item in ctx.plan.actions))
        try:
            result, verdict = await bus.perform(action)
        except BudgetExhausted as exc:
            status, reason, stopped_at = "stopped", f"tool budget exhausted before {action.id}: {exc}", write.index
            break
        reached = verdict.allowed and not (result.error or "").startswith("blocked_path:")
        step = ReplayStep(
            index=write.index,
            action_id=action.id,
            provider=action.provider,
            method=action.method,
            path=action.path,
            fingerprint=fingerprint(action),
            allowed=verdict.allowed,
            rule=verdict.rule,
            reached_provider=reached,
            ok=result.ok,
            status_code=result.status_code,
            substituted=used,
        )
        if not verdict.allowed:
            steps.append(step)
            status, stopped_at = "stopped", write.index
            reason = f"gate refused {action.id} ({verdict.rule}: {verdict.reason})"
            break
        if not result.ok:
            steps.append(step)
            status, stopped_at = "stopped", write.index
            reason = f"write {action.id} failed: {result.status_code} {str(result.error or '')[:160]}"
            break
        responses.append(result.body)
        created: dict[str, str] = {}
        missing: list[str] = []
        for field_path, rehearsed_id in write.created.items():
            value = extract_field(result.body, field_path)
            if value is None:
                missing.append(field_path)
                continue
            id_map[rehearsed_id] = value
            created[field_path] = value
        mismatches = [f"response has no generated id at {field_path}" for field_path in missing]
        checked = False
        if not mismatches:
            try:
                checked, found = await _read_back(bus, action, write, result.body, id_map, rehearsal_ids, normalizer)
            except BudgetExhausted as exc:
                checked, found = False, [f"read-back skipped: {exc}"]
            mismatches.extend(found)
        update = {"created": created, "readback_checked": checked, "mismatches": tuple(mismatches)}
        steps.append(step.model_copy(update=update))
        if mismatches:
            status, stopped_at = "stopped", write.index
            reason = f"write {action.id} landed but does not match the rehearsal: {mismatches[0]}"
            break

    final_hash: str | None = None
    matches: bool | None = None
    if snapshot is not None and baseline is not None:
        final = await snapshot()
        generated = normalizer.generated_ids(baseline, *responses, final)
        final_hash = state_hash(normalizer.normalize(final, generated))
        matches = final_hash == rehearsal.state_hash
    return ReplayReceipt(
        status=status,
        reason=reason,
        rehearsal_state_hash=rehearsal.state_hash,
        planned=planned,
        executed=sum(1 for step in steps if step.reached_provider),
        stopped_at=stopped_at,
        steps=tuple(steps),
        id_map=id_map,
        final_state_hash=final_hash,
        state_matches_rehearsal=matches,
    )


async def _read_back(
    bus: ToolBus,
    action: Action,
    write: PlannedWrite,
    body: object,
    id_map: Mapping[str, str],
    rehearsal_ids: Mapping[str, str],
    normalizer: Normalizer,
) -> tuple[bool, list[str]]:
    spec = action.readback
    if spec is None:
        return False, []
    response: Mapping[str, object] = cast(Mapping[str, object], body) if _is_mapping(body) else {}
    path = fill_placeholders(spec.path, response)
    query = {key: fill_placeholders(value, response) for key, value in spec.query.items()}
    result = await bus.read(action.provider, path, query=query, method=spec.method, body=spec.body)
    if not result.ok:
        return True, [f"read-back {path} failed: {result.status_code}"]
    mismatches: list[str] = []
    for check in field_checks(action, result.body):
        if check.observed is None:
            mismatches.append(f"{check.field}: wrote {check.expected!r}, read-back does not show it")
        elif not check.match:
            mismatches.append(f"{check.field}: wrote {check.expected!r}, read back {check.observed!r}")
    if write.readback is not None:
        replay_generated = {
            replay_id: rehearsal_ids[rehearsed_id]
            for rehearsed_id, replay_id in id_map.items()
            if rehearsed_id in rehearsal_ids
        }
        observed_record = normalizer.normalize(result.body, replay_generated)
        where = record_difference(write.readback, observed_record, frozenset(replay_generated.values()))
        if where is not None:
            mismatches.append(f"record differs from the rehearsed record at {where}")
    return True, mismatches


__all__ = [
    "ReplayReceipt",
    "ReplayStep",
    "record_difference",
    "replay",
    "substitute_action",
    "substitute_ids",
    "DEFAULT_NORMALIZER",
    "DEFAULT_REHEARSAL_SYSTEM_PROMPT",
    "Divergence",
    "ModelFactory",
    "Normalizer",
    "PlannedWrite",
    "Rehearsal",
    "RunRecord",
    "Stage",
    "StageFactory",
    "canonical_json",
    "extract_run",
    "first_difference",
    "rehearse",
    "state_hash",
    "verdict",
]
