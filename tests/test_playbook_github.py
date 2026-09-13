"""GitHub playbook: REST request shapes, candidate parsing, write constructors, gate interplay.

Every call goes through the fake harness gateway from `tests.test_playbooks`. Repositories, people
and issues are invented (`example-org/widget-api`); nothing here refers to a benchmark task.
"""

from __future__ import annotations

import base64
import inspect
from typing import Any, cast

import pytest

from benchpress.context import Candidate, Context, DefinitionOfDone, ResolvedTarget, TaskFrame
from benchpress.gate import Gate, classify, compound_id_in_path
from benchpress.playbooks import fill_placeholders, for_provider, has_placeholders
from benchpress.playbooks import github as github_module
from benchpress.playbooks.github import (
    SEARCH_ISSUES_PATH,
    GitHubPlaybook,
    NumberRef,
    RepoRef,
    body_value,
    decode_content,
    issue_candidate,
    number_refs_in,
    parse_number_id,
    repo_refs_in,
)
from tests.test_playbooks import Route, make_bus

REPO = "/repos/example-org/widget-api"
github = GitHubPlaybook()


def issue(
    number: int, title: str, *, body: str = "", labels: tuple[str, ...] = (), pull: bool = False
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": 900_000 + number,
        "number": number,
        "title": title,
        "body": body,
        "state": "open",
        "labels": [{"name": label} for label in labels],
        "assignees": [{"login": "maintainer-one"}],
        "user": {"login": "reporter-two"},
        "milestone": {"title": "2.1"},
        "repository_url": f"https://api.github.com{REPO}",
        "html_url": f"https://github.com/example-org/widget-api/{'pull' if pull else 'issues'}/{number}",
    }
    if pull:
        record["pull_request"] = {"url": f"https://api.github.com{REPO}/pulls/{number}"}
    return record


def b64(text: str) -> str:
    encoded = base64.b64encode(text.encode()).decode()
    return "\n".join(encoded[index : index + 60] for index in range(0, len(encoded), 60))


# --------------------------------------------------------------------------------------
# Registry and pure helpers
# --------------------------------------------------------------------------------------


def test_registered_by_name_and_role() -> None:
    assert isinstance(for_provider("github"), GitHubPlaybook)
    assert isinstance(for_provider("code_host"), GitHubPlaybook)


def test_repo_and_number_refs_are_parsed_from_text_and_urls() -> None:
    values = [
        "example-org/widget-api#41",
        "see https://github.com/example-org/widget-cli/issues/7 and example-org/widget-api",
        "a@b.example/x is not a repo",
    ]
    repos = repo_refs_in(values)
    assert [repo.full_name for repo in repos] == ["example-org/widget-api", "example-org/widget-cli"]
    numbers = number_refs_in(values, repos)
    assert [ref.compound for ref in numbers] == ["example-org/widget-api#41", "example-org/widget-cli#7"]
    only = [RepoRef("example-org", "widget-api")]
    assert [ref.compound for ref in number_refs_in(["please look at #12"], only)] == ["example-org/widget-api#12"]
    assert number_refs_in(["please look at #12"], []) == []
    assert parse_number_id("example-org/widget-api#41") == NumberRef(RepoRef("example-org", "widget-api"), 41)
    assert parse_number_id("widget-api#41") is None and parse_number_id("example-org/widget-api") is None


def test_body_values_are_validated() -> None:
    assert body_value("labels", "bug, needs-repro ,") == ["bug", "needs-repro"]
    assert body_value("state", "Closed") == "closed"
    assert body_value("state", "merged") is None
    assert body_value("state_reason", "not_planned") == "not_planned"
    assert body_value("milestone", "3") == 3 and body_value("milestone", "v3") is None
    assert body_value("title", "New title") == "New title"


def test_decode_content_handles_wrapped_base64_and_non_files() -> None:
    assert decode_content({"type": "file", "encoding": "base64", "content": b64("Releases need approval.")}) == (
        "Releases need approval."
    )
    assert decode_content({"type": "dir"}) == ""
    assert decode_content({"type": "file", "encoding": "base64", "content": ""}) == ""


