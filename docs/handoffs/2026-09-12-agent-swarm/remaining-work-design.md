# Harness remaining roadmap work

## Assignment to agent-swarm

Own the remaining-work inventory, decomposition, dependency queue and execution
for Harness. The user explicitly asked to identify all remaining work and hand
it to a workflow to complete, then clarified that agent-swarm should determine
the queue. Use the existing pipeline: inspect the accepted roadmap and current
implementation, produce a remaining-work implementation plan, convert it with
`agent-swarm:plan-to-manifest`, and pass the manifest to
`agent-swarm:parallel-orchestrate`. Do not build another scheduler or ask the
calling agent to manually construct your queue.

The user has authorized planning, conversion, dispatch, implementation,
validation, commits, pushes, PR creation and review fixes. Stage checkpoints
should report concrete artifacts and continue within that scope. The user
continues to own PR merges and release/publication decisions. A generated plan
or manifest is not an executing workflow: record actual worker registration,
dispatch and outcomes separately.

## Product objective and authoritative inputs

Harness should provide a smooth, persistent interface across providers and agent
runtimes. Project information and normal memory supply continuity. Distinguish
models (inference) from agents (execution loops) and the resident assistant
(continuing interaction). Make local models useful, support heterogeneous agent
collections, degrade gracefully when resources disappear, and treat measured
self-improvement as a core capability. Memory and agent-swarm remain plugins;
the core must remain useful without either and must not import their internals.

Read these paths relative to the repository, and reconcile their historical
claims against source and retained results:

- `docs/superpowers/plans/2026-09-06-core-agency-roadmap.md`: accepted direction,
  M0–M6 exit gates and the crosswalk to the older hardening backlog.
- `docs/superpowers/plans/2026-09-06-core-agency-progress.md`: implementation and
  evidence history. Its opening branch summary is stale; later entries and
  current Git state take precedence.
- `docs/superpowers/plans/2026-09-03-production-beta-hardening.md`: retained
  hardening details, reordered by the accepted roadmap. Unchecked boxes are not
  proof that a feature is missing. Preserve its explicitly deferred scope.
- `docs/architecture.md` and `docs/contributing.md`: contracts and validation
  conventions; verify statements against source where documentation has drifted.
- `docs/mixed-runtime-qualification.md`, `docs/local-model-candidates.md`, and
  the retained handoffs linked from the progress record: measured limits and
  candidate work. GGUF/Hugging Face sourcing exists; Unsloth training is not an
  implemented capability or an automatic requirement to train weights.

The broad assessment is that substantial core machinery exists, but a coherent,
dependable daily assistant is less well demonstrated. M3 passed a narrow 8B CUDA
offline/memory/restart profile; that does not establish general local model
quality. Semantic helpers remain advisory where quality gates failed. The live
mixed-runtime journey failed its local artifact correctness gate despite passing
most lifecycle checks. M5 plugin reconciliation and M6 daily-use qualification
remain open. Establish the exact remaining items yourself rather than copying
this assessment into a queue without inspecting the code.

## Sustained scenario requested by the user

Devise a properly complicated, evolving project that tests the harness over
multiple sessions and shows where it falls short. It should exercise ordinary
user-facing work, changing requirements, normal memory, different providers and
agent types, local/offline work, interruption/restart, partial side effects,
portable continuation and a later return to the project. Include an explicit
user correction and distinguish task acceptance from an acknowledgement or pause.

Use independently checked project outcomes, frozen criteria and retained
artifacts. Measure verified completion, regressions, duplicated/lost work,
operator interventions, useful recovery, latency and cost/unknown usage. Separate
model errors, orchestration failures, memory failures and interface friction.
Include a measured improvement and a rejected regression with rollback. A
deterministic script calling APIs is useful diagnostic evidence but cannot stand
in for actual interface use or human evaluation. Preserve failed trials, and
do not change the task or its acceptance tests merely to obtain a pass.

Agent-swarm determines the scenario, task decomposition and placement in the
execution queue. Use it early enough to expose product gaps and guide work;
do not postpone meaningful integrated use until all possible machinery is built.

## Current repository and unfinished work

Verified at handoff: GitHub `c-daly/harness` PR58 is merged at
`4c09803950d16a2ab86e58774524ad3e4cfd15ec`; no open PRs were listed. Refresh remote
state before implementation. The default checkout is deliberately on a research
branch; do not switch or reset it.

- Original checkout: `/home/fearsidhe/projects/harness`, branch
  `feat/resident-helper-measurements`, head `e5a54c3`, clean at inspection.
- This control checkout: `/home/fearsidhe/projects/harness/.worktrees/remaining-roadmap-workflow`,
  branch `docs/remaining-roadmap-workflow`, based on PR58's merged main.
