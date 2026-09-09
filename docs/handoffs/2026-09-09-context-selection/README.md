# Context-selection development and held-out gates

PR33 merged at `a4f3678`. This tranche tests whether a more explicit procedure
can fix the resident context selector's public unavailable-context and ambiguity
failures. **Neither development candidate qualified. No confirmation cases were
authored or evaluated, and no prompt was adopted. M4 remains in progress.**

## Core change

Previously, one improvement on a known regression could satisfy a paired plan
even when the candidate got every held-out case wrong. Plans and both supervised
prompt evaluation paths now accept frozen `min_held_out_correct` and
`min_held_out_improved` counts. The common verdict enforces them, so a declared
holdout failure blocks adoption. They appear in the paired report and survive
journal replay. Defaults of zero preserve old records and existing experiments;
operators must declare nonzero thresholds for this stronger claim. Core cannot
attest that operator-labelled cases were actually kept out of development.

The regression tests reproduce a development-only gain that passed the old
gate but fails the new gate, then exercise refusal through both operator-authored
comparison and generated-candidate evaluation/adoption. They also cover legacy
plans, strict count bounds, passing thresholds, and unknown measurements.

## Frozen experiment and results

The [protocol](protocol.json) limited development to two candidate prompts on
the existing five immutable critical cases plus `public-paraphrase`. All six
are public development data, including the case labelled `held_out` by the
example suite. The prompts were operator-authored from earlier public failures;
the local model did not generate them. The intended later confirmation required
32 new synthetic cases, at least 29 correct, three improvements over the paired
incumbent, three more correct than lexical rules, zero critical failures or
regressions, at most 2,000 ms per candidate response, and a total latency ratio
of at most 1.2. Confirmation would run once after freezing the candidate and
then the new inputs/oracles. That stage was not reached.

Each development comparison ran all six pairs, alternating arm order. The
usual fixed critical/no-regression and latency gates were unchanged.

| Candidate | Builtin | Candidate | Rules | Candidate max latency | Total latency ratio | Failure |
|---|---:|---:|---:|---:|---:|---|
| [1: eligibility then ambiguity](candidate-1.json) | 4/6 | 5/6 | 4/6 | 1,571 ms | 1.021 | Still chose a deployment policy for an unresolved “its” |
| [2: explicit ambiguity example](candidate-2.json) | 4/6 | 5/6 | 4/6 | 1,663 ms | 0.944 | Fixed ambiguity but lost the memory-storage paraphrase |

The second candidate passed all five critical cases but failed a previously
passing semantic match. A higher overall score is insufficient when a new
regression remains. No third candidate was tried and no fresh confirmation set
was spent on either failed prompt. These results do not establish useful
self-improvement, generalization, or automatic context injection.

## Runtime and retained evidence

Both scored runs used the installed Qwen3-8B Q4_K_M with llama.cpp b9603,
28 GPU layers, a 4,096-token context, thinking disabled, presence penalty zero,
and semantic temperature zero. The network-disabled container had at most four
CPUs, 4 GiB host RAM, and no swap. GPU memory is separate; other host workloads
remained running. The profile used a 120-second startup allowance and the
unchanged five-second semantic deadline. No plugins were loaded or user catalog
changed. Every owned runtime stopped.

The first setup attempt used full GPU offload and a 30-second startup allowance.
It timed out before any inference. The shared GPU had roughly 5 GiB free when
the profile was revised, before any scored development response. The retained
retry took 53.7 seconds to become ready; candidate 2's later load took 4.5 seconds.
These are individual startup observations with retained host caches, not a
new cold-start performance qualification. Candidate 1's first seed assessment
timed out; its subsequent paired measurements all completed. There were 24
paired calls and six ordinary seed calls across the two development candidates.
The seeds were deliberately supplied public snapshots, not natural project
failures. No model response was injected.

- [Setup failure](development-1/setup-failure.json) and
  [runtime observations](development-1/runtime-observations.json).
- [Candidate 1 report](development-1-retry/report.json) and
  [actual inference events](development-1-retry/inference-events.jsonl).
