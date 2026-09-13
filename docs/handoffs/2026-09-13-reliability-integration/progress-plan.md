# Progress Aware Completion - Implementation Plan

### Task 1: Progress aware completion

Continue the reliability increment on `feat/progress-aware-completion`, based
on the integrated checkpoint. The user rejected arbitrary supervisor deadlines
and wants actual work to continue while it makes progress. Reviews of the older
PRs are deferred. Do not change the preserved session lifecycle candidate.

**Files:**
- `src/harness/completion.py`
- `src/harness/events.py`
- `plugins/agent-swarm-runner/run_verified.py`
- `plugins/agent-swarm-runner/run_one.py` only for compatible host wiring
- `tests/test_completion.py`, `tests/test_agent_swarm_runner.py`
- `docs/verified-completion.md`

**Do:**
- Add meaningful regression tests first, reproduce them, then implement.
- Make CompletionPlan.timeout_seconds and max_attempts optional, default None.
  Explicit values remain strict finite positive limits and remain immutable on
  replay; never relax old recorded plans/deadlines or cumulative execution budgets.
- Make CompletionConfigured.deadline optional for new unlimited plans while
  retaining parsing/enforcement of existing numeric records. Preserve strict
  event projection, interrupted-attempt protection and single-owner guards.
- Add a recorded host progress review between settled attempts, using a typed
  decision and bounded reason. A supplied async host reviewer receives the prior
  and current independent observations plus the attempt outcome, and can return
  continue, pause, or block. It cannot rewrite checks, accept tasks, reset budgets,
  change limits, or act as model-controlled execution authority. Record the
  review before the next attempt. Do not manufacture a productivity score or
  infer useful progress merely from changed bytes, a model claim, or equal
  passing-test counts. With no reviewer, preserve ordinary check-and-continue
  behavior; host admission and explicit limits still apply.
- Validate review ordering/decisions and preserve pause/resume behavior. Invoke
  a review only after a settled attempt and a fresh independent check, never
  while an uncertain attempt is pending. Errors in review block without running
  another worker. Resumption rechecks before continuing and does not reinterpret
  an explicit exhaustion, cancellation, deadline or block as permission to retry.
- Update run_verified argparse defaults so it no longer silently inserts six
  attempts or a 30-minute completion deadline. Separate optional overall timeout
  from worker execution defaults; retain existing finite lower-level controls,
  clearly document those remaining boundaries, and record every effective host
  setting for configuration comparison on resume. Avoid substituting a huge
  number for unlimited. Do not broaden execution.py, agent.py or all agent
  timeout schemas in this increment. Keep run_one's existing CLI compatible.
- Keep model-visible check descriptions as the interface contract; ensure all
  descriptions are actually present in the worker prompt/requirements. The
  external grading path is not usable as the sole implementation specification.
- Document behavior, explicit limits, progress review callback, and the remaining
  worker/count boundaries honestly. This is continuation, not task acceptance,
  autonomous promotion, or a completed release gate.

**Verify:**
- Omitted supervisor limits permit more than four settled attempts without
  max-attempt exhaustion and have no recorded overall deadline.
- Explicit attempt limit and deadline stop and remain stopped after reopening;
  cancellation, budget exhaustion, changed workspace and pending attempts retain
  their existing protections. Old finite records remain enforced on replay.
- Review callback sees actual before/after checks and worker outcome, continued
  work records its decision, pause resumes after fresh verification, block/error
  cannot launch another attempt, and an invalid decision fails closed.
- Existing fake-provider native runner tests and event roundtrips still pass;
  new optional defaults do not break run_one or kernel limit construction.
- Run focused tests plus Ruff on touched files. Retain original tests unless
  changed behavior requires updating an expectation. The operator will run
  independent checks, integration suites and packaging separately. No commit,
  push, merge, package installation, network or alternate agent CLI from worker.
