# Normal-memory handoff failure and recovery

PR37 merged at `216762c`. This increment advances the independent M4 handoff
track while semantic context selection remains advisory and unqualified.

The [protocol](protocol.json) fixes two one-shot offline terminal journeys before
local inference: a plugins-absent control and a required normal-memory failure
followed by explicit reconciliation and session restart. The source process is
a controlled Codex-compatible fixture using real MCP, not a live subscription
agent. The destination is the installed Qwen3-8B model. The memory case uses the
installed memory plugin and normal vault through read-only mounts.

The shared operator controls now distinguish a preflight refusal from failure
after an attempt starts. The latter explains that its record was used and directs
the operator to inspect and record a new handoff. A returned incomplete outcome
gets the same guidance. Provider error bodies remain suppressed, and cancellation
retains its existing interruption path.

Every declared check is critical. A handoff cannot pass on caught exceptions or
a partially populated report. The memory failure must precede inference, preserve
artifacts and the draft, leave task acceptance outstanding, refuse the used
record, and settle its intents. The resumed continuation must retrieve fresh
normal memory, finish exactly B, preserve A/native effects and task identity,
retain original tool evidence, display failure and recovery, and stop its owned
processes. The 45-second task limit remains fixed.

Only metadata is retained. Temporary sessions containing normal memory are
deleted; no private payloads, generated prose, raw logs or terminal screenshots
are exported. The implementation and protocol were frozen at `5c6e399` before
the real local-model run.

## Observed result

**Both journeys passed every declared check:** 21 without plugins and 30 with
normal-memory failure and recovery. The [offline report](offline-report.json)
retains the exact catalog/context policies, resource bounds, model/runtime
identity, timings and checks.

| Journey | Checks | Resumed local continuation | Outcome |
|---|---:|---:|---|
| Plugins absent | 21/21 | 12.911 s | Same task, exactly one remaining write, earlier effects and review preserved |
| Required memory lost, then restored on restart | 30/30 | 6.569 s | Failed before inference, used record refused, fresh reconciliation completed remaining work |

The memory journey retrieved **13,802 bytes** from the normal `harness` subject
after restart. It records the byte count and hash, not the contents. Both final
attempts fetched project context; both had exactly two successful Harness
`write_file` calls overall
(one A before the controlled source failure and one B after reconciliation).
Operator review remained outstanding, and replay reconstructed the evidence
without inference. The terminal's final compositor showed the memory failure,
reconciliation guidance, completed handoff and freshly ready context. A typed
draft survived the failed attempt.

The image was the pinned llama.cpp CUDA build, version `9603 (ba1df050f)`, on
an RTX 5070 with 12,227 MiB VRAM. The unchanged task profile used full GPU offload,
8,192-token context and its recorded sampler defaults. Both ran with only loopback,
four CPUs, 4 GiB host RAM and no swap. Every owned memory process, model runtime
and the qualification container stopped. User-managed services were untouched.

These are two bounded smoke journeys, not latency distributions or live-provider
qualification. The source process intentionally fails and performs an opaque
native write; local inference is real. No destination model failure was injected.
The memory fault is an actual stop of the session-owned MCP child, not a claimed
cloud outage. The independent semantic-quality gate remains unsatisfied, and M4
still needs broader handoff fault coverage. M5 portability/mixed-agent workflows
and M6 daily use are not qualified by this result.

## Repository validation

- [Python 3.13 full suite](tests-python313.txt): **1,774 passed, seven skipped**,
  six existing MCP deprecation warnings, in **401.36 seconds**.
- [Python 3.12 affected suites](tests-python312.txt): **110 passed** in
  **14.26 seconds**, covering handoff, terminal controls, fallback and context.
- [Offline sdist/wheel build](build.txt) and [clean wheel smoke](wheel.txt) passed.
  All 74 shipped modules import; all **75 core source files** match wheel bytes.
- All **81 recorded core/driver/profile hashes** match the execution freeze and
  current source. Both journeys match the protocol's complete check sets and
  exact runtime catalog. The installed memory server hash was rechecked.
- Ruff and whitespace checks passed. The [validation record](validation.json)
  and [manifest](manifest.json) bind these results and retained artifacts.

Regression tests use a public MCP fixture with real processes/transport and
scripted destination inference. They exercise failed-context settlement,
single-use refusal, restored context after restart, preserved tool evidence,
terminal messages, and completed-write counts. Separate message tests cover
provider-error redaction and an incomplete iteration-limited continuation.
The local-model report above is separate evidence and uses normal memory.

## Reproduce

With the pinned image, Python environments, weights and normal memory already
installed on the measurement host:

```sh
handoff_reports=$(mktemp -d /tmp/harness-handoff-memory.XXXXXX)
timeout --signal=TERM --kill-after=10s 480s docker run --rm --pull never \
  --user 1000:1000 --network none --memory 4g --memory-swap 4g --cpus 4 --gpus all \
  --env LITELLM_LOCAL_MODEL_COST_MAP=True --env LD_LIBRARY_PATH=/app \
  --env PYTHONDONTWRITEBYTECODE=1 --env TERM=xterm-256color --env MEMORY_VAULT_DIR=/vault \
  --mount type=bind,src=/home/fearsidhe/projects/harness,dst=/home/fearsidhe/projects/harness,readonly \
  --mount type=bind,src=/home/fearsidhe/.local/share/uv/python,dst=/home/fearsidhe/.local/share/uv/python,readonly \
  --mount type=bind,src=/home/fearsidhe/.claude/plugins/memory,dst=/memory,readonly \
  --mount type=bind,src=/home/fearsidhe/projects/vault,dst=/vault,readonly \
  --mount type=bind,src=/home/fearsidhe/projects/harness/.local-runtime/Qwen3-8B-Q4_K_M.gguf,dst=/models/8b.gguf,readonly \
  --mount "type=bind,src=$handoff_reports,dst=/reports" \
  --workdir /home/fearsidhe/projects/harness \
  --entrypoint /home/fearsidhe/projects/harness/.venv/bin/python \
  sha256:841b199aed2649a748875b043b32fed2e8c2d4d87e1d563556817fb7fa44b72b \
  -m scripts.qualify_handoff_memory --memory-root /memory \
  --model-file /models/8b.gguf --output /reports/offline
```

The output directory must be new. Existing reports are never overwritten, and
setup failures are retained. The normal-memory subject must exist within the
unchanged shipped byte limits. An unavailable or oversized required source holds
work; do not substitute a fixture and label it normal-memory evidence.
