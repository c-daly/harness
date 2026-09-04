# Harness Production-Beta Hardening Implementation Plan

> **For implementers:** Execute this plan task by task, in order. Every code
> task is test-first, independently committable, and must leave the full suite
> green. Do not combine tasks into a large branch. After each implementation
> task, perform a spec-compliance review and a code-quality review before
> committing. Tasks 19B-19C additionally follow their immutable-candidate
> procedure; do not commit generated qualification evidence into its subject.

**Goal:** Turn Harness from a strong internal alpha into a dependable local
production tool whose provider, tool, permission, event, recovery, and UI
semantics remain explicit and consistent when the selected model changes, and
whose core interactive loop is responsive, legible, interruptible, and
pleasant enough for sustained daily work.

**Initial production scope:** One trusted local user; one Harness process per
active top-level session; Linux and WSL2; Python 3.12 and 3.13; API/local models
through LiteLLM plus subscription-CLI adapters that satisfy the provider
contract below. Native Windows, macOS, multi-user service operation, untrusted
plugin execution, and public package publication are not part of this plan.

**Architecture:** Preserve the event log as the source of truth and the
dispatcher as the sole enforcement point. Add a typed provider contract and an
execution-profile gate, then make provider adapters, security, recovery,
resource control, and the TUI consume that contract. A provider that cannot
meet the production contract remains usable only in an explicitly lower-trust
profile; it must never silently weaken the meaning of “production.”

**Tech stack:** Python 3.12+, Pydantic v2, asyncio, Textual, LiteLLM, MCP,
pytest, Ruff, Hatchling, uv.

**Review revision (2026-09-03):** This version incorporates the validated
findings from an independent Claude Code review. In particular, it separates
provider transport from model-native network access, moves containment proof
out of replay fixtures, prevents nested-coordination semaphore deadlocks,
adds telemetry-schema and resume/profile transitions, introduces a concrete
release support floor, and splits the largest security/UI/release tasks into
independently executable changes.

**Playability revision (2026-09-03):** Correctness and UI completeness are not
accepted as proxies for usability. Task 18C adds measurable interaction
budgets, prompt queueing, visible progress, low-risk retry/edit recovery, a
clean-config first-success journey, and an exact-candidate dogfood gate.

> **PLAYABILITY ADDITIONS — review map**
>
> - **Product contract:** the Goal and locked decision 16 now make playability
>   a release property and separate Harness latency from provider latency.
> - **Existing UI tasks:** Task 15 now uses progressive disclosure and guided
>   first-run setup; Task 16 keeps the composer editable during active work.
> - **New implementation tranche:** all of Task 18C is new, including activity
>   phases, prompt queueing, safe retry/edit recovery, fixed p95 interaction
>   budgets, six dogfood journeys, and usability-severity rules.
> - **Release enforcement:** Tasks 19A and 19B now validate deterministic UX
>   behavior, exact-candidate timing, terminal behavior, and the 90-minute
>   real-work session. The production-beta definition of done contains the new
>   blocking playability criteria.

**Current verified baseline (2026-09-03, `main` at `ce722b4`):**

- `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q`: 885 passed, 7 skipped.
- `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .`: fails on two E741 names in
  `tests/test_tui.py:442,444`.
- `UV_CACHE_DIR=/tmp/uv-cache uv build`: sdist and wheel succeed.
- A live no-MCP/no-plugin TUI smoke test rendered startup, user input, an echo
  response, and exited cleanly.
- Existing untracked `.claude/` and `.context/` directories belong to the user.
  Never stage, alter, or delete them.

---

## Locked product and engineering decisions

These decisions remove ambiguity for the implementer. If one must change,
update this plan first and obtain review before writing code that contradicts
it.

1. **Three execution profiles:**
   - `production`: fail closed unless the effective provider/model has verified
     containment, a complete required capability set, and a supported adapter
     or CLI version.
   - `compatible`: allow known capability gaps, but reject an operation before
     dispatch if that operation needs an unsupported capability.
   - `unsafe`: allow a steered or unknown provider after an explicit warning.
2. **Four provider tiers:** `strict`, `contained`, `steered`, `experimental`.
   `production` accepts only `strict` and `contained`.
3. **No silent degradation.** Unsupported images, structured content, spilled
   tool results, malformed arguments, and provider version drift produce
   typed, visible failures. A provider may explicitly declare token or cost
   accounting absent; that declaration is displayed once and unknown values
   remain unknown, never zero. If a requested hard budget depends on an absent
   metric, refuse before starting the provider rather than failing every call.
4. **Literal model routes are not production-verifiable.** A raw route that is
   absent from the catalog remains available in `unsafe`; `production` rejects
   it because no stored capability or verification record can be attached.
5. **The provider executable is trusted code.** The containment threat model is
   provider-native model tools and their subprocesses, not a deliberately
   malicious replacement binary. Plugin Python is also trusted only after the
   user records an explicit local trust decision.
6. **Production is not the compatibility default until the final release task.**
   Add the profile machinery with `compatible` as the migration default. Task
   19B may prepare an untagged candidate with the new default only after every
   entry in the shipped release support floor passes its required live and
   replay evidence. Keep
   `--profile compatible` and `--profile unsafe` as explicit escape hatches.
7. **Event changes are additive only.** New event fields have defaults. New
   native kernel facts get typed events and are added to the closed union. Old
   logs must parse and fold exactly as before.
8. **Tool implementations keep semantic validation.** Central JSON Schema
   validation is an additional boundary, not permission to delete tool-level
   checks for filesystem confinement, cross-field conditions, or teaching
   errors.
9. **The session log remains canonical.** SQLite, TUI panels, live status, and
   diagnostic reports are projections that can be rebuilt from JSONL plus the
   blob store.
10. **No public PyPI publication in this plan.** The release task produces and
    signs local/GitHub artifacts. Choosing a license and publication target is
    an owner decision outside this implementation plan.
11. **There are two network boundaries.** The trusted provider transport may
    need outbound provider API access and loopback access to Harness MCP. That
    is distinct from provider-native model tools receiving arbitrary network
    access. Capability types, probes, policy, and UI must report these two
    boundaries separately. Beta containment does not claim to restrict the
    trusted CLI's own provider egress.
12. **Resume never weakens trust silently.** Resuming under a stronger profile
    is allowed and recorded. Resuming under a weaker profile requires an
    explicit downgrade confirmation/flag and records both the current and
    weakest profile ever used for the session.
13. **Headless `Ask` never waits for a human.** It resolves to deny and records
    the ordinary denied tool/model fact. Exit code 4 is reserved for a refused
    top-level administrative or pre-turn operation; a denied tool call inside
    an otherwise valid agent turn remains a tool result. TUI subagent asks are
    shown in the parent UI with agent identity; headless subagents deny.
14. **Explicit CLI intent may override stored budget defaults.** Package
    defaults are replaced by user config; project config may only narrow them.
    A CLI value may raise/remove a stored limit only with
    `--allow-budget-increase`, and the effective limits plus override are
    recorded. Agent definitions may only narrow the resulting limits.
15. **The active session has one append owner.** Async tasks on the owning event
    loop may append because `Session.append` has no await point. Direct
    cross-thread append is rejected; integrations that originate on another
    thread must schedule onto the owner loop. Sequence assignment and publish
    therefore have a stated concurrency invariant.
16. **PLAYABILITY ADDITION — playability is a release property.** A technically
    correct action that leaves the user waiting without visible state, discards
    composed input, requires routine config-file editing, or makes recovery
    unnecessarily laborious is a product failure. Provider latency is measured
    separately from Harness-added latency so slow providers cannot excuse a
    sluggish UI and fast providers cannot hide an unresponsive one.

---

## Dependency order

```text
Task 1  clean baseline / CI
  |
Task 2  provider lifecycle + terminal model-call facts
  |
Task 3  CompletionRequest + ProviderStatus contract
  |
Task 4  execution profiles + recorded enforcement
  |\
  | +-- Task 5  compatibility manifests, probes, doctor
  | +-- Task 6A durable artifact/tool-result semantics
  |      Task 6B provider transcript/content fidelity
  | +-- Task 7  tool schema + risk contract
  |       |
  +-------+-- Task 8  provider conformance and adversarial corpus
                  |
               Task 9A containment primitives + live proof
                  |
               Task 9B subscription-CLI integration/classification

Tasks 10-13 follow Task 9B and consume Tasks 2-7:
  10 durable storage/recovery
  11A redaction/process environment
  11B outward MCP authentication
  12 permission persistence/plugin trust
  13 shared budgets/concurrency

Tasks 14-18D consume the recorded contracts above:
  14 loss-aware live projections
  15 TUI identity/onboarding/model switching
  16 multiline composer/permission review
  17A activity/tool/model surfaces
  17B session inspection/recovery surfaces
  18A compositor/accessibility matrix
  18B plain terminal frontend
  18C playable interactive loop and measured UX budgets
  18D final provider fixture/containment evidence renewal

Tasks 19A-19C are deterministic release tooling, construction/qualification of
an untagged candidate, and the immutable tag/promotion gate. They must be last
and run in that order.
```

---

## Global implementation rules

- Use TDD. Add the named failing test, run it and verify the intended failure,
  then implement only enough to pass it.
- For each task, run the targeted test, the whole affected test module, Ruff,
  and finally the complete suite before commit.
- Use `UV_CACHE_DIR=/tmp/uv-cache` when the default uv cache is not writable.
- Socket-binding tests may require unrestricted local execution. Do not weaken
  or skip them merely because a sandbox disallows `127.0.0.1:0`.
- Never pipe the final pytest invocation; report its real summary and exit code.
- Preserve `.claude/`, `.context/`, and unrelated dirty work.
- Keep documentation synchronized in the task that changes behavior.
- Every new CLI error must state what failed, why, and the corrective action.
- Every cap or truncation must disclose the original size, retained size, and
  how the user can obtain the complete content.
- Every provider-specific claim must name the adapter and detected version.
- **Playability addition:** Every user-visible asynchronous operation must
  expose a compact current phase, elapsed time after two seconds, a
  cancellation/recovery path when one exists, and a terminal outcome. Do not
  use an indefinite spinner as the only explanation of active work.
- Any change to a provider adapter, provider transcript, process environment,
  process sandbox, outward MCP server/config, or shared provider contract
  changes the verification-surface hash and automatically makes prior
  conformance/containment records unverified. Do not copy a prior hash forward.
  Task 18D deliberately renews all required records after the last such change.
- Before each commit:

  ```bash
  UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .
  UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q
  git diff --check
  git status --short
  ```

## Targeted verification command by task

Run the listed command after the focused RED/GREEN cycle and before the global
pre-commit commands above. A task is not complete merely because this targeted
command passes.

| Task | Targeted command |
|---|---|
| 1 | `uv run pytest -q tests/test_package_metadata.py tests/test_tui.py` |
| 2 | `uv run pytest -q tests/test_events.py tests/test_fold.py tests/test_resume.py tests/test_dispatcher.py tests/test_telemetry.py tests/test_provider_claude_code.py tests/test_provider_codex.py tests/test_provider_antigravity.py` |
| 3 | `uv run pytest -q tests/test_provider.py tests/test_provider_litellm.py tests/test_provider_claude_code.py tests/test_provider_codex.py tests/test_provider_antigravity.py tests/test_backend_dispatch.py tests/test_catalog_provider.py tests/test_dispatcher.py tests/test_subagent.py tests/test_mixture.py tests/test_tui.py` |
| 4 | `uv run pytest -q tests/test_runtime_policy.py tests/test_events.py tests/test_cli.py tests/test_dispatcher.py tests/test_telemetry.py` |
| 5 | `uv run pytest -q tests/test_diagnostics.py tests/test_catalog.py tests/test_catalog_backend.py tests/test_catalog_provider.py tests/test_backend_dispatch.py tests/test_package_metadata.py` |
| 6A | `uv run pytest -q tests/test_messages.py tests/test_blobs.py tests/test_mcp_host.py tests/test_artifact_repair.py` |
| 6B | `uv run pytest -q tests/test_provider_transcript.py tests/test_provider_litellm.py tests/test_provider_claude_code.py tests/test_provider_codex.py tests/test_provider_antigravity.py tests/test_e2e_provider_switch.py` |
| 7 | `uv run pytest -q tests/test_tools.py tests/test_dispatcher.py tests/test_native_wiring.py tests/test_mcp_host.py tests/test_plugins.py` |
| 8 | `uv run pytest -q tests/conformance tests/test_provider_litellm.py tests/test_provider_claude_code.py tests/test_provider_codex.py tests/test_provider_antigravity.py` |
| 9A | `uv run pytest -q tests/test_process_sandbox.py tests/containment/test_live_boundary.py tests/test_diagnostics.py` |
| 9B | `uv run pytest -q tests/containment tests/test_provider_claude_code.py tests/test_provider_codex.py tests/test_provider_antigravity.py` |
| 10 | `uv run pytest -q tests/test_storage.py tests/test_log.py tests/test_blobs.py tests/test_resume.py tests/test_sessions.py tests/test_session_admin.py tests/test_cli.py tests/test_diagnostics.py` |
| 11A | `uv run pytest -q tests/test_redaction.py tests/test_process_env.py tests/test_native_bash.py tests/test_mcp_host.py tests/test_provider_claude_code.py tests/test_provider_codex.py tests/test_provider_antigravity.py` |
| 11B | `uv run pytest -q tests/test_mcp_serve.py tests/test_provider_claude_code.py tests/test_provider_codex.py tests/test_provider_antigravity.py tests/test_process_env.py` |
| 12 | `uv run pytest -q tests/test_permissions.py tests/test_plugin_trust.py tests/test_plugins.py tests/test_cli.py` |
| 13 | `uv run pytest -q tests/test_budgets.py tests/test_dispatcher.py tests/test_loop.py tests/test_subagent.py tests/test_mixture.py tests/test_cli.py tests/test_telemetry.py` |
| 14 | `uv run pytest -q tests/test_session.py tests/test_projection.py tests/test_telemetry.py tests/test_tui.py tests/test_tui_panel.py tests/test_plugins.py` |
| 15 | `uv run pytest -q tests/test_tui_support.py tests/test_tui.py` |
| 16 | `uv run pytest -q tests/test_tui_support.py tests/test_tui.py` |
| 17A | `uv run pytest -q tests/test_tui_support.py tests/test_tui_panel.py tests/test_tui.py` |
| 17B | `uv run pytest -q tests/test_sessions.py tests/test_session_admin.py tests/test_tui.py tests/test_cli.py` |
| 18A | `uv run pytest -q tests/test_tui_compositor.py tests/test_tui.py tests/test_tui_panel.py` |
| 18B | `uv run pytest -q tests/test_plain.py tests/test_cli.py tests/test_permissions.py` |
| 18C | `uv run pytest -q tests/test_playability.py tests/test_tui.py tests/test_tui_panel.py tests/test_plain.py tests/test_cli.py` |
| 18D | `uv run pytest -q tests/conformance tests/containment tests/test_diagnostics.py tests/test_backend_dispatch.py` |
| 19A | `uv run pytest -q tests/test_release_check.py tests/test_release_gate.py tests/test_package_metadata.py tests/test_playability.py tests/conformance tests/test_tui_compositor.py` |
| 19B | `uv run pytest -q tests/qualification tests/containment tests/test_playability.py tests/test_tui_compositor.py tests/test_cli.py tests/test_diagnostics.py tests/test_package_metadata.py tests/test_storage.py tests/test_resume.py` |
| 19C | `uv run pytest -q tests/test_release_gate.py tests/test_cli.py tests/test_diagnostics.py tests/test_package_metadata.py` |

Prefix commands with `UV_CACHE_DIR=/tmp/uv-cache` where required by the local
environment. Tasks 2, 8, 9A, 9B, and 11B include loopback/socket tests and must be
rerun with unrestricted local socket access when the execution sandbox blocks
binding; a sandbox-induced failure is not evidence of a product failure or a
reason to skip the test.

---

### Task 1: Establish a clean, reproducible CI and package baseline

**Purpose:** Make “green” a real release prerequisite before changing runtime
contracts.

**Files:**

- Modify: `tests/test_tui.py:442-444`
- Modify: `pyproject.toml`
- Create: `.github/workflows/ci.yml`
- Create: `tests/test_package_metadata.py`
- Create: `scripts/smoke_wheel.sh`
- Modify: `docs/contributing.md`

**Interfaces produced:**

- `harness.__version__`, sourced from installed package metadata rather than a
  second hand-maintained version constant.
- `harness --version` in Task 5 consumes this interface; Task 1 only establishes
  and tests the metadata accessor.
- One CI job matrix over Python 3.12 and 3.13.

- [ ] **Step 1: Repair the current lint failure.** Rename the two generator
  variables `l` to `line` in `tests/test_tui.py`. Run
  `uv run ruff check .`; verify zero findings.
