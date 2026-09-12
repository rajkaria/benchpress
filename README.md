# Benchpress

> A task-agnostic agent scaffold that makes the same frontier model finish multi-app work,
> obey its authority, and prove it — shipped as a candidate adapter inside
> [ArgaBench](https://github.com/ArgaLabs/arga-twins-benchmark) and graded by ArgaBench.

Built for the Multi-App AI Agent Hackathon (2026-09-13).

## What it does

Benchpress wraps a tool-using model in a fixed loop: **policy sweep → candidate
enumeration + protected set → definition of done → typed plan → execution through a
code-enforced mutation gate → read-back verification → deliverables**. The model never
decides a write that the gate has not approved, and never reports success that provider
state does not evidence.

## Results

_Filled in from `reports/` after the scored runs. Target: ArgaBench zero-pass tasks
(ECOM-02, CRM-02, CRM-05) 0% → 100%; unsafe rate 0% on every task run; same-day
`opus-5-high` baseline on the same harness._

## Docs

- [Build spec](docs/BUILD-SPEC.md) · [Sunday plan](docs/SUNDAY-PLAN.md) ·
  [Research](docs/RESEARCH.md) · Reliability brief (generated after runs)

## Reproduce

See `docs/BUILD-SPEC.md` §11 for the exact harness commands.
