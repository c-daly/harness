"""Optional local interpretation must yield to delivery, foreground work and teardown."""

import asyncio
import os
import signal
import sys

import httpx
import pytest

from harness.cli import _amain, run_once
from harness.events import SemanticObserved, SessionEnded, UserInterrupt
from harness.log import read_session
from harness.resources import LocalResources
from harness.types import ModelId
from tests.test_ordinary_reply_semantics import ReplyProvider, ready_kernel
from tests.test_tui import make_app
from tests.test_tui_improvements import submit
from tests.test_tui_queue import screen_text


class SlowReplyProvider(ReplyProvider):
    def __init__(self):
        super().__init__(answer="First answer.", label="correction")
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def infer(self, request):
        if (request.response_schema and "kind" in request.response_schema["properties"]
                and not self.entered.is_set()):
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        async for chunk in super().infer(request):
            yield chunk


@pytest.mark.parametrize("interrupt", [False, True])
async def test_headless_stdout_is_flushed_before_slow_semantic_drain(tmp_path, monkeypatch, interrupt):
    provider = SlowReplyProvider()
    initial = await ready_kernel(tmp_path, provider)
    sid = initial.session.id
    await run_once(initial, "Read the register.")
    resumed = await ready_kernel(tmp_path, provider, session_id=sid)
    provider.answer = "Corrected answer."
    handlers = {}
    if interrupt:
        event_loop = asyncio.get_running_loop()
        monkeypatch.setattr(event_loop, "add_signal_handler", handlers.__setitem__)
        monkeypatch.setattr(event_loop, "remove_signal_handler", handlers.pop)
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb", buffering=0) as reader, os.fdopen(write_fd, "w", buffering=8192) as stdout:
        os.set_blocking(read_fd, False)
        with monkeypatch.context() as output:
            output.setattr(sys, "stdout", stdout)
            command = asyncio.create_task(_amain(resumed, "That register is outdated."))
            try:
                await asyncio.wait_for(provider.entered.wait(), 3)
                assert reader.read(4096) == b"Corrected answer.\n"
                assert not command.done() and resumed.controller.active is None
                assert resumed.controller.last_result.status == "completed"
                if interrupt:
                    handlers[signal.SIGINT]()
                    await asyncio.wait_for(provider.cancelled.wait(), 3)
            finally:
                provider.release.set()
            assert await command == "Corrected answer."
            stdout.flush()
            assert reader.read(4096) is None
    rows = read_session(tmp_path, sid)
    observation, = [row.event.observation for row in rows if isinstance(row.event, SemanticObserved)]
    assert observation.reason == ("cancelled" if interrupt else "classified")
    assert not any(isinstance(row.event, UserInterrupt) for row in rows)
    assert isinstance(rows[-1].event, SessionEnded)


async def test_tui_delivers_and_settles_before_shadow_review_and_new_prompt_preempts_it(tmp_path):
    provider = SlowReplyProvider()
    resources = LocalResources(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"data": [{"id": "fixture"}]})))
    app = make_app(tmp_path, model=ModelId("local"), provider=provider, resources=resources)
    session = app.kernel.session
    try:
        async with app.run_test(size=(90, 30)) as pilot:
            await pilot.pause()
            await resources.check(provider.catalog.resolve("local"), emit=session.append)
            await submit(app, pilot, "Read the register.")
            provider.answer = "Corrected answer."
            await submit(app, pilot, "That register is outdated.")
            await asyncio.wait_for(provider.entered.wait(), 3)
            assert "Corrected answer." in screen_text(app)
            assert app.controller.phase == "idle" and app.controller.active is None
            assert app.controller.last_result.status == "completed"
            provider.answer = "Next answer."
            await submit(app, pilot, "Different question.")
            await asyncio.wait_for(provider.cancelled.wait(), 3)
            async with asyncio.timeout(3):
                while "Next answer." not in screen_text(app):
                    await pilot.pause(0.02)
        rows = read_session(tmp_path, session.id)
        assert any(isinstance(row.event, SemanticObserved) and row.event.observation.reason == "cancelled"
                   for row in rows)
        assert not any(isinstance(row.event, UserInterrupt) for row in rows)
        assert isinstance(rows[-1].event, SessionEnded)
    finally:
        provider.release.set()
        session.close()
