"""The gate-rule corpus: every bundled case, the loader's contract, and `benchpress gate check`."""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

from benchpress.cli import main
from benchpress.gate_corpus import (
    CORPUS_DIR,
    GATE_RULES,
    CorpusError,
    GateCase,
    expand_body,
    load_corpus,
    run_case,
)

CASES = load_corpus()


def _param(case: GateCase) -> object:
    marks = [pytest.mark.xfail(strict=True, reason=case.xfail)] if case.xfail else []
    return pytest.param(case, id=case.name, marks=marks)


@pytest.mark.parametrize("case", [_param(case) for case in CASES])
def test_corpus_case(case: GateCase) -> None:
    result = run_case(case)
    assert result.matched, (
        f"{case.name} ({case.source}): expected {case.expect.label}, got {result.actual}: {result.verdict.reason}"
    )


def test_corpus_is_broad() -> None:
    assert len(CASES) >= 40
    providers = {case.action.provider for case in CASES}
    assert {"slack", "gmail", "hubspot", "stripe"} <= providers
    refused_rules = {case.expect.rule for case in CASES if case.expect.decision == "refuse" and not case.xfail}
    assert set(GATE_RULES) <= refused_rules, f"rules without a refusal case: {set(GATE_RULES) - refused_rules}"
    assert any(case.expect.decision == "allow" and not case.xfail for case in CASES)


def test_corpus_is_task_agnostic() -> None:
    banned = re.compile(r"(ECOM|CRM|DEV|IT|MKT)-0[0-9]|northwind|alder credit|checkout_tax|kira long|trent bell", re.I)
    for path in CORPUS_DIR.glob("*.yaml"):
        assert not banned.search(path.read_text()), f"task-specific reference in {path.name}"


def test_base64url_expansion() -> None:
    expanded = expand_body({"message": {"raw": {"$base64url": "To: a@corp.example"}}, "n": [1, {"k": "v"}]})
    assert isinstance(expanded, dict)
    raw = expanded["message"]["raw"]  # type: ignore[index]
    assert isinstance(raw, str) and "=" not in raw
    assert base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode() == "To: a@corp.example"
    with pytest.raises(CorpusError):
        expand_body({"$base64url": 3})


def _write(tmp_path: Path, text: str, name: str = "cases.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


_ALLOW_READ = """
cases:
  - name: {name}
    action: {{kind: read, provider: hubspot, method: GET, path: /crm/v3/objects/companies/1}}
    expect: {expect}
"""


def test_loader_rejects_duplicate_names(tmp_path: Path) -> None:
    _write(tmp_path, _ALLOW_READ.format(name="dup", expect="{decision: allow}"), "a.yaml")
    _write(tmp_path, _ALLOW_READ.format(name="dup", expect="{decision: allow}"), "b.yaml")
    with pytest.raises(CorpusError, match="duplicate"):
        load_corpus([tmp_path])


@pytest.mark.parametrize(
    "expect",
    [
        "{decision: refuse}",
        "{decision: refuse, rule: not_a_rule}",
        "{decision: allow, rule: method}",
        "{decision: maybe}",
    ],
)
def test_loader_rejects_bad_expectations(tmp_path: Path, expect: str) -> None:
    path = _write(tmp_path, _ALLOW_READ.format(name="bad", expect=expect))
    with pytest.raises(CorpusError):
        load_corpus([path])


def test_loader_rejects_unknown_fields_and_classes(tmp_path: Path) -> None:
    unknown_field = _write(tmp_path, _ALLOW_READ.format(name="x", expect="{decision: allow, colour: red}"), "u.yaml")
    with pytest.raises(CorpusError):
        load_corpus([unknown_field])
    bad_class = _write(
        tmp_path,
        """
cases:
  - name: y
    context: {forbidden: [launch_rockets]}
    action: {provider: hubspot, method: POST, path: /x}
    expect: {decision: allow}
""",
        "c.yaml",
    )
    with pytest.raises(CorpusError, match="launch_rockets"):
        load_corpus([bad_class])


def test_missing_path_is_an_error() -> None:
    with pytest.raises(CorpusError):
        load_corpus(["/nonexistent/corpus.yaml"])


def test_cli_gate_check_bundled_corpus_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gate", "check"]) == 0
    out = capsys.readouterr().out
    assert "STATUS" in out and f"{len(CASES)} case(s)" in out and "0 failed" in out


def test_cli_gate_check_reports_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, _ALLOW_READ.format(name="wrong", expect="{decision: refuse, rule: method}"))
    assert main(["gate", "check", str(path)]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "1 failed" in out


def test_cli_gate_check_strict_xpass_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = _ALLOW_READ.format(name="fixed", expect="{decision: allow}") + "    xfail: believed wrong\n"
    assert main(["gate", "check", str(_write(tmp_path, text))]) == 1
    assert "1 xpass" in capsys.readouterr().out


def test_cli_gate_check_bad_corpus_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gate", "check", str(_write(tmp_path, "cases: [ {name: 1"))]) == 2
    assert "gate check" in capsys.readouterr().err
