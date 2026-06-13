# Harness — Contributing & Best Practices

This document is for changing the harness itself: how the codebase is built, the
invariants you must not break, recipes for common extensions, and the testing
discipline the project holds to. Read [architecture.md](architecture.md) first.

---

## Development setup

```bash
uv sync                       # install deps + dev tools
uv run pytest -q              # run the suite (≈70s)
uv run ruff check             # lint
uv run ruff format            # format
```

Conventions:

- **Python 3.12+**, `uv` for everything.
- **Ruff**, line length 100. No unused imports, no `noqa`.
- **pytest** with `asyncio_mode = "auto"` — async tests need no decorator.
- Run tests from the repo root.

The suite is the contract. A change isn't done until the full suite is green
and ruff is clean; report the actual `N passed, M skipped` line from a complete
run, never a guess or a partial.

---

## The laws

These invariants hold across the whole codebase. They were each established (and
defended in review) for a concrete reason; breaking one re-opens a class of bug.

1. **The event log is the unit of truth.** State is a fold of the log. Don't add
   state that lives only in memory and can't be reconstructed by replay.

2. **The event union is closed and additive.** New event type → add to the union.
   New field → must have a default. Never remove or re-type a field; old logs
   must always parse. Unknown event types decode as `UnknownEvent`
   (preserve-and-skip), never crash. `CustomEvent` is the *plugin* channel;
   native features get their own typed event.

3. **Errors are values at tool boundaries.** A tool returns its failure as a
   string result with `is_error=True`; it does not raise to the loop. The
   dispatcher catches what does raise. This is what lets the model self-correct.

4. **Tool error text teaches.** A failure message states *what* failed, *why*,
   and *what to do instead*. This is the single highest-leverage thing for model
   self-correction — don't return bare `[Errno 2]`.

5. **Hooks: dispatch fails closed, lifecycle fails open.** Enforcement must deny
   on failure; advisory hooks must skip on failure. A broken dispatch hook must
   fail to *load*, never register — otherwise fail-closed becomes a runtime DoS.

6. **Plugin/manifest errors surface at load time, never runtime.** Loading is
   all-or-nothing. A broken callable never registers.

7. **The permission engine is the innermost dispatch hook (priority 1000).**
   Guards that canonicalize args run earlier so the engine matches canonical
   values. Deny wins absolutely.

8. **The workspace walk never follows symlinks** — file *or* directory, in any
   read path. The harness reads untrusted trees (plugins, imports); a followed
   symlink leaks content from outside the root. Confine on the way in:
   `resolve_in_workspace` is the hard floor every file tool calls itself.

9. **Never store secrets as literals.** MCP env values are env-var *names*. The
   importer refuses literal-looking secrets and never echoes a value in any
   message, report, or emitted file.

10. **Self-enforce what you advertise.** The dispatcher does not validate args
    against a tool's JSON schema, so a tool must runtime-check every constraint
    its schema claims (a `minLength`, a `maxItems`, …).

11. **No silent caps.** When output is truncated or a list is bounded, the
    output says so. A "covered everything" that didn't is worse than a visible
    "showing 100 of 412."

12. **Determinism where it's claimed.** Generated artifacts (the import report,
    emitted plugin trees) are byte-identical across runs: sorted iteration, no
    timestamps. `Date.now()`/randomness have no place in reproducible output.

---

## Recipes

### Add a native tool

1. Implement a class satisfying the `Tool` protocol in `native_tools.py`:
   ```python
   class MyTool:
       def __init__(self, *, workspace_root: Path) -> None:
           self.spec = ToolSpec(name=ToolName("my_tool"), description=..., parameters={...})
       async def __call__(self, args: dict[str, Any]) -> str:
           ...
   ```
2. **Confine paths yourself** if it touches the filesystem: call
   `resolve_in_workspace(root, raw)` — the hook is a soft guard, not the floor.
3. **Wrap I/O in `try/except OSError` → `ToolError`** with teaching text. Offload
   blocking work via `asyncio.to_thread`, passing primitives (never the live
   `args` dict) across the thread boundary.
4. **Self-enforce the schema** (law 10). Reject malformed `args` with a teaching
   `ToolError`, don't let a raw `KeyError`/`ValueError` reach the model.
