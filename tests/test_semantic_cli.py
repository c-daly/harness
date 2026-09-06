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
