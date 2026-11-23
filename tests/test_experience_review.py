import json

import pytest

from harness.agent import AgentResult, AgentTask
from harness.events import AgentRunFinished, AgentRunStarted, AssessmentObserved, ContextSourceObserved, ImprovementRecorded, ModelSelected, SessionResumed, TaskChecked, UserMessage
from harness.improvement_cli import main, perform
from harness.improvement_examples import select_examples, session_examples
from harness.improvement_feedback import record_feedback
from harness.improvement_journal import read_improvements
from harness.log import read_session
from harness.messages import Message
from harness.provider import FakeProvider, text_turn
from harness.semantics import read_semantics, render_semantics
from harness.session import Session
from harness.types import SessionId
from tests.test_improvement_examples import record_lookup
from tests.test_semantics import kernel_for


def begin_failed_turn(session, *, project=None):
    capabilities = {"project": project} if project is not None else {}
    start = session.append(AgentRunStarted(task_id="bad", run_id="bad", runtime="harness", capabilities=capabilities))
    request = session.append(UserMessage(text="Which register should I check now?"))
    return start, request


def confirm_failure(kernel):
    session = kernel.session
    finish = session.append(AgentRunFinished(result=AgentResult(
        task_id="bad", run_id="bad", status="completed", output=session.blobs.put(b"Wrong destination"))))
    record_feedback(kernel.improvements, "The reply named an unchecked destination.", turn=finish.seq, failure=True)
    failure, = (e for e in read_improvements(session.base, session.id).evidence.values()
                if e.category == "failure")
    return finish.seq, failure


def response(ids=(), reason="no_match"):
    return text_turn(json.dumps({"function": "context_selection", "selected_ids": ids, "reason": reason}))


@pytest.mark.parametrize("preparation", ["none", "ready", "other-run", "mismatched", "missing-blob"])
async def test_review_selects_examples_as_of_the_original_request_and_keeps_the_failure(tmp_path, preparation):
    provider = FakeProvider([])
    kernel = await kernel_for(tmp_path, provider, workspace_root=tmp_path)
    session = kernel.session
    try:
        record_lookup(session, run="prior")
        before = session_examples(tmp_path, session.id)
        provider.script.extend([response([before[0].id], "selected"), response([before[0].id], "selected")])
        start, request = begin_failed_turn(session)
        original, _ = await select_examples(kernel.semantics, model=kernel.loop.model, query=request.event.text, examples=before)
        prior_observation_seq = session.last_seq
        context_seq = None
        if preparation == "ready":
            from harness.experience_context import ExampleContext
            context = ExampleContext(original.id, before, kernel.loop.model)
            messages = [Message.system_text("Check the request"), Message.user_text(request.event.text)]
            prepared = context.admit(session, AgentTask(id="bad", prompt=request.event.text), "bad", messages, [], 32768)
            assert before[0].id in prepared[-1].blocks[0].text
            context_seq = session.last_seq
        elif preparation != "none":
            packet = {"assessment_id": original.id if preparation != "mismatched" else "another",
                      "examples": [{"id": before[0].id}]}
            ref = session.blobs.put(json.dumps(packet).encode())
            if preparation == "missing-blob":
                ref = ref.model_copy(update={"sha256": "b" * 64})
            context_seq = session.append(ContextSourceObserved(source_id="supervised-examples", status="ready",
                task_id="bad", run_id="other" if preparation == "other-run" else "bad",
                tool="context_selection", byte_count=ref.size, result=ref)).seq
        finish, failure = confirm_failure(kernel)
        record_lookup(session, run="later")
        message = await perform(kernel, ["review-failure", failure.id], explicit_model=True)
        assert before[0].evidence_id in message
        assert ("prepared as original task context" if preparation == "ready" else "delivery unverified") in message
        observation = read_semantics(tmp_path, session.id)[-1]
        assert observation.failure_review.original_selection_seqs == (prior_observation_seq,)
        assert observation.failure_review.original_context_seqs == ((context_seq,) if preparation == "ready" else ())
        assert observation.failure_review.request_seq == request.seq
        assert observation.failure_review.run_start_seq == start.seq and observation.failure_review.run_finish_seq == finish
        assert failure.id in render_semantics([observation])
        assert read_improvements(tmp_path, session.id).evidence[failure.id] == failure
        payload = json.loads(session.blobs.get(observation.input))
        assert payload["query"] == request.event.text
        assert [c["id"] for c in payload["candidates"]] == [before[0].id]
        assert "Wrong destination" not in str(payload) and "later" not in str(payload)
    finally:
        session.close()


