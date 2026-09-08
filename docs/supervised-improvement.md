# Supervised message-prompt improvement

[Assessment prompt comparison](assessment-evaluation.md) extends the same
controls with `/improvements compare ASSESSMENT.json`. Context/progress
candidates are operator-authored and comparison-only; they cannot use the
message-prompt adoption command.

Core can detect repeated classification failures, generate a bounded candidate,
run its frozen paired experiment, and explicitly select or roll back the result.
This loop changes **shadow message-prompt data only**. It cannot edit source,
change routing/context policies, execute tools, or accept tasks. Memory and
agent-swarm remain plugins.

## Operator workflow

Select an inference model alias, then use the current terminal session:

```text
/semantics classify TEXT
/improvements
/improvements propose
/improvements show CANDIDATE_ID
/improvements evaluate CANDIDATE_ID /absolute/path/to/experiment.json
/improvements show RESULT_ID
/improvements adopt RESULT_ID
/improvements rollback
```

Classification is opt-in and records observations in this session. For local
classification/evaluation, first run normal work or `/resources start ALIAS`;
background calls do not cold-start or displace a resident model. An explicitly
requested proposal is scheduled as work and can start the selected local runtime.

`/improvements` reports the selected version, compatibility, and whether two
distinct invalid outputs exist for the current prompt/model. This is a signal,
not a diagnosis of prompt causality. Generation receives at most eight failure
event IDs/reasons and the incumbent instructions. It receives no original
message prose, memory contents, or evaluation inputs/labels. Claims remain
hypotheses until measured. Observing failures does not automatically generate
proposals.

Only instructions inside the fixed `MessagePrompt` schema can change. Generation
is pinned to the selected inference alias, with no tools, 32,768 input bytes,
16,384 output bytes, 2,048 output tokens, 4,096 stream chunks and 60 seconds.
Blank, unchanged, malformed and oversized responses are rejected. Shared call
budgets, dispatch permissions, resource admission and cancellation apply.
Cancelled generation can leave evidence, but cannot leave a candidate/selection.

Freeze your own `PromptExperiment` JSON before evaluation: `EvaluatorConfig`,
`MessageEvaluationSuite`, and quality/latency gates. The
[public smoke example](examples/prompt-improvement.json) demonstrates the format;
**its public cases are not genuine held-out quality evidence**. Use representative
regressions and a separate held-out set. Preserve the fixed critical stop and
ambiguous-thanks cases. Require at least one improvement, no known regression,
every critical case passing, and predeclared latency limits. Do not revise labels
or gates after an unfavorable result.

The plan is recorded before alternating incumbent/candidate inference.
`show RESULT_ID` exposes paired pass/fail and latency measurements, exact gates
and versions, and candidate instructions. Artifacts retain detailed provenance.
This uses the [core paired evaluator](semantic-evaluation.md), not a model grader.

Proposal/evaluation and prompt changes require an idle session. The composer
stays editable; Esc cancels inference and new work takes priority after cleanup.
Inspection is available during inference. `semantics` and `improvements` are
reserved core commands that cannot be replaced by plugin prompt expansions.

## Adoption, suspension and recovery

Policy `supervised-message-prompt-v1` requires an explicit operator action,
the latest passing result for the candidate, at least one measured improvement,
a matching completed paired run, the current incumbent, and exact evaluated
candidate/configuration/evaluator versions. An older pass cannot override a
newer failure or inconclusive result. Manually recorded results without a paired
run cannot qualify. These controls are not model tools; automatic policy is empty.

`PromptChange` records the alias, preceding selection, exact before/after blobs,
result, evaluation configuration and policy. Selection persists **within this
session**, per alias. Default interpretation uses the selected prompt and its
evaluated inference limits. Explicit prompt overrides bypass selection; explicitly
different limits use the builtin prompt with a suspension reason. Context/progress
assessment prompts remain unchanged.

Changed provider endpoint/catalog declarations, evaluator source, Python/package
versions or other fingerprinted configuration suspend an adopted prompt. Normal
interpretation uses the builtin prompt and records suspension. Roll back before
proposing/evaluating a replacement. Fingerprints bind declarations; they do **not**
probe weights actually served by a mutable endpoint. `runtime_version` is an
operator-supplied declaration, not an attestation.

Rollback restores the exact preceding prompt/configuration without inference or
a running model. Rolling back a rollback restores the selection it undid. Blob
integrity remains mandatory; missing/corrupt artifacts must be restored, not
guessed. Replay rebuilds selections without generation or experiments. A run
without a durable result is aborted on recovery; an already committed completed
result can have its terminal event repaired without repeating it. Publication
failure cannot silently activate a prompt.

## Standalone CLI

Read-only inspection requires no catalog, session resume or model:

```sh
harness improvements SESSION_ID --base-dir /path/to/data
harness improvements SESSION_ID --base-dir /path/to/data --show RECORD_ID
```

The operator CLI resumes an idle session. Put common options before the action:

```sh
harness improve --base-dir /path/to/data --catalog /path/to/models.toml \
  --model local-small SESSION_ID propose
harness improve --base-dir /path/to/data --catalog /path/to/models.toml \
  --model local-small SESSION_ID evaluate CANDIDATE_ID /path/to/experiment.json
harness improve --base-dir /path/to/data --catalog /path/to/models.toml \
  --model local-small SESSION_ID adopt RESULT_ID
harness improve --base-dir /path/to/data --catalog /path/to/models.toml \
  --model local-small SESSION_ID rollback
```

Normal dispatch permissions apply to inference. Each standalone command closes
its owned runtime, so keep the terminal session open for locally owned evaluation
or use a separately managed warm endpoint.

The [offline smoke evidence](handoffs/2026-09-07-supervised-improvement/README.md)
uses real local generation/evaluation, explicitly injected format faults and
public cases. Controlled provider fixtures prove adoption, rollback, replay,
refusal and interruption paths. These do not establish improvement on unseen
interactions. M4 still needs held-out semantic qualification and external-agent
reconciliation. Assessment candidates, automatic policies and isolated source
changes remain later work.
