"""A normal headless follow-up reaches the supervised shadow classifier on resume."""

import asyncio
import json

import httpx
import pytest

from harness.catalog import Catalog
from harness.cli import build_kernel, run_once
from harness.events import AgentRunFinished, SemanticObserved, TaskAccepted, UserMessage
from harness.execution import ExecutionLimits
from harness.fold import fold
from harness.hooks import Allow, Ask, HookBus, ProposedModelCall
from harness.improvement_journal import read_improvements
from harness.log import read_session
from harness.provider import FakeProvider, text_turn
from harness.resources import LocalResources
from harness.types import ModelId


class ReplyProvider(FakeProvider):
    def __init__(self, *, answer, label):
        super().__init__([])
        self.answer = answer
        self.label = label
        self.classifications = []
        self.catalog = Catalog({"local": {"route": "openai/fixture", "api_base": "http://127.0.0.1:12345/v1",
                                          "local": {"auto_start": False}}})

    def infer(self, request):
        if request.response_schema is None:
            response = self.answer
        elif "kind" in request.response_schema["properties"]:
            self.classifications.append(request)
            response = json.dumps({"kind": self.label})
        else:
            assert "selected_ids" in request.response_schema["properties"]
            response = json.dumps({"selected_ids": [], "reason": "no_match"})
        self.script.append(text_turn(response))
        return super().infer(request)


