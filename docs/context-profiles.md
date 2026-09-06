# Bounded context profiles

A core context profile limits what Harness sends on each conversation request.
It applies to the root agent and its descendants, without requiring a plugin or
choosing a provider. Existing sessions retain full-history behavior until a
profile is explicitly configured.

```toml
# local-context.toml
history_turns = 4
max_input_bytes = 32768
tools = ["read_file", "glob", "grep", "mcp__memory__memory_list", "mcp__memory__memory_get"]
```

```sh
harness --model local --context-profile local-context.toml
harness --model local --context-profile local-context.toml -p "Inspect the project records."
```

Use an existing catalog alias and provision the runtime separately. This profile
is an input and tool budget, not a qualified model configuration. A byte limit
does not establish a token count, actual context window, or latency guarantee.

`history_turns` includes the current user turn. Harness keeps complete turns,
including their assistant tool proposals and tool results. It first applies the
turn limit, then drops additional old turns if necessary to meet the byte limit.
The current turn, system instructions, supplied task/project context, acceptance
criteria, and a leading compaction summary remain intact. If those cannot fit,
the request fails explicitly; reduce supplied context or tool output, compact,
or configure a larger profile. Selection accounts for tool schemas and selected
tool-result sidecars before inference. It never reads an oversized sidecar merely
to determine that it will not fit.

The canonical session history is retained. An omission notice accompanies the
model input, and the TUI displays the number of omitted earlier turns. The user
can inspect the complete transcript or resume it with a different profile. This
is deterministic selection, not summarization or semantic memory retrieval.

`tools` contains **exact names**. Omit it to retain all registered tools; `[]`
allows no tools. The filtered registry controls both advertisement and execution,
including tools registered later by MCP. Rewriting a call or delegating to an
agent with a broader tool definition cannot escape this restriction. The normal
permission engine still applies, and an included tool is not automatically
granted permission. Provider-native built-ins remain governed by their external
runtime; this profile does not extend Harness's containment to those built-ins.

## Inspection and continuation

TUI `/context` shows the profile and currently available allowed tools. `/tools`
also reflects the filtered inventory, and the status bar shows the input byte
cap instead of a percentage calculated from the complete stored history.

The profile is recorded in the session and restored by headless `--resume` or
`--continue`. TUI `/clear` carries it into the new session; `/resume` restores the
chosen session's own profile. An explicit `--context-profile` overrides the saved
profile. To return to default behavior on a headless continuation:

```sh
harness --continue --no-context-profile -p "Continue with the full conversation."
```

The embedding API is `build_kernel(context_policy=ContextPolicy(...))`.
`Kernel.registry` remains the mutable registration surface for plugins/MCP;
`kernel.loop.registry` is the effective view used for advertisement and dispatch.
Context profiles are configuration, not model-generated instructions. Internal
operations such as explicit compaction retain their own inference bounds.

`ContextPolicyConfigured` records configuration changes. `ContextPrepared` records
the policy digest, task ID, retained/omitted counts, prepared input bytes, and
advertised tool names. Replay preserves those facts without running selection or
inference. These events can serve as self-improvement evidence; they do not grade
a candidate, grant adoption, or activate a change.

## Normal memory stays a plugin

The sample tool names refer to a separately configured MCP server named `memory`.
The core profile does not discover a vault, import memory's implementation, or
create another memory store. Configure the existing memory plugin through the
normal MCP configuration, provision its environment before offline use, and grant
only the intended read operations. Missing plugins simply leave those tools
unavailable; native project inspection still works.

The installed-plugin integration check uses its real MCP server and normal vault,
with a scripted provider and no memory writes:

```sh
unshare --user --map-root-user --net .venv/bin/python -B \
  scripts/check_memory_context.py --memory-root /path/to/installed/memory
```

It checks a native project-file read with no plugins, then a scoped `memory_list`
call, and verifies profile/history continuity on resume. It requires the plugin's
preinstalled Python and a nonempty subject (default `harness`). It neither
bootstraps dependencies nor stores retrieved memory in its report. Temporary
session logs are removed on completion. Linux network namespaces are used by the
command above; the script reports whether IPv4 routes are present.
Empty or whitespace-only subjects fail before workspace setup or plugin access,
including when the check is invoked directly from Python.

The [observed result](handoffs/2026-09-06-core-agency/context-memory-offline.json)
establishes offline file/MCP plumbing against the actual installed plugin. It
does not establish model-generated answers, memory-entry body retrieval, a
resident assistant, or the full M3 cold-start/cancel/resume journey with real
cached weights. The available cached 30B/35B models still lack a useful measured
profile on this host. Local-model selection, resource ceilings, automatic
fallback, and semantic/self-improvement evaluation remain roadmap work.
