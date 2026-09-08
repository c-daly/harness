# Core agency implementation record

Current implementation branch: `feat/external-handoff`, based on merged `main`
at `e576a8e` (PR26). Previous branches: `feat/assessment-evaluation`, `feat/supervised-improvement`, `feat/local-scheduling`, `feat/task-fallback`, `feat/semantic-context-progress`, `feat/local-assistant-m3`, `feat/resident-workflow`, `feat/task-completion-evidence`, `fix/inference-client-lifecycle`, `feat/local-response-profiles`, `feat/local-tool-recovery`, `feat/local-tool-planning`, `feat/local-qualification`, `feat/semantic-evaluation`, `feat/local-context`, `feat/local-readiness`, `feat/agent-runtimes`,
and `feat/core-agency`.

Committed checkpoints: `0351b75` (roadmap, lifecycle, and storage/result integrity),
`a2e565e` (controller/UI, delegated scope and limits, catalog, authenticated MCP),
`7c5fa95` (merge main, including terminal math and experiment-plugin docs),
`9965b60` (bounded inference and core improvement records),
`94301a7` (native task outcomes and queue recovery), and `c4cd1cc` (resolve
CI/documentation conflicts with main at `88a9d42`), then `ca39617` (external
response stream bounds and review resolution).
[PR #9](https://github.com/c-daly/harness/pull/9) merged on September 6 at
`63449eb`. `dbe9db5` introduced typed Codex tasks and MCP cleanup in
[PR #10](https://github.com/c-daly/harness/pull/10), merged at `8d732f2`.
`18c2e38` added local readiness and process ownership; `ca213a2` addressed its
review findings. [PR #11](https://github.com/c-daly/harness/pull/11) merged at
`77a0108` after Python 3.12/3.13 CI and automated review passed.
`80008be` added context profiles and the offline normal-memory check; `ddfa908`
rejected blank subjects. [PR #12](https://github.com/c-daly/harness/pull/12)
merged at `63cfd04` on September 6.
`3c626af` added semantic observations and paired evaluation in
[PR #13](https://github.com/c-daly/harness/pull/13), merged at `68936ae` on
September 6 after Python 3.12/3.13 CI and automated review passed.
`20c36ad` added the local startup directory and real offline evidence in
[PR #14](https://github.com/c-daly/harness/pull/14), merged at `e3ea2c0` on
September 6 after both CI versions and automated review passed.
`adcdc7c` added the single-tool response bound and a rejected context experiment in
[PR #15](https://github.com/c-daly/harness/pull/15), merged at `0a45d43` on
September 6 after both CI versions and automated review passed.
`e3cbfde` added bounded correction and repeated offline evidence in
[PR #16](https://github.com/c-daly/harness/pull/16), merged at `fb07139` after
Python 3.12/3.13 CI and automated review passed (September 6 local time).
`93d006e` added response profiles and the expanded failed offline experiment in
[PR #17](https://github.com/c-daly/harness/pull/17), merged at `a7c24e3` after
Python 3.12/3.13 CI and automated review passed.
`de2350e` fixed explicit-endpoint HTTP ownership and retained larger model
candidates in [PR #18](https://github.com/c-daly/harness/pull/18), merged at
`02420aa` after Python 3.12/3.13 CI and automated review passed.

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
| M0 baseline and feasibility | In progress | Local feasibility is now established by the M3 8B CUDA workflow. Earlier 4B and 8B profiles failed; 30B/35B probes were too slow. Bounded local fallback is implemented; broader baseline qualification remains open. |
| M1 correctness and interaction | In progress | Storage, delegation, controller/UI, MCP capability and explicit-endpoint HTTP cleanup changes are implemented. Durable task obligations and explicit review are visible through the TUI and headless inspection. Broader enforcement/redaction, provider lifecycles and fault qualification remain. |
| M2 inference / agent contracts | In progress | Bounded inference, native tasks, Codex task binding, explicit execution kinds, nullable usage, core improvement records and recorded task-evidence checks implemented. Remaining adapters, richer artifact predicates and live capability qualification remain. |
| M3 local core assistant | Complete for the measured CUDA profile | Qwen3-8B passes six full offline project journeys: twelve exact native file writes, changed-record answers after restart, real cancellation, normal memory, and visible task/context status. Four missing-assets/startup-exit recovery cases pass. Shipped profiles and a launcher reproduce the workflow. CPU-only operation, automatic fallback and broader daily-use qualification remain outside this gate. |
| M4 bounded semantic agency | In progress | All three shadow functions now exist with explicit CLI/TUI access and evidence validation. The public 8B comparison failed context/progress gates; deterministic behavior remains. Bounded task-preserving fallback and session-tree local scheduling are implemented. Message-prompt pairing and earlier context/correction experiments use core improvement records. Supervised message-prompt proposal/evaluation/adoption/rollback is implemented. Paired context/progress candidate evaluation is implemented with explicit operator controls. Explicit external-to-native reconciliation and bounded handoff are implemented. Held-out qualification, broader handoff qualification and assessment adoption remain. |
| M5 heterogeneous work / plugins | Pending | Supervision, plugin reconciliation, portable continuation, isolated improvement patches and rollback. |
| M6 daily-use qualification | Pending | Measured UI, live adapter boundaries, offline and human dogfood gates. |

`4a98d3f` added durable task requirements and recorded evidence in
[PR #19](https://github.com/c-daly/harness/pull/19), merged at `eb77885` after
Python 3.12/3.13 CI and automated review passed.

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
  for `ca39617` passed on both Python 3.12 and 3.13; review threads were resolved
  and PR #9 subsequently merged at `63449eb`.

## Codex task runtime continuation

- A new branch, `feat/agent-runtimes`, starts from merged main at `63449eb`.
  Codex now binds the core `AgentRuntime` task API through ordinary dispatch.
  CLI/TUI/native child sessions select that path automatically for Codex aliases.
  The continuing Harness task owns a distinct Codex run, declared capability
  snapshot, execution progress, unverified acceptance, and verified answer blob.
- Hook rewrites cannot change a bound runtime into inference or another backend.
  MCP tools retain the run's dispatcher, permissions, shared budget, and task/run
  lineage. External tool results remain auditable without orphan replies in
  conversation history. The typed result preserves reasoning and signatures;
  its conversation terminal fact is folded once. One compatibility model-call
  record retains usage/pricing, without double-counting the nested run.
  Session summaries retain the completed Codex alias for resume/model continuity.
- Regression tests use both scripted streams and real fake-CLI subprocesses
  making MCP HTTP calls. They cover supplied context, routing denial, output
  bounds, zero-iteration refusal, unexecuted tool proposals, unknown usage,
  cancellation, allowed/denied MCP effects, cleanup order, and resume without
  replay. A final-compositor test verifies visible agent activity, cancellation,
  paused follow-ups, and preservation of the unsent draft.
- An additional RED shutdown test reproduced an existing MCP lifecycle defect:
  server shutdown could return while tool dispatch was still alive. The server
  now owns dispatch tasks, refuses new work during shutdown, cancels and settles
  in-flight tools, then closes HTTP serving. A real fake-CLI cancellation test
  asserts that tool cleanup precedes both external and enclosing task terminals.
- Capability declarations remain explicitly **unverified** for installed CLIs.
  Native resume and internal iteration caps are unsupported; native tools remain
  provider-controlled. Token caps reject reported overruns, and response bounds
  cover adapter chunks. Raw subprocess output/scratch containment, live version
  probes, the other adapters, local readiness, and self-improvement evaluator /
  activation / rollback services remain pending. This is progress within M2,
  not completion of M2 or the broader roadmap.
- The initial integration run passed **1053 tests, 7 skipped, 5 warnings in
  282.45s** before the extra active-tool shutdown and session-identity regressions.
  Those regressions were reproduced RED and fixed. Final focused integration:
  **29 passed, 6 warnings in 4.71s**. Final full integration: **1055 passed,
  7 skipped, 6 warnings in 282.68s**, using `UV_CACHE_DIR=/tmp/uv-cache uv run
  --offline --no-sync pytest -q -ra` with localhost/process access. The warnings
  are the deprecated MCP client helper; skips retain missing conformance fixtures
  and the opt-in live Antigravity gate. Locked dependency sync, Ruff, whitespace,
  sdist/wheel build, and a fresh offline wheel installation importing all 53
  modules passed. Hosted Python 3.12/3.13 CI also passed for `dbe9db5` before
  PR #10 merged into main.

## Local readiness and process ownership

- `feat/local-readiness` begins from merged main at `8d732f2`. Explicit catalog
  local profiles identify a loopback OpenAI-compatible endpoint and optionally
  a preinstalled argument-vector command, required files, and on-demand startup.
  Old aliases retain their existing behavior until a profile is added.
  The explicit llama.cpp probe checks health before inventory, because upstream
  permits inventory entries during loading. Generic OpenAI-compatible inventory
  checks carry the narrower model-listing evidence, not a loading-complete claim.
- The core execution scope owns `LocalResources` and shares it with descendants.
  Readiness runs after effective routing, permissions, and call-budget checks,
  inside the bounded inference/task deadline. Root capacity defaults to one
  owned process. A reachable existing service is never adopted or terminated.
  Cancelled startup, including an OS-spawn race, settles the owned child; shutdown
  reaps it even if stop-event writes fail. Readiness performs no provisioning.
- `ResourceObserved` records timestamped availability evidence and unknown
  capability fields. `LocalRuntimeRequested` records start/stop intent without
  logging argv, credential values, or server error bodies. Replay retains
  historical observations without reusing them as fresh cache entries or
  guessing process ownership. Changed configuration/credentials and expired
  evidence require new probes. Interrupted requests invalidate readiness.
  Request completion clears busy activity in replay without renewing probe
  freshness. The real-process cleanup test also covers a worker that ignores TERM.
- `harness resources` and TUI `/resources` inspect local profiles; explicit checks
  run bounded inventory requests and never launch a process or inference. TUI
  checks leave drafts editable and cancel independently of agent tasks. Failed
  local turns retain ordinary queue pause and draft recovery. Session rebuilds
  cancel their old diagnostic worker while retaining the core process owner.
- The existing improvement journal accepts these core observations as evidence
  without plugins. Availability is not acceptance or model-quality grading;
  experiment execution, adoption policy, activation, and rollback remain required.
- Focused tests passed **70 tests in 42.91s** before final fixture/error-state
  refinements. They include a real local HTTP process serving streamed inference,
  a native project-file task with a checked answer, existing-service preservation,
  spawn cancellation, process capacity, journal failure, and terminal-compositor
  checks. An initial full run passed **1084 tests, 7 skipped, 6 warnings in
  293.29s**. Subsequent health-contract, activity-replay, and descendant cleanup
  refinements passed **31 local-resource tests in 3.90s**; the earlier combined
  local/UI run passed **33 tests in 13.54s**. The final full suite, including
  headless shutdown and session-lock release after a stop-event write failure,
  passed **1090 tests, 7 skipped, 6 warnings in 299.32s**. Skips retain missing
  Anthropic/Ollama conformance recordings and the opt-in live Antigravity gate;
  warnings concern the deprecated MCP client helper. Locked dependency sync,
  Ruff, whitespace checks, sdist/wheel build, and a fresh offline wheel install
  importing all **54 modules** passed on Python 3.13. Hosted Python 3.12/3.13 CI
  passed for `18c2e38` in [PR #11](https://github.com/c-daly/harness/pull/11).
- A read-only live probe on September 6 returned `unreachable` /
  `probe_transport_failed` for `http://127.0.0.1:8080/v1`. The new CLI returned
  exit status 1 and a timestamped JSON observation. No inference was requested
  and the external service was not started or stopped. The configured probe ID
  was a placeholder; no installed model identity or quality was inferred.
- This advances M3 but does not complete its gate. Physical RAM/GPU ceilings,
  supported model/profile measurements, cold offline startup with actual cached
  weights, normal memory-plugin/vault access, crash-time orphan reconciliation,
  and automatic capability-aware fallback remain outstanding. See
  [local contract and configuration](../../local-runtime-readiness.md).

## PR #11 review fixes

- A diagnostic check during active inference could cache a transient failure
  and block subsequent requests until its TTL expired. Dispatch now rechecks
  every non-ready observation, preserving the diagnostic evidence and continuing
  to deny a request if the new probe fails. Only fresh ready observations skip
  probing. Recovery works for an owned runtime without restarting its process.
- TUI resource-cleanup errors now surface in the transcript while the session
  ending lifecycle still runs in `finally`. Failure to write either the stop
  intent or the final stopped observation cannot skip the session terminal.
- Eight new regression cases reproduced the review findings before the fix.
  The complete local-resource and TUI-resource suites then passed **43 tests in
  15.26s**, including a real owned process and compositor checks. Final full
  integration passed **1098 tests, 7 skipped, 6 warnings in 295.63s** on Python
  3.13. The skips and MCP deprecation warnings are unchanged. Locked dependency
  sync, Ruff, whitespace checks, sdist/wheel build, and a fresh offline wheel
  install importing all **54 modules** also passed.

## Bounded context profiles and normal-memory integration

- `feat/local-context` starts from merged main at `77a0108`. An explicit core
  profile selects recent complete user/tool turns and enforces an input byte
  budget. System/task/project context, acceptance criteria, and leading summaries
  remain pinned. Oversized current work fails explicitly. Selected tool blobs
  are bounded before loading, and canonical history remains available for replay.
- Exact tool names restrict both advertisement and execution. The effective
  registry remains a live filtered view of the plugin/MCP registration surface,
  and delegated work inherits the profile and existing authority/budget limits.
  Rewrites and broader child definitions cannot restore excluded tools.
- `--context-profile`, `--no-context-profile`, and TUI `/context` expose the
  controls. Profiles persist in core events and restore on resume; `/clear`
  retains the current profile while `/resume` uses the selected session's own
  profile. The TUI reports omissions and the byte cap, and `/tools` shows the
  actual restricted inventory. Internal compaction retains its own bounds.
- `ContextPolicyConfigured` and `ContextPrepared` add replayable configuration
  and preparation evidence. Their counts and policy digest can inform the
  improvement journal, without adding memory storage or an adoption mechanism.
- Focused integration passed **49 tests in 8.13s**. The final full suite passed
  **1117 tests, 7 skipped, 6 warnings in 306.50s** on Python 3.13. Existing skips
  and MCP deprecation warnings are unchanged. Locked sync, Ruff, whitespace,
  sdist/wheel build, and a fresh offline wheel install importing all **55 modules**
  passed.
- A live read-only integration check used the installed memory plugin's actual
  Python/server and normal vault in an isolated Linux network namespace. Both
  no-plugin project inspection and scoped `memory_list` plus project inspection
  passed, then resumed with the saved profile and complete history. The memory
  listing was **13,802 bytes**, and maximum prepared input was **16,153 bytes**
  under the **32,768-byte** cap. The [metadata-only report](../../handoffs/2026-09-06-core-agency/context-memory-offline.json)
  contains no memory text. The provider was scripted: this checks file/MCP and
  continuity plumbing, not actual model reasoning, memory body retrieval, or
  the full M3 offline product gate.
- Current runtime survey found cached Qwen3-Coder 30B and Qwen3.6 35B weights,
  an RTX 5070 with 8,321 MiB free VRAM, and roughly 7 GiB available host RAM.
  Prior 35B latency remains unsuitable for synchronous basic decisions. A useful
  measured local model/profile, physical ceilings, real cold startup and
  cancellation, automatic fallback, and semantic/evaluation/activation services
  remain outstanding. See [profile configuration](../../context-profiles.md).

## Shadow message interpretation and paired prompt evaluation

- `SemanticService` records bounded, versioned, tool-free message judgments
  through ordinary inference dispatch. Schema errors, permission/budget denial,
  timeout, unavailable providers, and concurrent calls abstain. Cancellation
  propagates. No classifier runs on the input path or changes queue/task state.
- Paired experiments bind exact incumbent/candidate/suite bytes, core grader and
  inference source, provider configuration, selected dependency/runtime versions,
  and limits before execution. Fixed critical stop/ambiguity cases remain intact;
  expected labels never enter prompts. Results include deterministic baseline,
  quality, abstention, sample counts, and latency by partition.
- Durable run boundaries and partial results distinguish completion, cancellation,
  deadline, failure, and crash recovery. Resume never reruns an experiment. It
  preserves an already durable result if only the final run marker was missing.
- `harness semantic classify/inspect/evaluate` exposes explicit use; TUI
  `/semantics` and `/improvements` expose recorded evidence. All adoption still
  requires the separate policy; this change adds no activation mechanism.
- The final full suite passed **1153 tests, 7 skipped, 6 warnings in 293.49s**
  on Python 3.13. Existing skips and MCP deprecation warnings are unchanged.
  Targeted integration passed **61 tests in 4.21s**. All **30 new tests passed
  in 3.79s** inside a Linux network namespace without external networking,
  including CLI execution and final terminal inspection. Locked dependency sync,
  Ruff, whitespace, sdist/wheel build, and a fresh offline wheel installation
  importing all **58 modules** passed. Real model quality/latency and the full
  M3 offline journey remain unqualified.
  See [contracts and examples](../../semantic-evaluation.md).

## Small local profile and real offline evidence (September 6)

- `feat/local-qualification` starts from merged PR #13 at `68936ae`. A real
  core-owned launch exposed a missing runtime option: the installed llama.cpp
  CUDA binary needs `/app` as its working directory to load its shared library.
  Optional `LocalProfile.cwd` now sets that directory. Invalid directories fail
  before launch with `missing_configuration`; existing profiles retain inherited
  working-directory behavior. Three real-process regression tests cover launch,
  cleanup, and missing/non-directory preflight failures.
- Provisioned Qwen3-4B-Instruct-2507 Q4_K_M separately, pinned by revision and
  SHA-256. Assets and scratch reports stay in ignored `.local-runtime/`. The
  profile uses the already cached CUDA image, 8,192 context tokens, one slot,
  and four CPU threads. It changes no installed model aliases or user services.
- `scripts/qualify_local.py` runs real native tasks, public semantic examples,
  and the terminal compositor inside a loopback-only Docker container with
  four CPUs, 4 GiB RAM and no swap allowance. Normal memory uses the installed
  plugin and read-only normal vault; private text stays in temporary sessions
  removed on completion. GPU memory is not hard-capped by these settings.
- The [final report](../../handoffs/2026-09-06-core-agency/local-qualification.json)
  binds final driver/core hashes, pinned model/runtime profile and dependencies.
  **The model profile fails the full smoke gate (exit 1).** Across three runs:
  no-plugin artifact work passed 3/3, no-plugin terminal journeys passed 3/3,
  memory-assisted artifacts passed 0/3 (two wrong objects, one malformed JSON).
  Successful execution stays acceptance-unverified. The oracle was not weakened.
- Stopped-server readiness took **2.07–2.47 seconds** for no-plugin tasks; those
  tasks finished in **3.16–5.01 seconds**. All **45 observed public semantic
  samples** matched their labels, taking **78–117 ms**. The malformed artifact
  case stopped before semantic sampling. This is neither held-out quality nor a
  statistical latency qualification. Host file cache was retained.
- Terminal mount measured **140–142 ms** inside the already-running worker.
  Real streamed cancellation settled in **63–105 ms**. Final compositor checks
  covered file answers, draft retention, session-picker resume, another answer,
  and usable failure recovery after a missing local asset. Combined terminal and
  normal-memory use, Python/Docker startup time, and human dogfooding remain.
- Both fully graded memory cases retrieved **13,802 bytes** of normal scoped
  memory. The model's wrong artifacts were produced in two model calls while
  dispatching three tools. Premature dependent tool batching is a hypothesis for
  the next experiment, not a proven memory-contamination diagnosis. Preserve the
  source-fact oracle and inspect batch boundaries before changing instructions
  or selecting a fallback profile. No adoption, fallback, or activation added.
- See [profile, reproduction and limitations](../../local-model-qualification.md).
  The full repository suite passed **1162 tests, 7 skipped, 6 warnings in
  292.42s** on Python 3.13, including all nine new regression/oracle tests.
  Existing skips and MCP deprecation warnings are unchanged. Locked offline
  dependency sync, Ruff, whitespace checks, sdist/wheel build, and a fresh
  offline wheel installation importing all **58 modules** passed. These checks
  validate the core fix and report logic; the real model-quality gate still fails.

## Local response policy and context experiment (September 6)

- `feat/local-tool-planning` begins from merged PR #14. A real diagnostic recorded
  `read_file`, `write_file`, and `memory_list` in one model response, confirming
  that the model generated write arguments before observing the source. A
  prompt-only candidate omitted the artifact and was not adopted.
- Optional `ContextPolicy.parallel_tool_calls = false` now constrains each
  inference response to one proposal. LiteLLM forwards the provider option;
  local validation rejects a complete oversized batch before tool execution.
  `ToolCallLimitExceeded` gives that failure an explicit, redaction-safe type.
  Root scope wins over a wider caller request. The setting survives normal
  profile persistence/resume and refuses external agents before provider execution
  because their internal batches cannot be constrained. Existing defaults remain.
- The opt-in evaluator imports prior evidence and records a context candidate,
  fixed plan, run boundaries, measurements and result through the existing core
  improvement journal. Eight paired cases cover original and reserved synthetic
  facts, with/without normal memory, twice each; order alternates. Case failures
  and interruptions cannot qualify adoption. Successful evaluation would still
  require review; there is no activation path.
- The [experiment contract](../../local-tool-planning.md) and
  [recorded result](../../handoffs/2026-09-06-core-agency/local-planning-evaluation.json)
  separate the protocol bound from model quality. The candidate remains opt-in;
  the failed PR #14 profile has not become an automatic fallback.
- Final paired result: **baseline 4/8, candidate 2/8**, with core verdict `failed`
  and adoption `refused`. Four candidate memory cases hit the typed batch-limit
  error before dispatch; two no-plugin cases omitted the file or wrote malformed
  JSON. The result matches the current driver/core hashes. Earlier paired reports
  also failed (candidate 3/8 and 4/8) and remain in ignored local reports; no run
  was substituted to qualify the candidate. The core audit session is
  `4f80a5dca95648bf869bfe0e2fc46f74` under `.local-runtime/reports/planning-audit`.
- Development validation exposed environment limits: `/tmp` filled and blocked
  sandbox initialization. A Harness wheel-test directory was preserved on project
  disk with a symlink at its old path; unrelated temporary data was untouched.
  Restricted native file-read tests stalled, while the same tests passed with
  normal localhost/thread wakeup access. An initial full run was deliberately
  interrupted after **804 passed, 6 skipped** to finish typed failure diagnostics;
  that partial run is not the final integration result.
- Final integration passed **1180 tests, 7 skipped, 6 warnings in 289.99s** on
  Python 3.13, including 18 added regression/oracle cases. Locked offline sync,
  Ruff, whitespace checks, sdist/wheel build, and a fresh offline wheel install
  importing all **58 modules** passed. The saved report's source hashes match
  the measured code, and the persisted core journal replays the failed verdict.
  Task-owned experiment containers exited; no model/server from this work remains.

## Bounded tool correction and repeated offline use (September 6)

- `feat/local-tool-recovery` starts from merged PR #15 at `0a45d43`.
  `ContextPolicy.tool_recovery_attempts` defaults to zero and permits at most two
  corrections when `parallel_tool_calls = false`. Only a recorded
  `ToolCallLimitExceeded` is eligible; the rejected batch executes no tools.
  Correction requests consume the original iteration, deadline and shared call
  limits and go through ordinary dispatch/permissions. Exhaustion, cancellation,
  other malformed responses and external-agent execution are not retried here.
- `ModelCorrectionRequested` records the failed call, task/run, attempt and fixed
  feedback before new inference. Live and resumed histories fold identically;
  prior completed tools are not replayed. No private rejected arguments enter
  feedback, and a recovered task does not claim known usage for failed generation.
  The TUI announces correction and clears rejected text synchronously before the
  replacement stream. A live compositor regression exposed and fixed an ordering
  bug where asynchronous clearing could erase valid replacement text.
- The [recovery experiment](../../local-tool-recovery.md) uses core evidence,
  candidate, plan, result and adoption contracts. Both arms keep the single-tool
  bound; only the candidate allows two corrections. Eight paired fixed cases use
  the original source facts and a reserved cobalt-μ/29 fixture, with/without normal
  memory, twice each. The independent artifact oracle and all gates remain.
- Initial paired result: baseline **4/8**, candidate **8/8**, `review_required`.
  The final repeat passed baseline **4/8**, candidate **6/8**, with verdict
  **failed** and adoption **refused**. Three memory cases recovered correctly;
  one omitted its output file. One no-plugin candidate produced malformed JSON.
  The [final report](../../handoffs/2026-09-06-core-agency/local-recovery-evaluation.json)
  binds the measured source and replays the same refusal from audit session
  `6707e3f214804549a2afa6e1e00fd4cf` in `.local-runtime/reports/recovery-audit`.
  The initial pass remains recorded there as `9cae3523438f4594b7ee77000b684ca7`.
- The driver now exercises the actual normal-memory plugin through the TUI,
  including exact artifact creation, streamed cancellation, retained drafts,
  session-picker resume, another memory call, and missing-runtime recovery.
  The [final three-repeat report](../../handoffs/2026-09-06-core-agency/local-recovery-journey.json)
  passed **9/12 journeys**: all six headless file tasks and all three no-plugin
  TUI journeys. All three memory TUI cases passed their initial artifact and
  control/cleanup checks but failed the resumed-answer visibility check.
- A separate [metadata-only diagnostic](../../handoffs/2026-09-06-core-agency/local-recovery-resume-diagnostic.json)
  reproduced the failure: both facts were present in a completed **14,036-byte**
  answer, but the project name was outside the final viewport. Response verbosity
  and presentation need work; this observation does not prove lost memory or
  incorrect restored facts. The observer returned all answers unchanged and saved
  no private text. Its timings overlapped integration tests and are not qualification.
- Final headless tasks took **3.14–5.79s**, ready observations **2.07–2.52s**,
  six real stream cancellations **83–306ms**, and warm-Python TUI mount checks
  **144–542ms**, including memory startup where enabled. All **54** public semantic
  samples passed at **82–139ms**. Broader semantic/model quality and human dogfood
  remain unqualified. CPU/RAM caps, loopback-only networking and pinned assets
  are in the reports; no automatic adoption, fallback or activation was added.
- An earlier expanded journey exposed missing/malformed artifacts and resume
  failures, plus a driver error from ending an already-ended TUI loop. The driver
  now mirrors TUI teardown; all 12 final cleanup checks pass. The earlier reports
  and observer script remain under ignored `.local-runtime/` for diagnosis.
- Integration passed **1201 tests, 7 skipped, 6 existing warnings in 301.89s** on
  Python 3.13, including 21 added regression/evaluation cases. Locked offline sync
  and Ruff, whitespace checks, sdist/wheel build, and a fresh offline wheel
  installation importing all **58 modules** passed.
  All task-owned model containers exited; user services and private memory remain
  unchanged. Next: qualify explicit sampling/runtime/model choices and useful
  bounded answers, retaining the artifact and final-viewport checks. M3 is still
  incomplete; an occasional small-suite pass must not become automatic fallback.

## Response profiles and repeated artifact/UI evaluation (September 6)

- `feat/local-response-profiles` starts from merged PR #16 at `fb07139`.
  Core `ResponsePolicy` adds optional sampling, output-token/byte limits and
  answer guidance to persisted context profiles. Defaults preserve existing
  behavior. Task and dispatch bounds take the narrower configured value;
  temperature applies only to conversation/agent-task inference. Semantic and
  compaction requests retain their settings. External agents reject explicit
  sampling rather than silently ignoring it; guidance and local bounds still work.
- Guidance is pinned context and consumes the input budget, without modifying
  canonical history or duplicating on resume. Token exhaustion stays incomplete;
  malformed or oversized output fails, and acceptance stays unverified. `/context`
  displays the settings. Memory and agent-swarm remain plugins.
- The [response contract](../../response-profiles.md) records three failed,
  four-journey pilots: greedy 4B (1/4), official non-thinking Qwen3 8B (1/4), and
  conservative unchanged-sampling 4B (2/4). The separately provisioned 8B asset
  was size/hash verified and ran under the same offline CPU/RAM limits. These
  probes establish feasibility observations, not a qualified model or minimum
  hardware requirement. Their metadata and failed checks are preserved in the
  [pilot report](../../handoffs/2026-09-06-core-agency/local-response-pilots.json).
- The fixed response experiment uses 18 paired cases: 12 file cases across
  original harbor/3 and reserved maple-ν/23 facts, with/without normal memory,
  repeated three times; six real TUI journeys cover both plugin configurations.
  It keeps the artifact, memory-refresh, compositor, cancellation, resume and
  failure checks. The candidate adds short guidance and 512-token/4096-byte
  limits to bounded tool recovery; runtime sampling stays unchanged in both arms.
- Final paired result: **incumbent 14/18, candidate 9/18**, six regressions and
  one improvement, core verdict **failed**, adoption **refused**. File cases fell
  from 10/12 to 5/12; both sides passed 4/6 UI journeys. Some short answers were
  useful, but missing/wrong/malformed artifacts and a memory-resume failure
  prevent adoption. The [result](../../handoffs/2026-09-06-core-agency/local-response-evaluation.json)
  binds the measured source and all observations; the journal is
  `b5324774b0e84388be1bf8ba9b79f70e` under `.local-runtime/reports/response-audit`.
- All model-process and MCP teardown checks passed, but the longer run emitted
  unclosed aiohttp client-session warnings. SDK transport ownership remains an
  explicit gap: stream closure and process cleanup are not full client cleanup.
  Installed LiteLLM client caching includes request timeouts, while dispatch uses
  varying remaining deadlines. A focused reproduction, bounded ownership and
  concurrent/cancelled-call tests are the next lifecycle work; avoid globally
  closing clients that other tasks may still use.
- Validation setup initially used `uv sync --dev`, but this repository declares
  development tools as the `dev` extra. That removed the test runner from the
  project environment and collection failed before any tests ran. Corrected to
  `uv sync --locked --offline --extra dev`, restoring the locked tools. This setup
  failure is separate from integration results. The saved evaluation's script
  and core hashes match the final measured implementation, and journal replay
  confirms the failed verdict and refused adoption.
- Full integration passed **1222 tests, 7 skipped, 6 warnings in 315.76s** on
  Python 3.13, including 21 new profile/evaluation cases. Ruff, whitespace checks,
  sdist/wheel build and the fresh offline wheel smoke importing all **58 modules**
  passed. The wheel smoke resolves cached compatible dependencies independently
  of the development lock; it verifies packaging/imports, not live model quality.
  All task-owned experiment containers exited.
- More tuning alone has not made local work reliable. Next core work should
  check task requirements against recorded execution/artifact evidence and expose
  unmet requirements in the interface. Semantic self-assessment stays advisory;
  it cannot certify completion. M3, automatic fallback and activation remain
  unqualified. No user model configuration, plugin internals or private memory
  were changed.

## Request-owned inference clients and model candidates (September 6)

- `fix/inference-client-lifecycle` starts from merged PR #17 at `a7c24e3`.
  The user asked to retain larger models, Unsloth and Hugging Face as options
  while continuing the existing priority. The [candidate list](../../local-model-candidates.md)
  records Qwen3-14B, existing larger hybrid candidates, quantization and model
  sourcing. No new weights, backend installation or default changes were made.
- The real SDK reproduced unclosed HTTP clients after ordinary responses,
  errors, output rejection and cancellation. Three legacy generator wrappers
  also deferred inner cleanup. Initial regression result: **9 failed, 1 passed**.
  The adapter now supplies a request-owned client for explicit `openai/` endpoints
  and credentials, including local catalogs. It retains the SDK transport factory
  and closes transport after stream cleanup, with AnyIO cancellation shielding.
  Concurrent calls keep independent clients; caller-owned transports stay borrowed.
- Legacy wrappers now propagate `aclose()` immediately. An additional regression
  reproduced that direct `complete()` ignored its configured key environment
  variable; that path now resolves credentials like `infer()`. The SDK is a direct
  declared dependency, with existing locked versions unchanged. Other provider
  types and ambient endpoint/credential routes retain their existing ownership;
  this slice is not general provider qualification.
- All **44 focused checks** passed, including 15 new cases covering actual
  HTTPX/aiohttp state, concurrent requests, pre-header and streamed cancellation,
  AnyIO cancellation, timeout, server error, output-size boundaries,
  stream-close failure, direct credentials and legacy cleanup.
- The [real offline lifecycle probe](../../inference-client-lifecycle.md) made
  24 bounded requests, cancelled one stream, then completed another request.
  The [final report](../../handoffs/2026-09-06-core-agency/local-client-lifecycle.json)
  passed in **5.90s**, with **zero open observed clients/sessions after every
  settled call**, 26 model streams, and no new SDK cache entries. Session state
  settled and the owned model process stopped. Source hashes match the measured
  implementation. This verifies lifecycle behavior, not model task quality.
- The first probe reported failure because its total-cache check included four
  clients created during SDK import. Their existence was reproduced before any
  request. The corrected probe records baseline and new cache identities; the
  [initial report](../../handoffs/2026-09-06-core-agency/local-client-lifecycle-initial.json)
  remains preserved alongside the passing repeat. No task-quality gate changed.
- Full integration passed **1237 tests, 7 skipped, 6 warnings in 309.08s** on
  Python 3.13. Locked offline sync, Ruff, whitespace checks, sdist/wheel build and
  a fresh offline wheel install importing all **58 modules** passed. The wheel
  smoke checks packaging against independently resolved cached dependencies;
  the real transport tests/probe use the locked SDK versions.
- Next core work is explicit task requirements, execution/artifact checks and
  visible unresolved obligations, followed by a coherent resident workflow.
  Model self-assessment remains advisory; the previously failed response profile
  and local fallback remain unqualified. Memory and agent-swarm stay plugins.

## Durable task requirements and recorded evidence (September 7)

- `feat/task-completion-evidence` starts from merged PR #18 at `02420aa`.
  [Task evidence](../../task-evidence.md) adds explicit objectives, immutable
  requirements, stable task IDs across attempts, selected-task persistence,
  recorded checks, user confirmations and user acceptance. Six additive event
  types fold into task state without altering historical agent-result meaning.
- Exact output/tool-result checks cite source sequences, effective arguments,
  observed digests and blob references. They require predeclared expectations,
  current-attempt scope and unambiguous execution boundaries. Rewrites, failed
  retries, missing/corrupt/oversized blobs, duplicate call/results and stale
  evidence cannot produce acceptance. These are recorded-byte checks, not live
  file-content assertions or claims about the adequacy of tests.
- A model answer, its `todo` state and legacy outcome scores cannot resolve a
  requirement. Manual criteria require explicit confirmation; acceptance
  requires a completed settled attempt and all requirements resolved. New work
  or an added requirement invalidates prior checks, confirmations and acceptance.
  Compaction and resume preserve the task; repair retains aborted obligations.
- `/task` exposes the workflow with a compact unresolved-work indicator.
  Switching/requirement changes cannot retarget active or queued prompts.
  Read-only `harness tasks SESSION` and headless resumed prompts use the same
  core service. The task command remains available if a plugin uses that name.
  Core improvement evidence can cite these source events without absorbing
  either memory or agent-swarm.
- A synthetic 5,004-event development probe initially measured 49.00–65.40 ms
  per prompt preparation because it reread history. The live projection now
  updates only after successful log writes; warm status/preparation uses independent
  snapshots with no full-log reread. The [final synthetic probe](../../handoffs/2026-09-07-task-evidence/probe.json)
  measured 0.030–0.159 ms after 5,000 extra events, retained source hashes and
  exported the actual terminal compositor at unresolved, accepted and changed
  checkpoints. This is narrow fixture evidence, not M6 latency or human dogfood.
- Regression development observed 20 missing-service failures, four missing-UI
  failures, a repeated-history-read failure and four ambiguous-evidence failures
  before their implementations. Final focused coverage passed **57 tests**
  (including session/fold checks). An earlier broader run hit four localhost
  sandbox binding failures plus four teardown errors; with localhost access the
  broader set passed **68 tests** before the final integrity additions.
- The earlier integration passed **1275 tests, 7 skipped, 6 warnings in 322.44s**
  before the final projection/provenance additions. Final integration passed
  **1283 tests, 7 skipped, 6 warnings in 313.17s**, including **46 new cases**.
  Locked sync, Ruff and whitespace checks passed. The sdist/wheel build and fresh
  offline wheel smoke passed, importing **60 modules**; packaging dependencies
  were independently resolved from cache, while tests use the locked environment.
- Next: combine the selected task, normal memory, local readiness, interruption
  and recovery into one useful resident workflow. Task amendments/waivers,
  richer file/check evidence and language-based requirement proposals remain
  subsequent work. The larger local-model options stay on the candidate list;
  no model defaults or adoption policy were changed. M0–M4 remain in progress,
  M5–M6 pending; earlier failed local-model quality gates are still failed.

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


## September 7 — resident context and continuation

**Addition to M1–M3:** [Resident continuity](../../resident-workflow.md) adds
explicit repeatable context sources to the existing saved context profile.
Root attempts fetch once before inference through the normal dispatcher;
external-agent bridges do not fetch twice and child scopes do not implicitly
query normal memory. Sources respect effective registry restrictions, argument
validation, rewrites, permissions, cumulative tool budgets, source and task
deadlines, result caps and total request limits. Optional failure supplies a
visible unavailable notice; required failure stops before inference. An oversized
sidecar is not materialized, and storage/journal failures remain fatal.

Typed source observations retain run/policy/call provenance and a reference to
the existing session blob store. A context purpose separates those tool facts
from conversation tool turns. The cached task projection supplies a bounded
previous-attempt brief before new execution invalidates old evidence. No recovery
step replays side effects. `/status` joins task, context and runtime snapshots;
`harness status SESSION` reads historical records without a provider or repair.
Drafts, queue interruption and literal compositor rendering retain their behavior.

Validation started with 12 red source-configuration regressions. The expanded
focused set passed 65 checks with localhost access. The first restricted run
had 61 passes and four MCP socket-bind failures plus four associated teardown
errors; it was not an implementation pass. A complete suite then passed 1317
tests, 7 skipped, 6 warnings in 311.49s. Review added two more regressions for
the standalone external-agent input cap and a failed journal write; the new
resident modules' focused set passed 36 checks. Final full-suite and packaging
results are recorded below.

The actual offline [pilot report](../../handoffs/2026-09-07-resident-workflow/resident-workflow.json)
remains **failed overall**. A pinned 4B model and read-only normal memory ran in a
loopback-only container capped at 4 GiB, zero swap and four CPUs. Both source
queries succeeded before inference, including after a new kernel reopened the
session. Status and resumed-answer compositor checks, real stream cancellation
(123 ms), draft preservation, task/profile continuity and unaccepted requirements
passed. The resumed answer contained the correct facts (6.0 s). The initial
write task failed to create the expected artifact or report those facts (10.3 s);
its model-selected file read failed. The 13,802-byte memory index and these tool
facts do not establish a cause. The initial failed probe, whose cancellation
method typo prevented the later stages, is retained separately with source hashes.
The corrected probe kept model settings, prompts and artifact checks unchanged.

Next: measure and improve context relevance and local tool selection against
exact project artifacts, then qualify a repeatable useful workflow. Continue UI
onboarding/discoverability, typed heterogeneous-agent work and portable handoff.
Automatic fallback and improvement activation stay disabled. Do not infer adoption
authority, lower the failed gates, or expand model installs as a substitute for
workflow evidence. Hugging Face, Unsloth and larger models remain candidates.
Memory and agent-swarm remain plugins. M0–M4 are still in progress; M5–M6 pending.


Final review found an expired source permission dialog could outlive its query.
The new regression reproduced that bug; `AppBoundAsk` now expires its own dialog
on cancellation, removes it when active, and retires it on reveal if another
live permission is stacked above it. An expired dialog cannot grant permission.
The nine focused permission/source checks pass, including the stacked case.

A later full run during severe host load (observed load averages near 200)
finished with **4 failed, 1315 passed, 7 skipped, 6 warnings in 5186.44s**. Failures
were the existing resume-picker/rebuild UI tests; a concurrent focused run took
4174.04s for eight tests and had two timing failures. This is retained as a failed
run, not counted as validation. After the host recovered, the same focused
permission tests passed in 11.92s without relaxed assertions. Two read-only
escalation requests also hit automatic-review timeouts during that slowdown;
normal access and a later full-test approval succeeded.

The offline pilot's source hashes precede the final permission-dialog cleanup
and source-error hardening. Those changes are covered by deterministic tests; no new real-model
quality claim is inferred from the final unit-test result.


Storage review further narrowed optional-source error handling to reading an
already-recorded result. Dispatcher encoding failures, corrupt objects encountered
during writes, journal failures and unrelated infrastructure timeouts remain
fatal; they cannot be mistaken for optional context absence while a tool intent
is unsettled. Two additional regressions cover dispatch encoding/blob failures.


After host recovery, the complete suite with permission cleanup passed **1321
tests, 7 skipped, 6 warnings in 298.15s**. The final focused resident tests,
including storage failure boundaries, passed **40 tests in 5.25s**. The final
full suite below includes those two additional storage regressions.


**Final validation:** **1323 passed, 7 skipped, 6 warnings in 294.43s**, including
40 new resident cases. Locked offline sync, Ruff and whitespace checks passed.
The sdist/wheel build and a fresh wheel smoke passed (`harness --help`, all
62 installed modules). The wheel smoke resolves cached compatible dependencies
independently of the lockfile; it is packaging evidence, not another locked
integration or model-quality run. Skips remain three missing Anthropic fixtures,
three missing Ollama fixtures and one opt-in live Antigravity test.


## PR20 review — source status immediately after resume

The reviewer correctly identified that an unchanged `ContextPolicyConfigured`
event emitted on resume advanced the status cutoff past the previous attempt's
source observations. `/status` and read-only inspection then falsely reported
that no retrieval had happened. The projection now advances its cutoff only
when the policy changes. It retains recorded success and failure across repeated
resumes, without fetching context or calling a provider. Actual clears, overrides
and restoration after a policy change still invalidate the old observations.

Six new regressions cover repeated resume, success/failure visibility, policy
invalidation and the final TUI compositor before new work. Before the fix, three
resume regressions failed and the three invalidation checks passed. After the
fix, all 63 focused context/status tests passed in 6.54s. Final validation:
**1329 passed, 7 skipped, 6 warnings in 308.39s**. Locked offline sync, Ruff,
whitespace, sdist/wheel build and the 62-module fresh-wheel smoke passed.


## September 7 — M3 offline local assistant

[PR20](https://github.com/c-daly/harness/pull/20) merged at `33a24fc` with
Python 3.12/3.13 CI and automated review passing. Work continues on
`feat/local-assistant-m3` from that merge.

**Completed M3 gate:** the [local assistant profile](../../local-assistant.md)
uses preinstalled Qwen3-8B Q4_K_M weights and llama.cpp b9603 on the measured
RTX 5070 host. Six complete journeys cover three project records, plugins absent
and the real normal-memory plugin/vault. Each performs an exact file write,
stream cancellation with draft/queue preservation, process stop, fresh-kernel
resume, a changed-record answer, and a second exact write. Four additional cases
exercise missing assets and actual runtime startup exit, with working records,
queue controls, input and recovery. No cloud connection is available in the
loopback-only container. This is a bounded workflow qualification, not M6.

**Observed defects fixed:** source labels lacked retrieval provenance, causing
`read_file("project-facts")`; context now includes the effective tool/arguments
and clear label semantics. Nested argument mutation and redaction cannot change
the recorded origin or reprocess stored content. Explicit pause of an empty
queue now survives prompt submission, while automatic error pauses still allow
a fresh prompt when no follow-ups remain. The real launcher also exposed an
existing CLI defect: `--allow` was ignored without a permission file. One engine
now receives baseline rules, explicit grants and interactive decisions. A real
native-write regression verifies that the grant works and its absence still
blocks a headless write.

**Model selection:** the narrow same-model 4B diagnosis improved from 0/3 to 3/3
exact writes after the provenance fix. The larger fixed suite still failed on
4B: it sometimes printed proposed JSON without writing a file. The 8B model
passed both the first full suite and the stricter version 2. Earlier failed
reports remain in the [evidence directory](../../handoffs/2026-09-07-local-assistant/).
The chosen profile is explicit, and the user's catalog/server are unchanged.
The launcher independently completed an exact public-fixture write through the
real terminal entry point, plus a separate normal-memory launcher task. Both
stopped their owned runtime. Memory and agent-swarm remain plugins.

**Validation provenance:** the first full repository run found two regressions
in restarting after an automatic error pause: **2 failed, 1344 passed, 7 skipped,
6 warnings in 303.16s**. The explicit-pause marker was restricted to user pauses;
all 45 focused UI/CLI regressions then passed. The initial restricted runtime-test
run was interrupted when localhost-dependent tests stalled; it is not a passing
run. Final verification: **1347 passed, 7 skipped, 6 warnings in 305.65s**, plus
locked offline sync, Ruff, whitespace, sdist/wheel build and a fresh-wheel smoke
importing 62 modules. The [matching-source offline report](../../handoffs/2026-09-07-local-assistant/m3-qualification-release.json)
passes all six journeys and all four fault cases. All recorded core/driver hashes
match the proposed files. Cold readiness was 7.36–14.35s under concurrent test
load, warm resumed writes 2.01–2.75s, cancellation 0.122–0.126s, and UI mount below
0.83s. GPU peak is a whole-device observation, not a per-process allocation cap.

**Next:** M4 bounded semantic functions and resource-aware fallback, followed by
a supervised improvement cycle with explicit activation and rollback. Preserve
this local profile as a regression gate. CPU-only support, broader task/model
reliability, heterogeneous supervision and daily-use qualification remain open.

## September 7 — M4 scoped context and recorded progress assessments

[PR21](https://github.com/c-daly/harness/pull/21) merged at `5dfd808` with
Python 3.12/3.13 CI and automated review passing. Work continues on
`feat/semantic-context-progress` from that merge.

**Added:** the two remaining core shadow functions. Context selection accepts a
bounded candidate set and rejects stale/unknown/unavailable entries, invented
IDs and excess selections. Progress reads existing task requirements, checks,
artifact references and explicit confirmations at a recorded event boundary.
It rejects dropped obligations, unsupported actions and blind retry suggestions
after interrupted execution. Neither function retrieves memory, executes tools,
accepts tasks or changes routing. Memory and agent-swarm remain plugins.

`harness semantic context` and `harness semantic progress` expose the functions
headlessly. TUI `/semantics progress` runs off the input path; Esc, new work,
model changes, compaction and session teardown settle its cancellation. Fresh
busy local-alias observations abstain. The UI retains deterministic evidence
facts when the model fails. Saved observations replay without inference.

**Measured outcome:** the fixed public comparison failed both new functions on
the M3 8B profile. Context was 15/18 correct (83.3%), matching lexical rules in
aggregate but failing a critical unknown-freshness case; its first call also
exceeded the two-second runtime-warm gate. Progress was 9/21 correct (42.9%),
versus 21/21 for simple deterministic rules. The model repeatedly suggested
reconciliation across different task states. Validation rejected those answers.
Message interpretation passed 12/12 public cases, also matched by rules. These
are three repetitions of public fixtures, not held-out promotion evidence.
No prompt, label or threshold was changed to obtain a passing result.

The [evidence handoff](../../handoffs/2026-09-07-semantic-assessments/README.md)
retains the initial failure, a diagnostic timeout with no scored responses,
and the matching-source repeat with bounded public outputs. It also contains
a final TUI compositor capture and a passing matching-source M3 regression:
six actual offline journeys, twelve exact writes, six cancellations/restarts,
normal memory and four recovery cases. Existing local workflow behavior passes
while the new semantic functions remain unqualified for automatic decisions.

**Final validation:** **1398 passed, 7 skipped, 6 warnings in 312.85s**. Locked
offline sync, Ruff, whitespace checks, sdist/wheel build and a fresh-wheel smoke
importing 63 modules passed. The seven skips remain the unavailable Anthropic/
Ollama fixtures and opt-in live Antigravity case. Both the semantic comparison
and M3 regression report hashes match the core/driver source at `d1857d1`.

**Recommendation and next scope:** keep factual progress and controls in code.
Test narrower semantic prioritization against that baseline before promotion.
Continue M4 with resource scheduling and recorded task-preserving fallback,
including uncertain-side-effect reconciliation; then complete the supervised
evidence/candidate/paired-evaluation/adoption/rollback cycle. Assessment candidate
evaluation, fresh held-out gates and broader fault journeys remain outstanding.

## PR22 review — completed execution cannot suggest more work

The reviewer identified that a completed execution with unchecked output or
pending user review could incorrectly validate a `work` suggestion. The validator
now rejects that action, and the prompt limits `work` to execution that has not
started. Checking, repair, review and uncertainty retain their evidence rules.

Two service-level regressions reproduced the defect before the change; both
now record abstentions with the obligations intact, while supported `check` and
`review` suggestions pass. A third regression retains `work` for unstarted tasks.
All 84 focused semantic tests pass. Live reports remain the historical evidence
from `d1857d1`; this deterministic correction does not qualify automatic decisions.

Final verification: **1401 passed, 7 skipped, 6 warnings in 321.23s**, plus
locked offline sync, Ruff, whitespace checks, sdist/wheel build and the clean
wheel smoke importing 63 modules. The existing seven opt-in/fixture skips remain.

## September 7 — M4 task-preserving local fallback

[PR22](https://github.com/c-daly/harness/pull/22) merged at `d74a6cf`; its
Python 3.12/3.13 CI and automated review passed. Work continues from that merge
on `feat/task-fallback`.

**Added:** an explicit routing policy with at most three local inference
candidates. Eligible first-call transport/authentication/availability failures
can switch the prepared inference request within the same task and root run.
Context retrieval, acceptance criteria, authority, cumulative call budgets and
deadlines survive the transition. Each destination re-enters dispatch, requires
declared capabilities and actual readiness, and cannot be redirected by a hook.
Fresh busy candidates are skipped. Explicit model pins remain authoritative.

**Recovery boundary:** accepted conversation responses, recorded non-context
tool proposals and child-agent work hold fallback for reconciliation. External
agent failures do not restart in a local native loop. The dispatcher's own
retries now stop when tools or children were dispatched during the failed call.
Cancellation, denial, limits and malformed output do not enable switching.
Selected aliases remain in use for the run; primary recovery cannot cause
oscillation. Replay restores choices without model execution, and current
routing configuration governs a restarted process.

The TUI clears failed partial output before replacement chunks, retains drafts,
shows the active model and records the fallback reason. Saved status includes
the choice and separates it from the execution outcome. Configuration and
recovery limits are in [local fallback](../../local-fallback.md).

**Evidence and defects:** the first real isolated gate completed all four exact
writes but failed both connection-failure label checks. LiteLLM had wrapped
connection refusal in a synthetic HTTP 500. The adapter now uses a bounded typed
cause chain to retain the network category; real server failures retain overload
classification. A regression reproduced this before correction. A stronger
side-effect test then demonstrated three tool executions through existing
provider retries; the retry guard reduces this to one and holds the task.
Another regression found that native child logs are separate from the parent's;
the shared child reservation now blocks fallback after that work as well.
The unchanged offline gates and final source provenance are recorded in the
[evidence handoff](../../handoffs/2026-09-07-local-fallback/README.md).

The first M3 run on the final core source had two runtime-deadline failures
and a GPU-monitor timeout, despite the preceding M3 pass. Both failed and
passing evidence remain retained; runtime latency is not consistently
qualified by a single passing run. The repeat keeps the original fixed gates.
That unchanged repeat passed all six M3 journeys and four recovery cases,
including twelve exact writes and six cancellations/restarts. The final
fallback gate passed all four real journeys in 13.03–24.76s. Both reports'
core/driver hashes match this implementation.

**Final validation:** **1439 passed, 7 skipped, 6 warnings in 314.92s**, plus
locked offline sync, Ruff, whitespace checks, sdist/wheel build and a clean
Python 3.13 wheel smoke importing 64 modules. The existing seven recorded-fixture
and opt-in live-provider skips remain.

**Next:** general device/cross-alias scheduling and explicit external-agent
reconciliation remain open, followed by the supervised improvement
candidate/paired-evaluation/activation/rollback cycle. The semantic assessments
remain advisory with their previous failed gates retained. M4 is in progress;
this bounded fallback slice does not qualify automatic semantic decisions or
heterogeneous-agent handoff. Memory and agent-swarm remain plugins.

## September 7 — M4 local device-group scheduling

[PR23](https://github.com/c-daly/harness/pull/23) merged at `f8b7c1c`; its
Python 3.12/3.13 CI and automated review passed. Work continues from that merge
on `feat/local-scheduling`.

**Added:** one active local startup/inference per declared device group within
the live session tree. All local aliases default to the same group. A bounded
queue gives root conversation/compaction priority over waiting work, with FIFO
within each class. Active streams are not preempted. Queueing consumes the
original request deadline and retains the existing call reservation; dispatch
authority and budgets remain prerequisites. Recursive requests fail promptly.

**Background and residency:** semantic/evaluation work abstains when the group
is busy, does not cold-start a runtime, and cannot replace a warm owned model.
Foreground/work requests can stop idle Harness-owned runtimes in their group
before loading a different alias, including a different model on the same
endpoint. Equivalent endpoint/model aliases reuse the process. Externally
managed processes are never stopped or adopted. The global owned-process cap
still applies. Device groups are operator declarations, not GPU memory probes
or coordination with other applications or Harness processes.

The TUI shows a waiting group while preserving the editable draft and interrupt
control. Scheduling events link admission to model calls and task runs; resource
inspection includes active aliases and queue counts. Saved status explicitly
distinguishes recorded admission from live state. Replay does not run the queue.
[Configuration and limits](../../local-scheduling.md) and the
[evidence handoff](../../handoffs/2026-09-07-local-scheduling/README.md) document
the boundary.

**Validation:** 160 focused tests passed, including actual fixture-process
replacement, permissions/budgets before admission, queue deadlines, handoff races,
failed journals and TUI cancellation. A cancellation during idle unload now
finishes owned-process cleanup before releasing admission; its regression uses
a real process that ignores TERM. The first real 8B scheduling gate passed both
plugin configurations, with 21–23ms queued cancellation, 10–13ms stream
cancellation, priority ordering and exactly one project write in each journey.

The first full M3 regression passed four of six project journeys and all four
recovery cases. The plugin-free harbor/maple resumed-answer attempts timed out;
a live event trace showed the runtime reporting loading. GPU telemetry also
exceeded its five-second deadline and escaped at teardown, leaving the report
unfinalized. Both the failed report and traceback are retained. This repeats
the kind of runtime variability recorded before scheduling; it is not evidence
that session-tree admission controls external GPU load.

A stronger regression then reproduced a live process remaining after repeated
cancellation during termination. The cleanup guard now withstands additional
interruptions until reaping finishes, then propagates cancellation. The first
scheduling run on that final source passed without plugins but timed out before
the first stream with normal memory. This failed report is also retained.

The unchanged scheduling repeat passed both full journeys on the final source,
with 21–27ms queue cancellation, 9–16ms stream cancellation and exactly one
project write per journey. The final-source M3 repeat passed all six journeys
and four recovery cases, and the final fallback run passed all four cases.
All three final reports' source hashes match the core and drivers. These passes
do not establish consistent cold-start latency under external GPU activity.

**Final repository validation:** **1468 passed, 7 skipped, 6 warnings in 328.32s**,
plus locked offline sync, Ruff, whitespace checks, sdist/wheel build and a clean
Python 3.13 wheel smoke importing 65 modules. The built wheel's core bytes match
the source. Existing recorded-fixture and opt-in live-provider skips remain.

**Next:** complete the supervised improvement candidate/paired-evaluation/
activation/rollback cycle, then the remaining held-out semantic and explicit
external-agent reconciliation/handoff gates. M4 remains in progress. Memory
and agent-swarm remain plugins, while the scheduler and improvement machinery
belong to core.

## PR24 review — independent groups do not block explicit stops

The review identified that `LocalResources.stop` still checked activity across
all aliases. It now checks the owned target's scheduler group, so unrelated
groups can continue working while an idle runtime is stopped. Group activity
also protects aliases borrowing the same runtime and requests in readiness.

A real two-process regression reproduced the rejection before the fix and now
confirms that the target is reaped while the other process stays live and its
HTTP readiness check succeeds. Two further cases retain stop denial for an
active target and a borrowing alias. **111 focused scheduling, local-resource,
resource-TUI and fallback tests passed in 22.31s**; Ruff and whitespace checks
passed. The earlier full suite, wheel and GPU reports remain evidence for
`6d5366a`; they were not rerun for this scoped stop correction.

## September 7 — M4 supervised message-prompt improvement

[PR24](https://github.com/c-daly/harness/pull/24) merged at `032f0fc`; its reviewed
head `cf4cf8a` passed Python 3.12/3.13 CI and automated review. Work continues
from that merge on `feat/supervised-improvement`.

**Added:** a complete supervised core loop for shadow message-prompt data.
`/semantics classify TEXT` records observations in the current session;
`/improvements` detects repeated invalid responses and exposes the selected
version. Explicit proposal generation sees bounded failure metadata and incumbent
instructions, with no tools or evaluation inputs. Operator-frozen experiments
use the existing paired evaluator. Inspection shows exact prompt data, gates,
versions and measurements before explicit adoption.

**Selection and recovery:** immutable `PromptChange` events require the latest
passing completed paired run, at least one measured improvement, exact versions
and matching evaluator/provider configuration. Adoption/rollback occur only at
an idle session boundary. Selection persists per alias within the session and
uses evaluated limits; changed declarations suspend it. Rollback restores exact
preceding prompt/configuration bytes without inference. Replay reconstructs
facts without rerunning experiments. Controls retain shared permissions/budgets,
editable drafts, interruption and new-work priority. These are core controls,
not plugin expansions or model tools. Automatic policy stays empty.

**Real local evidence:** the first offline smoke had a malformed proposal and an
incomplete normal-memory project attempt. The diagnostic repeat completed both
project tasks but both proposals omitted the required top-level fields. Explicit
response-shape instructions fixed generation; all failed reports are retained.
The final fixed-gate run completed both real project/proposal/evaluation journeys,
with plugins absent and normal memory enabled. Proposals took 42.931/50.131 seconds.
Both incumbents and candidates answered 4/4 public cases correctly, so both
candidates failed the minimum-improvement gate and explicit adoption was refused.
No gate was weakened. This proves the hold path, not quality improvement.
A preceding final-source attempt failed both project journeys before generation;
that failure is retained. The unchanged repeat was much slower than the earlier
complete pass (2.395/2.356-second proposals), so runtime reliability remains open.

The [operator guide](../../supervised-improvement.md) and
[evidence handoff](../../handoffs/2026-09-07-supervised-improvement/README.md)
record the workflow, retained failures and runtime bounds. The final report's
core/driver hashes match this implementation. Controlled provider fixtures prove
successful selection, exact rollback and replay separately from local quality.

**Remaining:** M4 held-out semantic qualification, assessment candidate evaluation
and explicit external-agent reconciliation/handoff. Broader automatic policies,
isolated source edits and daily-use improvement evidence remain later work.
Memory and agent-swarm remain plugins; the improvement lifecycle belongs to core.

**Final repository validation:** **1,495 passed, seven skipped, six warnings in
368.40s**, with `TERM=xterm-256color`; locked offline sync, Ruff, whitespace
checks, sdist/wheel build and clean Python 3.13 wheel smoke (67 modules) passed.
Wheel core bytes match the source. The initial `TERM=dumb` run had one math
width failure reproduced from merged main; Rich forced 80 columns despite the
test requesting 78. No math code, assertion or dependency changed. Both the
initial failure and successful repeat are retained. The focused improvement/
semantic suite passed all 59 tests, including provider-exception body redaction.

## PR25 review — catalog upgrades suspend adopted prompts

The review identified that the evaluator fingerprint included catalog data but
omitted `catalog.py`, whose resolution logic selects the inference route and
endpoint. The fingerprint now includes that implementation. A regression first
reproduced the missing suspension, then verified resume with changed catalog
source: the builtin prompt is selected, the old evaluation cannot authorize
adoption, and rollback remains available without inference.

PR25's Python 3.12 CI also exposed the context-cap status bar waiting for its
one-second statistics timer. The status bar now renders at mount. A regression
pauses periodic statistics updates and checks the actual terminal compositor,
so the fix does not depend on a longer test sleep. Both new regressions failed
before the fixes. The 69 focused improvement, semantic, catalog and context-TUI
tests pass on Python 3.13, and Ruff and whitespace checks pass.

The earlier full-suite, wheel and GPU reports describe `55c1367`, before these
review corrections. They remain retained as evidence for that revision.

**Python 3.12 validation:** 165 tests passed in 201.05s across the same focused
suites plus the full main TUI suite, including its localhost MCP cases and the
previously failing context-status journey. The Python 3.12 environment was
installed from the existing lockfile separately from the project's Python 3.13
environment. No dependency or lockfile changes were required.

## September 8 — M4 paired assessment prompt evaluation

[PR25](https://github.com/c-daly/harness/pull/25) merged at `700e5f0`; reviewed
head `8c03119` passed Python 3.12/3.13 CI and automated review. This slice starts
from that merge on `feat/assessment-evaluation`.

**Added:** paired comparison for context-selection and progress-assessment
prompt candidates, using the existing durable evaluator and improvement records.
The operator supplies a bounded `AssessmentExperiment`; `harness improve ...
compare ASSESSMENT.json` and `/improvements compare ASSESSMENT.json` link recent
live observations, record exact candidate/plan bytes, and run the frozen pairs.
Saved plans can also use `harness semantic evaluate`. Candidate/result inspection
shows assessment prompt data without starting inference.

**Oracle and control boundaries:** immutable critical cases cover stale,
unavailable, injected and ambiguous context, selection caps, unchecked completion,
failed checks, review, active work, and interrupted executions. Expected results
must satisfy the normal scope/evidence validators. Only each current input and
prompt reach inference. The grader compares complete ID sets and reason/action;
reports include rules baselines, partitioned sample counts, abstention and latency.
The assessment implementation is part of the evaluator fingerprint. Budget,
permission, timeout, cancellation, configuration drift and crash/replay semantics
use the existing shared runner.

**Fixture provenance and interface:** frozen progress snapshots never update the
live task. Assessment observations carry their evaluation run and the terminal
labels them as fixtures; they cannot recursively become live failure evidence.
Comparisons require an idle boundary, preserve the draft, and settle on Esc or
new work. No comparison changes task acceptance, routing, permissions, or prompt
selection. Even passing assessment results cannot use message-prompt adoption.
Assessment candidate generation/adoption remains open; these candidates are
operator-authored.

**Real offline evidence:** two public runs completed both comparisons on the
pinned Qwen3-8B/b9603 CUDA profile with external networking disabled and no plugins.
Each run made 30 paired calls plus two real seed assessments, preserved task state,
and stopped its owned runtime. Neither run injected model failures. Both had the
same scores: context builtin **4/6**, candidate **3/6**, rules **4/6**; progress
builtin **0/9**, candidate **2/9**, rules **9/9**. Both candidates failed fixed gates.
Final maximum incumbent/candidate latency was **277/318 ms** for context and
**488/422 ms** for progress. Fast responses did not justify adoption.

The initial driver missed llama.cpp's version on stderr; its report is retained.
Capturing both streams fixed the metadata, and the final report's hashes match
all core/driver source bytes. Prompt text, cases and gates did not change between
runs. Public examples labelled `held_out` are explicitly **not** genuine held-out
qualification. These measurements do not replace the earlier M3 normal-memory/
native-task gate or prove daily-use model quality.

The [operator guide](../../assessment-evaluation.md), public JSON examples, and
[evidence handoff](../../handoffs/2026-09-08-assessment-evaluation/README.md)
make the result reviewable. **138 focused tests passed on Python 3.12 in 29.30s**,
including terminal rendering and cancellation. Ruff and whitespace checks pass.

**Remaining:** M4 held-out semantic qualification, assessment generation/adoption,
and explicit external-agent reconciliation/handoff. Deterministic task control
remains appropriate. Broader adoption policies, isolated source experiments and
daily-use improvement evidence remain later work. Memory and agent-swarm remain
plugins; the evaluation machinery belongs to core.

**Final repository validation:** **1,530 passed, seven skipped, six warnings in
334.88s**, using Python 3.13 and `TERM=xterm-256color`, including localhost MCP
integrations. The skips remain the absent Anthropic/Ollama conformance fixtures
and opt-in live Antigravity check. Ruff, whitespace, sdist/wheel build and clean
wheel smoke passed; all 68 shipped modules import, and installed wheel bytes
match all 69 core source files. The first offline wheel install lacked cached
package-index metadata. The successful install fetched metadata with versions
constrained to the unchanged lockfile; it is distinct from the network-free GPU
evidence. Both packaging logs and repository results are retained in the handoff.

## September 8 — M4 explicit external-agent reconciliation and handoff

[PR26](https://github.com/c-daly/harness/pull/26) merged at `e576a8e`; reviewed
head `b02d1c7` passed Python 3.12/3.13 CI and automated review. This slice starts
from that merge on `feat/external-handoff`.

**Added:** durable operator reconciliation and a single-use external-to-native
continuation. `/handoff inspect`, `record`, `show`, and `run` expose the checkpoint
and remaining assignment in the existing interface; the headless CLI offers the
same controls. Provider-native effects remain opaque and require inspection and
an explicit stopped-process attestation. An interrupted tracked external task
cannot use ordinary retry to silently replay its original assignment.

**Authority and continuity:** a frozen checkpoint carries the original task,
criteria, artifacts, source permissions, workspace/context bindings, and budgets.
Only a configured, explicitly selected inference destination can continue it.
Current dispatch policy and the source permission floor both apply after rewrites.
The finite allowlist permits each exact native file call at most once, with no
shell or delegation. Completed file targets cannot be modified by the continuation.
The full conversation remains durable; only the remaining assignment and checkpoint
enter fresh working context. Previous successful tool checks retain their original
evidence; review and acceptance remain explicit. Chained handoffs preserve the
original authority floor and completed effects.

**Interruption and limits:** cancellation consumes the plan and requires another
reconciliation. A started native file thread settles before the terminal fact,
including repeated cancellation; the UI explains the wait. Replay performs no
inference. Unknown source hooks, source-version changes, missing/changed context
adapters, legacy attempts without scope evidence, and unaccounted child sessions
hold execution. This bounded implementation does not claim portable M5 workflows
or automatic reconciliation of external effects.

The [operator guide](../../external-handoff.md) and
[evidence handoff](../../handoffs/2026-09-08-external-handoff/README.md) record
commands, constraints, runtime evidence and retained failures. Memory and
agent-swarm remain plugins; core imports neither plugin's internals.

**Remaining:** M4 held-out semantic qualification and assessment adoption; broader
live-agent/normal-memory handoff evidence; M5 heterogeneous workflow supervision
and source-edit improvement experiments; M6 sustained daily-use qualification.
The earlier failed semantic quality gates remain failed.

**Repository validation:** **1,562 passed, seven skipped, six warnings in 348.24s**
on Python 3.13, including real subprocess/MCP and final terminal rendering.
**124 focused tests passed on Python 3.12 in 8.98s.** The first 3.12 invocation
failed three existing subprocess fixtures because their `python3` shebang found
an environment without MCP; activating the locked environment with `uv run`
fixed the invocation without changing those fixtures. Both results are retained.
Ruff, whitespace, offline sdist/wheel build and clean offline wheel smoke pass;
all 70 modules import and wheel bytes match all 71 core Python source files.
No dependency or lockfile changes were needed.

**Real offline handoff:** both public Qwen3-8B CUDA runs pass all 15 checks. A
controlled Codex-compatible process writes through real MCP, leaves an opaque
native effect, fails, and is reaped before terminal facts. After operator
reconciliation and session restart, the real local model writes only the remaining
artifact, retrieves project context, retains prior evidence, leaves review pending,
and replays without inference. Final continuation time is **11.648 seconds**
including cold startup (initial revision: **17.505 seconds**). The final report
matches all 71 core source files plus the driver. No external network, live
subscription agent, or memory plugin was used; those broader boundaries remain
unqualified. The existing managed endpoint was left untouched.

## PR27 review — discoverable handoff controls

The review found `/handoff` missing from built-in `/help`. Help now lists
`/handoff inspect|record|show|run`. Both existing help tests pass, and a
100-column final-compositor smoke confirms the complete action list is visible
without adding session events or invoking inference. Ruff and whitespace checks
pass. The full-suite, wheel and offline handoff reports above remain evidence
for `0598460`, before this help-text correction.