def test_issue_candidate_shape() -> None:
    candidate = issue_candidate(issue(41, "Upload stalls on large files", body="Seen on 2 GB files", labels=("bug",)))
    assert candidate is not None
    assert candidate.ref == "issue:example-org/widget-api#41"
    assert candidate.display == "Upload stalls on large files" and candidate.lifecycle == "open"
    assert "repository=example-org/widget-api" in candidate.notes and "labels=bug" in candidate.notes
    assert "Seen on 2 GB files" in candidate.notes
    pull = issue_candidate(issue(43, "Fix upload stall", pull=True))
    assert pull is not None and pull.ref == "pull_request:example-org/widget-api#43"
    assert issue_candidate({"title": "no number"}) is None


# --------------------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_candidates_searches_issues_scoped_to_named_repo() -> None:
    items = [
        issue(41, "Upload stalls on large files", labels=("bug",)),
        issue(42, "Upload stalls on large files (legacy client)"),
        issue(43, "Fix upload stall on large files", pull=True),
    ]
    bus, gateway = make_bus(Route("GET", SEARCH_ISSUES_PATH, {"total_count": 3, "items": items}))
    candidates = await github.find_candidates(bus, "Upload stalls on large files", ["example-org/widget-api"])
    refs = [candidate.ref for candidate in candidates]
    assert refs == [
        "issue:example-org/widget-api#41",
        "issue:example-org/widget-api#42",
        "pull_request:example-org/widget-api#43",
    ]
    queries = [cast(dict[str, str], call["query"])["q"] for call in gateway.to(SEARCH_ISSUES_PATH, "GET")]
    assert queries[0] == '"Upload stalls on large files" in:title,body repo:example-org/widget-api'
    assert len(queries) <= 2 and all("repo:example-org/widget-api" in query for query in queries)
    assert all(call["method"] == "GET" for call in gateway.calls)


@pytest.mark.asyncio
async def test_find_candidates_falls_back_to_listing_when_search_is_unavailable() -> None:
    listed = [issue(41, "Upload stalls on large files"), issue(50, "Dark mode toggle")]
    bus, gateway = make_bus(
        Route("GET", SEARCH_ISSUES_PATH, {"message": "not found"}, status=404),
        Route("GET", f"{REPO}/issues", listed),
    )
    candidates = await github.find_candidates(bus, "Upload stalls", ["example-org/widget-api"])
    assert [candidate.ref for candidate in candidates] == ["issue:example-org/widget-api#41"]
    listing = gateway.to(f"{REPO}/issues", "GET")
    assert listing and cast(dict[str, str], listing[0]["query"])["state"] == "all"


@pytest.mark.asyncio
async def test_number_hints_are_read_directly() -> None:
    bus, gateway = make_bus(
        Route("GET", f"{REPO}/issues/41", issue(41, "Upload stalls on large files")),
        Route("GET", SEARCH_ISSUES_PATH, {"items": []}),
    )
    candidates = await github.find_candidates(bus, "example-org/widget-api#41", [])
    assert [candidate.ref for candidate in candidates] == ["issue:example-org/widget-api#41"]
    assert gateway.to(f"{REPO}/issues/41", "GET")


@pytest.mark.asyncio
async def test_repositories_are_candidates_only_when_no_issue_matched() -> None:
    repo = {"full_name": "example-org/widget-api", "description": "Widget HTTP API", "default_branch": "main"}
    bus, _ = make_bus(Route("GET", SEARCH_ISSUES_PATH, {"items": []}), Route("GET", REPO, repo))
    candidates = await github.find_candidates(bus, "Widget API", ["example-org/widget-api"])
    assert [candidate.ref for candidate in candidates] == ["repository:example-org/widget-api"]
    bus, _ = make_bus(
        Route("GET", SEARCH_ISSUES_PATH, {"items": [issue(41, "Widget API upload stalls")]}), Route("GET", REPO, repo)
    )
    candidates = await github.find_candidates(bus, "Widget API", ["example-org/widget-api"])
    assert [candidate.resource_type for candidate in candidates] == ["issue"]


