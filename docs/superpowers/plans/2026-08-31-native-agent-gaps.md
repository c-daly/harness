# Native Agent Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three gaps that stop harness's native agent dispatch from being fully bounded and fully attributable.

**Architecture:** Three independent changes to the dispatch path. Task 1 adds an output bound to `AgentDef` enforced at the `SubagentRunner` boundary. Task 2 populates an event field that already exists but is never passed. Task 3 threads the proposing tool call's id to the spawn event via a `ContextVar` set by the dispatcher, which lets `tui_panel` delete a documented-wrong attribution heuristic.

**Tech Stack:** Python 3.13, pydantic v2, pytest (async tests, no `@pytest.mark.asyncio` — see `tests/conftest.py`), `uv run` for everything.

**Spec:** `docs/superpowers/specs/2026-08-31-harness-native-agents-design-study.md`

## Global Constraints

- **Do not modify the agent-swarm repository.** `git -C ~/.claude/plugins/agent-swarm status` must stay clean. This plan touches only `~/projects/harness`.
- **Event schema changes are additive-optional only.** New fields get a default so old logs stay readable, matching `ModelCallCompleted.stop_reason` (*"additive, default keeps old logs valid"*, `events.py:135`). Never rename or remove an event field.
- **`fold.py` never runs hooks or side effects.** Do not add behaviour there.
- Run tests with `uv run pytest`. The full suite is slow (~3 min); run the named test first, the file second, and the full suite only before the final commit of a task.

---

### Task 1: `max_output_chars` on `AgentDef`

An agent definition can bound how much its subagent returns to the parent. agent-swarm's agents used 2000–5000; harness currently returns `loop.run_turn()` output unbounded.

**Files:**
- Modify: `src/harness/frontmatter.py:64-78` (add field + validator to `AgentDef`)
- Modify: `src/harness/subagent.py:88` (truncate the returned result)
- Test: `tests/test_frontmatter.py`, `tests/test_subagent.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `AgentDef.max_output_chars: int | None = None`. Truncation marker is the exact string `"\n…[truncated]"` appended after slicing to the limit.

- [ ] **Step 1: Write the failing frontmatter test**

Add to `tests/test_frontmatter.py`:

```python
def test_agent_def_rejects_non_positive_max_output_chars(tmp_path):
    from harness.frontmatter import FrontmatterError, load_agent

    path = tmp_path / "bad.md"
    path.write_text("---\nname: bad\nmax_output_chars: 0\n---\nbody\n")
    try:
        load_agent(path)
    except FrontmatterError as exc:
        assert "max_output_chars" in str(exc)
    else:
        raise AssertionError("expected FrontmatterError")


def test_agent_def_accepts_max_output_chars(tmp_path):
    from harness.frontmatter import load_agent

    path = tmp_path / "ok.md"
    path.write_text("---\nname: ok\nmax_output_chars: 2000\n---\nbody\n")
    assert load_agent(path).max_output_chars == 2000
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_frontmatter.py -k max_output_chars -v`
Expected: FAIL — the second test errors because `AgentDef` has no attribute `max_output_chars`.

- [ ] **Step 3: Add the field and validator**

In `src/harness/frontmatter.py`, inside `class AgentDef(_Def)`, after the `experts` field:

```python
    # None = unbounded. A bound applies to what the child returns to the
    # parent, not to what the child's own model produced.
    max_output_chars: int | None = None
```

And add this validator alongside the existing ones:

```python
    @field_validator("max_output_chars")
    @classmethod
    def _max_output_chars_positive(cls, value):
        if value is not None and value < 1:
            raise ValueError("max_output_chars must be a positive integer")
        return value
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_frontmatter.py -k max_output_chars -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Write the failing truncation test**

Add to `tests/test_subagent.py`:

```python
async def test_agent_output_is_truncated_to_max_output_chars(tmp_path):
    parent = Session(tmp_path, SessionId("parent"))
    parent.start()
    runner = _runner(tmp_path, FakeProvider([text_turn("x" * 500)]))
    runner.agents = {
        "terse": AgentDef(name="terse", body="be terse", max_output_chars=100)
    }

    result = await runner.run(
        prompt="go", model=None, parent=parent, agent="terse"
    )

    assert result.startswith("x" * 100)
    assert result.endswith("…[truncated]")
    assert len(result) < 500


async def test_agent_output_under_the_bound_is_untouched(tmp_path):
    parent = Session(tmp_path, SessionId("parent"))
    parent.start()
    runner = _runner(tmp_path, FakeProvider([text_turn("short")]))
    runner.agents = {
        "terse": AgentDef(name="terse", body="be terse", max_output_chars=100)
    }

    result = await runner.run(
        prompt="go", model=None, parent=parent, agent="terse"
    )

    assert result == "short"
```

