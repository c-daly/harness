## Summary

Claude Code and Antigravity now bind to the same typed AgentRuntime task
contract that Codex already used, so every external agent alias (Codex,
Claude Code, Antigravity) gets distinct task/run identities, a capability
snapshot, an execution progress phase, and a terminal result through
bind_agent_runtime -- for direct providers and for catalog aliases routed
through CatalogProvider.

## What changed and why

- src/harness/agent_runtime.py: widened AgentRuntimeInfo.native_tools from a
  single fixed literal (provider-controlled) to also allow none and
  unconfined, so each adapter can declare its own native tool exposure.
  bind_agent_runtime and ExternalAgentRuntime needed no other changes: they
  already dispatch generically on whether a provider defines
  agent_runtime_info. Updated the module docstring and doc section title to
  name all three adapters instead of just Codex.
- src/harness/provider_claude_code.py: added
  ClaudeCodeProvider.agent_runtime_info returning
  AgentRuntimeInfo(runtime=claude-code, native_tools=provider-controlled).
  The adapter supplies Harness MCP tools and a finite built-in denylist;
  it has no verified exclusive tool inventory across supported CLI versions.
  Harness permissions and tool events cover its dispatched calls, without
  establishing control or auditing of remaining native tools.
- src/harness/provider_antigravity.py: added
  AntigravityProvider.agent_runtime_info returning
  AgentRuntimeInfo(runtime=antigravity, native_tools=unconfined). The agy
  built-in tools (about 57 of them) are not disabled (additive, like the
  codex shell), and v1 ships without --sandbox or --mode plan, so they are
  unconfined relative to the codex read-only-sandboxed shell.
- src/harness/provider_litellm.py (in scope, not separately named in the
  task file list, but required to actually reach these adapters through a
  catalog alias -- see Deviation below): generalized
  CatalogProvider.agent_runtime_info, which only routed backend == codex, to
  also route claude-code and antigravity to their own provider
  agent_runtime_info method. Without this, bind_agent_runtime would never
  fire for either adapter in the normal CatalogProvider-wired production path
  (build_kernel in src/harness/cli.py always constructs
  CatalogProvider(catalog, claude_code=..., codex=..., antigravity=...)),
  even though complete() already dispatched to all three backends.
- docs/core-inference-and-improvement.md: renamed the section titled
  External agent tasks: Codex migration to External agent tasks: Codex,
  Claude Code, and Antigravity bindings, and generalized its prose (binding
  selection, AgentLoop auto-selection, MCP tool execution, and the
  capability-snapshot paragraph, which now spells out each adapter
  native_tools declaration and why it differs).

### Deviation from the task file list

The task named agent_runtime.py, provider_claude_code.py, and
provider_antigravity.py as the files to modify. Reaching the stated goal (so
CLI, TUI and child sessions get distinct task/run identities for every
external agent alias) also required a small fix in provider_litellm.py,
CatalogProvider.agent_runtime_info, which is the only place that actually
calls each adapter agent_runtime_info for catalog-routed models (the normal
CLI/TUI path built by build_kernel). Without it, the new declarations on
ClaudeCodeProvider and AntigravityProvider would only be reachable by callers
holding the raw provider directly, never through the catalog aliases CLI/TUI
actually use. This is a six-line, single-method change with no behavior
change for Codex.

## Tests added

- tests/test_provider_claude_code.py::test_agent_runtime_info_declares_provider_controlled_native_tools
- tests/test_provider_antigravity.py::test_agent_runtime_info_declares_unconfined_native_tools
- tests/test_external_agent_runtime.py:
  - Added ScriptedClaudeCode and ScriptedAntigravity (the same
    ExternalAgent-mixin fake-chunk pattern already used for ScriptedCodex)
    plus a _BACKEND_FOR_KIND / _RUNTIME_FOR_KIND mapping.
  - Parametrized test_interface_uses_typed_runtime_and_replays_one_response
    over direct, codex, claude-code, antigravity (was direct, codex) --
    covers supplied context, unknown or partial usage, and typed capability
    snapshot per adapter.
  - Parametrized test_external_run_closes_before_terminal over codex,
    claude-code, antigravity times bytes, cancel, deadline -- output bounds,
    cancellation, and cleanup-before-terminal per adapter.
  - Parametrized
    test_resume_aborts_external_run_and_tools_without_fabricating_conversation
    over the three runtime names -- resume without replay per adapter.
  - Added test_claude_code_process_calls_scoped_mcp_tools_and_preserves_transcript
    and test_antigravity_process_calls_scoped_mcp_tools_and_preserves_transcript
    (parametrized over blocked and cancel_during_tool, mirroring the existing
    Codex test): real fake-CLI child processes, a real McpToolServer, real
    MCP round trips -- covers allowed and denied MCP tool effects, transcript
    and task-lineage fidelity, and cleanup ordering (the tool settles before
    the terminal event) for both adapters. The Antigravity fake binary
    handles both its mcp add registration call and its main turn invocation
    by branching on sys.argv[1], passing the harness URL between them via a
    file in the shared scratch HOME -- mirroring how the real provider shares
    state between the two real subprocess calls.
  - Added test_cancel_reaps_claude_code_process_before_task_terminal and
    test_cancel_reaps_antigravity_process_before_task_terminal (mirroring
    test_cancel_reaps_codex_process_before_task_terminal): cancellation reaps
    the child process before agent_run_finished is published.

