"""GitHub playbook (role `code_host`) — official REST v3 shapes, JSON bodies, data-plane routes only.

Records. Issues and pull requests share one number space per repository, so both are addressed as
`owner/repo#number`: `issue:owner/repo#12` and `pull_request:owner/repo#12`. A repository is
`repository:owner/repo`. The gate recognises these compound ids inside a request path
(`/repos/owner/repo/issues/12/comments`), so a write aimed at a look-alike is refused on identity
even when the planner forgot to declare a target ref.

Reads.
- Candidates: `GET /search/issues?q=<text> in:title,body [repo:owner/repo]` (issues and pull requests),
  falling back to a client-side-filtered `GET /repos/{owner}/{repo}/issues?state=all` when search is
  unavailable, plus direct `GET /repos/{owner}/{repo}/issues/{number}` for `owner/repo#N` hints.
  Repositories become candidates only when no issue or pull request matched: an unchosen repository
  would otherwise protect every issue inside it.
- Policy sources: `CONTRIBUTING.md`, `SECURITY.md` and `CODEOWNERS` (root and `.github/`) through
  `GET /repos/{owner}/{repo}/contents/{path}`, and open issues labelled or titled as policy/process.
  Pinned issues are GraphQL-only, so the REST label/title convention stands in for them.

Writes (the only ones ever constructed).
- update: `PATCH /repos/{owner}/{repo}/issues/{number}` with `title`, `body`, `state`, `state_reason`,
  `labels`, `assignees`, `milestone` (works for pull requests too), or the additive
  `POST /repos/{owner}/{repo}/issues/{number}/labels` when the only field is `add_labels`.
- message: `POST /repos/{owner}/{repo}/issues/{number}/comments`, read back through
  `GET /repos/{owner}/{repo}/issues/comments/{created_id}`.

Merges, ref updates, force pushes, content commits, branch/tag/release deletes, branch protection,
workflow toggles and releases are never offered — the gate classifies each of them regardless.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote as url_quote

from benchpress.context import Action, Candidate, ReadBack, TaskFrame
from benchpress.playbooks import (
    CREATED_ID_PLACEHOLDER,
    BasePlaybook,
    EntityTerms,
    PolicySource,
    ProviderRecord,
    as_records,
    as_str,
    can_read,
    entity_terms,
    flat_fields,
    parse_ref,
    read_json,
    text_matches_terms,
)
from benchpress.tools import BudgetExhausted, ToolBus, as_mapping

SEARCH_ISSUES_PATH = "/search/issues"
SEARCH_REPOS_PATH = "/search/repositories"
PAGE_SIZE = "50"
LIST_PAGE_SIZE = "100"
MAX_SEARCH_CALLS = 2
MAX_REPOS = 2
MAX_LIST_PAGES = 2
MAX_NUMBER_LOOKUPS = 3
MAX_POLICY_ENTITIES = 2
MAX_POLICY_FILE_READS = 6
MAX_NOTES_CHARS = 1_200
MAX_POLICY_TEXT_CHARS = 6_000
MAX_COMMENT_CHARS = 60_000

ISSUE_TYPES: frozenset[str] = frozenset({"issue", "issues"})
PULL_TYPES: frozenset[str] = frozenset({"pull_request", "pull_requests", "pull", "pulls", "pr"})
REPO_TYPES: frozenset[str] = frozenset({"repository", "repositories", "repo", "repos"})

# Fields `PATCH /repos/{owner}/{repo}/issues/{number}` accepts that an ordinary repo-ops request may change.
UPDATE_FIELDS: frozenset[str] = frozenset(
    {"title", "body", "state", "state_reason", "labels", "assignees", "milestone"}
)
LIST_FIELDS: frozenset[str] = frozenset({"labels", "assignees"})
ADD_LABELS_FIELD = "add_labels"
STATES: frozenset[str] = frozenset({"open", "closed"})
STATE_REASONS: frozenset[str] = frozenset({"completed", "not_planned", "reopened"})

POLICY_FILES: tuple[str, ...] = (
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "SECURITY.md",
    ".github/SECURITY.md",
    "CODEOWNERS",
    ".github/CODEOWNERS",
)
POLICY_LABELS: frozenset[str] = frozenset(
    {"policy", "process", "governance", "release-policy", "guidelines", "pinned", "announcement"}
)
_POLICY_TITLE = re.compile(r"\b(polic(?:y|ies)|process|guidelines?|checklist|code ?owners|how we|rules)\b", re.I)

_REPO_REF = re.compile(
    r"(?<![\w.@/-])(?:https?://)?(?:www\.)?(?:github\.com/)?"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})"
    r"(?:(?:#|/(?:issues|pull|pulls)/)(\d{1,9}))?"
)
_NUMBER_HINT = re.compile(r"(?:^|\s)#(\d{1,9})\b")
_COMPOUND_ID = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)#(\d+)$")
_REPO_ID = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)$")


@dataclass(frozen=True)
class RepoRef:
    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def path(self) -> str:
        return f"/repos/{self.owner}/{self.repo}"


@dataclass(frozen=True)
class NumberRef:
    repo: RepoRef
    number: int

    @property
    def compound(self) -> str:
        return f"{self.repo.full_name}#{self.number}"


# --------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------


def repo_refs_in(values: Sequence[str]) -> list[RepoRef]:
    """`owner/repo` mentions (bare, `owner/repo#N`, or github.com URLs), in first-seen order."""
    found: dict[str, RepoRef] = {}
    for value in values:
        for match in _REPO_REF.finditer(value):
            owner, repo = match.group(1), match.group(2).rstrip(".")
            if repo.casefold() in {"issues", "pull", "pulls"} or not repo:
                continue
            ref = RepoRef(owner, repo.removesuffix(".git"))
            found.setdefault(ref.full_name.casefold(), ref)
    return list(found.values())


def number_refs_in(values: Sequence[str], repos: Sequence[RepoRef]) -> list[NumberRef]:
    """`owner/repo#N` / issue URLs, plus bare `#N` bound to the only named repository."""
    found: dict[str, NumberRef] = {}
    for value in values:
        for match in _REPO_REF.finditer(value):
            if match.group(3):
                ref = NumberRef(RepoRef(match.group(1), match.group(2)), int(match.group(3)))
                found.setdefault(ref.compound.casefold(), ref)
    if len(repos) == 1:
        for value in values:
            for match in _NUMBER_HINT.finditer(value):
                ref = NumberRef(repos[0], int(match.group(1)))
                found.setdefault(ref.compound.casefold(), ref)
    return list(found.values())


def parse_number_id(resource_id: str) -> NumberRef | None:
    match = _COMPOUND_ID.match(resource_id.strip())
    if match is None:
        return None
    return NumberRef(RepoRef(match.group(1), match.group(2)), int(match.group(3)))


def parse_repo_id(resource_id: str) -> RepoRef | None:
    match = _REPO_ID.match(resource_id.strip())
    return RepoRef(match.group(1), match.group(2)) if match else None


def search_quote(value: str) -> str:
    """A phrase for GitHub's search syntax: double-quoted, inner quotes dropped."""
    cleaned = value.replace('"', " ").strip()
    return f'"{cleaned}"' if " " in cleaned else cleaned


