# Semantic observations and prompt evaluation

Core now supplies an explicit message-interpretation service and a paired prompt
evaluator. They use the existing inference dispatcher, permission engine, shared
call budget, local-readiness service, session log, and verified artifact store.
Both work with all plugins absent. Memory and agent-swarm retain their own roles.

This is a partial M4 implementation while real-model M3 qualification continues.
Normal submission, queueing, task acceptance, cancellation, routing, and recovery
remain deterministic. No background classifier runs on typing or submission.

## Run or inspect a shadow observation

```sh
harness semantic classify 'Thanks, I may have more changes.' --model local
harness semantic inspect SESSION_ID
```

Use an explicitly configured inference alias from the normal catalog. The
commands also accept `--catalog` and `--base-dir`; inference commands accept
`--allow` and load the normal permission configuration. External-agent aliases
are refused. The command starts no plugins or MCP servers. A local alias uses
its existing readiness/owned-process contract; assets must already be provisioned.

The classifier returns one of `new_request`, `continuation`, `correction`,
`question`, `acknowledgement`, `stop_request`, `pause`, or `uncertain`. There is
no accepted/completed-task label and no confidence number. Acknowledgement,
temporary pause, and accepted task completion remain distinct. Missing context
should produce `uncertain`; model compliance still requires measured evaluation.

Default limits are 4,096 message bytes, 8,192 total input bytes, 512 output bytes,
64 output tokens, 128 stream chunks, and five seconds including permission and
readiness waits. Hard ceilings bound configurable limits. The service requests
temperature zero, strict JSON, and no tools; validation is local. A malformed
response, proposed tool, denied permission, exhausted budget, timeout, or
unavailable provider produces an abstention. Cancellation records an abstention
and propagates to its caller. Concurrent service calls abstain as busy instead
of waiting behind an existing semantic call. This is not a GPU scheduler or a
promise that an already running model generation can be preempted.

Embedding code can use:

```python
observation = await kernel.semantics.interpret(
    "Thanks, I may have more changes.", model=ModelId("local"),
)
```

`SemanticObserved` records the schema/function version, exact prompt artifact,
input hash, bounds, requested/effective model, successful dispatch call ID,
latency, kind, and abstention reason. Model-call purposes contain the observation
ID, including failed calls. Oversized messages are rejected before unbounded
encoding/hashing and have no input hash. Inference results now expose effective model/call
identities. A rewritten model cannot be attributed to the originally requested
model. Semantic completions do not enter the conversational fold.

TUI `/semantics` reads the latest ten observations and reports the total count;
it makes no inference call. `/improvements` shows candidates, evaluation results,
and recent run states. Neither command adopts a candidate or changes task state.

## Prepare a fixed experiment

The embedding API records evidence and a plan before running the experiment.
`kernel` below is an already started session, and `source_seq` identifies the
actual prior event whose failure/correction motivates the candidate. The caller
owns these records and the oracle; models cannot supply their own grading rules.

```python
from harness.improvement import Candidate, Evidence, EvaluationPlan
from harness.semantic_evaluation import (
    EvaluatorConfig, MessageEvaluationCase, MessageEvaluationSuite, REQUIRED_CASES,
    evaluator_version, run_evaluation,
)
from harness.semantics import DEFAULT_MESSAGE_PROMPT, MessagePrompt
from harness.types import ModelId

blobs, journal = kernel.session.blobs, kernel.improvements
incumbent = blobs.put(DEFAULT_MESSAGE_PROMPT.model_dump_json().encode())
candidate = blobs.put(MessagePrompt(instructions=candidate_instructions).model_dump_json().encode())
config = EvaluatorConfig(model=ModelId("local"), runtime_version=runtime_declaration)
suite = MessageEvaluationSuite(cases=(
    *REQUIRED_CASES,
    MessageEvaluationCase(id="held-question", partition="held_out",
                          text="How is this going?", expected="question"),
))
journal.record(Evidence(id="e1", source_session=kernel.session.id, source_seq=source_seq,
    observation="The recorded question was misclassified.", category="correction"))
journal.record(Candidate(id="c1", target="prompt", incumbent_version=incumbent.sha256,
    artifact=candidate, evidence_ids=("e1",), hypothesis="Recognize the missed question pattern",
    expected_benefit="Fewer interpretation errors without losing critical stop/ambiguity cases"))
plan = EvaluationPlan(id="p1", candidate_id="c1", incumbent_version=incumbent.sha256,
    candidate_version=candidate.sha256, suite=blobs.put(suite.model_dump_json().encode()),
    evaluator_version=evaluator_version(kernel.provider, config), cases=suite.plan_cases(),
    min_improved_cases=1, max_latency_ratio=1.2, max_case_latency_ms=5000)
journal.record(plan)
result = await run_evaluation(kernel, plan.id, incumbent=incumbent, config=config)
```

