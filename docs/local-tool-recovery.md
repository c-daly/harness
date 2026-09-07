# Bounded local tool correction

The first single-tool profile stopped whenever the local model proposed several
tools together. The new opt-in policy gives the native agent a bounded chance to
correct that response:

```toml
parallel_tool_calls = false
tool_recovery_attempts = 2
```

The default is zero corrections. Values must be integers from zero through two;
positive values require the single-tool bound. `/context` displays the setting,
and normal session resume retains it.

Only `ToolCallLimitExceeded` from a recorded model failure is eligible. The whole
rejected response stays outside conversation history, and none of its proposals
execute. Core records `ModelCorrectionRequested` with the failed call, task/run,
attempt number, and fixed instructions to propose one next call and observe source
results before writing. The next iteration uses that feedback through ordinary
context preparation, routing, permissions and dispatch. It consumes the original
task's iteration, deadline and shared model-call budgets. Exhaustion still fails
explicitly; malformed arguments, policy failures, cancellation, and external agent
execution are not corrected by this mechanism. Earlier completed tools remain
completed and are not replayed.

Correction is a new inference request, not the dispatcher's unchanged-request
transport retry. Both the failed call and correction remain in the durable log.
Folding recreates the feedback exactly; resuming a crashed task records an aborted
run without automatically repeating work. Feedback contains no rejected arguments
or memory-derived exception text. Usage for a recovered task is unknown when the
rejected inference has no accepted usage result; its call reservation is retained.
The task's acceptance remains `unverified` until an independent evaluator checks it.

The TUI announces correction. A synchronous progress callback clears rejected
streamed text before the replacement response starts; the asynchronous event
notice cannot clear new response text that has already arrived.

## Evaluation and offline journey

**Measured candidate outcome: refused.** The
[final paired report](handoffs/2026-09-06-core-agency/local-recovery-evaluation.json)
records **6/8** candidate successes versus **4/8** for rejection alone. All four
candidate memory cases attempted one correction; three wrote correct artifacts
and one omitted the output file. One no-plugin candidate wrote malformed JSON.
The core verdict is `failed`, and adoption is `refused`.

An earlier paired run passed 8/8 candidate cases versus 4/8; its gate returned
`review_required`. Repetition and the broader journey exposed remaining quality
failures. That initial pass is not qualification, and neither run activated
anything. The initial report remains at `.local-runtime/reports/recovery-evaluation.json`
with audit session `9cae3523438f4594b7ee77000b684ca7`; the final report's audit
session is `6707e3f214804549a2afa6e1e00fd4cf`, both under
`.local-runtime/reports/recovery-audit` on the measured machine.

The [final three-repeat journey report](handoffs/2026-09-06-core-agency/local-recovery-journey.json)
passed **9/12 journeys**: 3/3 no-plugin file tasks, 3/3 memory-assisted file tasks,
and 3/3 no-plugin TUI journeys. All three memory-enabled TUI journeys created the
exact artifact, cancelled a real stream, preserved drafts, resumed their session,
retrieved normal memory again, and cleaned up. All three failed the resumed-answer
check, so the complete offline gate remains failed. No model output was accepted
as proof of an artifact.

A separate [metadata-only diagnostic](handoffs/2026-09-06-core-agency/local-recovery-resume-diagnostic.json)
reproduced that failure and distinguished content from visibility: the completed
resumed answer contained both source facts but was **14,036 bytes**, and the
project name was outside the final visible viewport. This points to response
verbosity/presentation for that failure, not proof that the facts were absent.
The diagnostic leaves model requests and returned answers unchanged and records
only status, lengths, fact-presence flags and hashes. It overlapped integration
testing, so its timings add no latency qualification.

On this provisioned CUDA machine, headless file tasks took **3.14–5.79 seconds**,
including stopped-runtime startup. Ready observations arrived in **2.07–2.52 seconds**.
The six TUI cancellations settled in **83–306 ms**; mount checks in an already
running Python process took **144–542 ms**, including memory selection/startup where
enabled. All 54 repeated public semantic samples passed in **82–139 ms**; those
three examples do not qualify semantic control. CPU/RAM/network isolation and
the GPU/model/runtime/dependency identities remain explicit in the report.

An earlier expanded journey exposed real artifact and resume failures as well as
a driver bug: it called `loop.end()` after TUI unmount had already ended the loop.
The final driver mirrors TUI teardown, preserves the same checks, and records
successful cleanup in all 12 cases. The earlier report remains at
`.local-runtime/reports/recovery-journey.json`; its cleanup failures are not counted
as model failures. Its other failed checks were not discarded.

The existing core improvement evaluator accepts `--recovery` to compare the
single-tool policy alone with the same policy plus two correction attempts.
It imports the failed planning report as evidence and records a distinct context
candidate, fixed suite, plan, paired measurements, and result. The eight paired
cases retain the original harbor/3 regression and reserve cobalt-μ/29 source facts,
each with/without normal memory, twice each. The artifact oracle and existing
latency/critical-case gates remain unchanged. No source facts enter answer hints.
The outcome uses the normal core adoption policy; even a passed candidate requires
review. No runtime configuration, model alias, or fallback is activated.

Use the isolated Docker recipe from [local qualification](local-model-qualification.md)
with these final Python arguments:

```sh
-B -m scripts.evaluate_local_planning --recovery \
  --memory-root "$qualification_memory_root" \
  --audit-dir /reports/recovery-audit --output /reports/recovery-evaluation.json
```

The complete journey driver now also exercises the TUI with the actual normal
memory plugin, including an exact file artifact, streamed cancellation, draft
retention, session-picker resume, a fresh memory read after resume, and usable
failure controls when a local asset disappears. It accepts the explicit profile:

```sh
-B scripts/qualify_local.py --single-tool --tool-recovery-attempts 2 --runs 3 \
  --memory-root "$qualification_memory_root" --output /reports/recovery-journey.json
```

Reports retain metadata, exact source hashes and independent check results. They
exclude private memory text and model answers. Trial sessions are temporary;
the separate improvement audit persists metadata and report blobs. Memory and
agent-swarm remain plugins.

This is a narrow fixed-fixture capability experiment. Broader task/model quality,
CPU-only performance, cold filesystem-cache startup, human dogfooding, scheduling,
automatic fallback, activation and rollback remain separate gates.