@pytest.mark.parametrize(("prior_count", "reason", "explicit", "expected", "expected_calls"), [
    (0, "no_match", False, "No matching example in this session", 0),
    (1, "no_match", True, "No matching example in this session", 1),
    (17, "no_match", True, "search is incomplete", 1),
    (1, "uncertain", True, "unassessed (uncertain)", 1),
    (1, "no_match", False, "unassessed (disabled)", 0),
])
async def test_no_match_is_distinct_from_limited_or_unassessed_history(
        tmp_path, prior_count, reason, explicit, expected, expected_calls):
    provider = FakeProvider([response(reason=reason)])
    kernel = await kernel_for(tmp_path, provider, workspace_root=tmp_path)
    try:
        for i in range(prior_count):
            record_lookup(kernel.session, run=f"prior-{i}")
        begin_failed_turn(kernel.session)
        _, failure = confirm_failure(kernel)
        message = await perform(kernel, ["review-failure", failure.id], explicit_model=explicit)
        assert expected in message
        assert len(provider.calls) == expected_calls
        obs, = [row.event.observation for row in read_session(tmp_path, kernel.session.id)
                if isinstance(row.event, AssessmentObserved)]
        assert obs.failure_review.eligible_examples == prior_count and obs.failure_review.failure_evidence_id == failure.id
    finally:
        kernel.session.close()


async def test_rewritten_destination_never_receives_recorded_history(tmp_path):
    from dataclasses import replace
    from harness.hooks import HookBus, ProposedModelCall, Rewrite
    from harness.types import ModelId
    hooks = HookBus()

    async def redirect(call):
        if isinstance(call, ProposedModelCall):
            return Rewrite(replace(call, model=ModelId("another")))

    hooks.register_dispatch("redirect", redirect)
    provider = FakeProvider([response()])
    kernel = await kernel_for(tmp_path, provider, hooks=hooks, workspace_root=tmp_path)
    try:
        record_lookup(kernel.session, run="prior")
        begin_failed_turn(kernel.session)
        _, failure = confirm_failure(kernel)
        message = await perform(kernel, ["review-failure", failure.id], explicit_model=True)
        assert "unassessed (denied)" in message and not provider.calls
    finally:
        kernel.session.close()


@pytest.mark.parametrize("scope", ["unbound", "bound", "changed", "legacy-origin"])
async def test_explicit_review_uses_the_original_workspace_and_launch(tmp_path, monkeypatch, scope):
    alpha, beta = {"git_common_dir": "a" * 64}, {"git_common_dir": "b" * 64}
    project = None if scope == "unbound" else alpha
    if scope == "legacy-origin":
        project = {"repository": "test/alpha"}

    async def original_project(workspace):
        return project

    monkeypatch.setattr("harness.project_binding.current_project", original_project)
    provider = FakeProvider([response()])
    kernel = await kernel_for(tmp_path, provider, workspace_root=tmp_path)
    try:
        session = kernel.session
        record_lookup(session, run="previous-launch", project=project)
        boundary = session.append(SessionResumed()).seq
        record_lookup(session, run="unrelated", project=beta)
        record_lookup(session, run="original-launch", project=project)
        begin_failed_turn(session, project=beta if scope == "changed" else project)
        _, failure = confirm_failure(kernel)
        session.append(SessionResumed())
        message = await perform(kernel, ["review-failure", failure.id], explicit_model=True)
        if scope in {"changed", "legacy-origin"}:
            assert "original workspace scope could not be verified" in message
            assert not provider.calls
        else:
            obs, = read_semantics(tmp_path, session.id)
            payload = session.blobs.get(obs.input).decode()
            assert "original-launch" in payload and "unrelated" not in payload
            assert ("previous-launch" in payload) == (scope == "bound")
            assert obs.failure_review.history_floor_seq == (0 if scope == "bound" else boundary)
            assert obs.failure_review.workspace_key == ("a" * 64 if scope == "bound" else None)
    finally:
        kernel.session.close()


