# Resume through Harness

The user required agent-swarm workflows to run through Harness and explicitly
approved the configured `gpt` route receiving relevant plugin instructions and
Harness source/plan/workflow files. Do not resume the direct Claude session
`4b0d4271`: that host bypassed Harness and is stopped.

Host integration work is committed at `556aa08` on `feat/harness-swarm-execution`
in `/home/fearsidhe/projects/harness/.worktrees/harness-swarm-execution`.
[PR 62](https://github.com/c-daly/harness/pull/62) is stacked on still-open PR 59.
Both full host suites passed: 2,415 passed, 7 skipped, 6 warnings on Python
3.12.14 and 3.13.15. Ruff, build and installed-wheel smoke passed.

Read that checkout's `plugins/agent-swarm-runner/README.md` and
`docs/handoffs/2026-09-13-harness-swarm/native-runner.md` with
`native-evidence.json`. The installed agent-swarm runner still computes queue
eligibility and builds prompts. Harness owns native inference, child sessions,
tools and journals. This is a partial manifest-runner binding, not a complete
port of the Claude router hooks, identities, Serena protocol or memory plugin.

## Task 9: partial work, not acceptance

Three real GPT attempts selected task 9 through the saved plugin queue:

- v1 root `b5ae710b698c43b1b60e6fa689844d23`: child stopped at its effective
  4,096-token cap without changing files. The core fix now exposes validated
  limits in native agent definitions; the next attempts used 16,384 tokens and
  48 child iterations, with the parent budget still enforced.
- v2 root `189158146f2f4ca18511e67ee0704019`: partial code, 23 passing focused
  tests, but independent review found broken CLI wiring, stale repair authority
  and four Ruff failures. Rejected candidate checkpoint: `73717b5`.
- v3 root `208e78c1af9c49088f40121c2f8baee8`: corrected all three frozen operator
  failures. Independent reruns: 3 operator checks passed, 23 focused tests
  passed, Ruff passed. Corrected WIP checkpoint: `4cf2819`.

Task worktree:
`/home/fearsidhe/projects/harness/.worktrees/harness-remaining-roadmap/9-session-integrity-authorized-repair-and-lifecycle-cli`.
It is clean at `4cf2819`, unpublished and unaccepted. The implementation still
lacks storage limits, real lifecycle CLI operations, session-listing integration,
additional integrity coverage and docs. The CLI has help/wiring with unfinished
operation bodies. Do not release dependent work or publish it as complete.

The v2 frozen operator checks remain at
`.worktrees/tmp/harness-swarm-native-v2/operator_review/test_candidate_review.py`
relative to the original Harness root. All native journals, prompts and result
reports remain under `.worktrees/tmp/harness-swarm-native-v1`, `-v2`, `-v3`.
The operator, not the worker, ran those frozen checks. This is supervised
correction evidence, not resident-led self-improvement.

## Queue continuation

No worker is live. The runner still records task 9 as `spawned` under the final
Harness root, pending review/continuation. Its previous `last_error` describes
the v2 rejection; the v3 evidence above supersedes those three defects, but the
full task is still incomplete. Preserve both checkpoints and report the actual
remaining requirements to agent-swarm through its public runner API before a
retry. Two retries have been consumed, so inspect the plugin's resulting
retry/escalation request and model choice. Do not substitute a different remote
provider silently; the native binding uses an explicit catalog inference alias.

The driver refuses a dirty worktree and writes into a fresh evidence directory.
Never reset, clean, force-remove or replay effects to satisfy that preflight.
Each attempted worker needs independent requirement checks, not just successful
model termination or its self-authored tests. Native attempts ended voluntarily
with significant requirements omitted, despite remaining iteration capacity.

Earlier task worktrees and open PRs 59–61 are preserved. PR 60 still had a
blocking workflow-reference review issue and PR 61 a formatting follow-up at the
last inspection; recheck their live heads before acting. The original 24-task
manifest remains authoritative for queue decomposition. Do not run the plugin's
automatic merge or worktree-cleaning commands; PR merges remain the user's.
