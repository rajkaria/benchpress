# Outreach to the Arga founders (email + X DM)

Both messages make the same ask: multi-twin Twin Runs for Sunday. Send the email first;
use the DM only as a nudge if there is no reply by Saturday evening.

---

## 1. Email

**To:** founders@argalabs.com
**Cc:** Phillip's personal address if it is visible on his cal.com profile
**Subject:** Multi-App Agent Hackathon — running a candidate adapter inside ArgaBench on Sunday, need multi-twin runs

Hi Phillip, Akira,

I'm registered for Sunday's Multi-App AI Agent Hackathon. I'm building the submission
*inside* ArgaBench: a task-agnostic candidate adapter (one new profile in
`model_matrix.json`, one new branch in `agents/runner.py`, graders/gateway/seeds untouched)
that wraps Claude Opus 5 in a fixed control loop — policy sweep before intent, candidate
enumeration with a code-enforced protected set, a mutation gate in front of `provider_api`,
read-back verification of every write, and a deliverables checklist. Target: the three
tasks at 0/111 (ECOM-02, CRM-02, CRM-05) plus DEV-03's unsafe rate, graded by your
`argabench_fair` verifier with 3 repeats each and a same-day `opus-5-high` baseline.

The blocker is plan limits. Every ArgaBench task provisions 3–5 twins per run, and the Free
plan allows one twin per run with a 10-minute TTL. To run the scored trials I need
multi-twin Twin Runs with ~60-minute TTLs, roughly 40 runs between 08:30 and 15:00 PT on
Sunday.

Could you enable hackathon-tier access on my workspace for the day? Workspace email:
rajkaria67@gmail.com. If that's not possible, I'll take Pro for a month — I just want to
avoid finding out at 08:30 that checkout is the only path.

I'll share the fork, the semantic reports, and a reliability brief in your report format
whether or not it places. If it works, I'd like to PR the adapter upstream as a community
profile.

Thanks,
Raj
rajkaria67@gmail.com · github.com/rajkaria

---

## 2. X DM

Verify the handles before sending. They are not recorded in `docs/RESEARCH.md`. Akira's
GitHub handle is `tonghx`; check argalabs.com and the Lemma/Comma announcement posts for
the real X accounts. If DMs are closed, reply to their latest ArgaBench post with the
short version and point them at founders@.

Written to sound like a person typing on a phone, not a cover letter. Read it out loud
before sending; if a line feels stiff, cut it.

**To Phillip (CEO — he cares about the messy-enterprise cases):**

> hey Phillip, Raj here. doing the hackathon tomorrow.
>
> heads up on what I'm building since it's your repo: instead of a standalone demo I'm
> shipping my agent as a candidate adapter inside ArgaBench. one profile in the model
> matrix, one branch in runner.py, graders and seeds untouched. going after ECOM-02,
> CRM-02 and CRM-05, the three nobody has passed yet.
>
> only problem is I'm blocked before I start. free plan gives me one twin per run and
> those tasks provision 3-5. any chance you can open up my workspace for sunday?
> rajkaria67@gmail.com. if it's easier I'll just pay for Pro, I only want to know that
> before 8:30am rather than during it.
>
> longer version is in an email to founders@. either way I'll send you what I find.

**To Akira (CTO — he wrote the benchmark; talk to him about the benchmark):**

> hey Akira, Raj. I'm in for the hackathon tomorrow.
>
> been living in arga-twins-benchmark this week and decided to build my submission inside
> it rather than next to it. a candidate adapter, one new profile, one branch in
> runner.py, nothing touched in the graders or the gateway. pointing it at the three
> 0/111 tasks and at DEV-03, where 90 of 111 trials went unsafe because the agent merged
> the PR.
>
> I'm stuck on twins though. free is one per run, tasks need 3-5. could you flip
> rajkaria67@gmail.com to hackathon access for sunday? ~40 runs between 8:30 and 3. happy
> to buy Pro if that's the normal path, just want it settled tonight.
>
> emailed founders@ with the full detail. I'll send you the fork and the numbers whatever
> happens.

**Short version, for a public reply or a cramped DM box:**

> hey Phillip, Raj here. I'm doing the hackathon tomorrow and building my submission as a
> candidate adapter inside ArgaBench, aiming at ECOM-02 / CRM-02 / CRM-05. free plan caps
> me at one twin per run and those need 3-5, so I'm stuck. any chance of hackathon access
> on rajkaria67@gmail.com for sunday? full detail is in an email to founders@. otherwise
> I'll just buy Pro.

---

**Fallback, if no reply by 08:30 PT Sunday:** Settings → Upgrade subscription → Pro
($1,250/mo, self-serve). Cancel after the run.

**Follow-up after the hackathon (regardless of result):** send the fork link, the
`reports/` folder, and `docs/RELIABILITY-BRIEF.md`; offer the upstream PR.