def issue_queries(terms: EntityTerms, repos: Sequence[RepoRef]) -> list[str]:
    """At most `MAX_SEARCH_CALLS` search expressions: the full name, then its distinctive tokens."""
    scope = " ".join(f"repo:{repo.full_name}" for repo in repos[:MAX_REPOS])
    texts: list[str] = []
    for name in terms.names[:1]:
        if not _REPO_REF.fullmatch(name):
            texts.append(search_quote(name))
    if terms.tokens:
        texts.append(" OR ".join(terms.tokens[:3]))
    queries = [" ".join(part for part in (text, "in:title,body", scope) if part) for text in texts]
    return list(dict.fromkeys(queries))[:MAX_SEARCH_CALLS]


def names_of(items: object, key: str) -> list[str]:
    """`[{"name": "bug"}, "docs"]` → `["bug", "docs"]` (label objects or plain strings)."""
    names: list[str] = []
    if isinstance(items, (list, tuple)):
        for item in as_records(cast(object, items)):
            value = as_str(item.get(key)).strip()
            if value:
                names.append(value)
        for item in cast(Sequence[object], items):
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
    return names


def repo_of_issue(record: Mapping[str, Any]) -> RepoRef | None:
    """The repository an issue payload belongs to (`repository_url` or `html_url`)."""
    for key in ("repository_url", "html_url", "url"):
        text = as_str(record.get(key))
        match = re.search(r"/repos/([^/]+)/([^/]+)", text) or re.search(r"github\.com/([^/]+)/([^/]+)", text)
        if match:
            return RepoRef(match.group(1), match.group(2))
    repository = as_mapping(record.get("repository"))
    full_name = as_str(repository.get("full_name"))
    return parse_repo_id(full_name) if full_name else None


