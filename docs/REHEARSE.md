# Rehearse and replay

> The model never decides a production write live.

Benchpress already refuses unsafe writes in code (the gate) and reads every write back. Rehearse
moves the model's judgment out of production entirely: the request is run several times against
isolated copies of the state, and only a plan that every run agreed on is replayed against
production, by code, with no model in the loop.

```
request ──▶ rehearse(n)  ──▶ fresh stage #0 ─ run_trial ─┐
                           ├▶ fresh stage #1 ─ run_trial ─┼▶ verdict ──▶ rehearsal.json
                           └▶ fresh stage #2 ─ run_trial ─┘               │
                                                                          ▼
                                    production ◀── replay(rehearsal) ── converged plan only
                                                   gate + id substitution + read-back
```

Module: `benchpress.rehearse` (ships in `benchpress-agent`). Twin-backed stages live in
`devsim/rehearse_stage.py` (not shipped).

## 1. Rehearse

```python
from benchpress.rehearse import rehearse

rehearsal = await rehearse(request, ["slack", "gmail", "hubspot", "stripe"], stage_factory, n=3)
print(rehearsal.summary())
rehearsal.write_json(Path("rehearsal.json"))
```

- `Stage` is a protocol: `execute_tool(tool_name, tool_input)`, `snapshot() -> dict` (the JSON state
  of everything the request can touch), `aclose()`.
- `StageFactory` is `async () -> Stage`, and every call must return a **fresh, isolated** copy.
  `devsim.rehearse_stage.twin_stage_factory(providers, seed)` builds new twin stores per call and
  serves them in-process through `httpx.ASGITransport` behind the real `RealAppGateway`. It binds
  no socket and reads no environment.
- Each run is a normal `run_trial`: policy sweep, gate, read-back, deliverables. The model is
  whatever `model_factory(run_index)` returns, or the configured model when you omit it.

Each run produces a `RunRecord`:

| field | meaning |
|---|---|
| `state_hash` | sha256 of the canonical JSON of the normalized final snapshot |
| `refusals` | every gate verdict that refused a write |
| `writes` | the typed plan: executed writes in order, each with the exact `Action`, gate `fingerprint`, `normalized_fingerprint`, the generated ids its response carried (`created`), and its normalized read-back |
| `failed_writes` | writes that reached the provider and failed |
| `generated_ids` | `<id:k>` → the concrete id this run saw |

The writes come from the controller's own ledger (the order and outcome of every gated call),
paired one-to-one with the calls the stage actually received. If the two disagree, the run is
marked `trace_consistent: false`.

## 2. The convergence rule

A rehearsal is `converged` only when **all** of these hold:

1. every run's `state_hash` is equal;
2. the gate refused nothing in any run;
3. every run executed the same sequence of `normalized_fingerprint`s;
4. no write failed, and the ledger matched the recorded calls in every run.

Otherwise `divergence` says what went wrong. It names the runs that differ from run 0, the first
differing state path (for example `hubspot.companies.<id>.description`), the first differing write
on each side, and every refusal with its rule. `Rehearsal.summary()` prints all of it. A diverged
rehearsal carries no plan.

## 3. Normalization

`Normalizer` holds the rules. They are explicit, configurable and unit-tested
(`tests/test_rehearse.py`):

- **Timestamps.** A scalar under a time key becomes `<time>`. Time keys are `created`, `updated`,
  `timestamp`, `internalDate`, HubSpot's `createdate` and `lastmodifieddate`, and anything ending
  in `_at`, `At`, `_time` or `_timestamp`. Any string that is a full ISO-8601 datetime also becomes
  `<time>`, wherever it appears. Plain dates (`2026-10-01`) are left alone.
