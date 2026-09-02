# Experiment Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `plugins/experiment/`, a harness-native plugin that runs the single-experiment loop (read, plan, work, eval, journal, decide) with an engine-run eval, a phase-gated dispatch hook, and the frozen (reader, writer) run store, lifted from the copy parked in the agent-swarm repository.

**Architecture:** Two processes share one truth on disk. A FastMCP server (`server.py`) owns the engine, the store and eval execution and reads no configuration from its environment; an in-process dispatch hook (`hooks.py`, priority 990) injects `store_root`, `session_id` and `state_dir` into every experiment tool call by `Rewrite`, enforces the phase policy (`Ask` for discipline, `Block` for integrity), and is dormant when no run is open. The run directory holds the frozen `run.json` and `journal/` plus an additive `state.json` and an append-only `evals/` history; per-session pointers live outside the store.

**Tech Stack:** Python 3.12+, harness kernel (`harness.hooks`, `harness.plugins`, `harness.mcp_host`), `mcp` FastMCP, `pyyaml`, pytest with `asyncio_mode = "auto"`, ruff (line length 100).

**Spec:** `docs/superpowers/specs/2026-09-01-experiment-plugin-design.md` (read it first; every task below cites its sections).

## Global Constraints

- Python 3.12+; run everything with `uv run ...` from the repo root; ruff line length 100; no `noqa`, no unused imports.
- The agent-swarm repository is never modified. Port by copying; do not `import` from it and do not add it as a dependency.
- Plugin modules load siblings with `importlib.util.spec_from_file_location` under a unique synthetic name (the memory plugin pattern). `hooks.py` imports only the stdlib and `harness.*`.
- On-disk store layout is byte-compatible with the parked store: `<root>/<experiment>/runs/run-NNN/run.json` (seven keys, `json.dumps(indent=2)`), `journal/NNN_<slug>.md` with the frontmatter keys `run_id, number, created_at, title, hypothesis, changes, result, diagnosis, next_direction` in that order, then a blank line, then the body template.
- `state.json` starts with `last_eval_metrics: null` and `last_eval_passed: null`; `{}` only after an eval that parsed nothing.
- Every non-deny branch of `phase_gate` for an `mcp__experiment__*` call returns `Rewrite`, never `Allow`.
- The hook injects three keys, overwriting unconditionally: `store_root`, `session_id`, `state_dir` (the pointer directory, `~/.local/state/harness/experiment/active` unless `HARNESS_EXPERIMENT_STATE_DIR` is set). The spec's section 4 names the first two; `state_dir` is the pointer location it describes and Task 10 amends the spec to say so.
- Server refusals raise `ValueError` with teaching text; FastMCP turns any exception into an `isError` result and the harness dispatcher records `is_error=True` with the text `tool error: Error executing tool <name>: <message>`.
- Tool results are JSON strings (`json.dumps`), like the parked server.
- Full suite green (`uv run pytest -q`) and `uv run ruff check` clean before the PR. Report the real `N passed, M skipped` line.
- Commit after every task on branch `feat/experiment-plugin`. Never commit to `main`. Chris merges.
- Commit messages never name a person.

## File Structure

Created:

- `plugins/experiment/plugin.toml`: manifest (hooks module, dispatch hook `phase_gate` at 990, lifecycle hook `capture_session`, MCP server `experiment`).
- `plugins/experiment/store.py`: `Observation`, `Run`, `ExperimentWriter`, `ExperimentReader`, `LocalFsExperimentStore`, `validate_experiment_name`, `render_observation`, `parse_observation`, `atomic_write_text`, `now_iso`.
- `plugins/experiment/evals.py`: `Goal`, `load_goal`, `Constraints`, `load_constraints`, `normalize_criteria`, `validate_environment`, `check_criteria`, `criteria_pass`, `parse_metrics`, `parse_pytest_summary`, `resolve_eval_spec`, `select_interpreter`, `build_command`, `run_eval` (async), `derive_metrics`, `protected_roots`, `digest_protected`, `bounded_tail`.
- `plugins/experiment/engine.py`: pointers, `state.json`, `start`, `resume`, `status`, `advance`, `run_eval` (async), `end`, `assert_loop_phase`, `EngineError`.
- `plugins/experiment/policy.py`: `PHASES`, `TRANSITIONS`, `decide(phase, tool)`, `is_protected(path, roots)`, validated at import.
- `plugins/experiment/hooks.py`: `phase_gate`, `capture_session`, `resolve_store_root`, `state_dir`.
- `plugins/experiment/server.py`: the twelve tools.
- `plugins/experiment/skills/experiment.md`, `commands/experiment.md`, `agents/experiment-worker.md`, `README.md`.
- `tests/test_experiment_store.py`, `tests/test_experiment_evals.py`, `tests/test_experiment_engine.py`, `tests/test_experiment_policy_hooks.py`, `tests/test_experiment_server.py`, `tests/test_experiment_plugin.py`, `tests/test_experiment_subprocess.py`.
- `tests/fixtures/experiment_parked_run/`: a run directory written by the parked code (byte-compat fixture).

Modified:

- `docs/user-guide.md` (plugins section), `docs/plugin-authoring.md` (MCP servers section), `README.md` (plugins bullet), the spec (section 4 amendment).
- Vault: new `10-projects/experiment/` entity; `10-projects/harness/decisions/2026-09-01-open-questions-from-the-experiment-plugin.md`; one-line pointers in the harness and agent-swarm narratives.

Test helper shared by several test files (define it in each file that needs it; tests must not import each other):

```python
import importlib.util
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent / "plugins" / "experiment"


def load_plugin_module(name: str):
    """Import plugins/experiment/<name>.py by path under a test-private module name."""
    spec = importlib.util.spec_from_file_location(
        f"experiment_plugin_test_{name}", PLUGIN_ROOT / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
```

---

### Task 1: Scaffold the plugin so it loads next to memory

**Files:**
- Create: `plugins/experiment/plugin.toml`, `plugins/experiment/hooks.py` (stub), `plugins/experiment/policy.py` (stub), `plugins/experiment/server.py` (stub), `plugins/experiment/skills/experiment.md`, `plugins/experiment/commands/experiment.md`, `plugins/experiment/agents/experiment-worker.md`
- Test: `tests/test_experiment_plugin.py`

**Interfaces:**
- Produces: the manifest names `hooks.py` callables `phase_gate` (dispatch, priority 990) and `capture_session` (lifecycle, `session_start`); server name `experiment` so tools are `mcp__experiment__<tool>`; agent `experiment-worker`; skill and command both named `experiment`.

- [ ] **Step 1: Write the failing load test**

```python
# tests/test_experiment_plugin.py
"""The experiment plugin loads next to the golden memory plugin."""

from pathlib import Path

PLUGINS_DIR = Path(__file__).parent.parent / "plugins"


def test_experiment_plugin_loads_with_memory():
    from harness.plugins import load_plugins

    loaded = load_plugins([PLUGINS_DIR])
    names = {p.name for p in loaded.plugins}
    assert {"memory", "experiment"} <= names
    exp = next(p for p in loaded.plugins if p.name == "experiment")
    assert [h.name for h in exp.dispatch_hooks] == ["phase_gate"]
    assert exp.dispatch_hooks[0].priority == 990
    assert [h.name for h in exp.lifecycle_hooks] == ["capture_session"]
    assert exp.lifecycle_hooks[0].point.value == "session_start"
    assert [s.name for s in exp.mcp_servers] == ["experiment"]
    assert exp.mcp_servers[0].tool_timeout_s == 3600
    assert {s.name for s in exp.skills} == {"experiment"}
    assert {c.name for c in exp.commands} == {"experiment"}
    assert {a.name for a in exp.agents} == {"experiment-worker"}
    worker = exp.agents[0]
    assert "mcp__experiment__experiment_advance" not in worker.tools
    assert "mcp__experiment__experiment_status" in worker.tools
    assert loaded.warnings == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_experiment_plugin.py -v`
Expected: FAIL (`"experiment"` not in `names`).

- [ ] **Step 3: Write the manifest**

```toml
# plugins/experiment/plugin.toml
[plugin]
name = "experiment"
version = "0.1.0"
description = "Autonomous experiment loop with an engine-run eval gate and a run store"

[hooks]
module = "hooks.py"

[[hooks.dispatch]]
name = "phase_gate"
function = "phase_gate"
priority = 990

[[hooks.lifecycle]]
name = "capture_session"
function = "capture_session"
point = "session_start"

[mcp.servers.experiment]
command = "python3"
args = ["${PLUGIN_ROOT}/server.py"]
tool_timeout_s = 3600
```

- [ ] **Step 4: Write the hook and server stubs (replaced in Tasks 5 to 7)**

```python
# plugins/experiment/hooks.py
"""Experiment plugin hooks. Replaced in Task 6."""

from harness.hooks import Allow


def phase_gate(action):
    return Allow()


def capture_session(ctx):
    return []
```

```python
# plugins/experiment/policy.py
"""Phase policy. Replaced in Task 5."""

PHASES = ("read", "plan", "work", "eval", "journal", "decide", "done")
```

```python
# plugins/experiment/server.py
"""Experiment MCP server. Replaced in Task 7."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("experiment", instructions="Experiment loop; start with experiment_start.")

if __name__ == "__main__":
    mcp.run("stdio")
```

- [ ] **Step 5: Write the skill, command and agent definitions**

```markdown
<!-- plugins/experiment/skills/experiment.md -->
---
name: experiment
description: Run one experiment through read, plan, work, eval, journal and decide with engine-run evals
---

# Experiment

You need the `mcp__experiment__*` tools. If `experiment_start` is not in your tool
list, stop and say the experiment MCP server did not start.

One run works one experiment directory (a `goal.yaml`, an `eval/`, optionally
`constraints.yaml`, `fixtures/`, `workspace/`). Phases: read -> plan -> work ->
eval -> journal -> decide -> (plan | done). Every phase ends with one tool call,
and **a phase transition is the only tool call in its message, and it ends
your reply**: call `experiment_advance`, then call `experiment_status`, then
stop and wait to be prompted again. Lost the run id? `experiment_status()` with
no arguments returns it.

## read
`experiment_start(experiment_dir, max_iterations)` returns the objective, the
success criteria, constraints (`do_not_do`, `known_findings`, `escalate_if`), and
a bounded summary of prior runs. Read the experiment directory. If a legacy
`journal/` exists under it, read those entries too. Then
`experiment_advance(run_id, "plan")`.

## plan
Review constraints and every prior failure. Never repeat a failed approach
without a new hypothesis. Then `experiment_advance(run_id, "work", hypothesis=...)`.

## work
Standalone experiments work in `<experiment_dir>/workspace/`. Integration
experiments (`target:` in goal.yaml) work in the target checkout, which must be
inside the directory harness was launched from; create a branch before editing
and note `git rev-parse HEAD` for the observation. `goal.yaml`,
`constraints.yaml`, `eval/` and `fixtures/` are immutable during a run. If you
dispatch a subagent, dispatch `experiment-worker`; it cannot move the run.
Then `experiment_advance(run_id, "eval")`.

## eval
`experiment_run_eval(run_id)`. The engine runs the eval you never choose. If the
result shows a crash (`return_code != 0` with no metrics, an import error, or
`timed_out`), fix it: `experiment_advance(run_id, "work")`. If the eval ran but
criteria are unmet, `experiment_advance(run_id, "journal")`. The engine refuses
eval -> journal until an eval has run in this visit.

## journal
`experiment_record_observation(run_id, observation)` with `title`,
`hypothesis`, `changes`, `result`, `diagnosis`, `next_direction`. Then
`experiment_advance(run_id, "decide")`.

## decide
If an escalation condition from `constraints.yaml` holds,
`experiment_end(run_id, "escalation", note)`. Otherwise
`experiment_advance(run_id, "done")` (the engine recomputes the criteria from the
recorded eval and refuses if unmet) or `experiment_advance(run_id, "plan")` for
another iteration. The iteration cap ends the run with `max_iterations`.
```

```markdown
<!-- plugins/experiment/commands/experiment.md -->
---
name: experiment
description: Start an experiment run in a directory that holds goal.yaml
---

Load the `experiment` skill with invoke_skill and follow it exactly. The
arguments are: $ARGUMENTS

The first token is the experiment directory. If `--max-iterations N` is present,
pass N to `experiment_start`; otherwise use the default. Begin with the read
phase now.
```

```markdown
<!-- plugins/experiment/agents/experiment-worker.md -->
---
name: experiment-worker
description: Does bounded work inside an open experiment run; cannot start, advance, resume or end the run
tools: [read_file, write_file, edit_file, glob, grep, bash, todo, invoke_skill, mcp__experiment__experiment_status, mcp__experiment__experiment_observations, mcp__experiment__experiment_get_run, mcp__experiment__experiment_list_runs]
max_output_chars: 6000
---

You are a worker inside an experiment run. The parent owns the phases. Do the
task you were given inside the workspace or target checkout, never touch
`goal.yaml`, `constraints.yaml`, `eval/` or `fixtures/`, and report what you
changed and what you observed.
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_experiment_plugin.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add plugins/experiment tests/test_experiment_plugin.py
git commit -m "feat(experiment): scaffold the plugin manifest, skill, command and worker agent"
```

---

### Task 2: Port the run store

**Files:**
- Create: `plugins/experiment/store.py`, `tests/fixtures/experiment_parked_run/exp-a/runs/run-001/run.json`, `tests/fixtures/experiment_parked_run/exp-a/runs/run-001/journal/001_first.md`
- Test: `tests/test_experiment_store.py`

**Interfaces:**
- Produces: `Observation(title, hypothesis="", changes="", result="", diagnosis="", next_direction="", run_id=None, number=None)`; `Run(run_id, experiment, goal="", started_at="", ended_at=None, outcome=None, metrics={})`; `LocalFsExperimentStore(root)` with `start_run(experiment, goal) -> str`, `record_observation(run_id, observation) -> str`, `end_run(run_id, outcome, metrics) -> None`, `list_runs(experiment) -> list[Run]`, `get_run(run_id) -> Run`, `observations(run_id) -> list[Observation]`, `run_dir(run_id) -> Path`, `runs_dir(experiment) -> Path`; module functions `validate_experiment_name(name) -> str`, `atomic_write_text(path, text)`, `now_iso() -> str`, `OBS_FIELDS`.

- [ ] **Step 1: Create the byte-compat fixture (exactly what the parked code wrote)**

`tests/fixtures/experiment_parked_run/exp-a/runs/run-001/run.json` (no trailing newline):

```json
{
  "run_id": "exp-a/run-001",
  "experiment": "exp-a",
  "goal": "objective X",
  "started_at": "2026-08-13T10:00:00",
  "ended_at": "2026-08-13T11:00:00",
  "outcome": "success",
  "metrics": {
    "accuracy": 0.95
  }
}
```

`tests/fixtures/experiment_parked_run/exp-a/runs/run-001/journal/001_first.md`:

```markdown
---
run_id: exp-a/run-001
number: 1
created_at: '2026-08-13T10:30:00'
title: first
hypothesis: h1
changes: c1
result: r1
diagnosis: d1
next_direction: n1
---

# first

**Attempt:** 1
**Hypothesis:** h1

## Changes
c1

## Result
r1

## Diagnosis
d1

## Next Direction
n1
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_experiment_store.py
"""The experiment run store: frozen contract, byte-compatible layout, tenancy guard."""

import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent / "plugins" / "experiment"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "experiment_parked_run"


def load_plugin_module(name: str):
    spec = importlib.util.spec_from_file_location(
        f"experiment_plugin_test_{name}", PLUGIN_ROOT / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def store_mod():
    return load_plugin_module("store")


@pytest.fixture
def store(tmp_path, store_mod):
    return store_mod.LocalFsExperimentStore(tmp_path / "experiments")


def test_backend_implements_both_reader_and_writer(store, store_mod):
    assert isinstance(store, store_mod.ExperimentReader)
    assert isinstance(store, store_mod.ExperimentWriter)


def test_start_run_returns_locatable_run(store, store_mod):
    run_id = store.start_run("exp-a", "objective X")
    run = store.get_run(run_id)
    assert isinstance(run, store_mod.Run)
    assert run.run_id == "exp-a/run-001"
    assert run.experiment == "exp-a"
    assert run.goal == "objective X"
    assert run.started_at
    assert run.ended_at is None


def test_record_observation_numbers_sequentially(store, store_mod):
    run_id = store.start_run("exp-a", "g")
    a = store.record_observation(run_id, store_mod.Observation(title="first", hypothesis="h1"))
    b = store.record_observation(run_id, store_mod.Observation(title="second", changes="c2"))
    assert (a, b) == (f"{run_id}#001", f"{run_id}#002")
    obs = store.observations(run_id)
    assert [o.number for o in obs] == [1, 2]
    assert obs[0].title == "first" and obs[0].hypothesis == "h1" and obs[0].run_id == run_id
    assert obs[1].changes == "c2"


def test_observations_empty_for_new_run(store):
    assert store.observations(store.start_run("exp-a", "g")) == []


def test_end_run_records_outcome_and_metrics(store):
    run_id = store.start_run("exp-a", "g")
    store.end_run(run_id, outcome="success", metrics={"accuracy": 0.95})
    run = store.get_run(run_id)
    assert run.outcome == "success" and run.metrics["accuracy"] == 0.95
    assert run.ended_at is not None


def test_list_runs_scoped_to_experiment(store):
    r1 = store.start_run("exp-a", "g")
    r2 = store.start_run("exp-a", "g2")
    store.start_run("exp-b", "g3")
    assert {r.run_id for r in store.list_runs("exp-a")} == {r1, r2}
    assert store.list_runs("nope") == []


def test_multiline_observation_prose_round_trips(store, store_mod):
    run_id = store.start_run("exp-a", "g")
    prose = "line one\nline two\n\nparagraph two"
    store.record_observation(run_id, store_mod.Observation(title="t", diagnosis=prose))
    (obs,) = store.observations(run_id)
    assert obs.diagnosis == prose


def test_persists_across_instances(tmp_path, store_mod):
    root = tmp_path / "experiments"
    run_id = store_mod.LocalFsExperimentStore(root).start_run("exp-a", "g")
    store_mod.LocalFsExperimentStore(root).record_observation(
        run_id, store_mod.Observation(title="t")
    )
    assert [o.title for o in store_mod.LocalFsExperimentStore(root).observations(run_id)] == ["t"]


def test_get_run_missing_raises(store):
    with pytest.raises(KeyError):
        store.get_run("exp-a/run-999")


def test_record_observation_on_missing_run_raises(store, store_mod):
    with pytest.raises(KeyError):
        store.record_observation("exp-a/run-999", store_mod.Observation(title="t"))


def test_malformed_run_id_raises(store):
    with pytest.raises(KeyError):
        store.get_run("not-a-run-id")


@pytest.mark.parametrize(
    "bad", ["../escape", "..", ".", ".hidden", "a/b", "a" + chr(92) + "b", "", "  "]
)
def test_validate_experiment_name_rejects_unsafe(bad, store_mod):
    with pytest.raises(ValueError):
        store_mod.validate_experiment_name(bad)


def test_validate_experiment_name_accepts_plain(store_mod):
    assert store_mod.validate_experiment_name("exp-a") == "exp-a"


def test_start_and_list_reject_traversal(store):
    with pytest.raises(ValueError):
        store.start_run("../escape", "g")
    with pytest.raises(ValueError):
        store.list_runs("..")


def test_run_dir_is_created_exclusively(store, tmp_path):
    """A pre-existing run-001 directory (another process won the race) is skipped."""
    runs = tmp_path / "experiments" / "exp-a" / "runs"
    (runs / "run-001").mkdir(parents=True)  # no run.json: an in-flight competitor
    assert store.start_run("exp-a", "g") == "exp-a/run-002"


def test_reads_a_run_written_by_the_parked_code(store_mod):
    store = store_mod.LocalFsExperimentStore(FIXTURE_ROOT)
    run = store.get_run("exp-a/run-001")
    assert run.goal == "objective X" and run.outcome == "success"
    assert run.metrics == {"accuracy": 0.95}
    (obs,) = store.observations("exp-a/run-001")
    assert (obs.number, obs.title, obs.next_direction) == (1, "first", "n1")


def test_writes_the_parked_layout_byte_for_byte(tmp_path, store_mod, monkeypatch):
    monkeypatch.setattr(store_mod, "now_iso", lambda: "2026-08-13T10:30:00")
    store = store_mod.LocalFsExperimentStore(tmp_path)
    run_id = store.start_run("exp-a", "objective X")
    store.record_observation(
        run_id,
        store_mod.Observation(
            title="first", hypothesis="h1", changes="c1", result="r1",
            diagnosis="d1", next_direction="n1",
        ),
    )
    written = (tmp_path / "exp-a" / "runs" / "run-001" / "journal" / "001_first.md").read_text()
    expected = (FIXTURE_ROOT / "exp-a/runs/run-001/journal/001_first.md").read_text()
    assert written == expected
    run_json = json.loads((tmp_path / "exp-a" / "runs" / "run-001" / "run.json").read_text())
    assert list(run_json) == [
        "run_id", "experiment", "goal", "started_at", "ended_at", "outcome", "metrics"
    ]


def test_state_and_evals_are_invisible_to_the_reader(store, tmp_path):
    run_id = store.start_run("exp-a", "g")
    run_dir = store.run_dir(run_id)
    (run_dir / "state.json").write_text("{}")
    (run_dir / "evals").mkdir()
    (run_dir / "evals" / "001.json").write_text("{}")
    assert [r.run_id for r in store.list_runs("exp-a")] == [run_id]
    assert store.observations(run_id) == []
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_experiment_store.py -v`
Expected: FAIL (`store.py` not found).