@pytest.mark.asyncio
async def test_failed_reads_never_raise() -> None:
    bus, _ = make_bus()
    assert await github.find_candidates(bus, "Upload stalls", ["example-org/widget-api"]) == []
    assert await github.read_record(bus, "issue:example-org/widget-api#41") is None
    assert await github.read_field(bus, "issue:example-org/widget-api#41", "comments") is None
    assert await github.policy_sources(bus, TaskFrame(subject_entities=("example-org/widget-api",))) == []
    assert await github.read_record(bus, "customer:cus_1") is None


# --------------------------------------------------------------------------------------
# Records and policy
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_record_issue_pull_and_comments() -> None:
    bus, _ = make_bus(
        Route("GET", f"{REPO}/issues/41", issue(41, "Upload stalls", labels=("bug", "needs-repro"))),
        Route("GET", f"{REPO}/issues/43", issue(43, "Fix upload stall", pull=True)),
        Route(
            "GET",
            f"{REPO}/pulls/43",
            {"number": 43, "merged": False, "draft": True, "head": {"ref": "fix-stall"}, "base": {"ref": "main"}},
        ),
        Route("GET", f"{REPO}/issues/41/comments", [{"id": 1, "body": "first"}, {"id": 2, "body": "second"}]),
    )
    record = await github.read_record(bus, "issue:example-org/widget-api#41")
    assert record is not None and record.ref == "issue:example-org/widget-api#41"
    assert record.fields["labels"] == "bug, needs-repro" and record.fields["assignees"] == "maintainer-one"
    assert record.fields["milestone"] == "2.1" and record.fields["repository"] == "example-org/widget-api"
    assert await github.read_field(bus, "issue:example-org/widget-api#41", "title") == "Upload stalls"
    assert await github.read_field(bus, "issue:example-org/widget-api#41", "comments") == "first\nsecond"
    pull = await github.read_record(bus, "pull_request:example-org/widget-api#43")
    assert pull is not None and pull.resource_type == "pull_request"
    assert pull.fields["merged"] == "false" and pull.fields["head"] == "fix-stall" and pull.fields["base"] == "main"
    history = await github.channel_history(bus, "example-org/widget-api#41")
    assert [item["text"] for item in history] == ["first", "second"]


@pytest.mark.asyncio
async def test_policy_sources_read_contribution_files_and_policy_issues() -> None:
    contributing = {"type": "file", "encoding": "base64", "content": b64("Merges require review by a code owner.")}
    codeowners = {"type": "file", "encoding": "base64", "content": b64("* @example-org/maintainers")}
    open_issues = [
        issue(1, "Release process", body="Every release must be approved by the release manager before publishing."),
        issue(2, "Crash on start", body="stack trace"),
        issue(3, "Tracking", body="Labels are applied by triage.", labels=("policy",)),
    ]
    bus, gateway = make_bus(
        Route("GET", f"{REPO}/contents/CONTRIBUTING.md", contributing),
        Route("GET", f"{REPO}/contents/.github/CODEOWNERS", codeowners),
        Route("GET", f"{REPO}/issues", open_issues, query={"state": "open"}),
        phase="P1",
    )
    frame = TaskFrame(subject_entities=("Upload stalls",), observed_identifiers=("example-org/widget-api",))
    sources = await github.policy_sources(bus, frame)
    by_ref = {source.resource_ref: source for source in sources}
    assert by_ref["file:example-org/widget-api/CONTRIBUTING.md"].text == "Merges require review by a code owner."
    assert "file:example-org/widget-api/.github/CODEOWNERS" in by_ref
    assert "file:example-org/widget-api/.github/CONTRIBUTING.md" not in by_ref
    assert {"issue:example-org/widget-api#1", "issue:example-org/widget-api#3"} <= set(by_ref)
    assert "issue:example-org/widget-api#2" not in by_ref
    content_reads = [call for call in gateway.calls if "/contents/" in str(call["path"])]
    assert len(content_reads) <= github_module.MAX_POLICY_FILE_READS
    assert all(call["method"] == "GET" for call in gateway.calls)


# --------------------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------------------


