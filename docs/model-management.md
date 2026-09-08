# Set up a local model

Core model controls let you inspect public Hugging Face GGUF repositories and
register an installed model with on-demand llama.cpp startup. Use `/models` in
the terminal or `harness models` from the shell. Memory and agent-swarm remain
plugins; these controls belong to the persistent interface.

## Inspect available files

```text
/models
/models hub Qwen/Qwen3-8B-GGUF
/models hub Qwen/Qwen3-8B-GGUF --revision COMMIT --offline
```

The inventory shows filenames, weight-file sizes, the resolved commit and when
metadata was observed. File size does not include context cache or runtime memory.
Inspection uses the public [Hub model metadata API](https://huggingface.co/docs/hub/api).
It downloads bounded metadata only, never weights or repository code, and does
not read Hugging Face credentials. Private/gated access is not supported.

Successful metadata is cached under `~/.cache/harness/model-index`, indexed by
both the requested revision and the resolved commit. `--offline` or
`HF_HUB_OFFLINE=1` forbids network access for this operation. An unreachable Hub
falls back to matching cached metadata, visibly labelled with its observation
date. Cached metadata can be stale; it is not a current availability check.
HTTP access errors and invalid responses are reported rather than concealed.

Use `--json` for a machine-readable inventory, including sizes and available
LFS SHA-256 digests. The human display limits long inventories to 100 filenames.

## Register installed weights

Install a compatible native `llama-server` executable and a single-file GGUF
first. Harness currently provides neither a runtime installer nor a model
downloader. Then, in the terminal:

```text
/models add my-local --file /absolute/path/model.gguf --runtime /absolute/path/llama-server --port 8082 --context 8192 --gpu-layers 99
/model my-local
```

Quote paths containing spaces. Registration uses the current session's catalog,
or `~/.config/harness/models.toml` when none was configured. It does not switch
the active model or launch the server. Selecting the alias through `/model`
reloads the catalog; the next turn starts its process. `/resources` then shows
readiness, and `/resources stop my-local` stops a process owned by that session.

Options:

| Option | Meaning |
|---|---|
| `--library-path DIR` | Set the runtime's `LD_LIBRARY_PATH`, useful for a native CUDA bundle. |
| `--port PORT` | Loopback server port; default 8080. Choose a free port for another profile. |
| `--context TOKENS` | Runtime context and catalog capacity; default 8192. |
| `--gpu-layers N` | Requested GPU layers; default 99. Use 0 for CPU execution. |
| `--threads N` | Compute and batch threads; default 4. |
| `--disable-thinking` | Pass the explicit chat-template setting for models that support it. |

This generates an OpenAI-compatible inference route, matching server model ID,
absolute paths, required-file checks and a `local` resource group shared with
other local profiles. It does not execute a shell. An idle process owned by the
same Harness session can be replaced under the existing resource scheduler;
externally managed servers are never stopped. Choose runtime options that fit
your hardware; registration cannot establish runtime compatibility or model
quality. Newly registered aliases remain `verified = false`.

To verify installed weights against a specific Hub artifact, append:

```text
--hub OWNER/REPO --revision COMMIT --hub-file model.gguf --offline
```

First inspect that repository online to populate its metadata cache. A supplied
file must match the selected artifact's available SHA-256 and size. The catalog
records the resolved Hub commit, repository and filename alongside the local
path, size, digest, GGUF header version and verification time. Without `--hub`,
registration works entirely offline and records the local digest without a
claim about its origin. These are observations at registration time; Harness
does not make the file immutable or rehash it on every inference.

Verification checks the regular file, supported GGUF v2/v3 header and full
digest. It rejects recognized shard filenames; split-file loading is unsupported.
A valid header alone does not prove that llama.cpp can load the complete model.
Registration checks the executable exists and can execute, without running it.
It neither qualifies hardware fit nor silently changes prompts, tools or history.

The composer stays available during verification and a separate progress line
shows bytes checked. Escape cancels setup when no task or assessment has priority;
clear, resume and shutdown also cancel pending setup. No alias is published if
verification is cancelled. Publication is a short atomic step after verification;
an already completed registration is not undone by a later Escape.

Registration adds new aliases only. Existing entries and comments are preserved,
and an exact backup is saved next to an existing catalog before atomic replacement.
An advisory lock coordinates registrations; a catalog edit detected during
verification aborts publication. Symlink catalogs must be addressed by their
actual path. To undo a new registration, remove its three TOML sections
(`models.ALIAS`, `.local` and `.artifact`) while keeping subsequent catalog edits.

## Shell use

```sh
harness models --catalog /path/to/models.toml list
harness models --cache-dir /path/to/cache hub OWNER/REPO --json
harness models --catalog /path/to/models.toml add my-local \
  --file /path/to/model.gguf --runtime /path/to/llama-server
```

Global `--catalog` and `--cache-dir` options precede the action. The terminal
always uses its current catalog. Ctrl-C cancels shell verification. Neither
inspection nor registration needs a running inference provider.

The [local assistant guide](local-assistant.md) describes the measured M3 task
profile. [Larger-model and quantization work](local-model-candidates.md) remains
separate: these setup controls do not implement Unsloth training, managed
downloads, automatic GPU placement, or larger-model qualification.
