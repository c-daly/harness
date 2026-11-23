"""Expose recorded task outcomes as evidence in the existing improvement journal."""

from collections import Counter

from harness.events import (
    AgentRunFinished, AgentRunStarted, ImprovementRecorded, SessionOutcome, SessionStarted, TaskAccepted,
    TaskChecked, TaskOutcome,
)
from harness.improvement import Candidate, Evidence, ImprovementState, SourceAuthoring
from harness.tasks import OutputCheck, TaskState, check_requirement


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


def _output_failures(task, check, envelopes, finishes, blobs):
    if blobs is None or not task or check.event.basis_seq != task.basis_seq or task.open_runs:
        return
    for key, observed in task.evidence.items():
        requirement = task.requirements[key]
        finish = finishes.get(observed.source_seq)
        if (not isinstance(requirement.check, OutputCheck) or observed.status != "failed" or finish is None
                or finish.event.result.task_id != check.event.task_id or finish.event.result.run_id != task.run_id
                or not task.run_started_seq < finish.seq < check.seq or observed.artifact is None
                or observed.artifact != finish.event.result.output
                or observed.actual_sha256 != observed.artifact.sha256
                or observed.actual_sha256 == requirement.check.sha256):
            continue
        # Use the native check on the evidence available at that point. This
        # also checks declaration timing, run boundaries and actual blob bytes.
        verified = check_requirement(requirement, task, envelopes, blobs)
        if (verified.status != "failed" or verified.source_seq != observed.source_seq
                or verified.actual_sha256 != observed.actual_sha256 or verified.artifact != observed.artifact
                or observed.call_id is not None):
            continue
        observation = (f"Native output check {key} at event {check.seq} failed for the response ending at "
                       f"event {finish.seq}. Required SHA-256 {requirement.check.sha256}. "
                       f"Verified output SHA-256 {observed.actual_sha256}.")
        yield f"output-mismatch-{key}", observation, finish.seq


def checked_failure_turn(evidence, envelopes, blobs):
    """Resolve an observed output mismatch to its original response for review."""
    if evidence.category != "failure":
        return None
    tasks = TaskState()
    for index, env in enumerate(envelopes):
        tasks.apply(env)
        if env.seq != evidence.source_seq:
            continue
        if env.session_id != evidence.source_session or not isinstance(env.event, TaskChecked):
            return None
        prefix = envelopes[:index + 1]
        finishes = {item.seq: item for item in finished_replies(prefix)}
        failures = _output_failures(tasks.items.get(env.event.task_id), env, prefix, finishes, blobs)
        return next((finish for _, observation, finish in failures if observation == evidence.observation), None)
    return None


def _observations(envelopes, blobs):
    finishes = {env.seq: env for env in finished_replies(envelopes)}
    for envelope in finishes.values():
        result = envelope.event.result
        observation = f"Primary execution ended {result.status} with task acceptance {result.acceptance}."
        if result.reason:
            observation += f" The recorded reason was {result.reason[:512]}."
        yield envelope, "run-outcome", "task_outcome", observation

    # Only requirement checks and acceptance need the historical task projection.
    tasks = TaskState() if any(isinstance(e.event, (TaskChecked, TaskAccepted)) for e in envelopes) else None
    for index, envelope in enumerate(envelopes):
        event = envelope.event
        if tasks is not None:
            tasks.apply(envelope)
        if isinstance(event, (TaskOutcome, SessionOutcome)):
            scope = "Task" if isinstance(event, TaskOutcome) else "Session"
            observation = f"{scope} outcome was marked {event.status}."
            if event.score is not None:
                observation += f" The recorded score was {event.score}."
            yield envelope, event.type, "task_outcome", observation
        elif isinstance(event, TaskChecked):
            task = tasks.items.get(event.task_id)
            if task and event.basis_seq == task.basis_seq and not task.open_runs and task.evidence:
                counts = Counter(item.status for item in task.evidence.values())
                yield envelope, "task-check", "task_outcome", (
                    f"Recorded requirement checks included {counts['passed']} passed, "
                    f"{counts['failed']} failed and {counts['unverified']} unverified."
                )
                if counts["failed"] and blobs is not None:
                    for kind, observation, _ in _output_failures(task, envelope, envelopes[:index + 1], finishes, blobs):
                        yield envelope, kind, "failure", observation
        elif isinstance(event, TaskAccepted):
            task = tasks.items.get(event.task_id)
            if task and task.acceptance and task.acceptance[0] == envelope.seq:
                yield envelope, "task-accepted", "task_outcome", "The recorded requirements were explicitly accepted."


def project_task_outcomes(state: ImprovementState, envelopes, blobs=None) -> None:
    """Expose history without appending on inspection or satisfying missing replay dependencies."""
    if not any(isinstance(env.event, (AgentRunFinished, TaskOutcome, SessionOutcome, TaskChecked, TaskAccepted))
               for env in envelopes):
        return
    positions = {env.event.record.id: env.seq for env in envelopes if isinstance(env.event, ImprovementRecorded)}
    for env, kind, category, observation in _observations(envelopes, blobs):
        stem = f"{kind}-{env.seq}"
        key = stem
        suffix = 0
        while key in positions:
            # An operator may have used this ID before automatic discovery existed.
            stored = state.evidence.get(key)
            if (stored and stored.category == category and stored.source_session == env.session_id
                    and stored.source_seq == env.seq and stored.observation == observation):
                break
            suffix += 1
            key = f"{stem}-{suffix}"
        else:
            state.evidence[key] = Evidence(id=key, source_session=env.session_id, source_seq=env.seq,
                                           category=category, observation=observation)
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
