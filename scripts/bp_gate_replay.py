"""Replay somebody else's writes through the Benchpress gate and report what it would have refused.

Two modes, both $0 and offline:

    # 1. our own baseline arm, gated by the Context the Benchpress arm actually built
    uv run python scripts/bp_gate_replay.py runs/billing-review/baseline/*

    # 2. ArgaBench's own recorded CRM trials, gated by a Context built from suite.json
    uv run python scripts/bp_gate_replay.py --fixture \
        arga-twins-benchmark/tests/fixtures/argabench_crm_legacy/historical-fable-5-high-crm.tar.gz

**What this is and is not.** The gate is evaluated against writes that already happened, in a
recording, on a different substrate. It therefore says *"Benchpress's gate would have refused this
call"* — nothing more. It does not say the trial would have passed, because a refusal changes the
trajectory and the rest of the recording no longer applies. Every table this script writes carries
that sentence. Mode 2 builds the context from the harness's own `suite.json` (`protected_terms` →
the deny-list, `allowed_providers` → the write scope) so the deny-list is the judges' data, not
ours; no other Benchpress knowledge is available to it, so its refusals are a floor, not a ceiling.

`--fixture` never modifies the vendored harness: the tarball is extracted into a temp directory.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchpress.context import (  # noqa: E402
    Action,
    Candidate,
    Context,
    DefinitionOfDone,
    Plan,
    PolicyRecord,
    ProtectedSet,
    ResolvedTarget,
    TaskFrame,
)
from benchpress.gate import Gate  # noqa: E402

DEFAULT_REPORTS = REPO_ROOT / "reports"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
# CRM and issue-tracker APIs express reads as POST bodies. Replaying those as "writes the gate
# would refuse" would inflate the number with calls that change nothing, so they are excluded and
# counted separately.
READ_SHAPED_SUFFIXES: tuple[str, ...] = ("/search", "/query", "/batch/read", "/read", "/list")
# The gate rules a Context built from `suite.json` alone can honestly support. `field_smuggling`
# needs the fields a plan declared and `external_destination` needs the domains a run resolved —
# neither exists in a recording, so firing them there would manufacture refusals. They sit *after*
# every scoped rule in the gate's own priority order, so excluding them cannot hide an earlier one.
SUITE_SCOPED_RULES = frozenset({"control_plane", "method", "action_class", "protected", "provider_scope"})
CAVEAT = (
    "A refusal here means the gate would have blocked that call. It does not mean the trial would "
    "have passed: refusing changes the trajectory, and the rest of a recording no longer applies."
)


@dataclass(frozen=True)
class ReplayCall:
    index: int
    provider: str
    method: str
    path: str
    allowed: bool
    rule: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "provider": self.provider,
            "method": self.method,
            "path": self.path,
            "allowed": self.allowed,
            "rule": self.rule,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReplayReport:
    label: str
    source: str
    context_source: str
    writes: int
    read_shaped_skipped: int
    calls: tuple[ReplayCall, ...]

    @property
    def refused(self) -> tuple[ReplayCall, ...]:
        return tuple(call for call in self.calls if not call.allowed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "source": self.source,
            "context_source": self.context_source,
            "writes": self.writes,
            "read_shaped_skipped": self.read_shaped_skipped,
            "refused": len(self.refused),
            "calls": [call.as_dict() for call in self.calls],
        }


# --------------------------------------------------------------------------------------
# Event → Action
# --------------------------------------------------------------------------------------


def is_read_shaped(path: str) -> bool:
    """A POST that reads (`/objects/deals/search`, `/query`) rather than mutating."""
    bare = path.split("?", 1)[0].rstrip("/").casefold()
    return any(bare.endswith(suffix) for suffix in READ_SHAPED_SUFFIXES)


def write_events(invocation: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """`(mutating provider_api calls in order, read-shaped POSTs skipped)`."""
    writes: list[dict[str, Any]] = []
    skipped = 0
    for item in _list(invocation.get("events")):
        event = _mapping(item)
        if event.get("type") != "tool_call" or event.get("name") != "provider_api":
            continue
        arguments = _mapping(event.get("arguments"))
        if str(arguments.get("method", "")).upper() not in WRITE_METHODS:
            continue
        if is_read_shaped(str(arguments.get("path", ""))):
            skipped += 1
            continue
        writes.append(arguments)
    return writes, skipped


def role_aliases() -> dict[str, str]:
    """`{role: provider}` from the harness's own table, so `hubspot_crm` resolves to `hubspot`.

    A recorded call may address a provider by its task role. Scoring that as "outside the write
    scope" would be a naming artefact, not a policy finding.
    """
    try:
        import evals.harness_bridge as harness_bridge

        return {role: name for name, role in harness_bridge.provider_roles().items()}
    except Exception:  # noqa: BLE001 - without the harness, provider names are used as recorded
        return {}


def action_from_arguments(index: int, arguments: Mapping[str, Any], aliases: Mapping[str, str] | None = None) -> Action:
    method = str(arguments.get("method", "GET")).upper()
    if method not in KNOWN_METHODS:
        method = "POST"
    provider = str(arguments.get("provider", ""))
    provider = (aliases or {}).get(provider, provider)
    return Action(
        id=f"replay-{index}",
        kind="update",
        provider=provider,
        method=cast(Any, method),
        path=str(arguments.get("path", "")),
        query={str(key): _scalar(value) for key, value in _mapping(arguments.get("query")).items()},
        body=arguments.get("body"),
        # `fields` is left empty on purpose: the field-smuggling rule compares a body against the
        # fields a *plan* declared, and a replayed call never declared any. Inventing them from the
        # body would make the rule trivially true and manufacture refusals.
        fields=(),
        rationale="replayed from a recorded trial",
    )


# --------------------------------------------------------------------------------------
# Contexts
# --------------------------------------------------------------------------------------


def context_from_receipt(payload: Mapping[str, Any]) -> Context:
    """Rebuild the Context a Benchpress trial actually built, from its receipt."""
    request = _mapping(payload.get("request"))
    protected = _mapping(payload.get("protected"))
    return Context(
        trial_id=str(payload.get("trial_id", "replay")),
        user_prompt=str(request.get("prompt", "")),
        providers=tuple(str(item) for item in _list(request.get("providers"))),
        frame=TaskFrame.model_validate(request.get("frame") or {}),
        policies=[PolicyRecord.model_validate(item) for item in _list(payload.get("policies"))],
        candidates=[Candidate.model_validate(item) for item in _list(payload.get("candidates"))],
        targets=[ResolvedTarget.model_validate(item) for item in _list(payload.get("targets"))],
        protected=ProtectedSet(
            ids={str(item) for item in _list(protected.get("ids"))},
            names={str(item) for item in _list(protected.get("names"))},
            domains={str(item) for item in _list(protected.get("domains"))},
            emails={str(item) for item in _list(protected.get("emails"))},
        ),
        dod=DefinitionOfDone.model_validate(payload.get("definition_of_done") or {}),
        plan=Plan(actions=tuple(Action.model_validate(item) for item in _list(payload.get("plan")))),
    )


def context_from_task_spec(task: Any) -> Context:
    """The minimum honest context the judges' own `suite.json` supports.

    `protected_candidate_mutation.selector.reference_any` is the deny-list; the mutation policy's
    `allowed_scope.providers` is the write scope; `structured_result.facts` supply the domains a
    write may legitimately address. Nothing else is assumed, so refusals are a floor.
    """
    return Context(
        trial_id=str(task.task_id),
        user_prompt=str(task.prompt),
        providers=tuple(str(item) for item in task.twins),
        protected=ProtectedSet(names={str(term) for term in task.protected_terms}),
        dod=DefinitionOfDone(
            write_scope=tuple(str(item) for item in task.allowed_providers),
            facts={str(key): str(value) for key, value in dict(task.facts).items()},
        ),
    )


# --------------------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------------------


def replay(
    invocation: Mapping[str, Any],
    context: Context,
    *,
    label: str,
    source: str,
    context_source: str,
    scoped_rules: frozenset[str] | None = None,
) -> ReplayReport:
    gate = Gate(context=context, allow_unplanned=True)
    aliases = role_aliases()
    writes, read_shaped = write_events(invocation)
    calls: list[ReplayCall] = []
    for index, arguments in enumerate(writes, start=1):
        action = action_from_arguments(index, arguments, aliases)
        verdict = gate.evaluate(action)
        allowed = verdict.allowed
        rule, reason = verdict.rule, verdict.reason
        if not allowed and scoped_rules is not None and rule not in scoped_rules:
            allowed = True
            rule, reason = f"not_evaluable:{rule}", "this rule needs context a recording does not carry"
        calls.append(
            ReplayCall(
                index=index,
                provider=action.provider,
                method=action.method,
                path=action.path,
                allowed=allowed,
                rule=rule,
                reason=reason,
            )
        )
    return ReplayReport(
        label=label,
        source=source,
        context_source=context_source,
        writes=len(calls),
        read_shaped_skipped=read_shaped,
        calls=tuple(calls),
    )


def find_receipt(trial_dir: Path) -> Path | None:
    """The Benchpress receipt for the same scenario: the newest sibling arm that has one."""
    explicit = trial_dir / "receipt.json"
    if explicit.exists():
        return explicit
    scenario_dir = trial_dir.parent.parent
    if not scenario_dir.exists():
        return None
    receipts = sorted(scenario_dir.glob("benchpress*/*/receipt.json"))
    return receipts[-1] if receipts else None


def replay_trial_dir(trial_dir: Path, *, receipt: Path | None = None) -> ReplayReport | None:
    invocation_path = trial_dir / "invocation.json"
    if not invocation_path.exists():
        return None
    receipt_path = receipt or find_receipt(trial_dir)
    if receipt_path is None or not receipt_path.exists():
        raise FileNotFoundError(
            f"no Benchpress receipt.json found for {trial_dir}; pass --receipt <path> "
            f"(the gate needs the Context the Benchpress arm built for that scenario)"
        )
    return replay(
        _read(invocation_path),
        context_from_receipt(_read(receipt_path)),
        label=f"{trial_dir.parent.parent.name} / {trial_dir.parent.name} / {trial_dir.name}",
        source=str(trial_dir),
        context_source=str(receipt_path),
    )


def replay_fixture(fixture: Path) -> list[ReplayReport]:
    """Replay the harness's own recorded CRM trials, one report per task."""
    import evals.harness_bridge as harness_bridge

    reports: list[ReplayReport] = []
    with tempfile.TemporaryDirectory(prefix="bp-gate-replay-") as workdir:
        root = Path(workdir)
        with tarfile.open(fixture) as archive:
            _safe_extract(archive, root)
        for invocation_path in sorted(root.glob("tasks/*/invocation.json")):
            task_id = invocation_path.parent.name
            try:
                task = harness_bridge.task_spec(task_id)
            except ValueError:
                continue
            reports.append(
                replay(
                    _read(invocation_path),
                    context_from_task_spec(task),
                    label=task_id,
                    source=f"{fixture.name}:tasks/{task_id}",
                    context_source=f"suite.json:{task_id} (protected_terms, allowed_scope.providers)",
                    scoped_rules=SUITE_SCOPED_RULES,
                )
            )
    return reports


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    """Extract without ever escaping `destination` (the fixture is trusted; the check is not free)."""
    base = destination.resolve()
    for member in archive.getmembers():
        target = (base / member.name).resolve()
        if not str(target).startswith(str(base)):
            raise ValueError(f"refusing to extract {member.name!r} outside {base}")
    archive.extractall(destination, filter="data")  # noqa: S202 - every member was checked above


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def render(reports: Sequence[ReplayReport], *, title: str, preamble: Sequence[str]) -> str:
    total_writes = sum(report.writes for report in reports)
    total_refused = sum(len(report.refused) for report in reports)
    lines: list[str] = [f"# {title}", "", *preamble, "", f"**{CAVEAT}**", ""]
    lines += [
        "| trial | mutating writes | would refuse | first rule | read-shaped POSTs skipped |",
        "|---|---:|---:|---|---:|",
    ]
    for report in reports:
        refused = report.refused
        first = refused[0].rule if refused else "—"
        lines.append(
            f"| `{report.label}` | {report.writes} | {len(refused)} | {first} | {report.read_shaped_skipped} |"
        )
    lines += [
        "",
        f"Totals: **{total_refused} of {total_writes}** replayed mutating writes would have been refused.",
        "",
        "Read-shaped POSTs (`/search`, `/query`, `/batch/read`) are excluded: they change nothing, so",
        "counting them as refused writes would inflate the number. Provider roles are resolved to",
        "provider names through the harness's own `PROVIDER_ROLES` table before scoping is checked.",
        "",
    ]

    for report in reports:
        refused = report.refused
        lines += [f"## {report.label}", "", f"- source: `{report.source}`", f"- context: `{report.context_source}`", ""]
        if not refused:
            lines += ["No replayed write triggers a gate rule.", ""]
            continue
        lines += ["| # | call | rule | reason |", "|---:|---|---|---|"]
        for call in refused:
            lines.append(
                f"| {call.index} | `{call.provider} {call.method} {call.path}` | `{call.rule}` | {call.reason} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _read(path: Path) -> dict[str, Any]:
    data: object = json.loads(path.read_text())
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def _mapping(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def _scalar(value: object) -> str:
    if isinstance(value, list):
        return ",".join(str(item) for item in cast(list[object], value))
    return str(value)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bp_gate_replay.py",
        description="Replay recorded writes through the Benchpress gate.",
    )
    parser.add_argument("trials", nargs="*", help="trial directories holding invocation.json")
    parser.add_argument("--receipt", help="Benchpress receipt.json to build the Context from")
    parser.add_argument("--fixture", help="ArgaBench recorded-trials tarball to replay instead")
    parser.add_argument("--out", default=str(DEFAULT_REPORTS), help="report directory (default: reports/)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(str(args.out))
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.fixture:
        reports = replay_fixture(Path(str(args.fixture)))
        if not reports:
            print("error: the fixture contained no replayable task", file=sys.stderr)
            return 1
        text = render(
            reports,
            title="Gate replay: ArgaBench's own recorded trials",
            preamble=[
                "Every write from ArgaBench's published recording of its own trials, replayed through",
                "the Benchpress gate. The deny-list and the write scope come from the harness's",
                "`suite.json` (`protected_candidate_mutation.selector.reference_any` and",
                "`mutation_policy.allowed_scope.providers`) — the judges' data, not ours. The gate has",
                "none of the context a live Benchpress run builds, so only the rules that data supports",
                "are evaluated (`control_plane`, `method`, `action_class`, `protected`, `provider_scope`);",
                "`field_smuggling` and `external_destination` are reported as *not evaluable* rather than",
                "counted. These refusals are therefore a floor, not a ceiling.",
            ],
        )
        path = out_dir / "gate-replay-historical.md"
    else:
        if not args.trials:
            print("error: pass trial directories, or --fixture <tarball>", file=sys.stderr)
            return 2
        receipt = Path(str(args.receipt)) if args.receipt else None
        reports = list[ReplayReport]()
        for raw in cast(list[str], args.trials):
            report = replay_trial_dir(Path(raw), receipt=receipt)
            if report is not None:
                reports.append(report)
        if not reports:
            print("error: none of those directories holds an invocation.json", file=sys.stderr)
            return 1
        text = render(
            reports,
            title="Gate replay: the stock baseline's writes, gated",
            preamble=[
                "Every write the stock baseline made on the same substrate, replayed through the",
                "Benchpress gate with the Context the Benchpress arm built for that same scenario.",
            ],
        )
        path = out_dir / "gate-replay.md"

    path.write_text(text)
    payload = {"protocol": "benchpress-gate-replay/1", "caveat": CAVEAT, "reports": [r.as_dict() for r in reports]}
    json_path = path.with_suffix(".json")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"wrote:{path}")
    print(f"wrote:{json_path}")
    refused = sum(len(report.refused) for report in reports)
    writes = sum(report.writes for report in reports)
    print(f"would_refuse={refused}/{writes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