- [ ] **Step 2: Add package metadata tests.** In
  `tests/test_package_metadata.py`, assert that `importlib.metadata.version("harness")`
  is a valid PEP 440 version, `harness.__version__` equals it, and the console
  entry point named `harness` resolves to `harness.cli:main`. Task 5 adds the
  package-resource assertion only after `provider_compat.toml` exists; do not
  add a temporary skip or xfail.
- [ ] **Step 3: Implement `harness.__version__`.** In `src/harness/__init__.py`,
  call `importlib.metadata.version("harness")`; fall back to `"0+unknown"` only
  when `PackageNotFoundError` is raised from an unpackaged source import.
- [ ] **Step 4: Add the CI workflow.** Use `actions/checkout`,
  `astral-sh/setup-uv`, `uv sync --frozen --extra dev`, `uv run ruff check .`,
  `uv run pytest -q`, and `uv build`. On Python 3.13, install the built wheel
  into a fresh uv venv and run `harness --help`. Do not cache session data,
  credentials, or `.harness` directories.
- [ ] **Step 5: Add `scripts/smoke_wheel.sh`.** The script accepts exactly one
  wheel path, creates a temporary directory with `mktemp -d`, installs the
  wheel into a temporary uv venv, runs `harness --help`, imports every
  `src/harness/*.py` module that is safe to import, and removes the tempdir via
  a trap. It must not modify the checkout.
- [ ] **Step 6: Pin CI commands in `docs/contributing.md`.** Document the frozen
  sync, lint, full test, build, and wheel-smoke sequence. Update the stale
  approximate suite duration rather than promising a fixed time.
- [ ] **Step 7: Verify locally.** Run the package metadata test, Ruff, full
  suite, `uv build --out-dir /tmp/harness-dist`, and the smoke script against
  the built wheel.
- [ ] **Step 8: Commit** `build: establish reproducible CI and wheel smoke gate`.

**Acceptance:** CI can reproduce the same lint, test, build, and clean-install
result from a clean checkout. No production task may merge while this workflow
is red.

---

### Task 2: Close every model-call intent and standardize provider cleanup

**Purpose:** Ensure model calls have terminal facts under success, policy
denial, provider failure, timeout, task cancellation, and process death.

**Files:**

- Modify: `src/harness/events.py`
- Modify: `src/harness/fold.py`
- Modify: `src/harness/resume.py`
- Modify: `src/harness/dispatcher.py`
- Modify: `src/harness/telemetry.py`
- Modify: `src/harness/mcp_serve.py`
- Modify: `src/harness/provider_claude_code.py`
- Modify: `src/harness/provider_codex.py`
- Modify: `src/harness/provider_antigravity.py`
- Test: `tests/test_events.py`
- Test: `tests/test_fold.py`
- Test: `tests/test_resume.py`
- Test: `tests/test_dispatcher.py`
- Test: `tests/test_telemetry.py`
- Test: all three `tests/test_provider_*.py` subscription-backend modules

**Interfaces produced:**

- New additive events:
  - `ModelCallFailed(call_id, model=None, error_type="provider_error",
    message="", retryable=False, duration_ms=0)`.
  - `ModelCallAborted(call_id, reason)` for resume-time repair.
- Add defaulted `reason: str = "cancelled"` and `duration_ms: int = 0` to
  `ModelCallCancelled`.
- `FoldedState.open_model_intents: dict[CallId, int]` alongside the existing
  tool `open_intents`.
- A shared async context manager in `mcp_serve.py`,
  `running_tool_server(server)`, which starts inside its `try` and always calls
  `stop()` in `finally`.

- [ ] **Step 1: Add event compatibility tests.** Round-trip the two new events;
  parse a pre-task `ModelCallCancelled` without the new fields; confirm an old
  `SessionStarted` log still parses; confirm an unknown future event still
  becomes `UnknownEvent`.
- [ ] **Step 2: Add fold tests.** Assert `ModelCallProposed` opens a model
  intent; `Completed`, `Failed`, and `Cancelled` each close it; none of the
  failure terminal events add transcript messages.
- [ ] **Step 3: Extend resume repair.** Change `resume_repairs` to return both
  tool and model repair events in the originating proposal's sequence order,
  never lexical call-ID order. Tool intents produce `ToolCallAborted`; model
  intents produce `ModelCallAborted`. Update its return annotation to
  `list[Event]` and its docstring. Assert repeat resume does not generate a
  second repair.
- [ ] **Step 4: Add dispatcher failure tests.** Cover hook block, cross-type
  rewrite, non-retryable provider error, exhausted retryable error, timeout,
  and `asyncio.CancelledError`. In every case, assert one proposal and exactly
  one terminal model event with the same `call_id`. Preserve cancellation by
  re-raising it after logging.
- [ ] **Step 5: Refactor `dispatch_model`.** Use one local terminal-emission
  guard so two nested exception paths cannot double-close the call. Compute
  duration with `time.monotonic_ns()` from the moment `ModelCallStarted` is
  appended. Record sanitized, capped failure messages; never include
  credentials or full provider payloads.
- [ ] **Step 6: Extend and version telemetry.** Add `status`, `error_type`, and
  `error_message` columns to `model_calls` and introduce
  `TELEMETRY_SCHEMA_VERSION` stored with `PRAGMA user_version`. Insert the
  pending row at `ModelCallProposed`, update its effective model at
  `DispatchResolved`/`ModelCallStarted`, and update it on each terminal event.
  Terminal handling must upsert defensively so a historical partial log does
  not lose the fact. `rebuild_index(base)` may delete and rebuild the derived
  database from JSONL. `open_store(path)` must reject a nonempty database with
  an old/unknown schema using `TelemetrySchemaMismatch` and an actionable
  rebuild command; it must never run new queries against stale columns. Add a
  fixture containing the pre-task schema and prove rebuild preserves its
  projected facts. Render counts for failed/cancelled/aborted calls.
- [ ] **Step 7: Add lifecycle regression tests to Claude and Codex.** Copy the
  existing Antigravity cancellation-during-start invariant: monkeypatch a
  server whose `start()` records `start` and then blocks; cancel `complete()`;
  assert `stop` was recorded exactly once. Also assert normal completion and a
  failing `stop()` do not leave the subprocess generator open.
- [ ] **Step 8: Introduce `running_tool_server`.** Move `await server.start()`
  inside the context manager cleanup scope and use the helper in all three CLI
  providers. Delete the now-duplicated hand-written start/stop structure.
- [ ] **Step 9: Update architecture docs.** State that tool and model intents
  both require exactly one terminal fact and that resume repairs both kinds.
- [ ] **Step 10: Commit** `fix(runtime): close model intents and unify provider cleanup`.

**Acceptance:** For every model proposal in a folded log, exactly one of
completed, failed, cancelled, or aborted exists after normal shutdown or the
next resume. Cancelling any subscription provider during MCP startup releases
its task and port.

---

### Task 3: Replace the provider call shape with an owned request and status contract

**Purpose:** Give every provider the same typed input and make capability
differences machine-readable rather than comments or tags.

**Files:**

- Modify: `src/harness/provider.py`
- Modify: `src/harness/provider_litellm.py`
- Modify: all three `src/harness/provider_*.py` subscription backends
- Modify: `src/harness/dispatcher.py`
- Modify: `src/harness/tui.py` (remove its direct provider call in Step 8)
- Modify: every source/test provider double found by
  `rg -n 'def complete\(|\.complete\(' src tests`
- Test: `tests/test_provider.py`
- Test: `tests/test_dispatcher.py`
- Test: all provider adapter tests

**Interfaces produced in `provider.py`:**

- `ExecutionTier(StrEnum)`: `strict`, `contained`, `steered`, `experimental`.
- `ToolSurface(StrEnum)`: `exclusive`, `additive`, `unknown`.
- `HostAccess(StrEnum)`: `none`, `workspace`, `host_read`,
  `host_read_write`, `unknown`.
- `NativeNetworkAccess(StrEnum)`: `none`, `outbound`, `unknown`.
- `Availability(StrEnum)`: `supported`, `declared_absent`, `unknown`.
- Frozen `AccountingCapabilities(tokens, cost)` using `Availability` for each
  independently; an absent subscription price does not imply absent token
  counts, and an unknown value is never normalized to zero.
- `ContentKind(StrEnum)`: `text`, `thinking`, `tool_call`, `tool_result`,
  `image`, `audio`, `resource`, `structured`.
- Frozen `ProviderCapabilities` with fields:
  `tool_surface`, `filesystem`, `transport_network_required`,
  `native_network`, `content`, `structured_history`, `streaming`,
  `accounting`, `cancellation`, and `parallel_tool_calls`.
- Frozen `ProviderStatus` with fields: `backend`, `adapter`,
  `adapter_version`, `provider_version`, `tier`, `capabilities`,
  `verified`, `evidence`, and `limitations`.
- Frozen `CompletionLimits`: `max_output_tokens=None`, `timeout_s=None`.
- Frozen `CompletionRequest`: `model`, tuple `messages`, tuple `tools`,
  `read_blob`, `limits`, `required_content`, and immutable string metadata.
- `required_content_kinds(messages, tools) -> frozenset[ContentKind]` is the
  single producer for policy preflight. Task 6B extends its block inspection;
  adapters consume the computed set and may not implement a second policy gate.
- Typed `ContextLimitError(ProviderError)` with optional provider-reported
  maximum/requested sizes. Providers map their overflow signal to it; Harness
  never silently drops old messages. Explicit `/compact` or a configured
  compaction policy starts a separately evented administrative model call.
- `ModelProvider.describe(model) -> ProviderStatus` and
  `ModelProvider.complete(request) -> AsyncIterator[Chunk]`.

**Required initial status declarations:**

- `EchoProvider` and `FakeProvider`: deterministic `experimental`, except
  tests may construct `FakeProvider(status=...)` explicitly.
- Direct LiteLLM API transport: exclusive tool surface, no host filesystem,
  `transport_network_required=True`, `native_network=none`; verification
  remains false until Task 8 binds a fixture manifest to the route family.
- Claude Code: exclusive tool surface only within the verified CLI range.
- Codex: additive; host reads possible until Tasks 9A-9B containment succeeds.
- Antigravity: additive; host read/write and network unconfined until a real
  containment or built-in-disable mechanism is verified.

- [ ] **Step 1: Add pure type tests.** Assert enum serialization, immutable
  tuples/frozensets, deterministic `ProviderStatus.as_dict()`, and explicit
  default limitations. Reject `verified=True` with empty evidence.
- [ ] **Step 2: Add `CompletionRequest` tests.** Assert it stores tuple copies
  of messages/tools, can read an existing blob through its callback, and turns
  a missing blob into `ProviderError` with digest and corrective guidance.
  Assert required content is computed once and remains immutable.
- [ ] **Step 3: Change the protocol.** Replace keyword arguments on
  `ModelProvider.complete` with the single request object and add `describe`.
  This is an intentional internal API break; do not preserve a permanent dual
  signature.
- [ ] **Step 4: Migrate providers.** Update Fake, Echo, LiteLLM,
  CatalogProvider, Claude, Codex, and Antigravity. `CatalogProvider.describe`
  must resolve the alias, delegate CLI backends to their adapter, and combine
  API transport capabilities with catalog model capabilities. Catalog data may
  only narrow adapter/manifest capabilities; it can never change unknown or
  absent support to supported, elevate a tier, or manufacture verification.
- [ ] **Step 5: Migrate the dispatcher.** Construct `CompletionRequest` only
  after hooks and routing resolve the effective model. Pass
  `session.blobs.get` as `read_blob`. Preserve the existing `on_chunk` tee at
  the dispatcher boundary.
- [ ] **Step 6: Mechanically migrate test doubles.** Change every fake provider
  to inspect `request.model`, `request.messages`, and `request.tools`. Run the
  grep from the Files section again; no production or test call may use the old
  three-keyword signature.
- [ ] **Step 7: Keep subagents on the same provider object.** Add a regression
  in `tests/test_cli.py` proving `Kernel.set_provider()` updates loop and runner
  and that both observe the same `describe()` result.
- [ ] **Step 8: Remove TUI direct dispatch.** Add
  `Dispatcher.dispatch_admin_model(...)`, implemented through the same internal
  dispatch function with `purpose="compaction"`, no tools, and ordinary
  events/telemetry. Change `/compact` to call it. Add a TUI test proving a
  compaction creates proposed/started/completed events and can be blocked by a
  model dispatch hook.
- [ ] **Step 9: Normalize context overflow.** Add fake streams for each adapter
  that exercise its real overflow shape and assert `ContextLimitError`. The
  headless and TUI paths show the preserved-history size plus `/compact`
  guidance. A provider that returns a generic error without a documented
  overflow signal remains a generic provider failure; do not guess from prose.
- [ ] **Step 10: Update architecture and contributing docs.** Document the owned
  request/status boundary and ban direct calls to provider adapters outside the
  dispatcher and provider-delegation code.
- [ ] **Step 11: Commit** `refactor(providers): own request and capability contracts`.

**Acceptance:** `rg` finds no legacy provider call signature. Every normal,
subagent, mixture, and administrative model call receives the same request and
runs through the dispatcher.

---

### Task 4: Enforce execution profiles and record the effective trust boundary

**Purpose:** Prevent provider/model changes from silently changing the safety
contract.

**Files:**

- Create: `src/harness/runtime_policy.py`
- Modify: `src/harness/events.py`
- Modify: `src/harness/session.py`
- Modify: `src/harness/fold.py`
- Modify: `src/harness/resume.py`
- Modify: `src/harness/cli.py`
- Modify: `src/harness/dispatcher.py`
- Modify: `src/harness/cli.py` (`Kernel` and `build_kernel`)
- Modify: `src/harness/telemetry.py`
- Create: `tests/test_runtime_policy.py`
- Test: `tests/test_events.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_dispatcher.py`
- Test: `tests/test_telemetry.py`

**Interfaces produced:**

- `ExecutionProfile(StrEnum)`: `production`, `compatible`, `unsafe`.
- `ProviderPolicyError(ProviderError)` with `retryable=False`.
- `evaluate_provider(profile, status, required_content, limits) -> None`,
  raising a teaching `ProviderPolicyError` on tier/content refusal or when a
  finite token/cost budget depends on accounting the provider declares absent.
- Add defaulted `execution_profile: str = "compatible"` to `SessionStarted`.
- Add defaulted `provider_status: dict = {}` to model-form
  `DispatchResolved`; this is the one recorded effective status consumed by
  start/failure/telemetry paths, including a refusal before start.
- Add defaulted `purpose: str = "turn"` to `ModelCallStarted`.
- New additive `ExecutionProfileChanged(previous, current, reason,
  explicit_downgrade=False)` event.
- `FoldedState.current_profile` and `weakest_profile` derived only from recorded
  start/change events.
- `Kernel.execution_profile` and `Dispatcher.execution_profile`.

- [ ] **Step 1: Write the profile truth-table tests.** Production accepts only
  verified `strict`/`contained`; compatible accepts verified or known
  limitations but rejects a missing required content kind; unsafe accepts all
  tiers. An unknown/literal route fails production with an error naming
  `--profile unsafe` as the deliberate escape hatch.
- [ ] **Step 2: Add event compatibility tests.** Old SessionStarted,
  DispatchResolved, and ModelCallStarted payloads parse with defaults. New
  resolved/change events round-trip with the exact effective provider status
  and profile transition.
- [ ] **Step 3: Thread the profile through construction.** Add
  `execution_profile` to `Session`, `build_kernel`, `Kernel`, Dispatcher, TUI
  rebuild arguments, and subagent runner construction. Child sessions inherit
  the parent's profile; no agent definition can elevate it.
- [ ] **Step 4: Enforce after routing, before start.** In `dispatch_model`, run
  hooks first, describe the effective post-routing model, evaluate the profile,
  append model `DispatchResolved` with that status, then append
  `ModelCallStarted` only after policy accepts. A refusal emits
  `ModelCallFailed(error_type="provider_policy")` and never invokes the
  provider; telemetry still obtains the effective status from
  `DispatchResolved`.
- [ ] **Step 5: Add CLI `--profile`.** Initially default to `compatible` per the
  migration decision. Reject invalid values through argparse. Print one
  prominent stderr warning per process for `unsafe`, including provider/model
  and limitations; do not repeat it on every iteration.
- [ ] **Step 6: Handle Echo explicitly.** No-model startup is a `demo` state,
  not a production provider. The TUI and headless output must say `DEMO — Echo
  provider; no model is configured`. A user-supplied real prompt without a
  model/catalog route must exit nonzero rather than pretending Echo is a real
  completion.
- [ ] **Step 7: Define resume transitions.** Resume with the recorded current
  profile unless the caller explicitly supplies another. A stronger profile
  appends `ExecutionProfileChanged`. A weaker profile fails before opening the
  writer unless TUI confirmation or headless `--allow-profile-downgrade` is
  present, then appends a downgrade event. Show current and weakest profile in
  diagnostics and session detail. Child sessions inherit current profile and
  cannot weaken it.
