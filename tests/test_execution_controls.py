"""Operator budgets survive restart without overriding task/source authority."""

import asyncio
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from harness.agent import AgentTask, TaskLimits
from harness.cli import build_kernel
from harness.events import AgentRunStarted, ExecutionConfigured, UnknownEvent
from harness.execution import ExecutionLimits
from harness.execution_controls import configure_execution, parse_overrides
from harness.fold import fold
from harness.log import read_session
from harness.provider import FakeProvider, StreamStop, TextDelta, text_turn


async def kernel_at(base, provider=None, **kwargs):
    kernel = build_kernel(base_dir=base, model="fake", provider=provider or FakeProvider([]), **kwargs)
    if not kernel.resumed:
        await kernel.loop.start()
    return kernel


def starts(kernel):
    return [e.event for e in read_session(kernel.session.base, kernel.session.id)
            if isinstance(e.event, AgentRunStarted)]


@pytest.mark.parametrize("name", ["task_timeout_seconds", "inference_timeout_seconds", "coordination_timeout_seconds"])
@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan"), "30"])
def test_time_budgets_reject_invalid_values(name, value):
    with pytest.raises(ValueError):
        ExecutionLimits(**{name: value})


@pytest.mark.parametrize("words", [[], ["task-timeout-seconds"], ["unknown", "1"],
    ["max-children", "1.5"], ["task-timeout-seconds", "inf"],
    ["max-children", "1", "max-children", "2"]])
def test_operator_parser_rejects_invalid_or_ambiguous_settings(words):
    with pytest.raises(ValueError):
        parse_overrides(words)


async def test_resume_restores_settings_and_partial_override_keeps_other_fields(tmp_path):
    kernel = await kernel_at(tmp_path, execution_overrides={"task_timeout_seconds": 2400, "max_children": 3})
    sid = kernel.session.id
    configure_execution(kernel, {"inference_timeout_seconds": 300})
    kernel.session.close()
    resumed = await kernel_at(tmp_path, resume_session_id=sid, execution_overrides={"coordination_timeout_seconds": 1800})
    try:
        limits = resumed.loop.dispatcher.scope.budget.limits
        assert limits == ExecutionLimits(task_timeout_seconds=2400, inference_timeout_seconds=300,
                                         coordination_timeout_seconds=1800, max_children=3)
        assert fold(read_session(tmp_path, sid)).execution_limits == limits
    finally:
        resumed.session.close()


async def test_legacy_resume_uses_defaults_and_invalid_override_does_not_write(tmp_path):
    from harness.session import Session
    with Session(tmp_path, "legacy") as session:
        session.start()
    before = read_session(tmp_path, "legacy")
    with pytest.raises(ValueError):
        await kernel_at(tmp_path, resume_session_id="legacy", execution_overrides={"task_timeout_seconds": -1})
    assert read_session(tmp_path, "legacy") == before
    resumed = await kernel_at(tmp_path, resume_session_id="legacy")
    assert resumed.loop.dispatcher.scope.budget.limits == ExecutionLimits()
    resumed.session.close()


async def test_malformed_stored_settings_refuse_resume_before_any_append(tmp_path):
    kernel = await kernel_at(tmp_path)
    sid = kernel.session.id
    kernel.session.append(UnknownEvent(raw={"type": "execution_configured", "limits": {}}))
    kernel.session.close()
    before = read_session(tmp_path, sid)
    with pytest.raises(ValueError, match="invalid stored execution"):
        await kernel_at(tmp_path, resume_session_id=sid)
    assert read_session(tmp_path, sid) == before


async def test_failed_resume_configuration_write_releases_session_lock(tmp_path, monkeypatch):
    from harness.session import Session
    kernel = await kernel_at(tmp_path, execution_overrides={"task_timeout_seconds": 1800})
    sid = kernel.session.id
    kernel.session.close()
    original = Session.append

    def fail(self, event):
        if isinstance(event, ExecutionConfigured):
            raise OSError("disk unavailable")
        return original(self, event)

    with monkeypatch.context() as scoped:
        scoped.setattr(Session, "append", fail)
        with pytest.raises(OSError, match="disk unavailable"):
            await kernel_at(tmp_path, resume_session_id=sid, execution_overrides={"task_timeout_seconds": 3600})
    resumed = await kernel_at(tmp_path, resume_session_id=sid)
    try:
        assert resumed.loop.dispatcher.scope.budget.limits.task_timeout_seconds == 1800
    finally:
        resumed.session.close()


@pytest.mark.parametrize("task_limits,expected", [
    (TaskLimits(), 1800), (TaskLimits(max_iterations=1), 1800),
    (TaskLimits(timeout_seconds=60), 60), (TaskLimits(timeout_seconds=5000), 1800),
    (TaskLimits.model_validate(TaskLimits().model_dump()), 600),
])
async def test_fresh_defaults_and_explicit_or_replayed_task_caps(tmp_path, task_limits, expected):
    requests = []

    class Recording(FakeProvider):
        async def infer(self, request):
            requests.append(request)
            async for chunk in super().infer(request):
                yield chunk

    kernel = await kernel_at(tmp_path, Recording([text_turn("ok")]),
                             execution_overrides={"task_timeout_seconds": 1800, "inference_timeout_seconds": 300})
    try:
        await kernel.loop.run_task(AgentTask(prompt="work", limits=task_limits))
        assert starts(kernel)[0].limits["timeout_seconds"] == expected
        assert requests[0].timeout_seconds == pytest.approx(min(expected, 300), abs=.1)
    finally:
        kernel.session.close()


