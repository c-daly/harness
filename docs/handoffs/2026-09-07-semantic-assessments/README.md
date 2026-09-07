# M4 shadow assessments: implementation and failed model gates

This continues merged PR21 (`5dfd808`) on `feat/semantic-context-progress`.
Core now supplies scoped context selection and recorded progress assessment,
alongside the existing message classifier. Memory retrieval and agent-swarm
remain plugin responsibilities. CLI commands and `/semantics progress` expose
explicit observations; normal workflow behavior stays deterministic.

## Measured result

The fixed **public** regression comparison ran three repetitions per case on the
preinstalled Qwen3-8B Q4_K_M / llama.cpp b9603 / RTX 5070 profile, inside a
loopback-only container capped at 4 GiB RAM, zero swap and four CPUs. It uses no
plugins or private memory. Progress cases are synthetic recorded evidence, not
claims that real project work happened.

| Function | Model correctness | Simple rules | Maximum latency | Gate |
|---|---:|---:|---:|---|
| Context selection | 15/18 (83.3%) | 15/18 (83.3%) | 2,309 ms | Failed: critical eligibility case and latency |
| Progress assessment | 9/21 (42.9%) | 21/21 (100%) | 453 ms | Failed: critical cases |
| Message interpretation | 12/12 (100%) | 12/12 (100%) | 147 ms | Passed public regression only |

The gates were fixed before inference: at least 90% correct, every critical case
correct, and no runtime-warm call exceeding two seconds. Runtime startup is
separate; the first inference includes client initialization and remains scored.
No thresholds, prompts, cases or labels were changed after observing failures.
All functions remain shadow/advisory. Nothing was promoted or automatically
applied. These repeated public cases are not independent held-out evidence.

The model selected an entry explicitly marked `unknown` when no eligible context
existed. It did find a semantic memory match missed by lexical overlap, but that
did not compensate for the eligibility mistake. For progress, it repeatedly
suggested `reconcile` across unchecked completion, failed checks, pending review
and active work. Validation rejected unsupported actions and incorrect focus
lists. Recorded passed/failed/remaining requirements remain visible independently
of inference. No requirement was erased and no task was accepted by a model.

The practical recommendation is to retain deterministic progress facts and
controls. A later experiment should test whether semantic prioritization or
explanation adds value beyond those facts. Broadly asking this model to decide
the task's next state is not justified by the evidence.

## Evidence and limits

- [Initial comparison](semantic-regression-initial.json): 51 scored observations;
  both new functions fail. Contains metadata only.
- [Diagnostic timeout](semantic-regression-diagnostic.json): no scored responses;
  an operational timeout during a run concurrent with repository tests. It is
  retained as a failed run, not counted as model-quality evidence. The precise
  timeout stage was not captured.
- [Matching-source comparison](semantic-regression-final.json): 51 observations
  after repository tests finished; reproduces the quality failures and includes
  bounded public-fixture responses explaining the rejections. Source digests,
  pinned weights, runtime version, hardware, limits and configuration are saved.
- [M3 regression](m3-regression.json): all six real offline project journeys and
  four missing-assets/startup-exit recovery cases pass against matching source.
  Includes native writes, actual cancellation/restart and normal memory reads;
  this separate report contains metadata only.
- [TUI compositor probe](tui-probe.json) and [terminal capture](progress-abstention.svg):
  a scripted invalid suggestion leaves a passed output, failed check and pending
  user review visible. This is interface verification, not real-model quality.

All native semantic operations remain bounded, tool-free, permission-checked,
budgeted, pinned, and recorded. Structural input errors are caller errors;
oversized valid inputs and runtime/output failures produce abstentions. The TUI
settles assessment cancellation before foreground work or session teardown. A
fresh busy observation for the selected local alias prevents a semantic call
from queuing behind it. This is not cross-alias GPU arbitration, remote generation
preemption, automatic fallback, or the M4 self-improvement promotion loop.

Remaining M4 work: fresh held-out evaluations for any proposed automatic
decision, broader resource scheduling and fault journeys, task-preserving
fallback with side-effect reconciliation, and the supervised evidence/candidate/
paired-evaluation/adoption/rollback cycle. The paired prompt evaluator currently
supports message interpretation only.

## Repository verification

The final complete suite passed: **1398 passed, 7 skipped, 6 warnings in
312.85s**. Skips are three missing Anthropic fixtures, three missing Ollama
fixtures and one opt-in live Antigravity test. Locked offline dependency sync,
Ruff, whitespace checks, sdist/wheel build and a fresh-wheel smoke importing all
63 modules passed. The earlier complete run also passed (327.85s); after adding
the deterministic evidence display, focused compositor/assessment tests and the
complete suite were rerun. These checks validate the implementation, not model
quality or M4 completion.

## Reproduce the public comparison

From the repository root, with the same preinstalled assets as M3:

```sh
mkdir -p .local-runtime/reports
docker run --rm --pull never --user "$(id -u):$(id -g)" \
  --network none --memory 4g --memory-swap 4g --cpus 4 --gpus all \
  --env LITELLM_LOCAL_MODEL_COST_MAP=True \
  --mount "type=bind,src=$PWD,dst=$PWD,readonly" \
  --mount "type=bind,src=$HOME/.local/share/uv/python,dst=$HOME/.local/share/uv/python,readonly" \
  --mount "type=bind,src=$PWD/.local-runtime/Qwen3-8B-Q4_K_M.gguf,dst=/models/8b.gguf,readonly" \
  --mount "type=bind,src=$PWD/.local-runtime/reports,dst=/reports" \
  --workdir "$PWD" --entrypoint "$PWD/.venv/bin/python" \
  sha256:841b199aed2649a748875b043b32fed2e8c2d4d87e1d563556817fb7fa44b72b \
  -B -m scripts.qualify_semantics --output /reports/semantic-regression-new.json
```

Exit code 1 represents an unmet gate and must not be relabeled as success.
Provisioning assets is separate; this command downloads nothing. Use a new
report filename to retain earlier evidence.