- [ ] **Step 4: Write the store**

```python
# plugins/experiment/store.py
"""Experiment run store: the (reader, writer) contract over a filesystem layout.

Ported from the experiment plugin parked in the agent-swarm repository. The
on-disk layout is unchanged and byte-compatible:

    <root>/<experiment>/runs/run-NNN/run.json
    <root>/<experiment>/runs/run-NNN/journal/NNN_<slug>.md   (frontmatter canonical)

Tenancy: a store only ever touches its own root; experiment names that could
escape it are rejected at every entry point.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

_RUN_RE = re.compile(r"run-(\d+)$")
_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
OBS_FIELDS = ("title", "hypothesis", "changes", "result", "diagnosis", "next_direction")
_OBS_BODY = """# {title}

**Attempt:** {number}
**Hypothesis:** {hypothesis}

## Changes
{changes}

## Result
{result}

## Diagnosis
{diagnosis}

## Next Direction
{next_direction}
"""
_MAX_ALLOCATION_ATTEMPTS = 50


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp sibling and rename, so a crash never leaves a torn file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def validate_experiment_name(name: str) -> str:
    """Reject names that would escape the store's own subtree."""
    if not name or not name.strip():
        raise ValueError("experiment name is required")
    if "/" in name or "\\" in name or name in (".", "..") or name.startswith("."):
        raise ValueError(f"invalid experiment name: {name!r}")
    return name


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return (s or "observation")[:40]


@dataclass
class Observation:
    """A single experiment observation (one journal attempt)."""

    title: str
    hypothesis: str = ""
    changes: str = ""
    result: str = ""
    diagnosis: str = ""
    next_direction: str = ""
    run_id: Optional[str] = None  # assigned by the writer
    number: Optional[int] = None  # assigned by the writer


@dataclass
class Run:
    """An experiment run: a bounded attempt sequence with an outcome."""

    run_id: str
    experiment: str
    goal: str = ""
    started_at: str = ""
    ended_at: Optional[str] = None
    outcome: Optional[str] = None
    metrics: dict = field(default_factory=dict)


class ExperimentWriter(ABC):
    """Write surface."""

    @abstractmethod
    def start_run(self, experiment: str, goal: str) -> str: ...

    @abstractmethod
    def record_observation(self, run_id: str, observation: Observation) -> str: ...

    @abstractmethod
    def end_run(self, run_id: str, outcome: str, metrics: dict) -> None: ...


class ExperimentReader(ABC):
    """Read surface."""

    @abstractmethod
    def list_runs(self, experiment: str) -> list[Run]: ...

    @abstractmethod
    def get_run(self, run_id: str) -> Run: ...

    @abstractmethod
    def observations(self, run_id: str) -> list[Observation]: ...


def render_observation(obs: Observation, number: int, run_id: str) -> str:
    front = {"run_id": run_id, "number": number, "created_at": now_iso()}
    for f in OBS_FIELDS:
        front[f] = getattr(obs, f)
    body = _OBS_BODY.format(number=number, **{k: getattr(obs, k) for k in OBS_FIELDS})
    dumped = yaml.safe_dump(front, sort_keys=False, allow_unicode=True)
    return f"---\n{dumped}---\n\n{body}"


def parse_observation(text: str) -> Observation:
    m = _FRONT_RE.match(text)
    if not m:
        raise ValueError("observation file missing frontmatter")
    front = yaml.safe_load(m.group(1)) or {}
    return Observation(
        title=front.get("title", ""),
        hypothesis=front.get("hypothesis", ""),
        changes=front.get("changes", ""),
        result=front.get("result", ""),
        diagnosis=front.get("diagnosis", ""),
        next_direction=front.get("next_direction", ""),
        run_id=front.get("run_id"),
        number=front.get("number"),
    )


class LocalFsExperimentStore(ExperimentWriter, ExperimentReader):
    """Filesystem-backed store rooted at an experiments parent directory."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    # -- writer --------------------------------------------------------------

    def start_run(self, experiment: str, goal: str) -> str:
        runs_dir = self.runs_dir(experiment)
        runs_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(_MAX_ALLOCATION_ATTEMPTS):
            run_name = f"run-{self._next_run_number(runs_dir):03d}"
            run_dir = runs_dir / run_name
            try:
                run_dir.mkdir(exist_ok=False)  # exclusive: a competitor's dir is skipped
            except FileExistsError:
                continue
            (run_dir / "journal").mkdir()
            run_id = f"{experiment}/{run_name}"
            self._write_run(
                run_dir, Run(run_id=run_id, experiment=experiment, goal=goal, started_at=now_iso())
            )
            return run_id
        raise RuntimeError(
            f"could not allocate a run directory under {runs_dir} after"
            f" {_MAX_ALLOCATION_ATTEMPTS} attempts"
        )

    def record_observation(self, run_id: str, observation: Observation) -> str:
        run_dir = self.run_dir(run_id)
        if not (run_dir / "run.json").exists():
            raise KeyError(f"no such run: {run_id!r}")
        journal = run_dir / "journal"
        journal.mkdir(parents=True, exist_ok=True)
        number = self._next_obs_number(journal)
        path = journal / f"{number:03d}_{_slug(observation.title)}.md"
        atomic_write_text(path, render_observation(observation, number, run_id))
        return f"{run_id}#{number:03d}"

    def end_run(self, run_id: str, outcome: str, metrics: dict) -> None:
        run = self.get_run(run_id)
        run.outcome = outcome
        run.metrics = dict(metrics or {})
        run.ended_at = now_iso()
        self._write_run(self.run_dir(run_id), run)

    # -- reader --------------------------------------------------------------

    def list_runs(self, experiment: str) -> list[Run]:
        runs_dir = self.runs_dir(experiment)
        if not runs_dir.exists():
            return []
        return [
            self._read_run(d)
            for d in sorted(runs_dir.iterdir())
            if d.is_dir() and _RUN_RE.match(d.name) and (d / "run.json").exists()
        ]

    def get_run(self, run_id: str) -> Run:
        run_dir = self.run_dir(run_id)
        if not (run_dir / "run.json").exists():
            raise KeyError(f"no such run: {run_id!r}")
        return self._read_run(run_dir)

    def observations(self, run_id: str) -> list[Observation]:
        journal = self.run_dir(run_id) / "journal"
        if not journal.exists():
            return []
        return [parse_observation(p.read_text()) for p in sorted(journal.glob("*.md"))]

    # -- paths / helpers -----------------------------------------------------

    def runs_dir(self, experiment: str) -> Path:
        validate_experiment_name(experiment)
        return self.root / experiment / "runs"

    def run_dir(self, run_id: str) -> Path:
        experiment, run_name = self._split_run_id(run_id)
        return self.root / experiment / "runs" / run_name

    @staticmethod
    def _split_run_id(run_id: str) -> tuple[str, str]:
        experiment, _, run_name = run_id.rpartition("/")
        if not experiment or not _RUN_RE.match(run_name):
            raise KeyError(f"malformed run_id: {run_id!r}")
        validate_experiment_name(experiment)
        return experiment, run_name

    @staticmethod
    def _next_run_number(runs_dir: Path) -> int:
        nums = [
            int(m.group(1)) for d in runs_dir.iterdir() if d.is_dir() and (m := _RUN_RE.match(d.name))
        ]
        return max(nums, default=0) + 1

    @staticmethod
    def _next_obs_number(journal: Path) -> int:
        nums = [
            int(p.name.split("_", 1)[0])
            for p in journal.glob("*.md")
            if p.name.split("_", 1)[0].isdigit()
        ]
        return max(nums, default=0) + 1

    @staticmethod
    def _write_run(run_dir: Path, run: Run) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            run_dir / "run.json",
            json.dumps(
                {
                    "run_id": run.run_id,
                    "experiment": run.experiment,
                    "goal": run.goal,
                    "started_at": run.started_at,
                    "ended_at": run.ended_at,
                    "outcome": run.outcome,
                    "metrics": run.metrics,
                },
                indent=2,
            ),
        )

    @staticmethod
    def _read_run(run_dir: Path) -> Run:
        data = json.loads((run_dir / "run.json").read_text())
        return Run(
            run_id=data["run_id"],
            experiment=data["experiment"],
            goal=data.get("goal", ""),
            started_at=data.get("started_at", ""),
            ended_at=data.get("ended_at"),
            outcome=data.get("outcome"),
            metrics=data.get("metrics", {}),
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_experiment_store.py -v`
Expected: PASS (17 tests).

- [ ] **Step 6: Commit**

```bash
git add plugins/experiment/store.py tests/test_experiment_store.py tests/fixtures/experiment_parked_run
git commit -m "feat(experiment): port the run store with the frozen on-disk layout"
```

---

### Task 3: Port the eval library, add criteria validation, the frozen eval spec and the protected-tree digest

**Files:**
- Create: `plugins/experiment/evals.py`
- Test: `tests/test_experiment_evals.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `Goal`, `load_goal(exp_dir) -> Goal`, `Constraints`, `load_constraints(exp_dir) -> Constraints`, `normalize_criteria(raw) -> list[dict]` (each `{metric, threshold, comparison, primary, report, description}`), `validate_environment(raw) -> dict[str, str]`, `CriteriaResult(passed, primary_passed, all_passed, details)`, `check_criteria(criteria, metrics)`, `criteria_pass(criteria, metrics) -> bool`, `parse_metrics(text) -> dict`, `parse_pytest_summary(text) -> (total, passed, failed)`, `resolve_eval_spec(exp_dir, goal) -> dict` (`{"kind": "pytest_dir"|"script"|"exec"|"command", "path": str|None, "argv": list|None}`), `select_interpreter(exp_dir, goal) -> str`, `build_command(spec, interpreter) -> list[str]`, `EvalResult`, `async run_eval(exp_dir, spec, *, interpreter, env, timeout) -> EvalResult`, `derive_metrics(result) -> dict`, `protected_roots(exp_dir, spec) -> list[Path]`, `digest_protected(roots) -> dict[str, str]`, `changed_protected(roots, old) -> list[str]`, `bounded_tail(text, limit=4000) -> str`, `GoalError`, `DEFAULT_EVAL_TIMEOUT_S = 300`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_experiment_evals.py
"""Goal loading, criteria validation, the frozen eval spec, eval execution, protected digest."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).parent.parent / "plugins" / "experiment"


def load_plugin_module(name: str):
    spec = importlib.util.spec_from_file_location(
        f"experiment_plugin_test_{name}", PLUGIN_ROOT / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def evals():
    return load_plugin_module("evals")


@pytest.fixture
def exp(tmp_path):
    d = tmp_path / "test-exp"
    d.mkdir()
    return d


def write_goal(exp_dir: Path, goal: dict) -> None:
    (exp_dir / "goal.yaml").write_text(yaml.dump(goal))


# -- goal / constraints -------------------------------------------------------


def test_loads_standalone_goal(evals, exp):
    write_goal(exp, {"objective": "Train", "eval": "eval/test_model.py",
                     "success_criteria": [{"metric": "accuracy", "threshold": 0.9, "primary": True}]})
    goal = evals.load_goal(exp)
    assert goal.objective == "Train" and goal.is_standalone and not goal.is_integration
    assert goal.primary_criterion["metric"] == "accuracy"


def test_loads_integration_goal_and_extras(evals, exp):
    write_goal(exp, {"objective": "o", "eval": "eval/", "target": "hermes/src/x.py",
                     "success_criteria": [], "environment": {"A": "1"},
                     "eval_python": "/usr/bin/python3", "eval_timeout_s": 42})
    goal = evals.load_goal(exp)
    assert goal.is_integration and goal.target == "hermes/src/x.py"
    assert goal.environment == {"A": "1"}
    assert goal.eval_python == "/usr/bin/python3" and goal.eval_timeout_s == 42


def test_missing_goal_raises(evals, exp):
    with pytest.raises(FileNotFoundError):
        evals.load_goal(exp)


def test_goal_must_be_a_mapping(evals, exp):
    (exp / "goal.yaml").write_text("- just\n- a list\n")
    with pytest.raises(evals.GoalError, match="mapping"):
        evals.load_goal(exp)


def test_primary_criterion_defaults_to_first_and_none_when_empty(evals, exp):
    write_goal(exp, {"objective": "o", "eval": "eval/",
                     "success_criteria": [{"metric": "loss", "threshold": 0.1},
                                          {"metric": "acc", "threshold": 0.9}]})
    assert evals.load_goal(exp).primary_criterion["metric"] == "loss"
    write_goal(exp, {"objective": "o", "eval": "eval/", "success_criteria": []})
    assert evals.load_goal(exp).primary_criterion is None


def test_loads_constraints_and_missing_is_empty(evals, exp):
    assert evals.load_constraints(exp).do_not_do == []
    (exp / "constraints.yaml").write_text(yaml.dump({
        "time_limits": {"max_hours_per_run": 4},
        "do_not_do": ["no fine-tune"], "escalate_if": ["cannot load"],
        "known_findings": ["bs>64 OOM"]}))
    c = evals.load_constraints(exp)
    assert c.max_hours_per_run == 4 and c.do_not_do == ["no fine-tune"]
    assert c.escalate_if == ["cannot load"] and c.known_findings == ["bs>64 OOM"]


# -- criteria -----------------------------------------------------------------


def test_normalize_accepts_symbols_and_aliases(evals):
    raw = [
        {"metric": "a", "threshold": 1, "primary": True},
        {"metric": "b", "threshold": 2, "comparison": "<="},
        {"metric": "c", "threshold": 0, "comparator": "gt"},
        {"metric": "d", "threshold": None, "comparator": "report", "description": "x"},
    ]
    out = evals.normalize_criteria(raw)
    assert [c["comparison"] for c in out] == [">=", "<=", ">", "report"]
    assert [c["report"] for c in out] == [False, False, False, True]
    assert out[0]["primary"] is True and out[1]["primary"] is False


def test_null_threshold_means_report_only(evals):
    (c,) = evals.normalize_criteria([{"metric": "x", "threshold": None}])
    assert c["report"] is True and c["comparison"] == "report"


@pytest.mark.parametrize("bad", [
    [{"threshold": 1}],                                    # no metric
    [{"metric": "m"}],                                     # no threshold
    [{"metric": "m", "threshold": 1, "comparison": "!="}],  # unknown operator
    [{"metric": "m", "threshold": "1"}],                   # threshold not numeric
    [{"metric": "m", "threshold": 1, "bogus": True}],      # unknown key
    ["not a mapping"],
])
def test_normalize_rejects_with_teaching_text(evals, bad):
    with pytest.raises(ValueError, match="success_criteria"):
        evals.normalize_criteria(bad)


def test_check_criteria_matches_parked_semantics(evals):
    criteria = evals.normalize_criteria([
        {"metric": "accuracy", "threshold": 0.9, "primary": True},
        {"metric": "loss", "threshold": 0.1, "comparison": "<="},
    ])
    r = evals.check_criteria(criteria, {"accuracy": 0.95, "loss": 0.05})
    assert r.passed and r.primary_passed and r.all_passed
    r = evals.check_criteria(criteria, {"accuracy": 0.95, "loss": 0.5})
    assert r.primary_passed and not r.all_passed
    r = evals.check_criteria(criteria, {})
    assert not r.passed and r.details[0]["actual"] is None


def test_check_criteria_report_only_never_gates(evals):
    criteria = evals.normalize_criteria([
        {"metric": "a", "threshold": 1, "primary": True},
        {"metric": "conflict", "threshold": None, "comparator": "report"},
    ])
    r = evals.check_criteria(criteria, {"a": 5})
    assert r.passed and r.all_passed
    assert r.details[1]["met"] is None


def test_check_criteria_unknown_operator_raises_not_defaults(evals):
    with pytest.raises(ValueError, match="comparison"):
        evals.check_criteria([{"metric": "s", "threshold": 0.5, "comparison": "!="}], {"s": 1})


def test_criteria_pass_primary_if_any_else_all(evals):
    with_primary = evals.normalize_criteria([
        {"metric": "a", "threshold": 1, "primary": True}, {"metric": "b", "threshold": 1}])
    assert evals.criteria_pass(with_primary, {"a": 1, "b": 0}) is True
    no_primary = evals.normalize_criteria([{"metric": "a", "threshold": 1}, {"metric": "b", "threshold": 1}])
    assert evals.criteria_pass(no_primary, {"a": 1, "b": 0}) is False
    assert evals.criteria_pass([], {}) is True


def test_validate_environment_coerces_and_rejects(evals):
    assert evals.validate_environment({"PORT": 8080, "FLAG": True}) == {"PORT": "8080", "FLAG": "True"}
    assert evals.validate_environment(None) == {}
    with pytest.raises(ValueError, match="environment"):
        evals.validate_environment({"NESTED": {"a": 1}})
    with pytest.raises(ValueError, match="environment"):
        evals.validate_environment(["not", "a", "mapping"])


# -- parsing ------------------------------------------------------------------


def test_parse_metrics_formats(evals):
    m = evals.parse_metrics("[METRIC] accuracy=0.95\n[METRIC] f1 = 0.88\n[METRIC] loss=1.5e-4\n[METRIC] t=-0.5")
    assert m["accuracy"] == 0.95 and m["f1"] == 0.88
    assert m["loss"] == pytest.approx(0.00015) and m["t"] == -0.5


def test_parse_pytest_summary(evals):
    assert evals.parse_pytest_summary("== 3 passed, 1 failed in 2.5s ==") == (4, 3, 1)
    assert evals.parse_pytest_summary("== 5 passed in 1.0s ==") == (5, 5, 0)
    assert evals.parse_pytest_summary("no tests ran") == (0, 0, 0)


# -- eval spec / interpreter / command ----------------------------------------


def test_resolve_eval_spec_forms(evals, exp):
    (exp / "eval").mkdir()
    goal = evals.Goal(objective="o", eval="eval/", success_criteria=[])
    spec = evals.resolve_eval_spec(exp, goal)
    assert spec["kind"] == "pytest_dir" and spec["path"] == str((exp / "eval").resolve())
    (exp / "eval" / "run.py").write_text("print('x')")
    spec = evals.resolve_eval_spec(exp, evals.Goal(objective="o", eval="eval/run.py", success_criteria=[]))
    assert spec["kind"] == "script"
    spec = evals.resolve_eval_spec(exp, evals.Goal(objective="o", eval="uv run harness/run.py --replay", success_criteria=[]))
    assert spec["kind"] == "command" and spec["argv"] == ["uv", "run", "harness/run.py", "--replay"]


def test_select_interpreter_order(evals, exp):
    goal = evals.Goal(objective="o", eval="eval/", success_criteria=[])
    assert evals.select_interpreter(exp, goal) == sys.executable
    venv_py = exp / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    assert evals.select_interpreter(exp, goal) == str(venv_py)
    goal = evals.Goal(objective="o", eval="eval/", success_criteria=[], eval_python="/opt/py")
    assert evals.select_interpreter(exp, goal) == "/opt/py"


def test_build_command(evals):
    assert evals.build_command({"kind": "pytest_dir", "path": "/e/eval", "argv": None}, "/py") == [
        "/py", "-m", "pytest", "/e/eval", "-v", "-s"]
    assert evals.build_command({"kind": "script", "path": "/e/run.py", "argv": None}, "/py") == ["/py", "/e/run.py"]
    assert evals.build_command({"kind": "exec", "path": "/e/run.sh", "argv": None}, "/py") == ["/e/run.sh"]
    assert evals.build_command({"kind": "command", "path": None, "argv": ["uv", "run", "x"]}, "/py") == ["uv", "run", "x"]


# -- run_eval -----------------------------------------------------------------


async def _run(evals, exp, spec, timeout=30, env=None):
    return await evals.run_eval(exp, spec, interpreter=sys.executable,
                                env={**os.environ, **(env or {})}, timeout=timeout)


async def test_run_pytest_eval_pass_and_metrics(evals, exp):
    (exp / "eval").mkdir()
    (exp / "eval" / "test_ok.py").write_text(
        'def test_ok():\n    print("[METRIC] accuracy=0.95")\n    assert True\n')
    r = await _run(evals, exp, {"kind": "pytest_dir", "path": str(exp / "eval"), "argv": None})
    assert r.passed and r.return_code == 0 and r.tests_run == 1 and r.tests_passed == 1
    assert r.metrics == {"accuracy": 0.95}


async def test_run_pytest_eval_fail(evals, exp):
    (exp / "eval").mkdir()
    (exp / "eval" / "test_bad.py").write_text("def test_bad(): assert False\n")
    r = await _run(evals, exp, {"kind": "pytest_dir", "path": str(exp / "eval"), "argv": None})
    assert not r.passed and r.tests_failed == 1


async def test_run_eval_timeout_kills_process_group(evals, exp):
    script = exp / "slow.py"
    script.write_text("import subprocess, sys, time\n"
                      "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                      "time.sleep(30)\n")
    r = await _run(evals, exp, {"kind": "script", "path": str(script), "argv": None}, timeout=1)
    assert r.timed_out and not r.passed


async def test_run_eval_stdin_is_devnull(evals, exp):
    script = exp / "reads.py"
    script.write_text("import sys\nprint('[METRIC] n=' + str(len(sys.stdin.read())))\n")
    r = await _run(evals, exp, {"kind": "script", "path": str(script), "argv": None})
    assert r.metrics == {"n": 0.0}


async def test_run_eval_environment_reaches_child(evals, exp):
    script = exp / "env.py"
    script.write_text("import os\nprint('[METRIC] v=' + os.environ['EXP_VALUE'])\n")
    r = await _run(evals, exp, {"kind": "script", "path": str(script), "argv": None}, env={"EXP_VALUE": "7"})
    assert r.metrics == {"v": 7.0}


async def test_run_eval_command_form_runs_in_experiment_dir(evals, exp):
    (exp / "note.txt").write_text("hi")
    spec = {"kind": "command", "path": None,
            "argv": [sys.executable, "-c", "import os; print('[METRIC] here=' + str(int(os.path.exists('note.txt'))))"]}
    r = await _run(evals, exp, spec)
    assert r.metrics == {"here": 1.0}


async def test_run_eval_missing_target_is_a_result_not_an_exception(evals, exp):
    r = await _run(evals, exp, {"kind": "exec", "path": str(exp / "nope.sh"), "argv": None})
    assert not r.passed and r.return_code != 0 and "nope.sh" in r.stderr


def test_derive_metrics_only_with_tests_and_never_overwrites(evals):
    r = evals.EvalResult(passed=True, tests_run=4, tests_passed=3, tests_failed=1,
                         metrics={"test_pass_rate": 0.5})
    m = evals.derive_metrics(r)
    assert m["test_pass_rate"] == 0.5  # explicit metric wins
    assert m["tests_run"] == 4 and m["tests_passed"] == 3 and m["tests_failed"] == 1
    r = evals.EvalResult(passed=True, tests_run=0, metrics={"x": 1.0})
    assert evals.derive_metrics(r) == {"x": 1.0}
    r = evals.EvalResult(passed=True, tests_run=4, tests_passed=4, tests_failed=0, metrics={})
    assert evals.derive_metrics(r)["test_pass_rate"] == 1.0


# -- protected tree -------------------------------------------------------------


def test_protected_roots_and_digest_detect_changes(evals, exp):
    (exp / "goal.yaml").write_text("objective: o\n")
    (exp / "eval").mkdir()
    (exp / "eval" / "metrics.py").write_text("a = 1\n")
    (exp / "eval" / "__pycache__").mkdir()
    (exp / "eval" / "__pycache__" / "x.pyc").write_text("cache")
    (exp / "fixtures").mkdir()
    (exp / "fixtures" / "f.json").write_text("{}")
    spec = {"kind": "script", "path": str(exp / "harness.py"), "argv": None}
    roots = evals.protected_roots(exp, spec)
    assert {r.name for r in roots} == {"goal.yaml", "eval", "fixtures", "harness.py"}
    digest = evals.digest_protected(roots)
    assert "eval/metrics.py" in digest and not any("__pycache__" in k for k in digest)
    assert evals.changed_protected(roots, digest) == []
    (exp / "eval" / "metrics.py").write_text("a = 2\n")
    (exp / "eval" / "extra.py").write_text("")
    assert evals.changed_protected(roots, digest) == ["eval/extra.py", "eval/metrics.py"]


def test_bounded_tail_marks_truncation(evals):
    assert evals.bounded_tail("short") == "short"
    tail = evals.bounded_tail("x" * 5000, limit=100)
    assert tail.startswith("[truncated 4900 chars]") and tail.endswith("x" * 100)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_experiment_evals.py -v`
