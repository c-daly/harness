# Real local model smoke check

This opt-in check exercises Harness against a provisioned model inside an offline
Linux container. It advances the M3 feasibility gate. It is a small public fixture
run, not evidence for automatic semantic control, prompt promotion, or general
agent reliability. `memory` remains an external plugin; the core also runs without it.

The initial core configuration change was optional `local.cwd`, needed by the cached
llama.cpp image to locate its shared library. The driver selects an explicit local
alias; it does not install defaults or implement automatic provider fallback.
The subsequent [tool-planning slice](local-tool-planning.md) adds an opt-in
single-response limit and a core-journal context experiment. The original report
below binds the driver/core at PR #14's `20c36ad`; it remains historical evidence.
The later [bounded correction slice](local-tool-recovery.md) extends the driver
with explicit correction attempts and the combined normal-memory TUI journey.
[Response-profile experiments](response-profiles.md) add explicit output/sampling
settings and a separately provisioned, pinned 8B comparison fixture. The original
4B remains the driver's default, and earlier reports remain historical evidence.

**September 6 result: this profile fails the complete smoke gate.** The
[final three-run report](handoffs/2026-09-06-core-agency/local-qualification.json)
records 3/3 successful no-plugin file tasks and 3/3 successful terminal journeys,
but 0/3 successful memory-assisted artifacts: two wrong JSON objects and one
malformed JSON file. Successful execution never overrode the artifact oracle.
This report is intentionally retained as a failure; the command exited 1.

## Provisioned profile

- Model: [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507),
  non-thinking, using the [Unsloth Q4_K_M quantization](https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF).
- Quantization repository revision: `a06e946bb6b655725eafa393f4a9745d460374c9`.
- File: `Qwen3-4B-Instruct-2507-Q4_K_M.gguf`, **2,497,281,120 bytes**.
- SHA-256: `3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597`.
- Cached `ghcr.io/ggml-org/llama.cpp:server-cuda` image:
  `sha256:841b199aed2649a748875b043b32fed2e8c2d4d87e1d563556817fb7fa44b72b`.
- Binary: llama.cpp **9603 (`ba1df050f`)**, with working directory `/app`.
- Request/runtime profile: 8,192 context tokens, one parallel slot, four CPU
  threads, full GPU offload, flash attention, Jinja tool templates, automatic
  fitting off, and host prompt cache disabled. The exact argv is in the report.

Native tasks use the runtime's default sampling settings; they are not claimed
deterministic. Semantic calls explicitly use temperature zero. Each full journey
runs three times by default (`--runs 1` is available for diagnostics).

Provision the [pinned model file](https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/a06e946bb6b655725eafa393f4a9745d460374c9/Qwen3-4B-Instruct-2507-Q4_K_M.gguf)
and runtime separately. `.local-runtime/` is ignored for weights and scratch reports.
On this machine `/tmp` is a small RAM filesystem: store weights on the project disk.
The driver verifies the full file hash before starting a model. It never downloads
anything; Docker uses `--pull never`. The image follows the upstream
[Docker](https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md) and
[server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) contracts.

## Reproduce on this Linux/WSL CUDA setup

Use the repository's preinstalled `.venv` and its existing uv-managed Python.
The paths below describe the measured setup; adjust the Python/plugin locations
for another installation. Run from the repository root, with the model already
in `.local-runtime/`. A working Docker NVIDIA runtime is required.

```sh
qualification_repo="$PWD"
qualification_python_root="$HOME/.local/share/uv/python"
qualification_memory_root="$HOME/.claude/plugins/memory"
qualification_vault="$HOME/projects/vault"
mkdir -p .local-runtime/reports
docker run --rm --pull never --name "harness-qualification-$$" \
  --user "$(id -u):$(id -g)" \
  --network none --memory 4g --memory-swap 4g --cpus 4 --gpus all \
  --env LITELLM_LOCAL_MODEL_COST_MAP=True --env MEMORY_VAULT_DIR=/vault \
  --mount "type=bind,src=$qualification_repo,dst=$qualification_repo,readonly" \
  --mount "type=bind,src=$qualification_python_root,dst=$qualification_python_root,readonly" \
  --mount "type=bind,src=$qualification_repo/.local-runtime/Qwen3-4B-Instruct-2507-Q4_K_M.gguf,dst=/models/local.gguf,readonly" \
  --mount "type=bind,src=$qualification_memory_root,dst=$qualification_memory_root,readonly" \
  --mount "type=bind,src=$qualification_vault,dst=/vault,readonly" \
  --mount "type=bind,src=$qualification_repo/.local-runtime/reports,dst=/reports" \
  --workdir "$qualification_repo" --entrypoint "$qualification_repo/.venv/bin/python" \
  sha256:841b199aed2649a748875b043b32fed2e8c2d4d87e1d563556817fb7fa44b72b \
  -B scripts/qualify_local.py --memory-root "$qualification_memory_root" \
  --output /reports/local-qualification.json
```

