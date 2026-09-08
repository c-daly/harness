# Supervised assessment prompt improvement

Core now supports proposal, evaluation, explicit shadow adoption and rollback for
context selection and progress assessment, using the same lifecycle as message
classification. Each model alias has independent selections for the three
functions. Memory and agent-swarm remain plugins; neither is required.

A selected assessment prompt still produces a suggestion. It cannot accept a
task, run checks, retrieve additional context, change routing or permissions,
execute tools, or alter the evidence it assesses. This does not enable autonomous
context selection or task control. Genuine held-out qualification remains required
before promoting either function into a decision-making role.

## Terminal workflow

Select an inference alias and start its provisioned local runtime with normal
work or `/resources start ALIAS`. Background assessments do not cold-start or
displace another resident model. Record ordinary observations:

```text
/semantics context /absolute/path/to/candidates.json
/semantics progress
/improvements
```

`candidates.json` is the existing bounded `ContextSelectionInput` format used by
`harness semantic context`: a query, up to 16 already scoped candidates, and a
selection cap. Candidate summaries are data, not retrieval instructions. The
terminal reads at most 16,384 bytes. `/semantics` shows observations and its
available actions, including the new context control.

When two distinct invalid-output observations exist for the current prompt,
model and function, an operator can request a proposal:

```text
/improvements propose context
/improvements show CANDIDATE_ID
/improvements evaluate CANDIDATE_ID /absolute/path/to/context-experiment.json
/improvements show RESULT_ID
/improvements adopt RESULT_ID context
/semantics context /absolute/path/to/candidates.json
/improvements rollback context
```

Use `progress` instead of `context` for the other assessment function. Omitting
the function keeps the existing `message` default. A context/progress result
cannot be adopted as a message prompt or as the other assessment function.
`/improvements` displays the selection and proposal eligibility for all three.
`/semantics` records and displays the exact selected version and change ID.

The composer stays editable while work runs. Esc cancels; a new prompt takes
priority after cleanup. Proposals/evaluations and prompt changes require an idle
session. Read-only improvement inspection stays available during inference.

## Evidence and frozen evaluation

Proposal generation uses at most eight distinct invalid-output observations
from the current prompt/model/function. Assessment evaluation fixtures are
excluded, including failed fixtures. Timeout, model unavailability and a merely
uncertain but valid answer do not establish a prompt defect. Repeated invalid
outputs are a signal, not proof that the instructions caused the failure.

The proposer receives the incumbent instructions and event IDs/reason codes.
It receives no original message, candidate summaries, project title, task details,
private memory, raw failed response, or evaluation inputs/answers. It returns
only changed prompt instructions, a hypothesis and an expected benefit. The
requested function is fixed, and proposals that add control fields, change
function, repeat the incumbent, or violate the output bounds are rejected.
Generation uses the existing pinned, tool-free inference request: 32 KiB input,
16 KiB output, 2,048 output tokens, 4,096 stream chunks, and 60 seconds, plus
shared permissions and budgets. Observation alone never starts generation.

Freeze an `AssessmentPromptExperiment` before scoring the candidate. It contains
`configuration`, `suite`, and the quality/latency gates; it does not contain a
candidate or a model-authored oracle. Public shape examples are available for
[context](examples/context-selection-improvement.json) and
[progress](examples/progress-assessment-improvement.json). Their public cases in
the `held_out` partition are **not genuine held-out evidence**.

The existing immutable critical cases, full ID-set/action grader, deterministic
baselines, alternating pair order and fixed gates remain in force. Evaluation
loads its exact incumbent/candidate artifacts explicitly and bypasses the live
selection. A new comparison uses the currently selected incumbent, rather than
silently returning to the builtin. The existing operator-authored
`/improvements compare ASSESSMENT.json` workflow is also available.

Policy `supervised-assessment-prompt-v1` requires an explicit operator action,
the latest passing result, a completed paired run, at least one measured
improvement, unchanged current incumbent/model/configuration/evaluator, and the
exact candidate artifact. Failed, cancelled, inconclusive, stale or manually
unproven results cannot be adopted. A comparison never changes the selection by
itself. Reports continue to set `activation_qualified: false`: an operator's
shadow prompt experiment is not a qualification for autonomous task decisions.

## Persistence and compatibility

Selections persist in the same session, per model and function. Normal live
assessments use their selected prompt and evaluated inference limits. An explicit
limits mismatch uses the builtin with a suspension reason; it does not overwrite
the saved selection. Changed source, catalog/provider declarations, runtime
configuration, Python or package fingerprints likewise suspend a selection.
Declared runtime versions and endpoints do not attest the weights actually
served by a mutable remote endpoint.

Rollback restores the exact preceding prompt/configuration without inference,
including when a selection is suspended. Other functions and aliases keep their
selections. Existing message-prompt records retain their original policy and
replay as `message_kind`. Replay does not generate candidates or rerun experiments.
Task evidence, acceptance and conversation history stay unchanged.

## Headless controls and qualification

The CLI uses the same service with an explicit `--function` option:

```sh
harness improve --model local-small SESSION_ID propose --function context
harness improve --model local-small SESSION_ID evaluate CANDIDATE_ID context-experiment.json
harness improve --model local-small SESSION_ID adopt RESULT_ID --function context
harness improve --model local-small SESSION_ID rollback --function context
harness improvements SESSION_ID --show RESULT_ID
```

Use `--catalog`, `--base-dir` and `--allow` as with message improvements. Prepare
ordinary scoped assessments through `harness semantic context` or `progress`.
The embedding API adds `function="context_selection"` or `"progress_assessment"`
to `improvement_service.propose`, `adopt`, `rollback`, and `status`; `evaluate`
accepts `AssessmentPromptExperiment`. The default remains message classification.

The [evidence record](handoffs/2026-09-08-assessment-improvement/README.md) separates
controlled passing lifecycle tests from real local-model quality. The opt-in
`scripts/qualify_assessment_improvement.py` freezes public cases and gates before
inference, requests real local responses to deliberately difficult inputs, and
retains refusals and failed candidates. It exercises no plugins and makes no
normal-memory, held-out, source-editing or sustained daily-use quality claim.