@pytest.mark.parametrize("delivered", [False, True])
async def test_original_external_candidate_is_reported_even_when_session_scope_is_empty(tmp_path, delivered):
    from harness.experience_context import ExampleContext
    with Session(tmp_path, SessionId("external-examples")) as source:
        source.start()
        record_lookup(source, run="outside-this-session")
        prior = session_examples(tmp_path, source.id)
    provider = FakeProvider([response([prior[0].id], "selected")])
    kernel = await kernel_for(tmp_path, provider, workspace_root=tmp_path)
    try:
        session = kernel.session
        _, request = begin_failed_turn(session)
        original, _ = await select_examples(kernel.semantics, model=kernel.loop.model,
                                            query=request.event.text, examples=prior)
        if delivered:
            context = ExampleContext(original.id, prior, kernel.loop.model)
            context.admit(session, AgentTask(id="bad", prompt=request.event.text), "bad",
                          [Message.user_text(request.event.text)], [], 32768)
        _, failure = confirm_failure(kernel)
        result = await perform(kernel, ["review-failure", failure.id], explicit_model=True)
        assert prior[0].id in result and "Coverage of the original candidate pool is incomplete" in result
        assert ("prepared as original task context" if delivered else "delivery unverified") in result
        assert "No matching example in this session" not in result
        review = read_semantics(tmp_path, session.id)[-1]
        assert review.failure_review.omitted_original_ids == (prior[0].id,)
        assert review.failure_review.eligible_examples == 0
        assert len(provider.calls) == 1 and b"outside-this-session" not in session.blobs.get(review.input)
    finally:
        kernel.session.close()


async def test_checked_output_failure_retrospective_uses_the_original_run_boundary(tmp_path):
    kernel = await kernel_for(tmp_path, FakeProvider([text_turn("OLD")]), workspace_root=tmp_path)
    try:
        kernel.tasks.create("Report the service")
        kernel.tasks.add_requirement({"id": "answer", "description": "Return the service name",
            "check": {"kind": "output", "sha256": kernel.session.blobs.put(b"NEW").sha256}})
        await kernel.loop.run_task(kernel.tasks.prepare("Which service?"))
        kernel.tasks.check()
        rows = read_session(tmp_path, kernel.session.id)
        start, = (r for r in rows if isinstance(r.event, AgentRunStarted))
        finish, = (r for r in rows if isinstance(r.event, AgentRunFinished))
        check, = (r for r in rows if isinstance(r.event, TaskChecked))
        failure, = (e for e in read_improvements(tmp_path, kernel.session.id).evidence.values()
                    if e.category == "failure" and e.source_seq == check.seq)
        message = await perform(kernel, ["review-failure", failure.id])
        assert "No matching example in this session" in message
        review = read_semantics(tmp_path, kernel.session.id)[-1].failure_review
        assert review.run_start_seq == start.seq and review.run_finish_seq == finish.seq < failure.source_seq
        assert review.failure_evidence_id == failure.id and review.eligible_examples == 0
    finally:
        kernel.session.close()


