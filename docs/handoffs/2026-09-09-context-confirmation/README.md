# Rejected context-selection experiments

PR35 merged at `db71d51`, with both Python CI versions and review passing.
This follow-up tried to resolve the remaining ambiguity failure after core
eligibility filtering. **Both prompt candidates and one separately declared
schema-order experiment failed their development gates. Nothing was adopted,
the schema change was reverted, and no fresh confirmation cases were written
or evaluated.** This PR retains evidence and updates the plan; it adds no
production behavior.

## Prompt experiment

The [protocol](protocol.json) allowed two operator-authored prompt candidates
against the six existing public cases. Candidate 1 appended a narrow unresolved
pronoun rule to the builtin. Candidate 2 clarified that semantic paraphrases
within the supplied project context should be selected when only one subject
is relevant. The local model evaluated these instructions; it did not author
them. No grading labels or other cases reached its inference requests.

Each run used the existing core paired evaluator, alternating prompt order per
case. Eligibility filtering was identical in both arms. The empty eligible set
was answered by core in both arms and is included in the six-case totals.

| Trial | Builtin correct | Candidate correct | Rules correct | Candidate maximum latency | Total latency ratio | Rejection |
|---|---:|---:|---:|---:|---:|---|
| [Candidate 1](candidate-1.json) | 5/6 | 5/6 | 4/6 | 1,897 ms | 1.079 | Fixed ambiguity but incorrectly abstained on the memory-storage paraphrase |
| [Candidate 2](candidate-2.json) | 5/6 | 5/6 | 4/6 | 1,243 ms | 1.006 | Preserved the paraphrase but still guessed the ambiguous policy |

Both failed the fixed critical/no-regression/improvement requirements. Both
completed all pairs and passed the replay driver's mechanics checks, including
actual adoption refusal. Those passing mechanics checks do not qualify either
prompt's behavior. The public case labelled `held_out` is still development
data, not confirmation evidence.

The planned later confirmation required 32 new synthetic cases, 29 correct
overall, at least 26/28 model-based cases correct, three improvements on fresh
cases and a three-answer advantage over lexical rules, no critical failures or
regressions, and the existing 2,000 ms / 1.2 latency limits. It also required
committing the candidate/configuration/gate freeze before authoring fresh cases.
The same agent would have authored candidate and cases, which would not establish
independent human-labelled production quality. That stage was never reached;
no post-development confirmation freeze or confirmation suite exists here.

## Schema-order experiment

The raw prompt-trial responses selected IDs before emitting their decision
label. After the two prompt trials ended, the separate
[schema protocol](schema-protocol.json) declared one implementation variant:
request the label before the IDs, keeping the builtin prompt and all logical
output constraints unchanged. This was a new source-format hypothesis, not a
third prompt candidate or a relaxed gate.

The [exact patch](schema-order.patch) changed only the field order of
`ContextSelection`. The [experimental source snapshot](semantic_assessment.experimental.py.txt)
and [executed driver](schema-development-driver.py.txt) retain the tested bytes.
Raw responses confirm that the model actually emitted `reason` first. It still
selected a deployment policy for “Find its policy,” however, and again scored
**5/6**. Its maximum recorded latency was **2,057.55 ms**, also exceeding the
unchanged 2,000 ms gate. The comparison was sequential on a shared host, so the
latency difference does not establish a causal performance effect of ordering.
The field-order change was reverted. Current core files match merged main.

## Runtime and evidence

All trials used the installed Qwen3-8B Q4_K_M, verified by the prompt driver
against its pinned artifact hash, with llama.cpp b9603. The profile used 28 GPU
layers, a 4,096-token context, thinking disabled, presence penalty zero, and
semantic temperature zero. Each owned container had no external network,
four CPUs, 4 GiB host RAM, and no swap. GPU memory was separate; no user runtime
was displaced. Cold loading had a 120-second allowance and per-call semantics
kept their five-second deadline. All owned runtimes stopped.

The prompt reports received a [post-run metadata correction](metadata-correction.json):
`weights` now records only artifact identity, and `runtime_command` copies the
effective command from each retained catalog. The original reports incorrectly
included profile defaults with presence penalty 1.5. The correction records their
original commit, hashes, and removed defaults; measurements and executed source
hashes are unchanged. The replay driver now writes this separation directly.
The command's server temperature default is 0.7; semantic requests override it
with temperature zero.

- [Prompt trial 1](development-1/report.json), [trial 2](development-2/report.json),
  and [schema trial](schema-development/report.json).
- [Unrounded comparison summary](summary.json) and [artifact manifest](manifest.json).
- Each trial retains actual inference events and `referenced-inputs.json`.
  The latter maps blob hashes to their exact UTF-8 prompt and input bytes,
  including the filtered payload. Prompt trials also retain their exact typed
  experiments and catalogs. The schema report records its catalog and suite.
- Seeds and the schema warmup used deliberately supplied public snapshots;
  they are not naturally collected user failures. No model outputs were injected.

Reproduce the prompt trials with `scripts/replay_context_selection.py` and the
container invocation in the [earlier handoff](../2026-09-09-context-selection/README.md),
substituting these candidate files and new output directories. For the schema
trial, apply the retained patch with `git apply --unidiff-zero` to `db71d51`
in a separate checkout and run
`schema-development-driver.py.txt` as a Python script in the same bounded
container, with that checkout's `src` and root in `PYTHONPATH`, model weights at
`/models/8b.gguf`, and `/reports` writable. Its positional argument is a new
output-directory name. Reusing any of these cases adds public regression
evidence, never fresh confirmation.

## Validation

Both frozen prompt experiments parse and independently retain a failed core
verdict. The candidate artifact hashes match their plans. All **131 retained
event envelopes**, **26 referenced input/prompt blobs**, and **231 recorded
source hashes** were checked. The schema trial's one changed source file matches
its retained experimental snapshot; all other source hashes match the executed
sources at `db71d51`. The current replay driver contains the later metadata fix,
covered by a regression check of the saved report and catalog before runtime
startup and after startup failure. [Validation results](validation.json) identify
these post-run script/test changes; core source, dependency declarations, and
lockfile still match `db71d51`. Ruff and whitespace checks passed. No new
full-suite, packaging, or inference result is claimed for this metadata fix.

## Decision and next work

Keep the builtin and the merged deterministic eligibility check. The tested
8B profile still fails a basic ambiguity case, while more conservative wording
can damage a previously correct semantic match. Neither these trials nor the
earlier ones support using it for automatic context selection.

The next useful quality investigation is a bounded comparison of alternative
inference profiles or models on this function, before further wording-only
tuning. Native tool-work performance and semantic-selection performance need
separate measurements. Keep the same critical and regression gates, and create
fresh confirmation cases only after a candidate clears development. M4 remains
open; broader handoff qualification, M5 heterogeneous workflows/source-edit
improvement, and M6 daily use also remain open. Memory and agent-swarm remain
plugins.
