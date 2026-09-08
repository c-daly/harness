# Compare assessment prompts

Core can now compare proposed context-selection and progress-assessment prompts
against their built-in versions. Comparisons use the same durable paired runner
as message prompts. The operator supplies the candidate instructions, hypothesis,
frozen cases, expected results, and gates. Neither function is activated by a
passing comparison; assessment adoption is not implemented.

## Operator workflow

First record an assessment in the session with the model you intend to evaluate:

```sh
harness semantic context candidates.json --model local-small
# Copy the printed session ID.
harness improve --model local-small SESSION_ID compare assessment.json
harness improvements SESSION_ID
harness improvements SESSION_ID --show RESULT_ID
```

Both inference commands support the normal `--catalog`, `--base-dir`, and
`--allow` controls. No plugin is required or started. For progress in the terminal:

```text
/semantics progress
/improvements compare /absolute/path/to/assessment.json
/semantics
/improvements show RESULT_ID
```

Create/select a tracked task before `/semantics progress`. Comparisons require
an idle session and use the existing cancellable improvement worker. Esc retains
the draft and records an interrupted experiment; new work cancels and settles
the comparison before it starts. Read-only inspection remains available.

Start with the [context](examples/context-selection-comparison.json) or
[progress](examples/progress-assessment-comparison.json) JSON example. Set the
model alias and runtime declaration for your own configured inference model.
The examples contain **public smoke cases**, including their `held_out` partition.
They do not qualify generalization. Replace the illustrative held-out cases with
a separately frozen, representative corpus before drawing promotion conclusions.

An experiment contains:

- `candidate`: an `AssessmentPrompt` with the fixed function/version and changed
  instruction text only; no executable code, schema, or grader.
- `hypothesis` and `expected_benefit`: operator claims to test.
- `configuration`: model/runtime declarations, bounded semantic call limits, and
  whole-experiment timeout. Assessment defaults permit 16,384 payload bytes,
  32,768 complete input bytes, 2,048 output bytes, 256 output tokens, and five
  seconds per call; the complete experiment defaults to 120 seconds.
- `suite`: one function, fixed critical cases, and additional regression and
  held-out cases. Each case has an ID, partition, critical flag, typed input,
  and typed expected result. There are at most 64 cases and 1 MiB of suite data.
- Gates fixed before inference: at least one improved case by default, no
  critical failures or regressions on previously passing cases, at most 1.2 times
  incumbent total latency, and at most 2,000 ms per candidate case. These are
  paired comparison gates, not an absolute model-quality qualification.

The control links the latest eight eligible observations of the same built-in
function and model to evidence records. At least one is required. Invalid output
and timeout are failure observations; assessed/no-match/uncertain results can
support an operator correction hypothesis. The record explicitly leaves prompt
causality and benefit unproven. Denied, unavailable, disabled, and evaluation
fixture observations cannot seed these claims. Existing evidence is reused;
each comparison records a new candidate, frozen plan, run, and result.

## Oracle and evidence boundaries

Context's fixed cases cover stale and unavailable entries, misleading candidate
instructions, ambiguity, and selection caps. Progress's cases cover unchecked
completion, failed checks, pending review, active execution, and failed,
incomplete, cancelled, or aborted execution. Removing, relabelling, changing
inputs, or downgrading these critical cases invalidates the suite. Expected
results must themselves obey the normal scope and evidence validators.

Only the current case's input and the selected prompt instructions reach
inference. Other cases, partitions, expected results, hypotheses, and gates stay
with the evaluator. Each pair alternates which prompt runs first. The grader
compares ID sets and the complete reason/action, including focus and remaining
obligations; it ignores ID ordering. A valid `uncertain` is correct only when the
oracle expects it. Invalid output/timeouts fail; unavailable inference remains
unknown, including on expected-uncertainty cases.

Reports include both prompts' grades, a lexical context baseline or deterministic
progress baseline, sample counts, abstentions, and maximum/p95 latency, overall
and by partition. The runtime and held-out provenance remain operator declarations;
reports explicitly set `activation_qualified: false`. A paired pass alone does
not establish an error threshold or justify putting a model in the control loop.

Progress inputs are frozen snapshots, possibly synthetic or from another session.
They never trigger checks or modify the evaluating session's task. Each assessment
observation links to its recorded evaluation run. `/semantics` visibly labels it
as an evaluation fixture instead of describing its sequence as live task evidence.
Those observations cannot recursively seed improvement proposals as live failures.

Candidate and incumbent must target the suite's function. The evaluator binds
exact artifacts, cases, limits, provider/catalog declarations, and source hashes,
including the assessment grader. Configuration changes refuse or fail a run.
Cancellation, deadline expiry, crash recovery, and durable-result recovery retain
the shared evaluator's incomplete-run behavior. Replay never calls a model.

The embedding entry point is
`await kernel.improvement_service.compare_assessment(AssessmentExperiment(...))`.
For a previously recorded plan, the existing `harness semantic evaluate SESSION
PLAN --incumbent EXACT.json --config CONFIG.json` also supports assessments.
Assessment results cannot be adopted by the message-prompt adoption command.

## Qualification

`python -m scripts.qualify_assessment_evaluation --output REPORT.json` runs both
public examples on the pinned Qwen3-8B/llama.cpp CUDA profile in an isolated,
network-free container with at most four CPUs, 4 GiB host RAM, and no swap.
Weights and dependencies must already be available. The script freezes prompts
and gates before inference, separates completion of the comparison from candidate
quality, preserves task state, and stops its owned runtime. It uses no plugins
and does not measure memory integration or new native-agent task behavior.

See the [measured comparisons](handoffs/2026-09-08-assessment-evaluation/README.md).
Operator-authored prompt comparison is implemented. Assessment candidate
generation/adoption and genuine held-out qualification remain M4 work.
