# Benchpress

> **The reliability layer for AI agents with write access.** Same model, different loop:
> Benchpress reads the workspace's rules first, locks look-alike records in code, reads back
> every write, leaves customer messages for owner review, and proves it with the benchmark
> judges' own grader.

<!-- B5: replace with a receipt.html screenshot (docs/img/receipt.png) -->

Built for the Multi-App AI Agent Hackathon, 2026-09-13.

## Why

Agents with write access fail at real multi-app work, and the agent can't see the failure. In
[ArgaBench](https://www.argalabs.com/benchmark), three billing and CRM tasks were passed by
**0 of 111** frontier runs, and a CI task was **unsafe in 90 of 111**: the agent merged a PR it
was told not to. The failures come from the loop around the model. Agents don't read policy,
they edit the look-alike account, and they report success from an HTTP 200.

## How it works

```
Slack request
  → P0 orient          parse the request, map providers, read the originating channel
  → P1 policy sweep    read every workspace for review/approval/embargo rules
  → P2 resolve         enumerate look-alikes, choose one target with cited evidence, lock the rest
  → P3 done-as-data    typed definition of done: end state, deliverables, forbidden classes
  → P4 plan            only writes that satisfy the definition of done
  → P5 execute         every write passes the mutation gate (code, not prompt), then read-back
  → P6 verify          end state, cross-system consistency, protected-set audit, one repair round
  → P7 deliver         channel update, unsent draft + owner review record, evidence-only status
```

The mutation gate refuses DELETE, sends, charges, merges, protected-record writes, out-of-scope
providers, unplanned writes, smuggled fields, external destinations, control-plane paths and
replayed fingerprints. See [`src/benchpress/gate.py`](src/benchpress/gate.py).

## Results

<!-- B5: fill from reports/compare.md. Every number links to its report file. -->

| Task | Baseline (same model, same substrate) | Benchpress | Published, 37 configs |
|---|---|---|---|
| ECOM-02 billing contact change under review policy | _pending_ | _pending_ | 0/111 pass |
| DEV-03 flaky test quarantine, do not merge | _pending_ | _pending_ | 90/111 unsafe |

<!-- Plan B wording; swap for Plan A per docs/SUBMISSION.md -->
ArgaBench's published ECOM-02 seed, loaded into real Slack, Gmail, HubSpot and Stripe (test mode).
The baseline is ArgaBench's stock Anthropic adapter (same model, prompt, tools and limits). Both
are scored on ArgaBench's pass/unsafe criteria, ported with citations. See
[`docs/RELIABILITY-BRIEF.md`](docs/RELIABILITY-BRIEF.md) for exactly what is real, repeats,
ablations and honest failures.

## Apps

Slack · Gmail · HubSpot · Stripe · GitHub · Linear. Real-app mode runs the identical agent on real
Slack, Stripe (test mode), HubSpot and Gmail.

## Reproduce

```bash
uv sync --group dev
uv run pytest -q                      # gate, DoD rules, playbooks, real-app gateway, assertion units
cp .env.example .env                  # add ANTHROPIC_API_KEY
# B4: exact eval commands land here
```

## Docs

[Vision](VISION.md) · [Reliability brief](docs/RELIABILITY-BRIEF.md) · [Build spec](docs/BUILD-SPEC.md)
