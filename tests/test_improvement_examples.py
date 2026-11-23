import json

import pytest

from harness.agent import AgentResult
from harness.events import (
    AgentRunFinished, AgentRunStarted, DispatchResolved, ToolCallCompleted, ToolCallProposed, UserMessage,
)
from harness.improvement_examples import select_examples, session_examples
from harness.improvement_feedback import record_feedback
from harness.improvement_journal import ImprovementJournal, read_improvements
from harness.provider import FakeProvider, text_turn
from harness.session import Session
from harness.tasks import TaskService
from harness.types import ModelId, SessionId
from tests.test_semantics import kernel_for


def record_lookup(session, *, run, tool="read_file", ambiguous=False, finished=True, project=None,
                  runtime="harness", agent=None):
    capabilities = {"project": project} if project else {}
    session.append(AgentRunStarted(task_id=run, run_id=run, runtime=runtime, agent=agent, capabilities=capabilities))
    request = session.append(UserMessage(text=f"Where is the assignment in case {run}?"))
    args = {"file_path": f"records/{run}.md"} if tool == "read_file" else {"pattern": "*.md"}
    session.append(ToolCallProposed(call_id=run, tool=tool, args=args, agent_run_id=run, task_id=run))
    action = session.append(DispatchResolved(call_id=run, kind="tool", tool=tool, args=args))
    terminal = ToolCallCompleted(call_id=run, result_text="PRIVATE OLD FILE CONTENT")
    result = session.append(terminal)
    if ambiguous:
        session.append(terminal)
    if not finished:
        return
    end = session.append(AgentRunFinished(result=AgentResult(
        task_id=run, run_id=run, status="completed", output=session.blobs.put(b"PRIVATE OLD ANSWER"))))
    return request.seq, action.seq, result.seq, end.seq


def test_attempt_examples_need_a_matched_root_read_and_cite_original_events(tmp_path):
    with Session(tmp_path, SessionId("source")) as session:
        session.start()
        first = record_lookup(session, run="first")
        second = record_lookup(session, run="second", tool="grep")
        record_lookup(session, run="ambiguous", ambiguous=True)
        record_lookup(session, run="mutated", tool="write_file")
        record_lookup(session, run="pending", finished=False)
        before = session.last_seq
        examples = session_examples(tmp_path, session.id)
        assert session.last_seq == before
        assert [(e.situation_seq, e.action_seq, e.result_seq, e.evidence_seq) for e in examples] == [second, first]
        assert examples[1].action == "read_file on records/first.md"
        evidence = read_improvements(tmp_path, session.id).evidence
        for example in examples:
            assert evidence[example.evidence_id].source_seq == example.evidence_seq
            assert "unverified" in example.outcome
            assert "PRIVATE OLD" not in example.context_candidate().summary
        assert session_examples(tmp_path, session.id, before_seq=second[-1]) == (examples[1],)
        assert not session_examples(tmp_path, session.id, before_seq=first[-1])


def test_examples_distinguish_compared_requirement_results_from_self_reported_execution(tmp_path):
    with Session(tmp_path, SessionId("tracked")) as session:
        session.start()
        tasks = TaskService(session)
        task = tasks.create("Resolve a new duty assignment")
        expected = session.blobs.put(b"PRIVATE OLD ANSWER")
        tasks.add_requirement({"id": "answer", "description": "Compare the approved assignment",
                               "check": {"kind": "output", "sha256": expected.sha256}})
        for run, content in [("miss", b"old"), ("success", b"PRIVATE OLD ANSWER")]:
            session.append(AgentRunStarted(task_id=task.id, run_id=run, runtime="harness"))
            session.append(AgentRunFinished(result=AgentResult(task_id=task.id, run_id=run, status="completed",
                output=session.blobs.put(content))))
            tasks.check()
        tasks.accept("Verified")
        examples = session_examples(tmp_path, session.id)
        assert [e.outcome.split(".")[0] for e in examples] == ["Requirement passed", "Requirement failed"]
        assert all(e.situation == task.title and "approved assignment" in e.action for e in examples)
        assert all("PRIVATE OLD ANSWER" not in e.context_candidate().summary for e in examples)


def test_ordinary_recall_rechecks_session_and_run_scope_on_resume(tmp_path):
    alpha = {"git_common_dir": "a" * 64}
    beta = {"git_common_dir": "b" * 64}
    with Session(tmp_path, SessionId("bound")) as session:
        session.start(project=alpha)
        record_lookup(session, run="first", project=alpha)
        record_lookup(session, run="other", project=beta)
        assert len(session_examples(tmp_path, session.id, project=alpha, same_project_only=True)) == 1
        assert not session_examples(tmp_path, session.id, project=beta, same_project_only=True)
        assert not session_examples(tmp_path, session.id, same_project_only=True)
    with Session(tmp_path, SessionId("unbound")) as session:
        session.start()
        record_lookup(session, run="previous-launch")
        boundary = session.last_seq
        record_lookup(session, run="this-launch")
        record_lookup(session, run="other-project", project=beta)
        example, = session_examples(tmp_path, session.id, same_project_only=True, after_run_seq=boundary)
        assert "this-launch" in example.situation