- **Generated ids.** An id is a scalar under an id key (`id`, `ts`, `thread_ts`, `*_id`, `*Id`,
  `*_ts`), or a mapping key that looks like an identifier (contains a digit, at least 4 characters).
  An id counts as *generated* when it shows up in a write response or in the final snapshot but not
  in the baseline snapshot. Generated ids are numbered `<id:1>`, `<id:2>`, … in order of first
  appearance: write responses in execution order first, then the final snapshot (sorted keys). They
  are replaced wherever they occur: as a whole value, as a mapping key, or as a delimited token
  inside a longer string (`xitm_1` is left alone; `see itm_1.` is rewritten).

Two runs that created the same things under different concrete ids therefore hash equal. One
consequence: a business datetime written by the plan is also collapsed in the state hash. The write
fingerprints are not time-normalized, so they still tell those runs apart.

## 4. Replay

```python
from benchpress.rehearse import Rehearsal, replay

receipt = await replay(Rehearsal.read_json(Path("rehearsal.json")), gateway.execute_tool)
print(receipt.summary())
```

- It refuses a rehearsal that did not converge, and makes no calls in that case.
- It rebuilds the gate from the rehearsal's `gate_context` (targets, protected set, definition of
  done, policies, candidates, prompt), so every write passes the same code-enforced rules again.
- It executes the writes in order through the `ToolBus`. Ids generated earlier in the plan (a
  draft id, a posted message `ts`) are swapped for the ids **this** replay received, using each
  write's `created` field paths.
- It reads back every write that defines a read-back. Each declared field must hold the written
  value, **and** the whole record must equal the rehearsed record (normalized; an id placeholder
  this replay has not bound matches any scalar).
- It stops at the first refusal, failed write or mismatch. `ReplayReceipt` is honest about how far
  it got: `executed` counts writes that reached the provider, including one that landed and then
  failed its read-back. `stopped_at` is that write's index, and later writes are listed as not
  attempted.
- With `snapshot=` (twins only), it also compares the final normalized state hash against the
  rehearsal's.

## 5. CLI

```bash
benchpress rehearse --providers slack,stripe --prompt-file request.txt --n 3 \
    --stage devsim.rehearse_stage:stage_from_args --seed seed.json --out rehearsal.json
benchpress replay rehearsal.json --out replay-receipt.json
```

- `--stage module:callable` is required. The callable receives `(providers, seed_file)` and returns
  a `StageFactory`. `--model-factory module:callable` and `--playbooks module:callable` are
  optional plug points (the tests use them to inject a scripted model).
- `rehearse` exits 0 when the rehearsal converged, 1 otherwise.
- `replay` targets `benchpress.realapp.gateway_from_env(providers)`. That means `DEVSIM_<P>_URL`
  twins, or real hosts only when `BENCHPRESS_SCRATCH_OK=1` (the gateway's scratch guard). It exits
  0 only on `completed`.

In this repository the CLI has been exercised **only in tests, against in-memory stages**.

## 6. Limits (what is and is not real today)

- **Forking live production state is not implemented.** The caller supplies the seed a stage is
  built from. Mirroring production into twins (and keeping pre-existing ids identical, which replay
  relies on) is a seeding step outside this module.
- **Only snapshotted state is compared.** A write to state the stage's `snapshot()` does not
  include cannot cause a divergence. Twins that change state on reads (for example Stripe
  `generic_resources` materialized by unknown list routes) will show up as divergence.
- **Replay cannot see production drift outside written records.** Its tamper detection is the
  post-write read-back of the records the plan touches. A change elsewhere that would have changed
  the model's decision is not detected. Keep the rehearse-to-replay window short.
- **Gate context is frozen at rehearsal time.** Replay does not re-run the policy sweep.
- **Budget.** Replay runs under the P5 tool budget (40 calls, less the delivery reserve), which is
  roughly 20 writes with read-backs. Past that it stops honestly with `tool budget exhausted`.
- **Runs are sequential.** N runs cost N times the model and tool budget of a single run.
- **Convergence is evidence, not proof.** Three agreeing runs of a stochastic model make a
  divergent fourth less likely. They do not rule it out.
