# Subscription-CLI model backends — design

**Date:** 2026-08-15 · **Branch:** `multimodel` · **Status:** awaiting review

## Goal

Make Claude, Codex, and Gemini usable as first-class harness models — same tool
registry, same permission engine, same event log as every other model — on the
subscriptions the user already pays for, with zero per-token API billing for
Claude and (eventually) Codex. The harness remains the agent shell, in the
Claude Code / opencode / codex class; backends are models, never nested agents.

## Constraints (non-negotiable)

1. **No per-token API billing** where a subscription exists. Claude runs on the
   Claude Code Max login; Codex should eventually run on its ChatGPT auth
   (`~/.codex/auth.json`). Gemini free-tier AI Studio API is acceptable.
2. **ToS-clean, verified 2026-08-15** against code.claude.com docs:
   - Anthropic does not allow third-party products to offer claude.ai login
     ("Unless previously approved, Anthropic does not allow third party
     developers to offer claude.ai login or rate limits for their products,
     including agents built on the Claude Agent SDK" —
     `code.claude.com/docs/en/agent-sdk/overview.md`). Therefore the harness
     **never implements claude.ai OAuth** and never sends subscription tokens
     to the Messages API itself (the opencode approach is out).
   - Headless `claude -p` is a documented mode of the Claude Code product
     (`code.claude.com/docs/en/headless.md`); the harness invokes the user's
     own installed, self-authenticated Claude Code as a subprocess. Since the
     harness is open source, this property must hold for every future adapter:
     **adapters shell out to locally installed, self-authenticated CLIs; the
     harness never handles provider credentials for subscription backends.**
3. **The harness owns the tool loop.** Backends receive the harness's tools
   and must not substitute their own. Built-in CLI tools are disabled in
   adapter invocations.

## Architecture

Two new pieces, one field, no changes to loop/dispatcher/subagents/mixture:

```
catalog entry (backend field)
        │
        ▼
CatalogProvider ──── backend absent ──► LiteLLM path (unchanged: local, gemini, gpt)
        │
        └─ backend = "claude-code" ──► ClaudeCodeProvider ──► spawns `claude -p`
                                             │                     │ model runs on
                                             │                     │ Max subscription
                                             ▼                     ▼
                                     harness MCP server ◄── tool calls via --mcp-config
                                             │
                                             ▼
                                     Dispatcher.dispatch_tool
                                     (registry, permissions, events — unchanged)
```

### 1. MCP server mode (`mcp_serve.py`) — the enabler

The inverse of `mcp_host.py`: expose the harness `ToolRegistry` as an MCP
stdio server.

- `tools/list` maps `ToolSpec`s to MCP tool declarations.
- `tools/call` routes through `Dispatcher.dispatch_tool`, so the permission
  engine, hook chain, and event log apply identically to tool calls arriving
  from a CLI-backed model and from the native loop.
- Started per adapter invocation (stdio child of the harness), scoped to the
  session's `FilteredRegistry` — a subagent's tool scoping carries through.
- Also independently useful: any external MCP client can mount the harness's
  tools.

### 2. `ClaudeCodeProvider` (`provider_claude_code.py`)

Implements the existing `ModelProvider` protocol (`provider.py:65`). On
`complete(model, messages, tools)`:

1. Write a temp MCP config pointing at the harness MCP server (which serves
   exactly `tools`).
2. Spawn `claude -p --output-format stream-json --input-format stream-json
   --mcp-config <cfg> --strict-mcp-config --disallowedTools <built-ins>`
   (flag set verified present in CC v2.1.233; exact built-in disable list is
   an implementation-time detail).
3. Map stream-json events to harness `Chunk`s: assistant text → `TextDelta`,
   usage → `UsageReport`, terminal result → `StreamStop`. Tool activity does
   not round-trip through the provider: it flows CC → harness MCP server →
   dispatcher, and is therefore already evented; the provider emits no
   `ToolCallDelta`.
4. Session continuity: first call creates a CC session; subsequent calls
   `--resume <session-id>` (works with `--print`), so CC keeps its own
   history and the harness sends only the turn delta. Fallback if resume
   proves unreliable: stateless re-render of harness `Message` history into
   the prompt. Session-id ↔ harness-session mapping lives in the provider.

Granularity note: one `complete()` call = one CC agent turn (which may
internally contain several model calls). Routing hooks and `model:` permission
checks apply per-turn for CLI-backed models. Cost/usage comes from CC's
reported usage; catalog entry carries zero per-token cost (subscription).

### 3. Catalog `backend` field

```toml
[models.claude]
backend = "claude-code"          # selects ClaudeCodeProvider
route = "claude-code/default"    # informational; optional CC --model override
tags = ["anthropic", "subscription", "tool-calling", "frontier"]
input_cost_per_token = 0.0
output_cost_per_token = 0.0
```

`Catalog.resolve` gains an optional `backend` field (default `None` →
LiteLLM). `CatalogProvider.complete` dispatches on it. Unknown backend names
error loudly at resolve time.

## Follow-up adapters (same seam, out of scope for this spec's implementation)

- **Codex:** ChatGPT-subscription auth via `codex exec` subprocess or the
  documented `codex mcp-server` (MCP-as-model-transport works for Codex —
  verified live). Harness tools mount via codex's MCP config; built-ins
  restricted per codex config.
- **Gemini CLI:** headless mode + MCP config, Google-account auth.
- Both must satisfy constraint 2's shell-out property.

Meanwhile Gemini is immediately usable through the existing LiteLLM path with
a free-tier `GEMINI_API_KEY` (catalog entry + verification only).

## Error handling

- CC subprocess failures (nonexistent binary, not logged in, nonzero exit,
  malformed stream-json) map to `ProviderError` via the existing
  `map_exception` seam — the loop already handles provider errors.
- MCP server crashes surface as tool errors to CC (which reports them in-band)
  and as harness events; the provider treats a dead server as a failed turn.
- Timeouts: per-turn wall-clock limit on the subprocess, configurable;
  on expiry, terminate the process group and raise `ProviderError`.

## Testing

- `mcp_serve.py`: protocol-level tests with a scripted MCP client
  (initialize / tools/list / tools/call happy path, permission-denied path,
  unknown tool) — no CC dependency.
- `ClaudeCodeProvider`: tests against a fake `claude` executable (a script
  emitting canned stream-json), covering chunk mapping, resume, error exits,
  timeout. No subscription use in CI.
- End-to-end (manual, the repo's `verified = true` bar): one real turn where
  a `claude`-backed model drives a `read_file` through the dispatcher; then a
  mixed session (claude parent + `local` subagent) showing two backends in one
  event log. Stamp the catalog entry `verified = true` with date.

## Risks

- **CC flag/format drift:** stream-json shapes and flags are CC-version
  dependent; the fake-executable tests pin our parser, and the provider
  should log the CC version it spawns.
- **Resume semantics under `--print`** may have edge cases; the stateless
  fallback bounds the risk.
- **Turn-granularity mismatch** (per-turn vs per-model-call hooks) is accepted
  by design; revisit only if routing rules need intra-turn control.
- **Rate limits:** Max-plan weekly limits govern throughput; mixture
  strategies fanning out over `claude` should be used deliberately.
