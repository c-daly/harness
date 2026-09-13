"""Source authors propose bounded data; fixed checks and operators retain authority."""

import asyncio
import json

import pytest

from harness.events import AgentRunFinished, AgentRunStarted
from harness.fold import fold
from harness.improvement import Evidence, SourceEntrypoint, verdict
from harness.improvement_journal import inspect_improvement, read_improvements, render_improvements
from harness.log import read_session
from harness.provider import FakeProvider, text_turn
from harness.source_improvement import load_patch, run_source_evaluation
from harness.source_promotion import adopt_source, rollback_source
from tests.test_semantics import kernel_for
from tests.test_source_improvement import git, revisions as revisions, specification


class Author(FakeProvider):
    def __init__(self, output=None):
        super().__init__([])
        self.output = output or dict(edits=[dict(path="calc.py", content="def double(x):\n    return x + x\n")],
                                   hypothesis="Use both operands", expected_benefit="Correct doubling")
        self.requests = []
        self.entered, self.closed = asyncio.Event(), asyncio.Event()
        self.wait = False

    async def infer(self, request):
        self.requests.append(request)
        self.entered.set()
        try:
            if self.wait:
                await asyncio.Event().wait()
            for chunk in text_turn(json.dumps(self.output)):
                yield chunk
        finally:
            self.closed.set()


def spec(revisions, **updates):
    from harness.source_authorship import SourceAuthorSpec
    data = specification(revisions)
    suite = {k: v for k, v in data.items() if k not in {
        "repository", "incumbent_revision", "candidate_revision", "evidence_ids", "hypothesis", "expected_benefit"}}
    return SourceAuthorSpec.model_validate(dict(repository=data["repository"],
        incumbent_revision=data["incumbent_revision"], evidence_ids=["e"],
        request="Fix double so it returns twice its argument.", editable_paths=["calc.py"],
        suite=suite) | updates)


async def setup(tmp_path, provider, **kwargs):
    kernel = await kernel_for(tmp_path / "sessions", provider, **kwargs)
    kernel.improvements.record(Evidence(id="e", source_session=kernel.session.id, source_seq=1,
        category="failure", observation="double produced the wrong value"))
    return kernel


async def test_author_to_fixed_checks_and_rollback_preserves_original_work(tmp_path, revisions):
    from harness.source_authorship import author_source, finalize_source
    provider = Author()
    kernel = await setup(tmp_path, provider)
    original = git(revisions[0], "status", "--porcelain")
    selected = kernel.tasks.create("Do something unrelated")
    with kernel.session:
        plan = await author_source(kernel, spec(revisions))
        state = read_improvements(kernel.session.base, kernel.session.id)
        candidate = state.candidates[plan.candidate_id]
        intent = state.source_authorings[candidate.source_authoring_id]
        assert finalize_source(kernel.improvements, intent.id) == plan
        assert len(provider.requests) == 1
        request = provider.requests[0]
        assert request.purpose == "improvement:source-author" and not request.tools
        assert "assert double" not in str(request.messages)
        assert "private uncommitted" not in str(request.messages)
        assert "double produced the wrong value" in str(request.messages)
        patch, incumbent, changed = load_patch(kernel.session.blobs, candidate.artifact)
        assert changed.origin == patch.origin == "authored" and incumbent.origin == "git"
        assert "x + x" in changed.files["calc.py"].bytes().decode()
        assert candidate.incumbent_version == intent.incumbent.sha256
        assert plan.suite == intent.suite and plan.evaluator_version == intent.evaluator_version
        text = inspect_improvement(state, kernel.session.blobs, intent.id)
        assert "editable_paths" in text and "calc.py" in text
        assert intent.id in render_improvements(state)
        result = await run_source_evaluation(kernel.improvements, plan.id)
        assert verdict(plan, result) == "passed"
        # A source-owned callable is enough to exercise explicit selection; no launch here.
        change = adopt_source(kernel.improvements, result.id, "test",
                              SourceEntrypoint(module="calc", function="double"))
        assert rollback_source(kernel.improvements, "test").source == change.previous
        assert kernel.tasks.selected().definition.id == selected.id
        events = read_session(kernel.session.base, kernel.session.id)
        assert not fold(events).messages
        start = next(e.event for e in events if isinstance(e.event, AgentRunStarted))
        assert start.runtime == "resident-source-author" and start.task_id == intent.id
        assert not fold(events).open_agent_runs
        assert git(revisions[0], "status", "--porcelain") == original