5. Register it in `register_native_tools` (before `apply_plugins`, so a plugin
   tool of the same name collides loudly), and decide its baseline permission in
   `baseline_ruleset()`.
6. TDD: write the failure-mode tests first (missing file, bad arg, oversize,
   timeout). The teaching path is part of the contract.

### Add an event type

1. Define a frozen `_Event` subclass in `events.py` with a unique `type` literal
   and **defaulted fields only**.
2. Add it to the `Event` union (and only there).
3. Handle it in `fold.py` if it changes model-ready state or a projection.
4. Test the three union probes: round-trip serialize/parse; a pre-existing log
   still parses; a mangled discriminator decodes as `UnknownEvent`.

Ask first: does this belong in the kernel's closed union at all, or is it a
plugin concern? If a plugin, use `CustomEvent` — don't grow the union for
non-kernel features.

### Add a hook

- **Dispatch hook** (changes what happens): register on the `HookBus` at a
  priority `< 1000`. Return `Allow`/`Block`/`Rewrite`/`Ask`. Remember it runs on
  *every* call — keep it cheap, and remember it fails closed.
- **Lifecycle hook** (advisory): register at a lifecycle point. Return
  `Inject`/`Annotate`/`Emit`. Catch your own exceptions.

### Add a CLI subcommand

Top-level dispatch is in `cli.py:main()` — a small `if argv[0] == "..."` ladder
that calls a `_<name>_subcommand(argv)` function with its own `argparse`. Mirror
the existing `mcp`/`import` arms.

---

## Testing discipline

- **TDD throughout.** Write the failing test, watch it fail for the right
  reason, then implement. The kernel is tested this way end to end.
- **`EchoProvider`/`FakeProvider`** give deterministic loop tests; script tool
  calls and assert on the resulting event sequence.
- **Test the law, not the instance.** When you fix a security or correctness
  bug, pin the *general* property. (A real example: the importer's symlink guard
  was added for two read paths and tested for exactly those two — leaving four
  other read paths unguarded and the hole invisible to a green suite. Pin "no
  read path follows a symlink," not "this path doesn't.")
- **Pin failure modes, not just happy paths.** Missing files, non-unique edits,
  timeouts, oversized output, malformed args, denied permissions — these are the
  behaviors that teach the model and protect the user.
- **Determinism tests** (byte-identical output) are cheap insurance for any
  generated artifact.
- **Verify counts independently.** If a subtask reports "N passed," re-run the
  suite yourself before trusting it. Pipes mask pytest exit codes; read the
  summary line from one complete run.

---

## How features get built here

The harness was built phase by phase with a consistent loop, and it's worth
following for any substantial change:

1. **Brainstorm → spec.** Agree on the design before code; write it down.
2. **Plan.** A full-code, test-first plan: every task has its failing tests and
   its implementation spelled out, each task independently committable with the
   suite green after it.
3. **Execute task by task** with **two-stage review** after each: a spec-
   compliance pass (does it match the plan?) then a code-quality pass (is it
   well-built? — probe the edges adversarially). Fix, re-review, commit.
4. **Mirror every fix back into the plan** so the plan stays a faithful record.
5. **Final holistic review** of the whole change with live adversarial probes
   before declaring it done.

The review machinery earns its keep: across the build it repeatedly caught
security holes (symlink content exfiltration, a one-keystroke allow-all grant),
correctness bugs (an O(n²) rewrite loop, a model-precedence line a contributor
called "redundant" that was load-bearing), and — notably — bugs in the *plan's
own code*, because a plan is a first draft of the implementation. Reviews verify
against evident intent, not text-matching.

---

## Repository layout

```
src/harness/        the package (flat modules — see architecture.md for the map)
tests/              one test module per subsystem; fixtures inline via tmp_path
plugins/memory/     the golden reference plugin
docs/               this documentation
pyproject.toml      deps, ruff, pytest config; the `harness` entry point
```

The authoritative design record lives in the project vault
(`vault/10-projects/harness/`): the design doc plus a dated completion note per
phase. When the code and a doc disagree, **the code is right** — fix the doc.