- [Candidate 2 report](development-2/report.json) and
  [actual inference events](development-2/inference-events.jsonl).
- Each attempt retains its exact experiment and catalog. The
  [executed development driver](executed-development.py) preserves the trial
  code; [manifest](manifest.json) binds the retained artifacts and core sources.
  Inference event extracts retain original session IDs and sequence numbers;
  repetitive readiness polling is summarized separately.

## Reproduce the public experiment

The [replay driver](../../../scripts/replay_context_selection.py) verifies the
pinned model weights and resource isolation, saves inputs before inference,
refuses an existing output directory, and retains setup failures. It also
checks that a failed candidate is refused by the real adoption control. Even
a passing replay does not adopt a prompt or become fresh confirmation evidence.

On the provisioned host, with the pinned image, model, and Python environment
already installed:

```sh
context_report_dir=$(mktemp -d /tmp/harness-context-replay.XXXXXX)
docker run --rm --pull never --user 1000:1000 \
  --network none --memory 4g --memory-swap 4g --cpus 4 --gpus all \
  --env LITELLM_LOCAL_MODEL_COST_MAP=True --env LD_LIBRARY_PATH=/app \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --mount type=bind,src=/home/fearsidhe/projects/harness,dst=/home/fearsidhe/projects/harness,readonly \
  --mount type=bind,src=/home/fearsidhe/.local/share/uv/python,dst=/home/fearsidhe/.local/share/uv/python,readonly \
  --mount type=bind,src=/home/fearsidhe/projects/harness/.local-runtime/Qwen3-8B-Q4_K_M.gguf,dst=/models/8b.gguf,readonly \
  --mount "type=bind,src=$context_report_dir,dst=/reports" \
  --workdir /home/fearsidhe/projects/harness \
  --entrypoint /home/fearsidhe/projects/harness/.venv/bin/python \
  sha256:841b199aed2649a748875b043b32fed2e8c2d4d87e1d563556817fb7fa44b72b \
  -m scripts.replay_context_selection \
  --candidate docs/handoffs/2026-09-09-context-selection/candidate-2.json \
  --output /reports/candidate-2
```

Exit zero means the replay's mechanics checks passed; read `verdict` for the
candidate's result. Use candidate 1's file and a new output path to replay it.
All these cases are now public and cannot be reused as fresh confirmation.

The [real driver replay](replay-2/report.json) reproduced candidate 2's 5/6
against builtin 4/6 and rules 4/6. All seven mechanics checks passed, including
actual adoption refusal, unchanged task state and builtin selection, and settled
calls. The runtime stopped; the container's peak host memory was 1,796,845,568
bytes. The driver verified the pinned 5,027,783,488-byte weight artifact and
recorded runtime `9603 (ba1df050f)`. Its `weights.runtime_args` describes the
shared helper's default profile; the retained catalog contains the actual
presence-penalty-zero override used in this experiment. This replay validates
the driver and refusal path, adding no new candidate or confirmation evidence.

## Validation

- Full Python 3.13 suite: **1,746 passed, 7 skipped**, six existing MCP
  deprecation warnings, in 412.92 seconds.
- Focused semantic/improvement checks: **103 passed** on Python 3.13 and
  Python 3.12. After adding two additional message-path refusal cases, all
  **eight holdout-gate tests passed** on both versions (3.79 / 4.54 seconds).
- Ruff and whitespace checks passed. Sdist/wheel build and a clean Python 3.13
  wheel install passed, importing 74 modules. All 75 core source files match
  the wheel bytes. Packaging installation used networking; the model trials
  and replay were network-disabled.

## Next experiment

Keep the builtin selection. More instructions repaired one failure while
damaging another, so further prompt wording alone has not earned confidence.
A concrete next hypothesis is to remove ineligible candidates deterministically
before asking the model about relevance, leaving only the uncertain semantic
decision to inference. That would need its own bounded development protocol,
unchanged regression controls, and then new confirmation cases. It is not an
implemented or qualified improvement in this tranche. Normal-memory integration,
broader handoff evidence, and daily use remain separate roadmap work.