async def test_children_inherit_settings_and_record_effective_limits(tmp_path):
    kernel = await kernel_at(tmp_path, FakeProvider([text_turn("child done")]),
                             execution_overrides={"task_timeout_seconds": 2400})
    try:
        result = await kernel.runner.run_result(prompt="work", model=None, parent=kernel.session)
        assert result.status == "completed"
        child = read_session(tmp_path, result.child_session_id)
        assert fold(child).execution_limits.task_timeout_seconds == 2400
        assert next(e.event for e in child if isinstance(e.event, AgentRunStarted)).limits["timeout_seconds"] == 2400
    finally:
        kernel.session.close()


@pytest.mark.parametrize("typed", [False, True])
async def test_external_agent_uses_task_budget_and_not_native_inference_budget(tmp_path, typed):
    from tests.test_agent_stream_limits import ExternalAgent
    from tests.test_external_agent_runtime import ScriptedCodex
    backend = (ScriptedCodex if typed else ExternalAgent)([TextDelta("done"), StreamStop("end_turn")])
    kernel = await kernel_at(tmp_path, backend, execution_overrides={
        "task_timeout_seconds": 2400, "inference_timeout_seconds": .001})
    try:
        result = await kernel.loop.run_task(AgentTask(prompt="work"))
        assert result.status == "completed"
        assert len(starts(kernel)) == (2 if typed else 1)
        assert all(start.limits["timeout_seconds"] == 2400 for start in starts(kernel))
    finally:
        kernel.session.close()


@pytest.mark.parametrize("scope", ["task", "inference"])
async def test_deadline_cause_and_cleanup_are_distinct(tmp_path, scope):
    closed = asyncio.Event()

    class Waiting:
        async def infer(self, request):
            try:
                await asyncio.Event().wait()
                yield
            finally:
                closed.set()

    limits = {"task_timeout_seconds": .05 if scope == "task" else 5,
              "inference_timeout_seconds": 5 if scope == "task" else .05}
    kernel = await kernel_at(tmp_path, Waiting(), execution_overrides=limits)
    try:
        message = "task time budget exhausted" if scope == "task" else "before the task time budget expired"
        with pytest.raises(TimeoutError, match=message):
            await kernel.loop.run_task(AgentTask(prompt="work"))
        assert closed.is_set()
        state = fold(read_session(tmp_path, kernel.session.id))
        result, = state.agent_runs.values()
        assert result.reason == ("deadline" if scope == "task" else "timeout")
        assert not state.open_agent_runs and not state.open_model_intents
    finally:
        kernel.session.close()


async def test_idle_configuration_is_atomic_and_never_refunds_counts(tmp_path, monkeypatch):
    kernel = await kernel_at(tmp_path)
    budget = kernel.loop.dispatcher.scope.budget
    try:
        budget.model_calls = 7
        configure_execution(kernel, {"task_timeout_seconds": 1800})
        assert budget.model_calls == 7
        before = budget.limits
        original = kernel.session.append

        def fail(event):
            if isinstance(event, ExecutionConfigured):
                raise OSError("disk unavailable")
            return original(event)

        monkeypatch.setattr(kernel.session, "append", fail)
        with pytest.raises(OSError):
            configure_execution(kernel, {"task_timeout_seconds": 3600})
        assert budget.limits == before
        monkeypatch.setattr(kernel.session, "append", original)
        for attribute in ["active_children", "active_coordinators"]:
            setattr(budget, attribute, 1)
            with pytest.raises(ValueError, match="idle"):
                configure_execution(kernel, {"task_timeout_seconds": 3600})
            setattr(budget, attribute, 0)
        kernel.loop._task_active = True
        with pytest.raises(ValueError, match="idle"):
            configure_execution(kernel, {"task_timeout_seconds": 3600})
        assert budget.limits == before
    finally:
        kernel.session.close()


async def test_handoff_preserves_source_time_caps(tmp_path):
    from tests.test_handoff import source, specification
    kernel, _ = await source(tmp_path, limits=ExecutionLimits(task_timeout_seconds=90, inference_timeout_seconds=30))
    try:
        record = kernel.handoffs.record(specification(kernel))
        configure_execution(kernel, {"task_timeout_seconds": 1800, "inference_timeout_seconds": 300})
        result = await kernel.handoffs.run(record.id)
        assert result.status == "completed"
        assert starts(kernel)[-1].limits["timeout_seconds"] == 10  # explicit source task is stricter than its session
        assert kernel.loop.dispatcher.scope.budget.limits.task_timeout_seconds == 90
        assert kernel.loop.dispatcher.scope.budget.limits.inference_timeout_seconds == 30
        assert fold(read_session(kernel.session.base, kernel.session.id)).execution_limits == kernel.loop.dispatcher.scope.budget.limits
    finally:
        kernel.session.close()


