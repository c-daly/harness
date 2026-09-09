# Core inference and improvement contracts

This describes the implemented M2 foundation and subsequent core additions. The complete
[core agency roadmap](superpowers/plans/2026-09-06-core-agency-roadmap.md) remains
active. Native agent tasks now have distinct results; external runtime migration,
broader experiment runners and source-change activation remain subsequent work.
The measured local profile and a supervised shadow-prompt loop are now implemented.

[Semantic evaluation](semantic-evaluation.md) adds explicit shadow message
interpretation and a bounded paired prompt evaluator with fixed grades and
recoverable run records.

[Task evidence](task-evidence.md) adds persistent user requirements, exact checks
of recorded results and artifacts, and explicit review/acceptance. These facts
can supply outcome evidence to the improvement journal; an agent's own completion
or self-assessment cannot accept the task or activate an improvement.

[Context profiles](context-profiles.md) add persisted request and tool restrictions.
Their preparation events provide input-size and omission evidence for later
evaluation; no candidate adoption or activation follows automatically.

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
Its response passes through the same bounded stream collector as inference:
bytes, frames, reported output tokens, terminal markers, and the request deadline
are checked before completion is persisted. Excess chunks never reach observers.
Catalog forwarding closes the underlying CLI stream before a terminal failure
is recorded. Sharing response collection does not make an external agent an
inference provider or allow it to serve internal inference requests.

For legacy CLI agents, token limits reject reported overruns as soon as usage
arrives; they cannot constrain generation before a runtime reports usage. Unknown
usage remains unknown. Response bounds cover owned chunks, not raw subprocess
stdout/stderr, native tool side effects, or arbitrary scratch-file growth; those
still require the planned external-runtime containment and capability gates.
Codex now has the task binding described below; Claude Code and Antigravity
remain on this legacy path.

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
1 MiB response, 4,096 output tokens, and 65,536 stream chunks per step. The final output is
also bounded to 1 MiB by default and stored as a verified blob. These per-run
and per-step limits supplement existing shared call/child budgets; they are not
a durable, cumulative token or cost budget. The loop's own iteration limit can
further reduce the task limit. A second simultaneous task on one loop is refused
before it writes input or starts inference.

Each run records `AgentRunStarted` before work and `AgentRunFinished` after
cleanup. Starts link nested runs to their parent run; model call proposals and
completions carry task/run identities. Progress callbacks expose inference,
streaming, and tool phases. Observer failures do not control execution. The
conversation fold ignores native lifecycle facts while retaining results and
open runs in separate projections. External conversation results are folded once,
as described below. Resume marks unfinished runs `aborted`,
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
tool returns text. [Coordination](coordination-outcomes.md) now consumes typed
delegation outcomes, preserves partial results and provenance, and settles
siblings before recording its terminal report. String tool APIs remain compatible;
their prose does not determine child execution status. Pure coordinators have
separate active capacity and an overall deadline while sharing descendant,
depth and call limits with their workers. Missing aggregate terminals remain
unconfirmed in inspection and export. Live heterogeneous
qualification and evidence-based escalation remain open.

## External agent tasks: Codex migration

`bind_agent_runtime(provider, model, dispatcher)` returns an `ExternalAgentRuntime`
for a Codex adapter or catalog alias. Unmigrated adapters return `None`. The bound
runtime implements the same `run_task(AgentTask, on_progress=...)` contract as
the native loop. It retains the supplied dispatcher's tools, hooks, permissions,
and shared budgets. Runtime selection is explicit: routing hooks may select
another Codex model, but cannot change an already bound task into inference or
another agent runtime. Internal inference still rejects agent aliases.

`AgentLoop` selects this binding automatically for Codex, so CLI, TUI, and child
sessions use it through their existing entry points. The continuing Harness task
owns a nested Codex run with distinct task/run IDs, a capability snapshot, an
`execution` progress phase, and its own result. Supplied context and acceptance
criteria reach the child without duplicating the user's prompt or persisting
ephemeral input. The TUI reports `agent running` and retains normal queue,
draft, and interruption behavior. Standalone runtime callers pass context through
`AgentTask`; the loop's internal `prepared_messages` bridge accepts context that
it has already assembled and recorded.

The transport still uses the adapter's `complete` stream and one compatibility
`ModelCallCompleted` for usage/pricing, with `execution_kind="agent"` and
`purpose="agent-task"`. It does not become a second conversation message or a
second accounting charge. `AgentResult.response` preserves the bounded assistant
response, including reasoning/signatures; its verified output blob holds the
plain answer. `AgentRunFinished(purpose="conversation")` owns transcript replay.
MCP tool proposals carry task/run lineage and `purpose="agent-task"`; their
results remain audited, including on recovery, without inserting orphan tool
replies into conversation history. Old events retain their existing defaults.

Codex executes its own MCP tools. Returned, unexecuted tool proposals are a
protocol failure, never instructions to start a second native tool loop. A
non-`end_turn` stop returns `incomplete`; acceptance always stays `unverified`.
Failures are never retried automatically. Task cancellation closes the transport,
reaps its child process, and tears down its scratch resources before terminal
publication. Resume aborts interrupted runs without relaunching the CLI.

The capability snapshot is an **adapter declaration, not live qualification**.
It explicitly reports unsupported native resume and internal iteration caps,
provider-controlled native tools, response bounds over adapter chunks, and token
overrun checks based on reported usage. Zero iterations prevents launch; a
positive iteration limit does not cap the CLI's internal steps. The task deadline
and Harness-dispatched call budgets still apply. Raw stdout/stderr bounds,
scratch-file containment, CLI-version probes, portable continuation, and the
remaining external adapters are outstanding. These changes advance M2 without
claiming its complete capability or live-provider gate.

## Improvement records and fixed evaluation gates

The [local readiness service](local-runtime-readiness.md) now contributes core
availability and owned-process evidence. These records can inform experiments;
inventory readiness does not establish model quality or authorize adoption.

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
Eligibility **does not activate changes**. The message-prompt runner now executes
paired inference with a core-owned fixed grader; candidates supply only prompt
data. External evaluator authority, source experiments, safe activation, and
rollback for broader targets remain required M4/M5 work. No candidate authorizes its own adoption.

[Supervised message-prompt improvement](supervised-improvement.md) adds failure
discovery, bounded proposal generation, explicit paired evaluation, and operator
adoption/rollback at an idle session boundary. Immutable `PromptChange` records
bind the preceding and selected prompt, model, result, configuration and policy.
Selection is session-local and remains shadow-only; changed evaluation declarations
suspend it. The default automatic policy stays empty, and replay never reruns inference.
[Assessment prompt improvement](assessment-improvement.md) uses the same lifecycle
with independent context/progress selections per model and explicit
`supervised-assessment-prompt-v1` policy. Legacy records default to message
classification; no assessment selection changes task evidence or acceptance.

Inspect the same records in either interface:

```text
/improvements
harness improvements SESSION_ID --base-dir /path/to/harness-data
```

The memory plugin can later retain accepted lessons through its existing write
contract. Agent-swarm and the proposed experiment plugin may supply workflow or
experiment strategies; core owns these records and their enforcement boundary.
