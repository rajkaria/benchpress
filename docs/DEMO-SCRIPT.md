# Demo video: recording script (≤ 2:00)

Target 1:50 of voice at a calm pace; the last ten seconds are the results card. Every number below
exists in `reports/summary.md`, `reports/compare.md` or `reports/gate-replay-historical.md`.
Judges score demo clarity at 10%, and the video also carries usefulness (20%) and originality (15%).

## Before you record

- 1920×1080, 30 fps. Terminal at 18 pt, dark theme, prompt shortened to `❯`. Browser at 125% zoom,
  bookmarks bar hidden, notifications off.
- Open these tabs in order: (1) Slack scratch workspace `#commerce-ops`; (2) Gmail scratch inbox
  with the *Customer communication review policy* message; (3) HubSpot companies list filtered to
  "Northwind"; (4) the receipt page `runs/real/billing-review-routable/benchpress/20260913T212814-r1/receipt.html`
  (`benchpress receipt <trial>/receipt.json --html`); (5) Stripe test-mode customer `cus_VFqeWBoxrE0QGA`;
  (6) Gmail **Drafts**; (7) a terminal showing `cat reports/summary.md`; (8) `reports/gate-replay-historical.md`.
- If the scratch apps have been reset, re-seed first: `uv run python -m evals.seed --task ECOM-02
  --apps slack,stripe,hubspot,gmail --verify`, then run one Benchpress trial so the after-state is live.
- Cuts, not live typing. Say the line, then cut to the next shot. Burn captions for every number
  (judges may watch muted).
- Pre-rendered 1080p slides for every non-app shot exist as B-roll (title card, 0/111 card,
  the "two reasons" split, the after-state panels, the 0/3 vs 3/3 card, the ablation and gate-replay
  cards, the results card); ask for the `slides/` folder if you want them.

## Shot list

| t | Show | Say |
|---|---|---|
| 0:00–0:10 | Slack `#commerce-ops`. Scroll to Marlon Price's message: *"Northwind Studio asked for renewal notices to move to its accounts-payable address…"* | "A customer asks ops to move their renewal notices to accounts payable. That's a Slack message, a billing system, a CRM and an inbox. It looks like a thirty-second job." |
| 0:10–0:19 | Card: **0 / 111** · ArgaBench ECOM-02 · 37 frontier configurations. Small print: *published result, context only*. | "Arga Labs gave this exact job to thirty-seven frontier model configurations. None of them passed. Not once in one hundred and eleven tries." |
| 0:19–0:29 | Split screen: Gmail policy email (*"…require a customer confirmation reviewed by the account owner before sending"*) and HubSpot companies: **Northwind Studio** next to **Northwind Studios Prospect**, *Northwind Studio — Operations*, *… Prospect — Archive*. | "Two reasons. There's a policy email nobody reads, saying customer confirmations need owner review. And there's a look-alike prospect that must not be touched." |
| 0:29–0:33 | Title card: **Benchpress. Same model. Different loop.** with the P0–P7 phase strip. | "Benchpress doesn't swap the model. It fixes the loop around it." |
| 0:33–0:48 | `receipt.html`, three cuts: **② Policies found** (the quote in amber) → **③ Candidates** with the chosen row green and six protected rows red, the 🔒 protected-set box → **⑤ Plan & execution**: `POST /v1/customers/cus_…` **ALLOWED** 200, read-back ✓; `PATCH /crm/v3/objects/contacts/…` **ALLOWED** 200, read-back ✓; one **REFUSED** duplicate repair write; the draft; two Slack posts. | "First it reads every workspace for rules. It enumerates every look-alike and locks them in a deny-list, enforced in code. It writes down what 'done' means, plans only those writes, and every write passes a gate and is read back before anything counts." |
| 0:48–1:07 | Real apps, quick cuts (4 s each), from the `billing-review-routable` trial: Stripe customer showing `ap@northwindstudio-example.com` → HubSpot contact with the same address → Gmail **Drafts** with the confirmation (not Sent) → Slack: the review request and the update posts. Lower third: *"published seed, hosts rewritten to -example.com so HubSpot accepts them · 13 of 13 assertions"*. | "On real Slack, Stripe, HubSpot and Gmail: billing contact changed in Stripe and HubSpot and read back, one unsent confirmation draft, one review request to the account owner. Nothing sent. Six look-alike records untouched. Thirteen of thirteen assertions." |
| 1:07–1:24 | Terminal: `cat reports/summary.md`, then a card **0 / 3** (stock loop) vs **3 / 3** (Benchpress). Lower third, on screen the whole shot: *"Local grader-faithful twins · ArgaBench runner and semantic grader unmodified (4a81785) · baseline = the harness's stock tool loop · same model, prompt, tools, 160/40 calls, 1,800 s · no leaderboard claim"*. | "How do we know it works? We rebuilt Arga's twins locally and ran Arga's own unmodified runner and grader over them, with Arga's stock tool loop as the baseline: same model, same prompt, same tools, same limits. Baseline: zero of three. Benchpress: three of three." |
| 1:24–1:36 | Ablation card, three bars: `no_policy_sweep` **0 / 3** (red) · `no_gate` 3 / 3 · `no_readback` 3 / 3. Caption: *"policy sweep off → the unsent draft and the owner review vanish, the same two misses as the stock loop"*. | "Switch the policy sweep off and the two deliverables every model missed vanish again: zero of three. The gate and read-back didn't change this task's score, and we say so." |
| 1:36–1:46 | `reports/gate-replay-historical.md`: **62** mutating writes from Arga's recorded frontier trials → **15 refused**, 8 of 8 trials, all by the `protected` rule. | "Where the gate earns its keep: every write from Arga's own recorded frontier trials, replayed through it. Fifteen of sixty-two would have been refused, each naming a protected look-alike." |
| 1:46–1:56 | Results card: the README §11 table · `github.com/rajkaria/benchpress` · `pip install benchpress-agent` · **Same model. Same limits. Different loop.** | "Same model. Same limits. Different loop. Benchpress. Everything's reproducible from the repo, and the brief says exactly what is real." |

