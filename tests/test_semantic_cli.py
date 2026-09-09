"""Explicit invocation and inspection share the normal core records."""

import json

import pytest

from harness.cli import main
from harness.log import read_session
from harness.provider import FakeProvider, text_turn
from harness.semantic_evaluation import EvaluatorConfig
from harness.semantics import SemanticLimits
from tests.test_semantic_evaluation import PairedProvider, prepare
from tests.test_semantics import kernel_for


def catalog_file(tmp_path):
    path = tmp_path / "models.toml"
    path.write_text('[models.fake]\nroute = "openai/scripted"\n')
    return path


def test_classify_and_inspect_use_saved_records_without_plugins(tmp_path, monkeypatch, capsys):
    provider = FakeProvider([text_turn('{"kind":"uncertain"}')])
    monkeypatch.setattr("harness.semantic_cli.CatalogProvider", lambda catalog: provider)
    monkeypatch.setattr("sys.argv", ["harness", "semantic", "classify", "thanks",
        "--model", "fake", "--base-dir", str(tmp_path), "--catalog", str(catalog_file(tmp_path)),
        "--allow", "model:fake"])
    main()
    output = capsys.readouterr().out
    session_id = output.splitlines()[0].removeprefix("Session: ")
    assert "shadow mode" in output and "uncertain" in output
    before = read_session(tmp_path, session_id)
    monkeypatch.setattr("sys.argv", ["harness", "semantic", "inspect", session_id, "--base-dir", str(tmp_path)])
    main()
    assert "Suggestions do not accept tasks" in capsys.readouterr().out
    assert read_session(tmp_path, session_id) == before and len(provider.calls) == 1


def test_evaluate_cli_resumes_fixed_plan_and_prints_measurements(tmp_path, monkeypatch, capsys):
    import asyncio
    provider = PairedProvider()

    async def setup():
        kernel = await kernel_for(tmp_path, provider)
        incumbent, _, plan, config = prepare(kernel, provider)
        prompt_path, config_path = tmp_path / "incumbent.json", tmp_path / "evaluation.json"
        prompt_path.write_bytes(kernel.session.blobs.get(incumbent))
        config_path.write_text(config.model_dump_json())
        session_id = kernel.session.id
        kernel.session.close()
        return session_id, plan.id, prompt_path, config_path

    session_id, plan_id, prompt, config = asyncio.run(setup())
    monkeypatch.setattr("harness.semantic_cli.CatalogProvider", lambda catalog: provider)
    monkeypatch.setattr("sys.argv", ["harness", "semantic", "evaluate", session_id, plan_id,
        "--base-dir", str(tmp_path), "--catalog", str(catalog_file(tmp_path)),
        "--incumbent", str(prompt), "--config", str(config), "--allow", "model:fake"])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "passed" and report["metrics"]["candidate"]["correct"] == 3
    assert read_session(tmp_path, session_id)[-1].event.type == "session_ended"


@pytest.mark.parametrize("config", [
    {"runtime_version": "x", "model": "fake", "timeout_seconds": 601},
    {"runtime_version": "x", "model": "fake", "limits": {"timeout_seconds": 31}},
])
def test_evaluation_cannot_remove_time_bounds(config):
    with pytest.raises(ValueError):
        EvaluatorConfig.model_validate(config)


async def test_final_terminal_shows_shadow_and_interrupted_evaluation_records(tmp_path):
    from harness.events import EvaluationRunStarted, EvaluationRunFinished
    from tests.test_tui import make_app
    from tests.test_tui_queue import screen_text
    app = make_app(tmp_path)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.1)
        observation = await app.kernel.semantics.interpret("thanks", model=app.kernel.loop.model,
            enabled=False, limits=SemanticLimits())
        app.kernel.session.append(EvaluationRunStarted(run_id="interrupted", plan_id="p",
            incumbent=observation.prompt, configuration=app.kernel.session.blobs.put(b"{}")))
        app.kernel.session.append(EvaluationRunFinished(run_id="interrupted", status="aborted"))
        await pilot.click("#prompt")
        await pilot.press(*"/semantics", "enter")
        await pilot.press(*"/improvements", "enter")
        await pilot.pause(0.1)
        visible = screen_text(app)
        assert "shadow mode" in visible and "uncertain: disabled" in visible
        assert "Evaluation run interrupted: aborted" in visible
        assert "do not activate" in visible


