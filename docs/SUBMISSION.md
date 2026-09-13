# Submission kit

## Checklist (B7, 15:45–15:55 PT; hard stop 15:55)

- [ ] Repo public: `github.com/rajkaria/benchpress`. README opens with one-liner, receipt screenshot, results table, reproduce commands.
- [ ] `docs/RELIABILITY-BRIEF.md` ≤ 2 pages, exported to `docs/RELIABILITY-BRIEF.pdf`.
- [ ] Video ≤ 2:00, YouTube unlisted, link in README and form.
- [ ] Brief requirement check: *one useful multi-step agent* ✓ · *≥ 3 external apps* (real Slack + Stripe + HubSpot [+ Gmail]) ✓ · *show how you know it works* (graded trials, baseline, ablations) ✓.
- [ ] Every number in README, brief and video matches `reports/`. Grep for `[X` placeholders: none left.
- [ ] Substrate disclosure present in README, brief and video (Plan B).
- [ ] Prep disclosure present: specs + gate modules written 2026-09-12 (commit timestamps).
- [ ] No secrets: `git log -p | grep -E "sk_test_|xoxb-|pat-|sk-ant-"` returns nothing.
- [ ] CI green on `main`. Tag `v0.1.0-hackathon` pushed.
- [ ] Plan A only: fork link `github.com/rajkaria/arga-twins-benchmark/tree/benchpress` and `git diff --stat` against upstream in the brief.
- [ ] Screenshot of the submission confirmation.

## One-liner

**Benchpress: the reliability layer for AI agents with write access. Same model, different loop:
it reads the rules first, locks look-alike records in code, reads back every write, and proves it
with the benchmark judges' own grader.**

## Description (≤ 200 words), Plan B

> Agents fail at real multi-app work because of the loop around the model, not the model. In
> ArgaBench, three billing and CRM tasks were passed by 0 of 111 frontier runs, and a CI task was
> unsafe in 90 of 111. Benchpress wraps the model in a fixed loop. It sweeps every workspace for
> policies before deciding what "done" means. It enumerates look-alike records and locks them in a
> deny-list enforced by a code gate, not a prompt. It plans only the writes the definition of done
> needs and reads back every one. Customer-facing messages are left as drafts for the owner to
> review. Status is computed from evidence alone. It runs on real Slack, Stripe (test mode),
> HubSpot and Gmail. To show it works, we rebuilt ArgaBench's published scenarios locally and
> graded runs with ArgaBench's own verifier, unmodified. The runs include a same-model baseline on
> the same substrate, 3 repeats, and ablations that remove each guarantee. Results: [fill from
> reports]. Not run on Arga twins; the brief states exactly what is real. Task-agnostic: no code
> references a task, name or domain.

**Plan A swap:** replace "we rebuilt … unmodified" with "we shipped it as a candidate adapter
inside ArgaBench and ran it on Arga twins under the same tools, limits and grader as every
published model", and delete "Not run on Arga twins".

## 60-second live pitch (if asked to present)

1. (10 s) "The best frontier model fails a third of real ops work, and 17% of runs do something unsafe. That's Arga's data."
2. (15 s) "It's not the model. The agent never reads the policy, edits the look-alike account, and reports success from a 200."
3. (20 s) "Benchpress fixes the loop. Policy sweep, locked look-alikes, a code gate on every write, read-back, drafts for review. Here's the receipt." Show receipt.html.
4. (15 s) "Graded by Arga's own verifier: baseline [X], Benchpress [Y], and take the gate out and unsafe comes back. Per-verified-task pricing for teams whose agents touch money and customers."

## Q&A prep

| Question | Answer |
|---|---|
| "Isn't this overfit to the benchmark?" | "Grep the repo for any task id, seeded name or domain: none. The rules are generic operational categories. The ablations show the lift comes from the loop's structure. [If run: we renamed every entity in a seed copy and it still passed.]" |
| "Your simulator could be more lenient than a twin." (Akira) | "Three controls: the grader-contract test shows a known-unsafe trial grades UNSAFE through the same path; the baseline runs on the same simulator, so leniency would help it equally; and the fidelity report compares simulator responses to real Stripe/HubSpot/Slack for every call the playbooks make." |
| "Why not just prompt better?" | "The baseline is the same model with the harness prompt. Safety in a prompt is a suggestion. The gate refuses the merge call before it leaves the process. You can see it in the trace." |
| "What happens when it can't tell two companies apart?" (Phillip) | "It escalates in the originating channel with the candidates and the evidence, and makes no primary write. Over-refusal is measured and reported in the brief." |
| "Did it send the email only once?" (Phillip) | "It sends no email at all when a review policy applies. Drafts are the only write primitive, and send endpoints are a forbidden class in the gate. Every write has an idempotency fingerprint, so a retry can't duplicate." |
| "Would my CSMs use this?" (Userlens) | "The renewal-rescue and billing-contact workflows are CS work. It prepares the change and the customer message, and the account owner approves. The receipt is what a CS lead audits." |
| "Latency / cost?" | "Minutes and a few dollars per task, from the reports. It's for consequential back-office writes, not chat." |
| "What fails?" | Open the brief's honest failures section. Never improvise a number. |
| "What's next / who pays?" (Comma) | "Rehearse: fork live state into twins, converge the plan, then replay it against production with read-back. Price per verified task. First users are teams already on Arga or Lemma." |
| "Did you write code before the window?" | "The specs and the gate module, Saturday, disclosed with timestamps. Everything that runs a task (phases, playbooks, substrate, evals) was built today." |
