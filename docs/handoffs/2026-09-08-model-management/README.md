# Core model-management evidence — September 8, 2026

Base: PR30's merge, `c15fac911661f7851d6bc85f37135bcb353f1502`.
Implementation and use: [model-management guide](../../model-management.md).

## Public discovery and offline registration

The actual command queried public metadata for `Qwen/Qwen3-8B-GGUF`, resolved
`main` to `7c41481f57cb95916b40956ab2f0b139b296d974`, and listed five GGUF
variants. No weights were downloaded. The existing Q4_K_M file matched:

- Size: **5,027,783,488 bytes**.
- SHA-256: `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`.

Registration then used `--offline` and that cached commit to verify the installed
file and create `setup-cpu` in a temporary catalog. The native runtime was the
already-installed llama.cpp b9603 bundle from the previous local-session repair.
The normal user catalog was not edited. Settings: port 8183, context 8192,
four threads, `--gpu-layers 0`, explicit library directory and disabled thinking.
The GPU had 9,531 MiB occupied before the check, so the test used CPU execution
and did not stop or replace the user's server.

Reproduction shape, substituting installed paths:

```sh
harness models --cache-dir /tmp/model-check/index hub Qwen/Qwen3-8B-GGUF --json
harness models --catalog /tmp/model-check/models.toml --cache-dir /tmp/model-check/index \
  add setup-cpu --file /path/to/Qwen3-8B-Q4_K_M.gguf \
  --runtime /path/to/llama-server --library-path /path/to/runtime-libraries \
  --port 8183 --context 8192 --gpu-layers 0 --disable-thinking \
  --hub Qwen/Qwen3-8B-GGUF --revision 7c41481f57cb95916b40956ab2f0b139b296d974 --offline
```

## Actual inference and terminal output

The ordinary CLI read a temporary `PROJECT.md` and wrote `RESULT.json` with native
tools. Independent parsing and dictionary equality verified
`{"project": "lantern", "retry_limit": 7}`. Three recorded model calls took
**14.012, 9.351 and 3.697 seconds**, including startup in the first call. Read and
write tool completions succeeded; the model returned exactly `SETUP_WRITE_OK`.
The [CLI report](cli-report.json) records those timings and the checked artifact.
The event log records the owned process starting and stopping.

The terminal pilot entered `/models add` against the same installed 8B file,
cached Hub commit and native CPU runtime, under a fresh `tui-local3` alias on
port 8184. It used a regular `HarnessApp`, full native tool inventory, no special
context profile, and no MCP/plugins. The initial echo provider was replaced by
the normal `/model` selection path before inference.

The [report](tui-report.json) records visible verification progress, actual
composer input changes during hashing, preserved draft text, successful
registration in **4.868 seconds**, and one real model call returning exactly
`SETUP_TUI_OK` in **12.573 seconds** including startup. The reply was checked in
the stored model message and final compositor. Shutdown recorded one matching
owned-process stop. The normal user catalog's digest was unchanged throughout.

- [Final compositor during verification](tui-verifying.svg).
- [Final compositor after model selection and inference](tui-final.svg).

Two preceding diagnostic attempts are not counted as complete journeys. The
first sampled progress after registration had already completed; the second
captured progress/input but dereferenced a turn worker after the controller had
cleared it. The corrected observer samples during verification and allows the
completed worker to be absent. Those were driver corrections; the core code and
model settings did not change between the terminal attempts. Their temporary
aliases and sessions were retained.

These checks demonstrate registration leading to actual local inference and one
exact native file write. They do not qualify general CPU latency, larger models,
memory/plugin workflows, downloads, fine-tuning or semantic self-improvement.
Offline metadata selection and local inference do not establish host network
isolation. Existing M3/M4 quality results retain their original scope.

## Validation record

The initial full-suite invocation was invalidated when a cross-version test
command rebuilt the shared virtual environment. Two resource tests failed when
their original certificate files disappeared. The first 3.12 invocation also
failed to import the editable package. The workspace environment was restored
with the locked Python 3.13 dependencies; subsequent 3.12 tests used a separate
environment and explicit interpreter.

That clean 3.13 suite produced **1,683 passes, seven skips and three failures in
376.77 seconds**. The three external-agent fixtures used `#!/usr/bin/env python3`;
the direct-interpreter invocation had not put the virtual environment on PATH,
so their child processes lacked MCP. Running the complete external-runtime and
Codex-provider group through `uv run --no-sync pytest` passed **29 tests in
6.29 seconds**, including all three failures, without source changes. This is
full-suite coverage plus a corrected affected-group rerun, not a claim that the
initial full-suite invocation was green.

**109 affected tests passed on Python 3.12 in 31.28 seconds.** Coverage includes
Hub bounds/cache/provenance, file verification, atomic catalog registration,
concurrent edits, cancellation, terminal model selection, runtime ownership and
inference client integration. The source distribution and wheel built; the wheel
installed offline in a clean environment and exposed `harness models --help`.
Validation and remaining milestone work are recorded in the
[core progress log](../../superpowers/plans/2026-09-06-core-agency-progress.md).
