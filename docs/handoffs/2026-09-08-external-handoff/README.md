# External handoff evidence

This bounded M4 slice starts from PR26's merge, `e576a8e`, on
`feat/external-handoff`. The [operator guide](../../external-handoff.md) describes
inspection, reconciliation, explicit destination selection, and continuation.
M4 remains in progress; this does not qualify portable M5 workflows.

The retained full-suite, wheel and offline reports below describe `0598460`.
The later PR27 review correction only adds `/handoff inspect|record|show|run`
to terminal help. Both existing help tests and a final-compositor smoke pass
for that correction; the earlier runtime reports were not rerun or relabelled.

## Real offline journey

A controlled Codex-compatible subprocess uses the real Codex adapter and real
MCP HTTP transport to write `A.txt`. It also writes `native.txt` outside the
Harness tool log, then exits with a controlled failure. The fixture asserts
that no authentication file was copied. The driver confirms the process is
reaped before both terminal run facts and inspects its effects.

The operator record authorizes only one exact `write_file` call for `B.txt`.
The session closes and resumes before the configured local Qwen3-8B model
continues it. No source-agent success, model response, or tool completion
constitutes task acceptance.

All 15 checks pass in both runs:

- Source failure, process settlement before terminal facts, completed Harness
  write, and an existing but explicitly opaque native effect.
- Native continuation completion with the same task, exact remaining artifact,
  unchanged completed artifacts, and exactly two successful writes overall.
- Earlier and new tool evidence with their original event provenance, outstanding
  operator review, fresh configured project context, and settled intent/run state.
- Evidence and reconciliation replay without executing inference.

The first continuation took **17.505 seconds**; the final revision took
**11.648 seconds**, including its cold local startup. These two public runs are
smoke evidence, not a latency distribution or held-out task-quality result.
The assignment, fixture, allowlist, expected artifacts and checks were unchanged.
The final revision adds stricter context adapter/configuration and post-approval
checks, a fingerprint of all core source files, and runtime version metadata.

Both runs used Qwen3-8B Q4_K_M on an RTX 5070 (12,227 MiB), llama.cpp
`9603 (ba1df050f)`, driver `596.36`, and image
`sha256:841b199aed2649a748875b043b32fed2e8c2d4d87e1d563556817fb7fa44b72b`.
Only loopback networking was present. Limits were four CPUs, 4 GiB host memory
and no swap; GPU memory is separate. The final cgroup peak was 4,037,574,656 bytes.
The owned container and runtime stopped; the user's separate endpoint was untouched.

The [initial report](initial-offline-8b.json) records the earlier revision.
The [final report](final-offline-8b.json) hashes all **71 core Python files plus
the driver**, verified against the final source. The frozen weight digest is
`d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`.
No model failures were injected into the local continuation. The source process
failure is intentional; this is **not a live subscription-agent qualification**.
The journey retrieves project context without plugins. The prior M3 normal-memory
gate remains separate; memory-enabled heterogeneous handoff needs further evidence.

Reproduce with the existing provisioned assets (adjust host paths if needed):

```sh
docker run --rm --pull never --name harness-handoff-qualification \
  --user 1000:1000 --network none --memory 4g --memory-swap 4g --cpus 4 --gpus all \
  --workdir /home/fearsidhe/projects/harness \
  --env PYTHONPATH=/home/fearsidhe/projects/harness \
  --env LITELLM_LOCAL_MODEL_COST_MAP=True --env LD_LIBRARY_PATH=/app \
  --env TERM=xterm-256color \
  -v /home/fearsidhe/projects/harness:/home/fearsidhe/projects/harness:ro \
  -v /home/fearsidhe/.local/share/uv/python:/home/fearsidhe/.local/share/uv/python:ro \
  -v /home/fearsidhe/projects/harness/.local-runtime/Qwen3-8B-Q4_K_M.gguf:/models/8b.gguf:ro \
  -v /home/fearsidhe/projects/harness/.local-runtime/reports:/reports \
  --entrypoint /home/fearsidhe/projects/harness/.venv/bin/python \
  sha256:841b199aed2649a748875b043b32fed2e8c2d4d87e1d563556817fb7fa44b72b \
  -m scripts.qualify_handoff --output /reports/handoff.json
```

The driver refuses other weights or unrestricted network/resource configuration.
Temporary fixture sessions are removed; durable reports contain only public
fixture metadata, checks, source hashes, and failure details.

## Repository verification

[Python 3.13 full suite](repository-tests.txt): **1,562 passed, seven skipped,
six warnings in 348.24s**, with real subprocess/MCP tests and final TUI compositor
inspection. The existing skips are unavailable Anthropic/Ollama conformance
fixtures and the opt-in live Antigravity test. No live provider claim follows
from those skipped tests.

[Python 3.12 focused suites](python312-focused.txt): **124 passed in 8.98s**,
covering handoff, terminal controls, task evidence, external runtimes and fallback.
The [first invocation](python312-initial-environment.txt) ran the parent interpreter
by absolute path without activating its environment. Three existing subprocess
fixtures consequently found a `python3` without MCP. `uv run --no-sync` against
the same locked environment fixed the invocation; their code did not change.

Ruff and whitespace checks pass. [Offline build](build.txt) and
[clean offline wheel smoke](wheel.txt) pass. All 70 shipped modules import and
wheel bytes match the 71 source files. Dependencies and lockfile are unchanged.

The new regressions cover retained authority across restart/chains, changed
source/configuration, uncertain effects, repeated-call reservation,
completed-target aliases, task evidence, replay/single-use plans, cancellation,
real native file-thread settlement under repeated cancellation, context binding
fingerprints, headless inspection, final terminal rendering and draft retention.
The existing dispatcher and external-runtime suites cover broader batching and
subprocess shutdown. Handoff-specific duplicate proposals are exercised
sequentially; the reservation itself occurs without yielding before invocation.

The [initial focused result](initial-focused-tests.txt) records the first 22
passing controls before later cases were added. A blocked duplicate initially
hid a successful earlier write from the evidence checker; the checker now
recognizes the hard guard's proof that the duplicate never dispatched. The
[initial process-fixture failure](initial-process-fixture-failure.txt) was an
incorrect `TaskDefinition.definition` lookup in the new driver; fixing the ID
lookup exposed and then passed the real process/MCP journey.

## Remaining boundaries

Operator inspection is required for provider-native effects, uncertain tool
outcomes, and a stopped process. No model supplies its own reconciliation or
acceptance. Finite native file calls finish separate remaining artifacts;
completed write/edit targets are held against further mutation in this tranche.
Legacy/source-version changes, arbitrary source hooks, missing/changed context
adapters, and unaccounted child sessions are held. Declared MCP bindings do not
attest remote server code or credential identity. No automatic handoff,
external-to-external continuation, plugin workflow migration, arbitrary source
self-editing, or daily-use quality gate is claimed.
