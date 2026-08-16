# TUI Usability Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thinking visibility, session resume/picker, status bar, /clear + /compact, and @-file mentions in the harness TUI.

**Architecture:** All five features ride the existing event-sourced kernel — TUI surface work plus two provider-parser additions. No event-schema changes (`CompactionApplied` already exists and folds correctly). The kernel rebuild-in-place mechanism (Task 3) is the shared substrate for `/clear` and `/resume`.

**Tech Stack:** Python 3.13, Textual, pytest (`make_app` + `pilot` idiom in tests/test_tui.py; fake CLI-script harnesses in tests/test_provider_*.py).

**Spec:** docs/superpowers/specs/2026-08-16-tui-usability-batch-design.md

## Global Constraints

- Thought text is display-only: it must never enter `loop.history`, event-log message content, or subagent transcripts.
- `/clear`, `/compact`, `/resume` are refused with a visible message while a turn worker is running (`self._turn_worker is not None and self._turn_worker.is_running`).
- Kernel rebuilds reuse the SAME PermissionEngine instance (build_kernel's reuse-across-rebuilds path, cli.py:133-137) and the same provider construction behavior as startup.
- Every behavioral spec claim naming an observable is a test contract; tests must fail against pre-task code (no vacuous tests).
- Docs sync is part of every task: update docs/user-guide.md (commands/features) in the task that ships the behavior.
- Follow existing file idioms; contract-level test stubs in this plan are intentional — write full tests against the target file's own fixtures (make_app, fake scripts), asserting the named contracts.
- Run the FULL suite before each task's final commit; never pipe pytest through filters.

---

### Task 1: Provider thinking emission (claude-code + codex)

**Files:**
- Modify: `src/harness/provider_claude_code.py` (content-block loop at ~194-196)
- Modify: `src/harness/provider_codex.py` (JSONL item.completed branch at ~250-255)
- Test: `tests/test_provider_claude_code.py`, `tests/test_provider_codex.py`

**Interfaces:**
- Consumes: `ThinkingDelta` from `harness.provider` (fields: `text`, optional `signature`).
- Produces: both subscription backends yield `ThinkingDelta` chunks interleaved before/among `TextDelta`s; consumed by Task 2's TUI display.

- [ ] **Step 1: claude-code — failing tests.** In the existing fake-claude stream-json script harness, add a fixture whose assistant message content includes `{"type": "thinking", "thinking": "let me check", "signature": "sig1"}` before a text block. Contracts: (a) a `ThinkingDelta(text="let me check", signature="sig1")` is yielded before the `TextDelta`; (b) a thinking block with empty text yields nothing; (c) assembled turn text (existing assembly helper in the test file) contains ONLY the text block — thinking never leaks into message content.
- [ ] **Step 2: claude-code — implement.** In the content-block loop (provider_claude_code.py:194-196), alongside the existing text branch:
```python
elif block.get("type") == "thinking" and block.get("thinking"):
    yield ThinkingDelta(
        text=block["thinking"], signature=block.get("signature")
    )
```
(import `ThinkingDelta` alongside `TextDelta`). Run tests: pass.
- [ ] **Step 3: codex — failing tests.** Extend the fake-codex JSONL script with `{"type":"item.completed","item":{"type":"reasoning","text":"considering options"}}` ahead of the agent_message item. Contracts: (a) `ThinkingDelta(text="considering options")` yielded before the `TextDelta`; (b) reasoning item with empty/missing text yields nothing; (c) turn text assembly unchanged.
- [ ] **Step 4: codex — implement.** In the `item.completed` branch (provider_codex.py:250-255), before the agent_message check:
```python
if item.get("type") == "reasoning" and item.get("text"):
    yield ThinkingDelta(text=item["text"])
```
(import ThinkingDelta; update the comment that currently says reasoning items are skipped). Run tests: pass.
- [ ] **Step 5: Docs.** user-guide backend sections: one sentence each — reasoning/thinking output is surfaced to the UI as thought chunks and never enters the transcript.
- [ ] **Step 6: Full suite, then commit** `feat(providers): surface claude and codex reasoning as ThinkingDelta`.

### Task 2: TUI thinking display + /thoughts

**Files:**
- Modify: `src/harness/tui.py` (`_on_chunk` at 243-253; `_run_turn` completion path; command dispatch at 413-427; help text)
- Test: `tests/test_tui.py`

**Interfaces:**
- Consumes: `ThinkingDelta` (all providers; LiteLLM already emits it).
- Produces: `HarnessApp._thought_mode: str` in {"collapse","full","off"} (default "collapse"), `/thoughts` command.

- [ ] **Step 1: Failing tests.** Using make_app with a scripted provider that yields ThinkingDelta("pondering…") then TextDelta("answer"): (a) mode collapse (default): during streaming the live view shows the thought text (distinct styling is a rendering concern; assert the thought TEXT is present), and after the answer starts the live view / transcript shows a `(thought for` summary line and the answer, NOT the raw thought text; (b) `/thoughts full` then a turn: thought text is retained above the answer in the transcript output; (c) `/thoughts off`: neither thought text nor summary, only "(thinking…)" suffix behavior; (d) in ALL modes `loop.history` message content contains no thought text; (e) `/thoughts bogus` reports valid modes.
- [ ] **Step 2: Implement.** Track `_thought_buffer: str` and `_thought_started: float | None` per turn (reset where `_stream_buffer` resets, tui.py:241). In `_on_chunk`: ThinkingDelta appends to `_thought_buffer`; render per mode (collapse/full: stream `_thought_buffer` + text so far; off: current suffix behavior). On first TextDelta with a non-empty `_thought_buffer` in collapse mode, replace the streamed thought with `(thought for {int(now-started)}s · {len(buffer)} chars)`. On turn completion in full mode, emit the thought block dimmed via `say`. Add `/thoughts` to the command dispatch and `/help`.
- [ ] **Step 3: Docs.** user-guide TUI commands table: `/thoughts collapse|full|off`, default and behavior; note reasoning models (local Qwen3.6) now show live thought instead of appearing hung.
- [ ] **Step 4: Full suite, then commit** `feat(tui): stream model thinking with /thoughts collapse|full|off`.

### Task 3: Kernel rebuild-in-place + /clear + /compact

**Files:**
- Modify: `src/harness/tui.py` (new `_rebuild_kernel` helper + two commands + help)
- Modify: `src/harness/cli.py` ONLY if a build_kernel parameter needs threading that the TUI cannot already pass (it already receives everything at startup; prefer no cli.py change)
- Test: `tests/test_tui.py`

**Interfaces:**
- Consumes: `build_kernel` (engine-reuse path), `resume_session` semantics for what fold produces (fold.py:89-97), `CompactionApplied(from_seq, to_seq, summary, model)`.
- Produces: `HarnessApp._rebuild_kernel(resume_session_id=None)` — tears down the old kernel loop/session cleanly, calls `build_kernel` with the SAME base_dir/provider-construction inputs/permission engine/registry inputs the app was started with (capture whatever startup kwargs are needed on the app at construction time; `run_tui`'s signature may grow optional params, defaulted so existing callers are unchanged), rewires `loop.on_chunk`, telemetry subscriber, and the F3 status bar if present. Used by /clear here and /resume in Task 4.
- Produces: `/clear`, `/compact` commands.

- [ ] **Step 1: Failing tests — /clear.** (a) after a turn, `/clear` yields a kernel whose `session.id` differs and whose `loop.history` is empty, while `loop.provider` IS the same object and the permission engine instance is unchanged; (b) `/clear` during a running turn is refused (message shown, session unchanged); (c) a turn after `/clear` completes normally (echo provider).
- [ ] **Step 2: Implement /clear** via `_rebuild_kernel()`. End the old session cleanly (the same shutdown path the app's exit uses — locate it in tui.py/run_tui teardown), then rebuild and rewire. Refuse while `_turn_worker.is_running`.
- [ ] **Step 3: Failing tests — /compact.** With a scripted provider returning "SUMMARY-TEXT" for the summarize call after two completed turns: (a) `/compact` leaves `loop.history` as exactly one system message containing "SUMMARY-TEXT"; (b) the event log's last events include a `CompactionApplied` whose from_seq/to_seq span the prior message-bearing envelopes and whose summary is "SUMMARY-TEXT"; (c) reading the session back through `read_session` + `fold` yields the same single-summary-message state (round-trip contract); (d) a failing summarize call (scripted error) leaves history untouched, no event, error shown; (e) refused while a turn runs.
- [ ] **Step 4: Implement /compact.** Issue one completion through `loop`'s current model/provider with a fixed summarize instruction over the rendered history; on success append `CompactionApplied` via the session's event append path (message-bearing seq range: track or derive the envelope seqs of logged history messages — read fold.py's `_msg_seqs` handling and mirror what resume produces), then set `loop.history` to `[Message.system_text(f"Summary of earlier conversation: {summary}")]` exactly matching fold's replay shape (fold.py:95-97).
- [ ] **Step 5: Docs.** user-guide: `/clear` (fresh session, same wiring) and `/compact` (whole-transcript fold, resume-safe, event-sourced).
- [ ] **Step 6: Full suite, then commit** `feat(tui): /clear and /compact via kernel rebuild and CompactionApplied`.

### Task 4: Session lister + --continue + /resume picker

**Files:**
- Modify: `src/harness/log.py` (or a new `src/harness/sessions.py` if log.py idiom fits poorly — one clear responsibility: enumerate sessions)
- Modify: `src/harness/cli.py` (`--continue` flag beside `--resume`, ~line 404 area)
- Modify: `src/harness/tui.py` (`/resume` + picker modal in the ServerChecklistScreen idiom, ~141-177)
- Test: `tests/test_log.py` or new `tests/test_sessions.py`; `tests/test_cli.py`; `tests/test_tui.py`

**Interfaces:**
- Consumes: event-log file layout (read `EventLogWriter.__init__`, log.py:16-42, for the authoritative path scheme), `read_session`, Task 3's `_rebuild_kernel(resume_session_id=...)`.
- Produces: `list_sessions(base: Path, limit: int | None = None) -> list[SessionSummary]`; `SessionSummary` dataclass: `session_id: SessionId`, `mtime: float`, `event_count: int`, `first_prompt: str` (first user text, truncated to 80 chars, "" if none), `last_model: str | None`, `error: str | None` (set for torn/unreadable logs instead of omitting the row).

- [ ] **Step 1: Failing tests — lister.** Create two real session logs via the writer/loop test helpers plus one torn file: (a) newest-first ordering by mtime; (b) first_prompt and event_count correct; (c) torn log appears with `error` set; (d) empty base → [].
- [ ] **Step 2: Implement `list_sessions`.** Enumerate the writer's layout; parse each log with the tolerant reader; never raise on a bad file.
- [ ] **Step 3: Failing tests — --continue.** (a) with sessions present, `--continue` resolves to the newest session id and reaches build_kernel as resume_session_id (assert via the built kernel's resumed flag/session id, following test_cli.py's existing resume test idiom); (b) no sessions → clear SystemExit message; (c) `--continue` with `--resume` → argparse error.
- [ ] **Step 4: Implement --continue** in the arg parser + `_run_main` resolution.
- [ ] **Step 5: Failing tests — /resume picker.** (a) `/resume` shows the picker listing existing sessions (age + first prompt); (b) choosing one rebuilds onto that session: `session.id` matches, `loop.history` reflects the resumed transcript; (c) Escape cancels, session unchanged; (d) refused while a turn runs.
- [ ] **Step 6: Implement /resume** (ModalScreen[SessionId | None], priority Enter/Escape per ServerChecklistScreen) → `_rebuild_kernel(resume_session_id=chosen)`.
- [ ] **Step 7: Docs.** user-guide: `--continue`, `/resume`, and the session list semantics.
- [ ] **Step 8: Full suite, then commit** `feat(sessions): list_sessions, --continue, and a /resume picker`.

### Task 5: Status bar

**Files:**
- Modify: `src/harness/tui.py` (compose(), CSS, refresh hook at the run_rollup site ~339-349, `/model`, `/clear`, `/compact`, `/resume` paths)
- Test: `tests/test_tui.py`

**Interfaces:**
- Consumes: `run_rollup` output (cost, tool count — see tui.py:339-349), `_schema_token_estimate`, catalog resolution of the current `loop.model` (`max_input_tokens`, `local` tag fallback 16384).
- Produces: `#statusbar` Static docked above `#prompt`; `_refresh_statusbar()` callable from all mutation points.

- [ ] **Step 1: Failing tests.** (a) after a completed turn the bar contains the model name and the rollup's tool count; (b) with a catalog model active (make_app + catalog_path + /model), the bar shows a `ctx` percentage and a `$` cost; (c) in echo mode the bar omits ctx/cost segments; (d) after `/model` the bar shows the new alias without requiring a turn.
- [ ] **Step 2: Implement.** Compose the bar; compute context fill = (`_schema_token_estimate(registry specs)` + sum(len(m.text())//4 for history)) / max_input_tokens (skip segment when no catalog model). Call `_refresh_statusbar()` from turn end, `/model`, and the Task 3/4 rebuild rewire hook.
- [ ] **Step 3: Docs.** user-guide TUI section: status bar segments and their sources.
- [ ] **Step 4: Full suite, then commit** `feat(tui): persistent status bar (model · ctx · cost · tools)`.

### Task 6: @-file mentions + docs sweep

**Files:**
- Modify: `src/harness/tui.py` (`HistoryInput` at 178-196 for Tab completion; submit path at 368-396 for injection)
- Test: `tests/test_tui.py`
- Modify: `docs/user-guide.md`, `README.md` (final sweep)

**Interfaces:**
- Consumes: dispatcher read path — locate the registered read tool in `native_tools.py` and dispatch through `kernel.loop.dispatcher` so mention reads are evented and permission-gated (same outcome type the model's own read gets).
- Produces: Tab completion for `@`-prefixed tokens; per-turn context injection.

- [ ] **Step 1: Failing tests — completion.** With workspace files `alpha.py`, `alpha_beta.py`: (a) prompt `see @alp` + Tab completes to the first match and repeated Tab cycles; (b) Tab with no `@` token behaves as before (no regression on HistoryInput history).
- [ ] **Step 2: Implement completion.** Candidate list: `git ls-files` + untracked-non-ignored (subprocess at first use per turn-idle, cached, workspace-rooted; non-git workspace falls back to a bounded os.walk). Match = path substring, case-insensitive, tracked files first.
- [ ] **Step 3: Failing tests — injection.** (a) submitting `explain @alpha.py` produces a model call whose input contains alpha.py's contents AND the literal user text; the logged user message keeps only the literal text; (b) the read appears in the event log as a dispatched tool call (permission-gated: with a deny rule on the path, injection is refused and the refusal is visible, turn still runs with literal text); (c) a >16 KiB file is truncated with a note; (d) `@nope.txt` (missing) passes through as literal text with no error.
- [ ] **Step 4: Implement injection** in the submit path before the turn worker starts: parse `@`-tokens, dispatch reads, prepend a context block to the turn's model input (NOT to the persisted user message).
- [ ] **Step 5: Docs sweep.** user-guide: @-mentions section; README feature bullet list refreshed for the whole batch; verify every new command appears in `/help` and the guide's command table.
- [ ] **Step 6: Full suite, then commit** `feat(tui): @-file mentions with Tab completion and evented reads`.

### Task 7: Markdown rendering for completed replies

**Files:**
- Modify: `src/harness/tui.py` (turn-completion transcript write; `/markdown` in command dispatch ~413-427; help text)
- Test: `tests/test_tui.py`

**Interfaces:**
- Consumes: the existing turn-completion path that writes the assistant reply into the `RichLog` transcript (locate where `_stream_buffer`/final text lands after `_run_turn`).
- Produces: `HarnessApp._markdown_mode: bool` (default True, session-local); `_render_reply(text: str) -> RenderableType` — returns `rich.markdown.Markdown` when on, the existing `_plain(...)` Text when off; `/markdown on|off`.

- [ ] **Step 1: Failing tests.** (a) unit: `_render_reply("# hi")` returns a `rich.markdown.Markdown` instance by default and a plain `Text` after `/markdown off`; (b) e2e (make_app + scripted provider replying with a fenced code block + a table): the turn completes without error and the transcript write for the reply used the markdown path (assert via the seam, not pixel output); (c) `/markdown bogus` reports valid args; (d) thought summary lines (Task 2) and error lines still go through `_plain` — assert the seam is applied ONLY to assistant reply text.
- [ ] **Step 2: Implement.** Factor the reply-write into `_render_reply`; wire `/markdown`; streaming stays plain (`#live` untouched); the completed reply is written rendered. Update `/help`.
- [ ] **Step 3: Docs.** user-guide: markdown rendering default + `/markdown off` escape hatch; note streaming stays plain by design.
- [ ] **Step 4: Full suite, then commit** `feat(tui): render completed replies as markdown with /markdown on|off`.

### Task 8: Activity panel — Files / Agents / Workflows

**Files:**
- Create: `src/harness/tui_panel.py` (panel widget + event-fold helpers — keep tui.py from bloating; one responsibility: the activity panel)
- Modify: `src/harness/tui.py` (compose/mount the hidden panel, `/panel` command, key binding, help)
- Test: `tests/test_tui_panel.py` (fold helpers), `tests/test_tui.py` (integration)

**Interfaces:**
- Consumes: the session event stream the TUI already observes (read how `TelemetrySubscriber` taps events, tui.py:262-266, and use the same mechanism — do NOT invent a second event path); tool-completion events for the native read/write/edit tools and for `dispatch_agent`/`ensemble`/`consult_panel`/`escalate`; `McpHost` server-enabled knowledge (`kernel.mcp`) for the agent-swarm section; the dispatcher for explicit agent-swarm state fetches.
- Produces: `ActivityPanel` widget (Textual `TabbedContent`, tabs Files/Agents/Workflows); pure fold helpers `fold_files(events) -> list[FileRow]` (path, markers set ⊆ {R,W,E}, newest first, deduped) and `fold_agents(events) -> list[AgentRow]` (call id, label, status running|done|error, model|None, strategy grouping key|None); `/panel` toggle + key binding (choose one not already bound — check existing Bindings — and document it).

- [ ] **Step 1: Failing tests — folds.** Feed synthetic envelopes (build with the real event classes): (a) read+edit of the same path → one row, markers {R,E}; (b) a dispatch_agent proposal without completion → status running; with completion → done; with error outcome → error; (c) ensemble with two experts → rows share a strategy grouping key; (d) event order newest-first respected.
- [ ] **Step 2: Implement the fold helpers** in tui_panel.py as pure functions over envelopes.
- [ ] **Step 3: Failing tests — panel behavior.** (a) `/panel` toggles visibility; toggling during a scripted running turn leaves the turn unaffected (turn still completes); (b) after a turn that read a file, the Files tab shows the row; (c) with NO agent-swarm server enabled, the Workflows tab contains the mixture section but NO agent-swarm section (assert absence — the cleanly-disableable invariant); (d) opening the panel writes nothing to the event log (compare event counts before/after toggle).
- [ ] **Step 4: Implement the panel + /panel + binding.** Hidden by default; refresh tab contents from folds on toggle and turn end.
- [ ] **Step 5: Failing test — agent-swarm section.** With a fake MCP server registered under the agent-swarm name exposing a `workflow__workflow_get_state`-shaped tool (use the existing fake-MCP idiom from tests/test_tools_allow.py / test_mcp_host.py): opening the Workflows tab and pressing refresh dispatches the fetch THROUGH the dispatcher (event logged, permission-gated) and renders the returned state read-only; when the server is enabled but the fetch fails, the section shows the error without crashing the panel.
- [ ] **Step 6: Implement the agent-swarm section** behind the server-enabled guard; fetch on tab open + explicit refresh key only (never a timer).
- [ ] **Step 7: Docs.** user-guide: the panel, its tabs, the toggle, the agent-swarm section's enabled-only presence; README feature bullet.
- [ ] **Step 8: Full suite, then commit** `feat(tui): activity panel with files, agents, and workflows tabs`.