def is_pull(record: Mapping[str, Any]) -> bool:
    return "pull_request" in record or as_str(record.get("html_url")).find("/pull/") >= 0


def issue_notes(record: Mapping[str, Any], repo: RepoRef) -> str:
    labels = names_of(record.get("labels"), "name")
    assignees = names_of(record.get("assignees"), "login")
    milestone = as_str(as_mapping(record.get("milestone")).get("title"))
    parts = [
        f"repository={repo.full_name}",
        f"number={as_str(record.get('number'))}",
        f"state={as_str(record.get('state'))}",
    ]
    if labels:
        parts.append(f"labels={', '.join(labels)}")
    if assignees:
        parts.append(f"assignees={', '.join(assignees)}")
    if milestone:
        parts.append(f"milestone={milestone}")
    author = as_str(as_mapping(record.get("user")).get("login"))
    if author:
        parts.append(f"author={author}")
    body = as_str(record.get("body")).strip()
    header = "; ".join(parts)
    return f"{header}\n{body}"[:MAX_NOTES_CHARS] if body else header


def issue_candidate(record: Mapping[str, Any], repo: RepoRef | None = None) -> Candidate | None:
    number = record.get("number")
    resolved = repo or repo_of_issue(record)
    if resolved is None or not isinstance(number, int) or isinstance(number, bool):
        return None
    title = as_str(record.get("title")).strip()
    compound = f"{resolved.full_name}#{number}"
    return Candidate(
        provider="github",
        resource_type="pull_request" if is_pull(record) else "issue",
        resource_id=compound,
        display=title or compound,
        name=title or None,
        lifecycle=as_str(record.get("state")) or None,
        notes=issue_notes(record, resolved),
    )


def repo_candidate(record: Mapping[str, Any]) -> Candidate | None:
    full_name = as_str(record.get("full_name")).strip()
    if parse_repo_id(full_name) is None:
        return None
    lifecycle = "archived" if record.get("archived") is True else ("private" if record.get("private") is True else None)
    topics = names_of(record.get("topics"), "name")
    notes = "; ".join(
        part
        for part in (
            as_str(record.get("description")).strip(),
            f"default_branch={as_str(record.get('default_branch'))}" if record.get("default_branch") else "",
            f"topics={', '.join(topics)}" if topics else "",
        )
        if part
    )
    return Candidate(
        provider="github",
        resource_type="repository",
        resource_id=full_name,
        display=full_name,
        lifecycle=lifecycle,
        notes=notes,
    )


def decode_content(payload: Mapping[str, Any]) -> str:
    """Text of a `GET /contents/{path}` file payload (base64 with line breaks)."""
    if as_str(payload.get("type")) not in {"", "file"}:
        return ""
    content = as_str(payload.get("content"))
    if not content:
        return ""
    if as_str(payload.get("encoding")) not in {"", "base64"}:
        return content
    try:
        data = base64.b64decode("".join(content.split()), validate=False)
    except (binascii.Error, ValueError):
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def issue_fields(record: Mapping[str, Any]) -> dict[str, str]:
    """Flat string fields of an issue or pull request, with list fields as comma-joined names."""
    fields = flat_fields(record)
    for noisy in [
        key for key in fields if key.startswith(("user.", "reactions.", "pull_request.")) or key.endswith("_url")
    ]:
        fields.pop(noisy, None)
    fields["labels"] = ", ".join(names_of(record.get("labels"), "name"))
    fields["assignees"] = ", ".join(names_of(record.get("assignees"), "login"))
    fields["milestone"] = as_str(as_mapping(record.get("milestone")).get("title"))
    fields["author"] = as_str(as_mapping(record.get("user")).get("login"))
    for key in ("title", "body", "state"):
        fields.setdefault(key, "")
    return fields