async def test_tui_controls_are_core_preserve_draft_and_resume_destination(tmp_path):
    from textual.widgets import Input
    from harness.tui_support import SlashCommand
    from tests.test_tui import make_app
    from tests.test_tui_queue import screen_text
    from tests.test_tui_tasks import command

    other = await kernel_at(tmp_path, execution_overrides={"task_timeout_seconds": 45})
    other_id = other.session.id
    other.session.close()
    provider = FakeProvider([])
    app = make_app(tmp_path, provider=provider)
    async with app.run_test(size=(150, 45)) as pilot:
        app._plugin_commands["execution"] = SimpleNamespace(body="model must not run")
        await command(app, pilot, "/execution task-timeout-seconds 1800 inference-timeout-seconds 300")
        assert app.kernel.loop.dispatcher.scope.budget.limits.task_timeout_seconds == 1800
        composer = app.query_one("#prompt", Input)
        composer.value = "unfinished draft"
        before = read_session(tmp_path, app.kernel.session.id)
        await app._run_command(SlashCommand("execution", ""))
        await pilot.pause(.1)
        assert "task-timeout-seconds: 1800" in screen_text(app)
        assert "not hang detection" in screen_text(app)
        assert composer.value == "unfinished draft" and not provider.calls
        assert read_session(tmp_path, app.kernel.session.id) == before
        await app._rebuild_kernel()
        await pilot.pause(.1)
        assert app.kernel.loop.dispatcher.scope.budget.limits.task_timeout_seconds == 1800
        await app._rebuild_kernel(other_id)
        await pilot.pause(.1)
        assert app.kernel.loop.dispatcher.scope.budget.limits.task_timeout_seconds == 45


def test_event_rejects_coercion_and_missing_fields():
    limits = asdict(ExecutionLimits())
    with pytest.raises(ValueError):
        ExecutionConfigured(limits={**limits, "max_children": True})
    with pytest.raises(ValueError):
        ExecutionConfigured(limits={"task_timeout_seconds": 1})


@pytest.mark.parametrize("kind", ["codex", "claude_code", "antigravity"])
@pytest.mark.parametrize("explicit", [None, 30])
async def test_real_process_adapters_receive_owned_budget(tmp_path, monkeypatch, kind, explicit):
    import importlib
    import json
    from pathlib import Path
    from harness.execution import current_agent_timeout

    module = importlib.import_module(f"harness.provider_{kind}")
    fixture = importlib.import_module(f"tests.test_provider_{kind}")
    helper = {"codex": "_fake_codex", "claude_code": "_fake_claude", "antigravity": "_fake_agy"}[kind]
    cls = {"codex": "CodexProvider", "claude_code": "ClaudeCodeProvider", "antigravity": "AntigravityProvider"}[kind]
    binary = getattr(fixture, helper)(tmp_path, fixture.HAPPY)
    backend = getattr(module, cls)(binary=binary, timeout_s=explicit)
    timers = []

    def observe(seconds):
        timers.append(seconds)
        return asyncio.timeout(seconds)

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(**{**vars(asyncio), "timeout": observe}))
    # Fakes write only their invocation metadata; no real provider is contacted.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex"))
    kernel = await kernel_at(tmp_path / "sessions", backend,
                             execution_overrides={"task_timeout_seconds": 1800, "inference_timeout_seconds": .001})
    try:
        result = await kernel.loop.run_task(AgentTask(prompt="work"))
        assert result.status == "completed"
        assert timers == [pytest.approx(explicit or 1800, abs=.1)]
        assert current_agent_timeout.get() is None
        if kind == "antigravity":
            argv = json.loads(Path(binary + ".argv").read_text())
            assert argv[argv.index("--print-timeout") + 1] == f"{(explicit or 1800) - 5}s"
    finally:
        kernel.session.close()


def test_cli_flags_apply_in_headless_and_interactive_paths(tmp_path, monkeypatch, capsys):
    import harness.cli as cli
    import harness.tui as tui
    seen = []

    async def inspect(kernel, *args, **kwargs):
        seen.append(kernel.loop.dispatcher.scope.budget.limits)
        kernel.session.close()
        return "done"

    monkeypatch.setattr(cli, "_amain", inspect)
    monkeypatch.setattr(tui, "run_tui", inspect)
    for mode in [["-p", "work"], []]:
        monkeypatch.setattr("sys.argv", ["harness", "--base-dir", str(tmp_path), "--no-plugins",
            "--task-timeout-seconds", "1800", "--inference-timeout-seconds", "300",
            "--coordination-timeout-seconds", "2400", "--max-active-children", "2", *mode])
        cli.main()
    assert len(seen) == 2
    assert all(limits == ExecutionLimits(task_timeout_seconds=1800, inference_timeout_seconds=300,
        coordination_timeout_seconds=2400, max_active_children=2) for limits in seen)
    capsys.readouterr()
