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
| 1-source-authorship-completion | `feat/source-authorship` (existing worktree) | `1-source-authorship-completion-w1` | batch 1 | running | — | — |
| 2-sustained-scenario-driver-and-protocol | `task/2-…` | `2-sustained-scenario-driver-and-protocol-w1` | batch 1 | running | — | — |
| 3-plugin-workflow-reconciliation | `task/3-…` | `3-plugin-workflow-reconciliation-w1` | batch 1 | running | — | — |
| 4-suspected-stall-observation-and-recorded-assessment | `task/4-…` | `4-suspected-stall-observation-and-recorded-assessment-w1` | batch 1 | running | — | — |
| 6-claude-code-and-antigravity-task-bindings | `task/6-…` | `6-claude-code-and-antigravity-task-bindings-w1` | batch 1 | running | — | — |
| 5, 9, 10, 11, 13, 14, 15 | `task/<slug>` | — | queued (spawnable, waiting for a free slot; max 5 parallel) | pending | — | — |
| 7, 8, 12, 16–24 | `task/<slug>` | — | blocked on dependencies | pending | — | — |

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

## Completion reporting

Implementation completion (PR opened, CI green) is reported per task above.
Qualification (live model, hardware, human) and release gates are separate
and remain open until run; see the inventory's disposition lists. No roadmap
milestone is claimed complete by this workflow.
