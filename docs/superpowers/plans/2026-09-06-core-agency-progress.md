# Core agency implementation record

Implementation branch: `feat/core-agency`, based on `main` at `ce722b4`.

Committed checkpoints: `0351b75` (roadmap, lifecycle, and storage/result integrity),
`a2e565e` (controller/UI, delegated scope and limits, catalog, authenticated MCP),
`7c5fa95` (merge main, including terminal math and experiment-plugin docs),
`9965b60` (bounded inference and core improvement records),
`94301a7` (native task outcomes and queue recovery), and `c4cd1cc` (resolve
CI/documentation conflicts with main at `88a9d42`).
PR: https://github.com/c-daly/harness/pull/9.
Implementation continues on this branch while the PR is unmerged.

**Resumed after machine restart.** All 26 files in the
[restart handoff](../../../HANDOFF.md) matched their saved hashes before new
edits. That handoff remains a historical snapshot; this record tracks subsequent
implementation and verification.

The user authorized implementation of the
[core agency roadmap](2026-09-06-core-agency-roadmap.md) on September 6, 2026.
This record separates implemented behavior, verification, and outstanding
qualification. Milestone completion requires its roadmap gates, not just code.

| Milestone | State | Evidence / remaining work |
|---|---|---|
| M0 baseline and feasibility | In progress | Baseline: 885 passed, 7 skipped. CI, build and fresh wheel installation checked locally. Two restart inference probes remain too slow for synchronous UI use. Hardware/profile selection and cold startup remain. |
| M1 correctness and interaction | In progress | 931 passed, 7 skipped after storage, delegation, controller/UI and MCP capability changes. Context budgets, broader enforcement/redaction and fault qualification remain. |
| M2 inference / agent contracts | In progress | Bounded inference, native task/progress/results, explicit execution kinds, nullable usage, and core improvement records implemented. External runtime migration and capability qualification remain. |
| M3 local core assistant | Pending | Readiness, runtime ownership, normal memory access, and offline task journey. |
| M4 bounded semantic agency | Pending | Typed functions, evaluation, scheduling, capability-aware fallback, and operational self-improvement lifecycle. |
| M5 heterogeneous work / plugins | Pending | Supervision, plugin reconciliation, portable continuation, isolated improvement patches and rollback. |
| M6 daily-use qualification | Pending | Measured UI, live adapter boundaries, offline and human dogfood gates. |

## Baseline observations

- Source baseline: `ce722b4`; existing user `.claude/` and `.context/` remain
  outside the implementation changes.
- Installed local endpoint: `127.0.0.1:8080`; health reports `ok` and the model
  inventory reports `unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ4_XS`.
- GPU inventory: RTX 5070, 12,227 MiB. This identifies the test host; it is not
  a performance qualification or portable minimum requirement.
- The existing local server is user-managed. Implementation must not stop or
  restart that process as if Harness owned it.
- CI now defines Python 3.12/3.13 lint, tests, packaging, and a wheel-install
  smoke check. A workflow definition is not evidence of a hosted CI run.

## Completed change: model lifecycle and internal inference

- Added failed/aborted model facts, cancellation metadata, and a defaulted call
  purpose. Resume repairs both tool and model intents in proposal order.
- All three CLI adapters use the same MCP lifecycle context manager, covering
  cancellation during startup and generator teardown before server shutdown.
- Compaction now runs through dispatch, permissions, and accounting; its result
  does not become an ordinary assistant message. Session model identity ignores
  internal inference.
- Versioned telemetry projects pending/terminal calls, rejects stale schemas,
  and applies event batches atomically and idempotently.
- Validation: 90 focused tests passed; the first full integration run found one
  compaction test relying on the old unaudited-call contract. Updated it to
  verify both accounting and transcript isolation; all five compaction journeys
  passed. The final integration run reported **897 passed, 7 skipped, 4 warnings
  in 249.31s**; Ruff and `git diff --check` passed.
- Integration command: `UV_CACHE_DIR=/tmp/uv-cache uv run --offline pytest -q -ra
  --disable-warnings --ignore=tests/test_result_integrity.py`. The ignored file
  contains the next change's seven newly reproduced RED cases and is not part
  of this completed change. Those cases must pass before the storage change is
  complete. No live provider-containment claim follows from these tests.

## Local feasibility investigation

