# GitHub playbook (`github`, role `code_host`)

`src/benchpress/playbooks/github.py` lets Benchpress run repo-ops and release-engineering requests
against GitHub's REST API. It gives the `release-engineering` policy pack a real provider to guard.
It follows the same contract as the Slack, Gmail, HubSpot and Stripe playbooks. It reads through the
tool bus and builds write `Action`s with a `ReadBack`. It never decides what to do.

## Record ids

| Record | Ref | REST route |
|---|---|---|
| Issue | `issue:owner/repo#12` | `/repos/{owner}/{repo}/issues/12` |
| Pull request | `pull_request:owner/repo#12` | `/repos/{owner}/{repo}/issues/12` (+ `/pulls/12` for merge state) |
| Repository | `repository:owner/repo` | `/repos/{owner}/{repo}` |

Issues and pull requests share one number space per repository, so both use `owner/repo#N`.

## Reads

- **Candidates.** The playbook calls `GET /search/issues` with `q="<entity>" in:title,body repo:owner/repo`,
  and a second query built from distinctive tokens. Both queries cover issues and pull requests. If search
  is unavailable, it falls back to `GET /repos/{o}/{r}/issues?state=all` and filters the results client-side.
  An `owner/repo#N` hint, or a URL for one, is read directly. Each candidate's notes carry the repository,
  state, labels, assignees, milestone, author and body excerpt. That gives the resolver evidence to tell an
  issue from its look-alike. **Repositories are candidates only when no issue or pull request matched.** An
  unchosen repository goes into the deny-list, and from there it would block every write to its issues.
- **Policy sources.** The playbook reads `CONTRIBUTING.md`, `SECURITY.md` and `CODEOWNERS` from the repository
  root or `.github/`, through `GET /repos/{o}/{r}/contents/{path}`. That is at most 6 reads, and the first hit
  per file name wins. It also reads open issues whose labels or titles mark them as policy (`policy`,
  `process`, `governance`, "checklist", …). Pinned issues only exist in GraphQL, so this label and title
  convention stands in for them.
- **Fields.** `read_record` flattens an issue into `title`, `body`, `state`, `labels` and `assignees` (both
  comma-joined), `milestone`, `author` and `number`. Pull requests also get `merged`, `draft`,
  `mergeable_state`, `head` and `base`. `read_field(ref, "comments")` returns the issue's comment bodies.

## Writes (the only ones constructed)

| Intent | Request | Read-back |
|---|---|---|
| `update` title/body/state/state_reason/labels/assignees/milestone | `PATCH /repos/{o}/{r}/issues/{n}` | `GET` the issue, first scalar field |
| `update` with only `add_labels` | `POST /repos/{o}/{r}/issues/{n}/labels` (additive) | `GET` the issue |
| `message` (comment) | `POST /repos/{o}/{r}/issues/{n}/comments` | `GET /repos/{o}/{r}/issues/comments/{created_id}` → `body` |

`labels` in a `PATCH` replaces the whole label set, which is how GitHub defines it. Use `add_labels` to keep
the existing labels. The constructors return `None` for unknown fields (`base`, `merged`, …), invalid
states, and refs that are not issues or pull requests. The playbook never builds a request to merge, update
a ref, commit contents, touch a release, change branch protection or toggle a workflow. A test checks this
against the module source.

## Gate behaviour

The corpus file `src/benchpress/corpus/github.yaml` has 26 cases:

- **Allowed:** comments, label replace and label add, title and assignee updates, and close when
  `close_regression` is not forbidden. The ordinary comment is also allowed under `release-engineering`.
- **Refused by the definition of done** (`action_class`): PR merge and fork sync (`merge_pr`), force push,
  contents commit and PR `update-branch` (`push_commit`), and issue close when `close_regression` is forbidden.
- **Refused by method:** branch, tag and release deletes.
- **Refused by the `release-engineering` pack:** merge without approval, force push, branch delete, release
  publish and delete, and branch-protection change.
- **Refused as `protected`:** a look-alike issue addressed by path alone, the same issue number in a
  look-alike repository, a declared look-alike ref, and a comment that quotes the look-alike's title. There
  is also an allow case: protected `#42` does not match issue `#420`.

Gate changes, all general and scoped to GitHub routes or to compound ids. Existing providers behave as before.

1. **Compound-id identity** (`gate.compound_id_in_path`). A protected id that contains `/` or `#` is refused
   when the request path spells it. That means each part is a whole path segment, in order, with at most one
   route word between parts. So `owner/repo#42` matches `/repos/owner/repo/issues/42/comments` and
   `/pulls/42/merge`, but never `/issues/420`. Single-part ids (every existing provider's) are unaffected.
2. **New GitHub classes:** `POST /merge-upstream` is `merge_pr`, `PUT /pulls/{n}/update-branch` is
   `push_commit`, and `PATCH /issues/{n}` with `"state": "closed"` is `close_regression`.

## Real-app gateway

`realapp.py` already configured `github` before this change: host `https://api.github.com`, token from
`GITHUB_TOKEN`, and the headers `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`
and `Authorization: Bearer …`. The gateway sets its own User-Agent. `tests/test_realapp_github.py` covers
this with `httpx.MockTransport` only. It checks the headers, JSON bodies, token redaction, the scratch
opt-in guard, the `code_host` alias, twin tokens, and playbook reads that pass the gateway's path checks.

## Offline end-to-end

`tests/test_github_e2e.py` runs the full controller against an in-memory GitHub (`GitHubWorkspace`), with
the real playbook and a scripted model. The request is to label and retitle one issue while a look-alike
issue sits in the same repository. The run reads the contribution guide and chooses the right issue. The
gate refuses the planted look-alike write (rule `protected`), so the look-alike stays unchanged. The two
real writes land, the title is read back, and the end-state and protected-unchanged evidence all match. With
the `no_gate` ablation, the look-alike write lands, which shows the gate is what stops it.

## Limits

- The playbook uses REST only. Pinned issues, Projects and discussions need GraphQL, and it does not use it.
- Search results are capped: two search calls, and two list pages of 100 per repository in the fallback.
- A bare `#N` binds to a repository only when exactly one repository is named.
- Comment text containing URLs is checked by the gate's external-destination rule like any other body,
  so a `github.com` link needs that domain to be known to the run.
- Protected *names* still match as substrings, as they do for every provider. A look-alike title that is a
  prefix of the chosen title can over-refuse a write that quotes the chosen title.
