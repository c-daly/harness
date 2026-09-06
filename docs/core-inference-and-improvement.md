# Core inference and improvement contracts

This is the implemented M2 foundation on `feat/core-agency`. The complete
[core agency roadmap](superpowers/plans/2026-09-06-core-agency-roadmap.md) remains
active. Native agent tasks now have distinct results; external runtime migration,
local readiness, experiment runners, activation, and rollback remain subsequent work.

## Bounded inference

Internal work uses `Dispatcher.dispatch_inference(provider=..., request=...)`.
`InferenceRequest` supplies a purpose, messages, optional tool schemas, explicit
tool-proposal policy, input/output byte limits, output token limit, frame limit,
timeout, optional temperature, and optional JSON response schema. Defaults are
4 MiB input, 1 MiB output, 4,096 output tokens, 65,536 frames, and 120 seconds;
short semantic functions should choose substantially smaller limits.

```python
from harness.inference import InferenceRequest
from harness.messages import Message
from harness.types import ModelId

result = await kernel.loop.dispatcher.dispatch_inference(
    provider=kernel.provider,
    request=InferenceRequest(
        model=ModelId("local-instruct"),
        purpose="intent-shadow",
        messages=(Message.user_text("Classify this supplied message: thanks"),),
        max_input_bytes=8192,
        max_output_bytes=2048,
        max_output_tokens=128,
        timeout_seconds=5,
        temperature=0,
        response_schema={
            "type": "object",
            "properties": {"kind": {"enum": ["acknowledgement", "uncertain"]}},
            "required": ["kind"],
            "additionalProperties": False,
        },
    ),
)
```

This example demonstrates the API, not a qualified intent classifier or local
profile. No semantic control decision follows automatically from its output.

The dispatcher retains normal hooks, permissions, accounting, and shared call
budgets. It checks the **effective** route after hooks: an inference operation
cannot become a Codex, Claude Code, or Antigravity agent run. Internal results
are recorded under their purpose and stay out of the conversation fold.
Compaction uses this path and retains history when a summary is incomplete.
For a session using an external agent alias, select an inference alias with
`/model` before `/compact`; automatic internal-model selection is not implemented.

Byte bounds include tool schemas, reasoning, signatures, and tool argument
fragments. Blob sizes are checked before reading large tool-result sidecars.
Over-limit inputs fail explicitly; this API does not silently truncate context.
Byte limits are not token estimates. LiteLLM receives the token cap and timeout;
the dispatcher owns retry counts and the overall inference retry deadline.
Missing terminal markers fail, and cancellation closes the source stream.

`tool_choice="none"` is the default. Native conversation dispatch uses `auto`:
inference proposes calls, and ordinary tool dispatch checks the final name and
arguments through hooks and permissions. Invalid proposals produce normal tool
error feedback so the agent can correct them. Inference itself executes no tools.

The LiteLLM and catalog adapters expose `infer(request)`. `complete(...)` remains
a migration API. Third-party providers exposing only `complete` receive locally
enforced byte/frame/time bounds through `LegacyCompletionAdapter`; remote token
limits, sampling, and structured output require an actual inference adapter.
Legacy external-agent completion still has its existing lifecycle and containment
limitations. It is classified as `agent` and is never retried automatically.
External runtimes still need migration to the task contract below.

Catalog entries infer `execution_kind = "agent"` from an external CLI backend,
otherwise `inference`. An explicit value must agree with the backend. Old aliases
remain valid. Events and telemetry record the execution kind; older entries
without the field retain `legacy` rather than guessing their historical runtime.

Token measurements are nullable. Missing provider usage is unknown; it does not
become zero. Totals containing unknown measurements remain unknown, including
cost. Telemetry schema 2 requires rebuilding an older derived database through
`harness stats --base-dir ...`; original session logs remain readable.

## Native agent tasks

`AgentLoop` implements `AgentRuntime.run_task(AgentTask, on_progress=...)`.
An `AgentTask` carries its own identity, prompt, supplied context, acceptance
criteria, optional agent label, and `TaskLimits`. Its dispatcher binding retains
the existing tool scope, permissions, and shared execution budget. Task input
cannot grant capabilities. `run_turn(text)` remains a text-returning compatibility
method; the CLI, TUI, and native subagent runner now consume task results.