The initial six probes produced three timeouts and three unparseable responses.
An explicit no-reasoning request completed more quickly but still lacked JSON
conformance. A subsequent probe using nested `response_format.json_schema`
returned the expected classification in **7,906.88 ms**, with 31 prompt and 19
completion tokens. The installed server's request shape matters; endpoint health
alone does not demonstrate the requested inference capability.

`scripts/measure_local.py` now records the corrected request profile and keeps
usage, finish reason, and response lengths when validation fails. It measures
an existing service; it does not qualify cold startup or a held-out decision
corpus. The initial response-format reference was the
[llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/tree/master/tools/server);
the running server's actual response determines whether a capability works.

After restart, the corrected nested-schema probe returned a valid acknowledgement
in **12,708.45 ms**; the supplied-context probe timed out after **15,012.37 ms**.
The endpoint reported the same model identifier. Exact output is preserved in
[the restart probe](../../handoffs/2026-09-06-core-agency/local-probe-after-restart.json).
These two samples do not qualify latency or offline startup. Core interface
controls must stay independent of inference completion.

## Storage and result integrity

- Blob references validate digest syntax and nonnegative size. Reads verify
  regular-file identity, size, and SHA-256 without following file symlinks.
  Publication syncs complete bytes before an exclusive link; an existing corrupt
  object is an error, never silently overwritten or reused.
- Dispatch resolves sidecars before inference. The external-agent text bridge
  now includes tool names, call IDs, arguments, results, and error status; it
  rejects unsupported images and unresolved sidecars. MCP returns full results.
- Recovery owns a persistent advisory guard through read, atomic repair, fold,
  and writer acquisition. Exact torn bytes survive partial UTF-8 and interrupted
  publication; failed recovery releases ownership for a subsequent retry.
- The original seven RED cases now pass in a 64-test focused run. Additional
  tests cover real process death, absent blobs, symlinks, newline commit boundaries,
  recovery retry, transcript fidelity, and the MCP client path.
- Full integration after storage changes: **914 passed, 7 skipped, 5 warnings
  in 255.15s**, using `UV_CACHE_DIR=/tmp/uv-cache uv run --offline pytest -q -ra
  --disable-warnings`. Localhost MCP integrations ran with socket permission.
  The next file, `tests/test_delegation_scope.py`, was written after this run's
  collection and separately produced four expected RED failures; it is not
  evidence against the completed storage change or a passing delegation gate.
- Full context budgeting and broader storage
  fault qualification remain separate work. This is not a complete M1 claim.

## Delegation, controller, and UI checkpoint

- Descendants retain their actual parent session and intersect tool restrictions,
  including through coordination tools. Root-shared dispatch/depth/child limits
  cannot be reset by a grandchild. Tests cover excluded tool attempts, recursive
  limits, retry accounting, cancellation release, and failed child initialization.
- Dispatcher validates final arguments, records tool cancellation, and preserves
  the same terminal result in live and folded history. Concurrent tool failures
  settle siblings before teardown. Outward MCP requires a per-run URL capability
  and rejects browser-origin requests.
- Both frontends use the core prompt controller. TUI follow-ups are acknowledged
  and queued; edit/remove/pause/resume/clear are explicit, draft history is
  preserved, and model changes wait for logical turn boundaries. The queue is
  visibly **memory only**. Queued text is not logged as user speech until started.
- Final-compositor tests found overlapping bottom widgets; one container now
  renders queue, status, and composer without hiding them. Tests verify visible
  pending/paused state, unsent drafts, edit/remove, and a model switch between
  whole turns rather than between tool iterations.
- Catalog resolution no longer imports LiteLLM or triggers its remote cost-map
  fetch. It reads the installed snapshot; unknown metadata remains unknown.
- Final integration: **931 passed, 7 skipped, 5 warnings in 261.13s** with
  `UV_CACHE_DIR=/tmp/uv-cache uv run --offline pytest -q -ra` and localhost/process
  access. Warnings are the existing deprecated MCP client helper in five tests.
  Ruff and whitespace checks passed. Sdist/wheel built, and the wheel installed
  offline into a new isolated environment for the CLI help check.
