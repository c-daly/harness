# M4 supervised improvement evidence

This slice starts from PR24's merge (`032f0fc`) on
`feat/supervised-improvement`. It implements the first complete supervised core
improvement loop for shadow message prompts. The
[operator guide](../../supervised-improvement.md) describes the controls and
limits. M4 remains in progress; no automatic semantic decision is qualified.

The reports below describe `55c1367`, before PR25's review corrections to the
catalog-source fingerprint and initial status-bar rendering. Validation of
those corrections is recorded in the implementation progress log. The GPU
gate and earlier local full suite were not rerun for this scoped correction.

## Real offline smoke

The [final report](supervised-improvement-final.json) passes both technical journeys
of `m4-supervised-improvement-v1`, with plugins absent and normal memory enabled.
Each journey performs a real project-context/native-write task in the TUI,
records two explicitly injected malformed classification responses, generates
a candidate with real Qwen3-8B inference, and runs the real paired evaluator.
The injected responses establish detector behavior; they are not observed model
defects or proof that a prompt caused them. Injection is removed before proposal
generation and every evaluation call.

| Plugins | Project seconds | Proposal seconds | Incumbent / candidate correct | Improvement gate |
|---|---:|---:|---:|---|
| Absent | 39.216 | 42.931 | 4/4 / 4/4 | Failed: zero improvements |
| Normal memory | 43.249 | 50.131 | 4/4 / 4/4 | Failed: zero improvements |

All warm classification calls took approximately 177–1,801ms. No candidate was automatically
selected, and explicit adoption was refused for both failed results. Task
acceptance remained unresolved, records settled and owned runtimes stopped.
**The loop smoke passed; the candidates did not pass their improvement gates.**
Controlled provider fixtures exercise the successful adoption/rollback/replay
path independently; they are protocol tests, not model-quality measurements.

Fixed before the first run: project <=45s, proposal <=60s; paired evaluation
requires at least one improvement, every critical case correct, no regressions,
candidate latency <=1.2x incumbent total and <=2s per case. The two fixed critical
cases and two public reserved cases are withheld from proposal input, but the
public suite is not genuine held-out generalization evidence. No gate was relaxed.

The [initial report](supervised-improvement-initial.json) failed: plugin-free
proposal generation violated its schema, and the memory-enabled project attempt
ended incomplete. The [diagnostic repeat](supervised-improvement-diagnostic.json)
completed both project tasks but both proposals lacked all three required
top-level fields. The generator instructions then named the exact response
object, field types and bounds. The [first complete run](supervised-improvement.json) produced valid three-field
objects and reached paired evaluation. All failed reports remain retained.

A final-source repeat followed a scoped provider-error display fix. Its
[initial attempt](supervised-improvement-final-initial.json) failed both project
journeys before proposal generation (one incomplete result, one timeout). The
unchanged repeat passed both full journeys, as shown above. Its proposals were
considerably slower than the first complete run (2.395/2.356 seconds), while
remaining within the same fixed gates. This reinforces the runtime reliability
limitation; a passing repeat does not erase failed attempts.

The driver verifies the pinned Qwen3-8B Q4_K_M asset hash and runs in the existing
offline container: pinned llama.cpp b9603 image, loopback only, <=4 GiB RAM,
zero swap, <=4 CPUs, RTX 5070 CUDA. Peak container RAM was 1,721,798,656 bytes;
this is not a GPU memory cap. Repository, weights, runtime and normal memory
vault are mounted read-only. Temporary sessions and retrieved private prose are
removed; retained reports contain metadata only. No memory writes occurred.
Prior runtime variability remains relevant; this run does not establish stable
cold-start latency under external GPU activity.

Run with the same container mounts documented for the
[local workflow](../../local-assistant.md):

```sh
python -B -m scripts.qualify_improvement \
  --memory-root /home/fearsidhe/.claude/plugins/memory \
  --model-file /models/8b.gguf --output /reports/supervised-improvement.json
```

The final report records SHA-256 hashes for every core Python module and the
qualification drivers. These match the final core and driver source. Earlier reports retain their
original hashes, including the first complete run before the error-display fix.

## Repository validation

**Final validation:** [1,495 passed, seven skipped, six warnings in 368.40s](repository-final.txt),
with `TERM=xterm-256color`. Locked offline sync, Ruff, whitespace checks,
sdist/wheel build and a clean Python 3.13 wheel smoke (67 modules) passed.
The wheel's core bytes match the final source. The existing six missing
Anthropic/Ollama fixtures and one opt-in Antigravity live test remain skipped.
Targeted tests cover the terminal loop, exact selection/replay/rollback, failed
and stale results, publication failures, interrupted experiments, repaired durable
results, drift suspension, explicit overrides, permissions/budgets, readonly
inspection, plugin command collisions, cancellation and new-work priority.

The initial full suite ran under this tool shell's `TERM=dumb`: 1,493 passed,
seven skipped, and the existing complex-inline-math width test failed. The
[failure log](repository-initial.txt) is retained. The same failure reproduced
from a clean archive of merged main ([baseline evidence](main-math-terminal-dumb.txt)).
Rich returns a fixed width of 80 for a dumb terminal despite the test constructing
`Console(width=78)`. The unchanged test passes with `TERM=xterm-256color`.
The full-suite repeat uses that normal terminal setting; no math source, test,
dependency or assertion was changed.
