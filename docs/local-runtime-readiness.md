# Local runtime readiness in core

Harness can now check an explicitly configured local inference server before a
request, start a configured process when needed, and stop only its own processes.
The core prompt, queue, draft, and recovery controls work while a model is loading
or unavailable. These facilities do not require the memory or agent-swarm plugins.

This is an initial M3 checkpoint. It does not yet provide automatic cloud-to-local
fallback, select a model for the resident assistant, or qualify model quality,
normal-memory access, and fully offline operation. Existing catalog aliases keep
their behavior until an explicit `local` profile is added.

[Context profiles](context-profiles.md) separately bound conversation history,
input bytes, and tool inventories for local or remote execution.

## A local alias fails with an OpenAI connection error

The OpenAI-compatible adapter also carries local inference. Its SDK name does
not imply that the request went to OpenAI. Check the alias's `api_base` in the
catalog used by the current session. A route pointing to `localhost:8080` needs
an actual server there, with the expected model ID.

Without a `[models.ALIAS.local]` section, Harness does not probe or start that
server. An alias, a local tag, or a past `verified` flag does not supply a startup
command. The separate local-assistant launcher uses its own catalog and does not
update the normal one. Add an explicit profile as below, or start the separately
managed server before sending a message.

Local transport failures now identify the loopback server and explain the startup
requirement, instead of displaying the upstream SDK's OpenAI error body. Timeout
and connection failures retain their typed retry/fallback behavior. This message
does not prove which transport failure occurred or that a listening model can
complete the task; `/resources check ALIAS` checks configured runtime readiness.

## Existing server

Use the model ID actually advertised by your server's `/v1/models` inventory.
The `openai/` prefix selects the existing OpenAI-compatible inference adapter.

```toml
[models.local]
route = "openai/my-provisioned-model"
api_base = "http://127.0.0.1:8080/v1"
max_input_tokens = 8192 # configured metadata, not a measured capability

[models.local.local]
probe_seconds = 2
ttl_seconds = 5
```

Local profiles accept loopback HTTP(S) endpoints without URL credentials, query
parameters, or fragments. `localhost` is normalized to `127.0.0.1`; literal
loopback IPv6 is also supported. Readiness requests ignore environment proxies
and refuse redirects. If authentication is needed, set `api_key_env` on the model
entry to the name of an environment variable; its value is never recorded.

```sh
harness resources --catalog /path/to/models.toml
harness resources --catalog /path/to/models.toml --check local --json
harness --catalog /path/to/models.toml --model local --no-plugins --no-mcp
```

Inspection alone performs no network request or inference. `--check` refreshes a
bounded inventory observation and exits nonzero unless the configured model is
listed. It never starts a process. JSON output includes observation/expiry times,
a configuration digest, ownership, status, and the source of evidence. One-off CLI
checks print their evidence; checks inside a session also append core events.

In the TUI, `/resources` shows local snapshots, `/resources check local` refreshes
one, and `/resources stop local` stops a process owned by this Harness instance
when no task is active. A background check leaves the composer available; Escape
cancels it when no agent task or compaction is active. Cancellation does not turn
the diagnostic command into a user message or an agent run. A session rebuild
cancels its old probe and retains the local process owner across `/clear` and
`/resume`.

## Harness-owned process

Provision the runtime binary and weights separately. For example, a preinstalled
server with a local model file can use a profile like this (paths and flags must
match that installed runtime):

```toml
[models.local]
route = "openai/my-provisioned-model"
api_base = "http://127.0.0.1:8080/v1"

[models.local.local]
auto_start = true
probe_kind = "llamacpp"
command = ["/opt/llama/bin/llama-server", "-m", "/models/local.gguf", "--alias", "my-provisioned-model", "--host", "127.0.0.1", "--port", "8080"]
cwd = "/opt/llama/bin"
required_files = ["/models/local.gguf"]
startup_seconds = 120
probe_seconds = 2
ttl_seconds = 5
```

