# Normal local-session repair

The user reported that `/model local` and other local aliases failed every turn.
Their normal session recorded `NetworkFailed` for both `local-instruct` and
`local`; direct loopback probes found no server listening on port 8080. All
three local aliases pointed there, and none had a `local` readiness/startup
profile. The separate qualification launcher never installed its configuration
into the normal user catalog. M3's passing isolated workflows did not establish
that ordinary `/model local` use worked on this installation.

## Repair on the provisioned host

- Extracted the existing pinned llama.cpp b9603 binary and its four CUDA
  dependencies into the ignored `.local-runtime/llama-b9603` directory. The
  source image remains
  `sha256:841b199aed2649a748875b043b32fed2e8c2d4d87e1d563556817fb7fa44b72b`.
  The native executable reports `9603 (ba1df050f)` and detects the RTX 5070.
- Configured `local` to serve the existing Qwen3-8B Q4_K_M file on loopback
  port 8080 and `local-instruct` to serve the existing Qwen3-4B-Instruct-2507
  Q4_K_M file on port 8081. Both have explicit on-demand startup, absolute
  paths, a shared resource group, 16,384-token context, four compute threads,
  CUDA offload, and disabled thinking. Both remain `verified = false`.
- Tested a candidate copy before replacing those two entries in the normal
  user catalog. Preserved a timestamped backup and all other model entries.
  This machine configuration and the extracted binary are not shipped assets.
- Did not repoint `local36` to a different model. Its old external-server
  configuration remains unavailable in this check. No larger weights were
  downloaded and no fine-tuning was performed.

This is native host execution, not the earlier network-disabled, 4-GiB
container qualification. It uses local model files and loopback inference;
the profile's offline environment flags do not establish OS network isolation
or a host RAM/VRAM ceiling.

## Direct evidence

The ordinary CLI, using the candidate catalog and no special context profile,
read a temporary `PROJECT.md` and performed a native write. Independent JSON
comparison verified `{"project": "lantern", "retry_limit": 7}` in `RESULT.json`.
The three model calls took 14.983, 0.506 and 0.805 seconds, respectively, including
cold startup in the first call. The log records successful read/write calls
and owned-process start/stop.

The terminal pilot typed actual `/model` commands into an ordinary `HarnessApp`
with its full 11-tool native inventory. No fake inference provider remained
after switching, and no response/context profile was installed:

| Action | Observed result | Turn time including startup |
|---|---|---:|
| `/model local`, request an exact reply | Real 8B inference; exact reply visible | 14.864 s |
| `/model local-instruct`, request another exact reply | 8B stopped; real 4B inference; exact reply visible | 6.631 s |
| `/model local36`, send a message | Expected connection failure with actionable local-server wording | Not a success |

The [metadata report](tui-report.json) records these checks. The saved final
terminal SVG also contains both successful replies and no `OpenAIException`.
Normal shutdown records the remaining owned runtime stopping. The first pilot
invocation completed its 8B inference but failed in the diagnostic code because
it treated an event's serialized message dictionary as an object; the corrected
pilot then completed the entire sequence. This was a driver error, not an
additional successful end-to-end run.

These checks used native tools with MCP/plugins disabled. They do not qualify
large plugin inventories, long histories carried over from cloud agents,
general 4B/8B task reliability, CPU-only operation, or automatic model setup on
other machines. The earlier failed semantic and self-improvement gates remain
failed. Hugging Face discovery/provisioning, systematic quantization/capacity
comparison and fine-tuning remain backlog work.

## Core diagnostic change

The LiteLLM adapter now describes typed loopback transport failures using the
local server origin and startup guidance. It omits SDK-branded error bodies and
endpoint paths. Remote failures, authentication/server errors, retryability,
fallback types, and the sanitized durable failure event remain unchanged.

The affected provider, inference-client, local-resource, scheduling, fallback,
queue and terminal suite passed **269 tests in 208.62 seconds** before rebasing
onto PR28's merge. Focused adapter/inference checks included **44 tests**.
Coverage includes IPv4,
IPv6, localhost normalization, wrapped SDK failures, timeouts, private error
body omission, remote endpoint classification, and propagation through catalog
inference. Post-rebase verification is recorded in the implementation progress log.