- [ ] **Step 8: Lock down `Ask` behavior.** Add tests for top-level tool/model
  asks, asks inside subagents, TUI and headless resolvers, and cancellation of
  a pending TUI modal. Headless resolves deny immediately and never leaves a
  pending future. TUI requests name the originating agent/session. Only a
  pre-turn or administrative refusal maps the process to exit 4.
- [ ] **Step 9: Extend telemetry.** Store profile and provider status on model
  calls and expose them in run detail output. Do not infer historical status
  from current adapter code; use recorded event fields.
- [ ] **Step 10: Update the guide.** Include the full profile/tier truth table,
  migration default, and exact Codex/Antigravity limitations.
- [ ] **Step 11: Commit** `feat(policy): enforce and record provider execution profiles`.

**Acceptance:** The same catalog alias cannot silently move from a verified
production tier to an unknown or steered adapter/version. It fails before the
provider process starts and records why.

---

### Task 5: Add compatibility manifests, provider probes, and diagnostics

**Purpose:** Replace prose-only version claims and a manual `verified` boolean
with reproducible evidence.

**Files:**

- Create: `src/harness/provider_compat.toml`
- Create: `src/harness/release_support.toml`
- Create: `src/harness/diagnostics.py`
- Create: `src/harness/cli_output.py`
- Create: `src/harness/exit_codes.py`
- Modify: `src/harness/provider.py`
- Modify: `src/harness/catalog.py`
- Modify: all provider adapters
- Modify: `src/harness/cli.py`
- Modify: `scripts/record_fixtures.py`
- Create: `scripts/record_cli_fixtures.py`
- Create: `tests/fixtures/*/manifest.json` as fixtures are recorded
- Create: `tests/test_diagnostics.py`
- Test: `tests/test_catalog.py`
- Test: provider adapter tests
- Modify: `tests/test_package_metadata.py` (assert the compatibility manifest
  is present in the installed package)

**Manifest format:** Each `[[verification]]` record contains `backend`,
`provider_family`, `adapter`, `version_spec`, `tier`, `scenarios`, sorted
`verification_surface` package-relative paths,
`verification_dependencies` package/version-spec pairs,
`verification_surface_sha256`, `fixture_manifest_sha256`, optional
`containment_evidence_sha256`, `verified_at`, and `limitations`. Runtime status
is verified only when backend/family/version match, the current installed bytes
for every verification-surface path plus the detected versions of named runtime
dependencies hash to the recorded value, and every named scenario/evidence
file matches its hash. User `models.toml` cannot set or elevate verification;
deprecate its `verified` key with a warning, then ignore it.

**Release support floor:** Ship `release_support.toml` as package data. Its
initial required records are named, not count-based:

- `litellm-openai-api`: backend `litellm`, family `openai`, minimum `strict`,
  all ScenarioIds, no process-containment evidence;
- `litellm-anthropic-api`: backend `litellm`, family `anthropic`, minimum
  `strict`, all ScenarioIds, no process-containment evidence;
- `claude-code-subscription`: backend `claude-code`, family `anthropic`,
  minimum `contained`, all ScenarioIds, live containment required;
- `codex-subscription`: backend `codex`, family `openai`, minimum `contained`,
  all ScenarioIds, live containment required.

Each record specifies `minimum_tier`, `required_scenarios`, and whether live
containment is required. Antigravity remains an optional, visibly lower-tier
backend until it meets the same production containment contract; it may be
promoted only by a reviewed edit to both manifests with fresh evidence. The
release check fails if the support floor is empty, a required record has no
matching verification, or tests have replaced a required record with a fake.

**Interfaces produced:**

- `CompatibilityStore.load_package_default()` using `importlib.resources`.
- `ReleaseSupportFloor.load_package_default()` and
  `ReleaseSupportFloor.evaluate(compatibility, evidence)`.
- Closed `ScenarioId` vocabulary shared by compatibility parsing, recorders,
  conformance parametrization, generated docs, and release checks. Task 8 may
  add scenarios only by changing this type and its schema tests first.
- `ProbeResult(status, checks)` where each check has `name`, `ok`, `detail`,
  and `remediation`.
- Each provider implements `probe(model) -> ProbeResult`.
- CLI:
  - `harness --version`
  - `harness models list [--json]`
  - `harness doctor [--model ALIAS] [--json]`
  - `harness config show --effective [--json]`

**Stable exit codes introduced here and reused by every later command:** 0
success; 2 CLI usage/invalid configuration; 3 provider unavailable,
unverified, or incompatible; 4 top-level policy/permission refusal; 5 session
locked/corrupt/integrity failure; 6 runtime provider/MCP failure; 7 budget or
storage limit exceeded. Agent-internal denied tool calls remain tool results,
not automatic process exit 4.

- [ ] **Step 1: Test strict manifest and support-floor parsing.** Reject unknown keys, invalid
  version specs, duplicate backend/family ranges, missing scenario files,
  digest mismatches, and a strict record with non-empty unmitigated host-access
  limitations. Reject duplicate/empty support IDs, an unknown `ScenarioId`, a
  malformed backend/family, and any attempt to elevate a capability in catalog
  data. A required support record may have no verification yet: parsing
  succeeds, `evaluate()` reports it unsatisfied, and release remains blocked.
- [ ] **Step 2: Implement package-resource loading.** Ensure the TOML ships in
  the wheel and add installed-package assertions for both
  `provider_compat.toml` and `release_support.toml` to Task 1's metadata test.
  Missing/corrupt packaged compatibility or support data is a release error and
  makes all real providers unverified; do not fall back to trust.
- [ ] **Step 3: Deprecate catalog `verified`.** Emit a warning naming the alias
  and telling the user verification comes from shipped evidence. Keep parsing
  the key for one release so existing files do not crash.
- [ ] **Step 4: Implement CLI probes.** For CLI backends, check binary presence,
  parse `--version`, report only `credential material present` when the expected
  authentication file exists without reading or printing it, validate the
  version against the manifest, and perform a no-side-effect config/argument
  capability check. Never label file presence `authenticated`; only `--live`
  may validate authentication with a minimal completion. For API/local routes,
  check required env-var presence by name only and likewise perform a minimal
  completion only when `--live` is supplied.
- [ ] **Step 5: Implement shared CLI rendering and exit mapping.** Commands
  return typed results/errors to one top-level renderer in `cli_output.py`;
  JSON has stable keys and no ANSI, and argparse retains code 2. Move doctor,
  models, stats, compare, and later commands onto this seam instead of
  scattering `print` and `SystemExit`. Add one test per stable exit-code family.
- [ ] **Step 6: Implement doctor and effective-config output.** Human doctor
  output is a checklist with remediation. JSON output contains no ANSI or
  secrets. Return exit 0 only when all requested production checks pass; exit 3
  for unavailable/incompatible providers. `config show --effective` names the
  source layer of every effective profile, budget, storage, environment,
  redaction, and provider setting while masking values classified as secrets.
- [ ] **Step 7: Extend fixture recorders.** Store raw provider stream records
  after removing request IDs and secrets, create a manifest with SHA-256 hashes,
  and refuse to overwrite existing fixtures unless `--force` is given. Output
  must be byte-deterministic after normalization.
- [ ] **Step 8: Seed the manifests honestly.** Add only evidence available in
  the repository. OpenAI may be verified against current fixtures; Anthropic,
  Ollama, and subscription CLIs remain unverified until Task 8 records the
  required real streams. The shipped support-floor records remain required even
  while unsatisfied; that keeps Task 19C blocked rather than making the gate
  vacuous. Do not claim a tier to make tests pass.
- [ ] **Step 9: Document `doctor`, `models list`, effective config, stable exit
  codes, support-floor policy, and fixture renewal.** Include
  the exact command used after any provider CLI upgrade.
- [ ] **Step 10: Commit** `feat(diagnostics): derive provider status from probes and fixtures`.

**Acceptance:** Changing an installed CLI version outside a verified range
changes runtime status and production startup fails with a corrective doctor
command. Editing user catalog data cannot manufacture verification. The final
default cannot flip while a named release-support requirement is unsatisfied.

---

### Task 6A: Preserve durable blob, artifact, and tool-result semantics

**Purpose:** Give every tool source one lossless durable result representation
before provider-specific rendering is changed.

**Files:**

- Modify: `src/harness/messages.py`
- Modify: `src/harness/tools.py`
- Modify: `src/harness/events.py`
- Modify: `src/harness/fold.py`
- Modify: `src/harness/dispatcher.py`
- Modify: `src/harness/mcp_host.py`
- Test: `tests/test_messages.py`
- Test: `tests/test_blobs.py`
- Test: `tests/test_mcp_host.py`
- Create: `tests/test_artifact_repair.py`

**Interfaces produced:**

- Transient `RawArtifact(kind, media_type, data, uri=None, name=None)` in
  `tools.py`; bytes exist only until the dispatcher stores them.
- Durable `ArtifactRef(kind, media_type, blob, uri=None, name=None)` in
  `messages.py`.
- `ToolResult(text=None, artifacts=(), structured=None)` return value. Existing
  `Tool` implementations may continue returning `str`; the dispatcher
  normalizes both.
- Add defaulted `artifacts: tuple[ArtifactRef, ...] = ()` and
  `structured_result: dict | list | None = None` to `ToolCallCompleted` and
  `ToolResultBlock`.
- Additive `ArtifactUnavailableMarked(blob, reason, integrity_report_sha256)`
  event. It never rewrites the event that originally referenced the blob.
  `FoldedState.unavailable_artifacts` is derived from these repair facts.

- [ ] **Step 1: Add backward-compatible message/event tests.** Old
  `ToolResultBlock` and `ToolCallCompleted` payloads parse with empty artifacts
  and no structured result. New values and `ArtifactUnavailableMarked`
  round-trip through event JSON and fold.
- [ ] **Step 2: Add tool normalization tests.** A string becomes text; a
  `ToolResult` remains structured; artifact bytes spill to the session-local
  BlobStore before the completion event; two identical artifacts within that
  session deduplicate; errors never carry untrusted raw binary into JSONL.
  Assert blobs live below `sessions/<id>/blobs`; do not introduce global blob
  reference counting.
- [ ] **Step 3: Implement MCP preservation.** Translate `TextContent`,
  `ImageContent`, `AudioContent`, resource links/resources, and
  `structuredContent`. Reject an unknown content block with a typed error
  naming its type rather than omitting it. Map `isError` to the existing error
  boundary.
- [ ] **Step 4: Define authorized missing-artifact repair.** A missing/corrupt
  blob makes verification fail. Task 10's repair API may append
  `ArtifactUnavailableMarked` only with an authorization bound to the current
  integrity-report hash and exact digest. Replaying then exposes a
  metadata-only unavailable artifact; no repair path edits old JSONL or invents
  bytes. Test an unauthorized marker, stale report hash, and repeated idempotent
  marker.
- [ ] **Step 5: Update architecture docs.** State that JSONL plus each
  session-local blob directory is the unit of truth and describe the additive
  unavailable-artifact repair.
- [ ] **Step 6: Commit** `feat(artifacts): preserve durable structured tool results`.

**Acceptance:** Native, plugin, and MCP results fold into the same durable
representation. Missing content is either a hard integrity failure or an
explicitly authorized, append-only unavailability fact.

---

### Task 6B: Render complete bounded transcripts across providers

**Purpose:** Ensure a model switch does not erase tool history or turn rich
results into silent placeholders.

**Files:**

- Create: `src/harness/provider_transcript.py`
- Modify: `src/harness/provider.py`
- Modify: `src/harness/events.py`
- Modify: `src/harness/dispatcher.py`
- Modify: `src/harness/provider_litellm.py`
- Modify: all subscription provider adapters
- Create: `tests/test_provider_transcript.py`
- Test: `tests/test_provider_litellm.py`
- Test: all subscription provider tests
- Create: `tests/test_e2e_provider_switch.py`

**Interfaces produced:**

- `render_provider_transcript(request, *, per_result_bytes=65_536,
  total_result_bytes=262_144)` emits a deterministic role/block transcript for
  CLI adapters. It includes tool name, call ID, canonical JSON arguments,
  result/error state, artifacts, and bounded blob content; it never uses only
  `Message.text()`.
- Defaulted `transforms: list[dict] = []` on `ModelCallStarted`, naming every
  provider-boundary exclusion/transformation by block kind, action, and reason.

**Bounded content policy:** Plan retention newest-result-first, then render in
original chronological order. Each retained text result gets at most 32,768
UTF-8 bytes from its head and the remaining allowance from its tail, trimming
inward so neither boundary splits a code point. Across the request, inject at
most 262,144 raw bytes of historical tool-result bodies. Every shortened or
budget-excluded result states digest, original bytes, retained bytes, and how
to inspect the full session artifact. Binary media is never decoded as text;
it is sent only through a target-supported media block. Missing/corrupt blobs
fail preflight unless the fold contains an authorized
`ArtifactUnavailableMarked`, in which case the renderer emits its explicit
metadata-only marker.

- [ ] **Step 1: Write renderer tests.** Cover every role/block type,
  interleaved calls, results from another provider, UTF-8 boundary cases,
  hostile delimiter text, per-result and total caps, missing/corrupt/authorized
  unavailable blobs, and deterministic output. Use explicit JSON framing so
  model content cannot spoof a role delimiter.
- [ ] **Step 2: Extend the central content producer.** Teach
  `required_content_kinds` from Task 3 about artifacts, structured results,
  resource/audio/image blocks, and provider-specific thinking. Adapters consume
  `request.required_content`; they do not independently decide profile policy.
- [ ] **Step 3: Replace `_render_prompt`.** Claude, Codex, and Antigravity use
  the shared renderer plus only their small orientation prefix/suffix. Delete
  provider comments and paths that intentionally elide another model's tool
  records.
- [ ] **Step 4: Complete LiteLLM conversion.** Dereference bounded text/blob
  results, pass structured results where supported, and convert images to data
  URLs only after capability preflight. Never send provider-specific thinking
  to a different provider unless the target supports the same signed format.
  Exclude such non-conversational blocks, record the transformation, and never
  invent a reasoning summary.
- [ ] **Step 5: Add cross-provider end-to-end coverage.** Script provider A to
  call a tool returning text plus a blob-backed artifact, switch to provider B,
  and assert B receives the original call ID, arguments, result text, artifact
  metadata, and explicit bounded content. Assert persisted canonical events are
  unchanged.
- [ ] **Step 6: Update documentation.** Define exact preservation,
  transformation, cap allocation, authorized unavailability, and capability
  errors.
- [ ] **Step 7: Commit** `feat(transcript): preserve bounded history across providers`.

**Acceptance:** No production adapter relies on `Message.text()` as complete
history or contains an unlabelled sidecar/content-omitted fallback. All
provider-boundary transformations are recorded and user-visible.

---

### Task 7: Centralize tool-schema validation and risk metadata

**Purpose:** Make tool invocation and permission presentation consistent for
native, plugin, and MCP tools.

**Files:**

- Modify: `pyproject.toml` (add direct `jsonschema` dependency)
- Modify: `src/harness/tools.py`
- Modify: `src/harness/dispatcher.py`
- Modify: `src/harness/native_tools.py`
- Modify: `src/harness/mcp_host.py`
- Modify: `src/harness/plugins.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_dispatcher.py`
- Test: `tests/test_native_wiring.py`
- Test: `tests/test_mcp_host.py`
- Test: `tests/test_plugins.py`
- Modify: `docs/contributing.md`

**Interfaces produced:**

- `ToolRisk(StrEnum)`: `read`, `write`, `execute`, `network`, `delegate`,
  `destructive`, `unknown`. `delegate` means the tool can start model/agent
  work; it is not mislabelled as raw network access.
- Defaulted `risk: ToolRisk = ToolRisk.UNKNOWN` and
  `source: str = "native"`, `args_validated: bool = True`, and
  `schema_error: str | None = None` on `ToolSpec`.
- `InvalidToolSchema` raised for controlled native/plugin definitions at
  registration/load time. Invalid third-party MCP schemas are quarantined per
  the rule below rather than crashing the entire server connection.
- `validate_tool_args(spec, args) -> str | None`; error text includes JSON path,
  violated constraint, received JSON type/value summary, and a correction.

- [ ] **Step 1: Add registration tests.** Reject invalid JSON Schema when a tool
  registers. Make duplicate names loud in `ToolRegistry.register` instead of
  silently overwriting; the canonical external key remains the flat
  `ToolName` (`mcp__<server>__<tool>` for MCP), not a new tuple key. Collision
  errors name both sources. Update callers that deliberately merge registries
  to resolve precedence before registration.