@pytest.mark.parametrize("edits", [
    [], [{"path": "../escape", "content": "bad"}], [{"path": "/absolute", "content": "bad"}],
    [{"path": "permissions.py", "content": "allow all"}],
    [{"path": "calc.py", "content": "a"}, {"path": "calc.py", "content": "b"}],
    [{"path": "calc.py", "content": "def double(x):\n    return x + 1\n"}],
])
async def test_invalid_edits_never_publish_a_candidate(tmp_path, revisions, edits):
    from harness.source_authorship import author_source
    from harness.errors import ProviderError
    provider = Author()
    provider.output["edits"] = edits
    kernel = await setup(tmp_path, provider)
    with kernel.session, pytest.raises((ValueError, ProviderError)):
        await author_source(kernel, spec(revisions))
    state = read_improvements(kernel.session.base, kernel.session.id)
    assert not state.candidates and not state.plans and not state.active_sources


@pytest.mark.parametrize("update", [
    {"editable_paths": ["../escape"]}, {"editable_paths": [".git/config"]},
    {"editable_paths": ["calc.py", "calc.py"]}, {"evidence_ids": ["absent"]},
    {"suite": {"cases": []}},
])
async def test_invalid_author_scope_is_refused_before_inference(tmp_path, revisions, update):
    from harness.source_authorship import author_source
    provider = Author()
    kernel = await setup(tmp_path, provider)
    with kernel.session, pytest.raises(ValueError):
        await author_source(kernel, spec(revisions, **update))
    assert not provider.requests