- [ ] **Step 6: Run to verify they fail**

Run: `uv run pytest tests/test_subagent.py -k max_output_chars -v`
Expected: FAIL — first test fails on `result.endswith("…[truncated]")`; output comes back at full 500 chars.

- [ ] **Step 7: Implement truncation**

In `src/harness/subagent.py`, the success path currently reads:

```python
            parent.append(SubagentFinished(child_session_id=child_id, status="ok"))
            return result
```

Replace with:

```python
            parent.append(SubagentFinished(child_session_id=child_id, status="ok"))
            return _bound(result, limit)
```

Add this module-level helper near the top of `subagent.py`, after the imports:

```python
_TRUNCATION_MARKER = "\n…[truncated]"


def _bound(text: str, limit: int | None) -> str:
    """Cap what a child returns to its parent. None = unbounded."""
    if limit is None or len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_MARKER
```

And capture the limit where the agent definition is resolved. In `run()`, alongside the existing `system_prompt`/`registry` defaults, add:

```python
        limit: int | None = None
```

then inside the `if agent is not None:` block, after `system_prompt = definition.body or system_prompt`:

```python
            limit = definition.max_output_chars
```

Note: the `definition.strategy is not None` branch returns before this point, so a coordination def's fan-out is deliberately unbounded — the bound belongs to leaf agents, and each expert applies its own.

- [ ] **Step 8: Run to verify they pass**

Run: `uv run pytest tests/test_subagent.py -k max_output_chars -v`
Expected: PASS (2 passed)

- [ ] **Step 9: Run the touched files**

Run: `uv run pytest tests/test_subagent.py tests/test_frontmatter.py -q`
Expected: all pass, no regressions.

- [ ] **Step 10: Commit**

```bash
git add src/harness/frontmatter.py src/harness/subagent.py tests/test_subagent.py tests/test_frontmatter.py
git commit -m "feat: bound subagent output with AgentDef.max_output_chars

An agent definition can cap what its child returns to the parent.
Unbounded by default; a coordination def's fan-out is exempt because each
expert applies its own bound."
```

---

### Task 2: Populate `SubagentSpawned.agent`

The field exists on the event (`events.py:172`) and the runner knows the agent, but `subagent.py:63` never passes it. Every dispatch records a model and not which agent ran.

**Files:**
- Modify: `src/harness/subagent.py:63`
- Test: `tests/test_subagent.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `SubagentSpawned.agent` is populated with the agent name for agent dispatches, and stays `None` for bare `dispatch_agent` calls with no `agent=`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_subagent.py`:

```python
async def test_spawn_event_records_which_agent_ran(tmp_path):
    parent = Session(tmp_path, SessionId("parent"))
    parent.start()
    runner = _runner(tmp_path, FakeProvider([text_turn("done")]))
    runner.agents = {"explorer": AgentDef(name="explorer", body="explore")}

    await runner.run(prompt="go", model=None, parent=parent, agent="explorer")

    events = [e.event for e in read_session(tmp_path, SessionId("parent"))]
    spawned = [e for e in events if isinstance(e, SubagentSpawned)]
    assert len(spawned) == 1
    assert spawned[0].agent == "explorer"


async def test_spawn_event_agent_is_none_without_an_agent(tmp_path):
    parent = Session(tmp_path, SessionId("parent"))
    parent.start()
    runner = _runner(tmp_path, FakeProvider([text_turn("done")]))

    await runner.run(prompt="go", model=None, parent=parent)

    events = [e.event for e in read_session(tmp_path, SessionId("parent"))]
    spawned = [e for e in events if isinstance(e, SubagentSpawned)]
    assert spawned[0].agent is None
```

- [ ] **Step 2: Run to verify the first fails**

Run: `uv run pytest tests/test_subagent.py -k spawn_event -v`
Expected: `test_spawn_event_records_which_agent_ran` FAILS with `assert None == 'explorer'`. The second test passes already.

