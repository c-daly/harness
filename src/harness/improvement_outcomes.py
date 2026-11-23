"""Expose recorded task outcomes as evidence in the existing improvement journal."""

from collections import Counter

from harness.events import (
    AgentRunFinished, AgentRunStarted, ImprovementRecorded, SessionOutcome, SessionStarted, TaskAccepted,
    TaskChecked, TaskOutcome,
)
from harness.improvement import Candidate, Evidence, ImprovementState, SourceAuthoring
from harness.tasks import TaskState


def finished_replies(envelopes):
    """Identify finished primary turns, skipping child runs and source authorship."""
    roots = {}
    for env in envelopes:
        event = env.event
        if isinstance(event, SessionStarted) and event.parent_session_id is not None:
            return
        if isinstance(event, AgentRunStarted) and event.parent_run_id is None and event.runtime != "resident-source-author":
            roots[event.run_id] = event.task_id
        elif isinstance(event, AgentRunFinished) and roots.pop(event.result.run_id, None) == event.result.task_id:
            yield env


def _observations(envelopes):
    for envelope in finished_replies(envelopes):
        result = envelope.event.result
        observation = f"Primary execution ended {result.status} with task acceptance {result.acceptance}."
        if result.reason:
            observation += f" The recorded reason was {result.reason[:512]}."
        yield envelope, "run-outcome", observation

    # Only requirement checks and acceptance need the historical task projection.
    tasks = TaskState() if any(isinstance(e.event, (TaskChecked, TaskAccepted)) for e in envelopes) else None
    for envelope in envelopes:
        event = envelope.event
        if tasks is not None:
            tasks.apply(envelope)
        if isinstance(event, (TaskOutcome, SessionOutcome)):
            scope = "Task" if isinstance(event, TaskOutcome) else "Session"
            observation = f"{scope} outcome was marked {event.status}."
            if event.score is not None:
                observation += f" The recorded score was {event.score}."
            yield envelope, event.type, observation
        elif isinstance(event, TaskChecked):
            task = tasks.items.get(event.task_id)
            if task and event.basis_seq == task.basis_seq and not task.open_runs and task.evidence:
                counts = Counter(item.status for item in task.evidence.values())
                yield envelope, "task-check", (
                    f"Recorded requirement checks included {counts['passed']} passed, "
                    f"{counts['failed']} failed and {counts['unverified']} unverified."
                )
        elif isinstance(event, TaskAccepted):
            task = tasks.items.get(event.task_id)
            if task and task.acceptance and task.acceptance[0] == envelope.seq:
                yield envelope, "task-accepted", "The recorded requirements were explicitly accepted."


def project_task_outcomes(state: ImprovementState, envelopes) -> None:
    """Expose history without appending on inspection or satisfying missing replay dependencies."""
    if not any(isinstance(env.event, (AgentRunFinished, TaskOutcome, SessionOutcome, TaskChecked, TaskAccepted))
               for env in envelopes):
        return
    positions = {env.event.record.id: env.seq for env in envelopes if isinstance(env.event, ImprovementRecorded)}
    for env, kind, observation in _observations(envelopes):
        stem = f"{kind}-{env.seq}"
        key = stem
        suffix = 0
        while key in positions:
            # An operator may have used this ID before automatic discovery existed.
            stored = state.evidence.get(key)
            if (stored and stored.category == "task_outcome" and stored.source_session == env.session_id
                    and stored.source_seq == env.seq and stored.observation == observation):
                break
            suffix += 1
            key = f"{stem}-{suffix}"
        else:
            state.evidence[key] = Evidence(id=key, source_session=env.session_id, source_seq=env.seq,
                                           category="task_outcome", observation=observation)
            state.derived_evidence_ids.add(key)
            positions[key] = env.seq
    if state.derived_evidence_ids:
        state.evidence = {key: state.evidence[key] for key in sorted(state.evidence, key=positions.__getitem__)}


def record_cited_outcomes(session, state: ImprovementState, record) -> None:
    """Persist the existing Evidence type before its first dependent proposal."""
    if isinstance(record, (Candidate, SourceAuthoring)):
        for key in dict.fromkeys(record.evidence_ids):
            if key in state.derived_evidence_ids:
                session.append(ImprovementRecorded(record=state.evidence[key]))
                state.derived_evidence_ids.remove(key)
