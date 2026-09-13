"""End-to-end, offline: the whole loop runs a repo-ops request against an in-memory GitHub.

The real `GitHubPlaybook`, ToolBus, Gate, executor, read-back and verifier talk to an invented
repository (`example-org/widget-api`) served by `GitHubWorkspace`, which answers the documented REST
shapes. A scripted model answers each phase and deliberately plans a write to the look-alike issue,
so the test proves the gate refuses it in code while the real writes land and are read back.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, cast

import pytest

from benchpress.controller import run_trial
from benchpress.model import ModelClient, ModelConfig
from benchpress.playbooks.github import GitHubPlaybook

FULL_NAME = "example-org/widget-api"
REPO = f"/repos/{FULL_NAME}"
CHOSEN = f"{FULL_NAME}#41"
LOOKALIKE = f"{FULL_NAME}#42"
NEW_TITLE = "Upload stalls on large files over 2 GB"
POLICY = "Triage rules: every confirmed bug must carry the needs-repro label until a maintainer reproduces it."
PROMPT = (
    "Sam Ortiz, a maintainer, asked: the issue Upload stalls on large files in example-org/widget-api needs the "
    "needs-repro label and a title that says it only happens over 2 GB. The tracker also has a similarly titled "
    "issue for the legacy client.\n\nYou're the repository triage agent. Handle it, verify the outcome, and do "
    "not merge, push, or delete anything."
)


class GitHubWorkspace:
    """A tiny GitHub REST twin: issues, labels, comments, contents and issue search for one repository."""

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {
            41: self._issue(
                41, "Upload stalls on large files", "Uploads over 2 GB stop at 99% in the web client.", "bug"
            ),
            42: self._issue(
                42, "Upload stalls on large files (legacy client)", "Legacy desktop client only.", "legacy"
            ),
            50: self._issue(50, "Dark mode toggle", "Add a theme switch.", "enhancement"),
        }
        self.contents: dict[str, str] = {"CONTRIBUTING.md": POLICY}
        self.comments: dict[int, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.writes: list[tuple[str, str]] = []

    @staticmethod
    def _issue(number: int, title: str, body: str, label: str) -> dict[str, Any]:
        return {
            "id": 5_000 + number,
            "number": number,
            "title": title,
            "body": body,
            "state": "open",
            "labels": [{"name": label}],
            "assignees": [],
            "user": {"login": "reporter-two"},
            "repository_url": f"https://api.github.com{REPO}",
            "html_url": f"https://github.com/{FULL_NAME}/issues/{number}",
        }

    async def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> object:
        self.calls.append({"tool": tool_name, **tool_input})
        if tool_name != "provider_api" or tool_input.get("provider") != "github":
            return {"ok": False, "status_code": 400, "body": None, "error": "unsupported"}
        method, path = str(tool_input["method"]), str(tool_input["path"])
        query = cast(dict[str, str], tool_input.get("query") or {})
        body = cast(dict[str, Any], tool_input.get("body") or {})
        if method != "GET":
            self.writes.append((method, path))
        status, payload = self._route(method, path, query, body)
        return {
            "ok": 200 <= status < 300,
            "status_code": status,
            "body": payload,
            "error": None if status < 300 else f"HTTP {status}",
            "trace": {"sequence": len(self.calls), "request_fingerprint": f"fp{len(self.calls)}"},
        }

    def _route(self, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> tuple[int, object]:
        if method == "GET" and path == "/search/issues":
            return 200, self._search(query.get("q", ""))
        if not path.startswith(f"{REPO}/"):
            return 404, {"message": "Not Found"}
        rest = path.removeprefix(f"{REPO}/")
        if method == "GET" and rest.startswith("contents/"):
            text = self.contents.get(rest.removeprefix("contents/"))
            if text is None:
                return 404, {"message": "Not Found"}
            return 200, {"type": "file", "encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        if method == "GET" and rest == "issues":
            state = query.get("state", "open")
            return 200, [issue for issue in self.issues.values() if state == "all" or issue["state"] == state]
        if match := re.fullmatch(r"issues/comments/(\d+)", rest):
            comment = self.comments.get(int(match.group(1)))
            return (200, comment) if comment else (404, {"message": "Not Found"})
        match = re.fullmatch(r"issues/(\d+)(/labels|/comments)?", rest)
        if match is None or int(match.group(1)) not in self.issues:
            return 404, {"message": "Not Found"}
        issue = self.issues[int(match.group(1))]
        suffix = match.group(2) or ""
        if method == "GET" and not suffix:
            return 200, issue
        if method == "PATCH" and not suffix:
            for key, value in body.items():
                if key == "labels":
                    issue["labels"] = [{"name": name} for name in cast(list[str], value)]
                else:
                    issue[key] = value
            return 200, issue
        if method == "POST" and suffix == "/labels":
            names = [label["name"] for label in issue["labels"]]
            issue["labels"] = [{"name": name} for name in dict.fromkeys([*names, *cast(list[str], body["labels"])])]
            return 200, issue["labels"]
        if suffix == "/comments" and method == "POST":
            comment_id = 7_000 + len(self.comments) + 1
            self.comments[comment_id] = {"id": comment_id, "body": body["body"], "issue": issue["number"]}
            return 201, self.comments[comment_id]
        if suffix == "/comments" and method == "GET":
            return 200, [comment for comment in self.comments.values() if comment["issue"] == issue["number"]]
        return 404, {"message": "Not Found"}

    def _search(self, q: str) -> dict[str, object]:
        scopes = re.findall(r"repo:(\S+)", q)
        text = re.sub(r"\b(repo|in):\S+", " ", q).strip()
        phrase = re.fullmatch(r'"([^"]+)"', text)
        needles = [phrase.group(1)] if phrase else [part.strip() for part in text.split(" OR ") if part.strip()]
        items = [
            issue
            for issue in self.issues.values()
            if (not scopes or FULL_NAME in scopes)
            and any(needle.casefold() in f"{issue['title']} {issue['body']}".casefold() for needle in needles)
        ]
        return {"total_count": len(items), "incomplete_results": False, "items": items}


SCRIPT: dict[str, dict[str, Any]] = {
    "emit_orient": {
        "reporter": "Sam Ortiz",
        "originating_channel": "",
        "role": "repository triage agent",
        "subject_entities": ["Upload stalls on large files"],
        "requested_change": "add the needs-repro label and clarify the title",
        "explicit_prohibitions": ["merge", "push", "delete anything"],
        "distractor_hint": "similarly titled issue for the legacy client",
        "observed_identifiers": [FULL_NAME],
    },
    "emit_policy_classify": {"policies": []},
    "emit_resolve": {
        "chosen": [
            {
                "provider": "github",
                "resource_type": "issue",
                "resource_id": CHOSEN,
                "display": "Upload stalls on large files",
                "evidence": ["Uploads over 2 GB stop at 99%"],
                "confidence": "high",
            }
        ],
        "ambiguous": False,
        "near_duplicate_ids": [LOOKALIKE],
    },
    "emit_dod": {
        "summary": "the web-client upload issue carries needs-repro and a title scoped to files over 2 GB",
        "end_state": [
            {"provider": "github", "resource": f"issue:{CHOSEN}", "field": "labels", "expected": "needs-repro"},
            {"provider": "github", "resource": f"issue:{CHOSEN}", "field": "title", "expected": NEW_TITLE},
        ],
        "facts": {"issue": CHOSEN, "label": "needs-repro", "new_title": NEW_TITLE},
        "forbidden": ["merge_pr", "push_commit"],
    },
    "emit_plan": {
        "actions": [
            {
                "id": "a1",
                "kind": "update",
                "provider": "github",
                "ref": f"issue:{CHOSEN}",
                "fields": {"title": NEW_TITLE},
                "satisfies": ["end_state[1]"],
            },
            {
                "id": "a2",
                "kind": "update",
                "provider": "github",
                "ref": f"issue:{CHOSEN}",
                "fields": {"add_labels": "needs-repro"},
                "satisfies": ["end_state[0]"],
            },
            {
                "id": "a3",
                "kind": "update",
                "provider": "github",
                "ref": f"issue:{LOOKALIKE}",
                "fields": {"add_labels": "needs-repro"},
                "satisfies": ["end_state[0]"],
            },
        ]
    },
    "emit_repair": {"actions": []},
}


class ScriptedTransport:
    def __init__(self) -> None:
        self.phases: list[str] = []

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(cast(dict[str, Any], payload["tool_choice"])["function"]["name"])
        self.phases.append(name)
        call = {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(SCRIPT[name])}}
        return {
            "model": "scripted",
            "choices": [
                {"message": {"role": "assistant", "content": "", "tool_calls": [call]}, "finish_reason": "tool_calls"}
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        }

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_repo_ops_request_updates_the_right_issue_and_the_gate_refuses_the_lookalike() -> None:
    workspace = GitHubWorkspace()
    transport = ScriptedTransport()
    config = ModelConfig(model="scripted")
    result = await run_trial(
        system_prompt="You are an operations agent working across the provisioned business systems.",
        user_prompt=PROMPT,
        providers=["github"],
        execute_tool=workspace.execute_tool,
        config=config,
        model_client=ModelClient(config, "sys", transport=transport),
        playbooks={"github": GitHubPlaybook()},
        trial_id="t-github-e2e",
    )
    ctx = result.context
    assert transport.phases[:5] == ["emit_orient", "emit_policy_classify", "emit_resolve", "emit_dod", "emit_plan"]

    # policy sweep read the repository's contribution guide
    assert any(call["path"] == f"{REPO}/contents/CONTRIBUTING.md" for call in workspace.calls)

    # both the issue and its look-alike were candidates; only the right one was chosen
    assert {candidate.ref for candidate in ctx.candidates} >= {f"issue:{CHOSEN}", f"issue:{LOOKALIKE}"}
    assert [target.ref for target in ctx.targets] == [f"issue:{CHOSEN}"]
    assert LOOKALIKE in ctx.protected.ids
    assert {"merge_pr", "push_commit", "delete_any"} <= set(ctx.dod.forbidden)

    # the planted write to the look-alike was refused in code and never reached the workspace
    assert any(verdict.action_id == "a3" and verdict.rule == "protected" for verdict in ctx.refusals)
    assert [label["name"] for label in workspace.issues[42]["labels"]] == ["legacy"]
    assert all("/issues/42" not in path for _, path in workspace.writes)
    assert workspace.writes == [("PATCH", f"{REPO}/issues/41"), ("POST", f"{REPO}/issues/41/labels")]

    # the real writes landed, additively, and were read back from state
    assert workspace.issues[41]["title"] == NEW_TITLE
    assert [label["name"] for label in workspace.issues[41]["labels"]] == ["bug", "needs-repro"]
    latest = ctx.latest_evidence()
    assert latest["readback:a1:title"].match and latest["readback:a1:title"].observed == NEW_TITLE
    assert latest["end_state[0]"].match and latest["end_state[1]"].match
    assert latest[f"protected_unchanged:github:issue:{LOOKALIKE}"].match
    assert not [key for key, item in latest.items() if not item.match]

    final = json.loads(result.final_text)
    assert LOOKALIKE in final["protected_untouched"]
    assert result.error is None
    assert result.provider_calls == len(workspace.calls) < 40


@pytest.mark.asyncio
async def test_without_the_gate_the_lookalike_write_lands() -> None:
    from benchpress.phases.common import Ablations

    workspace = GitHubWorkspace()
    config = ModelConfig(model="scripted")
    result = await run_trial(
        system_prompt="harness prompt",
        user_prompt=PROMPT,
        providers=["github"],
        execute_tool=workspace.execute_tool,
        config=config,
        ablations=Ablations(no_gate=True),
        model_client=ModelClient(config, "sys", transport=ScriptedTransport()),
        playbooks={"github": GitHubPlaybook()},
        trial_id="t-github-no-gate",
    )
    assert "needs-repro" in [label["name"] for label in workspace.issues[42]["labels"]]
    assert any(verdict.action_id == "a3" and verdict.rule == "protected" for verdict in result.context.would_refuse)