def test_context_cli_uses_only_explicit_candidates(tmp_path, monkeypatch, capsys):
    from tests.test_semantic_assessment import candidates, selection
    provider = FakeProvider([text_turn(json.dumps(selection()))])
    source = tmp_path / "candidates.json"
    source.write_text(candidates().model_dump_json())
    monkeypatch.setattr("harness.semantic_cli.CatalogProvider", lambda catalog: provider)
    monkeypatch.setattr("sys.argv", ["harness", "semantic", "context", str(source),
        "--model", "fake", "--base-dir", str(tmp_path), "--catalog", str(catalog_file(tmp_path)),
        "--allow", "model:fake"])
    main()
    assert "Suggested context: current" in capsys.readouterr().out
    assert len(provider.calls) == 1


def test_context_cli_reports_core_no_match_without_model_permission(tmp_path, monkeypatch, capsys):
    from tests.test_semantic_assessment import candidates
    provider = FakeProvider([])
    source = tmp_path / "candidates.json"
    source.write_text(candidates(candidates=[]).model_dump_json())
    monkeypatch.setattr("harness.semantic_cli.CatalogProvider", lambda catalog: provider)
    monkeypatch.setattr("sys.argv", ["harness", "semantic", "context", str(source),
        "--model", "fake", "--base-dir", str(tmp_path), "--catalog", str(catalog_file(tmp_path))])
    main()
    output = capsys.readouterr().out
    assert "no_match" in output and "Core eligibility:" in output and "no model call" in output
    assert not provider.calls


async def test_terminal_distinguishes_core_eligibility_from_a_model_answer(tmp_path):
    from tests.test_semantic_assessment import candidates
    from tests.test_tui import make_app
    from tests.test_tui_queue import screen_text
    provider = FakeProvider([])
    app = make_app(tmp_path, provider=provider)
    async with app.run_test(size=(150, 45)) as pilot:
        await app.kernel.semantics.select_context(candidates(candidates=[]), model=app.kernel.loop.model)
        await pilot.click("#prompt")
        await pilot.press(*"/semantics", "enter")
        await pilot.pause(0.1)
        visible = screen_text(app)
        assert "Core eligibility:" in visible and "no model call" in visible and "no_match" in visible
        assert not provider.calls


def test_assessment_compare_cli_and_saved_plan_replay(tmp_path, monkeypatch, capsys):
    import asyncio
    from harness.improvement_journal import read_improvements
    from tests.test_assessment_evaluation import AssessmentProvider, experiment, seed
    spec = experiment()
    provider = AssessmentProvider(spec)

    async def setup():
        kernel = await kernel_for(tmp_path, provider)
        await seed(kernel, provider)
        session_id = kernel.session.id
        kernel.session.close()
        return session_id

    session_id = asyncio.run(setup())
    spec_path = tmp_path / "assessment.json"
    spec_path.write_text(spec.model_dump_json())
    monkeypatch.setattr("harness.provider_litellm.CatalogProvider", lambda catalog: provider)
    catalog = catalog_file(tmp_path)
    monkeypatch.setattr("sys.argv", ["harness", "improve", "--model", "fake", "--base-dir", str(tmp_path),
        "--catalog", str(catalog), "--allow", "model:fake", session_id, "compare", str(spec_path)])
    main()
    assert "Explicit shadow adoption is available" in capsys.readouterr().out
    state = read_improvements(tmp_path, session_id)
    result = list(state.results.values())[-1]
    plan = state.plans[result.plan_id]
    from harness.semantic_assessment import CONTEXT_PROMPT
    # Re-run the saved plan through the same CLI entry as message experiments.
    incumbent, configuration = tmp_path / "incumbent.json", tmp_path / "config.json"
    incumbent.write_text(CONTEXT_PROMPT.model_dump_json())
    configuration.write_text(spec.configuration.model_dump_json())
    monkeypatch.setattr("harness.semantic_cli.CatalogProvider", lambda catalog: provider)
    monkeypatch.setattr("sys.argv", ["harness", "semantic", "evaluate", session_id, plan.id,
        "--base-dir", str(tmp_path), "--catalog", str(catalog), "--allow", "model:fake",
        "--incumbent", str(incumbent), "--config", str(configuration)])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["function"] == "context_selection" and report["verdict"] == "passed"
    assert len(read_improvements(tmp_path, session_id).results) == 2
    before = read_session(tmp_path, session_id)
    calls = len(provider.requests)
    monkeypatch.setattr("sys.argv", ["harness", "improvements", session_id, "--base-dir", str(tmp_path),
                                    "--show", report["result_id"]])
    main()
    assert '"function": "context_selection"' in capsys.readouterr().out
    assert read_session(tmp_path, session_id) == before and len(provider.requests) == calls


