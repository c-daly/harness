"""Read-only coordination checks over frozen task requirements and child evidence."""

from harness.agent import current_agent_run
from harness.blobs import BlobIntegrityError, BlobStore, MissingBlobError
from harness.tasks import MAX_EVIDENCE_BYTES, RequirementEvidence, TaskService, check_requirement, project_tasks


def capture_requirements(parent):
    """Use the task owning the actual active run, never an unrelated selection."""
    active = current_agent_run.get()
    if parent is None or active is None:
        return None, None
    state = TaskService(parent).state()
    task = state.items.get(state.run_owners.get(active.run_id))
    if task is None or not task.requirements:
        return None, None
    if (active.run_id not in task.open_runs or
            any(seq >= task.run_started_seq for seq in task.declared_at.values())):
        raise ValueError("task requirements were not declared before the active execution")
    return tuple(task.requirements.values()), {
        "session_id": parent.id, "task_id": task.definition.id,
        "run_id": task.run_id, "basis_seq": task.basis_seq, "objective": task.definition.title,
    }


def check_child_result(parent, result, requirements, *, after_seq, objective):
    """Recheck artifacts and lineage instead of accepting child-reported grades.

    Each source_seq/call_id/artifact in the returned evidence belongs to the
    participant's child session. No commands run and no user task is accepted.
    """
    from harness.events import AgentRunFinished, AgentRunStarted, SessionStarted, SubagentFinished, SubagentSpawned
    from harness.log import TornLogError, read_session

    def unresolved(reason):
        return tuple(RequirementEvidence(requirement_id=r.id, status="unverified", reason=reason)
                     for r in requirements)

    if result.status != "completed" or result.truncated:
        return unresolved("participant did not deliver a complete result")
    if result.child_session_id is None or result.run_id is None or result.output is None:
        return unresolved("participant has no recorded execution provenance")
    try:
        parent_events = read_session(parent.base, parent.id, repair=False)
        spawns = [e for e in parent_events if isinstance(e.event, SubagentSpawned)
                  and e.event.child_session_id == result.child_session_id]
        delivered = [e for e in parent_events if isinstance(e.event, SubagentFinished)
                     and e.event.child_session_id == result.child_session_id]
        if (len(spawns) != 1 or len(delivered) != 1 or not after_seq < spawns[0].seq < delivered[0].seq
                or delivered[0].event.run_id != result.run_id or delivered[0].event.status != "ok"
                or delivered[0].event.output != result.output or delivered[0].event.truncated):
            return unresolved("participant delivery is missing, unrelated or ambiguous")
        events = read_session(parent.base, result.child_session_id, repair=False)
        origins = [e.event for e in events if isinstance(e.event, SessionStarted)]
        starts = [e for e in events if isinstance(e.event, AgentRunStarted) and e.event.run_id == result.run_id]
        ends = [e for e in events if isinstance(e.event, AgentRunFinished) and e.event.result.run_id == result.run_id]
        if (len(origins) != 1 or origins[0].parent_session_id != parent.id or origins[0].parent_seq != spawns[0].seq
                or len(starts) != 1 or len(ends) != 1 or starts[0].seq >= ends[0].seq
                or ends[0].event.result.status != "completed" or ends[0].event.result.output != result.output):
            return unresolved("participant execution boundary does not match its delivery")
        task = project_tasks(events).items.get(starts[0].event.task_id)
        if (task is None or task.run_id != result.run_id or task.open_runs
                or task.definition.title != objective
                or tuple(task.requirements.values()) != requirements
                or starts[0].event.acceptance_criteria != task.criteria):
            return unresolved("participant did not execute the frozen requirements")
        blobs = BlobStore(parent.base / "sessions" / result.child_session_id / "blobs", create=False)
        if result.output.size > MAX_EVIDENCE_BYTES or blobs.get(result.output).decode("utf-8") != result.text:
            return unresolved("delivered text does not match bounded recorded output")
        return tuple(check_requirement(r, task, events, blobs) for r in requirements)
    except (OSError, ValueError, MissingBlobError, BlobIntegrityError, TornLogError) as exc:
        return unresolved(f"recorded child evidence unavailable ({type(exc).__name__})")
