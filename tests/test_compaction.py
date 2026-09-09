"""Recovery after switching long conversations to a smaller inference model."""

import asyncio
import json

import pytest
from textual.widgets import Input, Static

from harness.blobs import MissingBlobError
from harness.catalog import Catalog
from harness.cli import build_kernel
from harness.compaction import CompactionLimits
from harness.dispatcher import ModelDispatchBlocked
from harness.errors import ContextOverflow
from harness.events import CompactionApplied, ModelCallCompleted, ToolCallCompleted, ToolCallProposed, UserMessage
from harness.fold import fold
from harness.inference import input_bytes
from harness.log import read_session
from harness.messages import ImageBlock, Message
from harness.provider import FakeProvider, StreamStop, TextDelta, text_turn
from harness.provider_litellm import CatalogProvider
from harness.types import CallId, ModelId, ToolName
from tests.test_tui import make_app
from tests.test_tui_queue import screen_text


async def test_long_history_compacts_within_small_model_window(tmp_path, monkeypatch):
    calls = []

    async def infer(self, request):
        # A real provider rejects an oversized request after the switch too.
        input_bytes(request.messages, request.tools, limit=4096)
        calls.append(request)
        yield TextDelta("Project codename: amberfern. Outstanding: test the fix.")
        yield StreamStop("end_turn")

    monkeypatch.setattr(CatalogProvider, "infer", infer)
    provider = CatalogProvider(Catalog({"small": {"route": "openai/small", "max_input_tokens": 4096}}))
    app = make_app(tmp_path, provider=provider, model=ModelId("small"), model_pinned=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        for n in range(8):
            text = f"Turn {n}: " + "project detail " * 180
            app.kernel.session.append(UserMessage(text=text))
            app.kernel.loop.history.append(Message.user_text(text))
        await app._run_compact_body()
        state = fold(read_session(tmp_path, app.kernel.session.id))
        assert len(calls) > 1
        assert len(state.messages) == 1 and state.messages == app.kernel.loop.history
        assert "amberfern" in state.messages[0].text()


@pytest.fixture
async def kernel(tmp_path):
    kernel = build_kernel(base_dir=tmp_path, provider=FakeProvider([text_turn("summary")]),
                          model=ModelId("fake"))
    await kernel.loop.start()
    try:
        yield kernel
    finally:
        await kernel.loop.end()
        kernel.session.close()


def events(kernel):
    return read_session(kernel.session.base, kernel.session.id)


def seed(kernel, text):
    kernel.session.append(UserMessage(text=text))
    kernel.loop.history.append(Message.user_text(text))


async def test_fragments_cover_unicode_and_tool_sidecars_without_live_tool_messages(kernel):
    calls = []

    class Recorder:
        async def infer(self, request):
            calls.append(request)
            yield TextDelta('Keep "αβ" and pending work.')
            yield StreamStop("end_turn")

    kernel.set_provider(Recorder())
    seed(kernel, 'BEGIN "αβ🦉" ' * 400)
    blob = kernel.session.blobs.put(('tool evidence "\n🦉" ' * 400).encode())
    kernel.session.append(ToolCallProposed(call_id=CallId("read"), tool=ToolName("read_file"),
                                           args={"path": "notes.md"}))
    kernel.session.append(ToolCallCompleted(call_id=CallId("read"), result_blob=blob))
    seed(kernel, "END exact path /project/next.py")
    before = fold(events(kernel))
    result = await kernel.compaction.compact(limits=CompactionLimits(request_bytes=4096))
    assert result.parts == len(calls) > 1
    source = "".join(c.messages[-1].text().split("\n\nTranscript fragment ", 1)[1]
                     .split(":\n", 1)[1] for c in calls)
    recovered = [Message.model_validate(m) for m in json.loads(source)]
    assert recovered[0] == before.messages[0] and recovered[-1] == before.messages[-1]
    assert recovered[-2].blocks[0].text == kernel.session.blobs.get(blob).decode()
    for call in calls:
        assert call.tools == () and not any(m.tool_calls() for m in call.messages)
        input_bytes(call.messages, (), limit=4096)
    assert 'Keep "αβ"' in calls[1].messages[-1].text()
    assert fold(events(kernel)).messages == kernel.loop.history
    assert len(kernel.loop.history) == 1
    assert kernel.session.blobs.get(blob)
    assert len([e for e in events(kernel) if e.event.type == "compaction_applied"]) == 1


@pytest.mark.parametrize("failure", ["raise", "empty", "incomplete", "oversized"])
async def test_later_failure_keeps_original_history(kernel, failure):
    class FailsLater:
        calls = 0

        async def infer(self, request):
            self.calls += 1
            if self.calls == 2 and failure == "raise":
                raise RuntimeError("provider failed")
            text = "first summary"
            stop = "end_turn"
            if self.calls == 2:
                text = {"empty": "  ", "incomplete": "partial", "oversized": "s" * 1500}[failure]
                stop = "max_tokens" if failure == "incomplete" else "end_turn"
            yield TextDelta(text)
            yield StreamStop(stop)

    provider = FailsLater()
    kernel.set_provider(provider)
    seed(kernel, "long history " * 1000)
    before = list(kernel.loop.history)
    with pytest.raises((ValueError, RuntimeError)):
        await kernel.compaction.compact(limits=CompactionLimits(request_bytes=4096))
    assert provider.calls == 2
    assert kernel.loop.history == before == fold(events(kernel)).messages
    assert not fold(events(kernel)).open_model_intents
    assert not any(e.event.type == "compaction_applied" for e in events(kernel))


@pytest.mark.parametrize("timeout", [False, True])
async def test_cancellation_and_whole_operation_timeout_close_later_stream(kernel, timeout):
    entered, closed = asyncio.Event(), asyncio.Event()

    class Gate:
        calls = 0

        async def infer(self, request):
            self.calls += 1
            try:
                if self.calls == 2:
                    entered.set()
                    await asyncio.Event().wait()
                yield TextDelta("first summary")
                yield StreamStop("end_turn")
            finally:
                if self.calls == 2:
                    closed.set()

    kernel.set_provider(Gate())
    seed(kernel, "old transcript " * 500)
    before = list(kernel.loop.history)
    task = asyncio.create_task(kernel.compaction.compact(limits=CompactionLimits(
        request_bytes=4096, timeout_seconds=1 if timeout else 30,
    )))
    await asyncio.wait_for(entered.wait(), 5)
    if not timeout:
        task.cancel()
    with pytest.raises(TimeoutError if timeout else asyncio.CancelledError,
                       match="compaction timed out" if timeout else None):
        await task
    assert closed.is_set()
    assert kernel.loop.history == before == fold(events(kernel)).messages
    assert not fold(events(kernel)).open_model_intents
    assert not any(e.event.type == "compaction_applied" for e in events(kernel))


@pytest.mark.parametrize("case", ["parts", "prefix", "source", "image", "blob"])
async def test_preparation_failure_makes_no_model_calls(kernel, case):
    seed(kernel, "old history " * 500)
    limits = CompactionLimits(request_bytes=4096, max_parts=1) if case == "parts" else CompactionLimits()
    if case == "prefix":
        kernel.loop.system_prompt = "p" * 32768
    elif case == "source":
        limits = CompactionLimits(source_bytes=1024)
    elif case == "image":
        image = ImageBlock(media_type="image/png", blob=kernel.session.blobs.put(b"png"))
        kernel.session.append(ModelCallCompleted(call_id=CallId("image"), model=ModelId("fake"),
            message=Message(role="assistant", blocks=(image,)).model_dump(), usage={}))
    elif case == "blob":
        from harness.blobs import BlobRef
        kernel.session.append(ToolCallProposed(call_id=CallId("missing"), tool=ToolName("read_file"), args={}))
        kernel.session.append(ToolCallCompleted(call_id=CallId("missing"),
            result_blob=BlobRef(sha256="a" * 64, size=5)))
    before = fold(events(kernel)).messages
    kernel.loop.history = list(before)
    with pytest.raises((ValueError, ContextOverflow, MissingBlobError)):
        await kernel.compaction.compact(limits=limits)
    assert kernel.provider.calls == []
    assert kernel.loop.history == before == fold(events(kernel)).messages


async def test_explicit_inference_alias_does_not_change_selected_agent(kernel, tmp_path, monkeypatch):
    catalog = tmp_path / "models.toml"
    catalog.write_text('[models.agent]\nbackend="codex"\nroute="codex/default"\n'
                       '[models.small]\nroute="openai/small"\nmax_input_tokens=4096\n')
    provider = CatalogProvider(Catalog.load(catalog))
    kernel.set_provider(provider)
    kernel.loop.model = kernel.runner.default_model = ModelId("agent")
    kernel.loop.model_pinned = False
    seed(kernel, "retain task")
    before_selection = fold(events(kernel)).model_selection
    calls = []

    async def infer(self, request):
        calls.append(request)
        yield TextDelta("retain task")
        yield StreamStop("end_turn")

    monkeypatch.setattr(CatalogProvider, "infer", infer)
    with pytest.raises(ValueError, match="is an agent"):
        await kernel.compaction.compact()
    result = await kernel.compaction.compact("small", catalog_path=catalog)
    assert result.model == "small" and calls[0].model == "small"
    assert kernel.loop.model == kernel.runner.default_model == "agent"
    assert kernel.provider is kernel.loop.provider is kernel.runner.provider is provider
    assert not kernel.loop.model_pinned
    assert fold(events(kernel)).model_selection == before_selection
    assert next(e.event for e in events(kernel) if e.event.type == "compaction_applied").model == "small"


async def test_hooks_cannot_redirect_compaction_outside_planned_model(kernel):
    from dataclasses import replace
    from harness.hooks import ProposedModelCall, Rewrite

    def redirect(call):
        return Rewrite(replace(call, model=ModelId("other"))) if isinstance(call, ProposedModelCall) else None

    kernel.hooks.register_dispatch("redirect", redirect)
    seed(kernel, "keep this")
    with pytest.raises(ModelDispatchBlocked, match="model"):
        await kernel.compaction.compact()
    assert kernel.provider.calls == []
    assert len(fold(events(kernel)).messages) == 1
    assert not any(e.event.type == "compaction_applied" for e in events(kernel))


async def test_conversation_changes_are_not_overwritten(kernel):
    class Concurrent:
        async def infer(self, request):
            seed(kernel, "arrived during compaction")
            yield TextDelta("summary")
            yield StreamStop("end_turn")

    kernel.set_provider(Concurrent())
    seed(kernel, "first")
    with pytest.raises(ValueError, match="conversation changed"):
        await kernel.compaction.compact()
    assert [m.text() for m in kernel.loop.history] == ["first", "arrived during compaction"]
    assert kernel.loop.history == fold(events(kernel)).messages


async def test_prior_partial_compaction_sequence_order_is_fully_replaced(kernel):
    for value in ("first", "second", "third"):
        seed(kernel, value)
    second_seq = fold(events(kernel))._msg_seqs[1]
    kernel.session.append(CompactionApplied(from_seq=second_seq, to_seq=second_seq,
                                          summary="second summary", model=ModelId("fake")))
    state = fold(events(kernel))
    assert state._msg_seqs[0] > state._msg_seqs[-1]
    kernel.loop.history = state.messages
    await kernel.compaction.compact()
    assert len(fold(events(kernel)).messages) == 1
    assert kernel.loop.history == fold(events(kernel)).messages


async def test_progress_is_visible_and_cancellation_preserves_draft(tmp_path):
    entered = asyncio.Event()

    class Gate:
        async def infer(self, request):
            entered.set()
            await asyncio.Event().wait()
            yield TextDelta("unused")

    app = make_app(tmp_path, provider=Gate())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        seed(app.kernel, "conversation " * 3000)
        composer = app.query_one("#prompt", Input)
        composer.value = "/compact"
        await pilot.press("enter")
        await asyncio.wait_for(entered.wait(), 5)
        composer.value = "unsent draft"
        await pilot.pause(0.1)
        assert "Compacting 1/" in screen_text(app) and "Esc cancels" in screen_text(app)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert "compact cancelled" in screen_text(app)
        assert composer.value == "unsent draft"
        assert not app.query_one("#compact-progress", Static).display
        assert not any(e.event.type == "compaction_applied" for e in events(app.kernel))


async def test_tools_explains_agent_invocation_and_shows_panel_details(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        composer = app.query_one("#prompt", Input)
        composer.value = "/tools consult_panel"
        await pilot.press("enter")
        await pilot.pause(0.1)
        screen = " ".join(screen_text(app).split())
        assert "ask for them in ordinary language" in screen
        assert "not slash commands" in screen
        assert "consult_panel:" in screen and "Parameters:" in screen
        assert not any(e.event.type in ("model_call_proposed", "tool_call_proposed") for e in events(app.kernel))
