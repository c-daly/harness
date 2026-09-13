# Workflow status: Harness remaining roadmap (agent-swarm pipeline)

Living record maintained by the orchestrator session `4b0d4271` (started
2026-09-12). Machine-readable state is in `orchestration-state/` (runner JSON
`harness-remaining-roadmap.json`, initial pending prompts, spawn timestamps).
A generated plan or manifest is not an executing workflow; only the dispatch
table below records actual worker registration, dispatch and outcomes.

## Artifacts

| Artifact | Path | Status |
|---|---|---|
| Brief | `remaining-work-design.md` | provided (`f8f7284`) |
| Inventory and crosswalk | `remaining-work-inventory.md` | generated, committed `212ce2b` |
| Implementation plan (24 tasks) | `remaining-work-plan.md` | generated, committed `212ce2b` |
| Manifest | `remaining-work-manifest.yaml` | generated, runner `load` OK, committed `212ce2b` |
| Worker protocol | `worker-protocol.md` | committed `9b88255` |
| Spawn preface | `orchestration-state/spawn-preface.md` | committed `9b88255` |
| Runner state | `orchestration-state/harness-remaining-roadmap.json` | live |

## Pipeline stages

| Stage | Result |
|---|---|
| 0 Inventory | Complete. Source-verified crosswalk of roadmap M0–M6 and hardening tasks 1–19C. |
| 1 Plan | Complete. 24 tasks, 12 with explicit dependencies. |
| 2 Manifest | Complete. `project: harness-remaining-roadmap`, `base_branch: main` (fast-forwarded to `4c09803`). |
| 3 Orchestrate | Started 2026-09-12. 23 worktrees created under `.worktrees/harness-remaining-roadmap/`; Task 1 path linked to the existing `.worktrees/source-authorship`. |
| 4 Status export | This file; updated at every dispatch and completion. |

## Dispatch record

Worker ids are router registrations (`router__register_agent`, type
`implementer`, roles `editor`, `shell_full`). Spawned through the Agent tool
with the router briefing, the orchestrator preface and the runner prompt.