def test_update_action_patch_shape_and_readback() -> None:
    action = github.update_action(
        "u1",
        "issue:example-org/widget-api#41",
        {"title": "Upload stalls over 2 GB", "labels": "bug, needs-repro"},
        ("end_state[0]",),
        target_refs=("Upload stalls",),
    )
    assert action is not None
    assert (action.provider, action.method, action.path) == ("github", "PATCH", f"{REPO}/issues/41")
    assert action.body == {"title": "Upload stalls over 2 GB", "labels": ["bug", "needs-repro"]}
    assert action.fields == ("title", "labels") and action.body_encoding == "json"
    assert action.target_refs == ("Upload stalls", "issue:example-org/widget-api#41")
    assert action.readback is not None and action.readback.path == f"{REPO}/issues/41"
    assert action.readback.field_path == "title"


def test_add_labels_is_additive_post() -> None:
    action = github.update_action("u2", "issue:example-org/widget-api#41", {"add_labels": "needs-repro"}, ("e[0]",))
    assert action is not None
    assert (action.method, action.path, action.body) == (
        "POST",
        f"{REPO}/issues/41/labels",
        {"labels": ["needs-repro"]},
    )
    assert action.fields == ("labels",)


@pytest.mark.parametrize(
    ("ref", "fields"),
    [
        ("issue:example-org/widget-api#41", {"base": "main"}),
        ("issue:example-org/widget-api#41", {"merged": "true"}),
        ("issue:example-org/widget-api#41", {"state": "merged"}),
        ("issue:example-org/widget-api#41", {}),
        ("repository:example-org/widget-api", {"description": "x"}),
        ("issue:widget-api#41", {"title": "x"}),
        ("customer:cus_1", {"title": "x"}),
    ],
)
def test_update_action_refuses_what_it_does_not_offer(ref: str, fields: dict[str, str]) -> None:
    assert github.update_action("u3", ref, fields, ("e[0]",)) is None


def test_message_action_is_an_issue_comment_with_created_id_readback() -> None:
    action = github.message_action("m1", "issue:example-org/widget-api#41", "Could you attach logs?", ("d[0]",))
    assert action is not None
    assert (action.kind, action.method, action.path) == ("message", "POST", f"{REPO}/issues/41/comments")
    assert action.body == {"body": "Could you attach logs?"} and action.fields == ("body",)
    assert action.readback is not None and has_placeholders(action.readback)
    filled = fill_placeholders(action.readback, {"id": 7001})
    assert filled.path == f"{REPO}/issues/comments/7001" and filled.field_path == "body"
    assert github.message_action("m2", "example-org/widget-api#41", "ok", ("d[0]",)) is not None
    assert github.message_action("m3", "C0123", "ok", ("d[0]",)) is None
    assert github.message_action("m4", "example-org/widget-api#41", "   ", ("d[0]",)) is None
    assert github.draft_action("d1", "a@corp.example", "s", "b", ("d[0]",)) is None


@pytest.mark.asyncio
async def test_resolve_channel_requires_an_existing_issue() -> None:
    bus, _ = make_bus(Route("GET", f"{REPO}/issues/41", issue(41, "Upload stalls")))
    assert await github.resolve_channel(bus, "example-org/widget-api#41") == "example-org/widget-api#41"
    assert await github.resolve_channel(bus, "example-org/widget-api#40") is None
    assert await github.resolve_channel(bus, "general") is None


def test_playbook_never_offers_destructive_or_release_routes() -> None:
    source = inspect.getsource(github_module)
    code = source.split('"""', 2)[2]  # skip the module docstring, which names what is never offered
    for fragment in ('"DELETE"', "/merge", "/git/refs", "/releases", "/protection", "/actions/", 'method="PUT"'):
        assert fragment not in code, fragment


# --------------------------------------------------------------------------------------
# Gate interplay
# --------------------------------------------------------------------------------------