Expected: FAIL (`evals.py` not found).

- [ ] **Step 3: Write the eval library**

```python
# plugins/experiment/evals.py
"""Goal, constraints, criteria and eval execution for the experiment plugin.

Ported from the experiment plugin parked in the agent-swarm repository, plus:
criteria validation with an alias map, a frozen eval spec, interpreter
selection, an async subprocess runner that never inherits stdin and kills the
whole process group, derived pytest metrics, and a protected-tree digest.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

DEFAULT_EVAL_TIMEOUT_S = 300
OUTPUT_TAIL_CHARS = 4000
PROTECTED_NAMES = ("goal.yaml", "constraints.yaml", "eval", "fixtures")
_SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache"})
_OPS = {
    ">=": ">=", "<=": "<=", ">": ">", "<": "<", "==": "==",
    "ge": ">=", "le": "<=", "gt": ">", "lt": "<", "eq": "==",
    "report": "report",
}
_CRITERION_KEYS = frozenset(
    {"metric", "threshold", "comparison", "comparator", "primary", "description"}
)
_METRIC_RE = re.compile(r"\[METRIC\]\s*(\w+)\s*=\s*(-?[\d.]+(?:[eE][+-]?\d+)?)")


class GoalError(ValueError):
    """goal.yaml is present but unusable; the message says what to fix."""


# ---------------------------------------------------------------------------
# Goal / constraints
# ---------------------------------------------------------------------------


@dataclass
class Goal:
    objective: str
    eval: str
    success_criteria: list
    target: Optional[str] = None
    context: Optional[str] = None
    environment: Optional[dict] = None
    eval_python: Optional[str] = None
    eval_timeout_s: Optional[int] = None
    _raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_integration(self) -> bool:
        return self.target is not None

    @property
    def is_standalone(self) -> bool:
        return self.target is None

    @property
    def primary_criterion(self) -> Optional[dict]:
        for c in self.success_criteria:
            if isinstance(c, dict) and c.get("primary"):
                return c
        return self.success_criteria[0] if self.success_criteria else None

    def get(self, key, default=None):
        return self._raw.get(key, default)


def load_goal(exp_dir: Path) -> Goal:
    goal_path = Path(exp_dir) / "goal.yaml"
    if not goal_path.exists():
        raise FileNotFoundError(f"No goal.yaml in {exp_dir}")
    raw = yaml.safe_load(goal_path.read_text())
    if not isinstance(raw, dict):
        raise GoalError(f"{goal_path}: goal.yaml must be a YAML mapping with at least 'objective'")
    return Goal(
        objective=str(raw.get("objective", "")),
        eval=str(raw.get("eval", "eval/") or "eval/"),
        success_criteria=raw.get("success_criteria") or [],
        target=raw.get("target"),
        context=raw.get("context"),
        environment=raw.get("environment"),
        eval_python=raw.get("eval_python"),
        eval_timeout_s=raw.get("eval_timeout_s"),
        _raw=raw,
    )


@dataclass
class Constraints:
    max_hours_per_run: Optional[float] = None
    max_total_gpu_hours: Optional[float] = None
    do_not_do: list[str] = field(default_factory=list)
    escalate_if: list[str] = field(default_factory=list)
    known_findings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "max_hours_per_run": self.max_hours_per_run,
            "max_total_gpu_hours": self.max_total_gpu_hours,
            "do_not_do": list(self.do_not_do),
            "escalate_if": list(self.escalate_if),
            "known_findings": list(self.known_findings),
        }


def load_constraints(exp_dir: Path) -> Constraints:
    path = Path(exp_dir) / "constraints.yaml"
    if not path.exists():
        return Constraints()
    raw = yaml.safe_load(path.read_text()) or {}
    time_limits = raw.get("time_limits", {}) or {}
    return Constraints(
        max_hours_per_run=time_limits.get("max_hours_per_run"),
        max_total_gpu_hours=time_limits.get("max_total_gpu_hours"),
        do_not_do=list(raw.get("do_not_do", []) or []),
        escalate_if=list(raw.get("escalate_if", []) or []),
        known_findings=list(raw.get("known_findings", []) or []),
    )


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------


def normalize_criteria(raw: list) -> list[dict]:
    """Validate goal.yaml success_criteria and normalize operators.

    Accepts `comparison` or `comparator`, symbols or ge/le/gt/lt/eq, and
    `report` (or a null threshold) for report-only criteria. Any other key or
    operator is an error before a run is created.
    """
    if not isinstance(raw, list):
        raise ValueError("success_criteria must be a list of mappings")
    out = []
    for i, c in enumerate(raw):
        where = f"success_criteria[{i}]"
        if not isinstance(c, dict):
            raise ValueError(f"{where} must be a mapping with metric and threshold")
        unknown = sorted(set(c) - _CRITERION_KEYS)
        if unknown:
            raise ValueError(
                f"{where} has unknown key(s) {', '.join(unknown)}; allowed:"
                f" {', '.join(sorted(_CRITERION_KEYS))}"
            )
        metric = c.get("metric")
        if not isinstance(metric, str) or not metric:
            raise ValueError(f"{where} needs a non-empty string 'metric'")
        if "threshold" not in c:
            raise ValueError(f"{where} ({metric}) needs a 'threshold' (use null for report-only)")
        threshold = c["threshold"]
        op_raw = c.get("comparison", c.get("comparator"))
        if op_raw is None:
            op_raw = ">=" if threshold is not None else "report"
        op = _OPS.get(str(op_raw).strip())
        if op is None:
            raise ValueError(
                f"{where} ({metric}) has unknown comparison {op_raw!r}; use one of"
                f" {', '.join(sorted(set(_OPS)))}"
            )
        if threshold is None and op != "report":
            op = "report"
        if op != "report" and (isinstance(threshold, bool) or not isinstance(threshold, (int, float))):
            raise ValueError(f"{where} ({metric}) threshold must be a number, got {threshold!r}")
        out.append(
            {
                "metric": metric,
                "threshold": threshold,
                "comparison": op,
                "primary": bool(c.get("primary", False)),
                "report": op == "report",
                "description": str(c.get("description", "") or ""),
            }
        )
    return out


def validate_environment(raw) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("goal.yaml environment must be a mapping of NAME: value")
    out = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not k or isinstance(v, (dict, list)):
            raise ValueError(
                f"goal.yaml environment entry {k!r} must be a string name with a scalar value"
            )
        out[k] = str(v)
    return out


@dataclass
class CriteriaResult:
    passed: bool
    primary_passed: bool
    all_passed: bool
    details: list[dict] = field(default_factory=list)


def _compare(op: str, actual, threshold) -> bool:
    if op == ">=":
        return actual >= threshold
    if op == "<=":
        return actual <= threshold
    if op == ">":
        return actual > threshold
    if op == "<":
        return actual < threshold
    if op == "==":
        return actual == threshold
    raise ValueError(f"unknown comparison operator {op!r}")


def check_criteria(criteria: list[dict], metrics: dict) -> CriteriaResult:
    """Check metrics against criteria. Report-only criteria are listed, never gate."""
    details = []
    primary_passed = all_met = True
    for c in criteria:
        name = c["metric"]
        op = c.get("comparison", ">=")
        is_primary = bool(c.get("primary", False))
        actual = metrics.get(name)
        if op == "report" or c.get("report"):
            details.append({"metric": name, "threshold": c.get("threshold"), "actual": actual,
                            "met": None, "primary": is_primary, "report": True})
            continue
        if op not in _OPS.values():
            raise ValueError(f"criterion {name!r} has unknown comparison {op!r}")
        met = actual is not None and _compare(op, actual, c["threshold"])
        details.append({"metric": name, "threshold": c["threshold"], "actual": actual,
                        "met": met, "primary": is_primary, "report": False})
        if not met:
            all_met = False
            if is_primary:
                primary_passed = False
    return CriteriaResult(passed=primary_passed, primary_passed=primary_passed,
                          all_passed=all_met, details=details)


def criteria_pass(criteria: list[dict], metrics: dict) -> bool:
    """Primary criteria if any is marked primary, else all criteria; empty passes."""
    gating = [c for c in criteria if not c.get("report") and c.get("comparison") != "report"]
    if not gating:
        return True
    result = check_criteria(gating, metrics)
    return result.passed if any(c.get("primary") for c in gating) else result.all_passed


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def parse_metrics(output: str) -> dict:
    metrics = {}
    for match in _METRIC_RE.finditer(output):
        try:
            metrics[match.group(1)] = float(match.group(2))
        except ValueError:
            metrics[match.group(1)] = match.group(2)
    return metrics


def parse_pytest_summary(output: str) -> tuple[int, int, int]:
    passed = failed = 0
    for m in re.finditer(r"(\d+) passed", output):
        passed = int(m.group(1))
    for m in re.finditer(r"(\d+) failed", output):
        failed = int(m.group(1))
    return passed + failed, passed, failed


# ---------------------------------------------------------------------------
# Eval spec, interpreter, command
# ---------------------------------------------------------------------------


def resolve_eval_spec(exp_dir: Path, goal: Goal) -> dict:
    """Freeze what the eval phase runs. A string with whitespace is a command."""
    raw = (goal.eval or "eval/").strip()
    if any(ch.isspace() for ch in raw):
        return {"kind": "command", "path": None, "argv": shlex.split(raw)}
    target = (Path(exp_dir) / raw).resolve()
    if target.is_dir():
        kind = "pytest_dir"
    elif target.suffix == ".py":
        kind = "script"
    else:
        kind = "exec"
    return {"kind": kind, "path": str(target), "argv": None}


def select_interpreter(exp_dir: Path, goal: Goal) -> str:
    if goal.eval_python:
        return str(goal.eval_python)
    venv_python = Path(exp_dir) / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def build_command(spec: dict, interpreter: str) -> list[str]:
    kind = spec["kind"]
    if kind == "command":
        return list(spec["argv"])
    if kind == "pytest_dir":
        return [interpreter, "-m", "pytest", spec["path"], "-v", "-s"]
    if kind == "script":
        return [interpreter, spec["path"]]
    return [spec["path"]]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class EvalResult:
    passed: bool
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    metrics: dict = field(default_factory=dict)
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0


def _kill_group(proc) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def run_eval(exp_dir: Path, spec: dict, *, interpreter: str, env: dict[str, str],
                   timeout: float) -> EvalResult:
    """Run the frozen eval spec in its own process group with stdin closed."""
    argv = build_command(spec, interpreter)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(exp_dir),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return EvalResult(passed=False, return_code=127,
                          stderr=f"could not start eval {argv!r}: {exc}")
    out_task = asyncio.create_task(proc.stdout.read())
    err_task = asyncio.create_task(proc.stderr.read())
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        _kill_group(proc)
        await proc.wait()
    except asyncio.CancelledError:
        _kill_group(proc)
        raise
    stdout = (await out_task).decode("utf-8", errors="replace")
    stderr = (await err_task).decode("utf-8", errors="replace")
    output = stdout + stderr
    total, passed, failed = parse_pytest_summary(output)
    return_code = proc.returncode if proc.returncode is not None else -1
    return EvalResult(
        passed=(return_code == 0 and not timed_out),
        tests_run=total, tests_passed=passed, tests_failed=failed,
        metrics=parse_metrics(output), timed_out=timed_out,
        stdout=stdout, stderr=stderr, return_code=return_code,
    )


def derive_metrics(result: EvalResult) -> dict:
    """Parsed metrics plus pytest-derived ones; explicit [METRIC] values always win."""
    metrics = dict(result.metrics)
    if result.tests_run > 0:
        derived = {
            "test_pass_rate": result.tests_passed / result.tests_run,
            "tests_run": float(result.tests_run),
            "tests_passed": float(result.tests_passed),
            "tests_failed": float(result.tests_failed),
        }
        for k, v in derived.items():
            metrics.setdefault(k, v)
    return metrics


def bounded_tail(text: str, limit: int = OUTPUT_TAIL_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"[truncated {len(text) - limit} chars]\n" + text[-limit:]


# ---------------------------------------------------------------------------
# Protected tree
# ---------------------------------------------------------------------------


def protected_roots(exp_dir: Path, spec: dict) -> list[Path]:
    exp_dir = Path(exp_dir).resolve()
    roots = [exp_dir / name for name in PROTECTED_NAMES]
    if spec.get("path"):
        target = Path(spec["path"])
        if target not in roots and not any(
            target == r or target.is_relative_to(r) for r in roots
        ):
            roots.append(target)
    return [r for r in roots if r.exists()]


def _iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and not any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            yield path


def digest_protected(roots: list[Path]) -> dict[str, str]:
    """Relative path -> sha256 for every file under the protected roots."""
    digest = {}
    for root in roots:
        base = root.parent
        for path in _iter_files(root):
            digest[str(path.relative_to(base))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def changed_protected(roots: list[Path], old: dict[str, str]) -> list[str]:
    """Paths added, removed or modified since `old` was taken, sorted."""
    new = digest_protected(roots)
    changed = {k for k in set(old) | set(new) if old.get(k) != new.get(k)}
    return sorted(changed)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_experiment_evals.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/experiment/evals.py tests/test_experiment_evals.py
git commit -m "feat(experiment): eval library with criteria validation, frozen spec and protected digest"
```

---

### Task 4: The engine: state, pointers, transitions, gates, eval records

**Files:**
- Create: `plugins/experiment/engine.py`; replace stub `plugins/experiment/policy.py` with the constants the engine needs (Task 5 completes it)
- Test: `tests/test_experiment_engine.py`

**Interfaces:**
- Consumes: `store.py` (Task 2), `evals.py` (Task 3), `policy.PHASES`, `policy.TRANSITIONS`.
- Produces: `EngineError(ValueError)`; `Context(store_root, session_id, state_dir)`; `start(experiment_dir, *, ctx, max_iterations=10, store_root_note="") -> dict`; `resume(run_id, *, ctx) -> dict`; `status(run_id, *, ctx) -> dict` (`run_id=None` resolves the pointer); `advance(run_id, phase, *, ctx, hypothesis=None) -> dict`; `async run_eval(run_id, *, ctx) -> dict`; `end(run_id, outcome, *, ctx, note="") -> dict`; `assert_loop_phase(run_id, phase, *, ctx)`; `loop_run_open(run_id, *, ctx) -> bool`; constants `DEFAULT_MAX_ITERATIONS = 10`, `TOOL_TIMEOUT_S = 3600`, `END_OUTCOMES = ("user_stopped", "escalation")`.

- [ ] **Step 1: Give policy.py its constants (the full policy lands in Task 5)**

