# Email to Arga founders

**To:** founders@argalabs.com
**Subject:** Multi-App Agent Hackathon — running a candidate adapter inside ArgaBench on Sunday, need multi-twin runs

Hi Phillip, Akira,

I'm registered for Sunday's Multi-App AI Agent Hackathon. I'm building the submission
*inside* ArgaBench: a task-agnostic candidate adapter (new profile in `model_matrix.json`,
new branch in `agents/runner.py`, nothing else touched) that wraps Claude Opus 5 in a fixed
control loop — policy sweep before intent, candidate enumeration with a code-enforced
protected set, a mutation gate in front of `provider_api`, read-back verification, and a
deliverables checklist. Target: the three tasks with 0/111 passes (ECOM-02, CRM-02, CRM-05)
plus DEV-03's unsafe rate, graded by your `argabench_fair` verifier with 3 repeats each and
a same-day `opus-5-high` baseline.

The blocker is plan limits. Every ArgaBench task provisions 3–5 twins per run, and the Free
plan allows one twin per run with a 10-minute TTL. To run the scored trials on Sunday I need
multi-twin Twin Runs with ~60-minute TTLs for roughly 40 runs between 08:30 and 15:00 PT.

Could you enable hackathon-tier access on my workspace for Sunday? Workspace email:
rajkaria67@gmail.com. If that's not possible, I'll take the Pro plan for a month — I just
want to avoid finding out at 08:30 that checkout is the only path.

I'll share the fork, the semantic reports, and a reliability brief in your report format
whether or not it places. If it works, I'd like to PR the adapter upstream as a community
profile.

Thanks,
Raj
rajkaria67@gmail.com · github.com/rajkaria

---

**Fallback, if no reply by 08:30 PT Sunday:** Settings → Upgrade subscription → Pro
($1,250/mo, self-serve). Cancel after the run.

**Follow-up after the hackathon (regardless of result):** send the fork link, the
`reports/` folder, and `docs/RELIABILITY-BRIEF.md`; offer the upstream PR.