- [ ] **Step 3: Pass the agent through**

In `src/harness/subagent.py`, line 63 currently:

```python
        spawn_env = parent.append(SubagentSpawned(child_session_id=child_id, model=chosen))
```

Replace with:

```python
        spawn_env = parent.append(
            SubagentSpawned(
                child_session_id=child_id,
                agent=AgentId(agent) if agent is not None else None,
                model=chosen,
            )
        )
```

Add `AgentId` to the existing import from `harness.types` at the top of the file.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_subagent.py -k spawn_event -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/harness/subagent.py tests/test_subagent.py
git commit -m "fix: record which agent ran in SubagentSpawned

The event has carried an 'agent' field all along and the runner has always
known the value; it was never passed. Per-agent attribution was impossible
from the log alone."
```

---

### Task 3: Correlate spawns with their proposing call

`SubagentSpawned` carries no link to the tool call that caused it. `tui_panel` compensates with a stack-of-open-coordination-calls heuristic that its own docstring admits is wrong when two coordination calls run concurrently in one turn.

Four tools spawn subagents — `dispatch_agent`, `ensemble`, `consult_panel`, `escalate` — and the `Tool` protocol is `__call__(self, args)` with no call id. Rather than change that protocol for every tool, the dispatcher publishes the current call id in a `ContextVar` immediately before invoking the tool. Because `_run_one` coroutines become Tasks under `asyncio.gather`, each concurrent call gets its own context copy, so concurrent coordination calls stay isolated. Nested spawns inherit the coordination call's id, which is exactly the attribution the heuristic was guessing at.

**Files:**
- Create: `src/harness/callctx.py`
- Modify: `src/harness/events.py:167-175` (add `call_id` to `SubagentSpawned`)
- Modify: `src/harness/dispatcher.py:147` (set the ContextVar around the tool call)
- Modify: `src/harness/subagent.py:63` (read it into the event)
- Modify: `src/harness/tui_panel.py:101-135` (use `call_id`; delete `strategy_stack`)
- Test: `tests/test_subagent.py`, `tests/test_tui_panel.py`

**Interfaces:**
- Consumes: `SubagentSpawned(child_session_id=..., agent=..., model=...)` from Task 2.
- Produces: `harness.callctx.current_call_id() -> CallId | None` and `harness.callctx.set_current_call_id(call_id) -> Token`; `SubagentSpawned.call_id: CallId | None = None`.

- [ ] **Step 1: Create the context module**

Create `src/harness/callctx.py`:

```python
"""The tool call currently being executed, for tools that spawn.

The Tool protocol is __call__(args) -- no call id -- because almost no tool
needs one. The four that spawn subagents do: a spawn event must name the call
that caused it, or concurrent coordination calls become unattributable. The
dispatcher publishes the id here for the duration of the call.

asyncio semantics: each tool call in a turn runs in its own Task (loop.py's
gather over _run_one), and a Task gets a copy of the context at creation, so
concurrent calls never observe each other's id. Nested spawns inside a
coordination tool correctly inherit that tool's id.
"""

from contextvars import ContextVar, Token

from harness.types import CallId

_current: ContextVar[CallId | None] = ContextVar("harness_current_call_id", default=None)


def current_call_id() -> CallId | None:
    """The call being executed in this context, or None outside a tool call."""
    return _current.get()


def set_current_call_id(call_id: CallId | None) -> Token:
    """Publish the current call id. Reset with the returned token in a finally."""
    return _current.set(call_id)


def reset_current_call_id(token: Token) -> None:
    _current.reset(token)
```

- [ ] **Step 2: Write the failing correlation test**

Add to `tests/test_subagent.py`:

```python
async def test_spawn_event_records_its_proposing_call_id(tmp_path):
    from harness.callctx import reset_current_call_id, set_current_call_id
    from harness.types import CallId

    parent = Session(tmp_path, SessionId("parent"))
    parent.start()
    runner = _runner(tmp_path, FakeProvider([text_turn("done")]))

    token = set_current_call_id(CallId("call-abc"))
    try:
        await runner.run(prompt="go", model=None, parent=parent)
    finally:
        reset_current_call_id(token)

    events = [e.event for e in read_session(tmp_path, SessionId("parent"))]
    spawned = [e for e in events if isinstance(e, SubagentSpawned)]
    assert spawned[0].call_id == "call-abc"


