from dataclasses import replace

import pytest

from harness.agent import AgentTask
from harness.events import ContextSourceObserved
from harness.experience_context import ExampleContext
from harness.hooks import HookBus, ProposedModelCall, Rewrite
from harness.improvement_examples import ImprovementExample
from harness.log import read_session
from harness.messages import Role
from harness.provider import FakeProvider, text_turn
from harness.types import ModelId
from tests.test_semantics import kernel_for


@pytest.mark.parametrize("redirect", [False, True])
async def test_only_the_selected_destination_receives_scoped_examples(tmp_path, monkeypatch, redirect):
    hooks = HookBus()
    if redirect:
        async def other_model(call):
            if isinstance(call, ProposedModelCall):
                return Rewrite(replace(call, model=ModelId("elsewhere")))
        hooks.register_dispatch("redirect", other_model)

    provider = FakeProvider([text_turn("draft")])
    kernel = await kernel_for(tmp_path, provider, hooks=hooks)
    try:
        record = ImprovementExample(kernel.session.id, "private former request", "inspect a source",
                                    "historical result", 1, 2, 3, 4, "source-ref")
        async def selected(loop, task, project):
            return ExampleContext("assessment", (record,), ModelId("fake"))
        monkeypatch.setattr("harness.experience_context.ordinary_examples", selected)
        await kernel.loop.run_conversation(AgentTask(prompt="current question"))
        sent = provider.calls[0]
        assert all("private former request" not in message.text() for message in sent if message.role == Role.SYSTEM)
        assert any("private former request" in message.text() for message in sent if message.role == Role.USER) != redirect
        records = [row for row in read_session(tmp_path, kernel.session.id)
                   if isinstance(row.event, ContextSourceObserved) and row.event.source_id == "supervised-examples"]
        assert bool(records) != redirect
    finally:
        kernel.session.close()
