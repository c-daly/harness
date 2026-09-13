# Actual Harness and agent-swarm integration checks

The earlier 24-task execution was launched directly in Claude Code, not through
Harness. That did not test the host the user intended. The separate session
`4b0d4271` is now stopped. Its queue, branches, uncommitted work, PRs and
conversation are preserved; the runner's stored `active` flag must not be
mistaken for a live orchestrator.

These checks use Harness at `c6b1f51` in the isolated
`.worktrees/harness-swarm-execution` checkout. No core implementation was changed.
The [report](report.json) retains source hashes, journal/log paths and hashes,
terminal results, actual tool calls and explicit unqualified claims. Complete
journals remain in `.worktrees/tmp/harness-swarm-execution-v1`.

## Findings

- The user's ordinary Harness MCP configuration has agent-swarm disabled by
  default and points to a removed cache executable. The installed executable
  exists at `/home/fearsidhe/.claude/plugins/agent-swarm/bin/mcp-router`.
  The probes use an explicit local configuration; user configuration is unchanged.
- `harness import` loads the actual installed plugin's skills and agent
  definitions, but reports eight degraded tool mappings and six hooks requiring
  a hand port. Claude model aliases are absent from the Harness catalog and
  those defaults are dropped. The generated MCP command also retains an
  unresolved `${AGENT_SWARM_ROOT}` placeholder; the explicit probe configuration
  supplies the installed executable. Import success is not workflow compatibility.
- The installed workflow-state tool is reachable through Harness's actual MCP
  host, permission checks and dispatcher. A no-inference connection probe
  returned JSON `null` for workflow ID `orchestrate`, with no tool error.
  That is not evidence of a permission denial or loss of the saved parallel
  runner queue: that queue is in a separate plugin-owned JSON file.
- The 8B local profile failed its configured 60-second startup budget before
  inference. Harness recorded an incomplete task with unknown usage and stopped
  its owned runtime. The CLI surfaced a raw `TimeoutError`; useful startup error
  reporting remains a gap.
- The 4B profile started but proposed more than one tool in its first response.
  The configured one-tool rule rejected that batch; no tools were executed.
- With the existing `tool_recovery_attempts = 1` feature enabled, the 4B model
  again violated the rule, received one recorded correction, and then successfully
  invoked `invoke_skill(name="parallel-orchestrate")` and the real plugin's
  workflow-state tool in separate calls. Four model attempts, one correction,
  and two successful tool completions are in the journal. Core recorded execution
  completion with acceptance still unverified. The model's final prose loosely
  suggested possible denial; the actual result was a successful `null` response.

The failed initial attempts remain failures. The correction run changed one
explicit operating setting; it did not relax the one-tool rule. All requests
used existing local assets and loopback inference, with only skill invocation
and read-only workflow-state access allowed. No coding workers were started,
normal memory was not exercised, and the actual queue has not moved into Harness.

## Retained configuration and connection check

`mcp.toml`, `context.toml`, and `context-recovery.toml` are the exact probe
configuration files. `connection_probe.py` uses a provider that makes no model
calls, but the real Harness MCP host and dispatcher with the installed server.
It is only a connection check. The local-model results were produced by the
ordinary Harness CLI, not that helper.

The helper expects its configuration beside it and writes evidence there. Copy
the helper and `mcp.toml` to a new scratch directory to repeat it. The imported
plugin and original execution logs remain under the scratch directory named
above. Do not overwrite prior runs or treat these machine-specific paths as
portable installation defaults.

## Next execution boundary

Use the existing generated plan, manifest, runner state and worker artifacts
under `.worktrees/remaining-roadmap-workflow/docs/handoffs/2026-09-12-agent-swarm`.
Agent-swarm continues to own queue policy. Resolve its Harness bindings and
exercise one real bounded worker through Harness before broader dispatch; a
loaded skill or reachable server alone cannot satisfy that gate. Preserve
prerequisite commits, declared permissions, budgets, operator-owned PR merges
and the unfinished workers' edits during reconciliation.

Automatic approval review rejected the GPT-backed probe because loaded plugin
context might be sent to OpenAI without explicit payload approval. That remote
probe did not run. The successful local correction probe avoids that data
transfer; it does not authorize subsequent remote workflow execution.