- Diagnostic qualification: restricted test runs exposed a 300-second asyncio
  executor shutdown warning. A minimal `asyncio.to_thread(lambda: 1)` reproduction
  with a one-second shutdown timeout reproduced it in the restricted sandbox
  (1.003s), while the same program outside it finished in 0.002s without warning.
  This distinguishes the sandbox notification issue from catalog SDK imports;
  do not claim that the metadata change alone fixes that sandbox behavior.
- This checkpoint does not implement resident local fallback, the inference/agent
  contract split, or self-improvement experiments/adoption. Those remain active
  roadmap work, including the user's first-class self-improvement requirement.

## Bounded inference and core improvement foundation

- `dispatch_inference` owns typed requests/results, explicit tool-proposal policy,
  purpose, input/output/frame bounds, deadline, sampling, and optional validated
  JSON schema. Normal native conversation calls use the inference adapter, while
  invalid tool proposals retain ordinary dispatch error feedback and audit facts.
  Oversized sidecars are refused before reading; this is an explicit bound, not
  automatic context selection or a token-budget implementation.
- Catalog aliases expose inference versus agent execution kinds. Internal calls
  reject an external-agent route after routing hooks, and external-agent failures
  are not retried automatically. Legacy agent completion is still a migration
  path; dedicated task/progress/artifact/terminal contracts remain to implement.
- LiteLLM receives output-token, timeout, temperature, and schema parameters;
  SDK retries are disabled for bounded calls. Native retry attempts share a
  deadline. Cancellation and rejected streams close their underlying sources.
- Usage preserves missing measurements, including CLI adapter fields. Nullable
  telemetry schema 2 preserves unknown totals/cost and requires rebuilding older
  derived databases. Old session logs remain readable.
- Core `ImprovementJournal` persists verified evidence references, candidate and
  suite artifacts, fixed plans, and paired evaluator results. Plans bind exact
  incumbent/candidate/evaluator digests. Critical failures, regressions, incomplete
  results, or violated benefit/latency gates cannot qualify. Default adoption
  policy requires review. `/improvements` and `harness improvements SESSION_ID`
  inspect the same records without plugins or a replacement memory store.
- The improvement contracts **do not yet run experiments or activate changes**.
  Evaluator authority, background proposal generation, safe activation, isolated
  source patches, and rollback remain mandatory M4/M5 work. An eligible record
  is not permission to modify a running installation.
- Integration with current main initially passed **958 tests, 7 skipped**.
  First hosted runs at `7c5fa95` passed both 3.12 jobs and one 3.13 job; the other
  3.13 job exposed a stats timer firing after DOM removal. A deterministic RED
  reproduction now passes: timers stop at shutdown, late callbacks tolerate
  removed widgets, and the derived SQLite store closes on unmount.
- The first full inference integration run reported **5 failed, 977 passed,
  7 skipped, 5 warnings in 264.90s**. Three failures identified inference validation
  preempting the native tool-error/rewrite path; one asserted zero for an absent
  Antigravity usage field; one reproduced the timer shutdown defect. All were
  addressed, with focused regressions passing. A restricted MCP integration run
  was interrupted because that sandbox cannot provide its local socket path;
  it is not recorded as a passing run.
- Final local integration: **983 passed, 7 skipped, 5 warnings in 264.65s**,
  using `UV_CACHE_DIR=/tmp/uv-cache uv run --offline pytest -q -ra` with
  localhost/process access. Ruff and whitespace checks passed. Sdist/wheel built.
  Initial fresh offline installation lacked cached math dependencies from main;
  after provisioning versions constrained by `uv.lock`, another fresh installation
  succeeded offline with 83 packages. This verifies package provisioning, not
  local-model/offline-readiness qualification. All four hosted CI jobs at
  `9965b60` passed on Python 3.12/3.13, including builds and fresh wheel installs.
  See [contract details](../../core-inference-and-improvement.md).

## Native task outcomes and recovery

- Added core `AgentTask`, `TaskLimits`, `AgentRuntime`, progress, and `AgentResult`
  contracts. Native loop execution records its task/run identities, parent-run
  lineage, limits, terminal outcome, and verified output blob. Model facts link
  to the owning task/run. A task does not grant authority beyond its dispatcher
  binding, and simultaneous tasks on the same loop are refused before execution.