The short illustrative suite is not a model-qualification corpus. Use a frozen,
representative regression set and separately held-out cases not used to develop
the prompt. Fixed critical stop and ambiguous-thanks cases cannot be removed,
relabelled, or made noncritical. There must also be held-out cases. Set thresholds
before scoring, preserve failed trials, and use fresh held-out evidence after
candidate development; repeatedly tuning against the same cases is not held-out
qualification. Automatic candidate discovery/generation remains future work.

Prompt artifacts contain only the fixed `message_kind` function/version and
instruction text. They cannot provide executable code, schemas, expected labels,
resource limits, or adoption policy. Suites contain at most 64 cases and 1 MiB.
Every case identity, partition, and critical flag must agree with the stored plan.
Only the current case's text reaches the model; expected labels and other cases
are retained by the evaluator. Incumbent and candidate alternate running first.

The evaluator digest binds its implementation, semantic/inference/dispatch/gate
source, selected dependency/Python versions, provider type/configuration, model
alias, explicit runtime declaration, and limits. Changes before execution refuse
the plan; changes during execution fail the run. `runtime_version` is a caller
declaration, not remote weight attestation. Reports explicitly retain
`runtime_identity_verified: false`. Unknown server identity and behavioral model
quality remain separate qualification work.

To execute a saved plan from a closed session, save the **exact** incumbent
artifact bytes and `config.model_dump_json()` to files, then run:

```sh
harness semantic evaluate SESSION_ID p1 --incumbent incumbent.json --config evaluator.json
harness improvements SESSION_ID
```

`evaluate` prints the report and returns nonzero for failed/inconclusive results.
It uses the saved plan without rewriting thresholds. The default whole-experiment
deadline is 120 seconds, with a 600-second hard ceiling. Each attempt also shares
the normal call budget and the per-call semantic deadline.

## Grades, interruption, and evidence limits

The grader compares returned labels against fixed expected labels. It records
paired pass/fail/unknown measurements plus per-case observations, a conservative
deterministic-rule baseline, sample counts, correct counts, abstentions, and
maximum/p95 latency by regression/held-out partition. Baseline latency is unknown
because only inference paths are timed. Abstention on an expected `uncertain`
case can be correct; unavailable inference cannot count as a correct abstention.
There is no model-generated self-score.

`EvaluationRunStarted` is durable before inference. Cancellation and whole-run
timeout preserve completed measurements and leave missing ones unknown. A failed
or interrupted run cannot pass even if its known measurements look good. Known
critical failures remain failures. Resume closes a crashed run as aborted without
executing it again. If its `ExperimentResult` was already durable, resume restores
that result's terminal status instead. Old experiment records retain their
previous completed semantics through additive defaults.

Passing still goes through the separate adoption policy, whose default requires
review for every target. The runner has no activation or file-editing operation.
This implementation qualifies the evaluation/control plumbing with scripted
providers, not real local-model quality or latency. Context selection, progress
assessment, automatic evidence-driven candidate proposals, scheduling, fallback,
activation, isolated source experiments, and rollback remain roadmap work.
