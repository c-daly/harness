# Public context-selection profile comparison

PR36 merged at `bfe76e3`. The previous 8B prompt and schema experiments failed
their fixed development gates. This experiment compares the two installed model
profiles with the unchanged builtin prompt and core eligibility filtering.

The [protocol](protocol.json) freezes inputs, labels, profiles, run order, limits,
and gates before inference. Run order is 8B, 4B, 4B, 8B. Each block gets a fresh
isolated runtime/session, one unscored warmup, and all six public cases. Both
paired repetitions must pass every correctness and latency gate. All case data
remains public development data, including the case labelled `held_out`.

The [driver](../../../scripts/measure_context_profile.py) verifies pinned weights,
requires bounded offline execution, preserves exact commands and source hashes,
and refuses an existing output directory. It records real core observations and
raw model events, retaining scoped input/prompt blobs. Its comparison function
regrades responses and refuses mismatched profiles, inputs, prompts or sources.
It cannot adopt a model or change a user session.

The smaller model uses full GPU offload; 8B keeps the previously measured
28-layer placement. This compares usable profiles on a shared host. It does not
isolate model size, training version, placement or cache effects. No fresh
confirmation data will be authored during this comparison, and M4 stays open.

The protocol and executed driver were committed at `faab073` before inference.
The core source is unchanged from merged `bfe76e3`.

## Results

**Both profiles scored 5/6 in both repetitions. The 4B candidate failed the
development gate twice: no correctness improvement and a remaining critical
ambiguity failure.** Every run selected the deployment policy for “Find its
policy,” where the frozen oracle requires an uncertain response. No settings
were tuned, no failed block was retried, and no fresh confirmation cases were used.

| Block, in execution order | Correct | Model answers correct | Core answers correct | Maximum scored latency |
|---|---:|---:|---:|---:|
| [8B baseline 1](baseline-1/report.json) | 5/6 | 4/5 | 1/1 | 1,544 ms |
| [4B candidate 1](candidate-1/report.json) | 5/6 | 4/5 | 1/1 | 818 ms |
| [4B candidate 2](candidate-2/report.json) | 5/6 | 4/5 | 1/1 | 349 ms |
| [8B baseline 2](baseline-2/report.json) | 5/6 | 4/5 | 1/1 | 1,178 ms |

The lexical baseline scored 4/6. Neither candidate repetition regressed a scored
case, and both passed the unchanged 2,000 ms and 1.2 latency gates. Their total
scored latency ratios against the corresponding 8B blocks were **0.331** and
**0.267**. These small sequential measurements support retaining 4B as a faster
development option on this host; they do not qualify accuracy or establish a
causal size/speed relationship.

The first 4B **unscored warmup timed out**, recording 5,626 ms including overhead.
The second completed in 2,435 ms. 8B warmups completed in 4,385 and 4,102 ms.
Warmups were excluded from scoring before execution, and are retained in full.
Runtime startup took 14.96, 7.54, 2.70, and 7.49 seconds in block order. These
observations include host-cache effects and do not establish cold-start or
first-interaction readiness. The scoped calls attempted 24 model inferences
(20 scored, four warmups); 23 completed and one timed out. Four additional scored
answers came from core eligibility without inference.

All four blocks completed mechanics checks and stopped their owned runtimes.
The [unrounded summary](summary.json), reports, original inference event extracts,
and referenced input/prompt blobs preserve both successful and failed outcomes.
The full local session records remain at `/tmp/harness-context-profiles` on the
measurement host; the committed extracts omit only resource polling.

## Validation and next work

[Validation](validation.json) regraded all 24 scored observations, matched all
28 observations including warmups to their original events, parsed **224 event
envelopes**, checked **32 input/prompt blobs** and **312 executed source hashes**,
and verified all four reports against the frozen protocol. The [manifest](manifest.json)
binds the retained artifacts. Focused checks passed **29 tests on Python 3.13**
and **29 on Python 3.12**, including rejection of incomplete evidence, mismatched
inputs/settings, new regressions, critical failures, and latency failures. Ruff
and whitespace checks passed. Core source and dependencies match merged main;
no new local full-suite, packaging, or UI qualification is claimed.

Keep the current user profiles and advisory selector. Faster execution of the
same incorrect decision is not the missing M4 quality evidence. A future semantic
experiment needs a distinct, frozen hypothesis about ambiguity handling or a
different model, followed by fresh confirmation only if development passes.
The independent remaining M4 work is broader live handoff/failure qualification;
advance that track while keeping semantic quality visibly open. M5 heterogeneous
work/source-edit improvement and M6 daily use remain pending.

## Reproduction

On the provisioned host, with the pinned image, weights, and Python environment
already installed, run each block below in a new output directory. Follow the
protocol order and stop if a block exits nonzero. Exit zero means mechanics
completed; it does not mean the profile passed development.

```sh
profile_reports=$(mktemp -d /tmp/harness-context-profiles.XXXXXX)
profile=baseline-8b
attempt=baseline-1
weights=/home/fearsidhe/projects/harness/.local-runtime/Qwen3-8B-Q4_K_M.gguf
timeout --signal=TERM --kill-after=10s 240s docker run --rm --pull never \
  --user 1000:1000 --network none --memory 4g --memory-swap 4g --cpus 4 --gpus all \
  --env LITELLM_LOCAL_MODEL_COST_MAP=True --env LD_LIBRARY_PATH=/app \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --mount type=bind,src=/home/fearsidhe/projects/harness,dst=/home/fearsidhe/projects/harness,readonly \
  --mount type=bind,src=/home/fearsidhe/.local/share/uv/python,dst=/home/fearsidhe/.local/share/uv/python,readonly \
  --mount "type=bind,src=$weights,dst=/models/model.gguf,readonly" \
  --mount "type=bind,src=$profile_reports,dst=/reports" \
  --workdir /home/fearsidhe/projects/harness \
  --entrypoint /home/fearsidhe/projects/harness/.venv/bin/python \
  sha256:841b199aed2649a748875b043b32fed2e8c2d4d87e1d563556817fb7fa44b72b \
  -m scripts.measure_context_profile --profile "$profile" \
  --model-file /models/model.gguf --output "/reports/$attempt"
```

For the two 4B blocks, set `profile=candidate-4b`, use the installed
`Qwen3-4B-Instruct-2507-Q4_K_M.gguf`, and set `attempt=candidate-1` then
`attempt=candidate-2`. Finish with the 8B settings and `attempt=baseline-2`.
The full session logs remain under each output's `state/`; the compact extracts
omit only repetitive resource polling and retain original event sequence numbers.

Regrade each pair using `scripts.measure_context_profile.compare(baseline, candidate)`
with the parsed report dictionaries. Both pair results must pass. This reuses
public cases and can never become fresh confirmation evidence.