async def test_concurrent_spawns_keep_separate_call_ids(tmp_path):
    """The case tui_panel's stack heuristic cannot get right."""
    from harness.callctx import reset_current_call_id, set_current_call_id
    from harness.types import CallId

    parent = Session(tmp_path, SessionId("parent"))
    parent.start()

    async def spawn_under(call_id: str):
        runner = _runner(tmp_path, FakeProvider([text_turn("done")]))
        token = set_current_call_id(CallId(call_id))
        try:
            await runner.run(prompt="go", model=None, parent=parent)
        finally:
            reset_current_call_id(token)

    await asyncio.gather(spawn_under("call-a"), spawn_under("call-b"))

    events = [e.event for e in read_session(tmp_path, SessionId("parent"))]
    ids = {e.call_id for e in events if isinstance(e, SubagentSpawned)}
    assert ids == {"call-a", "call-b"}
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/test_subagent.py -k call_id -v`
Expected: FAIL — `SubagentSpawned` has no attribute `call_id`.

- [ ] **Step 4: Add the event field**

In `src/harness/events.py`, inside `class SubagentSpawned(_Event)`, after `child_session_id`:

```python
    # the dispatch_agent / ensemble / consult_panel / escalate call that caused
    # this spawn. Additive-optional: absent in logs written before this field.
    call_id: CallId | None = None
```

`CallId` is already imported in `events.py`.

- [ ] **Step 5: Read the context in the runner**

In `src/harness/subagent.py`, change the spawn append from Task 2 to:

```python
        spawn_env = parent.append(
            SubagentSpawned(
                child_session_id=child_id,
                call_id=current_call_id(),
                agent=AgentId(agent) if agent is not None else None,
                model=chosen,
            )
        )
```

Add at the top of the file:

```python
from harness.callctx import current_call_id
```

- [ ] **Step 6: Run to verify they pass**

Run: `uv run pytest tests/test_subagent.py -k call_id -v`
Expected: PASS (2 passed)

- [ ] **Step 7: Set the context from the dispatcher**

In `src/harness/dispatcher.py`, this line:

```python
            raw = await self.registry.get(effective.tool)(dict(effective.args))
```

becomes:

```python
            token = set_current_call_id(call.call_id)
            try:
                raw = await self.registry.get(effective.tool)(dict(effective.args))
            finally:
                reset_current_call_id(token)
```

Add at the top of `dispatcher.py`:

```python
from harness.callctx import reset_current_call_id, set_current_call_id
```

- [ ] **Step 8: Verify the dispatcher wiring end to end**

Run: `uv run pytest tests/test_dispatcher.py tests/test_subagent.py -q`
Expected: all pass. This proves a real `dispatch_agent` call now stamps its id without any test-set context.

- [ ] **Step 9: Commit the correlation half**

```bash
git add src/harness/callctx.py src/harness/events.py src/harness/dispatcher.py src/harness/subagent.py tests/test_subagent.py
git commit -m "feat: correlate SubagentSpawned with its proposing tool call

The spawn event is is_intent precisely because it is the causal link, yet
carried no link to its cause. The dispatcher now publishes the executing
call id in a ContextVar; the four spawning tools pick it up without a
Tool-protocol change, and nested spawns inherit the coordination call's id."
```

- [ ] **Step 10: Write the failing panel test**

Add to `tests/test_tui_panel.py`. The file already defines `env(seq, event)` and
imports everything needed — do not add a helper module.

```python
def test_fold_agents_concurrent_coordination_calls_attribute_experts_correctly():
    """Two ensembles open at once. The stack heuristic gave both experts to the
    top of the stack; the recorded call_id separates them."""
    ens1, ens2 = new_call_id(), new_call_id()
    c1, c2 = new_session_id(), new_session_id()
    events = [
        env(1, ToolCallProposed(call_id=ens1, tool=ToolName("ensemble"), args={})),
        env(2, ToolCallProposed(call_id=ens2, tool=ToolName("ensemble"), args={})),
        # ens2's expert lands first: under the stack heuristic BOTH spawns are
        # attributed to strategy_stack[-1] (ens2), so c1 is misfiled.
        env(3, SubagentSpawned(child_session_id=c2, call_id=ens2, model=ModelId("m"))),
        env(4, SubagentSpawned(child_session_id=c1, call_id=ens1, model=ModelId("m"))),
    ]

    rows = fold_agents(events)

    by_child = {r.call_id: r.strategy for r in rows}
    assert by_child[str(c1)] == str(ens1)
    assert by_child[str(c2)] == str(ens2)