- Existing unfinished implementation:
  `/home/fearsidhe/projects/harness/.worktrees/source-authorship`, branch
  `feat/source-authorship`, based on the same main. Its changes are largely
  staged, with additional unstaged tests. They are not committed or published.
- Scratch and retained runs: `/home/fearsidhe/projects/harness/.worktrees/tmp`.
  Preserve all logs, reports, model measurements and older worktrees.

Take ownership of the existing source-authorship work without starting a
competing implementation. Read its
`docs/superpowers/plans/2026-09-12-source-authorship.md`, `docs/source-authorship.md`
and `docs/handoffs/2026-09-12-source-authorship/README.md`. It implements one
bounded native proposal with immutable source/checks, deterministic finalization,
normal context access and supervised evaluation/selection/rollback. It is not
an unrestricted coding agent or a security sandbox.

Its immediate known defect: partial `SourceAuthorSpec.limits` JSON overrides
use generic `TaskLimits` defaults rather than source-author defaults. Three
new tests in `tests/test_source_authorship.py` are RED through the shared command:
`test_partial_limit_overrides_keep_author_defaults_in_the_shared_command`.
Examples override only `timeout_seconds`, `max_output_tokens` or
`max_input_bytes`. Preserve omission versus explicit timeout semantics and the
source-specific caps. The previous agent had not applied this fix.

Validation evidence for that unfinished branch:

- First full runs: Python 3.12.14 had 2,400 passed; Python 3.13.15 had 2,399
  passed and one existing coordinator setup-timer failure. Both had seven skips
  and six warnings. A controlled 3.2-second setup delay reproduced that failure;
  the fixture now waits for actual readiness or task completion. The focused
  author/coordinator/probe set passed 76 tests after that fixture change.
- Subsequent full runs were deliberately interrupted before adding the partial
  override tests: 2,255 passed on 3.13 and 2,254 on 3.12. These are incomplete
  runs, not green suites. The three latest override tests failed.
- The two existing local-model measurement runs are retained unchanged under
  `source-authorship-measurement-v1` and `source-authorship-measurement-v2` in
  scratch. They yielded two useful improvements out of six attempts, then one
  out of six with three startup failures before inference. Other proposals
  were malformed, incorrect or unchanged despite improvement claims.
- Once source changes, reconcile the measurement README's current-source hash
  claim honestly. Older measured versions remain evidence for those versions;
  do not silently relabel them as a measurement of final code.
- Dedicated locked environments already exist at scratch
  `source-authorship-py31214/bin/python` and
  `source-authorship-py31315/bin/python`. Use `PYTHONPATH=src:.` in that worktree,
  and `TMPDIR` plus `GIT_CEILING_DIRECTORIES` set to the scratch directory for
  nested Git fixtures. Rebuild and run installed-wheel smoke after final edits.

## Execution constraints and completion evidence

Preserve user settings, private memory, dirty work, untracked files, prior
measurements and services. Stop only processes owned by the workflow. Do not
edit the running Harness installation opportunistically. Preserve evaluation
authority outside candidates; no self-approved code activation or weight
training. Record accepted lessons through normal memory policy.

Use isolated worktrees and make prerequisite commits available in dependent
worktrees before workers start. The installed parallel runner creates all
branches from the base at startup; dependency status alone does not import
prerequisite code. Resolve this within your orchestration process. Do not invoke
its automatic merge-to-main or forced worktree-cleanup steps: user-owned merges
and preservation requirements take precedence. These constraints do not require
new permission for ordinary implementation or PR creation.

Use meaningful behavioral checks for changes. Do not manufacture tests to meet
an arbitrary minimum count, and do not require runtime tests for documentation
alone. At code integration boundaries, run complete supported Python 3.12/3.13
suites, Ruff, packaging and installed smoke; keep source/tests fixed during
runs and report actual complete summaries. Inspect hosted CI and review on the
actual PR head. Avoid resource contention between full suites and local trials.

Separate explicit execution budgets from hang detection. Silence or elapsed time
alone is not proof of a hang; inspect recorded activity, ownership, progress and
wait states. Preserve checkpoints when bounded work pauses. Repeated experimental
failure is a finding, not a reason to loosen a gate or retry indefinitely.

Export your generated inventory, plan, manifest and workflow progress to durable
files with their paths, task/worker identities, branch/PR references, evidence
and blockers. Include a crosswalk so every retained roadmap obligation is
accounted for as implemented, remaining, qualification-only or explicitly
deferred. Report implementation completion separately from qualification and
human/release gates. Do not claim the roadmap complete merely because all
coding workers finished.
