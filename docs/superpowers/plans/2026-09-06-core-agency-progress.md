# Core agency implementation record

Current implementation branch: `feat/evidence-escalation`, based on merged
`main` at `f0554c3` (PR44, per-session native file conflict detection).
Previous branches: `feat/native-file-conflicts`, `fix/file-mutation-lifetime`, `feat/coordination-admission`, `feat/typed-coordination-results`, `feat/portable-task-export`, `feat/handoff-destination-recovery`, `feat/handoff-failure-qualification`, `feat/context-profile-comparison`, `docs/context-experiment-results`, `feat/context-eligibility`, `feat/context-selection-qualification`, `fix/bounded-compaction`, `fix/model-selection-continuity`, `feat/local-model-management`, `feat/assessment-improvement`, `feat/external-handoff`, `feat/assessment-evaluation`, `feat/supervised-improvement`, `feat/local-scheduling`, `feat/task-fallback`, `feat/semantic-context-progress`, `feat/local-assistant-m3`, `feat/resident-workflow`, `feat/task-completion-evidence`, `fix/inference-client-lifecycle`, `feat/local-response-profiles`, `feat/local-tool-recovery`, `feat/local-tool-planning`, `feat/local-qualification`, `feat/semantic-evaluation`, `feat/local-context`, `feat/local-readiness`, `feat/agent-runtimes`,
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
| M3 local core assistant | Complete for the measured CUDA profile | Qwen3-8B passes six full offline project journeys: twelve exact native file writes, changed-record answers after restart, real cancellation, normal memory, and visible task/context status. Four missing-assets/startup-exit recovery cases pass. Shipped profiles and a launcher reproduce the workflow. Actual use exposed missing startup configuration in the normal catalog; the provisioned host's local aliases are now connected. Core `/models` now inspects public GGUF metadata and registers installed weights with startup profiles, with ordinary CLI/TUI CPU smoke evidence. Runtime installation, weight downloads and broader CPU/daily-use qualification remain outside this gate. |
| M4 bounded semantic agency | In progress | All three shadow functions now exist with explicit CLI/TUI access and evidence validation. The public 8B comparison failed context/progress gates; deterministic behavior remains. Bounded task-preserving fallback and session-tree local scheduling are implemented. Message-prompt pairing and earlier context/correction experiments use core improvement records. Supervised message-prompt proposal/evaluation/adoption/rollback is implemented. Paired context/progress candidate evaluation is implemented with explicit operator controls. Explicit external-to-native reconciliation and bounded handoff are implemented. Supervised assessment proposal/adoption/rollback is implemented per function/model. Core now filters context eligibility before inference and resolves empty eligible sets without a model call. Required normal-memory loss, busy preflight, destination loss after a write and Esc during continuation now have bounded offline terminal evidence. Truncated OpenAI-compatible streams cannot fabricate completion. Held-out quality and broader handoff qualification remain. |
| M5 heterogeneous work / plugins | In progress | Core CLI/TUI exports a documented Markdown/JSON continuation package with task evidence, context snapshots/references, artifacts and original provenance. A controlled independent frontend continues after source-database removal. Coordination now consumes typed child outcomes, preserves partial work/disagreement/provenance, settles cancelled siblings and exports its reports; `/coordination` inspects them. Pure coordinators have separate active admission and an overall deadline while retaining shared descendant/depth limits; interrupted starts remain unconfirmed. Native file mutations retain per-path ownership through cancellation and settle before descendant terminal facts. Per-session content observations now reject stale writes/edits and support reread recovery. Escalation applies frozen active-task requirements to both cheap and premium participants, with read-only evidence checks, inspection and export. Shared token/cost budgets, broader edit ownership, live mixed-agent supervision, installed-plugin reconciliation, broader portable continuation, isolated improvement patches and rollback remain. |
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

## September 8 — M4 supervised assessment prompt lifecycle