- [ ] **Step 2: Add dispatcher tests.** Missing required values, extra values
  under `additionalProperties=false`, wrong scalar types, and bounds fail after
  hook rewriting but before tool execution. Assert the event sequence is
  proposed → hook decisions → resolved → completed(error), and that the tool's
  call counter remains zero.
- [ ] **Step 3: Implement Draft 2020-12 validation.** Precompile a validator at
  registration and store it beside the Tool object so the dispatcher does not
  recompile on every call. Sort multiple validation errors deterministically
  and report the first plus a count of remaining errors.
- [ ] **Step 4: Annotate every native tool.** Reads/glob/grep are `read`,
  writes/edit are `write`, bash is `execute`, todo is `write`, and coordination
  tools are `delegate` because they can spend model tokens and indirectly
  invoke tools. Set explicit sources.
- [ ] **Step 5: Map MCP annotations.** Use MCP `readOnlyHint`,
  `destructiveHint`, and `openWorldHint` when present. Missing or contradictory
  annotations yield `unknown`, never `read`. Source is
  `mcp:<server-name>`.
- [ ] **Step 6: Define invalid-schema source behavior.** A native tool or
  in-process plugin with an invalid schema fails its controlled all-or-nothing
  load. For a third-party MCP server, keep the server connected but quarantine
  only the invalid tool: show it in diagnostics/tool browser with
  `args_validated=False`, `risk=unknown`, and the sanitized schema error; do not
  advertise or execute it in production. Compatible/unsafe may advertise it
  only behind an `Ask` on every call, using the valid fallback schema
  `{"type":"object","additionalProperties":true}` while preserving the remote
  tool's own validation errors. Never send the malformed schema to a provider.
  Add mixed valid/invalid server tests proving one bad tool does not erase the
  valid tools.
- [ ] **Step 7: Update permission defaults.** In production, unknown/write/
  execute/network/delegate/destructive tools ask unless an explicit deny/allow
  rule applies. A quarantined MCP tool follows Step 6 even if a broad allow
  rule exists. Deny remains absolute.
- [ ] **Step 8: Rewrite contributing law 10.** The dispatcher enforces declared
  structural schema; tools still enforce semantic/cross-field/filesystem laws.
- [ ] **Step 9: Commit** `feat(tools): validate schemas and standardize risk metadata`.

**Acceptance:** A provider cannot exploit inconsistent tool-local argument
handling. The TUI and headless policy receive the same normalized source/risk
information for every tool.

---

### Task 8: Build the provider conformance and adversarial substrate corpus

**Purpose:** Turn provider support from anecdotal success into a repeatable
release gate.

**Files:**

- Replace/expand: `tests/conformance/test_conformance.py`
- Create: `tests/conformance/contracts.py`
- Create: `tests/conformance/test_provider_contract.py`
- Create fixture scenarios beneath `tests/fixtures/<provider>/`
- Modify: `scripts/record_fixtures.py`
- Modify: `scripts/record_cli_fixtures.py`
- Test provider modules as required
- Modify: `src/harness/provider_compat.toml`
- Create: `docs/provider-support.md`

**Required scenario set for every production provider family/backend:**

1. text streaming and declared accounting state;
2. single tool call;
3. interleaved multiple tool calls;
4. tool error followed by self-correction;
5. large blob-backed tool result;
6. structured result and image preflight;
7. context overflow;
8. authentication failure;
9. retryable overload/rate limit;
10. malformed/truncated stream;
11. cancellation before first byte and mid-stream;
12. provider switch after another provider's tool call;
13. permission denial.

Scenario IDs 1-13 are adapter/protocol facts that deterministic recorded streams
can prove. Host filesystem/process/network containment is deliberately absent
from this replay corpus; Tasks 9A-9B prove it live against the operating system.

- [ ] **Step 1: Make missing required fixtures fail.** Parametrize from the
  compatibility manifest, not a hard-coded provider tuple. If a provider is
  marked production-verifiable and any scenario is absent, fail collection;
  do not `pytest.skip`.
- [ ] **Step 2: Keep optional providers explicit.** Providers absent from the
  production manifest may skip with one summarized reason. The release report
  must distinguish optional skips from production coverage.
- [ ] **Step 3: Build pure adapter contract helpers.** The same assertions run
  over normalized fixtures for LiteLLM, Claude, Codex, and Antigravity: ordered
  chunks, stable call IDs, valid JSON args, one stop, declared accounting
  semantics, normalized context overflow, typed errors, and no hidden content
  loss. A provider declaring accounting absent emits no fabricated zero-valued
  usage report.
- [ ] **Step 4: Add adversarial protocol fixtures.** Exercise duplicate stop,
  result-before-call, malformed structured content, invalid UTF-8 replacement,
  oversized records, and a provider stream that lies about a model route.
  These fixtures prove parser/contract behavior only. Do not record or replay a
  fake filesystem/network outcome and call it containment evidence.
- [ ] **Step 5: Record real streams.** Use the recorders with real credentials
  outside CI, review normalized fixture diffs for secrets, and commit only
  sanitized deterministic fixtures plus manifests. If credentials are not
  available, leave the provider non-production rather than manufacturing data.
- [ ] **Step 6: Generate `docs/provider-support.md`.** Build the table from the
  manifest so docs cannot drift. Include provider family, backend, version
  range, tier, capabilities, limitations, fixture date, and renewal command.
- [ ] **Step 7: Wire CI.** Run hermetic replay on every commit. Add a scheduled
  opt-in live workflow only when repository secrets are configured; it never
  blocks pull requests for missing credentials, but its last result is visible
  in the support report.
- [ ] **Step 8: Commit** `test(providers): enforce the production conformance corpus`.

**Acceptance:** Production providers have no conformance skips and pass all
applicable replay scenarios. A CLI provider is still ineligible for `strict` or
`contained` until its exact executable/version separately passes the live
Tasks 9A-9B boundary suite.

---

### Task 9A: Build filesystem-containment primitives and live evidence

**Purpose:** Establish exactly what the Linux/WSL2 process wrapper proves,
without confusing provider transport connectivity with model-tool access.

**Files:**

- Create: `src/harness/process_sandbox.py`
- Modify: `src/harness/diagnostics.py`
- Create: `tests/fixtures/fake_hostile_cli.py`
- Create: `tests/test_process_sandbox.py`
- Create: `tests/containment/test_live_boundary.py`
- Create: `tests/containment/conftest.py`
- Create: `docs/security/provider-containment.md`

**Interfaces produced:**

- `FilesystemSandboxPolicy`: allowed read-only runtime roots, private scratch
  read/write roots, inherited descriptors, executable, and
  `share_host_network=True`. The beta implementation rejects `False` because
  no network proxy/filter is implemented here.
- `SandboxAvailability(StrEnum)`: `missing`, `present_unusable`, `functional`.
- `SandboxProbe(availability, checks, bwrap_version, kernel, architecture)`.
- `build_sandboxed_argv(policy, argv) -> list[str]` using bubblewrap.
- `ContainmentEvidence`: exact Harness commit, platform/kernel, bubblewrap and
  provider CLI versions, boundary-test hash, timestamp, and pass/fail per
  filesystem/process/native-network boundary.
- `ContainmentUnavailable(ProviderPolicyError)`.

**Boundary rule:** Bubblewrap provides filesystem/process containment for the
trusted CLI and descendants. The outer CLI intentionally retains host network
so it can reach its provider API and Harness's loopback MCP server. Therefore
the wrapper alone never sets `native_network=none`. That status requires a
separate, live adapter-specific proof that native tools are disabled or held by
the provider's own verified sandbox. Do not add `--unshare-net`: it would also
cut off provider transport and loopback MCP. A future filtered proxy or
provider-supported Unix socket is a separate design.

**Bubblewrap filesystem policy:** New mount namespace; empty working directory;
private 0700 scratch HOME and auth/config directory; read-only runtime/library
mounts required to start the trusted CLI; minimal `/etc` resolver/certificate
material; private `/tmp`; no workspace, real home, SSH directory, Git config,
or Harness session directory bind; `--die-with-parent`; only null/random
devices. Never bind `/` wholesale. Copied credential files are writable inside
the private scratch because CLIs may refresh them; hash the real source before
and after and never write scratch changes back automatically.

- [ ] **Step 1: Write live hostile-process tests.** The executable attempts to
  read a unique real-home sentinel, enumerate real home, write a workspace
  sentinel, read/write its scratch auth copy, and leave a child alive. Mark the
  test `containment`, run it only on the scoped target platform, and assert OS
  outcomes directly: real-home read/enumeration/write fail, scratch access
  succeeds, the child dies, and real credentials remain byte-identical. Do not
  serialize these outcomes into a replay fixture.
- [ ] **Step 2: Implement argument construction.** Resolve and validate every
  runtime root, refuse an external symlink as a writable bind, keep provider
  args after `--`, and test spaces/leading dashes. Generate the minimal mount
  list from the executable and dynamic-library probe; do not silently fall back
  to binding the host root.
- [ ] **Step 3: Probe functionality, not presence.** `doctor` executes a
  harmless minimal sandbox (`/usr/bin/true`) and reports missing,
  present-but-denied, or functional with remediation. Unit tests inject each
  outcome; the live test records kernel/user-namespace/AppArmor details.
- [ ] **Step 4: Emit evidence.** The live suite writes one deterministic JSON
  evidence document only when explicitly passed an output path. Normal tests
  never update evidence. A reviewer checks it before its SHA-256 is entered in
  the compatibility manifest.
- [ ] **Step 5: Commit** `feat(sandbox): prove Linux filesystem containment`.

**Acceptance:** A functional probe and live OS test prove the declared
filesystem/process boundary. Documentation explicitly states that trusted
provider transport still has network access.

---

### Task 9B: Integrate and classify subscription-CLI containment

**Purpose:** Combine Task 9A's outer filesystem boundary with each CLI's actual
native-tool controls, or keep that backend below production.

**Files:**

- Modify: `src/harness/provider_claude_code.py`
- Modify: `src/harness/provider_codex.py`
- Modify: `src/harness/provider_antigravity.py`
- Modify: `src/harness/provider_compat.toml`
- Extend: all three subscription provider tests
- Create: `tests/containment/test_claude_live.py`
- Create: `tests/containment/test_codex_live.py`
- Create: `tests/containment/test_antigravity_live.py`
- Modify: `docs/security/provider-containment.md`

- [ ] **Step 1: Integrate writable private scratch credentials.** Each adapter
  copies only its enumerated auth files into a 0700 scratch tree, runs that tree
  inside Task 9A's wrapper, and destroys it after the process exits. Tests prove
  the real files are unchanged even when a fake CLI rewrites its copy. If a
  real CLI requires a refreshed credential to persist, fail with instructions
  to run that CLI's login/refresh outside Harness; do not copy it back silently.
- [ ] **Step 2: Prove Claude Code.** Keep strict MCP config and the complete
  built-in deny list. Live-probe a native absolute read, write, subprocess, and
  network attempt plus a successful Harness MCP call and completion. Renew the
  verified version range whenever the CLI's built-in inventory changes.
- [ ] **Step 3: Prove Codex.** Retain Codex's own `-s read-only` sandbox and add
  the outer filesystem namespace. Live-probe that absolute host reads/writes
  fail because of the outer wrapper and that model-native outbound network
  fails because of Codex's verified sandbox, while trusted provider transport,
  completion, and Harness MCP still work.
- [ ] **Step 4: Probe Antigravity.** Against the exact pinned version, test
  `--sandbox`, `--mode plan`, and any documented built-in-disable facility with
  real Harness MCP. If native filesystem/process/network actions cannot all be
  denied while transport and MCP remain functional, retain `steered` and make
  production refuse it. Scratch HOME/cwd alone is never containment.
- [ ] **Step 5: Bind classification to evidence.** A `contained` manifest record
  names and hashes both the Task 8 replay fixture set and Task 9A/9B live
  evidence. Version, platform, kernel feature, boundary, or built-in inventory
  drift makes status unverified until renewed.
- [ ] **Step 6: Handle unavailable containment.** Missing or nonfunctional
  bubblewrap yields doctor remediation and exit 3 in production. Compatible or
  unsafe may continue only after displaying and recording the exact boundary
  downgrade.
- [ ] **Step 7: Commit** `feat(providers): classify verified CLI containment`.

**Acceptance:** Every production subscription adapter has current live evidence
for native filesystem, process, and network behavior while provider transport
and Harness MCP remain functional. Every other adapter is visibly unavailable
in production.

---

### Task 10: Harden session storage, locking, atomic repair, and integrity checks

**Purpose:** Make sessions private and crash-recoverable at the filesystem
boundary.

**Files:**

- Create: `src/harness/storage.py`
- Modify: `src/harness/log.py`
- Modify: `src/harness/blobs.py`
- Modify: `src/harness/session.py`
- Modify: `src/harness/resume.py`
- Modify: `src/harness/sessions.py`
- Create: `src/harness/session_admin.py`
- Modify: `src/harness/events.py`
- Modify: `src/harness/cli.py`
- Modify: `src/harness/diagnostics.py`
- Create: `tests/test_storage.py`
- Test: `tests/test_log.py`
- Test: `tests/test_blobs.py`
- Test: `tests/test_resume.py`
- Test: `tests/test_sessions.py`
- Create: `tests/test_session_admin.py`

**Interfaces produced:**

- Linux/WSL `SessionLock` backed by a held `fcntl.flock` descriptor. The lock
  file remains present; lock state comes from the kernel, not path existence.
- Lock metadata JSON: pid, process start time, hostname, session ID, and random
  owner nonce. Metadata is diagnostic only; `flock` is authoritative.
- `atomic_write(path, bytes, mode=0o600)` and `fsync_dir(path)` helpers.
- `verify_session(base, session_id) -> IntegrityReport` covering sequence,
  parse, terminal intent pairing, blob existence/size/hash, and lock state.
- `VerificationMode(StrEnum)`: `quick` checks sequence/parse/pairing, lock,
  blob presence, and recorded size; `full` additionally hashes every event file
  and blob. Repair, export, qualification, and release gates always require
  `full`; interactive list/detail may use `quick` and must label it.
- `StorageProbe(filesystem_type, flock_ok, replace_ok, file_fsync_ok,
  directory_fsync_ok, detail)`. Production session storage requires every
  capability; known `v9fs`, `9p`, `drvfs`, NFS, and SMB roots fail even if the
  workspace itself is allowed to live there.
- Frozen `RepairAuthorization(session_id, operation, integrity_report_sha256,
  target_digest=None)`; `repair_session(report, authorization)` validates every
  field against a fresh locked report before mutation.
- `StorageLimits(max_event_bytes=1_048_576,
  max_session_log_bytes=1_073_741_824, min_free_bytes=134_217_728)` from the
  `[storage]` runtime config and shown by effective config/doctor.
- Typed `StorageLimitExceeded(kind, limit, observed)` event/error mapped to
  stable exit code 7.

- [ ] **Step 1: Probe the actual data root.** In a private temporary directory
  beneath the configured session base, test held `flock`, file/directory fsync,
  and atomic replace and report the filesystem type. Refuse production before
  session creation on known remote/Windows-mounted filesystems or a failed
  capability. Unit tests inject every result. Add a WSL-specific diagnostic
  proving a workspace on `/mnt/c` is allowed only when the session base is on
  the Linux filesystem.
- [ ] **Step 2: Add permission tests under a permissive umask.** Create a
  session with `umask(0)` inside a test-local restore guard. Assert Harness-owned
  session directories are 0700 and JSONL, lock, blob, quarantine, and temporary
  files are 0600. Do not chmod the user-supplied base directory itself.
- [ ] **Step 3: Replace PID lock semantics.** Hold the lock fd for the writer's
  lifetime. A second writer fails even if it edits metadata. A process-killed
  test proves the kernel releases the lock without manual deletion. Remove
  `_clear_stale_lock` and all `os.kill(pid, 0)` logic.
- [ ] **Step 4: Enforce the append-owner invariant.** Capture the owning thread
  and loop when a session starts. Direct append from another thread raises a
  teaching error; provide `schedule_append(event)` using
  `loop.call_soon_threadsafe` for integrations that need it. Concurrent async
  tasks on the owner loop produce unique contiguous sequence numbers. Test
  append failure gaps, subscriber publication order, and thread rejection.
- [ ] **Step 5: Make close idempotent.** Double close and cleanup after partial
  constructor failure must not raise or unlock another writer.
- [ ] **Step 6: Make blob publication durable.** Open temp files with 0600,
  write and fsync, `os.replace`, fsync the containing directory, and remove a
  leftover temp after failure. Verify existing content by hash before treating
  a race as success.
- [ ] **Step 7: Make torn-log repair atomic.** Write and fsync quarantine and
  repaired-log temp files, atomically publish them, then fsync the sessions
  directory. Never truncate the canonical log in place. Preserve the existing
  refusal to overwrite a different quarantine.
