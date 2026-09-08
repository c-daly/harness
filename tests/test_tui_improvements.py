"""Operator controls remain usable and cancellable in the final terminal."""

import asyncio

import pytest
from textual.widgets import Input

from harness.fold import fold
from harness.improvement_journal import read_improvements
from harness.log import read_session
from harness.types import ModelId
from tests.test_prompt_improvement import LearningProvider, experiment
from tests.test_tui import make_app
from tests.test_tui_queue import screen_text


async def submit(app, pilot, text):
    app.query_one("#prompt", Input).value = text
    await pilot.press("enter")
    await pilot.pause(0.05)
    if app._semantic_worker is not None and not app._semantic_worker.is_finished:
        await asyncio.wait_for(app._semantic_worker.wait(), 3)
        await pilot.pause(0.05)


async def test_full_operator_loop_from_terminal_commands(tmp_path):
    provider = LearningProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("fake"))
    path = tmp_path / "experiment.json"
    path.write_text(experiment().model_dump_json())
    async with app.run_test(size=(160, 55)) as pilot:
        await pilot.pause(0.1)
        await submit(app, pilot, "/task new Review the fix")
        for text in ("broken one", "broken two"):
            await submit(app, pilot, f"/semantics classify {text}")
        await submit(app, pilot, "/improvements")
        assert "a supervised proposal is available" in screen_text(app)
        await submit(app, pilot, "/improvements propose")
        candidate = list(app.kernel.improvements.state.candidates.values())[-1]
        await submit(app, pilot, f"/improvements show {candidate.id}")
        assert "classify questions" in screen_text(app) and "precisely" in screen_text(app)
        await submit(app, pilot, f"/improvements evaluate {candidate.id} {path}")
        result = list(app.kernel.improvements.state.results.values())[-1]
        assert "Explicit adoption is available" in screen_text(app)
        assert not read_improvements(tmp_path, app.kernel.session.id).prompt_changes
        await submit(app, pilot, f"/improvements adopt {result.id}")
        assert "Adopted message prompt" in screen_text(app)
        await submit(app, pilot, "/semantics classify Which branch has the fix?")
        assert "Prompt selection: adopt" in screen_text(app)
        await submit(app, pilot, "/improvements rollback")
        assert "Restored message prompt" in screen_text(app)
        assert not app.kernel.tasks.selected().accepted
        assert not fold(read_session(tmp_path, app.kernel.session.id)).messages


@pytest.mark.parametrize("interrupt", ["escape", "new-work"])
async def test_terminal_cancellation_preserves_draft_and_new_work_takes_priority(tmp_path, interrupt):
    provider = LearningProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("fake"))
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.1)
        for text in ("broken one", "broken two"):
            await submit(app, pilot, f"/semantics classify {text}")
        provider.hang = "improvement:"
        composer = app.query_one("#prompt", Input)
        composer.value = "/improvements propose"
        await pilot.press("enter")
        await asyncio.wait_for(provider.entered.wait(), 3)
        worker = app._semantic_worker
        state = read_improvements(tmp_path, app.kernel.session.id)
        evidence = list(state.evidence)[0]
        composer.value = f"/improvements show {evidence}"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert app._semantic_worker is worker and not worker.is_finished
        assert "prompt causality is unproven" in " ".join(screen_text(app).split())
        composer.value = "next user request"
        if interrupt == "escape":
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert composer.value == "next user request" and not composer.disabled
            assert "Improvement interrupted" in screen_text(app)
        else:
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert any(r.purpose == "conversation" for r in provider.requests)
        state = read_improvements(tmp_path, app.kernel.session.id)
        assert not state.candidates and not state.prompt_changes
        assert not fold(read_session(tmp_path, app.kernel.session.id)).open_model_intents


async def test_plugins_cannot_redirect_improvement_controls_into_conversation(tmp_path):
    from types import SimpleNamespace
    provider = LearningProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("fake"))
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.1)
        app._plugin_commands.update({key: SimpleNamespace(body="Unwanted agent task")
                                     for key in ("semantics", "improvements")})
        await submit(app, pilot, "/semantics classify broken one")
        await submit(app, pilot, "/improvements")
        assert len(provider.requests) == 1 and provider.requests[0].purpose.startswith("semantic:")
        assert not fold(read_session(tmp_path, app.kernel.session.id)).messages


async def test_failed_proposal_does_not_display_provider_exception_body(tmp_path):
    from harness.errors import AuthFailed
    provider = LearningProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("fake"))
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.1)
        for text in ("broken one", "broken two"):
            await submit(app, pilot, f"/semantics classify {text}")

        async def denied(request):
            raise AuthFailed("private provider response sentinel")
            yield
        provider.infer = denied
        await submit(app, pilot, "/improvements propose")
        visible = screen_text(app)
        assert "Improvement refused: AuthFailed" in visible
        assert "private provider response sentinel" not in visible
        assert not read_improvements(tmp_path, app.kernel.session.id).candidates


@pytest.mark.parametrize("interrupt", [False, True])
async def test_assessment_comparison_uses_terminal_worker_and_keeps_fixture_provenance(tmp_path, interrupt):
    from tests.test_assessment_evaluation import AssessmentProvider, experiment
    spec = experiment("progress_assessment")
    provider = AssessmentProvider(spec)
    app = make_app(tmp_path, provider=provider, model=ModelId("fake"))
    path = tmp_path / "assessment.json"
    path.write_text(spec.model_dump_json())
    async with app.run_test(size=(160, 55)) as pilot:
        await submit(app, pilot, "/task new Actual task")
        await submit(app, pilot, "/semantics progress")
        provider.seed = False
        before = app.kernel.tasks.state()
        if interrupt:
            provider.hang = True
            app.query_one("#prompt", Input).value = f"/improvements compare {path}"
            await pilot.press("enter")
            await asyncio.wait_for(provider.entered.wait(), 3)
            app.query_one("#prompt", Input).value = "preserve this draft"
            await pilot.press("escape")
            await pilot.pause(.1)
            assert app.query_one("#prompt", Input).value == "preserve this draft"
            assert "Improvement interrupted" in screen_text(app)
        else:
            await submit(app, pilot, f"/improvements compare {path}")
            assert "assessment adoption is unavailable" in screen_text(app)
        state = read_improvements(tmp_path, app.kernel.session.id)
        result = list(state.results.values())[-1]
        assert result.completion == ("cancelled" if interrupt else "completed")
        await submit(app, pilot, "/semantics")
        visible = " ".join(screen_text(app).split())
        assert "Evaluation fixture" in visible and "Not live task evidence" in visible
        assert app.kernel.tasks.state() == before and not state.prompt_changes
        assert not fold(read_session(tmp_path, app.kernel.session.id)).open_evaluations
