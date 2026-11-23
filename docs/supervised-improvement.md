# Supervised message-prompt improvement

[Assessment prompt comparison](assessment-evaluation.md) extends the same
controls with `/improvements compare ASSESSMENT.json`. Context/progress
candidates now also have a [supervised assessment lifecycle](assessment-improvement.md),
with explicit function-scoped proposal, adoption and rollback. The default
message-prompt adoption command still rejects assessment results.

Core can detect repeated classification failures, generate a bounded candidate,
run its frozen paired experiment, and explicitly select or roll back the result.
This loop changes **shadow message-prompt data only**. It cannot edit source,
change routing/context policies, execute tools, or accept tasks. Memory and
agent-swarm remain plugins.

## Evidence from ordinary tasks

After an ordinary follow-up, `/semantics` shows the classifier's interpretation
in the context of the previous request and delivered answer. A supported
`correction` judgment also appears in `/improvements` as advisory correction
evidence. Replay checks the original exchange, the exact model call and its
structured response. The observation identifies what the operator may be
challenging. Confirmed failures still require supporting evidence. Proposals
that cite a derived correction persist it through the ordinary improvement journal.

Open `/improvements` or run `harness improvements SESSION_ID` to see observations from recorded primary runs, requirement checks, explicit task acceptance and marked task or session outcomes. A completed run reports execution status. Check the cited requirements and acceptance events to determine what it established. Inspection reads the existing session journal without changing it. When a proposal cites one of these observations, Harness saves its evidence record before the proposal.

Use `/improvements feedback [--turn SEQ] TEXT` to attach a correction to a finished primary turn. For a closed session, use `harness improvements SESSION_ID --feedback TEXT [--turn SEQ]`. The default target is the latest finished primary turn. Repeating the same feedback for the same turn records it once. Use `/improvements show ID` to inspect the cited event and observation.

For a confirmed flawed response, use `/improvements failure [--turn SEQ] TEXT` or `harness improvements SESSION_ID --failure TEXT [--turn SEQ]`. Recording the failure needs no model or semantic service. A predeclared output requirement whose expected digest differs from a verified response artifact also yields native failure evidence. A missing or damaged output leaves correctness unverified.

Both failure commands save the observation first. They then attempt a retrospective using the selected local inference model if it is ready and permitted. Repeating the command preserves the existing evidence and assessment. The explicit review command below can revisit the failure later.

### Looking up prior experience

An ordinary Saoirse turn can use several earlier situations, actions and outcomes as worked examples. Harness allows this by default after checking the scope of the records. Native context selection proposes relevant examples and can abstain. A selected example can suggest a check. Only fresh evidence can establish the current answer. Look for the assessment in `/semantics` and the `supervised-examples` event to see what reached context preparation for the selected model. Changes to this default go through supervised evaluation and adoption.

In the current branch automatic selection uses an already-ready local inference model with capacity for the foreground call. It skips usage-capped work and history above the automatic scan limit. Optional examples must fit after ordinary context preparation. The original task and evidence records remain inspectable whether or not an example is selected.

Use `/improvements examples` to inspect examples from the session. The source records live in Harness's own session store. Saoirse's experience across tasks belongs to that same private system. Vault project notes document the design and qualification results. Recalling a failure or a worked example carries no permission to publish it to the vault.

For cross-session recall, linked Git worktrees share a locally verified checkout identity. Harness keeps an index of session IDs and journal positions. It checks the original run and the cited records before supplying an example. Two independent clones can have the same remote URL and still retain separate raw histories. Outside an identifiable checkout, Saoirse can use this launch's own examples.

When a turn uses examples and makes a successful local read or search, Harness can show the source and its observed result alongside a provisional interpretation. It can verify an exact answer against a declared output requirement. An unsupported answer stays unresolved. A wider claim about what follows from a source may still need a different check.

Run `/improvements review-failure FAILURE_ID` to investigate a recorded response failure. The retrospective reconstructs the original request and compares it with examples that existed in that session before the turn began. It reports which examples were eligible, which the selector saw, and whether selected examples had actually been prepared as original context. An incomplete search, abstention, or unavailable model leaves the question open. A complete no-match in that session can motivate proposing new wording. The retrospective does not generate or adopt a lesson.

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

The explicit classification command records an observation for the supplied text.
Ordinary replies use the same supervised function with their conversation context.
For local classification/evaluation, first run normal work or `/resources start ALIAS`;
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

Both the journal writer and replay check the matching evaluation start, paired result and completion before accepting a selection. Using a selected prompt or launching selected source also checks its supporting result artifact. Rollback can restore an intact original version if the departing candidate's result artifact has been damaged.

`PromptChange` records the alias, preceding selection, exact before/after blobs,
result, evaluation configuration and policy. Selection persists **within this
session**, per alias. Default interpretation uses the selected prompt and its
evaluated inference limits. Explicit prompt overrides bypass selection; explicitly
different limits use the builtin prompt with a suspension reason. Context/progress
assessments use their own independent selections.

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

The [offline smoke driver](../scripts/qualify_improvement.py) uses real local
generation/evaluation, explicitly injected format faults and public cases.
Controlled provider fixtures prove adoption, rollback, replay,
refusal and interruption paths. These do not establish improvement on unseen
interactions. M4 still needs held-out semantic qualification and broader live-agent
handoff qualification. Assessment candidates now have the supervised lifecycle
linked above; automatic policies and isolated source changes remain later work.