```python
# plugins/experiment/policy.py
"""Phase names and transitions (the deny table arrives in Task 5)."""

PHASES = ("read", "plan", "work", "eval", "journal", "decide", "done")
TRANSITIONS = {
    "read": ("plan",),
    "plan": ("work",),
    "work": ("eval",),
    "eval": ("journal", "work"),
    "journal": ("decide",),
    "decide": ("plan", "done"),
    "done": (),
}
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_experiment_engine.py
"""The loop engine: the parked test matrix on a tmp store, plus pointers, resume, end."""

import importlib.util
import json
import os
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).parent.parent / "plugins" / "experiment"


def load_plugin_module(name: str):
    spec = importlib.util.spec_from_file_location(
        f"experiment_plugin_test_{name}", PLUGIN_ROOT / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine():
    return load_plugin_module("engine")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A workspace: cwd holds the experiment dir; store and state dirs live beside it."""
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def make_experiment(ws, name="exp-a", criteria=None, eval_text=None, goal_extra=None):
    exp = ws / name
    (exp / "eval").mkdir(parents=True)
    goal = {"objective": "objective X", "eval": "eval/",
            "success_criteria": criteria if criteria is not None else [
                {"metric": "accuracy", "threshold": 0.9, "primary": True}]}
    goal.update(goal_extra or {})
    (exp / "goal.yaml").write_text(yaml.dump(goal))
    (exp / "eval" / "test_metric.py").write_text(
        eval_text or 'def test_m():\n    print("[METRIC] accuracy=0.95")\n    assert True\n')
    return exp


def ctx_for(engine, ws, session="sess-1"):
    return engine.Context(store_root=str(ws / "store"), session_id=session,
                          state_dir=str(ws / "state"))


def started(engine, ws, **kw):
    ctx = ctx_for(engine, ws)
    exp = make_experiment(ws, **{k: v for k, v in kw.items() if k in ("criteria", "eval_text", "goal_extra")})
    result = engine.start(str(exp), ctx=ctx, max_iterations=kw.get("max_iterations", 10))
    return ctx, exp, result


def walk(engine, ctx, run_id, *phases, hypothesis=None):
    for p in phases:
        engine.advance(run_id, p, ctx=ctx, hypothesis=hypothesis if p == "work" else None)


# -- start ----------------------------------------------------------------------


def test_start_creates_run_state_and_pointer(engine, ws):
    ctx, exp, r = started(engine, ws)
    assert r["run_id"] == "exp-a/run-001" and r["phase"] == "read"
    assert r["experiment_dir"] == str(exp.resolve()) and r["store_root"] == str(ws / "store")
    assert r["success_criteria"][0]["comparison"] == ">="
    state = json.loads((ws / "store/exp-a/runs/run-001/state.json").read_text())
    assert state["active"] is True and state["last_eval_metrics"] is None
    assert state["last_eval_passed"] is None and state["eval"]["kind"] == "pytest_dir"
    pointer = json.loads((ws / "state" / "sess-1.json").read_text())
    assert pointer["run_id"] == "exp-a/run-001"


def test_start_requires_goal_and_a_dir_under_cwd(engine, ws, tmp_path):
    ctx = ctx_for(engine, ws)
    (ws / "empty").mkdir()
    with pytest.raises(engine.EngineError, match="goal.yaml"):
        engine.start(str(ws / "empty"), ctx=ctx)
    outside = tmp_path / "outside"
    (outside / "eval").mkdir(parents=True)
    (outside / "goal.yaml").write_text("objective: o\n")
    with pytest.raises(engine.EngineError, match="launched from"):
        engine.start(str(outside), ctx=ctx)


def test_start_validates_criteria_before_creating_anything(engine, ws):
    ctx = ctx_for(engine, ws)
    exp = make_experiment(ws, criteria=[{"metric": "m", "threshold": 1, "comparison": "!="}])
    with pytest.raises(engine.EngineError, match="success_criteria"):
        engine.start(str(exp), ctx=ctx)
    assert not (ws / "store").exists()


def test_start_refuses_when_a_run_is_open_and_names_it(engine, ws):
    ctx, exp, r = started(engine, ws)
    with pytest.raises(engine.EngineError, match="exp-a/run-001") as exc:
        engine.start(str(exp), ctx=ctx)
    assert "experiment_resume" in str(exc.value)


def test_start_rejects_max_iterations_below_one(engine, ws):
    ctx = ctx_for(engine, ws)
    exp = make_experiment(ws)
    with pytest.raises(engine.EngineError, match="max_iterations"):
        engine.start(str(exp), ctx=ctx, max_iterations=0)


def test_start_summarizes_prior_runs_bounded(engine, ws):
    ctx, exp, r = started(engine, ws)
    assert r["prior_runs"]["shown"] == 0
    engine.end(r["run_id"], "user_stopped", ctx=ctx)
    r2 = engine.start(str(exp), ctx=ctx)
    assert r2["run_id"] == "exp-a/run-002" and r2["prior_runs"]["runs"] == 1


# -- transitions ---------------------------------------------------------------


def test_valid_transitions_and_invalid_transition_message(engine, ws):
    ctx, exp, r = started(engine, ws)
    rid = r["run_id"]
    assert engine.advance(rid, "plan", ctx=ctx)["phase"] == "plan"
    with pytest.raises(engine.EngineError, match="allowed from plan: work"):
        engine.advance(rid, "eval", ctx=ctx)
    with pytest.raises(engine.EngineError, match="phase"):
        engine.advance(rid, "Done", ctx=ctx)


def test_hypothesis_recorded_on_entry_to_work(engine, ws):
    ctx, exp, r = started(engine, ws)
    walk(engine, ctx, r["run_id"], "plan", "work", hypothesis="try X")
    assert engine.status(r["run_id"], ctx=ctx)["hypotheses_tested"] == [
        {"hypothesis": "try X", "iteration": 0}]


async def test_eval_to_journal_requires_an_eval_this_visit(engine, ws):
    ctx, exp, r = started(engine, ws)
    rid = r["run_id"]
    walk(engine, ctx, rid, "plan", "work", "eval")
    with pytest.raises(engine.EngineError, match="experiment_run_eval"):
        engine.advance(rid, "journal", ctx=ctx)
    await engine.run_eval(rid, ctx=ctx)
    assert engine.advance(rid, "journal", ctx=ctx)["phase"] == "journal"
    walk(engine, ctx, rid, "decide", "plan", "work", "eval")  # kickback: new visit
    with pytest.raises(engine.EngineError, match="experiment_run_eval"):
        engine.advance(rid, "journal", ctx=ctx)


def test_eval_to_work_kickback(engine, ws):
    ctx, exp, r = started(engine, ws)
    walk(engine, ctx, r["run_id"], "plan", "work", "eval", "work")
    assert engine.status(r["run_id"], ctx=ctx)["phase"] == "work"


# -- decide->done gate -----------------------------------------------------------


async def _to_decide(engine, ws, **kw):
    ctx, exp, r = started(engine, ws, **kw)
    rid = r["run_id"]
    walk(engine, ctx, rid, "plan", "work", "eval")
    result = await engine.run_eval(rid, ctx=ctx)
    walk(engine, ctx, rid, "journal", "decide")
    return ctx, rid, result


async def test_done_allowed_when_metrics_pass_and_ends_the_run(engine, ws):
    ctx, rid, result = await _to_decide(engine, ws)
    assert result["passed"] is True
    s = engine.advance(rid, "done", ctx=ctx)
    assert s["phase"] == "done" and s["active"] is False and s["exit_reason"] == "success"
    assert not (ws / "state" / "sess-1.json").exists()
    run = json.loads((ws / "store/exp-a/runs/run-001/run.json").read_text())
    assert run["outcome"] == "success" and run["metrics"]["accuracy"] == 0.95


async def test_done_blocked_when_recorded_metrics_fail(engine, ws):
    ctx, rid, result = await _to_decide(
        engine, ws, eval_text='def test_m():\n    print("[METRIC] accuracy=0.5")\n')
    assert result["passed"] is False
    with pytest.raises(engine.EngineError, match="recomputed from recorded metrics"):
        engine.advance(rid, "done", ctx=ctx)


def test_done_blocked_when_no_eval_recorded(engine, ws):
    ctx, exp, r = started(engine, ws)
    rid = r["run_id"]
    walk(engine, ctx, rid, "plan", "work", "eval")
    # force the journal transition past the visit check to isolate the gate
    state_path = ws / "store/exp-a/runs/run-001/state.json"
    state = json.loads(state_path.read_text())
    state["eval_recorded_this_visit"] = True
    state_path.write_text(json.dumps(state))
    walk(engine, ctx, rid, "journal", "decide")
    with pytest.raises(engine.EngineError, match="no eval result recorded"):
        engine.advance(rid, "done", ctx=ctx)


async def test_done_blocked_when_claimed_pass_but_metrics_fail(engine, ws):
    ctx, rid, result = await _to_decide(
        engine, ws, eval_text='def test_m():\n    print("[METRIC] accuracy=0.5")\n')
    state_path = ws / "store/exp-a/runs/run-001/state.json"
    state = json.loads(state_path.read_text())
    state["last_eval_passed"] = True  # a lie; the gate recomputes
    state_path.write_text(json.dumps(state))
    with pytest.raises(engine.EngineError, match="not met"):
        engine.advance(rid, "done", ctx=ctx)


async def test_done_allowed_with_empty_criteria_even_without_eval(engine, ws):
    ctx, exp, r = started(engine, ws, criteria=[])
    rid = r["run_id"]
    walk(engine, ctx, rid, "plan", "work", "eval")
    await engine.run_eval(rid, ctx=ctx)
    walk(engine, ctx, rid, "journal", "decide")
    assert engine.advance(rid, "done", ctx=ctx)["phase"] == "done"


async def test_no_primary_criteria_gate_requires_all(engine, ws):
    criteria = [{"metric": "accuracy", "threshold": 0.9}, {"metric": "f1", "threshold": 0.9}]
    ctx, rid, result = await _to_decide(
        engine, ws, criteria=criteria,
        eval_text='def test_m():\n    print("[METRIC] accuracy=0.95")\n    print("[METRIC] f1=0.5")\n')
    assert result["passed"] is False
    with pytest.raises(engine.EngineError, match="not met"):
        engine.advance(rid, "done", ctx=ctx)


async def test_empty_metrics_with_criteria_is_criteria_not_met(engine, ws):
    ctx, rid, result = await _to_decide(engine, ws, eval_text="def test_m():\n    assert True\n")
    assert result["metrics"]["tests_run"] == 1.0 and "accuracy" not in result["metrics"]
    with pytest.raises(engine.EngineError) as exc:
        engine.advance(rid, "done", ctx=ctx)
    assert "no eval result recorded" not in str(exc.value)


# -- iteration cap -------------------------------------------------------------


async def test_max_iterations_ends_run_and_clears_pointer(engine, ws):
    ctx, rid, _ = await _to_decide(engine, ws, max_iterations=2,
                                   eval_text='def test_m():\n    print("[METRIC] accuracy=0.5")\n')
    engine.advance(rid, "plan", ctx=ctx)  # iteration 1
    walk(engine, ctx, rid, "work", "eval")
    await engine.run_eval(rid, ctx=ctx)
    walk(engine, ctx, rid, "journal", "decide")
    with pytest.raises(engine.EngineError, match="Max iterations"):
        engine.advance(rid, "plan", ctx=ctx)  # iteration 2 == cap
    assert not (ws / "state" / "sess-1.json").exists()
    st = engine.status(rid, ctx=ctx)
    assert st["active"] is False and st["exit_reason"] == "max_iterations"
    run = json.loads((ws / "store/exp-a/runs/run-001/run.json").read_text())
    assert run["outcome"] == "max_iterations"
    with pytest.raises(engine.EngineError, match="not active"):
        engine.advance(rid, "work", ctx=ctx)


# -- run_eval ------------------------------------------------------------------


async def test_run_eval_requires_eval_phase_and_records_history(engine, ws):
    ctx, exp, r = started(engine, ws)
    rid = r["run_id"]
    with pytest.raises(engine.EngineError, match="phase 'eval'"):
        await engine.run_eval(rid, ctx=ctx)
    walk(engine, ctx, rid, "plan", "work", "eval")
    result = await engine.run_eval(rid, ctx=ctx)
    assert result["metrics"]["accuracy"] == 0.95 and result["eval_index"] == 1
    assert result["return_code"] == 0 and result["timed_out"] is False
    run_dir = ws / "store/exp-a/runs/run-001"
    assert (run_dir / "evals" / "001.json").exists() and (run_dir / "evals" / "001.out").exists()
    st = engine.status(rid, ctx=ctx)
    assert st["last_eval_metrics"]["accuracy"] == 0.95 and st["best_metrics"]["accuracy"] == 0.95


async def test_run_eval_refuses_when_protected_tree_changed(engine, ws):
    ctx, exp, r = started(engine, ws)
    rid = r["run_id"]
    walk(engine, ctx, rid, "plan", "work", "eval")
    (exp / "eval" / "test_metric.py").write_text('def test_m():\n    print("[METRIC] accuracy=1.0")\n')
    with pytest.raises(engine.EngineError, match="protected files changed"):
        await engine.run_eval(rid, ctx=ctx)


async def test_run_eval_best_metrics_lower_is_better(engine, ws):
    ctx, exp, r = started(
        engine, ws, criteria=[{"metric": "loss", "threshold": 0.1, "comparison": "<=", "primary": True}],
        eval_text='def test_m():\n    print("[METRIC] loss=0.5")\n')
    rid = r["run_id"]
    walk(engine, ctx, rid, "plan", "work", "eval")
    await engine.run_eval(rid, ctx=ctx)
    walk(engine, ctx, rid, "journal", "decide", "plan", "work", "eval")
    (exp / "workspace").mkdir()
    # eval output changes come from the workspace, not eval/: simulate by env
    st = engine.status(rid, ctx=ctx)
    assert st["best_metrics"]["loss"] == 0.5


async def test_run_eval_crash_with_empty_criteria_is_not_passed(engine, ws):
    ctx, exp, r = started(engine, ws, criteria=[], eval_text="def test_m():\n    assert False\n")
    rid = r["run_id"]
    walk(engine, ctx, rid, "plan", "work", "eval")
    result = await engine.run_eval(rid, ctx=ctx)
    assert result["passed"] is False and result["return_code"] != 0


async def test_run_eval_timeout_clamped_and_reported(engine, ws):
    ctx, exp, r = started(engine, ws, goal_extra={"eval_timeout_s": 99999})
    rid = r["run_id"]
    walk(engine, ctx, rid, "plan", "work", "eval")
    result = await engine.run_eval(rid, ctx=ctx)
    assert result["timeout_s"] == engine.TOOL_TIMEOUT_S - 60 and result["timeout_clamped"] is True


# -- ownership, resume, end, status --------------------------------------------


def test_mutating_tools_require_the_sessions_pointer(engine, ws):
    ctx, exp, r = started(engine, ws)
    other = ctx_for(engine, ws, session="sess-2")
    with pytest.raises(engine.EngineError, match="experiment_resume"):
        engine.advance(r["run_id"], "plan", ctx=other)


def test_resume_reattaches_another_session(engine, ws):
    ctx, exp, r = started(engine, ws)
    engine.advance(r["run_id"], "plan", ctx=ctx)
    other = ctx_for(engine, ws, session="sess-2")
    st = engine.resume(r["run_id"], ctx=other)
    assert st["phase"] == "plan"
    assert (ws / "state" / "sess-2.json").exists()
    assert engine.advance(r["run_id"], "work", ctx=other)["phase"] == "work"


def test_end_by_id_from_any_session_clears_every_pointer(engine, ws):
    ctx, exp, r = started(engine, ws)
    other = ctx_for(engine, ws, session="sess-2")
    engine.resume(r["run_id"], ctx=other)
    st = engine.end(r["run_id"], "user_stopped", ctx=ctx_for(engine, ws, session="sess-3"), note="bye")
    assert st["active"] is False and st["exit_reason"] == "user_stopped" and st["exit_note"] == "bye"
    assert not list((ws / "state").glob("*.json"))
    with pytest.raises(engine.EngineError, match="already ended"):
        engine.end(r["run_id"], "user_stopped", ctx=ctx)
    with pytest.raises(engine.EngineError, match="outcome"):
        engine.end(r["run_id"], "success", ctx=ctx)


def test_status_without_run_id_resolves_the_pointer(engine, ws):
    ctx, exp, r = started(engine, ws)
    assert engine.status(None, ctx=ctx)["run_id"] == r["run_id"]
    assert engine.status(None, ctx=ctx)["allowed_transitions"] == ["plan"]
    with pytest.raises(engine.EngineError, match="no open run"):
        engine.status(None, ctx=ctx_for(engine, ws, session="sess-9"))


def test_start_refuses_when_another_session_holds_the_directory(engine, ws):
    ctx, exp, r = started(engine, ws)
    other = ctx_for(engine, ws, session="sess-2")
    with pytest.raises(engine.EngineError, match="sess-1"):
        engine.start(str(exp), ctx=other)


def test_context_requires_store_root(engine, ws):
    with pytest.raises(engine.EngineError, match="hook"):
        engine.Context(store_root="", session_id="s", state_dir=str(ws / "state"))


def test_assert_loop_phase_and_loop_run_open(engine, ws):
    ctx, exp, r = started(engine, ws)
    rid = r["run_id"]
    assert engine.loop_run_open(rid, ctx=ctx) is True
    with pytest.raises(engine.EngineError, match="journal"):
        engine.assert_loop_phase(rid, "journal", ctx=ctx)
    raw = ctx.store.start_run("exp-a", "raw run")  # no state.json: not a loop run
    assert engine.loop_run_open(raw, ctx=ctx) is False
    engine.assert_loop_phase(raw, "journal", ctx=ctx)  # no-op for raw runs


def test_start_marks_run_aborted_when_state_write_fails(engine, ws, monkeypatch):
    ctx = ctx_for(engine, ws)
    exp = make_experiment(ws)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(engine.Context, "write_state", boom)
    with pytest.raises(OSError):
        engine.start(str(exp), ctx=ctx)
    run = json.loads((ws / "store/exp-a/runs/run-001/run.json").read_text())
    assert run["outcome"] == "aborted"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_experiment_engine.py -v`
Expected: FAIL (`engine.py` not found).

- [ ] **Step 4: Write the engine**