def test_scoped_recall_does_not_import_another_agents_records(tmp_path):
    with Session(tmp_path, SessionId("agents")) as session:
        session.start()
        record_lookup(session, run="own")
        record_lookup(session, run="external", runtime="codex")
        record_lookup(session, run="alternate", agent="other")
        example, = session_examples(tmp_path, session.id, same_project_only=True)
        assert "case own" in example.situation


def test_correction_and_task_definition_in_another_workspace_do_not_enter_examples(tmp_path):
    alpha = {"git_common_dir": "a" * 64}
    beta = {"git_common_dir": "b" * 64}
    with Session(tmp_path, SessionId("mixed")) as session:
        session.start(project=alpha)
        _, _, _, finish = record_lookup(session, run="before", project=alpha)
        record_lookup(session, run="elsewhere", project=beta)
        record_feedback(ImprovementJournal(session), "Private observation from elsewhere", turn=finish, failure=True)
        tasks = TaskService(session)
        task = tasks.create("Private task in the other checkout")
        expected = session.blobs.put(b"answer")
        tasks.add_requirement({"id": "answer", "description": "Verify the private expectation",
                               "check": {"kind": "output", "sha256": expected.sha256}})
        session.append(AgentRunStarted(task_id=task.id, run_id="moved", runtime="harness", capabilities={"project": alpha}))
        session.append(AgentRunFinished(result=AgentResult(task_id=task.id, run_id="moved", status="completed", output=expected)))
        tasks.check()
        raw = session_examples(tmp_path, session.id)
        assert any("Private observation" in e.outcome for e in raw)
        assert any("Private task" in e.situation for e in raw)
        scoped = session_examples(tmp_path, session.id, project=alpha)
        assert len(scoped) == 1 and scoped[0].result_seq < finish
        assert all("Private" not in e.context_candidate().summary for e in scoped)


async def test_two_examples_reach_the_native_selector_with_auditable_inputs(tmp_path):
    with Session(tmp_path, SessionId("prior")) as session:
        session.start()
        record_lookup(session, run="one")
        record_lookup(session, run="two")
    examples = session_examples(tmp_path, session.id)
    response = json.dumps({"function": "context_selection", "selected_ids": [e.id for e in examples],
                           "reason": "selected"})
    kernel = await kernel_for(tmp_path / "current", FakeProvider([text_turn(response)]))
    try:
        observation, selected = await select_examples(kernel.semantics, model=ModelId("fake"),
            query="How might I verify the current roster?", examples=examples)
        assert observation.status == "ok" and observation.function == "context_selection"
        assert selected == examples
        proposal = json.loads(kernel.session.blobs.get(observation.input))
        assert [item["id"] for item in proposal["candidates"]] == [e.id for e in examples]
        assert "PRIVATE OLD" not in str(proposal)
    finally:
        kernel.session.close()


async def test_no_tool_wrong_answer_becomes_a_selectable_example_only_after_confirmation(tmp_path):
    with Session(tmp_path, SessionId("prior")) as session:
        session.start()
        session.append(AgentRunStarted(task_id="case", run_id="first", runtime="harness"))
        session.append(UserMessage(text="Which record governs the new assignment?"))
        finish = session.append(AgentRunFinished(result=AgentResult(task_id="case", run_id="first",
            status="completed", output=session.blobs.put(b"SECRET WRONG ANSWER"))))
        assert not session_examples(tmp_path, session.id)
        record_feedback(ImprovementJournal(session), "The draft used the summary instead of the governing register.",
                        turn=finish.seq, failure=True)
        marked = session.last_seq
        assert not session_examples(tmp_path, session.id, before_seq=marked)
        example, = session_examples(tmp_path, session.id, before_seq=marked + 1)
        assert example.result_seq == finish.seq and example.evidence_seq == marked
        assert "governing register" in example.outcome and "SECRET WRONG ANSWER" not in example.context_candidate().summary
    response = json.dumps({"function": "context_selection", "selected_ids": [example.id], "reason": "selected"})
    kernel = await kernel_for(tmp_path / "current", FakeProvider([text_turn(response)]))
    try:
        observation, selected = await select_examples(kernel.semantics, model=ModelId("fake"),
            query="Where should I check today's roster?", examples=(example,))
        assert observation.status == "ok" and selected == (example,)
    finally:
        kernel.session.close()


@pytest.mark.parametrize("response", [
    {"function": "context_selection", "selected_ids": ["invented"], "reason": "selected"},
    {"function": "context_selection", "selected_ids": [], "reason": "uncertain"},
])
async def test_unavailable_or_abstaining_selection_does_not_supply_examples(tmp_path, response):
    with Session(tmp_path, SessionId("prior")) as session:
        session.start()
        record_lookup(session, run="one")
    examples = session_examples(tmp_path, session.id)
    kernel = await kernel_for(tmp_path / "current", FakeProvider([text_turn(json.dumps(response))]))
    try:
        observation, selected = await select_examples(kernel.semantics, model=ModelId("fake"),
            query="Which record should be checked?", examples=examples)
        assert not selected
        assert observation.reason in {"uncertain", "invalid_output"}
    finally:
        kernel.session.close()
