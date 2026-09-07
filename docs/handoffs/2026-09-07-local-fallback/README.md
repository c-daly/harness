# M4 local fallback evidence

This is the bounded first fallback slice from PR22's merge (`d74a6cf`) on
`feat/task-fallback`. The [behavior and configuration](../../local-fallback.md)
describe the recovery boundary. M4 remains in progress.

## Real offline gate

The fixed `m4-local-fallback-v1` gate runs four real TUI journeys with the
preinstalled Qwen3-8B Q4_K_M model: connection refusal and HTTP 401, each with
plugins absent and with the normal memory plugin. The fault endpoints are
loopback simulations; the local inference, context retrieval and file writes
are real. Model prose cannot satisfy the external exact-file check or accept
the task. No private memory or generated prose is included in these reports;
temporary sessions are deleted after each journey. Memory writes remain zero.

| Report | Outcome |
|---|---|
| [Initial](fallback-initial.json) | All four exact writes passed; both connection-refusal decision-label checks failed. |
| [After adapter correction](fallback-before-retry-guard.json) | All four journeys passed. Superseded by the additional retry safeguard. |
| [After retry correction](fallback-before-child-guard.json) | All four journeys passed. Superseded by the cross-session child safeguard. |
| [Final](fallback-final.json) | All four journeys passed against the final core source hashes. |

The initial failure was a real adapter classification defect: LiteLLM wrapped
an OpenAI SDK connection error in an `InternalServerError` with synthetic
status 500. Harness now walks a bounded, cycle-safe typed cause chain to
preserve transport failure identity. It does not guess from words such as
"connection refused" in server error prose. The regression failed before the
fix and passes afterwards. No gate label or threshold was relaxed.

A separate strengthened side-effect regression exposed retrying a provider
after it had dispatched a tool: three writes occurred with two configured
retries. Dispatch now stops retries after observed tool/child activity; fallback
then holds for reconciliation. The same regression now requires exactly one
tool execution. Ordinary first-call transport retries still consume the shared
model budget and deadline.

The child-session regression caught a second form of this boundary: native
subagents log their work in a separate session, so the parent log alone cannot
establish absence of child work. Fallback now checks the shared cumulative child
reservation as well. The regression failed before this correction and now holds
the parent task after exactly one child run, without a retry or local switch.

Final measured times:

| Injected fault | Plugins | Task seconds | Cold readiness seconds | UI mount seconds |
|---|---|---:|---:|---:|
| Connection refusal | Absent | 24.763 | 10.473 | 0.049 |
| HTTP 401 | Absent | 13.032 | 11.466 | 0.257 |
| Connection refusal | Normal memory | 24.190 | 11.001 | 0.894 |
| HTTP 401 | Normal memory | 13.907 | 11.325 | 0.664 |

Gates fixed before the first run: task <=45s, cold readiness <=30s, UI mount
<=3s. Every journey requires one assignment, unchanged unresolved criteria,
one retrieval per configured source, one exact native write, recorded/visible
fallback, no automatic acceptance, settled replay and stopped owned runtime.
Connection-refusal cases retain all three ordinary retry events and six model
reservations; HTTP 401 cases use three reservations. Local tools and completion
account for the additional calls. Failed-call usage remains unknown.

## Reproduce

Use the [M3 container setup](../../local-assistant.md) and pinned image
`sha256:841b199aed2649a748875b043b32fed2e8c2d4d87e1d563556817fb7fa44b72b`.
The driver verifies loopback-only networking, configured 4 GiB RAM / zero swap /
four CPU limits and the preinstalled model hash before running. Mount the
repository and normal memory/vault read-only, and the report directory writable.
Run the container's repository Python entry point with:

```sh
-m scripts.qualify_fallback \
  --model-file /models/8b.gguf \
  --memory-root /home/fearsidhe/.claude/plugins/memory \
  --output /reports/fallback-final.json
```

Provision assets separately; no downloading occurs in this gate. Run the M3
regression in a separate container using `-m scripts.qualify_m3
--model-profile qwen3-8b` and the same model/memory/output arguments. Avoid two
8B runtimes competing for the same GPU. The final report records all top-level
core module hashes and the qualification driver/helper hashes. Earlier reports
have intentionally different hashes and are retained as historical evidence.

The [M3 run before the child correction](m3-before-child-guard.json) passed all
six journeys and four recovery cases. It is retained separately from the final
matching-source run; changes after a live run are not silently included in its
qualification claim.

The first M3 run on the final source hit
[two runtime timeouts](m3-runtime-timeouts.json): plugin-free cobalt during
restart and normal-memory maple during its first write. Four other journeys
and all four unavailable-runtime recovery cases passed. The GPU telemetry
worker also exceeded its five-second `nvidia-smi` deadline; its
[traceback](m3-monitor-timeout.txt) escaped at teardown, leaving the report
unfinalized with `passed = false`. The failed cleanup checks include the
requirement that a ready owned process had been captured; they do not alone
prove a process leak. No threshold, prompt or model setting was changed for
the subsequent repeat. This variability limits claims about consistent local
runtime latency even when another run passes.

The [unchanged final repeat](m3-final.json) passed all six journeys and four
recovery cases. It includes twelve exact writes, six real stream cancellations,
restart with fresh project records, normal memory and unavailable-runtime queue
controls. Cold readiness was 10.19–12.77s, warm resumed writes 1.52–2.69s, and
cancellation 0.122–0.125s. Hardware was an RTX 5070 with 12,227 MiB and driver
596.36; peak whole-device memory was 10,645 MiB. That observation includes other
GPU activity and is not a per-process allocation cap. Both final reports'
recorded source hashes match the proposed core and drivers.

Final repository validation: **1439 passed, 7 skipped, 6 warnings in 314.92s**.
The skips are the unavailable Anthropic/Ollama recorded fixtures and opt-in live
Antigravity test. Locked offline sync, Ruff, whitespace checks, sdist/wheel build
and a clean Python 3.13 wheel smoke importing 64 modules passed. An earlier full
run before the adapter/retry/child corrections passed 1436 tests; the final run
above includes those corrections and their regressions.

## Scope

These public fixtures support this bounded 8B workflow. They do not qualify
CPU-only use, universal tool conformance, arbitrary task/model reliability,
automatic semantic decisions, general cross-alias scheduling, external-agent
handoff or the full M4 self-improvement activation/rollback cycle.