```python
# plugins/experiment/engine.py
"""The experiment loop engine: phases, transitions, gates, state.json, pointers.

Ported from experiment_workflow.py in the parked plugin; every DaemonClient
call became a read or atomic write of the run directory. Two processes rely on
this module's files: the MCP server (which calls these functions) and the
in-process dispatch hook (which reads state.json and the pointers).
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Optional


def _load_sibling(name: str):
    spec = importlib.util.spec_from_file_location(
        f"harness_plugin_experiment_{name}", Path(__file__).parent / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store_mod = _load_sibling("store")
evals = _load_sibling("evals")
policy = _load_sibling("policy")

PHASES = policy.PHASES
TRANSITIONS = policy.TRANSITIONS
DEFAULT_MAX_ITERATIONS = 10
TOOL_TIMEOUT_S = 3600  # must match plugin.toml [mcp.servers.experiment].tool_timeout_s
TIMEOUT_MARGIN_S = 60
END_OUTCOMES = ("user_stopped", "escalation")
PRIOR_RUNS_LIMIT = 5
PRIOR_OBS_LIMIT = 20
SUMMARY_FIELD_CHARS = 200


class EngineError(ValueError):
    """A refusal. The message teaches: what failed, why, what to do instead."""


class Context:
    """Where this call's store, pointers and owning session live."""

    def __init__(self, *, store_root: str, session_id: str, state_dir: str) -> None:
        if not store_root or not state_dir:
            raise EngineError(
                "experiment tools require the harness experiment plugin hook"
                " (store_root/state_dir were not injected)"
            )
        self.store_root = Path(store_root)
        self.session_id = session_id or ""
        self.state_dir = Path(state_dir)
        self.store = store_mod.LocalFsExperimentStore(self.store_root)

    # -- pointers -------------------------------------------------------------

    def pointer_path(self, session_id: str | None = None) -> Path:
        return self.state_dir / f"{session_id or self.session_id}.json"

    def read_pointer(self) -> Optional[dict]:
        path = self.pointer_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def write_pointer(self, run_id: str, experiment_dir: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        store_mod.atomic_write_text(
            self.pointer_path(),
            json.dumps(
                {
                    "run_id": run_id,
                    "run_dir": str(self.store.run_dir(run_id)),
                    "experiment_dir": experiment_dir,
                    "store_root": str(self.store_root),
                    "session_id": self.session_id,
                }
            ),
        )

    def all_pointers(self) -> list[dict]:
        out = []
        if not self.state_dir.exists():
            return out
        for path in sorted(self.state_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            data["_path"] = str(path)
            out.append(data)
        return out

    def clear_pointers_for(self, run_id: str) -> None:
        for p in self.all_pointers():
            if p.get("run_id") == run_id:
                Path(p["_path"]).unlink(missing_ok=True)

    # -- state ----------------------------------------------------------------

    def state_path(self, run_id: str) -> Path:
        return self.store.run_dir(run_id) / "state.json"

    def read_state(self, run_id: str) -> dict:
        path = self.state_path(run_id)
        if not path.exists():
            raise EngineError(
                f"run {run_id} has no loop state: it is not a loop run (or was created by the"
                " raw writer). Use experiment_start to begin a loop run."
            )
        return json.loads(path.read_text())

    def write_state(self, run_id: str, state: dict) -> None:
        store_mod.atomic_write_text(self.state_path(run_id), json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _status_dict(run_id: str, state: dict) -> dict:
    phase = state["phase"]
    return {
        "run_id": run_id,
        "experiment": state["experiment"],
        "experiment_dir": state["experiment_dir"],
        "phase": phase,
        "iteration": state["iteration"],
        "max_iterations": state["max_iterations"],
        "active": state["active"],
        "exit_reason": state["exit_reason"],
        "exit_note": state.get("exit_note", ""),
        "allowed_transitions": list(TRANSITIONS.get(phase, ())) if state["active"] else [],
        "best_metrics": state["best_metrics"],
        "last_eval_metrics": state["last_eval_metrics"],
        "last_eval_passed": state["last_eval_passed"],
        "eval_recorded_this_visit": state["eval_recorded_this_visit"],
        "hypotheses_tested": state["hypotheses_tested"],
        "constraints": state["constraints"],
        "success_criteria": state["success_criteria"],
    }


def _require_owned(ctx: Context, run_id: str) -> None:
    pointer = ctx.read_pointer()
    if pointer is None or pointer.get("run_id") != run_id:
        held = f" (this session's open run is {pointer['run_id']})" if pointer else ""
        raise EngineError(
            f"run_id {run_id} is not this session's open run{held}; call"
            f" experiment_resume('{run_id}') first, or experiment_status() to see the open run"
        )


def _check_phase_name(phase: str) -> None:
    if phase not in PHASES:
        raise EngineError(f"unknown phase {phase!r}; phases are {', '.join(PHASES)}")


def _end_state(ctx: Context, run_id: str, state: dict, outcome: str, note: str = "") -> None:
    state["active"] = False
    state["exit_reason"] = outcome
    state["exit_note"] = note
    state["ended_at"] = store_mod.now_iso()
    ctx.write_state(run_id, state)
    ctx.store.end_run(run_id, outcome, state["best_metrics"])
    ctx.clear_pointers_for(run_id)


def _clip(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= SUMMARY_FIELD_CHARS else text[:SUMMARY_FIELD_CHARS] + "..."


def _prior_runs_summary(ctx: Context, experiment: str) -> dict:
    runs = ctx.store.list_runs(experiment)
    lines, total = [], 0
    for run in runs[-PRIOR_RUNS_LIMIT:]:
        for obs in ctx.store.observations(run.run_id):
            total += 1
            if len(lines) < PRIOR_OBS_LIMIT:
                lines.append(
                    f"{run.run_id} [{run.outcome or 'open'}] #{obs.number} {_clip(obs.title)}:"
                    f" {_clip(obs.result)} | next: {_clip(obs.next_direction)}"
                )
    return {
        "runs": len(runs),
        "shown": len(lines),
        "total_observations": total,
        "note": (f"showing {len(lines)} of {total} observations from the last"
                 f" {min(len(runs), PRIOR_RUNS_LIMIT)} of {len(runs)} runs;"
                 " use experiment_observations(run_id) for the rest"),
        "lines": lines,
    }


def _update_best(state: dict, metrics: dict) -> None:
    lower = {c["metric"] for c in state["success_criteria"] if c.get("comparison") in ("<=", "<")}
    best = state["best_metrics"]
    for k, v in metrics.items():
        if not isinstance(v, (int, float)):
            continue
        if k not in best:
            best[k] = v
        elif k in lower:
            best[k] = min(best[k], v)
        else:
            best[k] = max(best[k], v)


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


def start(experiment_dir: str, *, ctx: Context, max_iterations: int = DEFAULT_MAX_ITERATIONS,
          store_root_note: str = "") -> dict:
    if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or max_iterations < 1:
        raise EngineError(f"max_iterations must be an integer >= 1, got {max_iterations!r}")
    exp_dir = Path(experiment_dir).expanduser().resolve()
    if not exp_dir.is_dir():
        raise EngineError(f"experiment_dir {exp_dir} is not a directory")
    if not (exp_dir / "goal.yaml").exists():
        raise EngineError(f"{exp_dir} has no goal.yaml; an experiment directory needs one")
    cwd = Path.cwd().resolve()
    if not exp_dir.is_relative_to(cwd):
        raise EngineError(
            f"experiment_dir {exp_dir} lies outside the directory harness was launched from"
            f" ({cwd}); file tools are confined to it. Launch harness from a directory that"
            " contains the experiment (and any integration target)."
        )
    experiment = store_mod.validate_experiment_name(exp_dir.name)
    try:
        goal = evals.load_goal(exp_dir)
        criteria = evals.normalize_criteria(goal.success_criteria)
        environment = evals.validate_environment(goal.environment)
    except (ValueError, FileNotFoundError) as exc:
        raise EngineError(f"{exp_dir / 'goal.yaml'}: {exc}") from exc
    if goal.target is not None:
        target = (cwd / str(goal.target)).resolve()
        if not target.is_relative_to(cwd):
            raise EngineError(
                f"goal.yaml target {goal.target} resolves outside {cwd}; launch harness from a"
                " directory containing both the experiment and the target checkout"
            )
    constraints = evals.load_constraints(exp_dir)
    for run in ctx.store.list_runs(experiment):
        path = ctx.state_path(run.run_id)
        if path.exists():
            try:
                st = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if st.get("active"):
                raise EngineError(
                    f"run {run.run_id} is still open (phase {st.get('phase')}, session"
                    f" {st.get('session_id')}); resume it with experiment_resume('{run.run_id}')"
                    f" or close it with experiment_end('{run.run_id}', 'user_stopped')"
                )
    for p in ctx.all_pointers():
        if p.get("experiment_dir") == str(exp_dir) and p.get("session_id") != ctx.session_id:
            raise EngineError(
                f"session {p.get('session_id')} holds an open run ({p.get('run_id')}) on"
                f" {exp_dir}; end it with experiment_end('{p.get('run_id')}', 'user_stopped')"
                " or wait for it"
            )
    spec = evals.resolve_eval_spec(exp_dir, goal)
    roots = evals.protected_roots(exp_dir, spec)
    digest = evals.digest_protected(roots)
    interpreter = evals.select_interpreter(exp_dir, goal)
    timeout_s = goal.eval_timeout_s or evals.DEFAULT_EVAL_TIMEOUT_S
    run_id = ctx.store.start_run(experiment, goal.objective)
    state = {
        "phase": "read",
        "iteration": 0,
        "max_iterations": max_iterations,
        "active": True,
        "exit_reason": None,
        "exit_note": "",
        "experiment_dir": str(exp_dir),
        "experiment": experiment,
        "task": goal.objective,
        "success_criteria": criteria,
        "constraints": constraints.as_dict(),
        "eval": spec,
        "eval_python": interpreter,
        "environment": environment,
        "eval_timeout_s": int(timeout_s),
        "protected_roots": [str(r) for r in roots],
        "protected_digest": digest,
        "hypotheses_tested": [],
        "best_metrics": {},
        "last_eval_metrics": None,
        "last_eval_passed": None,
        "last_eval_index": 0,
        "eval_recorded_this_visit": False,
        "eval_in_flight": None,
        "session_id": ctx.session_id,
        "started_at": store_mod.now_iso(),
        "ended_at": None,
    }
    try:
        ctx.write_state(run_id, state)
        ctx.write_pointer(run_id, str(exp_dir))
    except Exception:
        ctx.store.end_run(run_id, "aborted", {})
        raise
    result = _status_dict(run_id, state)
    result.update(
        {
            "store_root": str(ctx.store_root),
            "store_root_note": store_root_note,
            "objective": goal.objective,
            "context": goal.context,
            "target": goal.target,
            "eval": spec,
            "eval_python": interpreter,
            "protected": [str(r) for r in roots],
            "prior_runs": _prior_runs_summary(ctx, experiment),
        }
    )
    return result


def resume(run_id: str, *, ctx: Context) -> dict:
    state = ctx.read_state(run_id)
    if not state["active"]:
        raise EngineError(f"run {run_id} is not active (exit_reason={state['exit_reason']})")
    ctx.write_pointer(run_id, state["experiment_dir"])
    state.setdefault("resumed_by", []).append(ctx.session_id)
    ctx.write_state(run_id, state)
    return _status_dict(run_id, state)


def status(run_id: Optional[str], *, ctx: Context) -> dict:
    if run_id is None:
        pointer = ctx.read_pointer()
        if pointer is None:
            raise EngineError(
                "no open run for this session; call experiment_start(experiment_dir) or"
                " experiment_resume(run_id)"
            )
        run_id = pointer["run_id"]
    return _status_dict(run_id, ctx.read_state(run_id))


def advance(run_id: str, phase: str, *, ctx: Context, hypothesis: Optional[str] = None) -> dict:
    _check_phase_name(phase)
    state = ctx.read_state(run_id)
    if not state["active"]:
        raise EngineError(f"run {run_id} is not active (exit_reason={state['exit_reason']})")
    _require_owned(ctx, run_id)
    current = state["phase"]
    allowed = TRANSITIONS.get(current, ())
    if phase not in allowed:
        raise EngineError(
            f"cannot advance {current}->{phase}; allowed from {current}:"
            f" {', '.join(allowed) or '(none)'}"
        )
    if current == "eval" and phase == "journal" and not state["eval_recorded_this_visit"]:
        raise EngineError(
            "eval->journal refused: no eval recorded in this visit; call experiment_run_eval first"
        )
    if current == "decide" and phase == "plan":
        state["iteration"] += 1
        if state["iteration"] >= state["max_iterations"]:
            state["phase"] = "plan"
            _end_state(ctx, run_id, state, "max_iterations")
            raise EngineError(
                f"Max iterations ({state['max_iterations']}) reached; run {run_id} ended with"
                " outcome max_iterations"
            )
    if current == "decide" and phase == "done":
        criteria = state["success_criteria"]
        if [c for c in criteria if not c.get("report")]:
            recorded = state["last_eval_metrics"]
            if recorded is None:
                raise EngineError(
                    "decide->done blocked: success criteria not met (no eval result recorded);"
                    " go through work->eval and call experiment_run_eval"
                )
            if not evals.criteria_pass(criteria, recorded):
                details = evals.check_criteria(criteria, recorded).details
                raise EngineError(
                    "decide->done blocked: success criteria not met (recomputed from recorded"
                    f" metrics: {details})"
                )
    if phase == "work" and hypothesis:
        state["hypotheses_tested"].append(
            {"hypothesis": str(hypothesis), "iteration": state["iteration"]}
        )
    if phase == "eval":
        state["eval_recorded_this_visit"] = False
    state["phase"] = phase
    if phase == "done":
        _end_state(ctx, run_id, state, "success")
    else:
        ctx.write_state(run_id, state)
    return _status_dict(run_id, state)


async def run_eval(run_id: str, *, ctx: Context) -> dict:
    state = ctx.read_state(run_id)
    if not state["active"]:
        raise EngineError(f"run {run_id} is not active (exit_reason={state['exit_reason']})")
    _require_owned(ctx, run_id)
    if state["phase"] != "eval":
        raise EngineError(
            f"experiment_run_eval requires phase 'eval', the run is in '{state['phase']}';"
            " advance to eval first"
        )
    if state.get("eval_in_flight"):
        raise EngineError(
            f"eval already running (started {state['eval_in_flight'].get('started_at')});"
            " wait for it to finish"
        )
    roots = [Path(r) for r in state["protected_roots"]]
    changed = evals.changed_protected(roots, state["protected_digest"])
    if changed:
        raise EngineError(
            f"protected files changed since start: {', '.join(changed)}; restore them"
            " (goal.yaml, constraints.yaml, eval/ and fixtures/ are immutable during a run)"
        )
    timeout = int(state["eval_timeout_s"])
    clamped = timeout > TOOL_TIMEOUT_S - TIMEOUT_MARGIN_S
    if clamped:
        timeout = TOOL_TIMEOUT_S - TIMEOUT_MARGIN_S
    state["eval_in_flight"] = {"started_at": store_mod.now_iso()}
    ctx.write_state(run_id, state)
    try:
        result = await evals.run_eval(
            Path(state["experiment_dir"]), state["eval"], interpreter=state["eval_python"],
            env={**os.environ, **state["environment"]}, timeout=timeout,
        )
    finally:
        state["eval_in_flight"] = None
        ctx.write_state(run_id, state)
    metrics = evals.derive_metrics(result)
    criteria = state["success_criteria"]
    gating = [c for c in criteria if not c.get("report")]
    if gating:
        passed = evals.criteria_pass(criteria, metrics)
    else:
        passed = bool(result.return_code == 0 and not result.timed_out)
    details = evals.check_criteria(criteria, metrics).details
    _update_best(state, metrics)
    index = state["last_eval_index"] + 1
    record = {
        "index": index,
        "metrics": metrics,
        "passed": passed,
        "timed_out": result.timed_out,
        "return_code": result.return_code,
        "command": evals.build_command(state["eval"], state["eval_python"]),
        "interpreter": state["eval_python"],
        "timeout_s": timeout,
        "timeout_clamped": clamped,
        "digest": state["protected_digest"],
        "recorded_at": store_mod.now_iso(),
        "tests_run": result.tests_run,
        "tests_passed": result.tests_passed,
        "tests_failed": result.tests_failed,
    }
    evals_dir = ctx.store.run_dir(run_id) / "evals"
    evals_dir.mkdir(exist_ok=True)
    store_mod.atomic_write_text(evals_dir / f"{index:03d}.json", json.dumps(record, indent=2))
    store_mod.atomic_write_text(
        evals_dir / f"{index:03d}.out", f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    state["last_eval_metrics"] = metrics
    state["last_eval_passed"] = passed
    state["last_eval_index"] = index
    state["eval_recorded_this_visit"] = True
    ctx.write_state(run_id, state)
    out = dict(record)
    out.pop("digest")
    out.update(
        {
            "eval_index": index,
            "criteria_details": details,
            "stdout_tail": evals.bounded_tail(result.stdout),
            "stderr_tail": evals.bounded_tail(result.stderr),
            "best_metrics": state["best_metrics"],
        }
    )
    return out


def end(run_id: str, outcome: str, *, ctx: Context, note: str = "") -> dict:
    if outcome not in END_OUTCOMES:
        raise EngineError(f"outcome must be one of {', '.join(END_OUTCOMES)}, got {outcome!r}")
    state = ctx.read_state(run_id)
    if not state["active"]:
        raise EngineError(f"run {run_id} already ended (exit_reason={state['exit_reason']})")
    _end_state(ctx, run_id, state, outcome, note=str(note or ""))
    return _status_dict(run_id, state)


def loop_run_open(run_id: str, *, ctx: Context) -> bool:
    path = ctx.state_path(run_id)
    if not path.exists():
        return False
    try:
        return bool(json.loads(path.read_text()).get("active"))
    except (OSError, ValueError):
        return False


def assert_loop_phase(run_id: str, phase: str, *, ctx: Context) -> None:
    """Server-side floor for tools the policy confines to one phase."""
    path = ctx.state_path(run_id)
    if not path.exists():
        return
    state = json.loads(path.read_text())
    if state.get("active") and state.get("phase") != phase:
        raise EngineError(
            f"this tool is allowed only in the {phase} phase; run {run_id} is in"
            f" {state.get('phase')}"
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_experiment_engine.py -v`
Expected: PASS. (The `test_run_eval_best_metrics_lower_is_better` test exercises `_update_best` via the first eval only; that is intended.)

- [ ] **Step 6: Commit**

```bash
git add plugins/experiment/engine.py plugins/experiment/policy.py tests/test_experiment_engine.py
git commit -m "feat(experiment): the loop engine with state.json, pointers, gates and eval records"
```

---

### Task 5: The phase policy as validated data

**Files:**
- Modify: `plugins/experiment/policy.py` (replace the Task 4 version)
- Test: `tests/test_experiment_policy_hooks.py` (policy half; the hook half is added in Task 6)

**Interfaces:**
- Consumes: nothing.
- Produces: `PHASES`, `TRANSITIONS`, `EXPERIMENT_TOOL_PREFIX = "mcp__experiment__"`, `START_TOOL`, `RUN_EVAL_TOOL`, `RECORD_OBS_TOOL`, `PATH_TOOLS = ("write_file", "edit_file")`, `DISCIPLINE_DENY: dict[str, frozenset[str]]`, `LIFECYCLE_DENY: frozenset[str]`, `decide(phase, tool) -> "allow" | "ask" | "block"`, `is_protected(path: Path, roots: list[Path]) -> bool`. Import-time validation raises `RuntimeError` on an inconsistent table.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_experiment_policy_hooks.py
"""Phase policy (deny-only, Ask for discipline, Block for lifecycle) and the phase gate hook."""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).parent.parent / "plugins" / "experiment"


