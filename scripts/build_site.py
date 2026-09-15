"""Build the generated parts of the landing page from committed reports.

    uv run python scripts/build_site.py           # refresh site/ from reports/ and docs/img/
    uv run python scripts/build_site.py --check   # exit 1 when site/ is stale (tests/test_site.py runs this)

`site/index.html` is hand-written, except the blocks between `<!-- generated:NAME:start -->` and
`<!-- generated:NAME:end -->`. Those come from `reports/summary.json`, `reports/gate-replay-historical.json`,
`CHANGELOG.md`, `pyproject.toml`, the npm package manifest and the bundled gate corpus, so a number on the page can't
drift from the file it came from. Copied verbatim (never edit the copies): the receipt page, screenshots and terminal
recording from `docs/img/`, the step-through replay from `docs/demo/` (served at `/replay`), and the repo-root
`llms.txt`.

Deploy with `vercel deploy --prod --cwd site` (static files, no build step).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from benchpress.gate_corpus import load_corpus, run_corpus

REPO_ROOT = Path(__file__).resolve().parents[1]
SITE = REPO_ROOT / "site"
REPORTS = REPO_ROOT / "reports"
REPO_URL = "https://github.com/rajkaria/benchpress"

COPIED_ASSETS: dict[str, str] = {
    "docs/img/receipt-sample.html": "receipt.html",
    "docs/img/receipt-hero.png": "img/receipt-hero.png",
    "docs/img/receipt-gate.png": "img/receipt-gate.png",
    "docs/img/demo-terminal.svg": "img/demo-terminal.svg",
    "docs/img/demo-terminal-still.svg": "img/demo-terminal-still.svg",
    "docs/demo/index.html": "replay/index.html",
    "llms.txt": "llms.txt",
}

NPM_MANIFEST = REPO_ROOT / "packages" / "benchpress-guard" / "package.json"

# One entry per CHANGELOG release heading (the heading text before the date). build() fails on a heading with no
# entry, so a release can't land without the page saying what shipped.
SHIPPED: dict[str, tuple[str, str]] = {
    "1.0.0a2": (
        "The gateway",
        "benchpress serve and docker run expose VerifiedWrite over HTTP and MCP: sessions, an approval queue, "
        "SQLite/Postgres storage and a receipts console. 100-call library/HTTP/MCP parity.",
    ),
    "1.0.0a1": (
        "VerifiedWrite + ToolSpec + receipt schema",
        "Pre-release, tagged in git: gate and read back one write, no controller. Python 3.11 floor. Public roadmap.",
    ),
    "0.7.1": (
        "Docs and hygiene",
        "Every doc re-verified against the code; internal planning docs out of the public tree.",
    ),
    "benchpress-guard 0.1.0 on npm": (
        "Vercel AI SDK guard",
        "guardTools(tools, policy) refuses a call before execute runs. Same policy file and receipts as the Python "
        "guards, 80 parity cases.",
    ),
    "0.7.0": (
        "Regress and audit export",
        "regress pins a run's gate decisions as corpus cases; receipts export writes one row per write attempt.",
    ),
    "0.6.1": (
        "Claude transport fixes",
        "Offline contract tests for the Anthropic Messages transport found and fixed five bugs. Not yet run live.",
    ),
    "0.6.0": (
        "GitHub playbook",
        "Issue and pull-request look-alikes, policies from CONTRIBUTING and CODEOWNERS, every update read back.",
    ),
    "0.5.0": (
        "Composio guard and executor",
        "Every Composio tool execution is checked before it runs, and the full loop can drive Composio tools.",
    ),
    "0.4.0": (
        "OpenAI Agents SDK guard",
        "guard_tools returns FunctionTools that refuse before the tool body runs, under every Runner mode.",
    ),
    "0.3.3": ("Gate gap fix", "A bare file name like summary.pdf is no longer mistaken for an external domain."),
    "0.3.2": (
        "Gate gap fixes",
        "Deletes spelled as write routes, and mail marked SENT by a label change, now refused.",
    ),
    "0.3.1": ("Policy packs on the CLI", "benchpress run --policy-pack NAME enforces packs in the gate."),
    "0.3.0": (
        "Policy packs, gate corpus, rehearse",
        "Code-enforced rule sets, a public corpus of gate cases, and rehearse / replay with no model.",
    ),
    "0.2.0": ("MCP executor and mcp-guard", "A stdio MCP proxy that refuses writes your policy doesn't allow."),
    "0.1.1": ("The offline demo", "The whole loop on an in-memory workspace. No keys, no network, a receipt on disk."),
    "0.1.0": ("benchpress.wrap", "The eight-phase loop around any execute_tool-shaped tool layer."),
}

CHANGELOG_HEADING = re.compile(r"^## (.+?) \(\d{4}-\d{2}-\d{2}\)\s*$", re.M)

ARMS: tuple[tuple[str, str], ...] = (("benchpress", "Benchpress"), ("baseline", "Stock baseline"))


@dataclass(frozen=True)
class Substrate:
    key: str
    title: str
    short: str
    note: str


SUBSTRATES: tuple[Substrate, ...] = (
    Substrate(
        "devsim-harness",
        "ArgaBench's unmodified runner and grader, on local twins",
        "ArgaBench's unmodified grader on local twins",
        "Our twins of Slack, Gmail, HubSpot and Stripe, calibrated on ArgaBench's recorded responses. "
        "The runner and the grader are ArgaBench's own code, untouched.",
    ),
    Substrate(
        "devsim",
        "Our line-cited port of the grader, on local twins",
        "our port of ArgaBench's grader on local twins",
        "The same twins, scored by evals/assertions.py. Every assertion cites the grader line it ports.",
    ),
    Substrate(
        "real",
        "Our line-cited port of the grader, on real apps",
        "our port of ArgaBench's grader on real apps",
        "ArgaBench's published seed loaded into a real Slack workspace, Gmail inbox, HubSpot portal "
        "and Stripe test account.",
    ),
)

ASSERTION_LABELS: dict[str, str] = {
    "A1": "Stripe customer carries the new contact",
    "A2": "HubSpot company carries the new contact",
    "A3": "writes stayed on allowed paths",
    "A4": "look-alike records untouched",
    "A5": "exactly one unsent confirmation draft",
    "A6": "owner review request",
    "A7": "no email actually sent",
    "A8": "update in the originating channel",
    "A9": "facts present in two or more systems",
    "A10": "every fact accounted for",
    "A11": "no deletes or duplicates",
    "A12": "no control-plane calls",
    "A13": "no fact-less Slack posts",
    "gmail_draft_cardinality": "exactly one Gmail draft",
    "reviewed_unsent_confirmation": "owner-reviewed unsent confirmation",
    "hubspot_contact_verified": "HubSpot contact verified",
    "stripe_contact_verified": "Stripe contact verified",
}

RULE_EXPLAINERS: dict[str, str] = {
    "protected": "writes that named a record the task said to leave alone",
    "method": "DELETE calls",
    "action_class": "forbidden operations such as sends, merges and charges",
    "provider_scope": "writes to systems outside the task's scope",
    "control_plane": "calls to the benchmark's control plane",
    "external_destination": "writes addressed to a domain the task had never seen",
}

OUTCOME_CHIPS: dict[str, str] = {"pass": "chip-ok", "fail": "chip-warn", "unsafe": "chip-bad"}

PROOF_LINE_OPEN = '<p class="proof-line"><span class="dot"></span>'


@dataclass(frozen=True)
class Trial:
    substrate: str
    arm: str
    label: str
    outcome: str
    failing: tuple[str, ...]
    tool_calls: int | None
    cost_usd: float | None


def _read(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def collect_trials(summary: Mapping[str, Any]) -> list[Trial]:
    trials: list[Trial] = []
    for row in cast(list[dict[str, Any]], summary.get("real_trials") or []):
        trials.append(
            Trial(
                substrate=str(row["substrate"]),
                arm=str(row["arm"]),
                label=str(row.get("trial", "")),
                outcome=str(row.get("outcome") or "unscored"),
                failing=tuple(str(a) for a in cast(list[object], row.get("failing_assertions") or [])),
                tool_calls=_int(row.get("tool_calls")),
                cost_usd=None,
            )
        )
    for row in cast(list[dict[str, Any]], summary.get("twin_trials") or []):
        trials.append(
            Trial(
                substrate=str(row["substrate"]),
                arm=str(row["arm"]),
                label=str(row.get("repeat", "")),
                outcome=str(row.get("outcome") or "unscored"),
                failing=tuple(str(a) for a in cast(list[object], row.get("failing_assertions") or [])),
                tool_calls=_int(row.get("provider_tool_calls")),
                cost_usd=_float(row.get("estimated_cost_usd")),
            )
        )
    return trials


def humanize(assertion_id: str) -> str:
    key = assertion_id.split(":", 1)[0]
    return ASSERTION_LABELS.get(key, key.replace("_", " "))


def _substrates_in(trials: Sequence[Trial]) -> list[Substrate]:
    known = [s for s in SUBSTRATES if any(t.substrate == s.key for t in trials)]
    extra = sorted({t.substrate for t in trials} - {s.key for s in SUBSTRATES})
    return known + [Substrate(key, key, key, "") for key in extra]


def _runs(n: int) -> str:
    return "run" if n == 1 else "runs"


def _misses(trials: Sequence[Trial]) -> list[str]:
    counts = Counter(humanize(a) for t in trials for a in t.failing)
    return [name for name, _ in counts.most_common()]


def _cost(trials: Sequence[Trial]) -> str:
    costs = [t.cost_usd for t in trials if t.cost_usd is not None]
    if not costs:
        return ""
    low, high = f"{min(costs):.2f}", f"{max(costs):.2f}"
    return f"${low} per run" if low == high else f"${low}–{high} per run"


def render_arm(trials: Sequence[Trial], label: str, *, highlight: bool) -> str:
    css = "arm arm-bp" if highlight else "arm"
    if not trials:
        return (
            f'<div class="{css} arm-empty"><span class="arm-name">{html.escape(label)}</span>'
            '<span class="arm-score">–</span><span class="arm-sub">not run yet</span></div>'
        )
    passed = sum(t.outcome == "pass" for t in trials)
    unsafe = sum(t.outcome == "unsafe" for t in trials)
    misses = _misses(trials)
    detail = "Nothing missed." if not misses else "Missed: " + ", ".join(misses) + "."
    unsafe_chip = f' <span class="chip chip-bad">{unsafe} unsafe</span>' if unsafe else ""
    cost = _cost(trials)
    cost_html = f'<span class="arm-cost">{html.escape(cost)}</span>' if cost else ""
    return (
        f'<div class="{css}"><span class="arm-name">{html.escape(label)}</span>'
        f'<span class="arm-score">{passed}<span class="arm-of">/{len(trials)}</span></span>'
        f'<span class="arm-sub">{_runs(len(trials))} passed{unsafe_chip}</span>'
        f'<p class="arm-detail">{html.escape(detail)}</p>{cost_html}</div>'
    )


def render_scoreboard(trials: Sequence[Trial]) -> str:
    blocks: list[str] = []
    for substrate in _substrates_in(trials):
        group = [t for t in trials if t.substrate == substrate.key]
        arms = "".join(
            render_arm([t for t in group if t.arm == key], label, highlight=key == "benchpress") for key, label in ARMS
        )
        blocks.append(
            '<article class="score">'
            f'<header class="score-head"><span class="tag">{html.escape(substrate.key)}</span>'
            f"<h3>{html.escape(substrate.title)}</h3><p>{html.escape(substrate.note)}</p></header>"
            f'<div class="score-arms">{arms}</div></article>'
        )
    return "\n".join(blocks) if blocks else '<p class="fine">No scored trials yet.</p>'


def render_hero_proof(trials: Sequence[Trial]) -> str:
    for substrate in _substrates_in(trials):
        bench = [t for t in trials if t.substrate == substrate.key and t.arm == "benchpress"]
        base = [t for t in trials if t.substrate == substrate.key and t.arm == "baseline"]
        if bench and base:
            bench_pass = sum(t.outcome == "pass" for t in bench)
            base_pass = sum(t.outcome == "pass" for t in base)
            text = (
                f"Under {substrate.short}: Benchpress passed {bench_pass} of {len(bench)} {_runs(len(bench))}, "
                f"the stock baseline {base_pass} of {len(base)}."
            )
            return f'{PROOF_LINE_OPEN}{html.escape(text)} <a href="#proof">See every run</a></p>'
    return f'{PROOF_LINE_OPEN}Scored trials are in progress. <a href="#proof">See the proof</a></p>'


def _replay_totals(replay: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int, int, int, Counter[str]]:
    reports = cast(list[dict[str, Any]], replay.get("reports") or [])
    writes = sum(int(r.get("writes", 0)) for r in reports)
    refused = sum(int(r.get("refused", 0)) for r in reports)
    with_refusal = sum(int(r.get("refused", 0)) > 0 for r in reports)
    rules = Counter(
        str(c.get("rule"))
        for r in reports
        for c in cast(list[dict[str, Any]], r.get("calls") or [])
        if not c.get("allowed", True)
    )
    return reports, writes, refused, with_refusal, rules


def render_gate_replay(replay: Mapping[str, Any]) -> str:
    reports, writes, refused, with_refusal, rules = _replay_totals(replay)
    if not reports:
        return '<p class="fine">No gate replay committed yet.</p>'
    span = f"{reports[0].get('label')} to {reports[-1].get('label')}"
    reason = ""
    if rules:
        rule = rules.most_common(1)[0][0]
        explainer = RULE_EXPLAINERS.get(rule, "")
        reason = f" The most common reason was <code>{html.escape(rule)}</code>" + (
            f": {html.escape(explainer)}." if explainer else "."
        )
    return (
        '<article class="replay">'
        f'<div class="replay-num"><span class="arm-score">{refused}<span class="arm-of"> of {writes}</span></span>'
        '<span class="arm-sub">writes refused</span></div>'
        '<div class="replay-body"><h3>Replaying ArgaBench\'s own recorded trials through the gate</h3>'
        f"<p>We took every mutating call from a published frontier-model recording ({html.escape(span)}) and ran "
        "it through the gate, using the protected terms from the benchmark's own suite file. "
        f"{with_refusal} of {len(reports)} trials had at least one write the gate would have refused.{reason}</p>"
        '<p class="fine">A refusal isn\'t a pass. It means that call would never have left the process, and the '
        "rest of the recording no longer applies. No model was involved, so this cost nothing to run.</p>"
        "</div></article>"
    )


def _substrate_title(key: str) -> str:
    return next((s.short for s in SUBSTRATES if s.key == key), key)


def render_trials_table(trials: Sequence[Trial]) -> str:
    rows: list[str] = []
    for t in trials:
        arm = dict(ARMS).get(t.arm, t.arm)
        failing = ", ".join(humanize(a) for a in t.failing) or "–"
        calls = str(t.tool_calls) if t.tool_calls is not None else "–"
        cost = f"${t.cost_usd:.2f}" if t.cost_usd is not None else "–"
        chip = OUTCOME_CHIPS.get(t.outcome, "")
        rows.append(
            f"<tr><td>{html.escape(_substrate_title(t.substrate))}</td><td>{html.escape(arm)}</td>"
            f'<td class="mono">{html.escape(t.label)}</td>'
            f'<td><span class="chip {chip}">{html.escape(t.outcome)}</span></td>'
            f'<td>{html.escape(failing)}</td><td class="n">{calls}</td><td class="n">{cost}</td></tr>'
        )
    return (
        "<table><thead><tr><th>Substrate</th><th>Arm</th><th>Trial</th><th>Outcome</th><th>What it missed</th>"
        '<th class="n">Provider calls</th><th class="n">Model cost</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_llms_results(trials: Sequence[Trial], replay: Mapping[str, Any]) -> str:
    lines = ["Scored trials (generated from reports/summary.json; outcome precedence unsafe > fail > pass):"]
    for substrate in _substrates_in(trials):
        parts: list[str] = []
        for key, label in ARMS:
            arm = [t for t in trials if t.substrate == substrate.key and t.arm == key]
            if not arm:
                parts.append(f"{label} not run yet")
                continue
            passed = sum(t.outcome == "pass" for t in arm)
            misses = _misses(arm)
            missed = f" (missed: {', '.join(misses)})" if misses else ""
            parts.append(f"{label} {passed}/{len(arm)} pass{missed}")
        lines.append(f"- {substrate.title}: " + "; ".join(parts) + ".")
    reports, writes, refused, with_refusal, _ = _replay_totals(replay)
    if reports:
        lines.append(
            f"- Gate replay over ArgaBench's own recorded trials (reports/gate-replay-historical.json): {refused} of "
            f"{writes} mutating writes would have been refused; {with_refusal} of {len(reports)} trials had at least "
            "one. A refusal is a blocked call, not a counterfactual pass."
        )
    return "\n".join(lines)


def package_version() -> str:
    """The version in pyproject.toml, so the page's badge can't lag a release."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.M)
    if match is None:
        raise ValueError("pyproject.toml has no version")
    return match.group(1)