- [ ] **Step 8: Add bound repair authorization.** Verification hashes the
  normalized report. Repair reacquires the session lock, recomputes the report,
  and rejects a stale hash, wrong session/operation, or missing explicit
  authorization. Torn-tail repair and Task 6A's unavailable-artifact marker use
  this one API. Never accept a bare `repair=True` boolean on a public path.
- [ ] **Step 9: Add quick/full integrity reports.** Reports contain no
  transcript text by default. Each finding has severity, path relative to base,
  sequence/call ID, and remediation. Verification is read-only and states its
  mode. Tests prove quick detects missing/size-mismatched blobs but only full
  detects same-size corruption.
- [ ] **Step 10: Bound active log growth without rotation.** Serialize an event
  before assigning its sequence; if it exceeds `max_event_bytes`, spill a
  supported large payload to a blob or reject it with a small terminal error.
  Before starting a new turn, refuse when the current JSONL size reaches
  `max_session_log_bytes` or free space is below `min_free_bytes`. Emit one
  fixed-size `StorageLimitExceeded` fact when space permits and return exit 7.
  Never rotate or rewrite an active canonical log; closed sessions are exported
  and trashed through Task 17B. Test boundaries and a fake ENOSPC filesystem.
- [ ] **Step 11: Add fault injection.** Monkeypatch write, fsync, replace, and
  directory fsync at each boundary. After every injected failure, assert either
  the pre-operation or post-operation canonical state exists—never a hybrid.
- [ ] **Step 12: Commit** `fix(storage): make session persistence private atomic and verifiable`.

**Acceptance:** A killed process cannot leave a false live lock; session repair
survives a second crash; every referenced blob can be verified; no Harness-owned
sensitive file depends on ambient umask. Production refuses a session base that
cannot support its locking/durability contract, and active-log growth has an
explicit bounded behavior.

---

### Task 11A: Implement auditable redaction and minimal child environments

**Purpose:** Prevent ambient secrets from crossing log, blob, provider, and
child-process boundaries, and record whether sanitization succeeded.

**Files:**

- Create: `src/harness/process_env.py`
- Modify: `src/harness/redaction.py`
- Modify: `src/harness/messages.py`
- Modify: `src/harness/events.py`
- Modify: `src/harness/session.py`
- Modify: `src/harness/loop.py`
- Modify: `src/harness/dispatcher.py`
- Modify: `src/harness/native_tools.py`
- Modify: `src/harness/mcp_config.py`
- Modify: `src/harness/mcp_host.py`
- Modify: all subscription provider adapters
- Modify: `src/harness/cli.py`
- Test: `tests/test_redaction.py`
- Create: `tests/test_process_env.py`
- Test: `tests/test_native_bash.py`
- Test: `tests/test_mcp_host.py`
- Test: provider adapter tests

**Interfaces produced:**

- `RedactionConfig`: literal secret env names plus compiled regex rules with a
  stable replacement. Config values are never included in repr/error output.
- Frozen `RedactionOutcome(applied_rules, replacement_count, failed)` and
  generic `RedactionResult(value, outcome)`. Outcomes contain no matched text.
- `PatternRedactor` replaces the old unrelated string/event callable seams.
  Failure returns a fully masked safe fallback and never retries against the
  original content.
- Defaulted `redaction: dict = {}` on `UserMessage`, `ToolCallCompleted`, and
  `ModelCallStarted`. Provider request preparation aggregates already-recorded
  outcomes so each model call says whether its input was sanitized and whether
  any redactor failed, without exposing matches.
- `ChildEnvPolicy`: minimal baseline variables plus explicitly named
  pass-through variables. Baseline is PATH, locale, TERM/NO_COLOR, TMPDIR, and
  existing certificate paths. Credentials, agent sockets, cloud profiles, VCS
  tokens, proxies, and arbitrary variables are absent unless explicitly named.

**Configuration:** Load `~/.config/harness/security.toml` first and
`<workspace>/.harness/security.toml` second. The schema is
`[redaction].env = ["VARIABLE_NAME"]`, repeated
`[[redaction.patterns]]` entries with required `name`/`regex` and optional
`replacement`, and `[environment].pass = ["VARIABLE_NAME"]`. Project config
may add redaction rules. In production it may not add environment pass-through
names; only private user config may do that. Literal secret values are invalid.
Corporate proxy use is supported only by explicitly passing the named proxy
variables; effective config and the unsafe warning disclose that egress change.

- [ ] **Step 1: Write redaction tests.** Cover prompts, tool args/output,
  exceptions, nested event dictionaries, blobs, provider request history, and
  live subscriber events. Assert redaction runs before message insertion and
  blob spill so original secret bytes appear in no file beneath the test base
  and are not sent to a fake provider.
- [ ] **Step 2: Return and persist outcomes.** Replace identity/callable seams
  with `RedactionResult`; record only rule names/counts/failure. Ensure resumed
  history uses redacted canonical values. Add old-event compatibility tests for
  the defaulted fields and a per-model-call aggregation test.
- [ ] **Step 3: Make failure reporting recursion-safe.** On failure, persist the
  masked event plus exactly one fixed `ErrorRaised(where="redaction",
  message="redaction failed; original content was removed")` through a writer
  path that cannot invoke redaction recursively. The call's outcome has
  `failed=True`; no original bytes appear in diagnostic text.
- [ ] **Step 4: Load redaction configuration.** User rules cannot be disabled by
  project config. Validate regexes at startup. Doctor prints rule names/counts
  and outcome status only.
- [ ] **Step 5: Write child-environment tests.** Seed API keys, generic
  TOKEN/PASSWORD names, SSH_AUTH_SOCK, AWS/GCP/Azure variables, proxy URLs, and
  an explicitly allowed value. Native bash, MCP stdio, and provider children
  see only baseline plus the allowed value.
- [ ] **Step 6: Centralize environment construction.** Delete `_SCRUB_SUFFIX`,
  `_sanitized_env`, and `resolve_env(...) or None` inheritance. MCP spec env
  names and provider authentication variables are explicit additions to the
  minimal baseline.
- [ ] **Step 7: Add deliberate escape hatches.** CLI/config can pass exact
  variable names, never a wildcard in production. Unsafe may offer
  `--inherit-env`, with a prominent warning and event annotation.
- [ ] **Step 8: Commit** `feat(security): redact data and minimize child environments`.

**Acceptance:** Sentinel secrets do not reach canonical storage, providers, or
children unless a named policy deliberately passes them. Every persisted
prompt/result/model call records a non-secret redaction outcome.

---

### Task 11B: Authenticate outward MCP without exposing capability tokens

**Purpose:** Prevent a process that discovers the loopback port from invoking
Harness tools, without placing the credential in process arguments.

**Files:**

- Modify: `src/harness/mcp_serve.py`
- Modify: `src/harness/process_env.py`
- Modify: all subscription provider adapters
- Test: `tests/test_mcp_serve.py`
- Test: provider adapter tests
- Modify: `docs/security/provider-containment.md`

**Interfaces produced:**

- `McpEndpoint(url, credential_delivery, expires_at)` whose URL contains a
  per-turn 256-bit random capability path; the un-tokened `/mcp` route does not
  exist.
- Provider-specific credential delivery uses a 0600 scratch configuration file
  or inherited descriptor. The full URL/token is forbidden in argv, event
  logs, diagnostics, exception text, and telemetry.

- [ ] **Step 1: Add routing and lifetime tests.** Generate with
  `secrets.token_urlsafe(32)`, mount only the randomized route, return 404 for
  guessed base/wrong routes before dispatch, rotate every turn, and invalidate
  after provider context exit. Assert token text is absent from all persistent
  files except the ephemeral 0600 config and absent from captured argv/errors.
- [ ] **Step 2: Deliver Claude configuration by file.** Put endpoint details in
  its strict temporary MCP config and pass only that config path in argv.
- [ ] **Step 3: Deliver Codex configuration by file.** Put the endpoint in the
  isolated scratch `config.toml` alongside the approval setting; remove the
  dotted URL override from argv and prove unrelated user MCP config remains
  absent.
- [ ] **Step 4: Resolve Antigravity safely or downgrade it.** Probe for a config,
  stdin, or inherited-descriptor registration channel. If `agy mcp add` exposes
  the full URL only in argv, document that limitation and keep the adapter
  ineligible for production; do not weaken the token-visibility test.
- [ ] **Step 5: Redact transport failures.** Deliberately make each CLI echo its
  config/endpoint in stderr and assert the capability is masked before any
  provider error is logged or displayed.
- [ ] **Step 6: Document the local threat boundary.** The beta assumes one
  trusted OS user and does not claim defense from a malicious same-UID process
  that can read another process's private files/memory. It does protect against
  port discovery alone and other local users under normal `/proc`/file
  permissions.
- [ ] **Step 7: Commit** `feat(mcp): authenticate per-turn provider endpoints`.

**Acceptance:** Knowing the loopback port is insufficient to call Harness MCP,
and no production-capable adapter exposes the per-turn capability in argv or
durable output.

---

### Task 12: Make grants atomic and plugins explicitly trusted

**Purpose:** Prevent corrupted permissions and invisible execution of changed
plugin code.

**Files:**

- Modify: `src/harness/permissions.py`
- Create: `src/harness/plugin_trust.py`
- Modify: `src/harness/plugins.py`
- Modify: `src/harness/cli.py`
- Modify: `src/harness/events.py`
- Test: `tests/test_permissions.py`
- Create: `tests/test_plugin_trust.py`
- Test: `tests/test_plugins.py`
- Test: `tests/test_cli.py`
- Modify: `docs/plugin-authoring.md`
- Modify: `docs/user-guide.md`

**Interfaces produced:**

- Atomic `PermissionGrantStore` using a held advisory lock, parse/merge,
  deterministic TOML render, 0600 temp, fsync, replace, and directory fsync.
- Deterministic `plugin_digest(root)`: SHA-256 over sorted relative paths,
  modes, and bytes; never follows symlinks and excludes cache/generated bytecode.
- User-local trust records keyed by canonical path and digest.
- CLI: `harness plugins list`, `harness plugins trust PATH`,
  `harness plugins revoke PATH`, all with `--json` where applicable.
- New typed `PluginTrustDecided(plugin, path_hash, digest, trusted)` event. Do
  not record the full absolute path in the session log.

- [ ] **Step 1: Add concurrent grant tests.** Two processes append different
  grants; the final file parses and contains both exactly once. Failure during
  replace leaves the prior file valid. Repeating the same grant is idempotent.
- [ ] **Step 2: Implement atomic grant storage.** Preserve deny precedence and
  current layering. Sort rendered grants for deterministic diffs. Change the
  TUI/headless “always” path to use the store, not direct append.
- [ ] **Step 3: Add plugin digest tests.** Same tree produces the same digest;
  content/path/mode changes alter it; temporary/cache files do not affect it.
  Refuse symlinks resolving outside the plugin root. For internal symlinks,
  hash the link path and target text while the target file is also hashed by
  its own canonical in-root path; reject cycles and broken links.
- [ ] **Step 4: Enforce plugin trust by profile.** Production refuses every
  plugin with executable Python, MCP process/URL declarations, emitters, or
  dispatch hooks unless its current digest is trusted. Prompt-only skills,
  commands, and agent definitions load but are visibly marked `data-only`.
  Compatible warns; unsafe follows current behavior with annotation.
- [ ] **Step 5: Invalidate trust on change.** A modified trusted plugin becomes
  untrusted before any module import. Never import first and check later.
  Immediately before import, re-stat every hashed file and refuse if identity,
  size, mtime, or digest changed. Document honestly that this reduces accidental
  and same-user races but is not a sandbox against a malicious trusted user.
- [ ] **Step 6: Add CLI trust management.** Show canonical path to the user at
  trust time, but store it only in the private local trust file. Require an
  interactive confirmation unless `--yes` is supplied; `--yes` is invalid
  without an exact path.
- [ ] **Step 7: Document the threat model.** State clearly that trusted plugin
  Python executes in-process with the user's authority; isolation is not
  claimed.
- [ ] **Step 8: Commit** `feat(plugins): require digest-bound local trust in production`.

**Acceptance:** Concurrent grants cannot corrupt policy. Editing one byte of a
trusted executable plugin prevents it from loading in production until the
user explicitly trusts the new digest.

---

### Task 13: Add shared budgets, concurrency limits, and bounded fan-out

**Purpose:** Prevent one turn or coordination call from consuming unbounded
time, money, processes, or provider capacity.

**Files:**

- Create: `src/harness/budgets.py`
- Modify: `src/harness/events.py`
- Modify: `src/harness/dispatcher.py`
- Modify: `src/harness/loop.py`
- Modify: `src/harness/subagent.py`
- Modify: `src/harness/mixture.py`
- Modify: `src/harness/cli.py`
- Modify: `src/harness/telemetry.py`
- Create: `tests/test_budgets.py`
- Test: `tests/test_dispatcher.py`
- Test: `tests/test_loop.py`
- Test: `tests/test_subagent.py`
- Test: `tests/test_mixture.py`
- Test: `tests/test_cli.py`

**Interfaces produced:**

- Frozen `BudgetLimits`: `max_elapsed_s`, `max_model_calls`,
  `max_input_tokens`, `max_output_tokens`, `max_cost_usd`,
  `max_concurrent_models`, `max_concurrent_tools`,
  `max_concurrent_subagents`, and `max_fanout`.
- Shared `BudgetLedger` owned by Kernel and passed unchanged to loop,
  dispatcher, runner, and child sessions. Counters update under one async lock;
  concurrency uses owned leases rather than independently acquired semaphores.
- `ToolConcurrencyClass(StrEnum)`: `leaf`, `coordination`; default `leaf` on
  `ToolSpec`. Ensemble, panel, escalate, and dispatch-agent tools are
  `coordination` and never hold a leaf-tool permit while awaiting children.
- `ConcurrencyLease` and `BudgetLedger.reserve_fanout(...)` atomically reserve
  child subagent/model capacity or reject without creating tasks.
- Typed `BudgetExceeded(kind, limit, observed, call_id=None)` event.
- Additive `BudgetConfigured(limits, sources, explicit_increase=False)` event
  appended once per process lifetime before the first dispatch.
- CLI flags grouped under `--budget-*`; config parsing uses the same validator.

**Configuration:** Package defaults are loaded first, then values explicitly
present in `~/.config/harness/runtime.toml` replace those defaults. Values in
`<workspace>/.harness/runtime.toml` may only narrow the result. CLI values
normally narrow it; a CLI value may raise/remove a stored limit only together
with `--allow-budget-increase`. Record the source and override for every
effective value. The table is `[budgets]` and keys exactly match the
`BudgetLimits` fields above. `None` means unbounded; AgentDef values always take
the minimum with the effective parent limit.

**Package defaults:** `max_elapsed_s=3600`, `max_model_calls=100`,
`max_input_tokens=None`, `max_output_tokens=None`, `max_cost_usd=None`,
`max_concurrent_models=5`, `max_concurrent_tools=8`,
`max_concurrent_subagents=4`, and `max_fanout=4`. Unknown accounting therefore
does not make the default unusable; users who configure a token/cost ceiling
receive the Task 3/4 preflight guarantee.

- [ ] **Step 1: Add pure limit-validation tests.** Reject zero/negative,
  non-finite, contradictory, and bool-as-int values. `None` means unbounded and
  must be rendered explicitly as such by doctor.
- [ ] **Step 2: Add dispatcher accounting tests.** Reserve a model slot before
  provider start; release under completion/failure/cancellation; charge actual
  reported tokens/cost once; refuse the next call after a hard cumulative limit.
  Pass `max_output_tokens` and the smaller of provider/runtime deadlines into
  `CompletionRequest.limits`. If token/cost accounting is declared absent or
  unknown and the corresponding cumulative limit is finite, policy refuses
  before provider start with remediation; elapsed/model-call/concurrency limits
  remain usable. Never charge unknown as zero.
- [ ] **Step 3: Separate leaf and coordination tools.** Wrap only actual leaf
  execution—not proposal/hook/permission recording—in the leaf-tool lease.
  Coordination tools obtain no leaf permit while awaiting children. Add a
  regression with every leaf permit occupied by coordination calls and prove
  the calls either reserve child capacity or fail promptly, never deadlock.
- [ ] **Step 4: Share the ledger with children.** Subagents and mixture experts
  consume the parent's totals and leases. An AgentDef cannot increase a
  parent limit; it may only narrow one.
- [ ] **Step 5: Reserve nested fan-out before task creation.** Ensemble/panel
  validate against `max_fanout`, then atomically reserve child subagent and
  model leases. The parent provider may already hold one model slot while
  blocked on its coordination tool; if no child model slot is available,
  return a teaching capacity error immediately instead of waiting. Create only
  the tasks covered by the lease and release unused reservations on every
  failure/cancellation path.