[PR27](https://github.com/c-daly/harness/pull/27) merged at `d33b4e2`; reviewed
head `b4a57e9` passed Python 3.12/3.13 CI and automated review. This slice starts
from that merge on `feat/assessment-improvement`.

**Added:** the existing supervised prompt lifecycle now supports context selection
and progress assessment. An operator can request a candidate after two distinct
live invalid-output observations, evaluate against an operator-owned frozen suite,
explicitly select a passing shadow prompt, and restore the preceding version.
Selections are independent per model/function. Message records retain their legacy
default and policy. The source/evaluator fingerprint continues to suspend selections
when implementation or declared runtime configuration changes.

**Boundaries:** proposal generation sees incumbent instructions and bounded failure
metadata, never private context/task inputs or evaluation answers. Assessment
fixtures cannot seed live failure evidence. Function/policy matching, completed
paired-run provenance, the latest passing result, fixed critical cases and exact
incumbent/configuration binding are enforced before selection. Failed/inconclusive
results stay held. Assessment prompts remain advisory; no candidate grants itself
authority or changes task state, acceptance, routing, schemas, or source files.

**Interface:** `/improvements` shows all three functions and their controls.
`propose`, `adopt`, and `rollback` accept `message|context|progress`; the CLI offers
`--function`. `/semantics context FILE.json` completes the TUI path for supplied
scoped context, alongside existing progress assessment. Live observations display
selected version/change provenance; evaluations use explicit frozen artifacts.
Replay and rollback do not invoke inference. A new comparison binds the currently
selected assessment incumbent.

The [operator guide](../../assessment-improvement.md), public experiment shapes,
and [evidence record](../../handoffs/2026-09-08-assessment-improvement/README.md)
separate implementation controls from model quality. Memory and agent-swarm remain
plugins; this lifecycle belongs to core.

**Remaining:** M4 genuine held-out semantic quality and broader live-agent/memory
handoff qualification; M5 heterogeneous supervision, portability and isolated
source-edit experiments; M6 sustained daily-use qualification. Earlier failed
quality gates are not reclassified as passes by adding adoption machinery.

**Repository validation:** **1,588 passed, seven skipped, six warnings in 351.64s**
on Python 3.13, including MCP/process integrations and final TUI rendering.
**176 focused tests passed on Python 3.12 in 39.63s.** Ruff, whitespace, locked
offline sync, sdist/wheel build and clean offline wheel smoke pass. All 70 modules
import; the wheel matches all 71 core Python files. No lockfile/dependency changes
were required. The retained initial integration failures exposed the missing
context TUI action and an obsolete assertion that assessment adoption was
unavailable; the final UI/CLI paths pass.

**Real local outcome:** both offline Qwen3-8B runs produced two invalid responses
per function. Context generated a candidate, but incumbent/candidate/rules each
scored **4/6**, with critical failures and no improvement, so adoption was held.
Progress generation returned the unchanged incumbent instructions and was rejected
before candidate/evaluation recording. No prompt was adopted in either run.
Final context maximum incumbent/candidate latency was **303/295 ms**. The final
driver adds rejection diagnostics; the initial report is retained. Frozen cases,
gates, generation settings and core source were unchanged between runs. Final
hashes match all 71 core files and three qualification scripts. Successful
selection/rollback is verified with controlled providers; genuine held-out model
quality remains unproven. No plugins or external networking were used.

## September 8 — ordinary local-session setup and connection diagnostics

Actual use found a gap outside the earlier M3 launcher gate: `/model local`
and `/model local-instruct` failed with `NetworkFailed`. The normal catalog
pointed three local aliases at an absent server on port 8080 and contained no
readiness/startup profiles. The working qualification catalog was separate.

The provisioned host now has explicit on-demand profiles for the existing 8B
and 4B files in its normal catalog, backed up before editing. The pinned runtime
and GPU libraries were extracted from the already installed image into ignored
local assets. Other model entries are unchanged; the old `local36` server is
still unavailable. This repair does not provide general Hugging Face discovery,
an installer, additional weights, fine-tuning, or larger-model qualification.

The ordinary CLI performed an independently checked native JSON write. Actual
terminal `/model` switches then started 8B and 4B inference, returned exact replies,
replaced the idle owned runtime, and stopped it on exit. No special context
profile was used. These native-tool checks had MCP/plugins disabled and do not
qualify large plugin inventories or long cloud-agent histories. The
[repair evidence](../../handoffs/2026-09-08-normal-local-session/README.md) retains
results and the diagnostic-driver failure preceding the complete terminal run.

Core now gives typed loopback transport failures a local-server origin and
startup guidance instead of displaying the SDK's OpenAI-branded body. Failure
classification, retry/fallback behavior and sanitized event recording remain.
The local usage/readiness guides explicitly distinguish the launcher catalog
from the ordinary catalog and explain the required setup.

Validation: **269 affected provider/runtime/fallback/terminal tests passed in
208.62 seconds** before rebasing onto PR28's merge (`c825622`). On that updated
base, **59 provider/inference/client tests passed in 2.18 seconds** and **eight
model-switch/status terminal tests passed in 20.32 seconds**. The first
post-rebase client invocation lacked localhost socket permission and produced
12 fixture-bind errors; rerunning with that permission passed without source
changes. Ruff and whitespace checks pass. The full repository suite is left to
CI for this scoped error-message/documentation change.

## PR29 review — sanitize route-only local endpoint failures

The review found that the startup-profile validator rejected local URLs containing
credentials or query/fragment data, causing the diagnostic helper to fall back to
the raw SDK body. Loopback detection now operates independently and reconstructs
only the numeric server origin. Credentials, paths, queries, fragments and IPv6
scope identifiers are omitted; invalid ports/schemes receive local guidance, and
unparseable endpoints receive a generic sanitized connection diagnostic.

The new regression cases reproduced the leak before the fix. **167 affected
provider, catalog, inference/client, local-resource and fallback tests pass in
5.35 seconds** after the fix. Ruff and whitespace checks pass.

## September 8 — numeric inline LaTeX in normal replies

The user's local-model Jupiter reply contained valid `$1.898 \times 10^{27}$`
source. The renderer's currency guard rejected numeric spans containing spaces,
so it never reached the existing Unicode math renderer. The guard now recognizes
explicit mathematical notation inside a bounded inline span while preserving
currency, code and incomplete delimiters.

Regression tests reproduced both the extraction failure and the visible terminal
failure before the change. **34 math tests passed in 0.93 seconds** and **five
terminal math/Sixel tests passed in 6.12 seconds**, including final-compositor
assertions for `1.898 × 10²⁷` and its surrounding prose. Ruff and whitespace checks
pass. The model response and stored conversation were not rewritten.

## September 8 — core local-model discovery and registration

After PR29's local connection repair and PR30's numeric LaTeX fix merged, the
next tranche addresses the manual setup gap. `/models` and `harness models`
list configured models/agents, inspect public Hugging Face GGUF metadata, and
register an installed single-file GGUF with on-demand native llama.cpp startup.
Metadata is bounded, revision-pinned and cached for offline use. Optional source
verification compares the entire local file's SHA-256 and size with the selected
Hub artifact. Registration records provenance, preserves existing catalog bytes,
saves a backup and publishes a new alias atomically. It refuses duplicate aliases,
detected concurrent edits, invalid files and cancelled verification.

The terminal keeps the composer available, displays verification progress and
uses existing cancellation/rebuild/shutdown ownership. Registration does not
change the current model; ordinary `/model` selection applies the new catalog.
These are operator controls in core, independent of inference, memory and
agent-swarm. No model-download or runtime-install service is introduced.

The [setup guide](../../model-management.md), updated user guide and
[evidence record](../../handoffs/2026-09-08-model-management/README.md) distinguish
registration from hardware fit and model quality. The old user-guide claim that
the 35B launcher was fast on this GPU is removed; prior failures remain evidence.

**Real check:** public Hub inspection returned five 8B GGUF variants and the
expected Q4_K_M hash. Offline registration against that cached commit produced a
working CPU profile using existing assets, leaving the occupied GPU server and
normal catalog alone. The actual CLI read a project file and wrote independently
verified JSON. A terminal pilot registered another alias, captured typing during
visible hashing, switched through `/model`, and received the exact requested
reply through real inference. Both test-owned runtimes stopped; both ports were
confirmed closed. The two earlier terminal diagnostic failures are retained as
driver failures, not complete journeys.

**Validation:** 40 model-management tests and five new terminal journeys are
included in the suite. Python 3.12 passed **109 affected tests in 31.28 seconds**.
The restored Python 3.13 full-suite run had **1,683 passes, seven skips and three
subprocess-fixture failures in 376.77 seconds**. Correcting its invocation so
child `env python3` processes used the virtual environment passed **all 29
external-runtime/Codex tests in 6.29 seconds**, including those three failures.
No source change was required for that rerun. An earlier run was invalidated by
a shared-environment rebuild; the evidence record retains both environment
mistakes. Ruff and whitespace checks pass. The sdist/wheel build and clean offline
wheel command smoke pass; wheel bytes match all 73 core Python files. No
dependency or lockfile changes were made.

**Remaining:** managed downloads, runtime installation, split GGUFs, capacity
measurement and larger-model optimization; M4 held-out semantic improvement,
M5 heterogeneous supervision/portability and M6 sustained daily-use qualification.
One useful CPU write and terminal reply do not expand the existing measured M3
gate into general CPU or larger-model qualification.

## PR31 review — retain catalog saves that race publication

The review correctly identified a gap after the final byte comparison: an
external editor could save before replacement, and the backup still contained
the older snapshot. Three new regressions reproduced loss of in-place saves,
editor-style renames and late file creation on the reviewed commit.

Existing-catalog publication now uses Linux/WSL2 atomic file exchange to retain
the actual displaced inode under a unique backup name. A detected late edit or
unverifiable backup produces a reconciliation message through the shared CLI/TUI
path. The retained inode also receives writes completed through an editor's
already-open descriptor. It is never removed by successful-operation cleanup or
post-exchange interruption/sync failures. No rollback can overwrite another save.
Creation uses exclusive publication; unsupported exchange fails closed.

**Validation:** **77 affected model-management, terminal, catalog and log tests
passed on Python 3.13 in 22.90 seconds**; **55 model-management/terminal tests
passed on Python 3.12 in 12.50 seconds** using its separate environment. The
regressions include actual filesystem exchange, the final publication boundary,
open-descriptor writes, exclusive creation, unsupported exchange, disappearance,
post-exchange interruption and directory-sync failure. Ruff and whitespace checks
pass. PR31's original Python 3.12/3.13 CI jobs passed on `8299839`; checks for this
review correction are separate. No inference/runtime configuration was changed.

## Everyday workflow: preserve catalog selection across resume

PR31 merged at `ca5618c`. Following the project-wide status review, the next
priority is ordinary workflow evidence: switching, long histories, network loss,
normal memory, interruption and restart. The first three regressions reproduced
an actual gap: `--continue` forgot the selected alias and used Echo; `/resume`
used the model from the session being left; an unavailable target alias was not
checked before teardown.

Catalog preferences now have a durable `model_selected` event carrying the alias
and routing pin. Startup, explicit switches and resume overrides record it;
deferred choices wait for the turn boundary. Resume restores the target choice
against the current catalog, including prices and delegated defaults. Routing,
fallback and internal calls do not become preferences. Explicit `--model` wins.
Missing or invalid saved aliases fail with recovery instructions; TUI preflight
preserves the current session. Kernel configuration rechecks the actual replay
under its writer lock before publishing a resumed boundary. Existing context
profile inheritance uses that same locked replay.

Older logs did not record the selection's pin and may have unrecorded `/model`
changes. They keep the previous startup/default behavior; one explicit `--model`
establishes a saved preference. This change does not restore grants or fallback
authorization from past events, persist unsent drafts, or qualify other providers.

**Validation:** three regressions failed on merged main before implementation.
The final affected group passed **216 tests on Python 3.13 in 241.00 seconds**;
the separate Python 3.12 environment passed **36 continuation/replay tests in
3.59 seconds**. Two earlier expanded cases had fixture mistakes (an unavailable
message helper and an ineffective catalog substitution); the corrected cases
are included in the final passing group. No provider behavior was weakened.
Ruff and whitespace checks passed; source distribution and wheel built offline.

The [real local evidence](../../handoffs/2026-09-08-model-continuity/README.md)
uses installed Qwen3-8B Q4_K_M and llama.cpp b9603 on CPU. Three separate
processes share one session: TUI `/model` from Echo, headless `--continue`, then
TUI `--continue`. Both resumes omit `--model` and recall the synthetic project
codename exactly. Final compositor checks cover the alias, answer and unsent
draft. Three owned runtime starts have three stops; the test port is closed and
the normal user catalog is unchanged. This is a narrow continuation smoke test,
without MCP/plugins or network isolation; M3's existing qualified profile and
the broader M6 daily-use gate retain their original scope.

The agreed priorities remain: finish ordinary workflow checks (next, long-history
switches into smaller local contexts), demonstrate one useful held-out M4
semantic improvement, run a bounded larger-model capacity experiment, then
extend mixed-agent continuity and isolated source-improvement workflows. The
current self-improvement mechanisms still have no demonstrated useful local
quality gain. This continuity fix does not close that gate.

## PR32 review — keep legacy resume baselines out of saved preferences

The review identified an unconditional selection write during kernel resume.
For legacy logs, it turned a routing default or departing TUI selection into
durable preference. Administrative improvement/assessment commands could also
replace the conversation's saved model. Both original CI versions failed two
improvement-command tests when this unintended write encountered their injected
provider factory.

Resume now writes a preference only when the caller explicitly marks a
conversational model selection. Both ordinary CLI paths set that intent for
`--model`; the TUI's `/model` already records its own applied selection.
Inherited preferences remain in the log without another write. Routing pins
alone, repeated legacy resumes and administrative model overrides do not
establish or replace preference. Explicit selection also takes precedence over
inheritance inside kernel construction.

Six new regressions failed on the reviewed code before the fix. The final
coverage also exercises headless and terminal CLI paths, changing/removing an
incidental routing default, repeated TUI resumes with pinned/unpinned departing
models, explicit selection after legacy resume, and administrative overrides of
both legacy and already-recorded preferences. The terminal driver yields to
subscription workers between rebuilds, as ordinary UI commands do.

**Review validation:** **155 affected tests passed on Python 3.13 in 60.36
seconds**, and **64 continuation/improvement/resume tests passed on Python 3.12
in 22.78 seconds**, using the separate environment. Both previously failing CI
cases passed. Ruff, whitespace checks and offline source/wheel builds passed.
Full-suite PR checks for the review correction are separate from these local
results and the original `52e719f` CI failures.

## Everyday workflow: recover an oversized history with bounded compaction

PR32 merged at `cedbe09` after its review correction passed both CI versions.
The next regression reproduced `/compact` sending an entire oversized history
back to the smaller model and failing before summarization could begin.

`Kernel.compaction` now owns explicit, bounded rolling summarization. It prepares
the canonical folded transcript, verifies and includes text sidecars, and plans
fragments before dispatch. Each call carries the prior summary and a new fragment
as historical data, with no executable tool messages. Request, response, source,
portion-count and whole-operation limits are enforced. Incomplete, empty or
oversized summaries fail without replacing history. Cancellation, provider
failure, concurrent transcript changes and unsupported images also retain it.
One final event replaces all current messages only after every portion succeeds;
replay matches live state even after an earlier partial compaction.

`/compact [inference-alias]` displays preparation and portion progress, preserves
drafts, and uses existing queue/cancellation handling. An explicit inference
alias can summarize an external agent's history without changing the selected
agent or routing pin. Internal calls are pinned to the planned model, audited
through the normal dispatcher and charged normally. Byte guards reduce requests
according to catalog context metadata; they do not claim exact token counts.

The user's tool-discovery question also exposed ambiguous `/tools` output. The
listing now explains agent invocation, includes short descriptions, and supports
`/tools <name>` for full descriptions and parameters. Agent tools such as
`ensemble` and `consult_panel` remain distinct from interface slash commands.

[Real local recovery evidence](../../handoffs/2026-09-08-bounded-compaction/README.md)
includes a 24,283-token request rejected by an 8,192-token llama.cpp context,
followed by nine successful bounded CUDA calls in 25.788 seconds and a 1.714-second
continuation retaining early/late facts and the pending task. The probe exposed
and fixed generic SDK labeling of llama.cpp's structured context rejection.
Earlier CPU timeout and incomplete CUDA output are retained as failed attempts;
they prompted explicit timeout wording and a more concise summarization prompt.
The host catalog is unchanged and all owned test runtimes were stopped.

**Validation:** the full Python 3.13 suite passed **1,740 tests, seven skipped**;
after the final timeout/prose wording changes, **27 affected Python 3.13 tests**
and **76 Python 3.12 tests** passed. The evidence handoff records exact runs and
their limits. This is a narrow development recovery smoke, not held-out summary
quality, CPU qualification, useful M4 improvement, or completion of M6.


## September 9 — Context-selection development and explicit held-out gates

PR33 merged at `a4f3678` after both Python CI versions and the portability review
fix passed. This tranche targets M4's missing evidence of a useful semantic
improvement. Memory and agent-swarm remain plugins.

**Core change:** paired plans, message prompt experiments, and both assessment
experiment paths now accept frozen `min_held_out_correct` and
`min_held_out_improved` counts. Previously, fixing one known regression could
satisfy a plan despite every held-out answer remaining wrong. The common verdict
now enforces declared held-out minimums and the adoption controls refuse a
failing result. Strict counts cannot exceed the held-out partition size; defaults
of zero preserve old records. Reports expose the frozen gates. Operator-labelled
partitions still do not prove case independence.

**Measured result:** two operator-authored context-selection candidates were
tested on the six public development cases, under a protocol limiting candidate
development to two prompts. Qwen3-8B Q4_K_M ran offline with llama.cpp b9603,
28 GPU layers, 4,096-token context, thinking disabled, and presence penalty zero.
The container had four CPUs, 4 GiB host RAM, and no swap. No user runtime or
catalog was changed; owned test runtimes stopped.

- Candidate 1 scored **5/6**, against builtin **4/6** and lexical rules **4/6**.
  It fixed unavailable-context selection but still guessed an ambiguous subject.
- Candidate 2 scored **5/6** and passed all five critical cases, but lost a
  semantic paraphrase the incumbent got right. Both candidates failed their
  unchanged development gates. Maximum candidate latencies were 1,571 and
  1,663 ms; total latency ratios were 1.021 and 0.944.
- An earlier setup attempt timed out at 30 seconds before inference. The scored
  profile's partial offload and 120-second startup allowance were set before
  any scored response. Its first load took 53.7 seconds; a later cached load
  took 4.5 seconds. Candidate 1's first seed timed out. All paired cases were
  measured. These observations are retained rather than treated as passing
  cold-start qualification.

No confirmation cases were authored or evaluated, no prompt was adopted, and
M4 remains open. The intended later confirmation gates (29/32 correct, three
held-out improvements and a three-answer advantage over rules, no critical
failures or regressions, existing latency bounds) remain unconsumed. All public
example cases, including their `held_out` partition, are development evidence.

The [handoff](../../handoffs/2026-09-09-context-selection/README.md) retains the
protocol, candidate prompts, exact experiments/catalogs, failed setup, model
response events, paired reports, and source hashes. The new public replay driver
checks pinned assets and isolation, refuses existing attempt directories, saves
inputs before inference, preserves failures, and checks adoption refusal.

**Validation:** the full Python 3.13 suite passed **1,746 tests**, with seven
skips and six existing MCP warnings in 412.92 seconds. Focused checks passed
103 tests on both Python 3.12 and 3.13; the final eight holdout-gate tests,
including two additional message-path cases, also passed on both. Ruff,
whitespace checks, sdist/wheel build, and clean wheel install/import passed;
all 75 core file bytes match the wheel. A real offline replay of candidate 2
reproduced 5/6 versus builtin 4/6, refused adoption, preserved task state and
builtin selection, settled calls, and stopped the owned runtime. This is public
reproduction evidence, not a third development candidate or fresh confirmation.

**Next:** keep the builtin, and test a distinct bounded strategy such as applying
context eligibility in code before model relevance selection. That change still
requires its own development and fresh confirmation evidence. Broader M4 handoff
qualification, M5 heterogeneous supervision/portable continuation/source-edit
experiments, and M6 daily use remain open.


## September 9 — Core context eligibility before inference

PR34 merged at `eb1e6fb`, with Python 3.12/3.13 CI and review passing. The next
core change applies the existing available/current predicate before inference.
Only eligible summaries reach the model; the original scoped input remains in
its existing artifact, and `inference_input` records the filtered payload.

An enabled request within its original input-size limit now returns `no_match`
without inference when no candidates are eligible. It requires no model grant,
budget, runtime startup, or free model slot because no model work occurs. Other
requests retain normal permission, budget, readiness, and output validation.
The record marks `decision_source: eligibility`, and CLI/TUI inspection visibly
says “Core eligibility” and “no model call.” Old observations default to model
provenance. Core answers cannot seed prompt-improvement evidence; paired prompt
evaluation applies the same predicate to both arms and records each source.
Memory and agent-swarm remain plugins; no context is automatically injected.

**Measured public result:** one implementation variant, unchanged builtin prompt,
six public cases. The frozen baseline at `eb1e6fb` scored **4/6**; the changed
selection path scored **5/6**, consisting of **4/5 model answers** and **one
correct core answer**. The unavailable-context case changed from invalid model
output (963 ms) to a core no-match (0.2 ms recorded duration, no model call). All
previously passing cases stayed correct. Ambiguity still failed.

Both versions ran the existing Qwen3-8B Q4_K_M / llama.cpp b9603 profile offline
with 28 GPU layers, 4,096-token context, thinking disabled and presence penalty
zero. The container allowed four CPUs, 4 GiB host RAM, and no swap. Both settled
calls, preserved task and selection state, and stopped their owned runtimes.
Maximum filtered case latency was 912 ms; total recorded latency was 0.809
times baseline. These are sequential public development measurements, not a
randomized confirmation, model-learning claim, or performance certification.

The [handoff](../../handoffs/2026-09-09-context-eligibility/README.md) retains the
protocol, executed driver, original/filtered input blobs, model events, source
hashes, reports, and comparison. The baseline hashes match its Git object and
the filtered hashes match the core implementation. The semantic critical gate
still failed, so no fresh confirmation cases were authored or consumed.

**Validation:** full Python 3.13 suite **1,756 passed, 7 skipped**, six existing
MCP warnings in 377.06 seconds. Python 3.12's affected checks passed **140 tests**
in 29.19 seconds, including CLI and rendered terminal provenance. A real CLI
probe in a network-disabled container with no GPU, no model grant, and
nonexistent configured model/runtime assets returned core no-match; its log
contains zero model calls or runtime requests and ends cleanly. Ruff, whitespace,
sdist/wheel build and clean wheel installation/import passed; all 75 core files
match the wheel bytes.

**Remaining:** M4 reliable disambiguation, fresh semantic confirmation and broader
handoff evidence; M5 mixed-agent supervision/portability/source-edit experiments;
M6 sustained daily use. This closes a deterministic empty-context failure and
keeps model confidence separate from core correctness.


## September 9 — Reject remaining prompt and schema-order hypotheses

PR35 merged at `db71d51`, with both Python CI versions and review passing. This
follow-up changed no production behavior. Two bounded prompt trials and a
separate, predeclared one-variant schema-order experiment all failed development
gates; the temporary schema change was reverted.

- The first prompt appended a narrow unresolved-pronoun rule. It fixed the
  critical ambiguity case but incorrectly abstained on the memory-storage
  paraphrase: **5/6**, versus builtin **5/6** and rules **4/6**. It failed the
  no-regression requirement despite passing latency bounds.
- The second prompt clarified semantic matching within the supplied project
  context. It preserved the paraphrase but still guessed the ambiguous policy:
  **5/6**, with no measured improvement over the builtin.
- Raw responses selected IDs before the reason label, motivating a separately
  frozen source-format hypothesis. Requesting the reason first changed actual
  output order but still scored **5/6** and guessed the policy. Maximum recorded
  latency was **2,057.55 ms**, above the unchanged 2,000 ms gate. This sequential
  shared-host comparison does not prove that field order caused the slowdown.

The two prompt runs used the existing paired evaluator and passed its mechanics
checks, including actual adoption refusal. Each six-case score includes the
one deterministic core eligibility answer. All inputs remained public
development data; no fresh confirmation cases were authored or evaluated, and
no prompt was adopted. The model did not author the prompt candidates.

The [handoff](../../handoffs/2026-09-09-context-confirmation/README.md) retains both
protocols, exact candidate/configuration artifacts, raw model events, input blobs,
the rejected source patch/snapshot, reports and source hashes. All trials used
the pinned 8B/llama.cpp profile in network-disabled, resource-bounded containers.
Owned runtimes stopped; user model configuration remained untouched.

**Validation:** both frozen experiments retain failed verdicts under the core
grader. All 131 saved event envelopes, 26 referenced input/prompt blobs, and 231
source hashes checked successfully against the executed sources at `db71d51`
and the rejected schema snapshot. Review caught stale default runtime arguments
in both prompt reports: a documented metadata correction now separates weight
identity from the effective catalog command. The replay driver and a regression
check prevent recurrence; the original measurements and source hashes remain
unchanged. Core source and dependencies still match merged `db71d51`. Ruff and
whitespace checks passed. No new full-suite, packaging, or inference run is claimed.

**Decision:** keep merged behavior. Further wording-only tuning of this 8B profile
has not earned confidence. Next, compare alternative inference profiles/models
for the bounded semantic function, preserving the same critical and regression
gates and keeping fresh confirmation separate. Passing native tool-work gates
does not establish semantic-selection quality. M4 is still open, along with
broader handoff qualification and M5/M6 work.


## September 9 — Compare the installed 4B and 8B context profiles

PR36 merged at `bfe76e3`. The new public comparison driver and protocol were
frozen in `faab073` before inference. Four isolated blocks ran in 8B/4B/4B/8B
order with the same builtin prompt, six public cases, core eligibility filter,
4,096-token context, request temperature zero, and fixed correctness/latency
gates. The 8B profile retained 28 GPU layers; 4B used full offload. This compares
profiles on a shared host, not model size in isolation.

**Result:** both models scored **5/6 twice**, with **4/5 model decisions correct**
and **one correct core eligibility answer** per block. Every block guessed the
deployment policy for the ambiguous query. Neither 4B repetition improved or
regressed a scored answer. Both passed latency gates (candidate maxima **818 ms**
and **349 ms**, total ratios **0.331** and **0.267**) but failed the critical and
improvement gates. The first 4B unscored warmup timed out at **5,626 ms** including
overhead; that failure is retained separately from scored latency. No failed
block was retried, no configuration was tuned, and no fresh confirmation was run.

The [handoff](../../handoffs/2026-09-09-context-profiles/README.md) retains the frozen
protocol, reports, actual model events, input/prompt blobs, comparison and source
hashes. All blocks ran offline with four CPUs, 4 GiB host RAM and no swap; their
owned runtimes stopped. Core source, dependencies and user model settings are
unchanged. This does not qualify automatic context selection or demonstrate
successful self-improvement.

**Validation:** **29 focused tests passed on each of Python 3.12 and 3.13**.
Regraded 24 scored observations, checked all 28 observations against their
original events, parsed 224 event envelopes, and verified 32 blobs and 312 source
hashes against the execution freeze. The comparator rejects mismatched inputs,
prompts, profiles, sources and limits, and retains critical/regression/latency
gates. Ruff and whitespace checks passed; no new local full-suite, packaging or
UI result is claimed for this experiment driver and evidence update.

**Next:** keep context selection advisory and 4B available as a development
option. Resume the independent M4 live handoff/failure qualification track;
semantic quality remains open and needs a distinct frozen hypothesis before
more model work. M5 heterogeneous workflows/source-edit improvement and M6 daily
use remain pending. Memory and agent-swarm remain plugins.


## September 9 — Required-memory failure during handoff and resumed recovery

PR37 merged at `216762c` with both Python CI versions and review passing.
The independent M4 handoff track now has a frozen, bounded terminal journey
through required normal-memory loss and explicitly reconciled recovery. The
implementation and gates were committed at `5c6e399` before local inference.

**Core behavior:** operator controls distinguish preflight refusal from a failure
after the handoff starts. Started failures and returned incomplete results say
that the record was used and direct the operator to inspect and record a new
handoff. Provider exception bodies stay suppressed; existing cancellation and
single-use enforcement are preserved. This closes a recovery-instruction gap,
not a new authorization or automatic-retry path.

**Observed result:** the plugins-absent control passed **21/21 checks**, and the
normal-memory loss/recovery journey passed **30/30**. Stopping the owned memory
child caused required-context failure before inference. The draft and completed
files survived, the used record was refused, and the task remained unaccepted.
After session restart, a fresh reconciliation retrieved normal memory and wrote
only B. A and the inspected native effect remained intact. Exactly two successful
Harness writes existed across the source and continuation; earlier task evidence retained
its original provenance and replay required no inference.

The local continuations took **12.911 s** without plugins and **6.569 s** with
recovered memory, within the fixed 45-second limit. These are individual smoke
journeys, not latency distributions. Final-compositor checks covered failure,
reconciliation guidance, completed handoff and fresh context. The memory read
returned 13,802 bytes from the normal scoped subject; only its size/hash is retained.

The [handoff](../../handoffs/2026-09-09-handoff-memory-recovery/README.md) records the
protocol, metadata-only report and validation. Both journeys used real Qwen3-8B
inference offline, four CPUs, 4 GiB host RAM and no swap. The source was a controlled
Codex-compatible process with real MCP, not a live subscription agent. The installed
memory plugin and normal vault were mounted read-only; private temporary sessions
were removed. All owned runtimes, memory children and the container stopped.

**Validation:** full Python 3.13 suite **1,774 passed, seven skipped**, six existing
MCP deprecation warnings in 401.36 seconds. Python 3.12 affected suites **110 passed**
in 14.26 seconds. Offline build and clean wheel smoke passed; all 75 core files
match the wheel. All 81 recorded core/driver/profile hashes match the execution
freeze. Ruff and whitespace checks passed.

**Remaining:** M4 semantic held-out quality and other handoff failures, particularly
destination loss/busy handling and interruption during continuation. The existing
network/authentication fallback evidence remains separate from this memory test.
Live-provider qualification, M5 mixed-agent portability/source-edit improvement,
and M6 daily use remain open. Memory and agent-swarm remain plugins.


## September 9 — Destination loss, interruption and truthful stream completion

PR38 merged at `e248ac7`. The next M4 increment found a correctness bug in a real
SDK/process test: killing the local destination after B was written could produce
a fabricated normal finish marker and a completed handoff. The OpenAI-compatible
adapter now rejects LiteLLM terminal chunks without evidence of a provider finish
reason. Partial text and complete-looking tool arguments raise
`MalformedStreamError`; they cannot complete the attempt or dispatch those tools.
Client cleanup and sanitized failure reporting remain intact.

The core change was frozen at `f894004`. The public HTTP regressions cover EOF,
severed responses, text, tool proposals and valid completion. The terminal driver
covers busy preflight, process loss after a successful B write, and Esc during the
next stream, followed by session restart and explicitly reconciled recovery.
Memory and agent-swarm remain plugins.

**Evidence:** the final protocol, frozen at `439a173`, passed **6/6 offline journeys
and 172/172 checks**, with and without the installed normal memory plugin. Busy
preflight leaves the record unused. A started loss or cancellation consumes it,
retains completed B, and requires a new record for C. Exact stage writes, original
evidence, drafts, task identity, fresh context, visible outcomes and unresolved
operator review survived. Local recovery took **4.144–5.801 s**; all sessions
stayed within the fixed 45-second recovery bound. Replay needed no inference.

Three earlier development protocols remain failed: **4/6**, **5/6**, and **4/6**.
Corrections added observable owner outcomes, acknowledged terminal commands,
controlled consumption of the busy stream, and inspection that distinguishes
unapplied proposals from completed files. The controlled busy condition uses a
real stream with client backpressure, not proof of continuous GPU activity.
Earlier private sessions were deleted; the public rejected-C reproduction does
not retrospectively establish their exact refusal cause. These are bounded
smoke journeys, not held-out semantic confirmation or latency distributions.

The [evidence record](../../handoffs/2026-09-09-handoff-destination-recovery/README.md)
preserves all four protocols/reports. Execution used the unchanged M3 8B CUDA
profile, no network, four CPUs, 4 GiB host RAM and no swap. The external source was
a controlled Codex-compatible subprocess with real MCP, without subscription
credentials. Normal memory/vault mounts were read-only and only metadata was
exported. All qualification containers and Harness-owned model/memory children
stopped.

**Validation:** full Python 3.13 suite at the core freeze **1,785 passed, seven
skipped**, six existing MCP deprecation warnings (401.11 s). Affected Python 3.12
suites **239 passed** (44.64 s). Final driver and rejected-write reconciliation
regression **seven passed on each version**. Build and clean wheel smoke passed;
all 75 core files match the wheel. Ruff and whitespace checks passed. The full
suite predates only driver/inspection-test and evidence updates; core and
runtime dependencies did not change afterward.

**Remaining:** M4 semantic held-out quality is still open; context selection stays
advisory and no new model or improvement candidate was adopted. Live-provider
handoff qualification, M5 mixed-agent portability/source-edit improvement, and
M6 daily use remain open. This closes the named bounded destination fault cases,
not the whole core-agency roadmap.

**PR39 review:** the public HTTP fixture now inherits a socket held by the test
through startup, process loss and restart, removing the port-allocation race.
Every fixture launch checks that a competing bind fails, and each journey checks
both initial and resumed launches. The seven affected tests passed concurrently
on Python 3.13 (35.23 s) and 3.12 (36.00 s); Ruff and whitespace checks passed.
This changes test infrastructure only; the frozen core and offline reports remain
unchanged.

## September 9 — Portable task continuation through core export

PR39 merged at `5a79173`; both Python CI versions and review passed. The named
bounded offline destination-failure cases are covered, while M4 semantic quality
remains unqualified. This increment advances the independent M5 portability
track without promoting any semantic prompt or model.

**Core behavior:** `/export FILE.zip` and `harness export SESSION_ID FILE.zip`
produce a versioned Markdown/JSON package with the selected task's requirements,
recorded checks and review, attempts, original event references, verified result
artifacts, configured context snapshots and memory query references. Operator
handoff inspection notes travel as historical data. Source scope, execution
grants and provider bindings do not transfer. The
[format contract](../../portable-continuation.md) documents these distinctions.

Export opens no provider, invokes no plugin, performs no check and leaves the
source log unchanged. It refuses active task runs, unreadable records, damaged
artifacts and existing destinations. Publication is exclusive/atomic and the
archive is bounded to 32 MiB. The terminal prepares it off the UI thread; Esc
and new work can discard preparation without a late write. Core `/export` cannot
be redirected into plugin prompt execution. Markdown renders recorded text as
quoted data, including criteria containing headings, image links or terminal
controls.

**Portability evidence:** a public scripted-provider journey used the real native
file tools to read project context and write A, alongside a controlled memory
lookup. After export, its source database was deleted. A separate Python `-I -S`
frontend with no Harness import available verified the package, retained the
task ID and context references, inspected A, and wrote only B in a copied
workspace. A's bytes and modification time survived; operator review remained
unresolved. The archive continued to record the original B/review obligations.
The destination's explicit fixture command authorized its B write; exported
arguments alone could not authorize it.

**Validation:** the final full Python 3.13 suite passed **1,811 tests, seven
skipped**, with six existing MCP deprecation warnings (413.54 s). Python 3.12
affected suites passed **127 tests** (41.15 s); after the final Markdown quoting
correction, the export, terminal and blob suites passed **31 tests** (4.88 s).
The regression checks actual Markdown parsing, not just indentation strings.
The superseded full run was interrupted to validate the final correction; it
is not counted as a completed run. Ruff, whitespace, offline build and clean
wheel smoke passed. All 77 core files match the final wheel, and all 80 recorded
core/test/consumer hashes remained unchanged during final validation.

**Remaining:** this is a bounded format/continuation test, not live model
judgment or installed normal-memory interoperability. Child sessions remain
references; opaque external effects need inspection. System/provider/plugin
state, drafts, queues and mutable workspace files are outside the package.
M4 held-out semantic quality, M5 live mixed-runtime supervision and plugin
reconciliation, isolated source improvement/rollback, and M6 daily use remain
open. Memory and agent-swarm remain plugins; export is core.

**PR40 CI correction:** lint passed on both Python versions. The initial Python
3.13 job failed an existing permission-expiry test that assumed the whole turn
would settle after a fixed 400 ms pause. The test now observes dialog mounting
and subsequent inference, verifies the expired dialog is gone while a controlled
model response keeps the turn active, then awaits the actual worker with a bound.
The source deadline remains 300 ms, and the denied memory tool must never run.
All 12 resident-status tests passed on Python 3.13 (6.53 s) and 3.12 (6.75 s);
Ruff and whitespace checks passed. The correction changes tests and this record
only; core export behavior and the format remain unchanged.


## September 9 — Typed delegation and inspectable coordination outcomes

PR40 merged at `27b37cb`; Python 3.12/3.13 CI and automated review passed. This
increment addresses an M5 integrity prerequisite: mixture coordination previously
interpreted an `[subagent error]` prefix as execution status, and an interrupted
fan-out could leave siblings running after the coordinator returned.

**Core behavior:** `SubagentRunner.run_result()` returns a typed execution outcome
with child session/run, original output reference, reason and truncation. Existing
string APIs remain compatible. Coordination filters by execution status and
truncation; literal error-looking text remains ordinary answer data. It preserves
successful sibling work alongside failures, blocked children, disagreement and
review vetoes. Cancellation and unexpected runner failures settle siblings before
the aggregate terminal event. Budget/deadline outcomes retain their recorded
incomplete status; unreadable child terminal logs leave an explicit diagnostic
and parent failure rather than hiding the failure.

A versioned, validated report records each participant and the aggregate answer
in the calling session. `/coordination` and `harness coordination SESSION_ID`
inspect these facts without loading providers/plugins or changing the log. The
activity panel preserves a configured coordination agent's incomplete status
across a successful tool transport. Portable export copies reports associated
with the task's tool calls and their aggregate outputs. Participant references
remain references to child stores; recursive artifact transfer is not implemented.
The [contract](../../coordination-outcomes.md) documents status, provenance,
limits and advisory selection policies.

**Evidence:** regressions exercise real child sessions, error-prefix answer data,
partial/truncated results, failed run IDs, blocked fan-out, shared model-budget
exhaustion, cancellation cleanup ordering, bounded output, CLI/TUI inspection and
portable task evidence. A controlled catalog journey mixes inference with the
real external Codex binding using scripted transports. This qualifies contract
behavior, not live local/remote agents or installed plugins. The tracked task's
operator review remains unresolved after coordination and export.

**Remaining:** exact `APPROVE`/`PASS` first-line parsing only implements the
existing advisory review policy. It does not verify acceptance. Evidence-based
escalation, separate coordinator admission, shared token/cost accounting, edit
ownership and live heterogeneous/plugin reconciliation remain M5 work. Source
patch improvement, evaluation and rollback also remain; no prompt/model was
adopted. M4 held-out semantic quality and M6 daily-use qualification are unchanged.

**Validation:** the full Python 3.13 suite passed **1,835 tests, seven skipped**,
with six existing MCP deprecation warnings (425.66 s). Affected Python 3.12
runtime/delegation/task/UI/export suites passed **216 tests** (41.04 s).
Final review then distinguished missing critic output from actual disagreement
and retained a negative verifier's disagreement after premium recovery. The
final coordination suites, including those two added regressions, passed
**55 tests on each Python version** (1.44 s / 1.53 s); the full run predates this
small reporting correction. Ruff, whitespace, offline build, CLI help and clean
wheel smoke passed. All 79 core modules match the final wheel and all 81 final
core/test hashes remained unchanged through publication preparation.

**PR41 review:** direct `ensemble`, `consult_panel` and `escalate` workflows now
show their recorded aggregate outcome as a separate result row in the Workflows
group. Completed children remain completed when their aggregate is incomplete;
blocked workflows without children and concurrent cancelled/blocked results stay
visible and correctly attributed. Three real terminal-compositor regressions
reproduced the missing status before the fix and now pass. The activity-panel,
coordination and mixture suites passed **74 tests on each Python version**
(3.13: 3.16 s; 3.12: 3.41 s). Ruff and whitespace checks passed. Both hosted
Python CI versions had passed before this scoped projection correction; fresh
CI subsequently passed on both Python versions at `0c1b0d3`, and automated review
passed before PR41 merged. Execution and acceptance policies are unchanged.


## September 9 — Separate coordinator admission and bounded coordination

PR41 merged at `2c44465` after Python 3.12/3.13 CI and automated review passed.
This increment advances M5: a configured pure coordinator previously occupied a
worker slot while waiting for experts, and direct strategies had no aggregate
admission or deadline of their own.

**Core behavior:** direct strategies and configured coordination agents now reserve
separate active coordinator capacity (default 16), leaving the worker capacity
(default 16) available for actual agent execution. Both consume one cumulative
descendant and one level of nesting, sharing the existing root limits. Exhausted
capacity rejects admission without creating children; it does not queue work.
Ordinary executing agents still occupy worker slots while awaiting tools.

A default 600-second deadline covers the entire coordination, including later
judge/review/refinement/fallback stages. Expiry records an incomplete result and
awaits child cleanup before returning. Completed participant artifacts remain
inspectable. Cancellation remains cooperative, so cleanup can extend beyond the
deadline; earlier ancestor deadlines still apply. All exits release active
coordinator capacity, including persistence failures. Active coordinators block
idle-only handoff and improvement controls, and handoffs retain stricter source
limits while holding unreconciled descendant activity.

A persisted start makes interrupted coordination inspectable even when no
aggregate terminal could be written. CLI inspection and task export preserve
that outcome as unconfirmed. The activity panel keeps coordinators running while
nested coordination remains active, and shows unconfirmed when the enclosing
tool settles or is recovered without the aggregate terminal. Historical reports
remain readable without inventing admission or deadline metadata.

**Evidence:** controlled real-child tests exercise full worker utilization for
both entry paths, concurrent admission refusal, cumulative/depth limits, inherited
tool restrictions and actual parent lineage, cancellation/deadline cleanup,
preserved completed artifacts, publication failures, handoff accounting,
improvement idle guards, old reports, activity projection and portable export.
These qualify core contracts, not live mixed-provider or installed-plugin behavior.

**Remaining:** M5 still needs shared token/cost budgets, evidence-based escalation,
edit ownership, live heterogeneous/plugin reconciliation and isolated source
improvement experiments/adoption/rollback. M4 held-out semantic quality and M6
daily use remain open. The explicit user-correction-to-repair loop discussed with
the user is also still missing: current automatic prompt proposals react to
repeated invalid semantic output, not ordinary corrections or user frustration.
No mood model, prompt/model adoption or private replacement memory store is added.
Memory and agent-swarm remain plugins; supervision and improvement controls are core.

**Validation:** the full Python 3.13 suite passed **1,869 tests, seven skipped**,
with six existing MCP deprecation warnings (420.24 s). Affected Python 3.12
coordination, delegation, recovery, improvement, activity and export suites passed
**180 tests** (9.13 s). The skips remain missing Anthropic/Ollama conformance
fixtures and the opt-in live Antigravity probe. An earlier focused run found a
missing required reason in a new recovery-test fixture; the corrected fixture
and final nested-activity/legacy-report regressions are included in both passing
runs. Ruff, whitespace checks, offline sdist/wheel build, CLI help and a fresh
offline wheel installation/import smoke passed. All 79 core files match the
wheel, and all 210 source/test hashes remained unchanged through validation.
Hosted Python 3.12/3.13 CI passed at `1e86f5a` before the PR42 review correction
below.

**PR42 review:** compatible handoff scopes missing coordinator metadata now use
defaults for only the added fields: zero active coordinators, capacity 16 and a
600-second deadline. Explicit recorded values and stricter current limits remain
effective; earlier call reservations are restored after restart. Normalization
uses local copies and leaves authenticated checkpoint bytes unchanged. The
existing core-policy fingerprint check still holds snapshots from a different
implementation; this is not an authority migration across application versions.

Eight restart regressions reproduced the missing count/capacity/deadline keys
individually and together, with both stricter and looser destination limits.
Three further cases keep incompatible source versions and recorded worker or
coordinator activity held before inference. All eleven pass after the correction.
The affected coordination, handoff, destination/memory recovery, terminal,
portable-export and prompt-improvement suites passed **150 tests on each Python
version** (3.13: 54.13 s; 3.12: 56.89 s). Ruff and whitespace checks passed.
The earlier full-suite/build evidence predates this scoped compatibility fix.
Hosted Python 3.12/3.13 CI and automated review subsequently passed at `03a8466`
before PR42 merged.


## September 9 — Retain native file ownership through cancellation

PR42 merged at `f76a24c`. This increment addresses M5's concurrent-editing
boundary: cancelling a native write/edit previously released its canonical-path
lock while the mutation thread could still be running. Another agent could then
enter the same file's mutation, and task/coordination terminal facts could precede
completion of the actual file effect.

**Core behavior:** native writes and edits now retain their file lock until the
executor future settles. Waiting for a lock remains cancellable without starting
a file mutation; different paths can still run independently. Repeated
cancellation, deadline expiry, late thread failure and event-loop task shutdown
cannot release ownership before the thread settles. Worker context is preserved.
The enclosing tool/run still reports interruption rather than converting a
completed file effect into task success. This is cooperative cleanup and may
extend beyond a deadline when a file operation is slow.

The core records a cleanup notice in the actual calling session, and the terminal
explains the wait while preserving the unsent draft. Failure to save that notice
does not shorten cleanup or permit later model calls. Handoffs use the same native
mutation lifetime, allowing an interrupted lock waiter to stop before its worker
starts; other handoff file operations retain their existing cleanup contract.

**Evidence:** six initial regressions reproduced early ownership release for
writes/edits and late failures. Controlled worker barriers exercise competing
tool instances through canonical-path aliases, independent paths, repeated
cancellation, waiting cancellation and recovery after failure. Real task and
delegation journeys check terminal ordering, descendant capacity, retained file
effects and stopped inference under cancellation/deadlines and failed notices.
Further tests exercise event-loop shutdown, a handoff waiting on a file lock, and
Escape/draft preservation against the final terminal compositor.

**Remaining:** per-call serialization is not stale-read conflict detection or
whole-workflow ownership. Provider-native tools, shell commands and other
processes do not participate; cross-process ownership/isolated worktrees and live
mixed-agent qualification remain M5 work. Shared token/cost budgets,
evidence-based escalation, plugin reconciliation, and isolated source improvement
experiments/adoption/rollback also remain. The M4 held-out quality,
user-correction-to-repair and M6 daily-use gaps remain open. Memory and agent-swarm
remain plugins; this mutation lifetime is core.

**Validation:** the full Python 3.13 suite passed **1,899 tests, seven skipped**,
with six existing MCP deprecation warnings (438.58 s). Affected native-tool,
delegation, coordination, handoff/recovery and terminal suites passed **225 tests
on Python 3.12** (60.37 s). All 19 new regressions are included in both runs. The
skips remain missing Anthropic/Ollama conformance fixtures and the opt-in live
Antigravity probe. Ruff, whitespace checks, offline sdist/wheel build, CLI help
and clean offline wheel import smoke passed. All 79 core files match the wheel;
all 211 core/test hashes remained unchanged through validation. These are
controlled core-contract and terminal checks, not live provider/plugin or
local-model qualification. Hosted Python 3.12/3.13 CI and automated review
subsequently passed at `6d20c37` before PR43 merged.


## September 9 — Reject native file changes based on stale observations

PR43 merged at `673924b`. This increment continues M5 edit ownership: locking
individual mutations prevented overlap, but the shared path-only read state let
one agent overwrite another agent's work using old context or another session's
read. Serialized mutations alone did not prevent lost updates.

**Core behavior:** native file tools now retain the SHA-256 of the bytes used
for each successful read, scoped to the actual calling session. Each mutation
captures that caller's version before waiting for its file lock, checks current
bytes under the lock, and refuses stale content without changing the target.
Windowed reads still fingerprint the whole source file; matching file size,
timestamps or decoded text cannot mask changed raw bytes. Parent and sibling
reads do not satisfy another session's gate. Sequential successful writes/edits
supply their caller's next observation, while concurrent calls retain the
version they started with. Failed reads and cancelled reads/writes cannot
refresh a stale observation. A delivered missing-file read invalidates its old
version, permitting deliberate recreation after deletion.

Resume retains historical read paths for routing, but requires a fresh read
before overwriting existing files. Historical numbered/windowed/truncated tool
output cannot reconstruct a reliable original version. No event schema or
plugin API is added. The ordinary tool error explains rereading before retrying;
its guidance fits the terminal's result preview before the potentially long path.

**Evidence:** eleven initial regressions reproduced stale overwrites and
cross-session/legacy path reuse. Controlled tests exercise exact raw bytes,
changes outside a displayed window, deletion/recreation, queued mutations,
competing creates, canonical aliases, cancellation without observation refresh,
unchanged empty files and sequential edits. A real core ensemble runs two child
sessions against a shared file: the second gets a durable tool error, rereads,
and combines its change with the first. Scripted transport assertions verify
the error actually reaches the agent. A final terminal compositor journey checks
visible conflict/retry guidance, preserved work and an unsent draft through
recovery. The resume journey now verifies rejection followed by fresh-read
recovery through the ordinary dispatcher.

**Boundaries:** this is content conflict detection among cooperating native
tools on the owning event loop, not whole-workflow or cross-process isolation.
An earlier shell/provider-native/other-process change may be detected, but those
writers do not hold the lock; changes after the check remain outside the guarantee.
Identical replacement bytes do not conflict, and no semantic merge is attempted.
Execution completion still does not establish task acceptance. M5 retains shared
token/cost budgets, evidence-based escalation, broader edit ownership/isolated
worktrees, live mixed-agent/plugin reconciliation and isolated source improvement
experiments/adoption/rollback. M4 held-out quality and user-correction-to-repair,
and M6 daily-use qualification remain open. Memory and agent-swarm remain plugins.

**Validation:** all 22 new regressions passed (1.27 s). The full Python 3.13
suite passed **1,921 tests, seven skipped**, with six existing MCP deprecation
warnings (435.02 s). The affected native-tool, delegation, coordination,
handoff/recovery and terminal suites passed **247 tests on Python 3.12**
(56.39 s). Skips remain missing Anthropic/Ollama fixtures and the opt-in live
Antigravity probe. Ruff, whitespace checks, offline sdist/wheel build, isolated
wheel CLI help and all 78 module import checks passed. All 79 packaged core
files match source, and all 218 core/test hashes stayed unchanged through
validation. These are controlled contract and terminal checks, not live model
or installed-plugin qualification. Hosted Python 3.12/3.13 CI and automated
review subsequently passed at `f1651b2` before PR44 merged.


## September 9 — Gate escalation on recorded task evidence

PR44 merged at `f0554c3`. This M5 increment replaces execution/advisory-only
selection when the calling run belongs to a tracked task with declared
requirements. Previously, a cheap answer could stop escalation merely by
completing, and premium execution did not undergo the same verification gate.

**Core behavior:** escalation freezes the owning task's original objective,
requirements and source session/task/run/basis before starting either candidate.
Both candidates record the objective and requirements before execution, receive
them in context, and undergo the same existing exact-output/tool-result checks.
Core rechecks child lineage, delivered run/output, requirement identity, execution
boundaries and artifact integrity rather than trusting a grade supplied by a
participant. A cheap pass avoids premium; a failed or unverified cheap result
escalates. A premium result that does not pass remains incomplete with its output
and evidence retained. An optional advisory verifier can request premium after
passing cheap evidence, but its PASS cannot override a failed check.

Existing active-task requirements cannot be removed by model-supplied arguments.
The optional strict `require_checks` boolean additionally blocks an escalation
without active declared requirements before spawning children. Native tools and
configured escalation agents share the core path. Memory and agent-swarm remain
plugins; neither is required to use this feature. The task owning the actual run
supplies requirements, even if a different task is selected. Ordinary delegated
subtasks do not implicitly inherit all parent criteria.

Reports retain the frozen criteria/source and each participant's grades with
original child event, call and artifact references. `/coordination` and headless
inspection display them, and portable exports retain them. No event type or
execution-resume format is added. Older advisory reports remain readable without
invented checks. Coordinator verification runs off the event loop; interruption
leaves unfinished checks unconfirmed and a late check cannot revive or promote
an interrupted coordinator. Existing admission, deadline, permissions and child
cleanup still apply.

**Evidence:** five initial cases reproduced unchecked cheap/premium selection
and missing-criteria admission. Tests cover exact output and matching tool calls,
wrong arguments, denied/error/later-failed calls, absent evidence, mixed review
and machine checks, partial results, model attempts to weaken requirements,
missing/corrupt artifacts, mismatched output/run provenance and duplicate
terminal facts. Further journeys exercise configured agents, actual task ownership,
cancellation/deadlines during premium execution and verification, legacy reports,
read-only inspection/export/replay, and the final terminal compositor. A controlled
catalog journey combines native inference and the real Codex runtime binding
using scripted transports; this is contract evidence, not live provider evidence.

**Limits:** these are checks of recorded bytes. They do not establish current
workspace correctness, arbitrary answer quality or user approval. Review
requirements remain unverified and cannot make a gate pass. Selection does not
automatically import child evidence into the parent task's checks or accept it;
parent task acceptance retains its existing evidence and operator controls.
Without active declared requirements, legacy execution/advisory selection remains
available unless `require_checks` is enabled. M5 still needs shared token/cost
budgets, broader edit ownership/isolated worktrees, live mixed-agent and installed
plugin reconciliation, and isolated source improvement experiments/adoption/rollback.
M4 held-out quality and correction-to-repair, and M6 daily-use qualification remain
open. No improved prompt/model or source patch is adopted by this increment.

**Validation:** all 33 new regressions passed (3.38 s). The full Python 3.13
suite passed **1,954 tests, seven skipped**, with six existing MCP deprecation
warnings (434.23 s). Affected coordination, task evidence, runtime, handoff and
terminal suites passed **306 tests on Python 3.12** (61.57 s). Skips remain missing
Anthropic/Ollama fixtures and the opt-in live Antigravity probe. Ruff, whitespace
checks, offline sdist/wheel build, isolated wheel CLI help and all 79 module
imports passed. All 80 packaged core files match source; all 220 core/test hashes
stayed unchanged through validation. This qualifies the controlled core-contract
and terminal cases, not live model/provider/plugin operation or general grading
quality. Hosted CI and review are pending publication.