## Rules for the cut

- Never round up. If a shot's number is not in `reports/`, cut the shot.
- The disclosure lower-third stays on screen for the whole proof shot (1:07–1:24).
- Say "Arga's own unmodified runner and grader on local twins"; never "passed ArgaBench" or
  "on the leaderboard". The 0/111 card is context, not our comparator.
- Keep the published-seed HubSpot miss in the results card. Judges reward the disclosed failure more than a clean cut would.

## Receipt page visual spec (`receipt.html`)

This is the one UI judges see. It must look shipped.

- Single self-contained HTML file with no external requests. Font stack: `ui-sans-serif, Inter,
  system-ui` for text, `ui-monospace, JetBrains Mono` for ids and numbers.
- Palette: ground `#0B0D10`, panel `#12161B`, hairline `#1F2630`, text `#E6EAF0`, muted `#8B96A5`,
  allowed/verified `#3FB950`, refused/protected `#F85149`, policy/attention `#D29922`, accent `#58A6FF`.
- Layout: max-width 1080 px, centered, 8 px spacing grid. Sticky header with task one-liner,
  status pill (`COMPLETED` green / `PARTIAL` amber / `ESCALATED` blue), calls used `n/160`,
  elapsed, cost.
- Sections in order, each a panel with a numbered header:
  1. **Request**: originating channel, reporter, requested change, prohibitions as chips.
  2. **Policies found**: provider icon letter, resource ref, verbatim quote in a left-bordered amber block, kind chip.
  3. **Candidates**: table (provider, type, id, display, lifecycle, domain). Chosen row has a green left border and the evidence list. Protected rows are red with a lock glyph 🔒.
  4. **Definition of done**: checklist rows with ✓/✗ computed from evidence, never from the plan.
  5. **Plan & execution**: one row per action: kind chip, `METHOD path`, gate verdict pill, HTTP status, read-back expected vs observed (mono, diff-highlighted).
  6. **Refused**: every gate refusal with rule and reason. An empty state reads "No refusals needed."
  7. **Deliverables**: channel update, draft (with an **UNSENT** badge), review record, each with its ref.
  8. **Final JSON**: collapsible `<details>`, pretty-printed.
- Screen-recording legibility: body 15 px, table text 14 px, headers 20 px/600. No text under 12 px.