- [ ] **Step 6: Fix acquisition order.** The only order is fan-out reservation
  → subagent lease → model lease → leaf-tool lease. A child never acquires in
  reverse order. Add nested panel-inside-ensemble, `max_concurrent_models=1`,
  cancellation-while-reserving, and partial-child-start regression tests with
  short test-level timeouts.
- [ ] **Step 7: Define turn behavior.** A pre-call budget refusal returns a
  visible `[stopped: budget ...]` result and records `BudgetExceeded`; it is not
  retried. A limit crossed only after usage arrives prevents subsequent calls
  and remains fully accounted.
- [ ] **Step 8: Extend telemetry and docs.** Show configured limits, observed
  totals, and stop cause per run. Do not label unknown provider cost as zero.
- [ ] **Step 9: Commit** `feat(runtime): share budgets and bound concurrent fan-out`.

**Acceptance:** Stress tests cannot exceed configured active provider/tool/
subagent counts, and all children charge the same top-level run ledger.

---

### Task 14: Make live projections loss-aware and resume them from the log

**Purpose:** Keep TUI status, telemetry, plugins, and the activity panel honest
when subscribers start late or fall behind.

**Files:**

- Modify: `src/harness/session.py`
- Create: `src/harness/projection.py`
- Modify: `src/harness/telemetry.py`
- Modify: `src/harness/tui.py`
- Modify: `src/harness/tui_panel.py`
- Modify: `src/harness/plugins.py`
- Test: `tests/test_session.py`
- Create: `tests/test_projection.py`
- Test: `tests/test_telemetry.py`
- Test: `tests/test_tui.py`
- Test: `tests/test_tui_panel.py`
- Test: `tests/test_plugins.py`

**Interfaces produced:**

- `GapNotice(session_id, after_seq, before_seq, dropped_count)`.
- `Subscription.get() -> Envelope | GapNotice` plus `last_seq` and
  `dropped_count`; replace raw queues returned by `SubscriberBus.subscribe`.
- `SessionProjection`, a pure fold for UI-facing provider/model/profile,
  current calls, tools, usage, cost, budget, and session outcome.
- `Session.current_seq` and `replay_range(base, session_id, after_seq,
  through_seq)` for a subscriber to recover a closed canonical interval after
  late subscription or a gap.

- [ ] **Step 1: Add overflow tests.** Fill a tiny subscription, publish more
  envelopes, and assert one explicit GapNotice reports the exact missing range.
  The publisher remains nonblocking.
- [ ] **Step 2: Implement the subscription wrapper.** Never place a synthetic
  gap event in the canonical log. Coalesce adjacent drops into one notice and
  deliver it before the next surviving envelope.
- [ ] **Step 3: Seed without a subscribe/replay race.** Subscribe first, capture
  `high_water = session.current_seq` on the owning loop, replay JSONL through
  that sequence by parsing/folding immutable envelopes in a worker thread, and
  buffer live envelopes above the high water. Apply SQLite/projection updates
  in bounded batches on the owning loop—never use the live SQLite connection
  from the worker—then apply buffered events by sequence and continue live.
  Show `refreshing from log` so a long session never freezes Textual. Add an
  event appended between subscribe and replay completion regression proving no
  gap or duplicate. If the bounded live subscription itself reports a gap
  during replay, restart from the last applied canonical sequence rather than
  trusting the partial buffer.
- [ ] **Step 4: Recover projection gaps.** TUI telemetry and ActivityPanel use
  `replay_after` and rebuild their projection. During replay, show a visible
  `refreshing from log` state. If replay fails, show `status incomplete` with a
  doctor/verify command; never display stale totals as complete.
- [ ] **Step 5: Refresh panel rows on each projected change.** `record()` must
  trigger a batched Textual refresh at most once per event-loop tick; the panel
  cannot wait until toggle or turn completion.
- [ ] **Step 6: Update plugin subscriber contract.** Deliver GapNotice to
  subscribers that opt into the v2 interface. Legacy subscribers are paused
  and receive one out-of-band subscriber error callback explaining they missed
  events; do not describe that callback as a canonical `ErrorRaised` unless the
  session owner separately appends one. Never append recursively from inside
  `SubscriberBus.publish`.
- [ ] **Step 7: Commit** `fix(projections): recover subscriber gaps from the event log`.

**Acceptance:** Resume immediately shows a truthful refreshing state, then
historical usage/tool state without blocking the UI. Forced queue overflow
either reconstructs exact state from the log or visibly marks the projection
incomplete.

---

### Task 15: Add TUI identity, onboarding, and turn-boundary model switching

> **PLAYABILITY CHANGE:** Keep routine identity information compact, put full
> trust detail one action away, and let an already-authenticated supported CLI
> reach a real-model composer without manual TOML editing.

**Purpose:** Ensure the user always knows what model and trust boundary will
act before submitting work.

**Files:**

- Modify: `src/harness/tui.py`
- Create or modify: `src/harness/tui_support.py`
- Create: `tests/tui_screen.py`
- Test: `tests/test_tui.py`
- Test: `tests/test_tui_support.py`
- Modify: `docs/user-guide.md`

**Interfaces produced:**

- Compact `#identitybar` at the top: alias, profile/tier warning, current
  activity, and queued-prompt count. `/status` and the expanded identity view
  show the resolved route, backend/version, both network boundaries, workspace,
  session short ID, and enabled MCP count; do not force all of that metadata
  into every row of the normal work surface.
- `IdentityViewModel` pure formatter with plain-text and Rich renderers.
- `_pending_model: ModelId | None`; model changes during a turn apply only after
  that turn reaches a terminal state.
- First-run onboarding screen when no usable configured model exists, including
  a guided path for a discovered, already-authenticated supported provider.

- [ ] **Step 1: Build the shared final-screen helper.** In
  `tests/tui_screen.py`, render `app.screen._compositor.render_strips()`,
  preserve whitespace/cell positions, and normalize only explicitly supplied
  IDs/timestamps/elapsed values. Add a self-test proving text present in widget
  state but hidden by the compositor fails the helper. Task 18A reuses this
  file; it must not create a second renderer.
- [ ] **Step 2: Add view-model tests.** Cover compact and expanded production
  strict views, compatible limitations, unsafe warning, unknown version, demo
  Echo, narrow rendering, queued count, activity state, and strings containing
  ANSI/control characters.
- [ ] **Step 3: Mount the identity bar.** It must exist before the first prompt,
  use text labels in addition to color, and update after model switch, resume,
  clear, profile change, MCP selection, and provider probe refresh.
- [ ] **Step 4: Add model-switch tests.** Outside a turn `/model alias` applies
  immediately after policy validation. During a running turn it sets
  `_pending_model`, displays `next: alias`, and the current iteration and all
  tool follow-ups stay on the original model. Apply after success, failure, or
  user cancellation. A rejected pending model leaves the current model intact.
- [ ] **Step 5: Implement the boundary.** Snapshot model/provider status at
  turn start and pass that model through every iteration of `run_turn`; do not
  reread mutable `loop.model` mid-turn. Apply pending changes in one finalizer.
- [ ] **Step 6: Replace silent Echo startup.** If no model is configured, show
  an onboarding modal with exact config path, `doctor` command, discovered
  providers, and actions for Use Discovered Provider, Demo, or Quit. The guided
  action previews the alias/profile it will write, confirms once, atomically
  creates a private user config, runs the ordinary live model preflight, and
  focuses the composer on success; it never requests a credential already
  expected in an environment variable or provider login. Demo sets a
  persistent identity-bar label for that app run.
- [ ] **Step 7: Show policy refusal before teardown.** Model selection probes
  and validates before replacing the current provider/catalog snapshot. A
  failure is actionable and leaves the working session untouched.
- [ ] **Step 8: Add final compositor assertions for the bar and onboarding.**
  Use the Step 1 helper and assert transcript/prompt remain visible, not merely
  that widgets exist.
- [ ] **Step 9: Commit** `feat(tui): expose trust state and switch models at turn boundaries`.

**Acceptance:** At every prompt, the screen compactly identifies the effective
model and trust profile, full detail is one action away, and an already logged-
in supported CLI can reach the composer from clean Harness config without
editing TOML. A logical turn never changes models halfway through.

---

### Task 16: Replace the prompt with a multiline composer and make permission review complete

> **PLAYABILITY CHANGE:** The composer remains editable during active work and
> retains text after a busy or failed submission; Task 18C adds the bounded
> follow-up queue that accepts that submission.

**Purpose:** Make serious prompt entry and consequential approvals safe and
comfortable.

**Files:**

- Modify: `src/harness/tui.py`
- Modify: `src/harness/tui_support.py`
- Test: `tests/test_tui.py`
- Test: `tests/test_tui_support.py`
- Modify: `docs/user-guide.md`

**Composer contract:** Use a Textual `TextArea` subclass named
`PromptComposer`. Enter submits and Ctrl+J inserts a newline; Textual currently
maps carriage-return Enter separately from newline/Ctrl+J. Pasted newlines are
retained and Esc keeps its global cancellation meaning. Show the binding hint
beside the editor. Live-probe Windows Terminal through WSL2, tmux, and the
supported xterm before acceptance. Alt+Enter may be an additional binding only
where the terminal protocol distinguishes it; never make it the sole newline
path. `[ui].newline_key` may select another tested, non-conflicting binding.

- [ ] **Step 1: Fix history drafts independently.** Extend `HistoryRing` to
  stash the current unsent draft on the first `prev()` and restore it when
  navigating forward past the newest history entry. Add empty, duplicate, and
  multi-step tests.
- [ ] **Step 2: Add composer tests.** Cover multiline editing, paste, submit,
  history/draft restoration, mention completion inside a line, slash command
  detection only when the entire trimmed composer starts with `/`, editing
  while a turn is running, and draft retention after a busy or failed submit.
  Feed raw `\r` and `\n` parser inputs and assert Enter submits while Ctrl+J
  inserts rather than relying only on direct action calls.
- [ ] **Step 3: Implement PromptComposer.** Move mention completion behavior
  from HistoryInput without synchronous filesystem work on the event loop.
  Clear only after the submission has been accepted and logged.
- [ ] **Step 4: Create `PermissionPresentation`.** Purely derive title, source,
  risk, summary, grant scope, and full detail from `PermissionRequest` plus
  ToolSpec. Cap the initial detail view at 64 KiB: retain at most 32 KiB of
  UTF-8-safe head and 32 KiB of UTF-8-safe tail, disclose original/retained
  sizes, and provide an expand action that reads the already-held request, not
  the filesystem.
- [ ] **Step 5: Add permission-screen tests.** Cover read/write/edit/bash/MCP,
  unknown risk, destructive risk, model permission, long commands, full write
  content, exact edit diff, ANSI/control text, and concurrent stacked requests.
  The screen must say whether `always` means session-only or persisted and show
  the precise generated match pattern. Add parent and subagent requests with
  the same tool/call suffix and prove the modal identifies the correct
  agent/session. Headless equivalents deny immediately and never construct a
  screen.
- [ ] **Step 6: Implement review controls.** `y` allow once, `a` allow for
  session, `p` persist, `n` deny, `d` toggle full details. Persist is absent or
  disabled when no writable grant store exists. Never overload `always` with
  two scopes. `p` opens a second confirmation that repeats the exact persisted
  pattern and scope; only its affirmative action writes the atomic grant store.
- [ ] **Step 7: Add contextual diffs.** Write shows target plus content preview;
  edit shows a unified diff; bash shows the full argv/command and cwd; MCP shows
  server/tool and structured args. Escape denies only the top modal.
- [ ] **Step 8: Validate final compositing.** Permission text, diff, composer,
  and underlying transcript all remain visible at 80x24 and 120x40.
- [ ] **Step 9: Commit** `feat(tui): add multiline prompts and complete permission review`.

**Acceptance:** Users can compose and recover multiline prompts and can inspect
the complete consequential action and persistence scope before approving it.

---

### Task 17A: Make commands, models, tools, and live work discoverable

**Purpose:** Replace raw one-line listings with operational interfaces built on
the canonical live projection.

**Files:**

- Modify: `src/harness/tui.py`
- Modify: `src/harness/tui_panel.py`
- Modify: `src/harness/tui_support.py`
- Test: `tests/test_tui.py`
- Test: `tests/test_tui_panel.py`
- Test: `tests/test_tui_support.py`
- Modify: `docs/user-guide.md`

**Interfaces produced:**

- Searchable `CommandPaletteScreen`, `ModelPickerScreen`, and
  `ToolBrowserScreen`.
- Activity rows with call-ID short form, parent/agent identity, tool/model,
  state, elapsed, known tokens/cost, and terminal error/result summary.

- [ ] **Step 1: Add the command palette.** `/help` opens a searchable screen
  showing command, arguments, summary, source/trust, and current availability.
  Task 18B consumes the same command descriptors for plain/headless help.
- [ ] **Step 2: Add the model picker.** `/model` without an argument opens it;
  show alias, route, backend/version, tier, both network boundaries,
  accounting/content capabilities, cost/context, probe state, and limitations.
  Selection uses Task 15's preflight/pending semantics.
- [ ] **Step 3: Add the tool browser.** `/tools` opens it; show canonical name,
  description, source, risk, concurrency class, enabled/quarantined state,
  schema summary, and permission default. Never dump an enormous schema into
  the transcript.
- [ ] **Step 4: Make ActivityPanel responsive.** Replace fixed width 44 with a
  min/max responsive width and collapse to a full-screen overlay on narrow
  terminals. Refresh from `SessionProjection` on each batch. Add cancellation
  only for currently owned cancellable tasks; never imply a historical process
  can be cancelled.
- [ ] **Step 5: Make workflow results visible.** A coordination row expands to
  its experts/critics and shows the selected/final result or explicit failure.
  On terminal completion, render one concise transcript summary from canonical
  terminal facts with a stable command to open full details; do not append a
  second invented result event or leave results only in an off-screen panel.
- [ ] **Step 6: Add keyboard-only journeys.** Cover command search, model
  selection, tool inspection, live panel update, nested workflow expansion,
  cancellation, and escape. Assert navigation does not append unintended
  domain events.
- [ ] **Step 7: Commit** `feat(tui): add searchable operational surfaces`.

**Acceptance:** A user can discover commands, understand provider/tool trust,
and inspect live multi-agent work without knowing raw event schemas.

---

### Task 17B: Make session inspection, export, and recovery operable

**Purpose:** Provide safe session lifecycle operations without combining them
with the live activity UI implementation.

**Files:**

- Modify: `src/harness/sessions.py`
- Modify: `src/harness/session_admin.py`
- Modify: `src/harness/tui.py`
- Modify: `src/harness/cli.py`
- Test: `tests/test_sessions.py`
- Test: `tests/test_session_admin.py`
- Test: `tests/test_tui.py`
- Test: `tests/test_cli.py`
- Modify: `docs/user-guide.md`

**Interfaces produced:**

- Extended `SessionSummary`: first-prompt preview, last model,
  current/weakest profile, tier, outcome, event count, known cost, last
  activity, filesystem/lock/integrity state.
- `SessionPickerScreen` with search, preview, resume, verify, export, and trash.
- CLI `sessions list/show/verify/repair/export/trash/restore/purge`, all supporting
  stable JSON where meaningful and Task 5's exit mapping. `verify` accepts
  `--quick|--full`; repair/export default to and require full verification.

- [ ] **Step 1: Extend SessionSummary from log truth.** Read/fold without
  mutation; malformed sessions remain visible with error status. Preview text
  has an explicit truncation marker. Cover old logs, child sessions, profile
  downgrade history, and a stale derived telemetry database.
- [ ] **Step 2: Implement deterministic export.** Tar members are sorted;
  header mtime/uid/gid/uname/gname are fixed; modes are normalized to private
  values. Include canonical JSONL, every available referenced session-local
  blob, and an integrity manifest with hashes and format version. An authorized
  unavailable artifact is listed with its marker/reason and no invented member.
  The default export
  contains the complete already-redacted stored transcript. An explicit
  `--metadata-only` export omits event/blob content and lists every omission in
  the manifest.
- [ ] **Step 3: Implement safe trash and purge.** Require an unlocked verified
  session, explicit confirmation, and an atomic rename into a 0700 trash
  directory on the same filesystem. Purge is a separate command with a second
  exact-ID confirmation and reports that recovery is impossible. Provide
  `sessions restore ID` before purge and refuse collisions. Tests never use
  broad paths or globs.
- [ ] **Step 4: Wire authorized repair.** `sessions repair` first prints the
  integrity report/hash, then requires exact session ID plus operation (or
  `--yes --report-sha256 HASH`). Pass Task 10's `RepairAuthorization`; reject a
  changed report, live lock, or unspecified repair. Artifact-unavailable repair
  additionally requires the exact blob digest.
- [ ] **Step 5: Expand the session picker.** Show search, preview,
  model/profile/outcome, verify/export/trash actions, and clear
  lock/corruption status. Destructive actions repeat the exact target in a
  second confirmation.