def body_value(field: str, value: str) -> object | None:
    """The JSON value `PATCH /issues/{number}` expects for one field; `None` when invalid."""
    if field in LIST_FIELDS:
        return [item.strip() for item in value.split(",") if item.strip()]
    if field == "state":
        folded = value.strip().casefold()
        return folded if folded in STATES else None
    if field == "state_reason":
        folded = value.strip().casefold()
        return folded if folded in STATE_REASONS else None
    if field == "milestone":
        stripped = value.strip()
        return int(stripped) if stripped.isdigit() else None
    return value


async def read_list(
    bus: ToolBus, provider: str, path: str, query: Mapping[str, str] | None = None
) -> list[Mapping[str, Any]] | None:
    """One budgeted read of a list-shaped payload. `None` on failure, non-list body, or exhausted budget."""
    if not can_read(bus):
        return None
    try:
        result = await bus.read(provider, path, query=query)
    except BudgetExhausted:
        return None
    payload: object = result.body
    if not result.ok or not isinstance(payload, list):
        return None
    return as_records(cast(object, payload))


class GitHubPlaybook(BasePlaybook):
    provider: str = "github"
    role: str = "code_host"
    identity_fields: tuple[str, ...] = ("number", "title", "id")

    # -- low-level reads -----------------------------------------------------------------

    async def get_issue(self, bus: ToolBus, ref: NumberRef) -> Mapping[str, Any] | None:
        return await read_json(bus, self.provider, f"{ref.repo.path}/issues/{ref.number}")

    async def get_pull(self, bus: ToolBus, ref: NumberRef) -> Mapping[str, Any] | None:
        return await read_json(bus, self.provider, f"{ref.repo.path}/pulls/{ref.number}")

    async def get_repo(self, bus: ToolBus, repo: RepoRef) -> Mapping[str, Any] | None:
        return await read_json(bus, self.provider, repo.path)

    async def search_issues(self, bus: ToolBus, queries: Sequence[str]) -> list[Mapping[str, Any]] | None:
        """`GET /search/issues`; `None` when every call failed (search unavailable)."""
        results: dict[str, Mapping[str, Any]] = {}
        succeeded = False
        for query in queries[:MAX_SEARCH_CALLS]:
            payload = await read_json(bus, self.provider, SEARCH_ISSUES_PATH, query={"q": query, "per_page": PAGE_SIZE})
            if payload is None:
                continue
            succeeded = True
            for item in as_records(payload.get("items")):
                key = (
                    as_str(item.get("html_url")) or f"{as_str(item.get('repository_url'))}#{as_str(item.get('number'))}"
                )
                results.setdefault(key, item)
        return list(results.values()) if succeeded else None

    async def list_issues(self, bus: ToolBus, repo: RepoRef) -> list[Mapping[str, Any]]:
        """`GET /repos/{owner}/{repo}/issues?state=all` (issues and pull requests), bounded pages."""
        records: list[Mapping[str, Any]] = []
        for page in range(1, MAX_LIST_PAGES + 1):
            if not can_read(bus):
                break
            query = {"state": "all", "per_page": LIST_PAGE_SIZE, "page": str(page)}
            page_records = await read_list(bus, self.provider, f"{repo.path}/issues", query)
            if page_records is None:
                break
            records.extend(page_records)
            if len(page_records) < int(LIST_PAGE_SIZE):
                break
        return records

    async def search_repos(self, bus: ToolBus, terms: EntityTerms) -> list[Mapping[str, Any]]:
        if not terms.names:
            return []
        payload = await read_json(
            bus,
            self.provider,
            SEARCH_REPOS_PATH,
            query={"q": f"{search_quote(terms.names[0])} in:name,description", "per_page": PAGE_SIZE},
        )
        return as_records(payload.get("items")) if payload is not None else []

    # -- candidates ------------------------------------------------------------------------

    async def find_candidates(self, bus: ToolBus, entity: str, hints: Sequence[str]) -> list[Candidate]:
        terms = entity_terms(entity, hints)
        values = [entity, *hints]
        repos = repo_refs_in(values)
        found: dict[str, Candidate] = {}
        for number_ref in number_refs_in(values, repos)[:MAX_NUMBER_LOOKUPS]:
            record = await self.get_issue(bus, number_ref)
            candidate = issue_candidate(record, number_ref.repo) if record is not None else None
            if candidate is not None:
                found.setdefault(candidate.ref, candidate)
        if not terms.empty:
            queries = issue_queries(terms, repos)
            searched = await self.search_issues(bus, queries) if queries else None
            records: list[Mapping[str, Any]] = searched if searched is not None else []
            if searched is None:
                for repo in repos[:MAX_REPOS]:
                    listed = await self.list_issues(bus, repo)
                    records.extend(
                        record
                        for record in listed
                        if text_matches_terms(f"{as_str(record.get('title'))} {as_str(record.get('body'))}", terms)
                    )
            for record in records:
                candidate = issue_candidate(record)
                if candidate is not None:
                    found.setdefault(candidate.ref, candidate)
        if found:
            return list(found.values())
        # No issue or pull request matched: the subject may be the repository itself.
        for repo in repos[:MAX_REPOS]:
            record = await self.get_repo(bus, repo)
            candidate = repo_candidate(record) if record is not None else None
            if candidate is not None:
                found.setdefault(candidate.ref, candidate)
        for record in await self.search_repos(bus, terms) if not found and not terms.empty else []:
            candidate = repo_candidate(record)
            if candidate is not None and text_matches_terms(f"{candidate.display} {candidate.notes}", terms):
                found.setdefault(candidate.ref, candidate)
        return list(found.values())

    # -- records ---------------------------------------------------------------------------

    async def read_record(self, bus: ToolBus, ref: str) -> ProviderRecord | None:
        parsed = parse_ref(ref)
        if parsed is None:
            return None
        resource_type, resource_id = parsed
        if resource_type in REPO_TYPES:
            repo = parse_repo_id(resource_id)
            record = await self.get_repo(bus, repo) if repo is not None else None
            if repo is None or record is None:
                return None
            fields = flat_fields(record)
            fields["full_name"] = repo.full_name
            return ProviderRecord(
                provider=self.provider,
                resource_type="repository",
                resource_id=repo.full_name,
                fields=fields,
                raw=dict(record),
            )
        if resource_type not in ISSUE_TYPES | PULL_TYPES:
            return None
        number_ref = parse_number_id(resource_id)
        if number_ref is None:
            return None
        record = await self.get_issue(bus, number_ref)
        if record is None:
            return None
        fields = issue_fields(record)
        raw = dict(record)
        normalized_type = "issue"
        if resource_type in PULL_TYPES or is_pull(record):
            normalized_type = "pull_request"
            pull = await self.get_pull(bus, number_ref)
            if pull is not None:
                for key in ("merged", "draft", "mergeable_state"):
                    if pull.get(key) is not None:
                        fields[key] = as_str(pull.get(key))
                fields["head"] = as_str(as_mapping(pull.get("head")).get("ref"))
                fields["base"] = as_str(as_mapping(pull.get("base")).get("ref"))
                raw["pull"] = dict(pull)
        fields["id"] = number_ref.compound
        fields["repository"] = number_ref.repo.full_name
        fields["number"] = str(number_ref.number)
        return ProviderRecord(
            provider=self.provider,
            resource_type=normalized_type,
            resource_id=number_ref.compound,
            fields=fields,
            raw=raw,
        )

    async def read_field(self, bus: ToolBus, ref: str, field: str) -> str | None:
        """`comments` reads the issue's comment bodies; every other field comes from the record."""
        if field.strip().casefold() in {"comments", "comment", "comments.body"}:
            parsed = parse_ref(ref)
            number_ref = parse_number_id(parsed[1]) if parsed is not None else None
            if number_ref is None:
                return None
            comments = await read_list(
                bus,
                self.provider,
                f"{number_ref.repo.path}/issues/{number_ref.number}/comments",
                {"per_page": LIST_PAGE_SIZE},
            )
            if comments is None:
                return None
            return "\n".join(as_str(item.get("body")) for item in comments)
        return await super().read_field(bus, ref, field)

    # -- policy sources --------------------------------------------------------------------

    async def policy_repos(self, bus: ToolBus, frame: TaskFrame) -> list[RepoRef]:
        values = [*frame.subject_entities, *frame.observed_identifiers, frame.requested_change]
        repos = repo_refs_in([value for value in values if value])
        if repos:
            return repos[:MAX_REPOS]
        found: list[RepoRef] = []
        for entity in frame.subject_entities[:MAX_POLICY_ENTITIES]:
            terms = entity_terms(entity, ())
            for record in await self.search_issues(bus, issue_queries(terms, ())[:1]) or []:
                repo = repo_of_issue(record)
                if repo is not None and repo not in found:
                    found.append(repo)
            if len(found) >= MAX_REPOS:
                break
        return found[:MAX_REPOS]

    async def policy_sources(self, bus: ToolBus, frame: TaskFrame) -> list[PolicySource]:
        sources: dict[str, PolicySource] = {}
        file_reads = 0
        for repo in await self.policy_repos(bus, frame):
            seen_names: set[str] = set()
            for file_path in POLICY_FILES:
                name = file_path.rsplit("/", 1)[-1]
                if name in seen_names or file_reads >= MAX_POLICY_FILE_READS or not can_read(bus):
                    continue
                file_reads += 1
                encoded = "/".join(url_quote(part, safe="._-") for part in file_path.split("/"))
                path = f"{repo.path}/contents/{encoded}"
                payload = await read_json(bus, self.provider, path)
                text = decode_content(payload) if payload is not None else ""
                if not text.strip():
                    continue
                seen_names.add(name)
                ref = f"file:{repo.full_name}/{file_path}"
                sources.setdefault(
                    ref,
                    PolicySource(
                        provider=self.provider,
                        resource_ref=ref,
                        title=f"{repo.full_name} {file_path}",
                        text=text[:MAX_POLICY_TEXT_CHARS],
                        path=path,
                    ),
                )
            if not can_read(bus):
                break
            for record in await self.list_policy_issues(bus, repo):
                candidate = issue_candidate(record, repo)
                body = as_str(record.get("body")).strip()
                if candidate is None or not body:
                    continue
                sources.setdefault(
                    candidate.ref,
                    PolicySource(
                        provider=self.provider,
                        resource_ref=candidate.ref,
                        title=candidate.display,
                        text=body[:MAX_POLICY_TEXT_CHARS],
                        author=as_str(as_mapping(record.get("user")).get("login")),
                        path=f"{repo.path}/issues/{as_str(record.get('number'))}",
                    ),
                )
        return list(sources.values())

    async def list_policy_issues(self, bus: ToolBus, repo: RepoRef) -> list[Mapping[str, Any]]:
        """Open issues labelled or titled as standing policy (REST stand-in for pinned issues)."""
        listed = await read_list(
            bus, self.provider, f"{repo.path}/issues", {"state": "open", "per_page": LIST_PAGE_SIZE}
        )
        selected: list[Mapping[str, Any]] = []
        for record in listed or []:
            labels = {label.casefold() for label in names_of(record.get("labels"), "name")}
            if labels & POLICY_LABELS or _POLICY_TITLE.search(as_str(record.get("title"))):
                selected.append(record)
        return selected

    # -- writes ----------------------------------------------------------------------------

    def update_action(
        self,
        action_id: str,
        ref: str,
        fields: Mapping[str, str],
        satisfies: Sequence[str],
        *,
        target_refs: Sequence[str] = (),
        rationale: str = "",
    ) -> Action | None:
        parsed = parse_ref(ref)
        if parsed is None or not action_id or parsed[0] not in ISSUE_TYPES | PULL_TYPES:
            return None
        number_ref = parse_number_id(parsed[1])
        if number_ref is None or not fields:
            return None
        issue_path = f"{number_ref.repo.path}/issues/{number_ref.number}"
        normalized_ref = f"{'pull_request' if parsed[0] in PULL_TYPES else 'issue'}:{number_ref.compound}"
        refs = tuple(dict.fromkeys([*target_refs, normalized_ref]))
        names = [name.strip() for name in fields if name.strip()]
        if names == [ADD_LABELS_FIELD]:
            labels = body_value("labels", fields[ADD_LABELS_FIELD])
            if not labels:
                return None
            return Action(
                id=action_id,
                kind="update",
                provider=self.provider,
                method="POST",
                path=f"{issue_path}/labels",
                body={"labels": labels},
                fields=("labels",),
                satisfies=tuple(satisfies),
                target_refs=refs,
                readback=ReadBack(method="GET", path=issue_path, field_path="labels"),
                rationale=rationale or f"add labels on {normalized_ref}",
            )
        body: dict[str, object] = {}
        for name in names:
            if name not in UPDATE_FIELDS:
                return None
            value = body_value(name, fields[name])
            if value is None:
                return None
            body[name] = value
        if not body:
            return None
        first = next((name for name in body if name not in LIST_FIELDS), next(iter(body)))
        return Action(
            id=action_id,
            kind="update",
            provider=self.provider,
            method="PATCH",
            path=issue_path,
            body=body,
            fields=tuple(body),
            satisfies=tuple(satisfies),
            target_refs=refs,
            readback=ReadBack(method="GET", path=issue_path, field_path=first),
            rationale=rationale or f"update {', '.join(body)} on {normalized_ref}",
        )

    def message_action(
        self,
        action_id: str,
        channel_id: str,
        text: str,
        satisfies: Sequence[str],
        *,
        thread_ts: str | None = None,
        target_refs: Sequence[str] = (),
    ) -> Action | None:
        """A comment on an issue or pull request. `channel_id` is `owner/repo#N` or a full ref."""
        parsed = parse_ref(channel_id)
        resource_id = parsed[1] if parsed is not None and parsed[0] in ISSUE_TYPES | PULL_TYPES else channel_id
        number_ref = parse_number_id(resource_id)
        if number_ref is None or not action_id or not text.strip() or len(text) > MAX_COMMENT_CHARS:
            return None
        normalized_ref = f"issue:{number_ref.compound}"
        return Action(
            id=action_id,
            kind="message",
            provider=self.provider,
            method="POST",
            path=f"{number_ref.repo.path}/issues/{number_ref.number}/comments",
            body={"body": text},
            fields=("body",),
            satisfies=tuple(satisfies),
            target_refs=tuple(dict.fromkeys([*target_refs, normalized_ref])),
            readback=ReadBack(
                method="GET",
                path=f"{number_ref.repo.path}/issues/comments/{CREATED_ID_PLACEHOLDER}",
                field_path="body",
            ),
            rationale=f"comment on {normalized_ref}",
        )

    async def resolve_channel(self, bus: ToolBus, name: str) -> str | None:
        """An issue or pull request that exists, as its `owner/repo#N` comment channel."""
        refs = number_refs_in([name], repo_refs_in([name]))
        if not refs:
            return None
        record = await self.get_issue(bus, refs[0])
        return refs[0].compound if record is not None else None

    async def channel_history(self, bus: ToolBus, channel_id: str, limit: int = 50) -> list[dict[str, object]]:
        number_ref = parse_number_id(channel_id)
        if number_ref is None:
            return []
        comments = await read_list(
            bus,
            self.provider,
            f"{number_ref.repo.path}/issues/{number_ref.number}/comments",
            {"per_page": str(max(1, min(limit, int(LIST_PAGE_SIZE))))},
        )
        return [
            {
                "id": as_str(item.get("id")),
                "user": as_str(as_mapping(item.get("user")).get("login")),
                "text": as_str(item.get("body")),
                "ts": as_str(item.get("created_at")),
            }
            for item in comments or []
        ]


__all__ = [
    "ADD_LABELS_FIELD",
    "POLICY_FILES",
    "SEARCH_ISSUES_PATH",
    "UPDATE_FIELDS",
    "GitHubPlaybook",
    "NumberRef",
    "RepoRef",
    "body_value",
    "decode_content",
    "issue_candidate",
    "issue_fields",
    "issue_queries",
    "number_refs_in",
    "parse_number_id",
    "parse_repo_id",
    "read_list",
    "repo_candidate",
    "repo_refs_in",
]
