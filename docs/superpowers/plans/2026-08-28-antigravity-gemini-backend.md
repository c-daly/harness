# Antigravity (Gemini) Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gemini as a first-class catalog model via the Antigravity CLI on Google-subscription OAuth — the third subscription-CLI backend.

**Architecture:** `AntigravityProvider` mirrors `provider_codex.py`'s lifecycle (per-turn McpToolServer → scratch isolation → spawn CLI → parse JSONL → teardown in finally), with the departures the spec names: HOME-override scratch (agy has no config-dir env var), `agy mcp add` subprocess registration, prompt over stdin with `-p` orientation prefix, `--dangerously-skip-permissions` flag instead of config seeding.

**Tech Stack:** Python 3.13, asyncio subprocess, existing `McpToolServer` (mcp 1.27.2 — NO SDK bump), pytest fake-CLI-script harness.

**Spec:** docs/superpowers/specs/2026-08-28-antigravity-gemini-backend-design.md — read it first; its Global Constraints bind every task. Probe-verified wire facts live in the spec; do not re-derive them.

## Global Constraints

(Copied from the spec — every task's requirements include these.)
- No per-token API billing; subscription creds only; the harness never reads/stores/transmits credential contents beyond the mechanical per-turn copy (removed in finally).
- No ThinkingDelta emission in v1 (agy exposes no thought text).
- Every per-turn resource destroyed in finally: child process group, McpToolServer, scratch HOME, scratch cwd. Create-inside-try, None-guarded cleanup, scratch-builder cleans its half-built dir on internal failure, nested try/finally so gen-close cannot skip cleanup (the full post-review codex shape — see provider_codex.py at HEAD, which carries all of it).
- `_sanitized_env` strips `_SECRET_ENV_KEYS` and adds `GOOGLE_API_KEY` to that set; child env sets `HOME=<scratch home>`.
- Tests RED-first against pre-task code; full unfiltered suite (`.venv/bin/python -m pytest`) before every final commit; docs sync in the task that ships the behavior.
- Live verification uses REAL subscription turns sparingly (one per contract, not per test run).

---

### Task 1: AntigravityProvider

**Files:**
- Create: `src/harness/provider_antigravity.py`
- Test: `tests/test_provider_antigravity.py`

**Interfaces:**
- Consumes: `McpToolServer` (mcp_serve.py, unchanged), `current_dispatch_tool` (dispatcher.py), `Chunk` taxonomy (provider.py: TextDelta/UsageReport/StreamStop), `ProviderError`/`MalformedStreamError` (errors.py — check exact names/module at head), `_render_prompt`-style message rendering (copy the codex/claude idiom).
- Produces: `class AntigravityProvider` with `bind_dispatcher(dispatcher)` and `async complete(*, model, messages, tools) -> AsyncIterator[Chunk]`; module constant `_AUTH_FILES` listing the exact per-turn copies (spec's two lists); `_scratch_home()` builder.

- [ ] **Step 1: Read the reference.** Read `src/harness/provider_codex.py` at head IN FULL — it is the reviewed, post-fix-wave shape this provider mirrors (setup-inside-try, None-guarded finally, nested aclose try/finally, process-group kill, stderr drain, orientation prefix). Read the spec's event-mapping and departures sections.
- [ ] **Step 2: Failing tests — fake agy script.** Build a fake `agy` shell/python script fixture emitting probe-shape JSONL (init → agent_response step with text_delta + usage → result SUCCESS with usage). The fake must also handle being invoked as `agy mcp add ...` (exit 0, record argv to a file) vs the turn invocation. Contracts, each its own test: (a) TextDelta yielded for each agent_response text_delta, in order; (b) UsageReport maps input/output from result.usage and StreamStop follows; (c) result.status ERROR → ProviderError carrying result.error; (d) EOF without result → MalformedStreamError; (e) non-JSON lines skipped; (f) tool-step events yield nothing; (g) argv contract: `-p` orientation prefix present, `--output-format stream-json`, `--dangerously-skip-permissions`, `--print-timeout`, `--model <suffix>` present iff route carries one (`antigravity/gemini-x` → suffix; `antigravity/default` → absent); (h) prompt arrives on STDIN (fake echoes stdin to a file; assert rendered transcript there); (i) env contract: child env HOME points into the scratch, `_SECRET_ENV_KEYS` (incl. GOOGLE_API_KEY) absent; (j) `agy mcp add` invoked with `-t http harness <url>/` before the turn, in the scratch HOME env; (k) scratch HOME contains copies of exactly the spec's auth-file lists (fake source home fixture) and is REMOVED after the turn, as is the scratch cwd; (l) kill-on-timeout: a hanging fake → process group dead, cleanup ran; (m) kill-on-abandonment: closing the generator early → same; (n) setup failure AFTER server start (monkeypatch mkdtemp to raise) → server.stop() awaited (fake McpToolServer records it) — the codex-leak regression contract.
- [ ] **Step 3: Implement** `provider_antigravity.py` per spec. Structure mirror: module docstring (wire facts + honest trust-model note), `_SECRET_ENV_KEYS` import/extend, `_sanitized_env`, `_scratch_home()` (copies the two auth-file lists; cleans half-built dir on internal failure), `_argv(...)`, `complete()` with the full codex-shaped lifecycle; `agy mcp add` via `asyncio.create_subprocess_exec` (NEVER blocking subprocess.run — the probe-harness lesson). Run tests: all pass.
- [ ] **Step 4: Live verification (one real turn).** With the real `agy` and real auth (copy from the REAL ~/.gemini per spec), run one `complete()` turn through a real McpToolServer with a `read_file`-like spec dispatching to a stub: assert a TextDelta arrives and the stub was called. Record the transcript facts (incl. whether stdin prompt delivery worked; if not, implement the spec's argv fallback with its 100KiB guard and update tests) in the report. Skip-marked (`pytest.mark.skipif` on agy/auth absence) so CI stays hermetic.
- [ ] **Step 5: Full suite, commit** `feat: AntigravityProvider — Gemini turns via agy on subscription OAuth`.

### Task 2: Catalog + wiring + docs

**Files:**
- Modify: `src/harness/catalog.py` (KNOWN_BACKENDS), `src/harness/provider_litellm.py` (CatalogProvider slot + branch + bind_dispatcher), `src/harness/cli.py` (BOTH construction sites), `src/harness/tui.py` (`_switch_model`'s construction site)
- Test: `tests/test_catalog_backend.py`, `tests/test_backend_dispatch.py`, `tests/test_tui.py`
- Modify: `docs/user-guide.md`, `docs/architecture.md`, `README.md`

**Interfaces:**
- Consumes: `AntigravityProvider` (Task 1).
- Produces: `backend = "antigravity"` resolvable end-to-end; all construction sites wire `antigravity=AntigravityProvider()`.

- [ ] **Step 1: Enumerate construction sites by grep** (`grep -n "CatalogProvider(" src tests`) — the T2 lesson from the claude backend: a missed site ships a "backend not wired" runtime error. List them in your report.
- [ ] **Step 2: Failing tests.** (a) catalog: `backend = "antigravity"` resolves; unknown backends still raise UnknownBackendError; (b) dispatch: a CatalogProvider with an antigravity fake routes an `antigravity/...` alias to it, passes the route through, and errors with the established "needs the antigravity backend, which is not wired" message when the slot is None; bind_dispatcher forwards to all three backend slots; (c) tui: extend `test_slash_model_upgrade_wires_claude_code_backend`'s pattern — after /model upgrade the CatalogProvider has `antigravity is not None`.
- [ ] **Step 3: Implement** the four source files. The tui/cli sites go through the existing `Kernel.set_provider`/construction paths — do not invent new wiring.
- [ ] **Step 4: Docs.** user-guide: an "Antigravity (Gemini) backend" section parallel to the codex one — subscription OAuth via `agy` login, per-turn scratch HOME + auth-file copy (credentials posture sentence, codex-precise wording), the HONEST trust model (57 built-ins active; scratch steers, does not confine; unlike codex there is NO read-only sandbox in v1 — agy built-ins can read AND write outside the scratch; --sandbox/--mode plan named as the fast-follow), catalog snippet, requirements (agy on PATH, logged in). architecture.md module inventory + backend row. README: tri-backend bullet updated.
- [ ] **Step 5: Full suite, commit** `feat: antigravity backend — catalog, dispatch, TUI wiring, docs`.

### Task 3: Live end-to-end + user catalog

**Files:**
- Modify: `~/.config/harness/models.toml` (user config, not repo)
- Test: none new (uses the shipped suite + live turns)

- [ ] **Step 1:** Add the spec's `[models.gemini]` entry to the user catalog (backend antigravity, route `antigravity/gemini-3.7-flash-high`).
- [ ] **Step 2: Headless live turn:** `.venv/bin/harness -p "Use your tools to read pyproject.toml's first line" --model gemini` — assert the session event log shows `tool_call_proposed`/`tool_call_completed` for the read and `model_call_completed model=gemini`, and the reply reflects real file content.
- [ ] **Step 3:** Stamp `verified = true` with date + method comment in models.toml.
- [ ] **Step 4:** TUI smoke: scripted pilot run or headless equivalent confirming `/model gemini` resolves and dispatches (reuse the existing test-idiom if a live TUI drive is impractical overnight; note whichever was done).
- [ ] **Step 5:** Ledger the live-verification transcript facts; commit anything repo-side that changed (docs corrections only — models.toml is user config, uncommitted).
