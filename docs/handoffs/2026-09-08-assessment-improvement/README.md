# Supervised assessment improvement evidence

This M4 slice follows PR27's merge at `d33b4e2` on
`feat/assessment-improvement`. It extends the existing prompt lifecycle to
context selection and progress assessment, with independent selections per
model/function. The [operator guide](../../assessment-improvement.md) explains
explicit proposal, evaluation, shadow adoption, suspension and exact rollback.

## Real offline results

Both public runs used actual Qwen3-8B Q4_K_M responses. Each function was given
two deliberately difficult public inputs: scoped context with unavailable/unknown
candidates, and a synthetic task with unfinished output/review obligations.
Each produced two distinct invalid-output observations. These are public fixture
responses, not naturally collected failures or independent quality samples.
No model output or provider failure was injected.

The operator's suites and gates were saved before any inference. They preserve
the prior immutable critical cases and public representative cases: six context
cases and nine progress cases. Inputs, labels, thresholds, generation settings
and core source were unchanged between runs. The model generated different
context candidate instructions on the two runs. Both remain preserved.

| Function | Result in both runs | Selection outcome |
|---|---|---|
| Context selection | Candidate generated; incumbent 4/6, candidate 4/6, rules 4/6. Critical cases failed and there was no improvement. | Candidate held |
| Progress assessment | Proposal rejected before a candidate or evaluation was recorded. | Builtin retained |

The final progress diagnostics establish that the proposal named the correct
function and supplied nonblank instructions, but **repeated the incumbent
instructions**, ignoring surrounding whitespace. The initial report only recorded `ValueError`. The
final driver adds the core rejection reason, boolean shape diagnostics, and a
check that rejection left no candidate. It does not expose provider error bodies
or change proposal/evaluation behavior. The rerun was for diagnosis, not a
relaxed gate or a search for a passing result.

The final context incumbent/candidate maximum latency was **303/295 ms**.
These values do not compensate for failed quality gates. No prompt was adopted
in either real run, and no progress candidate received a grade. All control
checks passed: failed proposals/evaluations left the builtin active, task state
and acceptance stayed intact, no conversation actions appeared, and records and
owned runtime settled. The successful adoption/rollback path is covered by
controlled provider tests, not claimed as a local-model success.

Reports: [initial](initial-offline.json) and [final](final-offline.json). The final
report's hashes match all **71 core files and three qualification scripts**.
Its quality/held-out flags remain false. Cases in the public `held_out` partition
are operator declarations, not genuine held-out promotion evidence. Normal-memory
integration, native file tasks, portable workflows and daily-use improvement are
outside this measurement.

## Runtime and reproduction

Runtime: llama.cpp `9603 (ba1df050f)`, RTX 5070 (12,227 MiB), driver `596.36`,
Qwen3-8B Q4_K_M, pinned image
`sha256:841b199aed2649a748875b043b32fed2e8c2d4d87e1d563556817fb7fa44b72b`.
Weights: SHA-256
`d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`,
5,027,783,488 bytes. The runtime was warmed separately from fixed semantic-call
deadlines. Only loopback networking was present, with a four-CPU quota, configured
4 GiB host-memory limit and zero swap; GPU memory is separate. Both owned
containers/runtimes stopped. The user's existing local endpoint was untouched.

The final cgroup peak was 1,211,777,024 bytes; the first reported 4,294,975,488
bytes, about 8 KiB above the configured memory limit. These are observed kernel
counters, not a controlled RAM comparison.
The fixture/code/gates did not change to reduce resource use.

Reproduce with provisioned assets using the same bounded Docker mounts in the
[external handoff recipe](../2026-09-08-external-handoff/README.md), changing the
module and output path to:

```sh
-m scripts.qualify_assessment_improvement --output /reports/assessment-improvement.json
```

The driver checks weights and isolation before inference. Repository, interpreter
and weights mounts remain read-only; only the report directory is writable.
It loads no plugins. Temporary sessions are removed, and public candidate/report
artifacts are preserved in the report.

## Repository verification

[Full Python 3.13 suite](repository-tests.txt): **1,588 passed, seven skipped,
six warnings in 351.64s**, including subprocess/MCP and terminal integrations.
The skips remain absent Anthropic/Ollama conformance fixtures and the opt-in live
Antigravity check. [Python 3.12 focused suites](python312-focused.txt):
**176 passed in 39.63s** across all improvement/semantic/evaluator/CLI/TUI paths.

[Focused UI/lifecycle run](ui-focused.txt): **46 passed in 32.48s**. The terminal
journeys use the actual rendered compositor to verify context/progress proposal,
evaluation, explicit adoption, selected-prompt use, and rollback. Shared worker
cancellation, editable drafts and read-only inspection retain existing coverage.

Regression coverage includes fixture exclusion, proposal schema/function/change
checks, failed/cancelled/stale/wrong-model/wrong-function adoption refusal,
independent selections, source/configuration suspension, explicit limit mismatch,
current-incumbent comparisons, frozen evaluation overrides, legacy message
records, unchanged task evidence and model-free restart/rollback. Successful
fixture grades prove these controls, not model quality on unseen interactions.

The [initial integration failures](initial-integration.txt) revealed that the
context TUI command did not exist; implementing `/semantics context FILE.json`
completed the operator path. The other failure was an obsolete assertion that
assessment adoption was unavailable. Updated UI/CLI checks now verify explicit
shadow adoption while ordinary comparison leaves selection unchanged.

Ruff, whitespace, locked offline sync, [sdist/wheel build](build.txt), and
[clean offline wheel smoke](wheel.txt) pass. All 70 shipped modules import and
wheel bytes match all 71 core source files. Dependencies and lockfile are unchanged.

## Remaining work

M4 remains open for genuine held-out semantic quality and broader live-agent/
normal-memory handoff qualification. The measured local model has not passed
these assessment quality gates. M5 adds heterogeneous supervision, portable
continuation and isolated source-patch experiments; M6 adds sustained daily-use
qualification. The resident's self-improvement lifecycle now covers all three
shadow prompts, with explicit operator controls and no automatic adoption policy.