_STABLE_VERSION = re.compile(r"^\d+(?:\.\d+)*$")


def pypi_stable_version() -> str:
    """The newest stable release in CHANGELOG.md. The PyPI chip shows what `pip install` resolves to, and a
    pre-release (`1.0.0a1`) is tagged in git before it is uploaded, so it never claims PyPI availability."""
    for release in changelog_releases():
        if _STABLE_VERSION.match(release):
            return release
    raise ValueError("CHANGELOG.md has no stable release heading")


def changelog_releases() -> list[str]:
    """Release headings in CHANGELOG.md, newest first, without the date."""
    return CHANGELOG_HEADING.findall((REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))


def render_shipped(releases: Sequence[str]) -> str:
    missing = [r for r in releases if r not in SHIPPED]
    if missing:
        raise ValueError(f"CHANGELOG releases with no SHIPPED entry in scripts/build_site.py: {missing}")
    tiles: list[str] = []
    for release in releases:
        title, line = SHIPPED[release]
        tag = release.replace("benchpress-guard ", "npm ").replace(" on npm", "")
        tiles.append(
            f'<li class="ship"><span class="ship-v">{html.escape(tag)}</span>'
            f"<h3>{html.escape(title)}</h3><p>{html.escape(line)}</p></li>"
        )
    return f'<ul class="ships">{"".join(tiles)}</ul>'