```python
from harness.agent import AgentTask, TaskLimits

result = await kernel.loop.run_task(AgentTask(
    prompt="Check the supplied implementation",
    acceptance_criteria=("the fixed regression suite passes",),
    limits=TaskLimits(max_iterations=8, timeout_seconds=60),
))
answer = result.read_text(kernel.session.blobs)
```

Defaults are 20 iterations and a 600-second task deadline, with 4 MiB input,
1 MiB response, and 4,096 output tokens per inference step. The final output is
also bounded to 1 MiB by default and stored as a verified blob. These per-run
and per-step limits supplement existing shared call/child budgets; they are not
a durable, cumulative token or cost budget. The loop's own iteration limit can
further reduce the task limit. A second simultaneous task on one loop is refused
before it writes input or starts inference.

Each run records `AgentRunStarted` before work and `AgentRunFinished` after
cleanup. Starts link nested runs to their parent run; model call proposals and
completions carry task/run identities. Progress callbacks expose inference,
streaming, and tool phases. Observer failures do not control execution. The
conversation fold ignores these lifecycle facts while retaining results and
open runs in separate projections. Resume marks unfinished runs `aborted`,
after repairing model/tool intents, without replaying uncertain side effects.

`AgentResult.status` distinguishes `completed`, `incomplete`, `failed`,
`cancelled`, and `aborted`. Token cutoffs and iteration exhaustion return
`incomplete`. Exceptions still propagate after recording their terminal fact:
deadlines and exhausted budgets record `incomplete`, cancellation records
`cancelled`, and other errors record `failed`. Completed results include summed
reported usage; unknown fields remain unknown. Interrupted runs currently
report unknown aggregate usage; individual model facts retain known measurements.

**Execution completion is not acceptance.** Criteria reach the inference context,
but `acceptance` remains `unverified` and `remaining_criteria` preserves every
criterion regardless of model prose. The evaluation/adoption services must supply
separate evidence before any stronger claim can be made.

The shared controller pauses follow-ups on an incomplete result. The TUI shows
the reason and preserves queued input and the unsent draft; headless CLI prints
partial output and exits nonzero. Native children record `incomplete` rather
than `ok`, and the activity panel retains that status even when the enclosing
tool returns text. Legacy coordination still uses a string bridge with a
preserved error prefix; it is not yet a fully typed heterogeneous result protocol.

This is the native part of M2. External CLI execution still passes through the
legacy conversation adapter inside the native loop, with existing containment
limits. The outer native task deadline does not certify external process cleanup
or provider-native tool authority. Runtime capability qualification, explicit
resume support, durable task continuation, and external artifact contracts remain
required before advertising the full M2 gate.

## Improvement records and fixed evaluation gates

`kernel.improvements` exposes `ImprovementJournal`. It stores typed
`ImprovementRecorded` events and verified blob artifacts in the normal core
session record. It does not require any plugin or create another memory system.

- `Evidence` points to an existing session event and describes an observation.
- `Candidate` binds that evidence, a hypothesis, expected benefit, the incumbent
  digest, and a verified candidate artifact. Targets are prompts, routing,
  context policy, or code.
- `EvaluationPlan` fixes the candidate/incumbent/evaluator versions, suite
  artifact, regression and held-out cases, and quality/latency thresholds.
- `ExperimentResult` preserves evaluator output and paired measurements for
  every planned case. Missing measurements remain unknown.

IDs are immutable. A result must match its pre-existing plan and report each
planned case exactly once. Known critical failures or regressions fail the gate;
incomplete measurements cannot qualify; measured benefit and declared latency
limits must also pass. Plan records are synchronized before a caller can start
an experiment. Replay reconstructs facts and never runs inference or experiments.

`adoption_decision` reports `refused`, `review_required`, or `eligible` against
an explicit versioned `AdoptionPolicy`. The default has no automatic targets.
Eligibility **does not activate changes**. These contracts do not yet launch
evaluators, verify an evaluator process's authority, edit a running installation,
or implement safe activation and rollback. Those remain required M4/M5 work;
an eventual evaluator/activation service must prevent candidates from choosing
their own grader or authorizing their own adoption.

Inspect the same records in either interface:

```text
/improvements
harness improvements SESSION_ID --base-dir /path/to/harness-data
```

The memory plugin can later retain accepted lessons through its existing write
contract. Agent-swarm and the proposed experiment plugin may supply workflow or
experiment strategies; core owns these records and their enforcement boundary.