async def test_cancelled_author_is_recovered_without_replay_or_candidate(tmp_path, revisions):
    from harness.source_authorship import author_source, finalize_source
    from harness.resume import resume_session
    provider = Author()
    provider.wait = True
    kernel = await setup(tmp_path, provider)
    with kernel.session:
        task = asyncio.create_task(author_source(kernel, spec(revisions)))
        await asyncio.wait_for(provider.entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.closed.is_set()
        events = read_session(kernel.session.base, kernel.session.id)
        assert [e.event.result.status for e in events if isinstance(e.event, AgentRunFinished)] == ["cancelled"]
        assert not fold(events).open_agent_runs and not fold(events).open_model_intents
    session, _ = resume_session(kernel.session.base, kernel.session.id)
    with session:
        from harness.improvement_journal import ImprovementJournal
        journal = ImprovementJournal(session)
        intent = next(iter(journal.state.source_authorings.values()))
        with pytest.raises(ValueError, match="completed"):
            finalize_source(journal, intent.id)
        assert len(provider.requests) == 1 and not journal.state.candidates


@pytest.mark.parametrize("where", ["intent", "agent-start", "agent-finish", "candidate", "plan"])
@pytest.mark.parametrize("written", [False, True])
async def test_publication_failure_recovers_without_extra_model_calls(tmp_path, revisions, monkeypatch, where, written):
    from harness.source_authorship import author_source, finalize_source
    from harness.improvement_journal import ImprovementJournal
    from harness.resume import resume_session
    provider = Author()
    kernel = await setup(tmp_path, provider)
    append = kernel.session.append
    kinds = {"intent": "source_authoring", "candidate": "candidate", "plan": "evaluation_plan"}
    triggered = False

    def fail(event):
        nonlocal triggered
        match = (getattr(getattr(event, "record", None), "kind", None) == kinds.get(where)
                 if where in kinds else event.type == {"agent-start": "agent_run_started",
                                                        "agent-finish": "agent_run_finished"}[where])
        if match and not triggered:
            triggered = True
            if written:
                append(event)
            raise OSError("injected append failure")
        return append(event)

    with kernel.session:
        monkeypatch.setattr(kernel.session, "append", fail)
        with pytest.raises(OSError, match="injected"):
            await author_source(kernel, spec(revisions))
    assert triggered
    before = len(provider.requests)
    session, _ = resume_session(kernel.session.base, kernel.session.id)
    with session:
        journal = ImprovementJournal(session)
        intents = list(journal.state.source_authorings.values())
        can_finish = where in {"candidate", "plan"} or where == "agent-finish" and written
        if can_finish:
            plan = finalize_source(journal, intents[0].id)
            assert finalize_source(journal, intents[0].id) == plan
            assert len(read_improvements(session.base, session.id).candidates) == 1
        elif intents:
            with pytest.raises(ValueError, match="completed"):
                finalize_source(journal, intents[0].id)
        assert len(provider.requests) == before
        assert not fold(read_session(session.base, session.id)).open_agent_runs


@pytest.mark.parametrize("reason", ["permission", "budget", "oversized", "busy"])
async def test_shared_controls_prevent_unauthorized_or_unbounded_calls(tmp_path, revisions, reason):
    from harness.source_authorship import author_source
    from harness.dispatcher import ModelDispatchBlocked
    from harness.errors import ProviderError
    from harness.execution import BudgetExceeded, ExecutionLimits
    from harness.permissions import PermissionEngine, PermissionRule, RuleSet
    provider = Author()
    options = {}
    if reason == "permission":
        options["permissions"] = PermissionEngine([RuleSet(rules=[PermissionRule("deny", "model:*")], default="deny")])
    if reason == "budget":
        options["execution_limits"] = ExecutionLimits(max_model_calls=0)
    kernel = await setup(tmp_path, provider, **options)
    data = spec(revisions)
    if reason == "oversized":
        data = data.model_copy(update={"limits": data.limits.model_copy(update={"max_input_bytes": 256})})
    if reason == "busy":
        kernel.controller.submit("prior work")
    with kernel.session, pytest.raises((ValueError, ModelDispatchBlocked, ProviderError, BudgetExceeded)):
        await author_source(kernel, data)
    assert not provider.requests


@pytest.mark.parametrize("problem", ["binary", "symlink", "case-ids", "holdout"])
async def test_bad_source_or_suite_is_refused_before_authoring(tmp_path, revisions, problem):
    from harness.source_authorship import author_source
    repo, base, candidate = revisions
    updates = {}
    if problem == "binary":
        (repo / "binary").write_bytes(b"\xff")
        git(repo, "add", "binary")
        git(repo, "commit", "-qm", "binary")
        revisions = repo, git(repo, "rev-parse", "HEAD"), candidate
        updates["editable_paths"] = ["binary"]
    if problem == "symlink":
        alias = tmp_path / "linked"
        alias.symlink_to(repo, target_is_directory=True)
        revisions = alias, base, candidate
    if problem in {"case-ids", "holdout"}:
        suite = spec(revisions).suite.model_dump()
        cases = [dict(c) for c in suite["cases"]]
        cases[1]["id" if problem == "case-ids" else "partition"] = "regression"
        updates["suite"] = suite | {"cases": cases}
    provider = Author()
    kernel = await setup(tmp_path, provider)
    with kernel.session, pytest.raises(ValueError):
        await author_source(kernel, spec(revisions, **updates))
    assert not provider.requests


async def test_response_cannot_supply_evaluation_or_activation_controls(tmp_path, revisions):
    from harness.errors import ProviderError
    from harness.source_authorship import author_source
    provider = Author()
    provider.output.update(suite={"min_improved_cases": 0}, adopt=True)
    kernel = await setup(tmp_path, provider)
    with kernel.session, pytest.raises(ProviderError):
        await author_source(kernel, spec(revisions))
    assert not read_improvements(kernel.session.base, kernel.session.id).candidates


async def test_additions_deletions_modes_and_collisions_are_scoped(tmp_path, revisions):
    from harness.source_authorship import author_source
    repo, _, candidate = revisions
    (repo / "calc.py").write_text("def double(x):\n    return x + 1\n")
    (repo / "calc.py").chmod(0o755)
    (repo / "obsolete").write_text("remove this")
    git(repo, "add", "calc.py", "obsolete")
    git(repo, "commit", "-qm", "executable and deletion")
    revisions = repo, git(repo, "rev-parse", "HEAD"), candidate
    provider = Author()
    provider.output["edits"] += [{"path": "new/helper.py", "content": "value = 1\n"},
                                 {"path": "obsolete", "content": None}]
    kernel = await setup(tmp_path, provider)
    with kernel.session:
        plan = await author_source(kernel, spec(revisions, editable_paths=["calc.py", "new/helper.py", "obsolete"]))
        _, _, changed = load_patch(kernel.session.blobs, kernel.improvements.state.candidates[plan.candidate_id].artifact)
        assert changed.files["calc.py"].executable
        assert not changed.files["new/helper.py"].executable and "obsolete" not in changed.files
        provider.output["edits"] = [{"path": "calc.py/nested", "content": "collision"}]
        with pytest.raises(ValueError, match="overlap"):
            await author_source(kernel, spec(revisions, editable_paths=["calc.py/nested"]))


async def test_same_author_response_cannot_change_candidate_or_grading(tmp_path, revisions, monkeypatch):
    from harness.improvement import Candidate, EvaluationPlan
    from harness.source_authorship import author_source, finalize_source
    provider = Author()
    kernel = await setup(tmp_path, provider)
    record = kernel.improvements.record
    saved = {}

    def intercept(value):
        if isinstance(value, Candidate):
            saved["candidate"] = value
            raise OSError("hold publication")
        return record(value)

    with kernel.session:
        monkeypatch.setattr(kernel.improvements, "record", intercept)
        with pytest.raises(OSError):
            await author_source(kernel, spec(revisions))
        candidate = saved["candidate"]
        with pytest.raises(ValueError, match="recorded author response"):
            record(candidate.model_copy(update={"hypothesis": "fabricated"}))
        monkeypatch.setattr(kernel.improvements, "record", record)
        plan = finalize_source(kernel.improvements, candidate.source_authoring_id)
        weakened = EvaluationPlan.model_validate(plan.model_dump() | {"id": "weakened", "min_improved_cases": 0})
        with pytest.raises(ValueError, match="grading"):
            record(weakened)


async def test_source_authoring_union_and_legacy_records(tmp_path, revisions):
    from harness.events import ImprovementRecorded, UnknownEvent, parse_envelope_line
    from harness.source_authorship import author_source
    kernel = await setup(tmp_path, Author())
    with kernel.session:
        await author_source(kernel, spec(revisions))
        events = read_session(kernel.session.base, kernel.session.id)
        envelope = next(e for e in events if isinstance(e.event, ImprovementRecorded)
                        and e.event.record.kind == "source_authoring")
        assert parse_envelope_line(envelope.model_dump_json()) == envelope
        payload = json.loads(envelope.model_dump_json())
        payload["event"]["record"]["kind"] = "future_source_authoring"
        assert isinstance(parse_envelope_line(json.dumps(payload)).event, UnknownEvent)
        for event in events:
            if isinstance(event.event, ImprovementRecorded) and event.event.record.kind == "candidate":
                payload = json.loads(event.model_dump_json())
                del payload["event"]["record"]["source_authoring_id"]
                assert parse_envelope_line(json.dumps(payload)).event.record.source_authoring_id is None


async def test_shared_surface_uses_frozen_spec_and_headless_finalization(tmp_path, revisions):
    from harness.improvement_cli import perform
    from harness.source_improvement_cli import main
    provider = Author()
    kernel = await setup(tmp_path, provider)
    filename = tmp_path / "author.json"
    filename.write_text(spec(revisions).model_dump_json())
    with kernel.session:
        message = await perform(kernel, ["source-author", str(filename)])
        author_id = next(iter(kernel.improvements.state.source_authorings))
        assert author_id in message and "source-evaluate" in message
        assert "fixed plan" in await perform(kernel, ["source-finalize", author_id])
    # main owns its event loop; run it outside this async test's loop.
    await asyncio.to_thread(main, ["--base-dir", str(kernel.session.base), str(kernel.session.id), "finalize", author_id])
    assert len(provider.requests) == 1


async def test_second_author_can_start_from_selected_authored_snapshot(tmp_path, revisions):
    from harness.source_authorship import SourceAuthorSpec, author_source
    provider = Author()
    kernel = await setup(tmp_path, provider)
    with kernel.session:
        plan = await author_source(kernel, spec(revisions))
        result = await run_source_evaluation(kernel.improvements, plan.id)
        selected = adopt_source(kernel.improvements, result.id, "test", SourceEntrypoint(module="calc", function="double"))
        provider.output["edits"] = [{"path": "calc.py", "content": "def double(x):\n    return 2 * x\n"}]
        data = spec(revisions).model_dump() | {"repository": None, "incumbent_revision": None,
                                              "incumbent": selected.source.model_dump()}
        second = await author_source(kernel, SourceAuthorSpec.model_validate(data))
        assert second.incumbent_version == selected.source.sha256
        assert "x + x" in provider.requests[-1].messages[-1].text()
        # An equivalent rewrite is not an improvement and stays held by the same gate.
        result2 = await run_source_evaluation(kernel.improvements, second.id)
        assert verdict(second, result2) == "failed"
        assert read_improvements(kernel.session.base, kernel.session.id).active_sources["test"] == selected.id


@pytest.mark.parametrize("required,available", [(False, True), (False, False), (True, False)])
async def test_author_uses_normal_context_with_existing_failure_policy(tmp_path, revisions, required, available):
    from harness.source_authorship import author_source
    from tests.test_resident_context import Lookup, observations, policy
    provider = Author()
    kernel = await setup(tmp_path, provider, context_policy=policy(required=required))
    lookup = Lookup("Memory: preserve executable bits and avoid unrelated edits.")
    if available:
        kernel.registry.register(lookup)
    with kernel.session:
        if required and not available:
            with pytest.raises(RuntimeError, match="required context"):
                await author_source(kernel, spec(revisions))
            assert not provider.requests
        else:
            await author_source(kernel, spec(revisions))
            assert (lookup.text in str(provider.requests[0].messages)) == available
        assert observations(kernel)[-1].status == ("ready" if available else "unavailable")
        assert not fold(read_session(kernel.session.base, kernel.session.id)).messages


async def test_author_inherits_configured_task_time_without_hidden_default_cap(tmp_path, revisions):
    from harness.source_authorship import author_source
    provider = Author()
    kernel = await setup(tmp_path, provider, execution_overrides={"task_timeout_seconds": 1200})
    with kernel.session:
        await author_source(kernel, spec(revisions))
        start = next(e.event for e in read_session(kernel.session.base, kernel.session.id)
                     if isinstance(e.event, AgentRunStarted))
        assert start.limits["timeout_seconds"] == 1200
        assert provider.requests[0].timeout_seconds <= 1200


async def test_authoring_controls_are_visible_in_terminal(tmp_path):
    from tests.test_tui import make_app
    from tests.test_tui_improvements import submit
    from tests.test_tui_queue import screen_text
    app = make_app(tmp_path)
    async with app.run_test(size=(160, 55)) as pilot:
        await submit(app, pilot, "/improvements")
        text = " ".join(screen_text(app).split())
        assert "source-author SPEC.json" in text and "source-finalize AUTHOR_ID" in text