def load_plugin_module(name: str):
    spec = importlib.util.spec_from_file_location(
        f"experiment_plugin_test_{name}", PLUGIN_ROOT / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def policy():
    return load_plugin_module("policy")


# -- policy --------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["read", "plan", "decide"])
def test_read_plan_decide_ask_for_writes_bash_and_loop_tools(policy, phase):
    for tool in ("write_file", "edit_file", "bash", policy.RUN_EVAL_TOOL, policy.RECORD_OBS_TOOL):
        assert policy.decide(phase, tool) == "ask"
    assert policy.decide(phase, "read_file") == "allow"
    assert policy.decide(phase, "mcp__experiment__experiment_advance") == "allow"


def test_work_allows_bash_and_writes_but_not_eval_or_journal_tools(policy):
    assert policy.decide("work", "bash") == "allow"
    assert policy.decide("work", "write_file") == "allow"
    assert policy.decide("work", policy.RUN_EVAL_TOOL) == "ask"
    assert policy.decide("work", policy.RECORD_OBS_TOOL) == "ask"


def test_eval_and_journal_rows(policy):
    assert policy.decide("eval", "bash") == "ask"
    assert policy.decide("eval", policy.RUN_EVAL_TOOL) == "allow"
    assert policy.decide("eval", policy.RECORD_OBS_TOOL) == "ask"
    assert policy.decide("journal", "write_file") == "ask"
    assert policy.decide("journal", policy.RECORD_OBS_TOOL) == "allow"
    assert policy.decide("journal", policy.RUN_EVAL_TOOL) == "ask"


def test_lifecycle_tools_are_blocked_in_every_phase(policy):
    for phase in policy.PHASES:
        assert policy.decide(phase, "mcp__experiment__experiment_start_run") == "block"
        assert policy.decide(phase, "mcp__experiment__experiment_end_run") == "block"


def test_unknown_tools_fall_through_to_allow(policy):
    assert policy.decide("read", "mcp__serena__replace_content") == "allow"
    assert policy.decide("read", "dispatch_agent") == "allow"


def test_is_protected(policy, tmp_path):
    roots = [tmp_path / "eval", tmp_path / "goal.yaml"]
    assert policy.is_protected(tmp_path / "eval" / "x.py", roots)
    assert policy.is_protected(tmp_path / "goal.yaml", roots)
    assert not policy.is_protected(tmp_path / "workspace" / "x.py", roots)
    assert not policy.is_protected(tmp_path / "evaluate.py", roots)


def test_transitions_match_the_parked_table(policy):
    assert policy.TRANSITIONS == {
        "read": ("plan",), "plan": ("work",), "work": ("eval",), "eval": ("journal", "work"),
        "journal": ("decide",), "decide": ("plan", "done"), "done": (),
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_experiment_policy_hooks.py -v`
Expected: FAIL (`decide` not defined).

- [ ] **Step 3: Write the policy**

```python
# plugins/experiment/policy.py
"""Phase policy for the experiment plugin: deny-only, validated at import.

Two kinds of deny: DISCIPLINE (per phase) becomes an `Ask`, so a human in the
TUI can approve a one-off out-of-phase action and a headless run denies it;
LIFECYCLE (the raw writer tools while a loop run is open) becomes a `Block`.
The plugin never grants anything; harness's permission engine still runs
after the gate and its denies stay absolute.
"""

from __future__ import annotations

from pathlib import Path

PHASES = ("read", "plan", "work", "eval", "journal", "decide", "done")
TRANSITIONS = {
    "read": ("plan",),
    "plan": ("work",),
    "work": ("eval",),
    "eval": ("journal", "work"),
    "journal": ("decide",),
    "decide": ("plan", "done"),
    "done": (),
}

EXPERIMENT_TOOL_PREFIX = "mcp__experiment__"
START_TOOL = "mcp__experiment__experiment_start"
RUN_EVAL_TOOL = "mcp__experiment__experiment_run_eval"
RECORD_OBS_TOOL = "mcp__experiment__experiment_record_observation"
PATH_TOOLS = ("write_file", "edit_file")

_WRITES = ("write_file", "edit_file", "bash")
_READ_LIKE = frozenset({*_WRITES, RUN_EVAL_TOOL, RECORD_OBS_TOOL})
DISCIPLINE_DENY: dict[str, frozenset[str]] = {
    "read": _READ_LIKE,
    "plan": _READ_LIKE,
    "work": frozenset({RUN_EVAL_TOOL, RECORD_OBS_TOOL}),
    "eval": frozenset({*_WRITES, RECORD_OBS_TOOL}),
    "journal": frozenset({*_WRITES, RUN_EVAL_TOOL}),
    "decide": _READ_LIKE,
    "done": frozenset(),
}
LIFECYCLE_DENY = frozenset(
    {"mcp__experiment__experiment_start_run", "mcp__experiment__experiment_end_run"}
)


def decide(phase: str, tool: str) -> str:
    """'block' for lifecycle tools, 'ask' for phase discipline, else 'allow'."""
    if tool in LIFECYCLE_DENY:
        return "block"
    if tool in DISCIPLINE_DENY.get(phase, frozenset()):
        return "ask"
    return "allow"


def is_protected(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _validate() -> None:
    if set(TRANSITIONS) != set(PHASES):
        raise RuntimeError("experiment policy: TRANSITIONS must cover every phase")
    for phase, targets in TRANSITIONS.items():
        for t in targets:
            if t not in PHASES:
                raise RuntimeError(f"experiment policy: {phase}->{t} names an unknown phase")
    if set(DISCIPLINE_DENY) != set(PHASES):
        raise RuntimeError("experiment policy: DISCIPLINE_DENY must cover every phase")
    for phase, tools in DISCIPLINE_DENY.items():
        if not all(isinstance(t, str) and t for t in tools):
            raise RuntimeError(f"experiment policy: bad tool name in phase {phase}")
        if tools & LIFECYCLE_DENY:
            raise RuntimeError(f"experiment policy: lifecycle tools listed under {phase}")


_validate()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_experiment_policy_hooks.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/experiment/policy.py tests/test_experiment_policy_hooks.py
git commit -m "feat(experiment): deny-only phase policy validated at import"
```

---

### Task 6: The phase gate hook and session capture

**Files:**
- Modify: `plugins/experiment/hooks.py` (replace the Task 1 stub)
- Test: `tests/test_experiment_policy_hooks.py` (append the hook half)

**Interfaces:**
- Consumes: `policy.py` (Task 5); `engine.py` (Task 4) in tests only, to create real runs.
- Produces: `phase_gate(action) -> Allow | Ask | Block | Rewrite`; `capture_session(ctx) -> []`; `resolve_store_root(env=None) -> (Path | None, reason | None, note)`; `state_dir(env=None) -> Path`; module state `_OWNER = {"session_id": None}`. Injected keys on every non-deny experiment call: `store_root`, `session_id`, `state_dir`; on `experiment_start` also a canonical `experiment_dir` and `store_root_note`. Owner fallback when no SESSION_START was seen: `pid-<pid>`.

- [ ] **Step 1: Append the failing hook tests**

```python
# tests/test_experiment_policy_hooks.py (append)
from harness.hooks import Allow, Ask, Block, ProposedModelCall, ProposedToolCall, Rewrite
from harness.types import CallId, ModelId, ToolName


@pytest.fixture
def hooks():
    return load_plugin_module("hooks")


@pytest.fixture
def engine():
    return load_plugin_module("engine")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("HARNESS_EXPERIMENT_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("HARNESS_EXPERIMENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("MEMORY_VAULT_DIR", raising=False)
    monkeypatch.delenv("HARNESS_EXPERIMENT_PROJECT", raising=False)
    return root


def make_experiment(ws, name="exp-a"):
    exp = ws / name
    (exp / "eval").mkdir(parents=True)
    (exp / "goal.yaml").write_text(yaml.dump({
        "objective": "o", "eval": "eval/",
        "success_criteria": [{"metric": "accuracy", "threshold": 0.9, "primary": True}]}))
    (exp / "eval" / "test_m.py").write_text('def test_m():\n    print("[METRIC] accuracy=1.0")\n')
    return exp


def call(tool, **args):
    return ProposedToolCall(call_id=CallId("c1"), tool=ToolName(tool), args=args)


def open_run(engine, hooks, ws, session="sess-1"):
    hooks.capture_session({"session_id": session})
    ctx = engine.Context(store_root=str(ws.parent / "store"), session_id=session,
                         state_dir=str(ws.parent / "state"))
    exp = make_experiment(ws)
    return ctx, exp, engine.start(str(exp), ctx=ctx)


# -- store root resolution --------------------------------------------------


def test_resolve_store_root_explicit_dir(hooks, tmp_path):
    root, reason, note = hooks.resolve_store_root({"HARNESS_EXPERIMENT_DIR": str(tmp_path / "x")})
    assert root == tmp_path / "x" and reason is None and note == ""


def test_resolve_store_root_vault_default_entity(hooks, tmp_path):
    vault = tmp_path / "vault"
    (vault / "10-projects" / "experiment").mkdir(parents=True)
    root, reason, note = hooks.resolve_store_root({"MEMORY_VAULT_DIR": str(vault)})
    assert root == vault / "10-projects" / "experiment" / "experiments" and reason is None


def test_resolve_store_root_missing_default_entity_falls_through_with_note(hooks, tmp_path):
    root, reason, note = hooks.resolve_store_root({"MEMORY_VAULT_DIR": str(tmp_path / "vault")})
    assert root == hooks.DEFAULT_STORE_ROOT and reason is None and "does not exist" in note


def test_resolve_store_root_explicit_missing_project_blocks(hooks, tmp_path):
    root, reason, note = hooks.resolve_store_root(
        {"MEMORY_VAULT_DIR": str(tmp_path), "HARNESS_EXPERIMENT_PROJECT": "nope"})
    assert root is None and "nope" in reason
    root, reason, note = hooks.resolve_store_root(
        {"MEMORY_VAULT_DIR": str(tmp_path), "HARNESS_EXPERIMENT_PROJECT": "../evil"})
    assert root is None and "invalid" in reason


def test_resolve_store_root_default(hooks):
    root, reason, note = hooks.resolve_store_root({})
    assert root == hooks.DEFAULT_STORE_ROOT and reason is None


# -- owner capture ----------------------------------------------------------


def test_capture_session_keeps_first_owner_while_it_holds_a_pointer(engine, hooks, ws):
    assert hooks.capture_session({"session_id": "sess-1"}) == []
    open_run(engine, hooks, ws, session="sess-1")
    hooks.capture_session({"session_id": "child-1"})
    assert hooks._OWNER["session_id"] == "sess-1"


def test_capture_session_replaces_owner_without_pointer_and_fails_open(hooks, ws):
    hooks.capture_session({"session_id": "a"})
    hooks.capture_session({"session_id": "b"})
    assert hooks._OWNER["session_id"] == "b"
    assert hooks.capture_session(None) == []
    assert hooks.capture_session({"nope": 1}) == []


def test_owner_falls_back_to_pid_when_no_session_start_fired(hooks, ws):
    decision = hooks.phase_gate(call("mcp__experiment__experiment_status"))
    assert isinstance(decision, Rewrite)
    assert decision.action.args["session_id"].startswith("pid-")


# -- dormant ----------------------------------------------------------------


def test_dormant_allows_natives_and_rewrites_experiment_tools(hooks, ws, tmp_path):
    hooks.capture_session({"session_id": "sess-1"})
    assert isinstance(hooks.phase_gate(call("write_file", file_path="x")), Allow)
    assert isinstance(hooks.phase_gate(ProposedModelCall(call_id=CallId("m"), model=ModelId("x"))), Allow)
    d = hooks.phase_gate(call("mcp__experiment__experiment_start", experiment_dir="exp-a"))
    assert isinstance(d, Rewrite)
    assert d.action.call_id == CallId("c1") and str(d.action.tool) == "mcp__experiment__experiment_start"
    assert d.action.args["store_root"] == str(tmp_path / "store")
    assert d.action.args["state_dir"] == str(tmp_path / "state")
    assert d.action.args["session_id"] == "sess-1"
    assert d.action.args["experiment_dir"] == str((ws / "exp-a").resolve())


def test_rewrite_overwrites_model_supplied_keys(hooks, ws, tmp_path):
    hooks.capture_session({"session_id": "sess-1"})
    d = hooks.phase_gate(call("mcp__experiment__experiment_status", store_root="/evil",
                              session_id="other", state_dir="/tmp"))
    assert d.action.args["store_root"] == str(tmp_path / "store")
    assert d.action.args["session_id"] == "sess-1"


def test_blocked_store_root_blocks_experiment_tools_only(hooks, ws, tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_EXPERIMENT_DIR")
    monkeypatch.setenv("MEMORY_VAULT_DIR", str(tmp_path))
    monkeypatch.setenv("HARNESS_EXPERIMENT_PROJECT", "nope")
    hooks.capture_session({"session_id": "sess-1"})
    assert isinstance(hooks.phase_gate(call("mcp__experiment__experiment_status")), Block)
    assert isinstance(hooks.phase_gate(call("write_file", file_path="x")), Allow)


# -- active run -------------------------------------------------------------


def test_active_read_phase_asks_for_writes_and_names_the_run(engine, hooks, ws):
    ctx, exp, r = open_run(engine, hooks, ws)
    d = hooks.phase_gate(call("write_file", file_path="exp-a/workspace/x.py", content="1"))
    assert isinstance(d, Ask)
    assert "exp-a/run-001" in d.reason and "read" in d.reason and "plan" in d.reason
    assert isinstance(hooks.phase_gate(call("bash", command="ls")), Ask)
    assert isinstance(hooks.phase_gate(call("read_file", file_path="exp-a/goal.yaml")), Allow)


def test_protected_tree_is_blocked_in_every_phase_with_resolved_paths(engine, hooks, ws):
    ctx, exp, r = open_run(engine, hooks, ws)
    engine.advance(r["run_id"], "plan", ctx=ctx)
    engine.advance(r["run_id"], "work", ctx=ctx)
    for raw in ("exp-a/eval/test_m.py", str(exp / "eval" / "new.py"), "exp-a/../exp-a/goal.yaml"):
        d = hooks.phase_gate(call("edit_file", file_path=raw, old_string="a", new_string="b"))
        assert isinstance(d, Block), raw
        assert "protected" in d.reason
    assert isinstance(hooks.phase_gate(call("write_file", file_path="exp-a/workspace/x.py", content="1")), Allow)


def test_lifecycle_tools_blocked_while_open(engine, hooks, ws):
    open_run(engine, hooks, ws)
    d = hooks.phase_gate(call("mcp__experiment__experiment_start_run", experiment="x"))
    assert isinstance(d, Block) and "lifecycle" in d.reason


def test_experiment_tools_rewritten_while_open(engine, hooks, ws, tmp_path):
    ctx, exp, r = open_run(engine, hooks, ws)
    d = hooks.phase_gate(call("mcp__experiment__experiment_advance", run_id=r["run_id"], phase="plan"))
    assert isinstance(d, Rewrite) and d.action.args["store_root"] == str(tmp_path / "store")


def test_stale_pointer_is_removed_and_gate_goes_dormant(engine, hooks, ws, tmp_path):
    ctx, exp, r = open_run(engine, hooks, ws)
    engine.end(r["run_id"], "user_stopped", ctx=ctx)
    # simulate a leftover pointer for an ended run
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "sess-1.json").write_text(json.dumps(
        {"run_id": r["run_id"], "run_dir": str(ctx.store.run_dir(r["run_id"])),
         "experiment_dir": str(exp.resolve()), "store_root": str(tmp_path / "store"),
         "session_id": "sess-1"}))
    assert isinstance(hooks.phase_gate(call("write_file", file_path="exp-a/eval/x", content="")), Allow)
    assert not (tmp_path / "state" / "sess-1.json").exists()


def test_unreadable_state_blocks_work_but_passes_experiment_tools(engine, hooks, ws, tmp_path):
    ctx, exp, r = open_run(engine, hooks, ws)
    (ctx.store.run_dir(r["run_id"]) / "state.json").write_text("{not json")
    d = hooks.phase_gate(call("read_file", file_path="exp-a/goal.yaml"))
    assert isinstance(d, Block) and "experiment_end" in d.reason
    assert isinstance(hooks.phase_gate(call("mcp__experiment__experiment_end", run_id=r["run_id"], outcome="user_stopped")), Rewrite)


def test_hook_never_raises_on_odd_args(engine, hooks, ws):
    open_run(engine, hooks, ws)
    assert isinstance(hooks.phase_gate(call("write_file")), Ask)  # no file_path at all
    assert isinstance(hooks.phase_gate(call("write_file", file_path=123, content="")), Ask)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_experiment_policy_hooks.py -v`
Expected: the new tests FAIL (`resolve_store_root` not defined).

- [ ] **Step 3: Write the hooks module**

```python
# plugins/experiment/hooks.py
"""Experiment plugin hooks: the phase gate (dispatch, priority 990) and session capture.

The gate runs in the harness process, so it knows the environment, the cwd and
the owning session. It injects `store_root`, `session_id` and `state_dir` into
every experiment tool call by Rewrite (the MCP server sees only the SDK default
environment), and it enforces the phase policy while a run is open: `Ask` for
phase discipline, `Block` for the protected tree and run lifecycle. With no
open run it is dormant. Imports: stdlib and harness only.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from harness.hooks import Allow, Ask, Block, ProposedToolCall, Rewrite

_spec = importlib.util.spec_from_file_location(
    "harness_plugin_experiment_policy_from_hooks", Path(__file__).parent / "policy.py"
)
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)

DEFAULT_STORE_ROOT = Path.home() / ".local" / "share" / "harness" / "experiments"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "harness" / "experiment" / "active"
DEFAULT_PROJECT = "experiment"
_OWNER: dict = {"session_id": None}


def state_dir(env=None) -> Path:
    env = os.environ if env is None else env
    raw = env.get("HARNESS_EXPERIMENT_STATE_DIR")
    return Path(raw).expanduser() if raw else DEFAULT_STATE_DIR


def _valid_project(name: str) -> bool:
    return bool(name and name.strip()) and "/" not in name and "\\" not in name \
        and name not in (".", "..") and not name.startswith(".")


def resolve_store_root(env=None) -> tuple[Path | None, str | None, str]:
    """(root, block_reason, note). Never raises; a missing default degrades."""
    env = os.environ if env is None else env
    explicit = env.get("HARNESS_EXPERIMENT_DIR")
    if explicit:
        return Path(explicit).expanduser(), None, ""
    vault = env.get("MEMORY_VAULT_DIR")
    if vault:
        project_env = env.get("HARNESS_EXPERIMENT_PROJECT")
        project = project_env or DEFAULT_PROJECT
        if not _valid_project(project):
            return None, f"HARNESS_EXPERIMENT_PROJECT={project!r} is invalid (a plain directory name)", ""
        entity = Path(vault).expanduser() / "10-projects" / project
        if entity.is_dir():
            return entity / "experiments", None, ""
        if project_env:
            return (
                None,
                f"HARNESS_EXPERIMENT_PROJECT={project_env} but {entity} does not exist;"
                " create the vault entity or unset the variable",
                "",
            )
        return (
            DEFAULT_STORE_ROOT,
            None,
            f"vault entity {entity} does not exist; runs are stored under {DEFAULT_STORE_ROOT}",
        )
    return DEFAULT_STORE_ROOT, None, ""


def _owner() -> str:
    return _OWNER["session_id"] or f"pid-{os.getpid()}"


def _pointer_path() -> Path:
    return state_dir() / f"{_owner()}.json"


def capture_session(ctx):
    """SESSION_START: remember the owning session. Fail-open, returns nothing."""
    try:
        sid = ctx.get("session_id") if isinstance(ctx, dict) else None
        if sid and (_OWNER["session_id"] is None or not _pointer_path().exists()):
            _OWNER["session_id"] = str(sid)
    except Exception:
        pass
    return []


def _rewrite(action: ProposedToolCall, root: Path, note: str) -> Rewrite:
    args = dict(action.args)
    args["store_root"] = str(root)
    args["session_id"] = _owner()
    args["state_dir"] = str(state_dir())
    if str(action.tool) == policy.START_TOOL:
        raw = args.get("experiment_dir")
        if raw:
            args["experiment_dir"] = str((Path.cwd() / Path(str(raw)).expanduser()).resolve())
        args["store_root_note"] = note
    return Rewrite(action=ProposedToolCall(call_id=action.call_id, tool=action.tool, args=args))


def phase_gate(action):
    if not isinstance(action, ProposedToolCall):
        return Allow()
    tool = str(action.tool)
    is_exp = tool.startswith(policy.EXPERIMENT_TOOL_PREFIX)
    root = None
    note = ""
    if is_exp:
        root, reason, note = resolve_store_root()
        if root is None:
            return Block(reason=f"experiment tools are unavailable: {reason}")
    pointer_path = _pointer_path()
    if not pointer_path.exists():
        return _rewrite(action, root, note) if is_exp else Allow()
    try:
        pointer = json.loads(pointer_path.read_text())
        state = json.loads((Path(pointer["run_dir"]) / "state.json").read_text())
        run_id = str(pointer["run_id"])
        phase = str(state["phase"])
    except (OSError, ValueError, KeyError, TypeError):
        if is_exp:
            return _rewrite(action, root, note)
        return Block(
            reason="an experiment run is recorded as open for this session but its state is"
            f" unreadable ({pointer_path}); call experiment_status(), experiment_end(run_id,"
            " 'user_stopped') or experiment_start to repair before using other tools"
        )
    if not state.get("active"):
        pointer_path.unlink(missing_ok=True)  # stale: the run ended elsewhere
        return _rewrite(action, root, note) if is_exp else Allow()
    if tool in policy.PATH_TOOLS:
        raw = action.args.get("file_path")
        if isinstance(raw, str) and raw.strip():
            resolved = (Path.cwd() / Path(raw).expanduser()).resolve()
            roots = [Path(r) for r in state.get("protected_roots", [])]
            if policy.is_protected(resolved, roots):
                return Block(
                    reason=f"{resolved} is protected during experiment run {run_id} (goal.yaml,"
                    " constraints.yaml, eval/ and fixtures/ are immutable while a run is open);"
                    f" end the run with experiment_end('{run_id}', 'user_stopped') to change it"
                )
    verdict = policy.decide(phase, tool)
    if verdict == "block":
        return Block(
            reason=f"{tool} is not allowed while experiment run {run_id} is open (the loop owns"
            f" run lifecycle); close it with experiment_end('{run_id}', 'user_stopped') first"
        )
    if verdict == "ask":
        allowed = ", ".join(policy.TRANSITIONS.get(phase, ())) or "(none)"
        return Ask(
            reason=f"experiment run {run_id} is in phase {phase}, which does not allow {tool};"
            f" allowed transitions from {phase}: {allowed}. Approve to override once, or"
            " advance the phase with experiment_advance"
        )
    return _rewrite(action, root, note) if is_exp else Allow()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_experiment_policy_hooks.py tests/test_experiment_plugin.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/experiment/hooks.py tests/test_experiment_policy_hooks.py
git commit -m "feat(experiment): phase gate hook with argument injection and session capture"
```

---

### Task 7: The MCP server

**Files:**
- Modify: `plugins/experiment/server.py` (replace the Task 1 stub)
- Test: `tests/test_experiment_server.py`

**Interfaces:**
- Consumes: `engine.py` (Task 4), which loads `store.py` and `evals.py`.
- Produces: FastMCP instance `mcp` named `experiment` with twelve tools: `experiment_start`, `experiment_resume`, `experiment_status`, `experiment_advance`, `experiment_run_eval` (async), `experiment_end`, `experiment_start_run`, `experiment_record_observation`, `experiment_end_run`, `experiment_list_runs`, `experiment_get_run`, `experiment_observations`. Every tool takes `store_root: str = ""`, `session_id: str = ""`, `state_dir: str = ""` (injected by the hook). Results are JSON strings; refusals raise `ValueError` (FastMCP returns `isError=True`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_experiment_server.py
"""The experiment MCP server over the in-memory transport: tools, refusals as isError, schemas."""

import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
import yaml
from mcp import types
from mcp.shared.memory import create_client_server_memory_streams

from harness.mcp_config import McpServerSpec
from harness.mcp_host import ServerConnection

PLUGIN_ROOT = Path(__file__).parent.parent / "plugins" / "experiment"


def load_plugin_module(name: str):
    spec = importlib.util.spec_from_file_location(
        f"experiment_plugin_test_{name}", PLUGIN_ROOT / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _memory_transport(fastmcp):
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        lowlevel = fastmcp._mcp_server
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: lowlevel.run(
                    server_read, server_write, lowlevel.create_initialization_options(),
                    raise_exceptions=False,
                )
            )
            try:
                yield (client_read, client_write)
            finally:
                tg.cancel_scope.cancel()


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.chdir(root)
    exp = root / "exp-a"
    (exp / "eval").mkdir(parents=True)
    (exp / "goal.yaml").write_text(yaml.dump({
        "objective": "objective X", "eval": "eval/",
        "success_criteria": [{"metric": "accuracy", "threshold": 0.9, "primary": True}]}))
    (exp / "eval" / "test_m.py").write_text('def test_m():\n    print("[METRIC] accuracy=0.95")\n')
    return root


@pytest.fixture
async def conn(ws):
    server_mod = load_plugin_module("server")
    spec = McpServerSpec(name="experiment", transport="stdio", command="unused")
    c = ServerConnection(spec, transport_factory=lambda s: _memory_transport(server_mod.mcp))
    await c.start()
    try:
        yield c
    finally:
        await c.stop()


def injected(ws, **args):
    return {"store_root": str(ws.parent / "store"), "session_id": "sess-1",
            "state_dir": str(ws.parent / "state"), **args}


async def call(conn, name, ws, **args):
    result = await conn.call_tool(name, injected(ws, **args))
    text = "".join(c.text for c in result.content if isinstance(c, types.TextContent))
    return result.isError, text


async def test_tool_surface(conn):
    names = {t.name for t in conn.tools}
    assert names == {
        "experiment_start", "experiment_resume", "experiment_status", "experiment_advance",
        "experiment_run_eval", "experiment_end", "experiment_start_run",
        "experiment_record_observation", "experiment_end_run", "experiment_list_runs",
        "experiment_get_run", "experiment_observations",
    }
    obs_schema = next(t for t in conn.tools if t.name == "experiment_record_observation").inputSchema
    assert "observation" in obs_schema["properties"]


async def test_full_loop_roundtrip(conn, ws):
    err, text = await call(conn, "experiment_start", ws, experiment_dir="exp-a")
    assert not err
    run_id = json.loads(text)["run_id"]
    assert json.loads(text)["store_root"] == str(ws.parent / "store")
    for phase in ("plan", "work", "eval"):
        err, text = await call(conn, "experiment_advance", ws, run_id=run_id, phase=phase,
                               hypothesis="h" if phase == "work" else None)
        assert not err, text
    err, text = await call(conn, "experiment_run_eval", ws, run_id=run_id)
    assert not err and json.loads(text)["metrics"]["accuracy"] == 0.95
    err, text = await call(conn, "experiment_advance", ws, run_id=run_id, phase="journal")
    assert not err
    err, text = await call(conn, "experiment_record_observation", ws, run_id=run_id,
                           observation={"title": "t", "hypothesis": "h", "result": "0.95"})
    assert not err and json.loads(text)["observation_id"] == f"{run_id}#001"
    err, text = await call(conn, "experiment_advance", ws, run_id=run_id, phase="decide")
    assert not err
    err, text = await call(conn, "experiment_advance", ws, run_id=run_id, phase="done")
    assert not err and json.loads(text)["exit_reason"] == "success"
    err, text = await call(conn, "experiment_get_run", ws, run_id=run_id)
    assert not err and json.loads(text)["outcome"] == "success"
    err, text = await call(conn, "experiment_list_runs", ws, experiment="exp-a")
    assert not err and len(json.loads(text)) == 1
    err, text = await call(conn, "experiment_observations", ws, run_id=run_id)
    assert not err and json.loads(text)[0]["title"] == "t"


async def test_refusals_are_iserror_with_teaching_text(conn, ws):
    err, text = await call(conn, "experiment_start", ws, experiment_dir="exp-a")
    run_id = json.loads(text)["run_id"]
    err, text = await call(conn, "experiment_advance", ws, run_id=run_id, phase="eval")
    assert err and "allowed from read" in text
    err, text = await call(conn, "experiment_run_eval", ws, run_id=run_id)
    assert err and "phase 'eval'" in text
    err, text = await call(conn, "experiment_record_observation", ws, run_id=run_id,
                           observation={"title": "t"})
    assert err and "journal" in text
    err, text = await call(conn, "experiment_advance", ws, run_id=run_id, phase="Done")
    assert err
    err, text = await call(conn, "experiment_end", ws, run_id=run_id, outcome="success")
    assert err and "outcome" in text
    err, text = await call(conn, "experiment_get_run", ws, run_id="exp-a/run-999")
    assert err and "no such run" in text


async def test_schema_violations_are_iserror(conn, ws):
    err, text = await call(conn, "experiment_start", ws, experiment_dir="exp-a")
    run_id = json.loads(text)["run_id"]
    err, text = await call(conn, "experiment_record_observation", ws, run_id=run_id,
                           observation={"title": "", "bogus": 1})
    assert err
    err, text = await call(conn, "experiment_start", ws, experiment_dir="exp-a", max_iterations="five")
    assert err


async def test_status_without_run_id_and_resume(conn, ws):
    err, text = await call(conn, "experiment_start", ws, experiment_dir="exp-a")
    run_id = json.loads(text)["run_id"]
    err, text = await call(conn, "experiment_status", ws)
    assert not err and json.loads(text)["run_id"] == run_id
    result = await conn.call_tool("experiment_resume", {**injected(ws, run_id=run_id), "session_id": "sess-2"})
    assert not result.isError


async def test_missing_injection_is_refused(conn, ws):
    result = await conn.call_tool("experiment_status", {})
    assert result.isError
    assert "hook" in "".join(c.text for c in result.content if isinstance(c, types.TextContent))


async def test_raw_writer_tools_work_on_non_loop_runs_and_refuse_open_loop_runs(conn, ws):
    err, text = await call(conn, "experiment_start_run", ws, experiment="raw", goal="g")
    assert not err
    raw_id = json.loads(text)["run_id"]
    err, text = await call(conn, "experiment_record_observation", ws, run_id=raw_id,
                           observation={"title": "any phase"})
    assert not err
    err, text = await call(conn, "experiment_end_run", ws, run_id=raw_id, outcome="done", metrics={"x": 1})
    assert not err
    err, text = await call(conn, "experiment_start", ws, experiment_dir="exp-a")
    loop_id = json.loads(text)["run_id"]
    err, text = await call(conn, "experiment_end_run", ws, run_id=loop_id, outcome="x")
    assert err and "experiment_end" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_experiment_server.py -v`
Expected: FAIL (tool surface is empty).

- [ ] **Step 3: Write the server**

```python
# plugins/experiment/server.py
"""Experiment MCP server: the loop tools plus the frozen (reader, writer) contract.

Runs as a harness child with the MCP SDK default environment, so it reads no
configuration from the environment: `store_root`, `session_id` and `state_dir`
arrive on every call, injected by the plugin's dispatch hook. Refusals are
raised; FastMCP returns them as isError results with the message inline.
Run: python3 server.py (stdio).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

_spec = importlib.util.spec_from_file_location(
    "harness_plugin_experiment_engine", Path(__file__).parent / "engine.py"
)
engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine)
store_mod = engine.store_mod

mcp = FastMCP(
    "experiment",
    instructions=(
        "Experiment loop with engine-run evals. Begin with experiment_start(experiment_dir);"
        " phases are read, plan, work, eval, journal, decide. A phase transition"
        " (experiment_advance) must be the only tool call in its message."
    ),
)


class ObservationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    hypothesis: str = ""
    changes: str = ""
    result: str = ""
    diagnosis: str = ""
    next_direction: str = ""


def _ctx(store_root: str, session_id: str, state_dir: str) -> "engine.Context":
    return engine.Context(store_root=store_root, session_id=session_id, state_dir=state_dir)


def _dump(obj) -> str:
    return json.dumps(obj, default=str)


def _run_dict(run) -> dict:
    return {
        "run_id": run.run_id, "experiment": run.experiment, "goal": run.goal,
        "started_at": run.started_at, "ended_at": run.ended_at, "outcome": run.outcome,
        "metrics": run.metrics,
    }


def _obs_dict(obs) -> dict:
    d = {"run_id": obs.run_id, "number": obs.number}
    for f in store_mod.OBS_FIELDS:
        d[f] = getattr(obs, f)
    return d


# -- loop tools -------------------------------------------------------------------


@mcp.tool()
def experiment_start(
    experiment_dir: str, max_iterations: int = 10, store_root: str = "", session_id: str = "",
    state_dir: str = "", store_root_note: str = "",
) -> str:
    """Start a loop run on an experiment directory (needs goal.yaml). Returns run_id,
    objective, criteria, constraints and a bounded summary of prior runs."""
    ctx = _ctx(store_root, session_id, state_dir)
    return _dump(engine.start(experiment_dir, ctx=ctx, max_iterations=max_iterations,
                              store_root_note=store_root_note))


@mcp.tool()
def experiment_resume(run_id: str, store_root: str = "", session_id: str = "",
                      state_dir: str = "") -> str:
    """Re-attach this session to an open run started elsewhere."""
    return _dump(engine.resume(run_id, ctx=_ctx(store_root, session_id, state_dir)))


@mcp.tool()
def experiment_status(run_id: Optional[str] = None, store_root: str = "", session_id: str = "",
                      state_dir: str = "") -> str:
    """Phase, iteration, metrics and allowed transitions. Omit run_id to find this
    session's open run."""
    return _dump(engine.status(run_id, ctx=_ctx(store_root, session_id, state_dir)))


@mcp.tool()
def experiment_advance(
    run_id: str, phase: str, hypothesis: Optional[str] = None, store_root: str = "",
    session_id: str = "", state_dir: str = "",
) -> str:
    """Move to the next phase (read->plan->work->eval->journal->decide->plan|done).
    Pass hypothesis when entering work. decide->done recomputes the criteria."""
    return _dump(engine.advance(run_id, phase, ctx=_ctx(store_root, session_id, state_dir),
                                hypothesis=hypothesis))


@mcp.tool()
async def experiment_run_eval(run_id: str, store_root: str = "", session_id: str = "",
                              state_dir: str = "") -> str:
    """Run the frozen eval (eval phase only); records metrics and an evals/ entry."""
    return _dump(await engine.run_eval(run_id, ctx=_ctx(store_root, session_id, state_dir)))


@mcp.tool()
def experiment_end(run_id: str, outcome: str, note: str = "", store_root: str = "",
                   session_id: str = "", state_dir: str = "") -> str:
    """End an open run early: outcome is user_stopped or escalation."""
    return _dump(engine.end(run_id, outcome, ctx=_ctx(store_root, session_id, state_dir),
                            note=note))


# -- the frozen (reader, writer) contract -------------------------------------------


@mcp.tool()
def experiment_start_run(experiment: str, goal: str = "", store_root: str = "",
                         session_id: str = "", state_dir: str = "") -> str:
    """Raw writer: begin a run record without the loop. Returns run_id."""
    ctx = _ctx(store_root, session_id, state_dir)
    return _dump({"run_id": ctx.store.start_run(experiment, goal)})


@mcp.tool()
def experiment_record_observation(
    run_id: str, observation: ObservationIn, store_root: str = "", session_id: str = "",
    state_dir: str = "",
) -> str:
    """Append an observation (journal attempt) to a run. Journal phase only for loop runs."""
    ctx = _ctx(store_root, session_id, state_dir)
    engine.assert_loop_phase(run_id, "journal", ctx=ctx)
    obs = store_mod.Observation(**observation.model_dump())
    return _dump({"observation_id": ctx.store.record_observation(run_id, obs)})


@mcp.tool()
def experiment_end_run(run_id: str, outcome: str, metrics: Optional[dict] = None,
                       store_root: str = "", session_id: str = "", state_dir: str = "") -> str:
    """Raw writer: finalize a non-loop run with an outcome and metrics."""
    ctx = _ctx(store_root, session_id, state_dir)
    if engine.loop_run_open(run_id, ctx=ctx):
        raise ValueError(
            f"run {run_id} is an open loop run; close it with experiment_end or"
            " experiment_advance(done), not experiment_end_run"
        )
    ctx.store.end_run(run_id, outcome, metrics or {})
    return _dump({"success": True})


@mcp.tool()
def experiment_list_runs(experiment: str, store_root: str = "", session_id: str = "",
                         state_dir: str = "") -> str:
    """All runs for an experiment, oldest first."""
    ctx = _ctx(store_root, session_id, state_dir)
    return _dump([_run_dict(r) for r in ctx.store.list_runs(experiment)])


@mcp.tool()
def experiment_get_run(run_id: str, store_root: str = "", session_id: str = "",
                       state_dir: str = "") -> str:
    """One run record by id."""
    ctx = _ctx(store_root, session_id, state_dir)
    return _dump(_run_dict(ctx.store.get_run(run_id)))


@mcp.tool()
def experiment_observations(run_id: str, store_root: str = "", session_id: str = "",
                            state_dir: str = "") -> str:
    """A run's observations in order."""
    ctx = _ctx(store_root, session_id, state_dir)
    return _dump([_obs_dict(o) for o in ctx.store.observations(run_id)])


if __name__ == "__main__":
    mcp.run("stdio")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_experiment_server.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/experiment/server.py tests/test_experiment_server.py
git commit -m "feat(experiment): FastMCP server with loop tools and the frozen contract tools"
```

---

### Task 8: Kernel integration and the real subprocess path

**Files:**
- Modify: `tests/test_experiment_plugin.py` (append)
- Create: `tests/test_experiment_subprocess.py`

**Interfaces:**
- Consumes: everything from Tasks 1 to 7; harness `build_kernel`, `run_once`, `FakeProvider`, `tool_call_turn`, `text_turn`, `PermissionEngine`, `RuleSet`, `PermissionRule`, `baseline_ruleset`, `read_session`.
- Produces: nothing new; proves the spec's section 7 and section 11 claims end to end.

The `dispatch_agent` tool's parameters are `prompt` (required), `model` and `agent` (`DispatchAgentTool.__post_init__` in `src/harness/subagent.py`, confirmed 2026-09-01); the subagent test below uses `prompt` and `agent`.

- [ ] **Step 1: Append the failing kernel tests**

```python
# tests/test_experiment_plugin.py (append)
import sys
from dataclasses import replace

import yaml

from harness.events import DispatchResolved, PermissionRequested, SubagentSpawned, ToolCallCompleted, ToolCallProposed
from harness.log import read_session
from harness.native_tools import baseline_ruleset
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.types import ModelId, ToolName

START = ToolName("mcp__experiment__experiment_start")
ADVANCE = ToolName("mcp__experiment__experiment_advance")
STATUS = ToolName("mcp__experiment__experiment_status")
RESUME = ToolName("mcp__experiment__experiment_resume")


def _allow_engine(extra_rules=(), default=None):
    rules = [
        PermissionRule(action="allow", tool="mcp__experiment__*"),
        PermissionRule(action="allow", tool="invoke_skill"),
        PermissionRule(action="allow", tool="write_file"),
        PermissionRule(action="allow", tool="edit_file"),
        PermissionRule(action="allow", tool="read_file"),
        PermissionRule(action="allow", tool="dispatch_agent"),
        *extra_rules,
    ]
    return PermissionEngine([RuleSet(rules=rules, default=default), baseline_ruleset()])


def _workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    exp = ws / "exp-a"
    (exp / "eval").mkdir(parents=True)
    (exp / "workspace").mkdir()
    (exp / "goal.yaml").write_text(yaml.dump({
        "objective": "o", "eval": "eval/",
        "success_criteria": [{"metric": "accuracy", "threshold": 0.9, "primary": True}]}))
    (exp / "eval" / "test_m.py").write_text('def test_m():\n    print("[METRIC] accuracy=1.0")\n')
    monkeypatch.chdir(ws)
    monkeypatch.setenv("HARNESS_EXPERIMENT_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("HARNESS_EXPERIMENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("MEMORY_VAULT_DIR", raising=False)
    return ws


def _kernel(tmp_path, script, engine=None, resume_session_id=None):
    from harness.cli import build_kernel
    from harness.plugins import load_plugins

    loaded = load_plugins([PLUGINS_DIR])
    specs = [replace(s, command=sys.executable) for s in loaded.mcp_servers if s.name == "experiment"]
    return build_kernel(
        provider=FakeProvider(script), base_dir=tmp_path / "base", model=ModelId("fake"),
        plugins=loaded, mcp=specs, native_tools=True, workspace_root=tmp_path / "ws",
        permissions=engine or _allow_engine(), resume_session_id=resume_session_id,
    )


def _events(tmp_path, session_id):
    return [e.event for e in read_session(tmp_path / "base", session_id)]


def _completed(events, tool):
    proposed = {e.call_id: e for e in events if isinstance(e, ToolCallProposed) and str(e.tool) == tool}
    return [e for e in events if isinstance(e, ToolCallCompleted) and e.call_id in proposed]


async def test_e2e_protected_write_is_blocked_and_injection_is_audited(tmp_path, monkeypatch):
    from harness.cli import run_once

    _workspace(tmp_path, monkeypatch)
    script = [
        tool_call_turn("start", START, {"experiment_dir": "exp-a"}),
        tool_call_turn("edit eval", ToolName("write_file"),
                       {"file_path": "exp-a/eval/test_m.py", "content": "cheat"}),
        text_turn("done"),
    ]
    kernel = _kernel(tmp_path, script)
    await run_once(kernel, "go")
    events = _events(tmp_path, kernel.session.id)
    (start_done,) = _completed(events, str(START))
    assert start_done.is_error is False, start_done.result_text
    resolved = next(e for e in events if isinstance(e, DispatchResolved) and str(e.tool) == str(START))
    assert resolved.args["store_root"] == str(tmp_path / "store")
    assert resolved.args["session_id"] == kernel.session.id
    assert resolved.args["experiment_dir"] == str((tmp_path / "ws" / "exp-a").resolve())
    (write_done,) = _completed(events, "write_file")
    assert write_done.is_error and "protected" in write_done.result_text
    assert (tmp_path / "ws" / "exp-a" / "eval" / "test_m.py").read_text() != "cheat"
    assert (tmp_path / "store" / "exp-a" / "runs" / "run-001" / "state.json").exists()


async def test_e2e_dormant_allows_the_same_write(tmp_path, monkeypatch):
    from harness.cli import run_once

    _workspace(tmp_path, monkeypatch)
    script = [
        tool_call_turn("edit", ToolName("write_file"),
                       {"file_path": "exp-a/eval/test_m.py", "content": "fine"}),
        text_turn("done"),
    ]
    kernel = _kernel(tmp_path, script)
    await run_once(kernel, "go")
    (write_done,) = _completed(_events(tmp_path, kernel.session.id), "write_file")
    assert write_done.is_error is False


async def test_e2e_ask_layer_with_shipped_rules_never_prompts(tmp_path, monkeypatch):
    from harness.cli import run_once

    _workspace(tmp_path, monkeypatch)
    script = [
        tool_call_turn("start", START, {"experiment_dir": "exp-a"}),
        tool_call_turn("status", STATUS, {}),
        text_turn("done"),
    ]
    kernel = _kernel(tmp_path, script, engine=_allow_engine(default="ask"))
    await run_once(kernel, "go")
    events = _events(tmp_path, kernel.session.id)
    assert not any(isinstance(e, PermissionRequested) for e in events)
    assert all(not e.is_error for e in _completed(events, str(STATUS)))


async def test_e2e_subagent_is_gated_by_the_parents_run(tmp_path, monkeypatch):
    from harness.cli import run_once

    _workspace(tmp_path, monkeypatch)
    script = [
        tool_call_turn("start", START, {"experiment_dir": "exp-a"}),
        tool_call_turn("delegate", ToolName("dispatch_agent"),
                       {"prompt": "write exp-a/workspace/x.txt", "agent": "experiment-worker"}),
        tool_call_turn("child writes", ToolName("write_file"),
                       {"file_path": "exp-a/workspace/x.txt", "content": "hi"}),
        text_turn("child done"),
        text_turn("parent done"),
    ]
    kernel = _kernel(tmp_path, script)
    await run_once(kernel, "go")
    parent = _events(tmp_path, kernel.session.id)
    spawned = next(e for e in parent if isinstance(e, SubagentSpawned))
    child = _events(tmp_path, spawned.child_session_id)
    asked = [e for e in child if isinstance(e, PermissionRequested)]
    assert asked and "phase read" in asked[0].reason
    (write_done,) = _completed(child, "write_file")
    assert write_done.is_error  # HeadlessResolver denies the Ask
    assert not (tmp_path / "ws" / "exp-a" / "workspace" / "x.txt").exists()


async def test_e2e_resume_needs_experiment_resume_then_gates(tmp_path, monkeypatch):
    from harness.cli import run_once

    _workspace(tmp_path, monkeypatch)
    first = _kernel(tmp_path, [tool_call_turn("start", START, {"experiment_dir": "exp-a"}), text_turn("ok")])
    await run_once(first, "go")
    run_id = json.loads(_completed(_events(tmp_path, first.session.id), str(START))[0].result_text)["run_id"]
    script = [
        tool_call_turn("resume", RESUME, {"run_id": run_id}),
        tool_call_turn("edit eval", ToolName("write_file"),
                       {"file_path": "exp-a/eval/test_m.py", "content": "cheat"}),
        text_turn("done"),
    ]
    second = _kernel(tmp_path, script, resume_session_id=first.session.id)
    await run_once(second, "continue")
    events = _events(tmp_path, first.session.id)
    (write_done,) = [e for e in _completed(events, "write_file") if e.is_error]
    assert "protected" in write_done.result_text
```

Add `import json` at the top of the file.

- [ ] **Step 2: Write the failing subprocess test**

```python
# tests/test_experiment_subprocess.py
"""The real path: python3 server.py under McpHost with the SDK default environment.

The harness process has HARNESS_EXPERIMENT_DIR set; the child cannot see it. If the
run lands under that directory, the injection worked; a call without the injected
keys is refused."""

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from harness.cli import build_kernel, run_once
from harness.events import ToolCallCompleted, ToolCallProposed
from harness.log import read_session
from harness.native_tools import baseline_ruleset
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.plugins import load_plugins
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.types import ModelId, ToolName

PLUGINS_DIR = Path(__file__).parent.parent / "plugins"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    exp = ws / "exp-a"
    (exp / "eval").mkdir(parents=True)
    (exp / "goal.yaml").write_text(yaml.dump({
        "objective": "o", "eval": "eval/",
        "success_criteria": [{"metric": "accuracy", "threshold": 0.9, "primary": True}]}))
    (exp / "eval" / "test_m.py").write_text('def test_m():\n    print("[METRIC] accuracy=1.0")\n')
    monkeypatch.chdir(ws)
    monkeypatch.setenv("HARNESS_EXPERIMENT_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("HARNESS_EXPERIMENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("MEMORY_VAULT_DIR", raising=False)
    return ws


def _kernel(tmp_path, script):
    loaded = load_plugins([PLUGINS_DIR])
    specs = [replace(s, command=sys.executable) for s in loaded.mcp_servers if s.name == "experiment"]
    engine = PermissionEngine([RuleSet(rules=[PermissionRule(action="allow", tool="mcp__experiment__*")]),
                               baseline_ruleset()])
    return build_kernel(provider=FakeProvider(script), base_dir=tmp_path / "base", model=ModelId("fake"),
                        plugins=loaded, mcp=specs, native_tools=True, workspace_root=tmp_path / "ws",
                        permissions=engine)


async def test_run_lands_under_the_injected_store_root(tmp_path, ws):
    T = "mcp__experiment__experiment_"
    script = [
        tool_call_turn("start", ToolName(T + "start"), {"experiment_dir": "exp-a"}),
        tool_call_turn("plan", ToolName(T + "advance"), {"run_id": "exp-a/run-001", "phase": "plan"}),
        tool_call_turn("work", ToolName(T + "advance"), {"run_id": "exp-a/run-001", "phase": "work"}),
        tool_call_turn("eval", ToolName(T + "advance"), {"run_id": "exp-a/run-001", "phase": "eval"}),
        tool_call_turn("run", ToolName(T + "run_eval"), {"run_id": "exp-a/run-001"}),
        tool_call_turn("journal", ToolName(T + "advance"), {"run_id": "exp-a/run-001", "phase": "journal"}),
        tool_call_turn("obs", ToolName(T + "record_observation"),
                       {"run_id": "exp-a/run-001", "observation": {"title": "t"}}),
        tool_call_turn("decide", ToolName(T + "advance"), {"run_id": "exp-a/run-001", "phase": "decide"}),
        tool_call_turn("done", ToolName(T + "advance"), {"run_id": "exp-a/run-001", "phase": "done"}),
        text_turn("finished"),
    ]
    kernel = _kernel(tmp_path, script)
    await run_once(kernel, "go")
    events = [e.event for e in read_session(tmp_path / "base", kernel.session.id)]
    completed = [e for e in events if isinstance(e, ToolCallCompleted)]
    errors = [e.result_text for e in completed if e.is_error]
    assert errors == [], errors
    run = json.loads((tmp_path / "store" / "exp-a" / "runs" / "run-001" / "run.json").read_text())
    assert run["outcome"] == "success" and run["metrics"]["accuracy"] == 1.0
    assert not list((tmp_path / "state").glob("*.json"))


async def test_call_without_injection_is_refused_by_the_server(tmp_path, ws):
    """Bypass the hook by calling the registered tool directly."""
    kernel = _kernel(tmp_path, [text_turn("noop")])
    await kernel.mcp.start()
    try:
        tool = kernel.registry.get(ToolName("mcp__experiment__experiment_status"))
        with pytest.raises(Exception, match="hook"):
            await tool({})
    finally:
        await kernel.mcp.stop()
```

- [ ] **Step 3: Run both files to verify they fail or pass**

Run: `uv run pytest tests/test_experiment_plugin.py tests/test_experiment_subprocess.py -v`
Expected: the new tests PASS if Tasks 1 to 7 are correct; a failure here is a defect in an earlier task, not in the tests. Fix the earlier task, re-run its own tests, then re-run these.

- [ ] **Step 4: Commit**

```bash
git add tests/test_experiment_plugin.py tests/test_experiment_subprocess.py
git commit -m "test(experiment): kernel end-to-end and real subprocess coverage"
```

---

### Task 9: Plugin README and harness docs

**Files:**
- Create: `plugins/experiment/README.md`
- Modify: `docs/user-guide.md` (plugins section), `docs/plugin-authoring.md` (MCP servers section), `README.md` (Plugins bullet)

- [ ] **Step 1: Write the plugin README**

```markdown
# experiment plugin

The experiment loop for harness: read, plan, work, eval, journal, decide, with
the eval run by the engine (never self-reported), a run store whose layout is
the constellation (reader, writer) contract, and phase discipline enforced by a
dispatch hook. Lifted from the copy parked in the agent-swarm repository
(constellation Wave G); the parked copy is unchanged.

## Layout

```
plugins/experiment/
  plugin.toml     manifest: hooks, MCP server "experiment" (tool_timeout_s 3600)
  server.py       FastMCP server (runs as a harness child)
  engine.py       the loop: phases, gates, state.json, evals/ records, pointers
  evals.py        goal.yaml, constraints.yaml, criteria, eval runner, protected digest
  store.py        run store: <root>/<experiment>/runs/run-NNN/{run.json,journal/}
  policy.py       phase deny table (validated at import)
  hooks.py        phase gate (dispatch, priority 990) + session capture
  skills/experiment.md, commands/experiment.md, agents/experiment-worker.md
```

## Using it

```
uv run harness --plugin-dir plugins
/experiment path/to/experiment-dir [--max-iterations N]
```

Launch harness from a directory that contains the experiment directory (and, for
integration experiments with `target:`, the target checkout): file tools are
confined to the launch directory.

Headless, one turn at a time (a turn stops at the loop's per-turn model-call
budget, twenty by default, so a run spans several `-p` calls):

```
uv run harness --plugin-dir plugins \
  --allow 'mcp__experiment__*' --allow invoke_skill --allow write_file --allow edit_file \
  --allow 'bash(pytest*)' -p "Load the experiment skill and start a run on exp-a"
uv run harness --plugin-dir plugins --allow ... -p "Call experiment_resume('exp-a/run-001') and continue the experiment skill"
```

With any `permissions.toml` present, add rules for `mcp__experiment__*`,
`invoke_skill`, and the native tools you want the work phase to use.

## Run record

```
<store_root>/<experiment>/runs/run-NNN/
  run.json     frozen 7-key record (also written by the parked plugin)
  state.json   loop state (phase, iteration, criteria, eval spec, digest, metrics, session_id)
  journal/     NNN_<slug>.md observations, frontmatter canonical
  evals/       NNN.json + NNN.out per eval, append-only
~/.local/state/harness/experiment/active/<session_id>.json   pointer while a run is open
```

## Environment (read by the hook in the harness process, never by the server)

| variable | effect |
|---|---|
| `HARNESS_EXPERIMENT_DIR` | store root; wins over everything |
| `MEMORY_VAULT_DIR` | with no explicit root, runs go to `<vault>/10-projects/<project>/experiments` |
| `HARNESS_EXPERIMENT_PROJECT` | the vault project (default `experiment`); must already exist |
| `HARNESS_EXPERIMENT_STATE_DIR` | pointer directory (default under `~/.local/state`) |

Without any of these, runs go to `~/.local/share/harness/experiments`. The server
receives the MCP SDK default environment only, so the hook injects `store_root`,
`session_id` and `state_dir` into every experiment tool call by `Rewrite`; a
permission prompt for such a call shows those arguments. This is a provisional
answer to a harness-level question (optional env references in plugin manifests).

## goal.yaml

`objective`, `eval`, `success_criteria` (each `metric`, `threshold`, optional
`comparison`/`comparator` in `>= <= > < == ge le gt lt eq report`, `primary`,
`description`; `threshold: null` or `report` marks report-only), optional
`target`, `context`, `environment` (flat NAME: value), `eval_python`,
`eval_timeout_s` (default 300, clamped under the server's 3600 s tool timeout).

`eval` forms: a directory (pytest), a `.py` file (run with the interpreter), or a
command string such as `uv run harness/run.py --replay` (run in the experiment
directory). Interpreter: `eval_python`, else `<experiment>/.venv/bin/python`, else
the server's Python. Metrics are `[METRIC] name=value` lines; with a pytest
summary, `test_pass_rate`, `tests_run`, `tests_passed`, `tests_failed` are derived
unless printed explicitly.

## Phases

| phase | Ask (human override in the TUI, denied headless) |
|---|---|
| read, plan, decide | write_file, edit_file, bash, run_eval, record_observation |
| work | run_eval, record_observation |
| eval | write_file, edit_file, bash, record_observation |
| journal | write_file, edit_file, bash, run_eval |

Blocked outright while a run is open: `experiment_start_run`,
`experiment_end_run`, and any `write_file`/`edit_file` under `goal.yaml`,
`constraints.yaml`, `eval/`, `fixtures/` or the eval target. The eval also
refuses to run if those files changed since the run started (digest check).

Versus the parked plugin's allow-lists: tools the plugin does not know (web
tools, `dispatch_agent`, other MCP servers) are not gated by phase; the journal
phase no longer allows `write_file` (the journal is the store); `bash` prefix
allow-lists are gone (the permission engine owns shell policy).

## Known limits

- `bash` and other servers' tools are not path-confined; the digest check is the backstop.
- One open run per harness process; tool calls batched in one message dispatch
  concurrently, so a phase transition must be the sole tool call in its message.
- `--resume` does not re-fire session start: a resumed session calls `experiment_resume(run_id)`.
- `--workspace` other than the launch directory is unsupported.
- `tool_timeout_s` is per server: a hung server delays every experiment tool.
```

- [ ] **Step 2: Edit docs/user-guide.md**

Find the line `this repo is a complete, working reference plugin.` in the Plugins section and change the sentence to:

```markdown
[plugin-authoring.md](plugin-authoring.md). The `plugins/memory/` directory in
this repo is a complete, working reference plugin, and `plugins/experiment/` is
the experiment loop (see its README).
```

- [ ] **Step 3: Edit docs/plugin-authoring.md**

In the section `## MCP servers and emitters`, after the paragraph ending `so a user's \`mcp.toml\` can shadow them.`, add:

```markdown
A plugin server runs as a child process with the MCP SDK's **default
environment** (`HOME`, `PATH`, `SHELL`, `TERM`, `USER`, `LOGNAME`), not the
harness process environment. A manifest `env` entry passes a variable through,
but a variable that is missing at start fails that server (the session
continues without its tools). How a server should receive optional
configuration is an open harness question; the experiment plugin's README
documents the provisional pattern it uses.
```

- [ ] **Step 4: Edit README.md**

Change the Plugins bullet to:

```markdown
- **Plugins.** Eight primitives (skills, commands, agents, dispatch/lifecycle
  hooks, subscribers, MCP servers, emitters) validated at load time. Two ship in
  `plugins/`: `memory` (the golden reference) and `experiment` (the experiment loop).
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/test_experiment_plugin.py -q` (the load test still passes with the README present).

```bash
git add plugins/experiment/README.md docs/user-guide.md docs/plugin-authoring.md README.md
git commit -m "docs(experiment): plugin README and harness doc updates"
```

---

### Task 10: Spec amendments, vault entity, full suite, PR

**Files:**
- Modify: `docs/superpowers/specs/2026-09-01-experiment-plugin-design.md`
- Vault (repo at `~/projects/vault`, branch `main`): create `10-projects/experiment/Experiment.md`, `10-projects/experiment/narrative.md`, `10-projects/experiment/decisions/2026-09-01-scope-single-run-loop-plus-store.md`, `10-projects/experiment/decisions/2026-09-01-model-driven-no-kernel-change.md`, `10-projects/experiment/decisions/2026-09-01-home-harness-plugins-plus-vault-entity.md`, `10-projects/experiment/decisions/2026-09-01-store-root-default.md`, `10-projects/experiment/specs/2026-09-01-experiment-plugin-design.md` (copy of the repo spec), `10-projects/harness/decisions/2026-09-01-open-questions-from-the-experiment-plugin.md`; append pointers to `10-projects/harness/narrative.md` and `10-projects/agent-swarm/narrative.md`.

- [ ] **Step 1: Amend the spec for what implementation settled**

In section 3, replace the second invariant bullet with:

```markdown
- **A run is bound to the session id that started it.** `harness --resume`
  does not re-fire the session-start point (harness question 3), so a resumed
  session, like any other session, calls `experiment_resume(run_id)`; the
  hook's owner falls back to `pid-<pid>` when no session start was seen.
```

In section 4, after the `session_id` bullet, add:

```markdown
- `state_dir`: the pointer directory (`HARNESS_EXPERIMENT_STATE_DIR`, default
  `~/.local/state/harness/experiment/active`), so both processes agree on
  where pointers live.
```

and change "The rewrite **overwrites** these keys" to "The rewrite **overwrites** these three keys".

- [ ] **Step 2: Create the vault entity**

`10-projects/experiment/Experiment.md`:

```markdown
---
created: 2026-09-01
type: project
status: active
tags: [experiment, harness, constellation, plugin, eval-gates]
---

# Experiment

**The experiment loop: a ticket (goal.yaml), an engine-run eval, a journal, and
phase discipline, as a harness plugin**

**Code:** `~/projects/harness/plugins/experiment/` (branch `feat/experiment-plugin` until merged)
**Parked copy:** the agent-swarm repository (`lib/experiment_*.py`, unchanged)

## What it does

Runs one experiment through read, plan, work, eval, journal and decide. The eval
is run by the engine from a spec frozen at start; decide -> done recomputes the
success criteria from recorded metrics; the run record is the constellation
(reader, writer) contract on disk; a dispatch hook enforces phase discipline and
keeps `goal.yaml`, `constraints.yaml`, `eval/` and `fixtures/` immutable.

## Structure

| Where | What |
|---|---|
| `narrative.md` | running account |
| `decisions/` | decision records |
| `specs/` | design documents |

## Key decisions (2026-09-01)

- Scope: single-run loop plus store first; the arms x trials x compare coordinator is a follow-on
- Driving: model-driven, no harness kernel change
- Home: `harness/plugins/experiment`, docs in this entity
- Store root: explicit dir, else the vault entity's `experiments/` subtree when the vault is configured, else the XDG data dir

## Related

- [[../harness/Harness]] (the shell it runs in)
- [[../agent-swarm/Agent Swarm]] (where the plugin was parked; not part of it)
- [[../constellation/2026-05-16-implementation-plan-v2|constellation Wave G]]
```

`10-projects/experiment/narrative.md`:

```markdown
---
project: experiment
updated: 2026-09-01
---

# Experiment

The experiment plugin is a constellation plugin (implementation plan item 8.5,
Wave G) that was parked inside the agent-swarm repository as
`lib/experiment_workflow.py`, `lib/experiment_harness.py`,
`lib/experiment_store.py` and `lib/experiment_server.py`, with the phase policy
in `config/workflows/experiment.yaml` and the protocol in `skills/experiment/`.
Seven real experiments use its `goal.yaml` + `eval/` shape across
logos-experiments, logos-workspace and the LOGOS vault.

## 2026-09-01: lifted into harness

Designed and specced as a harness-native plugin at `harness/plugins/experiment/`:
the loop, the run store (byte-compatible layout), engine-run evals with a
protected-tree digest, and a dispatch hook that gates phases (`Ask` for
discipline, `Block` for integrity) and injects the store location into every
call because plugin MCP servers see only the SDK default environment. The draft
design went through a four-lens adversarial review before approval; the
confirmed findings reshaped the hook, the eval spec, run lifecycle, the policy
and the store-root default. Spec: `specs/2026-09-01-experiment-plugin-design.md`.
```

Decision files, each with frontmatter `date: 2026-09-01`, `project: experiment`, `status: decided`, a `# Title`, `## Decision`, `## Why`, `## Rejected`:

- `2026-09-01-scope-single-run-loop-plus-store.md`: Decision: v1 is the single-run loop plus the (reader, writer) run store against the existing goal.yaml + eval/ directories; the coordinator is a follow-on. Why: the code is already shell-agnostic except for its daemon coupling; harness supplies enforcement, outcomes and telemetry natively. Rejected: coordinator first; store and tools only with the loop as prose.
- `2026-09-01-model-driven-no-kernel-change.md`: Decision: the model advances phases through plugin tools; the hook enforces; no kernel change. Why: works in the TUI across turns and headlessly with grants; keeps harness designed on its own terms. Rejected: an autonomous multi-turn driver (a harness change); a coordinator dispatching a subagent per phase.
- `2026-09-01-home-harness-plugins-plus-vault-entity.md`: Decision: code at `harness/plugins/experiment`, docs in `10-projects/experiment/`. Why: experiment is its own constellation plugin, not agent-swarm; in-repo placement mirrors the golden memory plugin. Rejected: docs under harness; a separate marketplace-pinned repo.
- `2026-09-01-store-root-default.md`: Decision: `HARNESS_EXPERIMENT_DIR`, else `<vault>/10-projects/<project>/experiments` when `MEMORY_VAULT_DIR` is set (project `HARNESS_EXPERIMENT_PROJECT` or `experiment`, which must exist), else `~/.local/share/harness/experiments`; never a failure. Why: keeps the 2026-08-13 "vault canonical when present" default under the new entity; a missing default entity degrades. Rejected: a mode switch with a Block on missing configuration; a hidden data directory as the unconditional default.

Copy the repo spec to `10-projects/experiment/specs/2026-09-01-experiment-plugin-design.md`.

`10-projects/harness/decisions/2026-09-01-open-questions-from-the-experiment-plugin.md` with frontmatter `date: 2026-09-01`, `project: harness`, `status: open`, title `# Open harness questions surfaced by the experiment plugin`, and the eight numbered items from the spec's section 12 copied verbatim, plus a closing line: `Source: [[../../experiment/specs/2026-09-01-experiment-plugin-design|experiment plugin design]], section 12.`

Append to `10-projects/harness/narrative.md`:

```markdown

## 2026-09-01: the experiment plugin lands beside memory

The experiment loop (constellation Wave G, parked until now in the agent-swarm
repository) was designed and specced as `plugins/experiment/`. It exercised
the plugin contract harder than memory did and surfaced eight harness-level
questions, recorded in `decisions/2026-09-01-open-questions-from-the-experiment-plugin.md`.
Entity: [[../experiment/Experiment]].
```

Append to `10-projects/agent-swarm/narrative.md`:

```markdown

## 2026-09-01: experiment moves to harness

The experiment plugin parked in this repository (`lib/experiment_*.py`) now
lives in harness as `plugins/experiment/` ([[../experiment/Experiment]]). The
parked copy is unchanged and still serves Claude Code sessions.
```

Commit in the vault on `main` with the message `experiment: add entity, decisions and spec; harness open questions` (the vault is a docs repo synced by cron on `main`; if `git status` shows unrelated changes, leave them unstaged).

- [ ] **Step 3: Full suite and lint**

Run: `uv run pytest -q` then `uv run ruff check` and `uv run ruff format --check`.
Expected: all green; record the exact `N passed, M skipped` line for the PR body. Fix anything red in the task that owns it.

- [ ] **Step 4: Commit, push, open the PR (ready for review, never draft)**

```bash
git add docs/superpowers/specs/2026-09-01-experiment-plugin-design.md
git commit -m "docs(experiment): spec amendments settled by implementation"
git push -u origin feat/experiment-plugin
gh pr create --base main --title "Experiment plugin: the experiment loop as a harness plugin (Wave G)" --body "$(cat <<'EOF'
## Summary
- lifts the experiment loop parked in the agent-swarm repository into `plugins/experiment/`: read/plan/work/eval/journal/decide with an engine-run eval and a decide->done recompute gate
- run store keeps the frozen (reader, writer) contract and byte-compatible layout; adds `state.json` and an append-only `evals/` history
- phase policy enforced by a dispatch hook (Ask for discipline, Block for the protected tree and lifecycle); store location injected by Rewrite because plugin servers see only the SDK default environment
- spec: docs/superpowers/specs/2026-09-01-experiment-plugin-design.md; plan: docs/superpowers/plans/2026-09-01-experiment-plugin.md

## Test plan
- [ ] `uv run pytest -q` green (paste the N passed line)
- [ ] `uv run ruff check` clean
- [ ] manual: `uv run harness --plugin-dir plugins`, `/experiment <dir>` on a real experiment directory
EOF
)"
```

Then watch for automated review comments and address every one in-branch. Merging is the owner's call.
