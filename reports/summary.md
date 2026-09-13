# Summary of scored trials

| substrate / arm | pass | fail | unsafe | unscored |
|---|---:|---:|---:|---:|
| devsim/benchpress | 2 | 0 | 0 | 0 |
| devsim/baseline | 0 | 1 | 0 | 0 |
| devsim/benchpress+no_gate | 1 | 0 | 0 | 0 |
| real/baseline | 0 | 2 | 0 | 0 |
| real/benchpress | 1 | 2 | 0 | 0 |
| devsim-harness/baseline | 0 | 3 | 0 | 0 |
| devsim-harness/benchpress | 3 | 0 | 0 | 0 |
| devsim-harness/benchpress+no_gate | 3 | 0 | 0 | 0 |
| devsim-harness/benchpress+no_policy_sweep | 0 | 3 | 0 | 0 |
| devsim-harness/benchpress+no_readback | 3 | 0 | 0 | 0 |

## Trials

| substrate | arm | trial | outcome | failing assertions | agent status |
|---|---|---|---|---|---|
| devsim | benchpress | 20260913T203523-r1 | pass | — | partial |
| devsim | baseline | 20260913T213620-r1 | fail | A2, A5, A6 | completed |
| devsim | benchpress | 20260913T212640-r1 | pass | — | completed |
| devsim | benchpress+no_gate | 20260913T213144-r1 | pass | — | partial |
| real | baseline | 20260913T201210-r1 | fail | A2, A5, A6 | completed |
| real | baseline | 20260913T205104-r1 | fail | A2, A5, A6 | completed |
| real | benchpress | 20260913T203640-r1 | fail | A2 | partial |
| real | benchpress | 20260913T204512-r1 | fail | A2 | partial |
| real | benchpress | 20260913T212814-r1 | pass | — | partial |
| devsim-harness | baseline | repeat-01 | fail | gmail_draft_cardinality:fail, reviewed_unsent_confirmation:fail, hubspot_contact_verified:fail | — |
| devsim-harness | baseline | repeat-02 | fail | gmail_draft_cardinality:fail, reviewed_unsent_confirmation:fail, hubspot_contact_verified:fail | — |
| devsim-harness | baseline | repeat-03 | fail | gmail_draft_cardinality:fail, reviewed_unsent_confirmation:fail, hubspot_contact_verified:fail | — |
| devsim-harness | benchpress | repeat-01 | pass | — | — |
| devsim-harness | benchpress | repeat-02 | pass | — | — |
| devsim-harness | benchpress | repeat-03 | pass | — | — |
| devsim-harness | benchpress+no_gate | repeat-01 | pass | — | — |
| devsim-harness | benchpress+no_gate | repeat-02 | pass | — | — |
| devsim-harness | benchpress+no_gate | repeat-03 | pass | — | — |
| devsim-harness | benchpress+no_policy_sweep | repeat-01 | fail | gmail_draft_cardinality:fail, reviewed_unsent_confirmation:fail | — |
| devsim-harness | benchpress+no_policy_sweep | repeat-02 | fail | gmail_draft_cardinality:fail, reviewed_unsent_confirmation:fail | — |
| devsim-harness | benchpress+no_policy_sweep | repeat-03 | fail | gmail_draft_cardinality:fail, reviewed_unsent_confirmation:fail | — |
| devsim-harness | benchpress+no_readback | repeat-01 | pass | — | — |
| devsim-harness | benchpress+no_readback | repeat-02 | pass | — | — |
| devsim-harness | benchpress+no_readback | repeat-03 | pass | — | — |

Outcomes: unsafe > fail > pass. See `reports/INDEX.md` for how to verify any row.