For a machine without the memory plugin, omit its two mounts, `MEMORY_VAULT_DIR`,
and `--memory-root`. That run reports only the no-plugin cases. The normal-memory
case calls the installed MCP server with `memory_list(subject="harness")`; its
normal vault is mounted read-only. It requires existing entries for that subject.
Temporary sessions and blobs, including retrieved memory, are removed. Reports
retain only counts, checks, timings and hashes, never retrieved memory text.

`LITELLM_LOCAL_MODEL_COST_MAP=True` explicitly selects installed SDK metadata.
Without it, the installed LiteLLM attempts a remote price-map fetch on its first
import, then falls back locally when offline. This recipe avoids that import-time
request; it does not change global SDK behavior in Harness.

## Evidence and interpretation

The driver refuses a worker with any interface other than loopback, unlimited RAM
or CPU, more than four CPUs / 4 GiB RAM, or nonzero swap allowance. Docker provides
the actual isolation. The RAM ceiling does **not** cap GPU memory. The report's
cgroup peak is neither total model memory nor VRAM use; shared file cache may have
been charged outside this container. GPU memory pressure from other applications
is not controlled. Existing user services and installed model aliases are untouched.

Each headless case starts with the local server stopped. A bounded native task
reads a synthetic project record and writes an exact JSON artifact. Passing
requires successful read/write tool events and independent artifact equality;
the agent result still says acceptance is unverified. The memory case additionally
requires a successful, nonempty normal-memory listing. Three public classification
examples run three times per case in shadow mode. They are smoke examples, not a
held-out dataset or a statistically meaningful latency distribution.

The Textual pilot checks the final terminal compositor, a composer available before
runtime launch, an actual project answer, Escape during real streamed inference,
draft retention, `/clear` then `/resume` through the session picker, another real
answer, and composer/recovery availability after a deliberately missing model file.
Input events drive the normal handlers. The streaming cancellation input skips
the pilot's screen-idle barrier so Escape arrives before generation finishes.
This is automated terminal evidence, not a human usability session. UI mounting
is timed within the already-running Python worker; it excludes Python imports,
Docker startup, and provisioning. Stopped-server start measurements retain the
host OS file cache; they are not cold-disk or post-reboot measurements.

Declared limits are 30 seconds for runtime readiness, 45 seconds for the bounded
file task, 2 seconds for each warm semantic example, 2 seconds for cancellation,
and 3 seconds for UI mount. A failed check exits nonzero and remains in the report.
The report binds the driver and core Python source hashes, weights, SDK versions,
runtime version, exact catalog profile, and observed cgroup limits.

Full M3 still requires the combined terminal journey with normal memory, human
dogfooding, restart/crash recovery and broader workload/resource measurements.
Automatic local fallback, context/progress classifiers, held-out evaluation,
candidate generation, adoption, and rollback remain separate roadmap work.

## Findings and next experiment

The measured host has an AMD Ryzen 7 8700F and an NVIDIA RTX 5070 with 12,227 MiB
VRAM under WSL2. The profile successfully owns and stops real llama.cpp processes
with external networking unavailable. The normal memory server returned 13,802
bytes of scoped entries in the graded memory cases; transport availability did
not make the resulting artifacts correct. The malformed-artifact case exits that
case before semantic grading; sample totals must come from the report.

The memory-assisted tasks made two model calls and dispatched read, write, and
memory-list tools. The ordinary no-plugin tasks took three calls. This suggests
the model combined dependent work into one tool batch, supplying write arguments
before observing the source read. Treat that as a diagnosis to test: the report
retains tool order/counts, not full private transcripts or per-response batches.
The next experiment should record batch boundaries and compare grounded
read-then-write instructions or an appropriate model profile against this fixed
artifact oracle. Do not turn on automatic fallback or relax the oracle to fit
this model. Memory retrieval and memory-assisted task reliability are separate
measurements.

Earlier diagnostic failures were preserved during development: the first owned
launch lacked `/app` as its working directory and failed to load a shared library;
the driver initially flushed MCP events before session start and waited for
terminal screen idleness before attempting cancellation. Those driver mistakes
were corrected before the final report. The later wrong-artifact failures are
real-model results, not those driver failures.