def _triage_context() -> Context:
    ctx = Context(
        trial_id="t-github",
        user_prompt="Label the Upload stalls on large files issue in example-org/widget-api as needs-repro.",
        providers=("github",),
    )
    chosen = Candidate(
        provider="github",
        resource_type="issue",
        resource_id="example-org/widget-api#41",
        display="Upload stalls on large files",
        name="Upload stalls on large files",
    )
    lookalike = Candidate(
        provider="github",
        resource_type="issue",
        resource_id="example-org/widget-api#42",
        display="Upload stalls on large files (legacy client)",
        name="Upload stalls on large files (legacy client)",
    )
    ctx.candidates = [chosen, lookalike]
    ctx.targets = [
        ResolvedTarget(
            provider="github",
            resource_type="issue",
            resource_id=chosen.resource_id,
            display=chosen.display,
            evidence=("open",),
        )
    ]
    ctx.protected.add_candidate(lookalike)
    ctx.dod = DefinitionOfDone(
        forbidden=("delete_any", "merge_pr", "push_commit", "edit_source", "mutate_protected"),
        write_scope=("github",),
    )
    return ctx


def test_constructed_github_writes_pass_the_gate_and_lookalike_writes_do_not() -> None:
    gate = Gate(context=_triage_context(), allow_unplanned=True)
    allowed = [
        github.update_action("u1", "issue:example-org/widget-api#41", {"labels": "bug, needs-repro"}, ("e[0]",)),
        github.update_action("u2", "issue:example-org/widget-api#41", {"add_labels": "needs-repro"}, ("e[0]",)),
        github.message_action("m1", "example-org/widget-api#41", "Could you attach the client version?", ("d[0]",)),
    ]
    for action in allowed:
        assert action is not None and action.readback is not None
        verdict = gate.evaluate(action)
        assert verdict.allowed, f"{action.id}: {verdict.rule} {verdict.reason}"
    lookalike = github.message_action("m2", "example-org/widget-api#42", "Could you attach it?", ("d[0]",))
    assert lookalike is not None
    stripped = lookalike.model_copy(update={"target_refs": ()})
    for action in (lookalike, stripped):
        verdict = gate.evaluate(action)
        assert not verdict.allowed and verdict.rule == "protected"


def test_compound_ids_match_whole_path_segments_in_order() -> None:
    def segments(path: str) -> list[str]:
        return [part for part in path.split("/") if part]

    ident = "example-org/widget-api#42"
    assert compound_id_in_path(ident, segments("/repos/example-org/widget-api/issues/42"))
    assert compound_id_in_path(ident, segments("/repos/Example-Org/widget-api/pulls/42/merge"))
    assert compound_id_in_path(ident, segments("/repos/example-org/widget-api/issues/42/comments"))
    assert not compound_id_in_path(ident, segments("/repos/example-org/widget-api/issues/420"))
    assert not compound_id_in_path(ident, segments("/repos/example-org/widget-api-legacy/issues/42"))
    assert not compound_id_in_path(ident, segments("/repos/example-org/widget-api/a/b/42"))
    assert not compound_id_in_path("701", segments("/crm/v3/objects/companies/701"))


@pytest.mark.parametrize(
    ("method", "path", "body", "label"),
    [
        ("POST", f"{REPO}/merge-upstream", {"branch": "main"}, "merge_pr"),
        ("PUT", f"{REPO}/pulls/7/update-branch", {}, "push_commit"),
        ("PATCH", f"{REPO}/issues/41", {"state": "closed"}, "close_regression"),
        ("PATCH", f"{REPO}/git/refs/heads/main", {"sha": "abc", "force": True}, "push_commit"),
        ("DELETE", f"{REPO}/releases/12", None, "delete_any"),
    ],
)
def test_github_classifications(method: str, path: str, body: object, label: str) -> None:
    assert label in classify("github", method, path, body)


def test_ordinary_github_writes_carry_no_forbidden_class() -> None:
    assert classify("github", "PATCH", f"{REPO}/issues/41", {"state": "open", "labels": ["bug"]}) == frozenset()
    assert classify("github", "POST", f"{REPO}/issues/41/comments", {"body": "closed the loop"}) == frozenset()
    assert classify("github", "POST", f"{REPO}/issues/41/labels", {"labels": ["needs-repro"]}) == frozenset()