```

- [ ] **Step 11: Run to verify it fails**

Run: `uv run pytest tests/test_tui_panel.py -k concurrent_coordination -v`
Expected: FAIL with `assert <ens2> == <ens1>` — `c1` is attributed to `ens2`,
the top of the stack. If instead you get a `KeyError` or import error, stop and
fix the test before touching `tui_panel.py`.

- [ ] **Step 12: Replace the stack with the recorded call id**

In `src/harness/tui_panel.py`, `fold_agents`:

1. Replace the stack declaration:

```python
    strategy_stack: list[str] = []  # open ensemble/consult_panel/escalate call_ids
```

with a set of known coordination calls:

```python
    strategy_calls: set[str] = set()  # ensemble/consult_panel/escalate call_ids seen
```

2. In the `ToolCallProposed` branch, replace `strategy_stack.append(call_id)` with:

```python
                strategy_calls.add(call_id)
```

3. In the `ToolCallCompleted` branch, delete this now-dead arm entirely:

```python
            elif strategy_stack and strategy_stack[-1] == call_id:
                strategy_stack.pop()
```

A coordination call id stays in `strategy_calls` after completion — spawns are
logged before their call completes, and keeping it costs nothing.

4. Replace the whole `SubagentSpawned` branch:

```python
        elif isinstance(ev, SubagentSpawned):
            if strategy_stack:
                group = strategy_stack[-1]
                child = str(ev.child_session_id)
                model = str(ev.model) if ev.model else None
                upsert(child, label=model or "(agent)", model=model, strategy=group)
```

with:

```python
        elif isinstance(ev, SubagentSpawned):
            group = str(ev.call_id) if ev.call_id is not None else None
            # a spawn from dispatch_agent already has its own row keyed by the
            # dispatch call; only coordination experts get a child-keyed row
            if group is not None and group in strategy_calls:
                child = str(ev.child_session_id)
                model = str(ev.model) if ev.model else None
                label = str(ev.agent) if ev.agent else (model or "(agent)")
                upsert(child, label=label, model=model, strategy=group)
```

Note `_STRATEGY_TOOLS` is still required — it is what populates `strategy_calls`,
and the `group in strategy_calls` check is what keeps
`test_fold_agents_unrelated_subagent_spawned_outside_any_strategy_is_ignored`
passing. Do not delete the constant.

5. `SubagentSpawned.agent` (Task 2) now supplies the expert label when present,
   which is why `label` is computed above rather than defaulting to the model.

6. In the `ToolCallProposed` dispatch branch, `agent = ev.args.get("agent")` stays
   as-is: that row is created from the tool call, before any spawn event exists,
   so the args are still the only source at that point.

7. Replace the module docstring paragraph beginning *"Coordination-call
   attribution (the "strategy" grouping key on AgentRow) is a best-effort
   sequential assumption"* and running through *"...no way to recover that in
   general."* with:

```
Coordination-call attribution (the "strategy" grouping key on AgentRow) is
exact: SubagentSpawned carries the call_id of the tool call that caused it,
so coordination calls running concurrently in one turn stay correctly
separated.
```

- [ ] **Step 13: Run to verify it passes**

Run: `uv run pytest tests/test_tui_panel.py -v`
Expected: all pass, including the new test and every pre-existing panel test.

- [ ] **Step 14: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass. This is the first full run; it takes about 3 minutes.

- [ ] **Step 15: Confirm agent-swarm is untouched**

Run: `git -C ~/.claude/plugins/agent-swarm status --porcelain`
Expected: empty output. Any output is a failure of this plan's global constraint.

- [ ] **Step 16: Commit**

```bash
git add src/harness/tui_panel.py tests/test_tui_panel.py
git commit -m "fix: attribute fan-out experts by recorded call_id

Replaces the stack-of-open-coordination-calls heuristic, which its own
docstring admitted was wrong for concurrent coordination calls -- the exact
case mixture fan-out produces. Attribution is now read from the spawn event."
```
