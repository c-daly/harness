# Local model candidates

These are evaluation options and one measured task profile, not qualified fallbacks.
The user requested that larger models, Unsloth and Hugging Face be retained on
the list while implementation continues with client cleanup and task evidence.

| Candidate | Purpose | Current evidence |
|---|---|---|
| Qwen3-4B-Instruct-2507 Q4_K_M | Small local reference profile | Provisioned; earlier artifact and memory/TUI checks failed. The [context-profile comparison](handoffs/2026-09-09-context-profiles/README.md) scored 5/6 twice, faster than 8B on the scored calls but with the same ambiguity failure and one warmup timeout. Not qualified for automatic context selection. |
| Qwen3-8B Q4_K_M | Initial M3 local task profile | Provisioned; the [M3 offline workflow](local-assistant.md) passes with the current context and response profile. Context selection remains advisory: the latest profile comparison scored 5/6 twice and failed ambiguity. PR17's earlier four-journey feasibility probe remains a historical failure. |
| [Qwen3-14B Q4_K_M](https://huggingface.co/Qwen/Qwen3-14B-GGUF) | Candidate for a larger model with most or all weights on the GPU | Not provisioned or tested here. Published weights are about 9 GB; runtime and context cache need additional memory. |
| Existing Qwen3-Coder 30B and Qwen3.6 35B assets | Revisit hybrid CPU/GPU placement and quantization | Earlier configurations were too slow for synchronous resident decisions; alternative configurations remain unevaluated. |

Tooling options to assess when returning to local capacity work:

- [Unsloth dynamic GGUFs](https://unsloth.ai/blog/dynamic-v2): compare quantization
  choices against the same task-quality and latency gates. Training optimizations
  do not establish equivalent inference-memory savings. Fine-tuning remains a
  separate future experiment, not a prerequisite for current core work.
- [Hugging Face Hub](https://huggingface.co/docs/huggingface_hub/guides/download):
  [core model controls](model-management.md) now inspect public GGUF metadata,
  cache it offline and verify installed files against pinned revisions/hashes
  before registration. A model source is distinct from its inference backend
  and agent runtime. Managed weight downloads and runtime installation remain
  backlog items.
- [llama.cpp capacity controls](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md):
  bounded context, cache precision, GPU placement and CPU expert offload. Verify
  support in the selected runtime version and measure actual working memory.

The 4B/8B fixtures and their CPU/RAM limits are experiment choices, not Harness
architecture limits. Model-loading success is separate from useful task quality.
Do not change user-managed servers, download new weights, enable fallback or
replace the current model merely because an option appears on this list.