All new tests use fake CLI subprocess fixtures only (a throwaway Python
script standing in for claude or agy); no real claude, codex, or agy binary
or subscription credential is ever invoked.

## Review-fix validation (2026-09-15)

The provider/runtime/backend/MCP regression selection passed on Python 3.12
and 3.13: 116 passed, one opt-in live probe skipped, six MCP deprecation
warnings on each. Repository-wide Ruff and `git diff --check` passed.
Capability recording is covered through catalog routing and fake CLI/MCP
execution; this does not qualify a live CLI or its native-tool confinement.

The first 3.13 attempt failed because fake CLI subprocesses found system
Python without MCP. Its result is retained separately; the passing runs put
the matching virtual environment on PATH.

## Initial implementation validation (before review fixes)

uv run ruff check src/harness/agent_runtime.py src/harness/provider_claude_code.py src/harness/provider_antigravity.py src/harness/provider_litellm.py tests/test_external_agent_runtime.py tests/test_provider_claude_code.py tests/test_provider_antigravity.py
All checks passed.

uv run pytest -q   (Python 3.13.15)
2378 passed, 7 skipped, 6 warnings in 680.63s (0:11:20)

uv sync --locked --extra dev --python 3.12 then uv run --python 3.12 pytest -q
2378 passed, 7 skipped, 6 warnings in 612.09s (0:10:12)

uv build --out-dir dist
Successfully built dist/harness-0.0.1.tar.gz and dist/harness-0.0.1-py3-none-any.whl

bash scripts/smoke_wheel.sh dist/*.whl
wheel smoke OK

git diff --check
clean, no output

Both full-suite numbers (2378 passed, 7 skipped, 6 warnings) match the
documented baseline exactly (seven skips are the Anthropic and Ollama
fixtures plus the opt-in Antigravity live probe; six warnings are the known
MCP streamable_http_client deprecation notices).

### Flaky, unrelated failures observed and ruled out

An earlier Python 3.12 run under heavy concurrent load (other parallel agent
worktrees on the same shared machine) intermittently failed one or two of:
tests/test_source_improvement.py::test_process_group_is_settled_before_result[timeout],
tests/test_source_improvement.py::test_bounded_failure_cannot_pass[output],
and tests/test_tui.py::test_tab_completes_at_mention_and_cycles_through_matches.
None of these files are touched by this change (confirmed via
git diff --stat HEAD -- tests/test_tui.py src/harness/tui.py showing no diff,
and src/harness/source_improvement.py has no reference to agent_runtime,
provider_litellm, provider_claude_code, or provider_antigravity). The
process-group test is a /proc/pid/stat read racing a reap under load; the TUI
test is order or timing dependent (it failed standalone under both 3.12 and
3.13 yet passed inside the full suite). Two clean, complete re-runs of the
full 3.12 suite (once the shared machine quieted down) both landed at exactly
2378 passed, 7 skipped, 6 warnings, matching 3.13 -- these are pre-existing
environment flakes, not regressions from this change.

## Known limits and left outstanding

Same limits already documented for Codex, now shared by all three bindings:
no native resume, no internal-iteration cap enforcement past a
zero-iteration guard, raw stdout and stderr bounds, scratch-file containment,
and CLI-version probes remain outstanding. All three descriptors stay
qualification=unverified -- this is an adapter declaration, not a live-CLI
qualification gate.
