# The first local core assistant (M3)

The M3 profile uses the preinstalled Qwen3-8B Q4_K_M model through a Harness-owned
llama.cpp process. It provides project-record context, bounded file work, durable
tasks, normal-memory retrieval, cancellation and continuation in the existing
terminal interface. Memory remains a separately installed plugin. No plugin is
required for the project-only workflow.

## Ordinary `/model local` sessions

The qualification launcher uses its own catalog. It does **not** install its
runtime profile into `~/.config/harness/models.toml`. `/model local` reads that
normal catalog; an alias with only a route and `api_base` expects a server that
someone has already started. Tags such as `local` and `verified = true` do not
start a process or establish current availability.

To use on-demand startup in an ordinary session, provision a native llama.cpp
server and its GPU libraries, then use
[`/models add`](model-management.md) to register the installed GGUF under a new
alias. For an existing alias, edit its `local` runtime profile in the normal
catalog. Use the actual model ID, absolute binary/weight
paths, and a working directory in the
[owned-process example](local-runtime-readiness.md#harness-owned-process).
Each distinct model needs its own model file and matching server ID; renaming
an alias does not load different weights. Profiles in the same resource group
share the owned-process limit and unload an idle model when another needs it.

After saving the catalog, `/model local` reloads it. The next message starts the
configured model; `/resources` shows startup/readiness and `/resources stop local`
can stop a runtime owned by this session. A long cloud-model history or large
plugin tool inventory may still need an explicit
[context profile](context-profiles.md); local setup does not silently discard
conversation history or tools.

The [normal-session repair evidence](handoffs/2026-09-08-normal-local-session/README.md)
records the missing-startup configuration found during actual use. The original
M3 workflow below did not test this installation path.

## Use the measured profile

On the provisioned Linux/WSL CUDA setup, from this repository:

```sh
scripts/run_local_assistant.sh /path/to/project
# With the separately installed normal memory plugin and vault:
scripts/run_local_assistant.sh /path/to/project --memory
# Resume the most recent session for that project:
scripts/run_local_assistant.sh /path/to/project --memory --continue
```

The launcher starts the UI inside the pinned offline container. The model starts
on first inference, under core process ownership. The container has only loopback
networking, four CPUs, 4 GiB RAM and no swap. It uses the repository's preinstalled
`.venv`, uv-managed Python, Docker NVIDIA support, and the already downloaded
`.local-runtime/Qwen3-8B-Q4_K_M.gguf`. It never installs assets or changes your
catalog or user-managed server. Missing weights leave the interface available;
the first attempted inference explains the unavailable resource.

The example reads `PROJECT.md` before each root attempt. For example:

```markdown
# Project record

project: harbor
retry_limit: 3
```

For a real project, edit a copy of the [project context profile](examples/local-resident/project.toml)
or [normal-memory profile](examples/local-resident/memory.toml) to query its own
brief or project record. Pass it as `--context-profile /project/local-context.toml`;
`/project` is the mounted workspace inside the container. Sources, result caps,
and exact tool inventory are explicit. The memory example queries the normal
plugin's `memory_list(subject="harness")`, capped at 16 KiB; change the subject for
another project. The source is required in this profile, so missing memory stops
that attempt with a clear source failure. It does not silently invent continuity.

In the interface:

```text
/task new Extract the current project settings
/task require Review RESULT.json
Using the project record, write RESULT.json with the project and retry_limit values. Confirm the values.
/status
```

Escape interrupts loading, context retrieval or generation. Draft text stays in
the composer and follow-ups pause. `/queue` lists them; edit, remove, clear and
resume remain available after failure. Explicit `/queue pause` also works with
an empty queue and persists when another prompt is submitted. Task obligations
survive restart, and a resumed attempt retrieves current records again. Model
completion does not accept the task; inspect the result and use the existing
[task review controls](task-evidence.md).

The launcher explicitly allows file writes within the selected workspace and
memory listing when enabled. Other tools are excluded by the context profile;
normal dispatch and workspace enforcement still apply. Session logs and context
blobs persist under `PROJECT/.local-runtime/sessions` by default. Set
`HARNESS_LOCAL_STATE` to select another store. The queue and unsent draft remain
memory-only and do not survive process death. Memory roots can be overridden with
`HARNESS_MEMORY_ROOT` and `HARNESS_MEMORY_VAULT`; the vault is mounted read-only.
`HARNESS_PYTHON_ROOT` selects an alternative preinstalled uv Python root.

The launcher inherits the saved context policy with `--continue` or `--resume`;
an explicit `--context-profile` still overrides it.

The same [model catalog](examples/local-resident/models-8b.toml) and context files
work with the ordinary `harness` CLI after adjusting runtime paths for your
installation. The container launcher is a reproducible setup for the measured
host, not a new provider API or a replacement interface.

## Measured result

The [final matching-source report](handoffs/2026-09-07-local-assistant/m3-qualification-release.json)
passes all six journeys and all four fault cases: twelve exact file writes and
six real cancellations/restarts. On the RTX 5070 (12,227 MiB), cold readiness
was 7.36–14.35 seconds, warm resumed writes 2.01–2.75 seconds, cancellation
0.122–0.126 seconds, and UI mount below 0.83 seconds. This final run overlapped
the full repository tests. The earlier passing run was faster at cold startup;
these measurements retain that contention rather than assuming idle hardware.
Peak observed device use was 10,212 MiB across applications. The cgroup was
configured for 4 GiB with no swap; its reported peak was 4,295,176,192 bytes.
No per-process VRAM or CPU-only qualification is implied.

Repository verification: **1,347 passed, 7 skipped, 6 warnings in 305.65 seconds**;
locked offline sync, Ruff, build and the fresh-wheel 62-module smoke passed.
The skips are three missing Anthropic fixtures, three missing Ollama fixtures,
and the opt-in live Antigravity integration. These do not certify those adapters.

## Fixed qualification and evidence

The opt-in `scripts/qualify_m3.py` gate uses the CLI's default system prompt and
the shipped profiles. Three varied project records run with plugins absent and
with the actual memory plugin and normal vault. Each journey independently:

1. Starts with the local process stopped and mounts an editable UI before loading.
2. Creates a durable task and writes an independently checked JSON artifact.
3. Cancels real streaming inference, retains a draft and pauses a queued follow-up.
4. Stops the owned process, opens a fresh kernel on the same session, and changes
   the source record while stopped.
5. Answers from the changed record and performs another independently checked
   file update, with fresh memory retrieval and visible status.

Version 2 additionally exercises missing model assets and a real llama.cpp process
that exits during startup, in both plugin modes. Failed task records, queue editing
and clearing, draft input, and resumption without model-generated answers must work.
The four fault cases cannot pass just because an exception was caught.

Every check is critical: exact typed artifact equality, successful native writes,
factual answers, final-compositor visibility, settled intents, fresh source
retrieval, persisted task/profile, and actual owned-process exit. Limits remain
45 seconds per task, 30 seconds from owned start to readiness, three seconds to
mount the interface, and two seconds to cancel. The profile supplies at most
32 KiB input and 512 output tokens, with six native iterations in the gate.
The full gate has a 900-second deadline. These are bounded workflow checks, not
a statistical reliability claim or daily-use qualification.

Reports preserve source hashes, profile digests, runtime/model identity, observed
8,192-token context, timings, cgroup ceilings and GPU observations. Private memory
text and model prose are not exported; temporary evaluation sessions are deleted.
Device memory observations include other applications and are not a per-process
VRAM cap. CPU-only operation and structured-output mode are unqualified here.
The catalog remains `verified = false`: passing this task profile does not
establish universal adapter conformance or automatic fallback eligibility.

The earlier 4B resident failure was partly a Harness context defect: the model
tried to read the source label `project-facts` as a filename. Core now supplies the
effective tool/arguments that retrieved the data and explains that labels are not
paths. Rewrite/canonicalization provenance, argument mutation, denial and redaction
have regression coverage. The same-model diagnosis improved from 0/3 to 3/3 exact
writes after that fix. The broader 4B journeys still missed writes, including
printing JSON instead of changing the file. Both failed profile runs are retained.
The 8B profile is selected on the broader gate, including warm writes and cold
restart latency, rather than on inventory health or the narrow diagnostic.

See the [preserved reports](handoffs/2026-09-07-local-assistant/) for the complete
sequence. The 4B diagnostic used the historical resident pilot's system prompt;
full qualification uses the ordinary CLI prompt and shipped profiles. Version 1
measured missing assets; version 2 tightens numeric grading, verifies child exit,
adds startup-exit faults, measures GPU/RAM, and uses only the current process's
startup timestamps after resume. Earlier failures are not relabeled as passes.

## Reproduce

Use the pinned Docker recipe in [local qualification](local-model-qualification.md#reproduce-on-this-linuxwsl-cuda-setup).
Mount the preinstalled `Qwen3-8B-Q4_K_M.gguf` at `/models/8b.gguf` instead of the
4B file, and replace the Python invocation with:

```sh
-B -m scripts.qualify_m3 --model-profile qwen3-8b --model-file /models/8b.gguf \
  --memory-root "$qualification_memory_root" --output /reports/m3-qualification.json
```

The driver verifies the pinned full weight hash before evaluation. The exact
image, weight revision, server arguments and source hashes are in every report.
It requires the actual normal memory plugin and vault with preinstalled
dependencies, not a fixture replacement. It leaves external services untouched.

M4 adds bounded semantic decisions and resource-aware fallback. M5 adds
heterogeneous supervision and improvement activation/rollback. M6 qualifies
broader daily use, crash/fault behavior and advertised provider support. M3 does
not automatically select a cloud provider or adopt an improvement candidate.