async def test_remote_primary_uses_the_only_known_ready_local_alias():
    from harness.reply_semantics import _local_alias
    entries = {alias: {"route": f"openai/{alias}", "api_base": "http://127.0.0.1:12345/v1",
                       "local": {"auto_start": False}} for alias in ("local-a", "local-b")}
    catalog = Catalog(entries)
    resources = LocalResources(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": "local-b"}]})))
    try:
        assert _local_alias(catalog, "remote", resources) == (None, "ambiguous_local_model")
        await resources.check(catalog.resolve("local-b"), emit=lambda event: None)
        assert _local_alias(catalog, "remote", resources) == ("local-b", None)
        assert _local_alias(catalog, "local-a", resources) == ("local-a", None)
    finally:
        await resources.close(emit=lambda event: None)


async def ready_kernel(base, provider, *, session_id=None, **kwargs):
    resources = LocalResources(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": "fixture"}]})))
    resolved = provider.catalog.resolve("local")
    await resources.check(resolved, emit=lambda event: None)
    assert resources.snapshot(resolved).status == "ready"
    return build_kernel(base_dir=base, workspace_root=base.parent, model=ModelId("local"),
                        provider=provider, resources=resources, resume_session_id=session_id, **kwargs)


@pytest.mark.parametrize(("reply", "label", "answer"), [
    ("That is incorrect. Please check the updated desk record.", "correction", "The desk record says FINCH."),
    ("Thanks.", "acknowledgement", "You're welcome."),
    ("How many sheets do twelve badges need at four per sheet?", "question", "3"),
])
async def test_resumed_ordinary_reply_observes_only_the_followup(tmp_path, reply, label, answer):
    base = tmp_path / "sessions-dir"
    first_provider = ReplyProvider(answer="The desk record says LOFT.", label="uncertain")
    first = await ready_kernel(base, first_provider)
    session_id = first.session.id
    question = "Which destination is in the desk record?"
    assert await run_once(first, question) == first_provider.answer
    before = read_session(base, session_id)
    assert not first_provider.classifications
    assert not any(isinstance(row.event, SemanticObserved) for row in before)
    prior_finish, = [row.seq for row in before if isinstance(row.event, AgentRunFinished)]
    prior_request, = [row.seq for row in before if isinstance(row.event, UserMessage)]

    provider = ReplyProvider(answer=answer, label=label)
    resumed = await ready_kernel(base, provider, session_id=session_id)
    assert await run_once(resumed, reply) == answer
    rows = read_session(base, session_id)
    new = rows[len(before):]
    followup, = [row for row in new if isinstance(row.event, UserMessage)]
    recorded, = [row for row in new if isinstance(row.event, SemanticObserved)]
    result = recorded.event.observation
    assert followup.event.text == reply
    assert prior_finish < followup.seq < recorded.seq
    assert result.mode == "shadow" and result.kind == label
    assert result.reply_origin.request_seq == prior_request
    assert result.reply_origin.run_finish_seq == prior_finish
    assert result.reply_origin.followup_seq == followup.seq
    request, = provider.classifications
    assert request.tool_choice == "none" and not request.tools
    assert json.loads(request.messages[-1].text()) == {
        "previous_request": question, "assistant_reply": first_provider.answer, "followup": reply,
    }
    assert fold(rows).messages[-1].text() == answer
    assert not any(isinstance(row.event, TaskAccepted) for row in new)
    evidence = read_improvements(base, session_id).evidence.values()
    assert not any(item.category == "failure" for item in evidence)
    corrections = [item for item in evidence if item.category == "correction"]
    if label == "correction":
        item, = corrections
        assert item.source_seq == recorded.seq
        assert str(prior_finish) in item.observation and str(followup.seq) in item.observation
    else:
        assert not corrections


async def test_correction_to_displayed_file_uses_the_delivered_snapshot_on_resume(tmp_path):
    from harness.provider import tool_call_turn

    base = tmp_path / "sessions"
    source = tmp_path / "release.md"
    source.write_bytes(b"# Release\r\n\r\nBuild A\r\n")
    provider = FakeProvider([tool_call_turn("", "read_file", {"file_path": source.name, "display": True})])
    first = build_kernel(base_dir=base, workspace_root=tmp_path, model=ModelId("local"),
                         provider=provider, native_tools=True)
    prompt = "Show the release notes."
    delivered = await run_once(first, prompt)
    assert delivered == "# Release\r\n\r\nBuild A\r\n"
    source.write_text("Build B\n")

    reviewer = ReplyProvider(answer="I'll check the release number.", label="correction")
    resumed = await ready_kernel(base, reviewer, session_id=first.session.id)
    followup = "Those notes have the wrong build."
    await run_once(resumed, followup)
    classification, = reviewer.classifications
    assert json.loads(classification.messages[-1].text()) == {
        "previous_request": prompt, "assistant_reply": delivered, "followup": followup,
    }
    observations = [row.event.observation for row in read_session(base, first.session.id)
                    if isinstance(row.event, SemanticObserved)]
    assert any(o.kind == "correction" and o.reply_origin is not None for o in observations)


async def test_explicit_shadow_classification_does_not_attach_text_to_a_reply(tmp_path):
    base = tmp_path / "sessions-dir"
    provider = ReplyProvider(answer="Unchanged.", label="correction")
    kernel = await ready_kernel(base, provider)
    await kernel.loop.start()
    try:
        await kernel.loop.run_turn("Check with the desk.")
        observed = await kernel.semantics.interpret("That answer was wrong.", model=ModelId("local"))
        assert observed.kind == "correction" and observed.reply_origin is None
        assert provider.classifications[0].messages[-1].text() == "That answer was wrong."
        evidence = read_improvements(base, kernel.session.id).evidence.values()
        assert all(item.category not in {"correction", "failure"} for item in evidence)
    finally:
        await kernel.resources.close(emit=kernel.session.append)
        await kernel.loop.end()
        kernel.session.close()


@pytest.mark.parametrize("abstention", ["budget", "denied"])
async def test_foreground_reply_completes_when_shadow_work_lacks_authority(tmp_path, abstention):
    base = tmp_path / "sessions-dir"
    provider = ReplyProvider(answer="Desk report.", label="correction")
    hooks = HookBus()
    if abstention == "denied":
        def confirm_pinned_call(action):
            if isinstance(action, ProposedModelCall) and action.pinned:
                return Ask(reason="Extra inference needs approval")
            return Allow()
        hooks.register_dispatch("approval", confirm_pinned_call)
    limits = ExecutionLimits(max_model_calls=2) if abstention == "budget" else ExecutionLimits()
    kernel = await ready_kernel(base, provider, execution_limits=limits, hooks=hooks)
    await kernel.loop.start()
    try:
        assert await kernel.loop.run_turn("What did the desk say?") == "Desk report."
        assert await kernel.loop.run_turn("That account is incomplete.") == "Desk report."
        rows = read_session(base, kernel.session.id)
        result, = [row.event.observation for row in rows if isinstance(row.event, SemanticObserved)]
        assert result.reply_origin is not None and result.status == "abstained"
        assert result.reason == abstention and not provider.classifications
        assert not any(row.event.type == "permission_requested" for row in rows)
        evidence = read_improvements(base, kernel.session.id).evidence.values()
        assert all(item.category not in {"correction", "failure"} for item in evidence)
    finally:
        await kernel.resources.close(emit=kernel.session.append)
        await kernel.loop.end()
        kernel.session.close()


async def test_queued_terminal_input_cannot_review_an_answer_that_was_still_running(tmp_path):
    from tests.test_tui import make_app
    from tests.test_tui_improvements import submit

    entered, release = asyncio.Event(), asyncio.Event()

    class GatedProvider(ReplyProvider):
        async def infer(self, request):
            if not self.calls and request.response_schema is None:
                entered.set()
                await release.wait()
            async for chunk in super().infer(request):
                yield chunk

    provider = GatedProvider(answer="Desk report.", label="correction")
    resources = LocalResources(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": "fixture"}]})))
    app = make_app(tmp_path, model=ModelId("local"), provider=provider, resources=resources)
    session = app.kernel.session
    try:
        async with app.run_test(size=(90, 30)) as pilot:
            await pilot.pause()
            await resources.check(provider.catalog.resolve("local"), emit=session.append)
            await submit(app, pilot, "Read the desk record.")
            await asyncio.wait_for(entered.wait(), 3)
            await submit(app, pilot, "Check the date too.")
            assert [item.text for item in app.controller.pending] == ["Check the date too."]
            release.set()
            async with asyncio.timeout(4):
                while app.controller.pending or app.controller.active:
                    await pilot.pause(0.02)
            assert not provider.classifications
            await submit(app, pilot, "That date is wrong.")
            async with asyncio.timeout(4):
                while app.controller.pending or app.controller.active:
                    await pilot.pause(0.02)
        rows = read_session(tmp_path, session.id)
        first, second, followup = [r.seq for r in rows if isinstance(r.event, UserMessage)]
        observed, = [r.event.observation for r in rows if isinstance(r.event, SemanticObserved)]
        assert observed.reply_origin.request_seq == second and second > first
        assert observed.reply_origin.followup_seq == followup
        request, = provider.classifications
        assert json.loads(request.messages[-1].text())["previous_request"] == "Check the date too."
    finally:
        release.set()
        session.close()