- Native tasks retain stop reasons. Token cutoff, missing tool calls, and
  exhausted iterations cannot become successful completion. Truncated tool
  proposals are paired with cancellation results without executing them. Failure
  and cancellation propagate after cleanup and terminal recording. Resume closes
  interrupted runs as `aborted` without replay or self-certified acceptance.
- Acceptance criteria reach the model context but remain explicitly unverified
  in every execution result. Returning prose, including a claim that checks pass,
  cannot clear criteria. This provides task-level evidence for the improvement
  journal without making the candidate its own evaluator.
- The shared controller and both frontends consume results. An incomplete task
  pauses follow-ups; TUI output shows the reason and preserves the queue/draft;
  headless CLI prints partial output and exits nonzero. Native child outcomes
  retain `incomplete`, and successful string delivery cannot overwrite that
  status in the activity panel. Output caps cannot strip the child error prefix.
- Native tasks have bounded steps, a task deadline, and per-step inference
  bounds. Shared cumulative token/cost budgets, persistence of those budgets,
  typed coordination results, and capability-qualified external task execution
  are still pending. External CLI aliases retain the legacy conversation bridge;
  this checkpoint does not certify their process cleanup or native tool authority.
- Focused recovery/controller/subagent/UI checks: **59 passed in 15.78s**.
  Tests include final terminal-compositor output, interrupted-run repair,
  parent-run lineage, single terminal publication on write failure, and rejection
  of overlapping tasks. The first full run reported **1 failed, 998 passed,
  7 skipped, 5 warnings in 266.21s**: the new CLI test replaced the interactive
  echo provider instead of the headless fake provider. Correcting that test's
  injection produced **1 passed, 33 deselected in 0.40s**.
- Final full integration: **999 passed, 7 skipped, 5 warnings in 265.69s**,
  using `UV_CACHE_DIR=/tmp/uv-cache uv run --offline pytest -q -ra` with
  localhost/process access. Ruff and whitespace checks passed. Sdist/wheel built;
  a fresh offline Python 3.13 wheel installation with 83 cached dependencies
  executed an incomplete native task and replayed its output/acceptance from
  verified blobs and events. Hosted checks for `94301a7` passed on Python 3.12
  and 3.13. The subsequent merge at `c4cd1cc` passed **1003 tests, 7 skipped**
  locally, lint, build, all 52 wheel module imports, and both hosted CI jobs.
  This is not live provider or offline-model qualification.

## PR review: bounded external-agent response streams

- The [external stream review](https://github.com/c-daly/harness/pull/9#discussion_r3944545662)
  identified a real bypass: external
  conversation agents used the unbounded collector and could publish oversized
  completions before the native task's final result-size check.
- Inference and external-agent response collection now share byte/frame bounds,
  reported output-token checks, terminal validation, and a request deadline.
  Rejected chunks do not reach observers or completion persistence. External
  agents retain their execution kind and no-retry rule; internal inference
  still cannot route to an agent. `TaskLimits.max_stream_chunks` is passed into
  each model/agent response step.
- Catalog forwarding explicitly closes Claude Code, Codex, and Antigravity
  streams. Limit failures settle that cleanup before recording the failed model
  call. Missing usage remains unknown. These are owned-response bounds, not
  enforcement of raw subprocess output, scratch storage, or native tool effects.
- Regression reproduction stopped after **6 failing cases in 0.72s** before
  the fix. The initial focused integration passed **74 tests in 3.39s** after
  implementation. Final integration: **1039 passed, 7 skipped, 5 warnings in
  263.42s** with `UV_CACHE_DIR=/tmp/uv-cache uv run --offline --no-sync pytest -q -ra`
  and localhost/process access. Ruff, whitespace checks, sdist/wheel build, and
  the clean wheel smoke gate (all 52 module imports) passed. Hosted validation
  for this review-fix commit follows its push; check PR #9 for that status.

## Validation policy

Use meaningful failing regression cases for behavior changes, then targeted
checks and full-suite integration checks. Preserve skipped, unavailable, and
human-only evidence as such. Replay fixtures cannot certify live provider
containment; a fake local endpoint cannot qualify actual local-model quality.

## Scope steering

On September 6 the user made self-improvement a first-class requirement.
The roadmap now includes evidence, candidate and experiment records, evaluation,
adoption policy, safe activation, and rollback throughout M0–M6. Automatic
adoption policy is an outstanding product preference; foundation work continues.