| Task | Branch | Worker | Spawned | Outcome | Branch/PR | Evidence |
|---|---|---|---|---|---|---|
| 1-source-authorship-completion | `feat/source-authorship` (existing worktree) | `1-source-authorship-completion-w1` | batch 1 | completed: 3 commits (`7577abf`, `5c1b985`, `e385d49`); partial-limit defect fixed; 46/46 authorship tests on 3.12 and 3.13; local complete runs 2398/2399 passed with 6/5 failures, all traced to host contention or the fake-CLI `python3` PATH artifact (verified: 3 passed with locked `bin` on PATH) | [PR #59](https://github.com/c-daly/harness/pull/59) head `e385d49`; hosted CI run 34728082425 succeeded on 3.12 and 3.13 at `e385d49`; reviewer `pr59-review-r1` found no blocking issues, one should-fix and one nit, both fixed in `1bce83b` (+ body `c6b1f51`); hosted CI run 34729387605 succeeded on 3.12 and 3.13 at `c6b1f51`. Implementation complete; merge is the user's decision | `docs/handoffs/2026-09-12-source-authorship/{README.md,pr-body.md}` |
| 12-user-correction-to-repair-loop | `task/12-…` (prerequisite `feat/source-authorship` merged in) | `12-user-correction-to-repair-loop-w1` | batch 2 | running | PR base `feat/source-authorship` | — |
| 2-sustained-scenario-driver-and-protocol | `task/2-…` | `2-sustained-scenario-driver-and-protocol-w1` | batch 1 | running | — | — |
| 3-plugin-workflow-reconciliation | `task/3-…` | `3-plugin-workflow-reconciliation-w1` | batch 1 | completed: `9827f33` (+ body `e52543d`); 11 focused tests; complete suites 2369 passed / 7 skipped on 3.13 (681 s) and 3.12 (616 s, third attempt after two host-load flakes in untouched files); build and smoke OK | [PR #60](https://github.com/c-daly/harness/pull/60) head `e52543d`, CI pending; reviewer `pr60-review-r1` found 1 blocking (errored `ToolCallCompleted` fabricated a workflow ref from args), 2 should-fix (missing negative tests; architecture paragraph undercounts outcomes); fixes dispatched to the worker | `docs/handoffs/2026-09-12-plugin-reconciliation/pr-body.md`, `docs/plugin-reconciliation.md` |
| 4-suspected-stall-observation-and-recorded-assessment | `task/4-…` | `4-suspected-stall-observation-and-recorded-assessment-w1` | batch 1 | running | — | — |
| 6-claude-code-and-antigravity-task-bindings | `task/6-…` | `6-claude-code-and-antigravity-task-bindings-w1` | batch 1 | completed: `bfa2a9e`; adds `agent_runtime_info` for Claude Code and Antigravity, widens `native_tools`, generalizes catalog routing (declared deviation in `provider_litellm.py`); complete suites 2378 passed / 7 skipped on 3.13 (681 s) and 3.12 (after two host-load flakes in untouched files); build and smoke OK | [PR #61](https://github.com/c-daly/harness/pull/61) head `bfa2a9e`, CI pending, reviewer `pr61-review-r1` dispatched | `docs/handoffs/2026-09-12-agent-task-bindings/pr-body.md` |
| 7-outward-mcp-capability-delivery-without-argv | `task/7-…` (prerequisite `task/6-…` merged in) | `7-outward-mcp-capability-delivery-without-argv-w1` | batch 3 | running | PR base `task/6-…` | — |
| 5-pre-call-usage-reservations | `task/5-…` | `5-pre-call-usage-reservations-w1` | batch 2 | running | — | — |
| 9, 10, 11, 13, 14, 15 | `task/<slug>` | — | queued (spawnable, waiting for a free slot; max 5 parallel) | pending | — | — |
| 8, 16–24 | `task/<slug>` | — | blocked on dependencies | pending | — | — |

## Orchestration decisions

- The runner's automatic merge-to-main (`merge`), `verify` and `stop`
  (forced worktree removal) are not used; the user owns merges and all
  worktrees are preserved.
- Dependent tasks receive prerequisite code by merging the prerequisite
  branch into their worktree before spawn; their PRs are opened against the
  prerequisite branch.
- The implementer role cannot push. Workers commit and write
  `docs/handoffs/2026-09-12-<slug>/pr-body.md`; the orchestrator pushes,
  opens the PR and inspects hosted CI on the PR head.
- Live local-model runs (sustained scenario sessions, measurements) are
  scheduled by the orchestrator only when no full suites are running.

## Blockers and findings

- `workflow__workflow_start` (agent-swarm workflow server) is blocked for
  agent type `main` by the router binding. Not worked around; the runner's
  JSON state and router registrations record the workflow instead.
- Local `main` was 32 commits behind `origin/main`; fast-forwarded with
  `git fetch origin main:main` before worktree creation.
- The repository's automated PR reviewer (Greptile) has paused: its free
  open-source credits for the billing period are exhausted and reviews resume
  on 2026-09-24. Its "pass" check on PR #59 reviewed nothing. Until then the
  orchestrator dispatches an `agent-swarm:reviewer` per PR and records the
  findings and fixes here; the user's own review remains the merge gate.
- Task 1's worker invoked the locked interpreters by absolute path without
  their `bin` on `PATH`, which makes the Codex fake-CLI subprocess tests fail
  deterministically; worker protocol now requires `uv run …`.
- The router shell exports `PYTHONPATH=/home/fearsidhe/.claude/plugins/agent-swarm`
  globally (found by Task 3's worker). That plugin's `scripts` package shadows
  the repository's `scripts/` namespace and breaks collection of the nine
  `scripts.*`-importing test files. Worker protocol now requires
  `env -u PYTHONPATH uv run …`; running workers were notified. This is an
  agent-swarm environment issue, not a Harness defect.

## Completion reporting

Implementation completion (PR opened, CI green) is reported per task above.
Qualification (live model, hardware, human) and release gates are separate
and remain open until run; see the inventory's disposition lists. No roadmap
milestone is claimed complete by this workflow.
