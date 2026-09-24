# Resident capture through the installed memory plugin

This adapter connects Harness's native capture lifecycle to the existing memory
plugin. It imports that installation's `session_recorder`, `memory_writer`,
`memory_reader`, provider, index and lock. It creates no separate memory store
and never invokes `claude -p`. Run it in a separate MCP process because the
installed plugins use overlapping Python module names.

This is the first capture slice of the [continuity outing plan](../../docs/superpowers/plans/2026-09-18-resident-continuity-first-outing.md).
It does not yet bind the continuity plugin's resume-brief composer, PM, experiment
or agent-swarm. Those remain separate integrations.

## Configuration

Use a project-specific context profile and an existing, unambiguous memory
subject. Both the server and the profile bind the project explicitly. Replace
the example absolute paths with your actual paths. The server uses the memory
plugin's normal `MEMORY_VAULT_DIR` resolver and default provider.

```toml
# ~/.config/harness/mcp.toml
[servers.resident-memory]
command = "/path/to/memory/.venv/bin/python"
args = ["/path/to/harness/plugins/resident-memory/server.py",
        "--memory-plugin", "/path/to/memory", "--project", "my-project"]
tools_allow = ["capture_prepare", "capture_write", "memory_search", "memory_read"]

# Optional: the value is the NAME of an environment variable, not a path.
[servers.resident-memory.env]
MEMORY_VAULT_DIR = "MY_MEMORY_VAULT"
```

Add to the chosen context profile:

```toml
# Include these exact tool names in the profile's existing tools list:
# mcp__resident-memory__capture_prepare
# mcp__resident-memory__capture_write
# mcp__resident-memory__memory_search
# mcp__resident-memory__memory_read

[capture]
project = "my-project"
workspace = "/absolute/project/checkout"
prepare_tool = "mcp__resident-memory__capture_prepare"
write_tool = "mcp__resident-memory__capture_write"
model = "local" # An explicit inference alias, independent of the resident's model.
timeout_seconds = 30
max_transcript_bytes = 16384
max_input_bytes = 32768
max_record_bytes = 4096
max_output_tokens = 1024

[[sources]]
id = "project-memory"
tool = "mcp__resident-memory__memory_search"
args = { subject = "my-project", type = "project", limit = 6, max_bytes = 4096 }
max_bytes = 4096
timeout_seconds = 10
```

The normal permission engine must allow the two capture tools and recorder
model; listing tools does not grant permission. Core also requires the active
native workspace guard to match `capture.workspace`. Use `--workspace` when
starting outside that checkout. No user configuration is changed by installing
this source. Capture is disabled unless the profile declares it.

The opening search returns the normal index descriptions and entry identifiers.
The resident can retrieve bodies through `memory_read`, including its existing
revision-bound pagination. Retrieval quality and record selection still need
evaluation; a search result is not proof that a correction was understood.

## Lifecycle and recovery

After a root turn has a terminal execution fact, core records a capture request.
The recorder receives the recorded user message, final assistant text, source
session/run/event references, and execution and acceptance status. It does not
receive tool bodies or temporary mention expansions. Assistant claims remain
claims; this record cannot certify that an external action succeeded.

The installed recorder constructs the prompt. Harness dispatches bounded,
tool-free inference with normal permissions and usage accounting. Local
scheduling assigns capture background priority. An explicit skip sentinel means
there was nothing to retain; empty or incomplete output is a pending failure.

Before dispatching a write, core saves the prepared content in the canonical
session's blob store and journals its reference. The plugin writes through its
normal writer, verifies the body through the provider and normal indexed reader,
and returns a receipt binding the capture ID, project and content hash. Only a
matching receipt becomes `saved` in `/status` and the TUI.
The prepared artifact also binds the adapter's destination fingerprint (normal
vault root and project). Reconfiguring the server to use another vault cannot
silently redirect a retry, even when the tool names remain unchanged.

A repeated write with the same ID and content returns the same receipt. Changed
content or project is rejected. If the memory file exists but the index update
was interrupted, the adapter uses the memory plugin's existing index rebuild.
It does not replace or edit the record. Corrupt or conflicting records remain
pending for inspection.

On the next root turn, core reconstructs missing intents from terminal journal
facts and retries the oldest pending capture once. It then attempts that turn's
capture after execution. A prepared record is reused without another model
call. Changed capture policy or workspace cannot redirect an earlier intent.
Disabling capture suspends retries; pending records remain inspectable. Restart
alone performs no write: retry occurs with subsequent work or an explicit
`kernel.loop.captures.retry()` call. Cancellation records pending status and
propagates normally.

This first implementation performs capture before the turn worker returns,
within `timeout_seconds`; it can add latency. A deferred scheduler, explicit
retry/discard UI, capture before compaction and at orderly shutdown, richer
tool evidence, project selection, and conflict/staleness reconciliation remain
open. The configured byte bounds reject oversized input without dropping text.
No automatic retention policy or deletion is introduced.

## Verification

Normal unit tests exercise the core outbox with fixture providers. To additionally
exercise the actual installed recorder, writer, provider, index and reader over
real MCP, with a temporary test vault:

```sh
HARNESS_MEMORY_PLUGIN=/path/to/memory uv run --locked pytest -q tests/test_resident_memory_integration.py
```

The live behavioral trial uses Harness for every model and tool call. It seeds a
correction, starts a fresh session without memory as a control, and starts a
fresh session with automatic project-memory retrieval. Its gates require actual
file contents: `production` for the control and `staging` with continuity. It
also requires a verified capture receipt and distinct session IDs. The driver
does not author either choice file. The selected endpoint must already be
running; the trial never starts or stops a model runtime.

```sh
uv run --locked python plugins/resident-memory/outing.py \
  --memory-plugin /path/to/memory --catalog /path/to/models.toml \
  --model local --output /tmp/a-new-continuity-outing
```

The output directory must not exist. It holds the isolated vault, workspace,
canonical Harness journals/blobs and `report.json`, including all failures.
A successful run is finite evidence for this correction scenario, not completion
of the planned five-session outing or proof of reliable long-term continuity.
