# Bounded compaction: local recovery evidence

The final check starts the ordinary CLI/TUI with installed Qwen3-8B Q4_K_M,
llama.cpp b9603, an 8192-token context and disabled thinking. The isolated
`compaction-gpu` profile offloads to CUDA on the RTX 5070. About 7.4 GiB was
free before startup. Port 8186 was unused; Harness owns and closes this runtime.
The normal user catalog is unchanged. MCP and plugins are disabled; this check
does not isolate network access or qualify normal memory.

The [driver](terminal_check.py) replaces only the CLI's display launch with a
Textual pilot. It seeds three canonical messages: an early project codename,
2,400 disposable diagnostic records, and a late release token plus pending task.
The original full-history request is rejected for context overflow. Its initial
raw llama.cpp response reported **24,283 prompt tokens against an 8,192-token
window**. The owned transcript occupies 29,689 serialized bytes.

On the same input, `/compact` now produces nine bounded inference calls and one
final compaction event. The [report](report.json) records **25.788 seconds** for
compaction and **1.714 seconds** for an ordinary continuation asking about both
facts and the unfinished task. The reply preserves `amberfern`, `skyglass`, and
`/project/next.py`. Folded and live histories agree after compaction. All nine
progress states and the unsent draft were verified against the compositor.
The [progress](progress-terminal.svg) and [answer](complete-terminal.svg)
screenshots capture those surfaces. The original events remain in the log.

The timings are single observations on a shared host, not latency qualification.
This is a development smoke input used to correct the prompt, not a held-out
semantic evaluation. It does not establish arbitrary summarization fidelity,
CPU usability, broader M6 completion, or a useful M4 improvement.

## Retained failures

[Earlier attempts](earlier-attempts.json) retain their session IDs and model
outcomes. Every failed attempt retains all three original user messages and has
no compaction event.

- The first CPU startup timed out after a long host scheduling gap. It did not
  reach inference and is not a model-performance result.
- The next full-history probe received llama.cpp's structured context rejection,
  which the adapter mislabeled as a generic provider error. The adapter now maps
  the structured error to `ContextOverflow`, with `/compact` recovery guidance.
- CPU compaction timed out on the first portion after 120 seconds. History and
  draft survived; the initially blank timeout text prompted an explicit core
  timeout message. CPU compaction remains unqualified.
- The first CUDA response copied diagnostic records and exhausted its 1024-token
  output allowance. The core refused to apply that incomplete summary. The final
  instruction explicitly asks for one prose paragraph and condensation of
  repetitive records. The input and acceptance conditions were unchanged.

## Reproduce

Review the host paths and available GPU memory in [models.toml](models.toml).
Copy this directory to a temporary directory before running the driver: it writes
reports, screenshots and sessions beside itself. With the repository's installed
environment, run `uv run --no-sync python /tmp/<copy>/terminal_check.py` from the
repository. The driver refuses an occupied test port and closes the owned
runtime even on failure. It does not download weights or modify the user catalog.
The user catalog is optional: the driver records absence separately from a file
digest, so creation, deletion, or content changes are detected during cleanup.

## Automated checks

The full Python 3.13 suite passed **1,740 tests, with seven skips and six existing
MCP deprecation warnings**, in 403.36 seconds. This run preceded the final timeout
wording and prose instruction. After those changes, **27 affected regressions
passed on Python 3.13 in 18.20 seconds**, and **76 compaction/provider tests passed
on Python 3.12 in 4.98 seconds**. An earlier Python 3.12 group passed 113 affected
core/provider/selection tests. The new tests cover bounded portions, Unicode and
sidecars, partial failure, cancellation/timeout, stale snapshots, partial prior
compaction, execution-kind and routing boundaries, and rendered tool discovery.
Ruff and whitespace checks passed. The final source distribution and wheel built
offline; a clean temporary wheel installation passed CLI and all-module import
smoke checks.

PR33's review exposed an unconditional read of that optional user catalog. The
original driver reproduced `FileNotFoundError` before CLI startup. After the fix,
five isolated startup/cleanup checks passed for absent, unchanged, newly created,
deleted, and edited catalogs. These checks stop before inference; the runtime
and compaction behavior above are unchanged.