def npm_version() -> str:
    """The benchpress-guard version in its package.json."""
    data = cast(dict[str, Any], json.loads(NPM_MANIFEST.read_text(encoding="utf-8")))
    return str(data["version"])


def corpus_summary() -> str:
    """Run the bundled gate corpus, as `benchpress gate check` does, and state the count."""
    results = run_corpus(load_corpus())
    passed = sum(r.status == "pass" for r in results)
    tail = "all passing" if passed == len(results) else f"{passed} passing"
    return f"<b>{len(results)}</b> cases, {tail}"


def replace_block(text: str, name: str, content: str) -> str:
    marker = re.escape(name)
    pattern = re.compile(rf"(<!-- generated:{marker}:start -->)(.*?)(<!-- generated:{marker}:end -->)", re.S)
    if not pattern.search(text):
        raise ValueError(f"missing generated block {name!r}")
    return pattern.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(3)}", text, count=1)


def build(check: bool = False) -> list[str]:
    """Refresh site/ (or, with check=True, only report what is stale). Returns stale paths relative to the repo."""
    summary = _read(REPORTS / "summary.json")
    replay = _read(REPORTS / "gate-replay-historical.json")
    trials = collect_trials(summary)
    stale: list[str] = []
    targets: dict[Path, dict[str, str]] = {
        SITE / "index.html": {
            "hero-proof": render_hero_proof(trials),
            "scoreboard": render_scoreboard(trials),
            "gate-replay": render_gate_replay(replay),
            "trials": render_trials_table(trials),
            "version": f"<b>{html.escape(package_version())}</b>",
            "pypi-version": html.escape(pypi_stable_version()),
            "npm-version": html.escape(npm_version()),
            "shipped": render_shipped(changelog_releases()),
            "corpus": corpus_summary(),
        },
    }
    for target, blocks in targets.items():
        current = target.read_text(encoding="utf-8")
        updated = current
        for name, content in blocks.items():
            updated = replace_block(updated, name, content)
        if updated != current:
            stale.append(str(target.relative_to(REPO_ROOT)))
            if not check:
                target.write_text(updated, encoding="utf-8")
    for source, destination in COPIED_ASSETS.items():
        src, dst = REPO_ROOT / source, SITE / destination
        if not dst.is_file() or dst.read_bytes() != src.read_bytes():
            stale.append(str(dst.relative_to(REPO_ROOT)))
            if not check:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
    return stale


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 instead of writing when site/ is stale")
    args = parser.parse_args(argv)
    stale = build(check=bool(args.check))
    if args.check:
        if stale:
            print("stale: " + ", ".join(stale))
            return 1
        print("site/ is in sync with reports/")
        return 0
    print("updated: " + ", ".join(stale) if stale else "site/ already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
