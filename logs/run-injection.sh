#!/bin/zsh
cd /Users/rajkaria/Projects/benchpress/.claude/worktrees/resume-submission-scope-d29b49
export $(cat logs/devsim.env | xargs)
for spec in "benchpress " "baseline " "benchpress no_gate"; do
  set -- ${=spec}
  ab=""; [[ -n "$2" ]] && ab="--ablations $2"
  echo "=== $1 $ab $(date)"
  uv run python -m evals.run --scenario billing-review-injection --agent $1 ${=ab} --substrate devsim --out runs/devsim 2>&1 | tail -4
done
echo "=== done $(date)"