- [ ] **Step 6: Add CLI and UI journeys.** Cover list/show, resume, verify,
  deterministic export, stale-repair refusal, trash/restore/purge, and
  purge. Assert no operation mutates a locked session.
- [ ] **Step 7: Commit** `feat(sessions): add safe inspection export and recovery`.

**Acceptance:** Sessions can be inspected, verified, exported, repaired, and
removed through explicit recoverable operations. Labels, pins, and archive
metadata are deferred rather than expanding this beta-critical task.

---

### Task 18A: Add final-compositor, terminal-size, and accessibility gates

**Purpose:** Prevent UI backing state from passing while the composited terminal
is unreadable or incomplete.

**Files:**

- Create: `tests/tui_golden/`
- Create: `tests/test_tui_compositor.py`
- Create: `scripts/capture_tui_golden.py`
- Reuse: `tests/tui_screen.py` from Task 15
- Modify: `src/harness/tui.py`
- Modify: `src/harness/tui_panel.py`
- Modify: `pyproject.toml` if a snapshot dependency is deliberately selected
- Create: `docs/tui-testing.md`

**Golden set:** Keep exactly 14 reviewed default-theme 80x24 goldens: empty
startup, onboarding, idle identity/prompt, streaming thought plus prose,
Markdown/code/table, long Unicode/combining text, read permission, edit diff,
MCP checklist, model picker, session picker, expanded multi-agent workflow,
retry/error, and resumed session. Do not create a golden for every Cartesian
combination.

**Structural matrix:** Run each state at 60x20, 80x24, and 120x40 under default,
no-color, and high-contrast modes using assertions rather than snapshots.

- [ ] **Step 1: Reuse the deterministic screen helper.** Import Task 15's
  compositor helper; do not duplicate it or inspect only widget backing data.
  Golden normalization is limited to explicitly supplied IDs, elapsed values,
  and timestamps and preserves whitespace/cell positions.
- [ ] **Step 2: Add structural assertions beside goldens.** For every matrix case,
  assert critical prose appears exactly once, prompt and identity bar remain
  visible, modals do not erase surrounding content, and no raw ANSI/control
  characters survive. Assert narrow screens use the documented overlay/collapse
  behavior rather than clipping controls.
- [ ] **Step 3: Add no-color/high-contrast modes.** Status must never be encoded
  solely by color; include words/icons with text alternatives. Focus indication
  remains visible in both modes.
- [ ] **Step 4: Audit keyboard navigation.** Every interactive element is
  reachable and escapable; focus returns to the composer after closing a modal;
  global cancellation does not accidentally approve/deny a background modal.
- [ ] **Step 5: Add a manual terminal smoke checklist.** Cover Windows Terminal
  through WSL2, tmux, a basic xterm-compatible terminal, resize during stream,
  large paste, Enter/Ctrl+J, Ctrl-C/Esc, and reconnect/resume. Record raw-key
  probe outcome, Harness/Textual/terminal versions, and compositor result.
- [ ] **Step 6: Run the complete matrix in CI.** Golden updates require an
  explicit command and reviewed diff; normal tests never auto-update them.
- [ ] **Step 7: Commit** `test(tui): gate release on final compositor and accessibility`.

**Acceptance:** The final composed cells—not merely widget state—show all
critical surrounding prose and controls across the supported terminal matrix.

---

### Task 18B: Add a plain interactive terminal frontend

**Purpose:** Provide a separate accessible frontend instead of hiding a second
UI implementation inside compositor testing.

**Files:**

- Create: `src/harness/plain.py`
- Modify: `src/harness/cli.py`
- Modify: `src/harness/interaction.py`
- Create: `tests/test_plain.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_permissions.py`
- Modify: `docs/user-guide.md`

**Interfaces produced:**

- `PlainFrontend` consumes the same Kernel, SessionProjection, command
  descriptors, and provider/tool events as Textual.
- `PlainResolver` renders Task 16's `PermissionPresentation` and accepts only
  full words: `allow-once`, `allow-session`, `persist`, `deny`, `details`.

- [ ] **Step 1: Define mode selection.** `harness --plain` is interactive and
  requires a TTY unless `--input FILE` is supplied. Existing `-p` remains
  noninteractive/headless and uses `HeadlessResolver`; it never starts a hidden
  permission prompt. A normal input line submits one turn. `:compose` enters
  multiline mode, an exact `:send` line submits, `:cancel` discards, and
  `::send`/`::cancel` escape literal sentinel lines; print these rules on entry.
- [ ] **Step 2: Add stream/render tests.** Script input and capture output for
  startup identity, multiline prompt framing, streaming text, tool call/result,
  retry/error, subagent/workflow activity, and resume. Output remains useful
  with `NO_COLOR=1` and contains no cursor-control sequences.
- [ ] **Step 3: Implement complete permission review.** Print source, risk,
  agent identity, exact action summary, and full detail on request. Persist
  repeats the generated pattern and requires a second full-word confirmation.
  EOF or Ctrl-C denies the request and closes its terminal fact.
- [ ] **Step 4: Share command descriptors.** Help, model/tool/session listing,
  doctor guidance, and workflow-detail commands use the same data models as the
  TUI; no separate policy or session logic is allowed in `plain.py`.
- [ ] **Step 5: Handle signals and terminal restoration.** First SIGINT cancels
  the active turn and records it; a second exits. SIGTERM performs bounded
  provider/process cleanup and session close. Tests inject signals into fake
  children and assert no prompt/control-state corruption on restart.
- [ ] **Step 6: Commit** `feat(cli): add accessible plain terminal frontend`.

**Acceptance:** Unsupported terminals and screen readers can operate the same
dispatcher, permissions, models, agents, and sessions without Textual or an
implicit web UI.

---

### Task 18C: Make the core interaction loop playable and measure it

> **NEW TASK:** This entire task was added by the playability revision.

**Purpose:** Make sustained daily use feel responsive and under the user's
control. This task treats fast feedback, visible momentum, preserved input,
easy interruption, and recovery from ordinary failure as release behavior—not
subjective polish to consider after correctness.

**Files:**

- Create: `src/harness/playability.py`
- Modify: `src/harness/interaction.py`
- Modify: `src/harness/tui.py`
- Modify: `src/harness/tui_panel.py`
- Modify: `src/harness/plain.py`
- Modify: `src/harness/cli.py`
- Create: `scripts/measure_playability.py`
- Create: `tests/test_playability.py`
- Test: `tests/test_tui.py`
- Test: `tests/test_tui_panel.py`
- Test: `tests/test_plain.py`
- Test: `tests/test_cli.py`
- Create: `docs/playability-report.schema.json`
- Create: `docs/playability.md`
- Modify: `docs/user-guide.md`

**Interfaces produced:**

- `ActivityPhase`: `idle`, `queued`, `preparing`, `contacting_provider`,
  `streaming`, `waiting_permission`, `running_tool`, `delegating`, `retrying`,
  `cancelling`, `completed`, `failed`, and `cancelled`.
- `ActivityStatus(phase, summary, started_at, last_progress_at, cancellable,
  recovery_action)`, derived from canonical session facts plus local compositor
  observations. UI-only paint/keypress observations never enter the canonical
  session log.
- `PromptQueue(max_items=8)` with stable item IDs, FIFO order, inspect/remove/
  clear operations, and an explicit paused state after failure or cancellation.
- `InteractionTrace`, an in-memory sanitized measurement stream. Persistence is
  opt-in through `--ux-trace PATH`, uses an atomic 0600 file, and records only
  phase/timing/size/count data plus randomized IDs—never prompts, tool arguments,
  results, credentials, filesystem paths, or model thoughts.
- `InteractionBudget` and a versioned `PlayabilityReport` consumed by the final
  qualification bundle.

**Reference interaction budgets:** Measure Harness-added latency with a fake
streaming provider and injected tool/permission events so provider/network time
is excluded. The release measurement runs on the scoped WSL2 reference machine
and records hardware, load, terminal, tmux, Python, Textual, and Harness
versions. The gate is p95 over the sample counts below:

| Interaction | Budget | Minimum sample |
|---|---:|---:|
| Process start to focused usable composer | 1,500 ms | 10 cold starts |
| Textual key event to changed composited cell | 50 ms | 200 keys |
| Accepted submit to visible working state | 100 ms | 30 turns |
| Provider chunk received to visible composited cell | 100 ms | 100 chunks |
| Open command/model/tool/activity surface | 200 ms warm, 500 ms cold | 30/10 opens |
| Cancel input to visible `cancelling` state | 100 ms | 20 cancels |
| Cancel input to fake provider/tool tree settled | 3,000 ms | 20 cancels |

An active operation may be externally quiet, but after two seconds the UI must
show its concrete phase and elapsed time and refresh elapsed time at least every
second. This is a legibility rule, not fake progress. Live time to first model
output is recorded as provider latency and has no cross-provider pass threshold;
the two Harness overhead intervals around it retain the 100 ms budgets above.
Unit tests use an injected monotonic clock and deterministic compositor probes;
wall-clock budgets run in Task 19B, not as flaky generic CI assertions.

- [ ] **Step 1: Specify the activity state machine before changing widgets.**
  Add table-driven RED tests for every legal transition, concurrent parent/
  subagent activity, provider retry, permission wait, tool execution,
  cancellation, and terminal state. Reject impossible regressions such as
  `completed -> streaming`. Preserve agent identity and current provider/model
  in detail views without making them part of the compact phase label.
- [ ] **Step 2: Make all waiting legible.** Wire the TUI activity line and plain
  frontend to `ActivityStatus`. A normal screen shows one compact top-level
  phase; the activity panel expands per-agent/provider/tool detail. After two
  seconds show elapsed time and update it without appending log events. A
  provider reconnect says `retrying provider (2/3)`, a permission wait names
  the requesting agent and action class, and tool work names the safe summary.
  Never show a generic spinner when a more specific phase is known.
- [ ] **Step 3: Keep the composer live and queue follow-ups.** While a turn is
  active, keep the editor fully usable. Enter validates and adds the composed
  prompt to the bounded queue, then clears only after the queue acknowledges
  it; a full queue leaves the text untouched with an actionable message. Show
  the next prompt preview plus total count, and provide `/queue`,
  `/queue remove ID`, `/queue clear`, and equivalent keyboard-accessible TUI
  actions. Do not append `UserMessage` until an item actually starts, so edits
  and removals are not fictional conversation history. After success start the
  next item automatically; after failure or cancellation pause the queue and
  require an explicit Resume Queue action. Pending Task 15 model switches apply
  before the next queued turn. Clean exit with a nonempty queue requires
  confirmation because unsubmitted queued text is deliberately memory-only and
  is not claimed to survive a process crash.
- [ ] **Step 4: Add safe edit/retry recovery.** `/edit-last` copies the last
  user prompt into the composer without dispatching or changing history.
  `/retry` is available only for a failed or cancelled turn and creates a new
  turn with a visible link to the prior turn. It may proceed directly if only
  read-only tools completed. If any write, execute, network, or destructive
  action completed, require confirmation stating that retry does not rewind
  side effects and list the completed action summaries. Permission checks and
  budgets run again normally. Cover repeated failure, refusal, cancellation,
  resume, and an unavailable prior artifact; never reconstruct missing text.
- [ ] **Step 5: Remove first-run and common-action friction.** Starting
  `harness` with clean Harness config and one already-authenticated supported
  CLI in `PATH` must reach a focused real-model composer in at most three
  deliberate choices and without manual TOML editing. Model switch, open live
  work, retry/edit a failed request, and resume a recent session must each be at
  most two deliberate actions from the normal work surface. Keep the default
  identity row compact; put routes, versions, network boundaries, session ID,
  workspace, and MCP inventory in the one-action expanded status screen. No
  informational modal may prevent the user from continuing to type.
- [ ] **Step 6: Implement sanitized measurement.** Instrument process-ready,
  key-received, cell-painted, submit-accepted, working-state-painted,
  provider-chunk-received, chunk-painted, surface-open requested/painted,
  cancellation requested/painted, and process-tree settled milestones. Match
  paired observations by randomized in-memory IDs and reject negative,
  unmatched, duplicated, or cross-session samples. `measure_playability.py`
  drives the fixed sample counts, checks the table above, emits the versioned
  JSON report, and exits nonzero on a missing sample or exceeded budget. Add a
  trace privacy test using seeded secrets in prompts, paths, tool arguments,
  results, and provider output.
- [ ] **Step 7: Test the complete interaction rather than isolated backing
  state.** Add deterministic journeys for type/submit/stream, queue/edit/remove,
  permission wait, nested mixed-model workflow, cancellation, failure/retry,
  model switch at a turn boundary, resume, and clean exit with queued work.
  Assert final composited cells show the draft, queue count, current phase,
  result/error, and recovery action together. Add queued, cancelling, and
  paused-after-failure cases to Task 18A's structural matrix; do not increase
  the fixed 14-golden set merely to avoid stronger structural assertions.
- [ ] **Step 8: Write the candidate dogfood protocol.** Define six named
  journeys: clean-config first success; a ten-turn inspect/edit/test loop with
  permissions and queued follow-ups; API-to-subscription model switching; a
  nested mixed-model workflow; provider failure followed by edit/retry; and
  provider/tool cancellation followed by queue recovery and session resume.
  For each, record completion, elapsed time, decisions/approvals, unexplained
  gaps over two seconds, lost/duplicated input, recovery attempts, and issue
  severity without storing raw prompts. P0 means data loss, an unintended
  consequential action, or inability to operate Harness; P1 means a journey
  cannot be completed from product surfaces, input is lost/duplicated, state is
  materially misleading, or recovery requires a shell, config/log inspection,
  or restart; P2 is completable friction with an in-product workaround; P3 is
  cosmetic. Have a person who did not implement Task 18C run the clean-config
  journey without live coaching. Passing requires every journey to finish,
  zero lost or duplicated prompts, no unexplained busy state over two seconds,
  the action-count limits in Step 5, zero P0/P1 usability findings, and one
  continuous 90-minute real-work session on the exact candidate. Provider wait
  time is disclosed separately. The same P2 friction observed in two journeys
  is promoted to P1; at most three other unresolved P2 findings may remain, and
  each needs an owner, disposition, and release-note limitation.
  Define `docs/playability-report.schema.json` here; Task 19B's qualification
  schema references this report by SHA-256 instead of duplicating its fields.
- [ ] **Step 9: Commit** `feat(ux): gate the playable interactive loop`.

**Acceptance:** The work loop responds immediately, always explains active
work, preserves composed input, accepts useful follow-ups, and recovers from an
ordinary failure without forcing the user into configuration files or logs.
The deterministic interaction model is green; the wall-clock budgets and
dogfood journeys remain explicit blocking inputs to Task 19B.

---

### Task 18D: Renew provider evidence against the final runtime surface

**Purpose:** Prevent earlier conformance/containment results from surviving
later changes to transcript, redaction, environment, MCP, or sandbox behavior.

**Files:**

- Modify: `src/harness/provider_compat.toml`
- Modify: fixture manifests beneath `tests/fixtures/<provider>/`
- Create or modify: evidence beneath `tests/containment/evidence/`
- Modify: `docs/provider-support.md`
- Modify: `docs/security/provider-containment.md`

**Verification surfaces:** Every record includes common files
`provider.py`, `provider_transcript.py`, `dispatcher.py`, `runtime_policy.py`,
`redaction.py`, and `tools.py`, plus detected Pydantic and MCP versions. API
records additionally include `catalog.py`, `provider_litellm.py`, and the
detected LiteLLM version. CLI records additionally include
`process_env.py`, `process_sandbox.py`, `mcp_serve.py`, and their exact adapter
module. Add a dependency only when it can change provider-visible content,
capability policy, tool execution, or containment; schema tests pin each list.

- [ ] **Step 1: Prove stale evidence fails.** Change one byte in each category
  under a temporary copied package and assert `ProviderStatus.verified=False`
  with a renewal command. Changing docs/tests alone does not affect the surface.
- [ ] **Step 2: Renew replay fixtures.** Record/review every required Task 8
  ScenarioId for all four Task 5 support targets, sanitize secrets, and update
  deterministic fixture manifests. Missing credentials leave the record
  unsatisfied and stop the task.
- [ ] **Step 3: Renew live containment.** Run Tasks 9A-9B for Claude Code and
  Codex on the scoped release platform. Store sanitized evidence containing
  source-surface hash, CLI/bubblewrap/kernel versions, and per-boundary facts.
  Antigravity evidence may be stored as a failed/steered optional record but
  cannot satisfy a required target.
- [ ] **Step 4: Update manifests and generated docs.** Enter only recorder-
  produced hashes, version ranges, and limitations; regenerate provider support
  and containment docs and review the exact diff. Do not hand-edit `verified`
  to make the floor pass.
