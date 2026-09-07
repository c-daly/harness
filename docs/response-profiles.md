# Conversation response profiles

Core context profiles can now configure conversation generation explicitly:

```toml
# Add this section to an existing --context-profile file.
[response]
max_output_tokens = 512
max_output_bytes = 4096
instructions = "In the final answer, give the requested facts briefly. Do not quote retrieved memory unless its contents were explicitly requested."
# Optional, model-specific sampling setting:
# temperature = 0.7
```

Every field is optional. Omitting the section preserves existing behavior.
The settings persist in `ContextPolicyConfigured`, survive headless and TUI
resume, and appear in `/context`. They do not activate a model, alter permission
rules, or depend on either memory or agent-swarm.

Output limits narrow the task's existing limits before the run is recorded.
The dispatcher also applies them to conversation and agent-task requests, so a
wider request cannot bypass the configured limit. Bytes include reasoning, tool
arguments and stream metadata. The token limit is sent to inference adapters
and checked when usage is reported; the byte bound is enforced locally. A provider
cutoff remains incomplete, and an oversized or malformed response fails. Harness
does not truncate a failed response and label the task successful.

An explicit temperature overrides conversation sampling requests. It requires
an inference adapter; external agents are refused before execution rather than
silently ignoring the setting. Guidance and local output checks remain usable
with external agents when temperature is omitted. Their existing limits on
reported usage and adapter-visible streams still apply.

The native task loop adds guidance as pinned system context, measured against
the input budget. It does not mutate canonical conversation messages or append
duplicate guidance on resume. Low-level inference callers supply their own
messages; conversation generation bounds still apply at dispatch. Semantic
functions and explicit compaction retain their separate requests and settings.
Instructions are advisory: they cannot establish artifact correctness, tool
execution, or acceptance. `AgentResult.acceptance` remains `unverified`.

## Local experiments

The prior recovery profile produced a resumed answer containing the right facts
but about 14 KB of text, leaving the project name outside the visible viewport.
This slice tests response configuration against both artifact correctness and
the same final-viewport checks. The checks have not been relaxed to accept shorter
but incorrect answers, stale memory, or omitted file operations.

The [saved pilot reports](handoffs/2026-09-06-core-agency/local-response-pilots.json)
contain three one-repeat feasibility probes that failed the complete four-journey gate:

| Model and response candidate | No-plugin file | Memory file | No-plugin TUI | Memory TUI |
|---|---|---|---|---|
| Existing 4B, temperature zero, longer guidance, output bounds | Fail | Fail | Fail | Pass |
| Official 8B, configured non-thinking sampling, longer guidance, output bounds | Fail | Fail | Pass | Fail |
| Existing 4B, unchanged sampling, shorter guidance, output bounds | Fail | Pass | Pass | Fail |

The first two candidates omitted requested file work. The conservative candidate
wrote malformed JSON in its no-plugin task. The latter two memory TUI probes
displayed short answers but omitted the required fresh memory access after resume.
These are measured failure observations, not qualified replacement profiles.
Pilot reports bind the driver versions used at that time; the first two precede
the final driver's model metadata additions.

The 8B experiment uses the
[official Qwen3-8B GGUF](https://huggingface.co/Qwen/Qwen3-8B-GGUF), pinned at
`7c41481f57cb95916b40956ab2f0b139b296d974`, file `Qwen3-8B-Q4_K_M.gguf`,
5,027,783,488 bytes, SHA-256
`d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`.
The runtime profile requests `enable_thinking=false`, temperature 0.7, top-p 0.8,
top-k 20, min-p 0 and presence penalty 1.5, following that model card's
non-thinking configuration. Both models use the previously pinned llama.cpp
image and 8,192-token context. The 8B asset is provisioned separately, stays
under ignored `.local-runtime/`, and does not change the user's configured model.

The formal response experiment keeps the original 4B sampling and compares the
recovery profile alone with the
[conservative response settings](handoffs/2026-09-06-core-agency/concise-response.toml).
Its fixed plan contains 18 paired cases: original harbor/3 regression and reserved
maple-ν/23 source facts, with/without memory, repeated three times (12 file cases),
plus no-plugin and normal-memory TUI journeys repeated three times (six UI cases).
All cases are critical, pair order alternates, and the candidate must improve at
least two cases without a failure. Per-case 60-second and 2x aggregate latency
gates remain. The whole-run deadline is 600 seconds for this expanded suite.
Core evidence, candidate, plan, run and result records feed the adoption gate; no activation occurs even if evaluation passes.

The [completed paired result](handoffs/2026-09-06-core-agency/local-response-evaluation.json)
rejected the candidate: **incumbent 14/18, candidate 9/18**, with six regressions
and one improvement. File cases fell from 10/12 to 5/12; both arms passed 4/6 TUI
journeys, with different failures. Two incumbent memory answers repeated about
14 KB and hid the project name. The candidate fixed one of those cases but also
produced an answer without the project and a failed memory-resume response.
Missing files, wrong facts, malformed JSON and invalid tool arguments remain
failures. No profile was activated. The core audit session is
`b5324774b0e84388be1bf8ba9b79f70e`, under ignored
`.local-runtime/reports/response-audit`.

**Resource limitation:** the longer paired run emitted unclosed
`aiohttp.ClientSession` warnings. The current checks establish model-process and
MCP-plugin teardown, not complete HTTP transport cleanup. Inspection of the
installed LiteLLM 1.88.1 shows cached clients whose keys include request timeouts;
Harness supplies a varying remaining deadline. That explains a potential source
of client growth, but a dedicated lifecycle reproduction and fix remain pending.
A subsequent [client-ownership fix](inference-client-lifecycle.md) addresses
explicit OpenAI-compatible endpoints and adds a real-model lifecycle probe.
Closing the global SDK cache during concurrent calls would be unsafe. The report
retains the original checks and source hashes; these warnings are an additional
qualification gap, not a reason to reinterpret the failed quality result.

Use the [offline container recipe](local-model-qualification.md) with these final
Python arguments:

```sh
-B -m scripts.evaluate_local_planning \
  --response-profile docs/handoffs/2026-09-06-core-agency/concise-response.toml \
  --memory-root "$qualification_memory_root" \
  --audit-dir /reports/response-audit --output /reports/response-evaluation.json
```

For a standalone journey, pass the same `--response-profile` to
`scripts/qualify_local.py` with `--single-tool --tool-recovery-attempts 2`.
`--model-profile qwen3-8b` selects the separate pinned 8B fixture and verifies its
size/hash; mount that model at `/models/local.gguf`. The default fixture remains
the original 4B. Neither driver downloads models or changes global settings.
The TUI report now separates resumed-answer status, byte count, fact presence
and visibility, retaining only hashes and metadata rather than private text.

More model tuning alone does not establish task completion. The next gap is
checking requested work against execution/artifact evidence and presenting unmet
requirements clearly, while keeping model assessments advisory until evaluated.
