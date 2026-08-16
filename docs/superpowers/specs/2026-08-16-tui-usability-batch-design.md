# TUI Usability Batch — Design

Date: 2026-08-16 · Status: approved (design approved in-chat) · Branch: multimodel

Five commonly-used features that close the daily-driver gap between the harness
TUI and Claude-Code-class shells. All ride the existing event-sourced kernel:
no event-schema changes, no new subsystems — TUI surface plus two small
provider-parser additions.

## F1 — Thinking visibility (all backends)

**Problem.** `ThinkingDelta` exists in the Chunk taxonomy and the LiteLLM path
already emits it with real text (`reasoning_content`), but the TUI discards the
text and shows a static "(thinking…)" suffix. The claude-code and codex parsers
never emit it at all. On reasoning models (local Qwen3.6 especially) a turn
looks hung for minutes while the model thinks invisibly.

**Design.**
- TUI streams thought text into the live view as it arrives, visually distinct
  (dim/italic). When the first `TextDelta` of the answer arrives, the thought
  stream collapses to one dim summary line: `(thought for <N>s · <N> chars)`.
- `/thoughts collapse|full|off` sets the mode (default `collapse`; `full`
  keeps the thought text above the answer; `off` restores today's suffix-only
  behavior). Mode is session-local, not persisted.
- `provider_claude_code`: content blocks with `type == "thinking"` yield
  `ThinkingDelta(text=block["thinking"])` (signature block field carried if
  present).
- `provider_codex`: `item.completed` items with `item.type == "reasoning"` and
  non-empty `text` yield `ThinkingDelta(text=item["text"])`.
- Thought text is display-only: it must NOT enter `loop.history`, the event
  log's message content, or subagent transcripts (unchanged from today —
  `ThinkingDelta` is already excluded from assembled messages).

## F2 — Session resume + picker

**Problem.** `resume_session` is wired through `build_kernel`
(`--resume <id>` already reaches the TUI), but there is no way to discover
session ids and no `--continue`.

**Design.**
- `list_sessions(base) -> list[SessionSummary]` beside the log reader:
  session id, last-modified time, event count, first user prompt (truncated),
  and last model — read from the event-log files in the writer's own layout.
  Torn/unreadable logs appear with an error marker instead of being hidden.
- `--continue` on the main entrypoint: resolve to the most recently modified
  session and behave exactly like `--resume <that id>`. Error clearly when no
  sessions exist. `--continue` and `--resume` together are an argparse error.
- `/resume` in the TUI opens a modal picker (ServerChecklistScreen idiom:
  ModalScreen returning a value, priority Enter/Escape) listing the most
  recent sessions (age · first prompt). Choosing one rebuilds the kernel
  in place on that session (the F4 rebuild mechanism), same permission
  engine, same provider construction path as startup. Escape cancels.

## F3 — Status bar

**Design.** A persistent one-line bar (docked above the prompt) showing:
`<model alias> · ctx <fill>% · $<session cost> · <tool-call count>`.
- Cost and tool count come from the existing `TelemetrySubscriber` +
  `run_rollup` (already invoked at turn end).
- Context fill = (tool-schema estimate via `_schema_token_estimate` + a
  chars//4 estimate over `loop.history` text) / the resolved model's
  `max_input_tokens` (fall back: 16384 when a `local`-tagged model declares
  none; blank segment when no catalog model is active).
- Refreshes at turn end, on `/model`, on `/clear`, on `/compact`, on `/resume`.
- Echo mode (no catalog): bar shows model name and tool count only.

## F4 — /clear + /compact

**Design.**
- `/clear`: end the current session cleanly and rebuild the kernel in place —
  fresh session id and event log, same provider instance, same permission
  engine (the reuse-across-rebuilds path build_kernel already supports), same
  enabled MCP servers. Refused (with a toast) while a turn is running.
- `/compact`: one summarize call through the CURRENT model asking for a
  handoff-quality summary of `loop.history`; on success emit
  `CompactionApplied(from_seq=<first message-bearing seq>, to_seq=<last>,
  summary=..., model=...)` — the event and its fold semantics already exist —
  and replace `loop.history` with the summary as a system message (mirroring
  exactly what fold produces on replay, so a later resume reconstructs the
  same state). v1 compacts the WHOLE transcript (no keep-tail). On any
  failure of the summarize call: history untouched, error surfaced, no event
  emitted. Refused while a turn is running.

## F5 — @-file mentions

**Design.**
- In the prompt, a token starting with `@` triggers path completion: Tab
  cycles matches (workspace-relative; git-tracked files first, then untracked
  non-ignored), completing inline in the existing `HistoryInput` (no popup
  in v1).
- On submit, each `@path` token that names an existing workspace file is read
  THROUGH THE DISPATCHER (the same read tool the model would use), so the
  read is evented and permission-gated; contents are injected into the turn
  as context ahead of the user text, each capped (16 KiB per file); over-cap
  files are truncated with a note visible to the model. Non-existent paths
  pass through as literal text.
- The user's message in history keeps the literal `@path` text; the injected
  content is additional context for that turn only.

## F6 — Markdown rendering (added 2026-08-16, approved in-chat)

**Design.** Completed assistant replies are written to the transcript as
rendered markdown (Rich `Markdown` renderable into the existing `RichLog`):
headers, code fences with syntax highlighting, tables, lists. Streaming stays
plain text (partial-markdown flicker avoided by design); the final reply
replaces the plain stream. `/markdown on|off` (default on, session-local)
falls back to today's escaped-plain behavior. Thought summaries/blocks (F1)
and system/error lines stay plain — markdown applies to assistant reply text
only.

## F7 — Activity panel: Files / Agents / Workflows (added 2026-08-16, approved in-chat)

**Design.** A toggleable sidebar (`/panel` command + a key binding), hidden by
default, with three tabs fed by the session's own event stream:
- **Files**: workspace paths touched this session with R/W/E markers, folded
  from read/write/edit tool completions. Newest first, deduped per path with
  merged markers.
- **Agents**: one row per subagent/mixture dispatch (`dispatch_agent`,
  `ensemble`, `consult_panel`, `escalate`): label, status
  (running/done/error), model when known.
- **Workflows**: two stacked parts. (1) Harness-native: mixture strategy runs
  grouped with their per-expert rows — always available. (2) agent-swarm:
  a read-only workflow/phase-state view that EXISTS ONLY when the agent-swarm
  MCP server is enabled this session (the cleanly-disableable invariant:
  disabled ⇒ the section is absent, not empty-with-apology). State is
  fetched via the server's own workflow tools THROUGH the dispatcher (so
  fetches are evented and permission-gated), on tab open and on an explicit
  refresh key — never on a timer.
- Merely opening/toggling the panel causes no event-log writes; the only
  eventing the panel initiates is the explicit agent-swarm state fetch.
- Panel toggle must not disturb a running turn.

## Non-goals (this batch)

Permission modes (auto-accept-edits), mid-session MCP server toggling,
keep-tail compaction, popup-style completion UI, persisting /thoughts,
/markdown, or panel state; streaming markdown (final-reply rendering only);
agent-swarm workflow CONTROL from the panel (read-only view).

## Testing philosophy

Per-feature contract tests in the existing test files' idioms (make_app +
pilot for TUI; fake CLI scripts for providers). Every behavioral claim above
that names an observable (event emitted, history replaced, thought text never
in history, bar contents, torn-log marker) is a named test contract.
