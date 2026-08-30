# Antigravity (Gemini) Subscription-CLI Backend — Design

Date: 2026-08-28 · Status: approved-in-direction (Chris 2026-08-24: "gemini and
chat gpt will be subscription creds"; overnight execution authorized 2026-08-28)
· Branch: antigravity-backend

The third subscription-CLI model backend, completing the original tri-model
goal (Claude, Codex, Gemini interchangeably behind the harness's own loop and
tools). Mirrors the codex backend pattern wherever the wire allows; departures
are listed explicitly. All wire facts below were live-probed 2026-08-28
against agy 1.1.22 (see memory: antigravity-cli-wire-facts-for-gemini-backend).

## Why Antigravity, not gemini-cli or an API key

Google server-side-killed gemini-cli for individual accounts
(IneligibleTierError, any client version). The Antigravity CLI (`agy`) is its
successor: Google-account OAuth (subscription creds, no per-token billing —
the binding constraint), and Chris's login exposes Gemini 3.7/3.6/3.5 flash
tiers and 3.1 pro (plus Claude and GPT-OSS models, out of scope for v1
routing but reachable via route suffix).

## Architecture

New `AntigravityProvider` in `src/harness/provider_antigravity.py`, a sibling
of `CodexProvider` with the same lifecycle contract:

1. Per turn: start a `McpToolServer` (UNCHANGED, mcp 1.27.2 — probe-verified
   agy negotiates with it as-is) exposing the turn's ToolSpecs, dispatching
   through `current_dispatch_tool.get() or self._dispatch`.
2. Build a scratch HOME (`tempfile.mkdtemp`, 0700) — NOT just a scratch
   config dir: agy has no home-relocation env var, so the child env sets
   `HOME=<scratch>`. Inside it, `.gemini/` receives copies of exactly:
   `oauth_creds.json, google_accounts.json, installation_id, settings.json,
   projects.json, state.json` and `.gemini/antigravity-cli/` receives
   `antigravity-oauth-token, installation_id, settings.json` (the oauth token
   is the operative credential; missing → "authentication required").
   Missing source files are skipped silently (agy recreates what it needs).
3. Register the tool server: run `agy mcp add -t http harness <url>/` with
   the scratch HOME env — writes only scratch config; the real ~/.gemini is
   never touched (probe-verified with mtime comparison).
4. Run the turn: `agy -p - is NOT supported (no stdin-prompt flag probed);
   the prompt is passed as the -p argument value` — see Departures — with
   `--output-format stream-json --dangerously-skip-permissions
   --print-timeout <turn timeout>`, cwd = a scratch work dir, env sanitized
   (same `_SECRET_ENV_KEYS` strip as the other backends) + `HOME` override.
   `-m/--model <suffix>` passed when the catalog route carries one.
5. Parse stdout JSONL; kill the process group on timeout/abandonment
   (start_new_session=True, same as codex); stderr drained concurrently.
6. Teardown in finally: kill child if alive, stop the McpToolServer,
   rmtree scratch HOME and scratch cwd (create-inside-try, None-guarded —
   the post-review codex shape, including _scratch cleanup-on-internal-
   failure and nested try/finally around gen.aclose()-equivalent).

### Event mapping (stream-json, probe-verified shapes)

- `step_update.step_type == "agent_response"` with `text_delta` →
  `TextDelta(text=...)`. Steps without text_delta (pure thinking) → nothing.
  The provider emits NO ThinkingDelta in v1: agy exposes thinking token
  COUNTS but never thought text, and an empty ThinkingDelta would render a
  useless "(thought for Ns · 0 chars)" line in the TUI.
- `result.status == "SUCCESS"` → UsageReport mapping
  `input=usage.input_tokens, output=usage.output_tokens,
  cache_read=usage.cache_read_tokens` (harness Usage has no thinking field;
  thinking_tokens are not folded in), then StreamStop.
- `result.status == "ERROR"` → ProviderError(f"antigravity: {result.error}").
- Process EOF without a `result` event → MalformedStreamError.
- Non-JSON lines skipped (agy may interleave noise).
- `step_type == "tool"` events are informational (the tool call itself
  arrives via the MCP server → dispatcher); the parser ignores them.

### Catalog integration

- `KNOWN_BACKENDS` gains `"antigravity"`.
- `CatalogProvider` gains an `antigravity:` slot, `bind_dispatcher` forwards,
  and a `backend == "antigravity"` branch that passes
  `model=resolved.route` (the provider itself splits `antigravity/<suffix>`:
  suffix present → `--model <suffix>`, bare `antigravity/default` → no flag).
- All THREE CatalogProvider construction sites (cli.py ×2, tui.py
  `_switch_model` via `Kernel.set_provider`) wire
  `antigravity=AntigravityProvider()` — enumerate by grep, the T2 lesson.
- User catalog entry:
  ```toml
  [models.gemini]
  backend = "antigravity"
  route = "antigravity/gemini-3.7-flash-high"
  input_cost_per_token = 0.0
  output_cost_per_token = 0.0
  tags = ["google", "subscription", "tool-calling"]
  ```

### Trust model (documented honestly, the codex lesson applied from day one)

agy's 57 built-in tools (run_command, write_to_file, browser_*, view_file,
…) are NOT disabled — tool parity is additive, exactly like codex. The
scratch cwd + scratch HOME steer; they do not confine. `--sandbox` and
`--mode plan` exist but are UNPROBED for their interaction with MCP calls —
v1 ships without them and the docs say plainly: the harness permission
engine gates harness-tool calls; agy's built-ins can read (and, unlike
codex's read-only sandbox, potentially WRITE) outside the scratch dirs.
A `--sandbox`/`--mode plan` investigation is a named fast-follow. The
orientation prompt prefix (codex pattern) directs work through harness
tools.

