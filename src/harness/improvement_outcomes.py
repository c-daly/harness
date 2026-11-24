"""Expose recorded task outcomes as evidence in the existing improvement journal."""

import hashlib
from collections import Counter, defaultdict

from harness.events import (
    AgentRunFinished, AgentRunStarted, ContextPrepared, DispatchResolved, HookDecided, ImprovementRecorded,
    ModelCallCompleted, ModelCallProposed, PermissionResolved, SemanticObserved, SessionOutcome, SessionStarted,
    TaskAccepted, TaskChecked, TaskOutcome, ToolCallAborted, ToolCallCancelled, ToolCallCompleted, ToolCallProposed,
)
from harness.improvement import Candidate, Evidence, ImprovementState, SourceAuthoring
from harness.messages import Message, Role
from harness.semantics import MessageInterpretation
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


def _dispatch_receipt(block, facts, run, response_seq, before_seq):
    """A model proposal alone proves neither dispatch nor a successful tool result."""
    intents = [row for row in facts if isinstance(row.event, (ToolCallProposed, ModelCallProposed))]
    if len(intents) != 1:
        return None
    intent, = intents
    call = intent.event
    if (not isinstance(call, ToolCallProposed) or call.purpose != "conversation"
            or call.agent_run_id != run.run_id or call.task_id != run.task_id
            or not response_seq < intent.seq < before_seq or call.tool != block.tool or call.args != block.args):
        return None
    # No receipt can precede the native intent or cross into the next model response.
    if any(not intent.seq <= row.seq < before_seq for row in facts):
        return None
    terminals = [row for row in facts if isinstance(row.event, (ToolCallCompleted, ToolCallAborted, ToolCallCancelled))]
    if len(terminals) > 1 or (terminals and terminals[0].seq != facts[-1].seq):
        return None
    details = [f"{call.tool} native proposal at event {intent.seq}"]
    for row in facts:
        event = row.event
        if isinstance(event, HookDecided) and event.decision.get("kind") == "block":
            details.append(f"blocked by {event.hook} at event {row.seq}")
        elif isinstance(event, PermissionResolved) and not event.allowed:
            details.append(f"permission denied at event {row.seq}")
        elif isinstance(event, DispatchResolved) and event.kind == "tool":
            details.append(f"dispatch resolved at event {row.seq}")
        elif isinstance(event, ToolCallCompleted):
            status = "error" if event.is_error else "completed without error"
            details.append(f"{status} at event {row.seq}")
        elif isinstance(event, (ToolCallCancelled, ToolCallAborted)):
            status = "cancelled" if isinstance(event, ToolCallCancelled) else "aborted"
            details.append(f"{status} at event {row.seq}")
    if not terminals:
        details.append("terminal status unrecorded")
    return ", ".join(details) + "."


def failure_tool_choices(envelopes, finish_seq):
    """Describe the failed primary's decisions without inferring a recommendation or intent."""
    from pydantic import ValidationError

    finish = next((row for row in finished_replies(envelopes) if row.seq == finish_seq), None)
    if finish is None:
        return []
    run = finish.event.result
    started = False
    prepared = None
    facts = defaultdict(list)
    responses = []
    for row in envelopes:
        if row.seq >= finish_seq:
            break
        event = row.event
        if isinstance(event, (ToolCallProposed, ModelCallProposed, HookDecided, PermissionResolved, DispatchResolved,
                              ToolCallCompleted, ToolCallCancelled, ToolCallAborted)):
            facts[event.call_id].append(row)
        if isinstance(event, AgentRunStarted) and event.run_id == run.run_id and event.task_id == run.task_id:
            started = True
            prepared = None
            responses.clear()
        if not started:
            continue
        if isinstance(event, ContextPrepared) and event.task_id == run.task_id:
            prepared = event
        if (not isinstance(event, ModelCallCompleted) or event.purpose != "conversation"
                or event.execution_kind == "agent" or event.agent_run_id != run.run_id or event.task_id != run.task_id):
            continue
        try:
            message = Message.model_validate(event.message)
        except ValidationError:
            proposals = None
        else:
            proposals = message.tool_calls() if message.role == Role.ASSISTANT else None
        requested = "unavailable" if proposals is None else (", ".join(block.tool for block in proposals) or "none")
        if event.offered_tools is not None:
            inventory = "Harness offered " + (", ".join(event.offered_tools) or "no tools")
        elif prepared is not None:
            inventory = "context prepared " + (", ".join(prepared.tools) or "no tools") + " (final offer unrecorded)"
        else:
            inventory = "tool inventory unrecorded"
        summary = f"Model response at event {row.seq} — {inventory}. Model requested {requested}."
        responses.append((row.seq, proposals or (), summary))
    lines = []
    next_response_seqs = [response[0] for response in responses[1:]] + [finish_seq]
    for (response_seq, proposals, summary), before_seq in zip(responses, next_response_seqs):
        lines.append(summary)
        counts = Counter(block.call_id for block in proposals)
        for block in proposals:
            if counts[block.call_id] != 1:
                continue
            receipt = _dispatch_receipt(block, facts[block.call_id], run, response_seq, before_seq)
            if receipt:
                lines.append(receipt)
    return lines


