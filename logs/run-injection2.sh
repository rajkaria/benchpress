#!/bin/zsh
cd /Users/rajkaria/Projects/benchpress/.claude/worktrees/resume-submission-scope-d29b49
export $(cat logs/devsim.env | xargs)
uv run python -m evals.run --scenario billing-review-injection --agent baseline --substrate devsim --out runs/devsim 2>&1 | tail -3
echo "=== done2 $(date)"
