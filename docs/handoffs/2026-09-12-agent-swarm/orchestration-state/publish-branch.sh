#!/usr/bin/env bash
# Orchestrator step run after a worker reports: push its branch and open the PR
# with the worker-authored body. Usage: publish-branch.sh WORKTREE BRANCH BASE TITLE BODY_FILE
set -euo pipefail
wt="$1"; branch="$2"; base="$3"; title="$4"; body="$5"
cd "$wt"
test -z "$(git status --porcelain)" || { echo "worktree not clean:"; git status --short; exit 2; }
test "$(git rev-parse --abbrev-ref HEAD)" = "$branch" || { echo "unexpected branch $(git rev-parse --abbrev-ref HEAD)"; exit 2; }
test -f "$body" || { echo "missing PR body $body"; exit 2; }
git push -u origin "$branch"
if gh pr view "$branch" --json number >/dev/null 2>&1; then
  echo "PR already exists"; gh pr view "$branch" --json number,url,state
else
  gh pr create --base "$base" --head "$branch" --title "$title" --body-file "$body"
fi
gh pr view "$branch" --json number,url,state,headRefOid