def _reply_corrections(envelopes, blobs):
    """Project a classifier observation only when its exchange and inference receipt agree."""
    from harness.reply_semantics import ReplyHistory, exchange_for_origin

    history = ReplyHistory()
    latest_exchange = None
    by_seq = None
    proposals = {}
    calls = {}
    for row in envelopes:
        event = row.event
        if isinstance(event, AgentRunStarted) and event.parent_run_id is None:
            latest_exchange = None
        elif (isinstance(event, AgentRunFinished)
                and history.root and history.root.run_id == event.result.run_id
                and history.root.task_id == event.result.task_id and not history.ambiguous
                and history.request is not None and history.pending_prior is not None
                and history.request.event.reply_to_seq == history.pending_prior.finish_seq
                and history.pending_prior.agent == history.root.agent):
            # Only the conversation actually preceding this submitted message
            # can supply its target, even when two turns contain identical text.
            prior = history.pending_prior
            latest_exchange = (prior.request_seq, prior.finish_seq, history.request.seq)
        history.apply(row)
        if isinstance(event, ModelCallProposed) and event.purpose.startswith("semantic:"):
            proposal = (latest_exchange, event.purpose, event.model)
            proposals[event.call_id] = proposal if event.call_id not in proposals else None
        elif isinstance(event, ModelCallCompleted) and event.purpose.startswith("semantic:"):
            proposal = proposals.pop(event.call_id, None)
            if proposal is not None and (event.purpose, event.model) == proposal[1:]:
                calls[event.call_id] = (row, proposal[0]) if event.call_id not in calls else None
        elif isinstance(event, SemanticObserved):
            observed = event.observation
            origin = observed.reply_origin
            if (origin is None or observed.status != "ok" or observed.reason != "classified"
                    or observed.kind != "correction" or observed.effective_model != observed.model):
                continue
            receipt = calls.get(observed.call_id)
            if receipt is None or receipt[1] != (origin.request_seq, origin.run_finish_seq, origin.followup_seq):
                continue
            call, _ = receipt
            if (not origin.followup_seq < call.seq < row.seq or call.session_id != row.session_id
                    or call.event.model != observed.model or call.event.purpose != f"semantic:{observed.id}"
                    or call.event.stop_reason != "end_turn"):
                continue
            try:
                response = Message.model_validate(call.event.message)
                label = MessageInterpretation.model_validate_json(response.text())
            except ValueError:
                continue
            if response.role != Role.ASSISTANT or response.tool_calls() or label.kind != observed.kind:
                continue
            if by_seq is None:
                by_seq = {env.seq: env for env in envelopes}
            packet = exchange_for_origin(by_seq, origin, blobs)
            if packet is None or hashlib.sha256(packet.encode()).hexdigest() != observed.input_sha256:
                continue
            del calls[observed.call_id]
            description = (f"Shadow classification identified the follow-up at event {origin.followup_seq} "
                           f"as a correction to the answer ending at event {origin.run_finish_seq} "
                           f"for the request at event {origin.request_seq}. Response correctness is unverified.")
            yield row, "reply-correction", "correction", description


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

    if any(isinstance(env.event, SemanticObserved) and env.event.observation.reply_origin is not None
           and env.event.observation.kind == "correction" for env in envelopes):
        yield from _reply_corrections(envelopes, blobs)


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