def test_progress_cli_resumes_task_and_never_checks_or_accepts_it(tmp_path, monkeypatch, capsys):
    import asyncio
    from tests.test_semantic_assessment import assessment
    from tests.test_task_evidence import add_review, finish, start
    provider = FakeProvider([text_turn(json.dumps(assessment(["review"], ["review"], "review")))])

    async def setup():
        kernel = await kernel_for(tmp_path, provider)
        task = kernel.tasks.create("Review output")
        add_review(kernel.tasks)
        start(kernel, task.id)
        finish(kernel, task.id)
        kernel.session.close()
        return kernel.session.id

    session_id = asyncio.run(setup())
    monkeypatch.setattr("harness.semantic_cli.CatalogProvider", lambda catalog: provider)
    monkeypatch.setattr("sys.argv", ["harness", "semantic", "progress", session_id,
        "--model", "fake", "--base-dir", str(tmp_path), "--catalog", str(catalog_file(tmp_path)),
        "--allow", "model:fake"])
    main()
    assert "Suggested next step: review" in capsys.readouterr().out
    events = read_session(tmp_path, session_id)
    assert not any(e.event.type in {"task_checked", "task_accepted", "task_requirement_confirmed"} for e in events)
    assert len(provider.calls) == 1


async def test_progress_tui_displays_remaining_obligations_and_historical_basis(tmp_path):
    from tests.test_semantic_assessment import assessment
    from tests.test_task_evidence import add_review, finish, start
    from tests.test_tui import make_app
    from tests.test_tui_queue import screen_text
    app = make_app(tmp_path, provider=FakeProvider([
        text_turn(json.dumps(assessment(["review"], ["review"], "review")))]))
    async with app.run_test(size=(150, 45)) as pilot:
        task = app.kernel.tasks.create("Review output")
        add_review(app.kernel.tasks)
        start(app.kernel, task.id)
        finish(app.kernel, task.id)
        await pilot.click("#prompt")
        await pilot.press(*"/semantics progress", "enter")
        await pilot.pause(0.1)
        visible = screen_text(app)
        assert "Remaining: review" in visible and "Suggested next step: review" in visible
        assert "Recorded evidence as of event" in visible and "shadow mode" in visible
        assert not app.kernel.tasks.selected().accepted


@pytest.mark.parametrize("interrupt", ["escape", "prompt", "clear", "quit"])
async def test_semantic_worker_settles_before_foreground_or_session_teardown(tmp_path, interrupt):
    import asyncio
    from harness.fold import fold
    from harness.semantics import read_semantics
    from tests.test_task_evidence import add_review
    from tests.test_tui import make_app
    from tests.test_tui_queue import screen_text
    entered, closed = asyncio.Event(), asyncio.Event()

    class Hanging(FakeProvider):
        async def infer(self, request):
            if not request.purpose.startswith("semantic:"):
                assert closed.is_set()  # Foreground waits for cancellation to settle.
                async for chunk in super().infer(request):
                    yield chunk
                return
            try:
                entered.set()
                await asyncio.Event().wait()
                yield
            finally:
                closed.set()

    app = make_app(tmp_path, provider=Hanging([text_turn("New request answered")]))
    async with app.run_test(size=(150, 45)) as pilot:
        app.kernel.tasks.create("Work")
        add_review(app.kernel.tasks)
        session_id = app.kernel.session.id
        await pilot.click("#prompt")
        await pilot.press(*"/semantics progress", "enter")
        await asyncio.wait_for(entered.wait(), 2)
        if interrupt == "escape":
            await pilot.press("escape")
        else:
            text = {"prompt": "New request", "clear": "/clear", "quit": "/quit"}[interrupt]
            await pilot.press(*text, "enter")
        await asyncio.wait_for(closed.wait(), 2)
        await pilot.pause(0.1)
        observations = read_semantics(tmp_path, session_id)
        assert observations[-1].reason == "cancelled"
        events = read_session(tmp_path, session_id)
        assert not fold(events).open_model_intents
        assert not any(e.event.type == "user_interrupt" for e in events)
        if interrupt == "prompt":
            assert "New request answered" in screen_text(app)
