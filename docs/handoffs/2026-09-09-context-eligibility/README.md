# Context eligibility before inference

PR34 merged at `eb1e6fb`. This tranche moves the existing available/current
predicate before inference and resolves an empty eligible set in core. It does
not change the builtin prompt, enable automatic context injection, or adopt any
of the previously failed prompts. Memory and agent-swarm remain plugins.

## Behavior and provenance

The model receives only candidates that the caller marked available and current.
Core retains the full bounded input in the observation's original `input` blob
and hash, and records the filtered request in `inference_input`. Query, selection
cap, and eligible candidate order/content remain unchanged. Returned IDs still
pass the original scope validator; filtering never makes an invented ID valid.

If nothing remains, an enabled request within the original input-size limit
returns `no_match` immediately. It needs no model permission, inference budget,
runtime startup, or available inference slot because it performs no model work.
The observation has `decision_source: eligibility`, no call ID or effective
model, and no inference-input blob. Other answers keep the default
`decision_source: model`; old observations remain readable. CLI and terminal
inspection display “Core eligibility” and “no model call.” Existing terminal
worker/queue admission remains in place.

Deterministic answers cannot seed prompt-comparison evidence or generated prompt
proposals. Paired prompt evaluation applies the same core predicate to both arms
and exposes each answer's source. Correct totals describe the entire selection
path; they must not be reported as model-only accuracy. A shared deterministic
answer cannot produce a paired prompt improvement.

## Public before/after experiment

The [protocol](protocol.json) allowed one implementation variant with unchanged
builtin instructions and six existing public cases. Both versions used the
installed Qwen3-8B Q4_K_M / llama.cpp b9603 profile: 28 GPU layers, 4,096-token
context, thinking disabled, presence penalty zero, semantic temperature zero,
120-second startup allowance, and five-second call deadline. Each ran in a
network-disabled container with four CPUs, 4 GiB host RAM, and no swap. GPU
memory was separate. Existing user runtimes and catalogs were left untouched.

The baseline ran from a separate copy of `eb1e6fb` before the filtered version.
Both warmed the same public first case once before scoring. The
[comparison](comparison.json) verifies identical prompt artifacts and records:

| Measure | Baseline | Filtered |
|---|---:|---:|
| Correct across all six cases | 4/6 | 5/6 |
| Model answers correct | 4/6 | 4/5 |
| Core eligibility answers correct | 0 | 1/1 |
| Scored model calls | 6 | 5 |
| Unavailable-context case | Invalid output, 963 ms | Core no-match, 0.2 ms |
| Critical ambiguity case | Failed | Failed |

All previously passing cases remained correct. The filtered run's maximum
recorded case latency was 912 ms; total recorded latency was 0.809 times the
baseline. These timings exclude cold startup and some preparatory work; they
are single observed service measurements on a shared host, not a general
performance qualification. Both versions preserved task state, prompt selection,
and an empty action transcript, settled calls, and stopped their owned runtime.

**The semantic gate still failed on ambiguity.** No fresh confirmation cases
were authored or run. This is a deterministic correction to the core selection
path, not learned improvement, new model accuracy evidence, or completion of M4.
The experiment used sequential source versions rather than randomized paired
arms, and every case was public development data. No third prompt candidate was
introduced after the previous experiment's two-candidate limit.

## Evidence and reproduction

- [Baseline report](baseline-run/report.json) and
  [inference events](baseline-run/inference-events.jsonl).
- [Filtered report](filtered-run/report.json) and
  [inference events](filtered-run/inference-events.jsonl).
- Each `blobs/` directory retains exact prompt and input bytes, including the
  filtered request payloads. Filenames contain their SHA-256 digests.
- [Executed driver](measure.py.txt), [comparison](comparison.json), and
  [artifact manifest](manifest.json). Baseline source hashes were checked against
  the Git object; filtered source hashes match the final core implementation.

Use the pinned image and read-only repository/interpreter/model mounts described
in the [previous handoff](../2026-09-09-context-selection/README.md). Copy
`measure.py.txt` into a new writable `/reports` directory and run:

```sh
python /reports/measure.py.txt filtered-run
```

Run the same driver for the baseline with a source copy created by
`git archive eb1e6fb | tar -x -C BASELINE_DIR`, setting the container's
`PYTHONPATH=/reports/baseline/src:/reports/baseline` and working directory to
that baseline. For the filtered version, set `PYTHONPATH` to the current
repository's `src` and root. Use separate output names; existing output
directories are refused. Inputs, catalog, isolation, and source hashes are
saved before inference. The model must be mounted at `/models/8b.gguf` and the
runtime must be the already installed pinned image. The historical driver
records the supplied profile; it does not install or independently attest
model weights. Asset verification was performed in the preceding PR's replay.

## Validation

- Full Python 3.13 suite: **1,756 passed, 7 skipped**, six existing MCP
  deprecation warnings, in 377.06 seconds.
- Python 3.12 affected semantic/evaluation/CLI tests: **140 passed** in 29.19
  seconds. CLI invocation and final rendered terminal inspection show core
  provenance without contacting a provider.
- A [real CLI invocation](offline-cli.txt) ran in a network-disabled container
  with no GPU and no model permission. Its [catalog](offline-catalog.toml)
  names nonexistent runtime and model assets. It returned core no-match, and
  [log checks](offline-cli-checks.json) verify zero model calls, zero runtime
  requests, and session cleanup. [Events](offline-cli-events.jsonl) and input
  artifacts are retained. This verifies the deterministic path without model
  availability; it is not a new offline inference qualification.
- Ruff, whitespace checks, sdist/wheel build, clean Python 3.13 wheel install,
  and 74-module import smoke passed. All 75 core file bytes match the wheel.
  Package installation used networking; model trials and the CLI probe did not.

## Remaining work

M4 still needs reliable disambiguation and fresh confirmation of useful semantic
behavior, plus broader handoff qualification. This change makes the empty-context
case dependable even without inference. It does not make the remaining relevance
and ambiguity decisions reliable enough to control context injection or tasks.
M5 heterogeneous supervision/portability/source-edit experiments and M6 sustained
daily use remain open.
