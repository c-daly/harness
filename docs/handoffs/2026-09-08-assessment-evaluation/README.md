# Paired assessment evaluation evidence

This M4 slice follows PR25's merge at `700e5f0`. The core evaluator now compares
operator-authored context/progress prompts using frozen typed cases and the same
durable runner as message prompts. The [operator guide](../../assessment-evaluation.md)
describes the CLI/TUI controls and evidence boundaries.

## Real local comparison

The pinned Qwen3-8B Q4_K_M weights ran on the RTX 5070 using llama.cpp b9603,
container image `sha256:841b199aed2649a748875b043b32fed2e8c2d4d87e1d563556817fb7fa44b72b`.
The container had only loopback networking, at most four CPUs and 4 GiB host RAM,
and no swap. GPU memory is separate from that host cap. No plugins were loaded.
Cold startup used preinstalled assets; per-call measurements follow startup.
The owned runtime stopped after each run. The user's existing runtime was not
modified or stopped.

Both runs completed 30 paired inference calls and two real seed assessments.
No malformed response was injected. Seeds use public context and a synthetic
tracked task, not naturally observed project failures. All task state was
preserved, every arm produced a measured grade, and no prompt selection occurred.

| Function | Builtin correct | Candidate correct | Rules correct | Max builtin / candidate latency | Candidate gate |
|---|---:|---:|---:|---:|---|
| Context selection | 4/6 | 3/6 | 4/6 | 277 / 318 ms | Failed |
| Progress assessment | 0/9 | 2/9 | 9/9 | 488 / 422 ms | Failed |

These are final-run values. Both runs produced the same correctness counts.
The candidate instruction text, immutable critical cases, representative public
cases, and gates were unchanged between runs. The initial report omitted the
runtime version because llama.cpp prints it on stderr. The only driver change
captures both output streams; the final report records `9603 (ba1df050f)`.

The fixed gates require at least one improved case, every critical case passing,
no regressions on cases the incumbent passed, candidate total latency at most
1.2 times incumbent, and at most 2,000 ms per candidate case. Failed/unknown grades
are not hidden by latency. Both candidates fail quality requirements, despite
finishing quickly. Deterministic progress rules remain substantially stronger on
this small set. No threshold was relaxed and no prompt was adopted.

The `held_out` entries in these **public** examples are not genuine held-out
qualification. Reports explicitly state this distinction and set
`quality_qualification: false`; per-experiment reports also set
`activation_qualified: false`. Public comparisons establish runner behavior,
not a reliable model policy, improvement on unseen interactions, normal-memory
integration, or a new offline task qualification.

Retained reports:

- [Initial run](assessment-evaluation-initial.json), including its missing version metadata.
- [Final run](assessment-evaluation-final.json), with complete version metadata and
  hashes matching all 69 core source files and the two qualification scripts.

Reproduce through `python -m scripts.qualify_assessment_evaluation --output
/reports/assessment-evaluation.json` inside the pinned container with the
repository/interpreter/weights read-only and the report directory writable.
The script refuses missing/incorrect weights or unrestricted network/resource
configuration. It records exact experiments before inference.

## Control-path verification

Scripted providers prove paired success, critical-failure refusal, no label/gate
leakage to inference, impossible-oracle rejection, cross-function refusal,
budget/permission/provider abstention, cancellation/deadline persistence,
configuration drift, and model-free replay. Evaluation observations cannot seed
new live failure claims. Passing assessment results cannot enter message-prompt
adoption. Both CLI replay/inspection and the terminal's final rendered fixture
label, editable draft, cancellation, and preserved task state are exercised.

The first terminal run exposed two test assumptions: an inspector awaited a
previously cancelled worker, and an assertion required a long label to stay on
one line. Tests now wait only for active workers and compare normalized rendered
text; no production cancellation behavior or layout was weakened. The corrected
terminal cases pass.

Python 3.12's focused assessment, semantic, improvement, CLI and terminal run
passed **138 tests in 29.30s** using the unchanged lockfile in an isolated
temporary environment. Full repository and wheel validation are recorded in the
[implementation record](../../superpowers/plans/2026-09-06-core-agency-progress.md).

Sdist/wheel build and a clean Python 3.13 install/import smoke passed for all
68 shipped modules. Wheel bytes match all 69 source files including `__init__`.
The first offline install could not resolve uncached package-index metadata;
the successful smoke fetched metadata using the unchanged lockfile as version
constraints. This packaging check used networking; the GPU comparisons did not.

## Remaining work

M4 remains in progress: genuine held-out qualification, assessment proposal/
adoption policy, and explicit external-agent side-effect reconciliation/handoff
are still open. Prompt comparison is core; memory and agent-swarm remain plugins.