- [ ] **Step 5: Verify and commit evidence.** Run conformance, live containment,
  package-resource, and diagnostics tests; then commit only manifests, fixtures,
  evidence, and generated docs as `test(providers): renew production evidence`.
  Recompute after the commit and prove the provider source-surface hash is
  unchanged by that evidence-only commit.

**Acceptance:** Every required provider record matches the final provider
runtime bytes that enter Task 19A. Any later verification-surface change sends
the plan back to this task.

---

### Task 19A: Build deterministic release evidence and artifacts

> **PLAYABILITY CHANGE:** Deterministic interaction behavior and report schemas
> are now release inputs; hardware-sensitive timing remains an exact-candidate
> qualification rather than a flaky generic-CI assertion.

**Purpose:** Make every automated release gate reproducible, named, and fast
enough to rerun from a clean checkout.

**Files:**

- Create: `scripts/release_check.py`
- Create: `scripts/build_release.py`
- Create: `src/harness/release_gate.py`
- Create: `.github/workflows/release.yml`
- Create: `tests/test_release_check.py`
- Create: `tests/test_release_gate.py`
- Create: `tests/qualification/test_faults.py`
- Modify: `pyproject.toml` and `uv.lock` for pinned audit/SBOM tooling
- Create: `.gitleaks.toml`
- Create: `security-allowlist.toml`
- Create: `docs/release-checklist.md`

**Selected supply-chain policy:** `pip-audit` scans the locked runtime
dependencies; any advisory fails unless `security-allowlist.toml` names its
CVE/GHSA, rationale, owner, and unexpired removal date. `cyclonedx-bom`
generates a CycloneDX JSON SBOM from the clean release environment. A pinned,
SHA-256-verified `gitleaks` binary scans the repository; ignored fingerprints
require a reviewed rationale. Tagged artifacts are signed keylessly with
Sigstore's GitHub OIDC action pinned by full commit SHA and include the
certificate/bundle. No scanner silently downgrades errors to warnings.

- [ ] **Step 1: Build deterministic fault gates.** With fake providers and
  injectable storage, cover killed provider, killed Harness between every
  intent/publication boundary, ENOSPC, expired-auth response, unavailable MCP,
  subscriber overflow, max fan-out, resume, and export. These tests run in CI;
  they never fill the host disk, consume paid providers, or depend on wall-clock
  hours.
- [ ] **Step 2: Implement `release_check.py`.** From a clean checkout verify
  Ruff, full tests, zero required conformance skips, live-evidence document
  schemas/hashes, fixture and compatibility hashes, generated provider-support
  docs with no diff, the playability-report schema and deterministic
  interaction tests, 14 UI goldens plus the structural matrix, wheel/sdist,
  clean-wheel smoke, audit/SBOM/secret policy, and `git diff --check`. Emit
  deterministic JSON with command/tool versions, commit, platform, inputs, and
  artifact hashes. Do not run hardware-sensitive wall-clock budgets in generic
  CI; require their exact-candidate report in the final gate.
- [ ] **Step 3: Implement the non-vacuous preflight gate.** For every exact
  record in `release_support.toml`, require matching verified compatibility
  data, complete Task 8 scenarios, and current embedded containment evidence
  when required. Assert the four initial IDs individually in tests so deleting
  or replacing records with fakes cannot turn the gate green. Final mode also
  accepts an external Task 19B qualification bundle and verifies its commit and
  artifact hashes.
- [ ] **Step 4: Make builds reproducible.** Set `SOURCE_DATE_EPOCH` from the
  commit timestamp, build wheel/sdist twice in separate clean temporary trees,
  and fail if corresponding SHA-256 hashes differ. Do not reuse local build or
  virtualenv state. Adding audit/SBOM tools must not update a provider
  verification dependency; if lock regeneration changes Pydantic, MCP, or
  LiteLLM, return to Task 18D before proceeding.
- [ ] **Step 5: Implement supply-chain outputs.** Generate SBOM, audit report,
  gitleaks report, checksums, compatibility/support matrices, and evidence JSON.
  Test allowlist schema/expiry and scanner-command failure. Reports contain no
  credentials or full transcripts.
- [ ] **Step 6: Add the release workflow.** Pull requests run all deterministic
  gates but never sign/upload. A `workflow_dispatch` release job accepts only an
  existing annotated beta tag, downloads the already-uploaded qualification
  bundle/final evidence from that tag's draft release, verifies them, rebuilds
  from the tagged clean checkout, reruns all gates, signs every source and
  qualification artifact via the selected Sigstore action, and uploads wheel,
  sdist, SBOM, checksums, signatures/bundles, matrices, and evidence to the same
  draft. No PyPI step exists.
- [ ] **Step 7: Commit** `build(release): add deterministic evidence and signed artifacts`.

**Acceptance:** Two clean builds match byte-for-byte, every automated gate has
a named failure policy, and a candidate workflow can create a signed draft
without claiming manual qualification occurred.

---

### Task 19B: Construct and qualify the exact untagged beta candidate

> **PLAYABILITY CHANGE:** Candidate qualification now requires the Task 18C
> latency report, all six dogfood journeys, terminal queue/recovery validation,
> and the continuous 90-minute real-work session.

**Purpose:** Put the production default/version into the actual candidate
before collecting nondeterministic evidence, so the evidence can bind the exact
commit and artifacts without a self-referential checked-in report.

**Files changed before the candidate commit:**

- Create: `scripts/record_qualification.py`
- Create: `docs/qualification/schema.json`
- Create: `docs/qualification/README.md`
- Modify: `src/harness/cli.py`
- Modify: `src/harness/diagnostics.py`
- Create or modify: `CHANGELOG.md`
- Create or modify: `SECURITY.md`
- Modify: `README.md`
- Modify: `docs/user-guide.md`
- Modify: `docs/architecture.md`
- Modify: `docs/release-checklist.md`
- Modify: `pyproject.toml` (`0.1.0b1` for the untagged candidate)
- Test: `tests/test_cli.py`
- Test: `tests/test_diagnostics.py`
- Test: `tests/test_package_metadata.py`

**Evidence location:** Per-run qualification JSON is written beneath
`dist/qualification/<commit>/`, not committed into the source tree. It names
the exact commit and artifact hashes, is hashed into the final release bundle,
and is attached/signed with the draft release. This avoids changing the commit
after attesting to it.

- [ ] **Step 1: Implement and test the recorder/schema.** Record commit,
  artifact hashes, platform/kernel/filesystem, Python, terminal/tmux/Textual,
  provider/CLI/model versions, profile, command/scenario IDs, time bounds,
  pass/fail/not-run, known cost, the SHA-256 and schema version of Task 18C's
  playability report, its six dogfood journey outcomes, and unresolved issues.
  Reject unknown fields, missing evidence files, mismatched commit/artifact/
  report hashes, a failed interaction budget, a missing required journey, or a
  pass with an unresolved production finding. Never store credentials or raw
  private prompts.
- [ ] **Step 2: Run the Task 19A preflight.** Every named support-floor record,
  deterministic test, embedded compatibility/containment record, build,
  scanner, and documentation check must pass before changing the default or
  version.
- [ ] **Step 3: Prepare the candidate behavior and docs.** Change the CLI
  default from compatible to production; keep explicit compatible/unsafe escape
  hatches and Task 4 resume downgrade rules. Add `doctor --release` for the
  shipped floor and keep `doctor --model ALIAS --live` as the distinct local
  alias check. Finish changelog, security reporting, support matrix,
  install/upgrade/rollback, backup/restore, network boundaries, known
  limitations, recovery, and signature-verification docs. Set `0.1.0b1`.
- [ ] **Step 4: Commit the untagged candidate.** Run all ordinary pre-commit
  gates, then commit `release: prepare production beta candidate`. From this
  point through Task 19C, any source/doc change creates a new candidate commit
  and invalidates all qualification evidence; never amend evidence into the
  attested commit.
- [ ] **Step 5: Build the exact candidate.** From a fresh checkout of the
  candidate commit, run Task 19A twice, preserve matching artifacts/reports in
  `dist/`, and initialize the external qualification bundle with their hashes.
- [ ] **Step 6: Qualify every release-support target live.** Run minimal text,
  tool, denial, cancellation, context-overflow, model-switch, subagent, and
  mixed-model workflow turns using the default production profile. Subscription
  targets regenerate Tasks 9A-9B containment evidence on the release kernel.
  Missing credentials or paid runs record `not_run` and block release.
- [ ] **Step 7: Run soak and disposable faults.** Run the eight-hour
  mixed-provider soak with repeated switches, bounded nested ensemble/panel,
  errors, cancellation, telemetry, TUI/plain, resume/full verify/export, and
  record process/log/blob/subscriber/accounting/cleanup observations. Repeat
  real process kills, full-disk, expired-auth, MCP-loss, and abrupt-terminal
  cases only in a disposable VM/container or quota-limited filesystem; never
  fill the developer host.
- [ ] **Step 8: Complete playability qualification.** On the exact candidate,
  run Task 18C's fixed-sample measurement on the recorded WSL2 reference
  machine and require every Harness-overhead budget to pass. Execute all six
  dogfood journeys without consulting source, config files, logs, or a shell;
  include the continuous 90-minute real-work session. Record provider waiting
  separately, every pause longer than two seconds, action/approval counts,
  queue and recovery outcomes, and severity-classified friction. Any lost or
  duplicated input, missed journey, P0/P1 usability finding, unexplained busy
  state, or action-count violation blocks the candidate.
- [ ] **Step 9: Complete terminal qualification.** Execute Task 18A's checklist
  in Windows Terminal/WSL2, tmux, and supported xterm; validate Enter/Ctrl+J,
  paste, resize, permissions, signals, reconnect, queue/retry controls, visible
  activity phases, and TUI/plain output.
- [ ] **Step 10: Freeze external evidence.** Review every observation; any fail,
  not-run required target, or unresolved invariant blocks Task 19C. Run
  `release_gate.py --final` against the candidate checkout, artifacts, and
  qualification bundle. It verifies exact commit/artifact hashes and emits a
  final release-evidence JSON without modifying the checkout.

**Acceptance:** One immutable, untagged candidate commit and its exact artifacts
have reviewed live evidence for every required provider/environment, the
completed soak/fault record, all interaction budgets, and all six playable-work
journeys. Manual evidence is release-blocking but never represented as
deterministic CI or committed after the fact.

---

### Task 19C: Tag, sign, and promote the immutable candidate

**Purpose:** Release only the already-qualified commit and artifacts; perform no
source mutation in this task.

**Inputs:** Clean checkout at the Task 19B candidate commit, matching `dist/`
artifacts, final release-evidence JSON, and qualification bundle. There are no
source-file edits or commits in this task.

- [ ] **Step 1: Revalidate immutability.** Require a clean worktree, HEAD equal
  to every evidence `commit`, package version `0.1.0b1`, default profile
  production, and artifact hashes equal across build/evidence/qualification.
- [ ] **Step 2: Rerun final gates.** Run Task 19A from a fresh checkout and
  `release_gate.py --final` with the frozen external bundle. Any difference,
  expired allowlist, stale containment evidence, or failed provider target
  stops here.
- [ ] **Step 3: Create the candidate tag.** Create annotated tag `v0.1.0b1` on
  the exact qualified commit only after explicit human authorization. Refuse if
  that tag exists at another commit; never move or overwrite it. Push the tag,
  create a draft release targeting it, and upload the frozen qualification
  bundle plus final evidence before dispatching the release workflow.
- [ ] **Step 4: Verify the signed draft.** Manually dispatch the Task 19A
  workflow for that exact tag. It verifies the uploaded evidence, rebuilds,
  checks reproducible hashes, signs via Sigstore, and attaches wheel, sdist,
  SBOM, audits, checksums, signatures/bundles, support matrices, qualification
  bundle, and final evidence to the draft. Compare its hashes with the locally
  qualified artifacts.
- [ ] **Step 5: Promote deliberately.** A human reviews the exact commit,
  evidence, support matrix, security exceptions, signatures, and limitations
  before publishing the draft. No PyPI action occurs. If any invariant cannot
  be proven, leave the candidate untagged/unpublished and start a new candidate
  after the fix.

**Acceptance:** The signed draft and any promoted beta identify the exact
qualified immutable commit, provider/platform versions, evidence, artifacts,
and limitations; tagging cannot make an unqualified default appear production.

---

## Production-beta definition of done

Do not call the result production-grade unless every item below is true.

> **PLAYABILITY CHANGE:** The highlighted revision adds the first-use,
> activity-legibility, queue/recovery, latency-budget, dogfood, and usability-
> severity gates in this checklist.

- [ ] CI passes on Python 3.12 and 3.13 from a clean checkout.
- [ ] Ruff, full tests, wheel/sdist build, and clean-wheel smoke all pass.
- [ ] Every production provider has complete, hash-bound conformance fixtures.
- [ ] Every named release-support target has matching replay and live evidence.
- [ ] Every production subscription CLI/version passes live OS/native-tool
  containment; no replay fixture is counted as containment evidence.
- [ ] Provider transport networking and model-native network access are reported
  and enforced as separate capabilities.
- [ ] Every model and tool intent has exactly one terminal fact after resume.
- [ ] Provider changes preserve structured tool history and bounded blob content.
- [ ] Unsupported content fails visibly before dispatch; every bounded exclusion
  or authorized missing artifact is disclosed and recorded.
- [ ] Tool schemas validate centrally and risk/source metadata is present.
- [ ] Invalid third-party MCP schemas are quarantined without silently erasing
  valid tools from the same server.
- [ ] Session, lock, quarantine, temp, and blob files use private permissions.
- [ ] The production session base passes filesystem/flock/fsync/replace probes;
  a WSL Windows-mounted data root is refused.
- [ ] Crash injection proves atomic log/blob repair and publication.
- [ ] Persistent telemetry schema drift rebuilds or fails with remediation; it
  never queries stale columns.
- [ ] Secrets do not enter logs, providers, or child environments without
  explicit config, and each relevant call records redaction outcome.
- [ ] Outward per-turn MCP endpoints require an unguessable capability URL that
  is absent from argv and durable output.
- [ ] Production plugins are digest-trusted before import.
- [ ] Model/tool/subagent concurrency and cumulative budgets are bounded, and
  nested coordination cannot deadlock on its own capacity leases.
- [ ] Unknown/absent provider accounting is never shown or charged as zero; a
  hard budget requiring it refuses before dispatch.
- [ ] Subscriber overflow is recovered from the log or visibly degraded.
- [ ] Resume immediately reports refresh state and then reconstructs model,
  usage, tool, budget, and trust state without blocking the UI.
- [ ] TUI always shows the effective model alias and trust profile/tier before
  submission; provider route/version and both network boundaries are one
  action away.
- [ ] A logical turn cannot change models midway.
- [ ] Permission UI shows complete action, risk, origin, and persistence scope.
- [ ] Multiline prompts, drafts, session management, and diagnostics work by
  keyboard.
- [ ] **PLAYABILITY:** With clean Harness config and an already-authenticated
  supported CLI, first real-model use takes at most three deliberate choices
  and no manual config-file editing.
- [ ] **PLAYABILITY:** Active work always shows a concrete phase; after two
  seconds it shows and refreshes elapsed time, and no dogfood journey contains
  an unexplained busy interval longer than two seconds.
- [ ] **PLAYABILITY:** The composer remains usable during a turn; bounded
  queued prompts are never lost or duplicated, and failure/cancellation
  exposes safe edit, retry, and resume-queue actions.
- [ ] **PLAYABILITY:** Every Task 18C p95 Harness-overhead budget passes on the
  recorded release machine, all six dogfood journeys pass, and the exact
  candidate completes the 90-minute real-work session with zero P0/P1
  usability findings and no more than three owned, documented, non-repeating
  P2 findings.
- [ ] Fourteen reviewed compositor goldens and the full size/theme structural
  matrix pass; terminal keybinding smoke evidence is current.
- [ ] Plain mode operates the same dispatcher, permission, agent, and session
  semantics without Textual.
- [ ] `harness doctor` and `config show --effective` give actionable human and
  stable secret-free JSON output.
- [ ] Deterministic CI evidence and separately identified live/soak evidence
  include exact platform/provider versions and artifact hashes.
- [ ] Default execution profile is `production`; lower-trust operation is explicit.

## Deliberately deferred until after the first production beta

- Multi-user daemon/service mode and tenant isolation.
- Native Windows and macOS production claims.
- Running untrusted plugin Python in a sandbox.
- Automatic PyPI publication.
- General remote orchestration or distributed workflow scheduling.
- Session labels, pins, and archive metadata beyond safe export/trash.
- Provider/model features not represented by the owned content contract.
- A claim that Antigravity or any other CLI is production-contained before its
  native tools pass the adversarial containment gate.

These are deferred to keep the critical path short. They must remain visible as
limitations rather than being approximated behind a production label.
