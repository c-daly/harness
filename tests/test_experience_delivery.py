import pytest

from harness.events import ModelCallCompleted
from harness.experience_context import ExampleContext
from harness.fold import fold
from harness.improvement_examples import ImprovementExample
from harness.log import read_session
from harness.provider import FakeProvider, TextDelta, text_turn, tool_call_turn
from harness.tools import ToolSpec
from tests.test_semantics import kernel_for
from tests.test_task_evidence import add_output, add_tool


@pytest.fixture
def selected_context(monkeypatch):
    async def selected(loop, task, project):
        example = ImprovementExample(loop.session.id, "Earlier question", "Prior check", "Historical answer",
                                     1, 2, 3, 4, "source-ref")
        return ExampleContext("assessment", (example,), loop.model)
    monkeypatch.setattr("harness.experience_context.ordinary_examples", selected)


@pytest.mark.parametrize("case, verified", [
    ("no_check", False), ("failed_tool", False), ("unrelated_tool", False),
    ("tool_only", False), ("wrong_output", False), ("all_pass", True), ("output_only", True),
])
async def test_current_native_checks_gate_previous_examples(tmp_path, selected_context, case, verified):
    scripted = []
    if case in {"failed_tool", "unrelated_tool", "all_pass", "tool_only"}:
        args = {"target": "elsewhere" if case == "unrelated_tool" else "fixture"}
        scripted.append(tool_call_turn("Unverified intermediate guess", "verify", args))
    draft = "changed" if case == "wrong_output" else "done"
    scripted.append(text_turn(draft))
    kernel = await kernel_for(tmp_path, FakeProvider(scripted))

    class Verify:
        spec = ToolSpec("verify", "Read a test fixture", {"type": "object"})

        async def __call__(self, args):
            if case == "failed_tool":
                raise ValueError("fixture unavailable")
            return "PASS"

    kernel.registry.register(Verify())
    chunks = []
    kernel.loop.on_chunk = chunks.append
    try:
        if case != "no_check":
            kernel.tasks.create("Check the current state")
            if case != "tool_only":
                add_output(kernel.tasks)
            if case not in {"output_only", "wrong_output"}:
                add_tool(kernel.tasks)
        answer = await kernel.loop.run_conversation(kernel.tasks.prepare("Check again"))
        visible = answer.read_text(kernel.session.blobs)
        assert answer.acceptance == "unverified"
        assert answer.status == ("completed" if verified else "incomplete")
        if verified:
            assert visible == draft
        elif case == "tool_only":
            assert "Provisional interpretation (unverified)" in visible and draft in visible
        else:
            assert "unresolved" in visible and draft not in visible
        if case == "tool_only":
            assert "test passed" in visible
        if case == "failed_tool":
            assert "test failed" in visible
        if case == "unrelated_tool":
            assert "test unverified" in visible
        assert [c.text for c in chunks if isinstance(c, TextDelta)] == [visible]
        records = read_session(tmp_path, kernel.session.id)
        drafts = [e.event for e in records if isinstance(e.event, ModelCallCompleted)]
        assert all(d.response_deferred for d in drafts)
        assert drafts[-1].message["blocks"][0]["text"] == draft
        assert answer.draft_call_id == drafts[-1].call_id
        assert all(c.source_seq for c in answer.delivery_checks if c.status == "passed")
        assert kernel.loop.history == fold(records).messages
        assert fold(records[:-1]).messages[-1].text() != draft
    finally:
        kernel.session.close()


@pytest.mark.parametrize("source", ["schedule.txt", "other.txt", "missing.txt"])
async def test_ordinary_read_shows_actual_observation_and_provisional_interpretation(tmp_path, selected_context, source):
    (tmp_path / "schedule.txt").write_text("Platform seven")
    (tmp_path / "other.txt").write_text("office supplies")
    draft = "I think the train leaves from platform seven."
    kernel = await kernel_for(tmp_path / "state", FakeProvider([
        tool_call_turn("An unchecked intermediate conclusion", "read_file", {"file_path": source}, call_id="current-read"),
        text_turn(draft),
    ]), workspace_root=tmp_path, native_tools=True)
    chunks = []
    kernel.loop.on_chunk = chunks.append
    try:
        answer = await kernel.loop.run_conversation(kernel.tasks.prepare("Which platform does the schedule give?"))
        visible = answer.read_text(kernel.session.blobs)
        assert answer.status == "incomplete" and not answer.delivery_checks
        if source == "missing.txt":
            assert not answer.observed_calls and draft not in visible
        else:
            assert answer.observed_calls == ("current-read",)
            assert source in visible and "current-read" in visible
            assert (tmp_path / source).read_text() in visible
            assert "Provisional interpretation (unverified)" in visible and draft in visible
        assert [c.text for c in chunks if isinstance(c, TextDelta)] == [visible]
        records = read_session(kernel.session.base, kernel.session.id)
        assert kernel.loop.history == fold(records).messages
        assert kernel.loop.history[-1].text() == visible
    finally:
        kernel.session.close()
