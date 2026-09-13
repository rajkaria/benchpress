# Policy packs

A policy pack is a set of enforceable rules for one business function, written as YAML and
enforced by the mutation gate in code. The model never sees a pack as a suggestion: a write that a
pack rule matches is refused before it leaves the process, and the refusal names the rule.

```bash
benchpress policy list                 # bundled packs and their rule counts
benchpress policy show billing         # every rule: id, reason, matcher, approval facts
benchpress policy show ./my-pack.yaml  # a pack file of your own
```

## Bundled packs

| Pack | Rules | What it enforces |
|---|---|---|
| `billing` | 6 | email is draft-only; refunds, credits (credit notes, balance adjustments), and charges need an approval fact; no voids or write-offs; no Stripe deletes |
| `customer-success` | 5 | email is draft-only; chat writes limited to `chat.postMessage`; no CRM delete, archive, merge, or GDPR delete; no money movement |
| `it-offboarding` | 6 | no mail forwarding (addresses, auto-forwarding, forwarding filters); no mailbox purge; no external file shares; accounts never deleted; deactivation needs an approval fact |
| `release-engineering` | 6 | no force-push; no branch-protection changes; no branch or tag deletion; workflows stay enabled; merges and releases need an approval fact |

Every rule is proven by at least one refusal case in the gate corpus
(`src/benchpress/corpus/policy-packs.yaml`), next to the nearest write the same pack still allows.
`tests/test_policy_packs.py` fails if a rule is added without one.

## Using packs

```python
from benchpress.controller import run_trial
from benchpress.gate import Gate
from benchpress.packs import load_policy_pack, load_policy_packs

packs = load_policy_packs(["billing", "customer-success"])   # names, or paths to YAML files
result = await run_trial(..., policy_packs=packs)             # the whole loop
gate = Gate(context, policy_packs=packs)                      # or the gate alone
```

Semantics:

- **Packs only add refusals.** A gate built without packs (`policy_packs=()` is the default)
  behaves exactly as before. With packs, anything the gate allows it would also allow without them.
  `tests/test_policy_packs.py` checks this across the entire corpus.
- **Order.** The control-plane rule runs first. Pack rules run next, in the order the packs were
  given, then the default rules (`method`, `action_class`, `protected`, ...). So a Stripe DELETE under
  `billing` reports `pack:billing.no-money-deletes`, not the generic `method`.
- **Writes only.** Pack rules never apply to GET requests.
- **Refusals name the rule.** The verdict's rule is `pack:<rule id>`, recorded verbatim in
  `Context.refusals`, the tool-bus result (`gate:pack:<rule id>`), and `receipt.json`. The reason
  reads `<human reason> [policy pack rule <rule id>]`, plus the missing approval facts when relevant.
- **Approval facts.** A rule with `unless_approved: [refund_approval]` is lifted when the
  definition of done's `facts` carry that key (case-insensitive) with one of the values `approved`,
  `granted`, `true`, or `yes`. An approval never relaxes a default rule: a refund stays refused when
  the definition of done forbids `create_charge`.
- **The ablation.** Under `no_gate`, the auditing gate records pack refusals in `would_refuse` like
  any other rule.

## Pack format

```yaml
name: billing                      # kebab-case; a bundled pack's name must match its file name
title: Billing operations
description: Free text shown by `benchpress policy show`.
rules:
  - id: billing.refund-requires-approval    # <pack name>.<kebab-case rule>; stable, unique
    reason: Refunds return money to a customer and need an explicit approval.
    match:                                  # every given condition must hold
      providers: [stripe]                   # any of (case-insensitive); omit for any provider
      methods: [POST]                       # any of POST | PUT | PATCH | DELETE; omit for any write
      path: '/v1/refunds\b'                 # regex, searched in the path, case-insensitive
      path_not: '...'                       # regex; the rule does not apply when it matches
      body: '"force"\s*:\s*true'            # regex over the JSON body plus decoded base64url `raw` text
      action_classes: [create_charge]       # any of the gate's classes (send_email, merge_pr, ...)
    unless_approved: [refund_approval]      # optional approval fact keys
```

A matcher needs at least one of `providers`, `methods`, `path`, `body`, or `action_classes`. The
loader rejects unknown fields, invalid regexes, unknown action classes, rule ids that are not
prefixed with the pack name, and duplicate ids.

## Writing a pack

1. Write the YAML (in `src/benchpress/policy_packs/` to bundle it, or anywhere to load it by path).
2. For every rule, add a refusal case and its nearest allowed neighbour to the corpus, with
   `packs: [<name>]` and `expect: {decision: refuse, rule: "pack:<rule id>"}`. A case file can also
   name a pack file by a path relative to the case file (`packs: [../my-pack.yaml]`).
3. Run `uv run benchpress policy show <name>`, `uv run benchpress gate check`, and `uv run pytest tests/test_policy_packs.py`.

## Limits

- Rules match the request shape (provider, method, path, body). They do not know what a record
  *is*: a rule about "money objects" is written as Stripe paths.
- Approval facts come from the definition of done, which a model drafts in P3. A pack is safe
  against an agent that *forgets* approval, but not against one that invents an approval fact. Where
  that matters, populate approvals from a trusted source, or use a rule without `unless_approved`.
- The CLI `benchpress run` does not take a `--policy-pack` flag yet; pass `policy_packs=` to `run_trial`.