async def test_terminal_confirmation_saves_then_reviews_once_with_the_ready_local_model(tmp_path):
    import httpx
    from harness.catalog import Catalog
    from harness.resources import LocalResources
    from harness.types import ModelId
    from tests.test_tui import make_app
    from tests.test_tui_improvements import submit
    provider = FakeProvider([response()])
    provider.catalog = Catalog({"review": {"route": "openai/fixture", "api_base": "http://127.0.0.1:12345/v1",
                                         "local": {"auto_start": False}}})
    resources = LocalResources(transport=httpx.MockTransport(lambda _:
        httpx.Response(200, json={"data": [{"id": "fixture"}]})))
    app = make_app(tmp_path, model=ModelId("review"), provider=provider, resources=resources, workspace_root=tmp_path)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        session = app.kernel.session
        await resources.check(provider.catalog.resolve("review"), emit=session.append)
        record_lookup(session, run="prior")
        begin_failed_turn(session)
        finish = session.append(AgentRunFinished(result=AgentResult(task_id="bad", run_id="bad", status="completed")))
        for _ in range(2):
            await submit(app, pilot, f"/improvements failure --turn {finish.seq} The answer was incorrect.")
        rows = read_session(tmp_path, session.id)
        failure, = (r for r in rows if isinstance(r.event, ImprovementRecorded) and r.event.record.kind == "evidence")
        assessment, = (r for r in rows if isinstance(r.event, AssessmentObserved))
        assert assessment.seq > failure.seq and assessment.event.observation.failure_review.failure_evidence_id == failure.event.record.id
        assert len(provider.calls) == 1


@pytest.mark.parametrize("local", [True, False])
def test_closed_confirmation_automatically_assesses_only_local_models(tmp_path, monkeypatch, capsys, local):
    import httpx
    from harness.cli import _subcommand
    from harness.resources import LocalResources
    from harness.types import ModelId
    catalog = tmp_path / "models.toml"
    catalog.write_text('[models.review]\nroute="openai/fixture"\n' +
        ('api_base="http://127.0.0.1:12345/v1"\n[models.review.local]\nauto_start=false\n' if local else ''))
    probes = []
    def respond(req):
        probes.append(req.url)
        return httpx.Response(200, json={"data": [{"id": "fixture"}]})
    monkeypatch.setattr("harness.resources.LocalResources", lambda: LocalResources(transport=httpx.MockTransport(respond)))
    with Session(tmp_path, SessionId("closed-auto")) as session:
        session.start()
        session.append(ModelSelected(model=ModelId("review"), pinned=True))
        begin_failed_turn(session)
        finish = session.append(AgentRunFinished(result=AgentResult(task_id="bad", run_id="bad", status="completed")))
    command = ["improvements", "--base-dir", str(tmp_path), "--catalog", str(catalog), session.id,
               "--turn", str(finish.seq), "--failure", "The answer was incorrect."]
    _subcommand(command)
    rows = read_session(tmp_path, session.id)
    failure, = (r for r in rows if isinstance(r.event, ImprovementRecorded) and r.event.record.kind == "evidence")
    observations = [r for r in rows if isinstance(r.event, AssessmentObserved)]
    assert "Saved failure" in capsys.readouterr().out
    if local:
        assert probes and len(observations) == 1 and observations[0].seq > failure.seq
        assert observations[0].event.observation.failure_review.failure_evidence_id == failure.event.record.id
    else:
        assert not probes and not observations


def test_closed_review_uses_explicit_model_and_empty_scope_without_inference(tmp_path, capsys):
    from harness.improvement_journal import ImprovementJournal
    catalog = tmp_path / "models.toml"
    catalog.write_text('[models.review]\nroute="openai/review"\n')
    with Session(tmp_path, SessionId("test-retrospective")) as session:
        session.start()
        begin_failed_turn(session)
        finish = session.append(AgentRunFinished(result=AgentResult(task_id="bad", run_id="bad", status="completed"))).seq
        record_feedback(ImprovementJournal(session), "Incorrect.", turn=finish, failure=True)
    failure, = (e for e in read_improvements(tmp_path, session.id).evidence.values() if e.category == "failure")
    main(["--base-dir", str(tmp_path), "--catalog", str(catalog), "--model", "review", session.id,
          "review-failure", failure.id])
    assert "No matching example in this session" in capsys.readouterr().out
    obs, = read_semantics(tmp_path, session.id)
    assert obs.failure_review.failure_evidence_id == failure.id and obs.call_id is None
