# Destination loss and interruption during handoff

The implementation and each revised protocol were committed before their
real-model journeys. Each protocol runs busy, destination-loss and Esc cases first
without plugins, then with the installed normal memory plugin and read-only normal
vault. Reports retain each failed development run without regrading it.

The public process test reproduced false completion: after a successful B write,
killing the HTTP destination during its next response produced a fabricated
`end_turn`, followed by a completed handoff. LiteLLM's OpenAI stream wrapper can
synthesize a finish reason at EOF. The adapter now requires evidence that the
wrapper received a provider finish reason before emitting a terminal chunk.
Truncated text and complete-looking tool arguments fail with `MalformedStreamError`.
Provider exception bodies remain suppressed and owned HTTP clients close.

Busy preflight and started failure have different recovery rules. A busy refusal
does not consume the handoff record; after settling the other request, a still
current record can be used. A started loss or cancellation consumes the record.
The new checkpoint must retain completed B and reject another write to B, then a
fresh reconciliation authorizes only C. Task acceptance remains explicit.

The source process is a controlled Codex-compatible fixture with real MCP, not a
live subscription agent. The CLI runs actual destination inference; the pytest
fixture instead uses scripted HTTP responses. The long numeric response is a
fault trigger, not a semantic or task-quality benchmark. These checks qualify one
offline CUDA profile and do not establish provider parity or general portability.

## Development failures and corrections

| Protocol | Freeze | Result | Finding |
|---|---|---|---|
| [v1](protocol.json) | `f894004` | [4/6](offline-v1-report.json) | The normal-memory busy owner had finished by inspection; `done()` did not distinguish normal completion from preemption. The normal-memory interruption case timed out without phase diagnostics. |
| [v2](followup-protocol.json) | `5270b41` | [5/6](offline-v2-report.json) | Record the busy owner's actual outcome. The normal-memory loss journey settled correctly but failed to record its fresh handoff after restart. |
| [v3](acknowledged-protocol.json) | `5458515` | [4/6](offline-v3-report.json) | Click the composer and wait for submission acknowledgement. Normal-memory busy work completed before preflight and legitimately let the handoff start. The normal-memory loss record was again refused. |

The [v4 protocol](controlled-protocol.json), frozen at `439a173`, makes the busy
condition deterministic by holding consumption of a real stream after its first
text delta. The request, HTTP stream and local lease stay owned until explicit
cancellation, within the existing deadline. This exercises an active request with
controlled client backpressure; it does not claim sustained GPU work during the
hold or cross-session scheduling.

It also corrects the fixture's inspection of uncertain effects. An absent stage
write is `not_applied`; a write with matching file contents is `completed`;
unrecognized or mismatched effects remain uncertain. The public regression shows
that incorrectly marking a rejected C write completed blocks the next C
reconciliation. The earlier private sessions were deleted, so this reproduction
does not prove the exact cause of their recording failures. No core behavior or
model profile changed after `f894004`.

These are development qualification runs, not held-out confirmation or a latency
distribution. The original 4/6, 5/6 and 4/6 results remain failed.

## Final observed result

The [v4 report](offline-v4-report.json) passed **6/6 journeys and 172/172 checks**.
Each busy case passed 28 checks; each loss/Esc case passed 29. No critical checks
were dropped or regraded from the earlier runs.

| Fault | Plugins absent: recovery | Normal memory: recovery |
|---|---:|---:|
| Busy preflight | 4.282 s | 4.860 s |
| Destination loss after B | 4.144 s | 5.041 s |
| Esc after B | 4.349 s | 5.801 s |

Loss produced `MalformedStreamError`, not completion, and settled in 0.114/0.095 s.
Esc produced cancellation and settled in 0.063/0.104 s. Recovery stayed below the
fixed 45-second deadline. Exactly one successful Harness write per required stage
was recorded; A/B evidence retained its original provenance through C recovery.
Drafts survived, the final compositor displayed completion and fresh context,
operator review remained unresolved, and replay required no inference.

All six sessions used real Qwen3-8B Q4_K_M inference, loopback-only containers,
four CPUs, 4 GiB host RAM and zero swap. The three memory cases retrieved the
normal scoped subject (13,802 bytes; identical recorded hash). All qualification
containers and Harness-owned model and memory processes stopped. Runtime versions
were LiteLLM 1.88.1, OpenAI SDK 2.41.1, Textual 8.2.7 and MCP 1.27.2.

## Verification

The full Python 3.13 suite at the core freeze passed **1,785 tests**, with seven
skipped and six existing MCP deprecation warnings, in **401.11 s**. The affected
Python 3.12 provider/inference/handoff/resource/fallback suites passed **239 tests**
in **44.64 s**. The final driver and its inspection regression passed **seven
tests on each Python version** (26.32 s on 3.13; 26.16 s on 3.12).

The sdist and wheel built offline. The first clean wheel install could not resolve
dependencies from the fresh cache; its failure log is retained. The subsequent
clean wheel smoke fetched the missing dependencies and passed. A final rebuild
and offline clean wheel smoke also passed, including all 74 module imports.
All 75 core source files byte-match the wheel. Ruff and
whitespace checks passed. No dependency or core source change followed the
full-suite run.

Reports export metadata only: context byte counts/hashes, event/error types,
artifact checks, timings, source hashes and runtime identity. They exclude normal
memory payloads, generated prose, raw sessions, screenshots and exception bodies.

All **328 source hashes** across the four reports match their recorded freezes.
The [validation record](validation.json) and [artifact manifest](manifest.json)
record the checks and file hashes.

## Reproduce

Use the [normal-memory container recipe](../2026-09-09-handoff-memory-recovery/README.md#reproduce)
with the same installed 8B weights, pinned image, resource limits and read-only
mounts. Use a fresh container name and output directory, raise only the outer
container timeout to the recorded **600 s**, and replace its Python module and
arguments with:

```sh
-m scripts.qualify_handoff_destination --memory-root /memory \
  --model-file /models/8b.gguf --output /reports/controlled
```

The script refuses an existing output directory. It executes each of the six
journeys once and saves every outcome, including failures. A rerun is a new
observation; these reports are historical evidence, not expected exact outputs.
