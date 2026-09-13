# Audit export

`benchpress receipts export` turns one or many receipts into an audit log with one row per write
attempt: every write the mutation gate judged, whether it allowed or refused it.

```bash
benchpress receipts export runs/ --format csv --out audit.csv      # every receipt.json under runs/
benchpress receipts export runs/a/receipt.json runs/b --format jsonl  # files and directories, to stdout
```

Directories are searched recursively for `receipt.json`; the same receipt named twice is exported once.
A path that does not exist, or a JSON file that is not a `benchpress-receipt/1`, exits `2`.

## What an auditor can answer

| Question | Columns |
|---|---|
| Who changed which customer record? | `requested_by`, `agent_model`, `trial_id`, `provider`, `path`, `target_refs` |
| What changed? | `action_kind`, `method`, `fields_changed` (field names only, never values) |
| When? | `timestamp`, `sequence`, `phase` |
| Was it allowed, and under which rule? | `gate_decision`, `gate_rule`, `gate_reason` |
| Did it actually reach the provider? | `executed`, `status_code`, `provider_ok` |
| Is there evidence it took effect? | `readback`, `readback_checks`, `dod_evidence` |
| Why was it done? | `dod_items` (the definition-of-done items the write claims to satisfy) |
| Under which policy? | `policies` (quotes of the discovered policies that govern it) |
| How did the run end? | `final_status` |

Typical filters: every refused write (`gate_decision == refuse`), every executed write without a
matching read-back (`executed == true and readback != match`), every write governed by a review policy
(`policies` not empty), or every change to one record (`target_refs` contains it).

## Columns

The order is a contract. New columns are only ever appended; existing ones are never renamed or
reordered. In CSV, lists are joined with ` | `, booleans are `true`/`false`, and a missing value is an
empty cell. In JSONL every row carries every key, in this order, with lists as arrays and missing values
as `null`.

| # | Column | Meaning |
|---|---|---|
| 1 | `timestamp` | ISO-8601 UTC time the gate judged the write (receipt `gate_decisions[].at`; older receipts: the ledger entry's `at`, then the receipt's `generated_at`, else empty) |
| 2 | `trial_id` | The run |
| 3 | `receipt` | Path of the receipt the row came from |
| 4 | `sequence` | Ledger sequence of the provider call; empty when the write never reached the tool bus (dropped while planning) |
| 5 | `phase` | Loop phase: `P4` plan dry-run, `P5` execute, `P6` repair, `P7` deliver |
| 6 | `requested_by` | The reporter parsed from the request (`request.frame.reporter`) |
| 7 | `agent_model` | Model that planned the run (`meta.model`) |
| 8 | `provider` | System of record written to |
| 9 | `method` | HTTP method |
| 10 | `path` | Request path (identifies the record) |
| 11 | `action_id` | Plan action id |
| 12 | `action_kind` | `update`, `create`, `comment`, `draft`, `message` |
| 13 | `target_refs` | Records the action declares it targets (`type:id` and display) |
| 14 | `fields_changed` | Field names the action declares it writes; the gate refuses a body that touches any other field |
| 15 | `gate_decision` | `allow` or `refuse` |
| 16 | `gate_rule` | The refusing rule (`protected`, `action_class`, `pack:<pack>.<rule>`, ...); empty when allowed |
| 17 | `gate_reason` | The gate's reason text |
| 18 | `executed` | The request left the process and a provider answered |
| 19 | `status_code` | Provider status code when executed |
| 20 | `provider_ok` | Provider reported success when executed |
| 21 | `readback` | `match` (every read-back check agreed), `mismatch`, `not_checked` (executed, no read-back check), `not_executed` |
| 22 | `readback_checks` | `readback:<action>:<field>=match\|mismatch`, latest evidence per check |
| 23 | `dod_items` | Definition-of-done items the action claims: `end_state[i]: <provider> <record> <field>` or `deliverable:<kind>` (never the expected value) |
| 24 | `dod_evidence` | For executed writes, the final evidence on those items: `<item>=match\|mismatch` |
| 25 | `policies` | `[kind] policy:<provider>:<ref>: <quote>` for each policy cited by a deliverable the write satisfies, or whose `applies_to` names one of its targets |
| 26 | `final_status` | The run's status computed from evidence: `completed`, `partial`, `escalated` |

## What the log is built from

Receipts record `gate_decisions` (phase, timestamp, the exact action, the verdict) for every write the
gate judged, including writes refused during the plan dry-run that never reached a provider. Each
decision is paired with its ledger entry (same action id and outcome) for execution facts, and with the
receipt's evidence, definition of done and policies for the rest. Receipts written before
`gate_decisions` existed still export: rows come from gated ledger entries plus refusals that never
reached the ledger, with the plan filling in kind, fields, targets and definition-of-done items.

## Limits

- Values are deliberately absent: the log says *which* fields changed, not *to what*. The receipt holds
  the values for anyone entitled to read it.
- A policy is linked to a write through the definition of done (`because`) or the policy's `applies_to`.
  A policy the run discovered but never tied to a deliverable or target is in the receipt, not the row.
- `mcp-guard` JSONL receipts are a different format and are not read by this command.
- Rows under the `no_gate` ablation show `allow` with the gate's would-be refusals only in the receipt's
  `would_refuse`.