### Credentials posture

The harness never reads, stores, or transmits credential contents; it
mechanically copies the auth files into the per-turn scratch HOME (removed
in finally). Same ruling as codex's auth.json, documented in the guide the
same way.

## Departures from the codex pattern (each deliberate)

1. Prompt delivery: the rendered prompt goes over STDIN, with a short
   orientation string as the `-p` argument value — agy documents `-p` as
   "appended to input on stdin (if any)", so stdin carries the transcript
   (avoiding the argv-visibility/MAX_ARG_STRLEN problems the codex/claude
   backends already solved this way) and `-p` carries the orientation
   prefix. This exact combination is unprobed: Task 1 includes a mandatory
   live verification step, and if stdin delivery proves unreliable the
   fallback is the full prompt as the `-p` value with a documented 100KiB
   guard raising ProviderError. The test suite pins whichever form ships.
2. HOME override instead of a config-dir env var (agy has none).
3. `agy mcp add` subprocess for registration instead of a config-file seed
   (agy's config format is undocumented; the CLI is the stable interface).
   One extra ~1s subprocess per turn — acceptable v1.
4. No approvals config seeding needed: `--dangerously-skip-permissions` is a
   flag, not config (verified: init reports permission_mode always-proceed).

## Global Constraints

- No per-token API billing anywhere; subscription creds only.
- The harness never implements Google OAuth or handles credential contents
  beyond the mechanical per-turn file copy (removed in finally).
- Thought text must never enter loop history or assembled message text; agy
  exposes no thought text, so the provider emits NO ThinkingDelta in v1.
- Every resource created per turn is destroyed in finally: child process
  group, McpToolServer, scratch HOME, scratch cwd. Create-inside-try,
  None-guarded cleanup, cleanup-on-internal-failure in any multi-step
  scratch builder (the full post-review codex shape).
- `_sanitized_env` strips `_SECRET_ENV_KEYS` (add `GOOGLE_API_KEY` to the
  set if absent) and then sets `HOME` to the scratch.
- Docs sync is part of every task (user-guide backend section with the
  honest trust model, architecture module inventory, README bullet).
- Tests RED-first; the fake-agy test harness mirrors tests/test_provider_codex.py's
  fake-CLI-script idiom (a script emitting probe-captured JSONL shapes).
- The catalog `verified` flag for [models.gemini] is stamped only after a
  live tool-call round-trip through the dispatcher (the standard bar).

## Non-goals (v1)

Routing to Antigravity's Claude/GPT-OSS models (works via route suffix, but
untested and pointless while native backends exist); --sandbox/--mode plan
restriction; thinking-text surfacing; stream-json INPUT mode (multi-turn
NDJSON); reusing one agy conversation across harness turns (stateless v1,
like codex); mcp SDK 2.x migration (works on 1.27.2; 2.x is a separate
follow-up with its own motivation).
