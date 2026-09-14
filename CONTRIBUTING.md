# Contributing to Benchpress

Benchpress is built in public. Issues, playbooks, policy packs, adapters and gate-corpus cases are all welcome.

## Ground rules
- **Task-agnostic.** Nothing in `src/benchpress` may reference a benchmark task, a seeded name, email or domain. CI greps for it.
- **Code beats prompt for safety.** Anything that must never happen is refused by the gate, never asked of the model.
- **State is truth.** A write is not done until it is read back. New playbooks must implement read-back.
- **Tests first.** Every change ships with tests. The gate corpus (`benchpress gate check`) must stay green.

## Setup
```bash
git clone https://github.com/rajkaria/benchpress && cd benchpress
uv sync --group dev
uv run pytest -q && uv run ruff check . && uv run pyright
```
The ArgaBench harness is optional. Without it, harness-backed tests skip cleanly rather than failing.

## What to contribute
- **Playbooks** (`src/benchpress/playbooks/`): a provider with search, read, update, read-back and policy sources. Open a "New playbook" issue first.
- **Policy packs** (`src/benchpress/policy_packs/`): YAML rules for a function (billing, CS, IT...). Packs only add refusals.
- **Gate-corpus cases** (`src/benchpress/corpus/`): a write the gate should refuse or allow, with its reason. `benchpress regress` generates them from any receipt.
- **Adapters** (`src/benchpress/shims/`, `packages/`): one file per framework, parity-tested against the shared fixture.

## Pull requests
- One change per PR. Keep the CHANGELOG entry in the PR.
- Sign off your commits (`git commit -s`). By signing off you agree to the [Developer Certificate of Origin](https://developercertificate.org/).
- Breaking changes to a public contract go through an RFC first: see [docs/rfcs/](docs/rfcs/README.md).

## Security and conduct
Found a vulnerability? Do not open a public issue — see [SECURITY.md](SECURITY.md) for how to report it privately.
This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Releases
Python releases are tagged `v*` and published to PyPI as `benchpress-agent` by the maintainer. The TypeScript guard
is tagged `benchpress-guard-v*` and published to npm as `benchpress-guard`. There is no container image or Helm chart
yet. Each artifact is versioned on its own today; one shared version across every artifact is the goal for 1.0.