The command is an argument vector, executed without a shell. It is trusted local
configuration, not model-supplied input. Missing required files prevent launch.
Optional `cwd` sets the child's working directory; use an absolute path for a
profile that works across projects. A missing directory or a regular file reports
`missing_configuration` before launch. When omitted, the child inherits Harness's
working directory. Some runtimes load shared libraries relative to this directory;
the pinned llama.cpp CUDA image used in the [real local smoke check](local-model-qualification.md)
requires `/app`.
The llama.cpp profile checks `/v1/health` before `/v1/models`: its inventory can
list an ID during loading. The health shapes and command flags follow the
[upstream server contract](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
The default `openai_inventory` probe only checks model listing and does not claim
that a server has finished loading. Use an inference-only runtime configuration;
the local profile does not constrain provider-native tool execution.
Process output goes to the null device; command strings, environment values, and
server error bodies are not copied into events. Use the runtime's own separately
configured diagnostics when investigating a launch failure.

Startup happens within the normal, post-routing model dispatch, after permission
and shared-call-budget checks. The request/task deadline includes startup and
[local queue admission](local-scheduling.md). A healthy endpoint that Harness
did not launch is treated as externally managed, even when an auto-start command
is configured. A loading or denied external endpoint does not
trigger a second process. Only an unreachable endpoint permits on-demand launch.

The default root scope allows **one owned local process**, shared with delegated
work. `LocalResources(max_owned_processes=...)` can explicitly set another bound
for an embedding application. This is a process count limit, not a RAM or GPU
memory guarantee. A running owned profile cannot silently change its command or
endpoint; stop it before applying that configuration change.

An owned server remains available between turns and across session rebuilds
until another foreground/work alias in its declared group needs that capacity.
Core then stops the idle owned server before starting its replacement; background
assessments cannot cold-start or displace a warm runtime. Equivalent endpoint/model
aliases can share the existing process. See [scheduling](local-scheduling.md) for
priority, cancellation and residency rules.
Cancellation or failure during startup reaps the new process before returning;
normal application shutdown settles active work, terminates owned process groups,
and reaps its children, including when a worker ignores graceful termination.
Failure to write a stop event does not abandon the child. The TUI reports resource
cleanup errors and still attempts the session-ending lifecycle before teardown.
Externally managed services are never adopted or killed. Direct embedding callers must settle their
tasks and call `await kernel.resources.close(emit=kernel.session.append)` before
closing the session; the supplied CLI/TUI perform this lifecycle themselves.

Child environment inheritance is limited to basic path/home/locale/cache variables
plus explicit `env_names`. Hugging Face and Transformers offline flags are set.
These flags do not sandbox an arbitrary configured command or enforce an OS-level
network ban. Use preinstalled assets and qualify the actual runtime offline;
Harness does not download dependencies or weights as part of readiness.

## Evidence and limits

Readiness distinguishes unknown, checking, missing configuration, loading, ready,
busy, authentication failure, unreachable, denied, failed, and stopped states.
An ambiguous HTTP 503 stays unknown unless the server identifies loading. A ready
inventory means the configured ID was listed; inference may still fail. Context
window, tool support, structured output, and runtime version remain unknown until
separate capability evidence exists.

Successful observations expire. Configuration or credential changes invalidate
cached readiness; interrupted/failed requests also require rechecking. Caches use
only fresh ready observations to skip a dispatch probe. A negative diagnostic
remains visible but is rechecked on the next request even within its TTL, so a
check during active inference cannot delay recovery for the rest of that TTL.
Caches use monotonic time and are not restored from session logs. `ResourceObserved` events
are historical evidence in the fold, not permission to reuse readiness after a
restart. `LocalRuntimeRequested` records start/stop intent without replaying it.
No recovery path guesses an old process's ownership from a PID or kills a process
found listening on a remembered port.

These core observations can be referenced by the existing improvement journal.
They can motivate an experiment but do not grade a candidate, activate a change,
or establish task acceptance. Operational self-improvement evaluation, adoption,
and rollback remain required roadmap work.

The tests cover owned/external lifecycle, a native file-reading task over real
local HTTP streaming, policy and budget denial, credential/configuration freshness,
bounded probes, startup cancellation including a spawn race, process capacity,
journal failure cleanup, and final terminal-compositor behavior. The scripted
server is a protocol fixture. The separate [real local smoke check](local-model-qualification.md)
records a provisioned 4B profile, offline file work and normal-memory access,
resource ceilings, and terminal recovery. Broad local-model quality, crash-time
orphan reconciliation, and the complete M3 product gate remain unqualified.
