"""Inspect the examples available before a confirmed failed conversation turn."""

import json

from harness.blobs import BlobIntegrityError, MissingBlobError
from harness.events import (
    AgentRunFinished, AgentRunStarted, AssessmentObserved, ContextSourceObserved, SessionResumed, SessionStarted,
    UserMessage,
)
from harness.improvement_examples import select_examples, session_examples
from harness.improvement_journal import project_improvements
from harness.improvement_outcomes import failure_tool_choices, finished_replies
from harness.log import read_session
from harness.semantic_assessment import ContextSelection, ContextSelectionInput
from harness.semantics import ASSESSMENT_LIMITS, FailureReviewOrigin
from harness.telemetry import _safe


def _original_turn(envelopes, finish_seq):
    finish = next((row for row in finished_replies(envelopes) if row.seq == finish_seq), None)
    if finish is None:
        raise ValueError("The confirmed failure does not cite a finished primary turn in this session.")
    result = finish.event.result
    active = set()
    start = None
    requests = []
    selections = []
    receipts = []
    for row in envelopes:
        if row.seq >= finish_seq:
            break
        event = row.event
        if isinstance(event, AgentRunStarted):
            active.add(event.run_id)
            if event.run_id == result.run_id and event.task_id == result.task_id:
                start = row
        elif isinstance(event, AgentRunFinished):
            active.discard(event.result.run_id)
        elif start and active == {result.run_id}:
            if isinstance(event, UserMessage):
                requests.append(row)
            elif isinstance(event, AssessmentObserved):
                observation = event.observation
                if (observation.function == "context_selection" and observation.evaluation_run_id is None
                        and observation.failure_review is None):
                    selections.append(row)
            elif (isinstance(event, ContextSourceObserved) and event.source_id == "supervised-examples"
                    and event.tool == "context_selection"
                    and event.status == "ready" and event.run_id == result.run_id and event.task_id == result.task_id):
                receipts.append(row)
    if start is None or len(requests) != 1:
        return None
    return start, requests[0], selections, receipts


def _prepared_examples(blobs, selections, receipts):
    """A suggestion becomes prepared context only with a later matching native receipt."""
    by_id = {row.event.observation.id: row for row in selections}
    prepared = {}
    for row in receipts:
        ref = row.event.result
        if ref is None or row.event.byte_count != ref.size or ref.size > ASSESSMENT_LIMITS.max_message_bytes:
            continue
        try:
            packet = json.loads(blobs.get(ref))
        except (ValueError, OSError, BlobIntegrityError, MissingBlobError):
            continue
        if not isinstance(packet, dict) or not isinstance(packet.get("assessment_id"), str):
            continue
        selected = by_id.get(packet["assessment_id"])
        if selected is None or selected.seq >= row.seq:
            continue
        observation = selected.event.observation
        entries = packet.get("examples")
        if (observation.status != "ok" or not isinstance(observation.result, ContextSelection)
                or not isinstance(entries, list)):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("id") in observation.result.selected_ids:
                prepared[entry["id"]] = row.seq
    return prepared


def _original_candidates(blobs, selections, query):
    """Identify the original request's recorded candidate IDs without forwarding their contents."""
    matching, ids, unreadable = [], set(), []
    for row in selections:
        obs = row.event.observation
        ref = obs.input
        if ref is None or ref.sha256 != obs.input_sha256 or ref.size > ASSESSMENT_LIMITS.max_message_bytes:
            unreadable.append(row.seq)
            continue
        try:
            original = ContextSelectionInput.model_validate_json(blobs.get(ref))
        except (ValueError, OSError, BlobIntegrityError, MissingBlobError):
            unreadable.append(row.seq)
            continue
        if original.query == query:
            matching.append(row)
            ids.update(candidate.id for candidate in original.candidates)
    return matching, ids, unreadable


def _already_reviewed(rows, failure_id):
    return any(isinstance(row.event, AssessmentObserved) and row.event.observation.failure_review is not None
               and row.event.observation.failure_review.failure_evidence_id == failure_id for row in rows)


async def review_confirmed(kernel, failure_id):
    """Automatic follow-up uses the current ready local inference alias only."""
    session = kernel.session
    if _already_reviewed(read_session(session.base, session.id, repair=False), failure_id):
        return f"An assessment is already recorded for {failure_id}."
    catalog = getattr(kernel.provider, "catalog", None)
    resolved = catalog.resolve(str(kernel.loop.model)) if catalog else None
    if resolved is None or resolved.execution_kind != "inference" or not resolved.local:
        return "The failure is saved. Automatic review requires a selected local inference model."
    readiness = kernel.resources.snapshot(resolved)
    if readiness.status != "ready" or readiness.stale:
        return "The failure is saved. The local review model is not currently ready."
    return await review_failure(kernel, failure_id)


async def review_closed_confirmation(base, evidence, catalog_path):
    """The failure is durable before discovering or probing an eligible local reviewer."""
    from harness.catalog import Catalog
    from harness.cli import build_kernel
    from harness.fold import fold
    from harness.permissions import default_engine
    from harness.provider_litellm import CatalogProvider
    from harness.resources import LocalResources
    from pathlib import Path

    sid = evidence.source_session
    rows = read_session(base, sid, repair=False)
    if _already_reviewed(rows, evidence.id):
        return f"An assessment is already recorded for {evidence.id}."
    state = fold(rows)
    if state.open_intents or state.open_model_intents or state.open_agent_runs or state.open_evaluations:
        return "The failure is saved. Automatic review waits for the session’s open work to be reconciled."
    finish = next((row.event for row in finished_replies(rows) if row.seq == evidence.source_seq), None)
    start = next((row.event for row in rows if finish and isinstance(row.event, AgentRunStarted)
                  and row.event.run_id == finish.result.run_id and row.event.task_id == finish.result.task_id), None)
    model = state.model_selection.model if state.model_selection else (start.model if start else None)
    if not model or not catalog_path.is_file():
        return "The failure is saved. No recorded review model is available from the local catalog."
    catalog = Catalog.load(catalog_path)
    resolved = catalog.resolve(str(model))
    if resolved.execution_kind != "inference" or not resolved.local:
        return "The failure is saved. Automatic review requires a selected local inference model."
    resources = LocalResources()
    kernel = None
    try:
        observed = []
        ready = await resources.check(resolved, emit=observed.append)
        if ready.status != "ready" or ready.stale:
            return "The failure is saved. The local review model is not currently ready."
        kernel = build_kernel(base_dir=base, model=model, provider=CatalogProvider(catalog),
                              permissions=default_engine(project_dir=Path.cwd()), resources=resources,
                              resume_session_id=sid, require_idle_resume=True)
        for event in observed:
            kernel.session.append(event)
        return await review_confirmed(kernel, evidence.id)
    finally:
        try:
            await resources.close(emit=kernel.session.append if kernel else lambda event: None)
        finally:
            if kernel is not None:
                try:
                    await kernel.loop.end()
                finally:
                    kernel.session.close()


async def review_failure(kernel, failure_id, *, explicit_model=False):
    session = kernel.session
    rows = read_session(session.base, session.id, repair=False)
    evidence = project_improvements(rows, blobs=session.blobs).evidence.get(failure_id)
    if evidence is None or evidence.category != "failure" or evidence.source_session != session.id:
        raise ValueError("Select the evidence ID of a confirmed response failure in this session.")
    if failure_id.startswith("failure:"):
        finish_seq = evidence.source_seq
    else:
        from harness.improvement_outcomes import checked_failure_turn
        finish_seq = checked_failure_turn(evidence, rows, session.blobs)
        if finish_seq is None:
            raise ValueError("The failure could not be verified against a checked response in this session.")
    turn = _original_turn(rows, finish_seq)
    if turn is None:
        return f"Failure {failure_id} remains recorded. Review unassessed: the original request could not be identified."
    start_row, request, earlier_selections, receipts = turn
    start = start_row.seq
    if not request.event.text.strip() or len(request.event.text) > 2048:
        return f"Failure {failure_id} remains recorded. Review unassessed: the original request is empty or exceeds the selection input limit."
    from harness.project_binding import project_key, same_project
    header = rows[0].event
    project = start_row.event.capabilities.get("project")
    if (not isinstance(header, SessionStarted) or header.parent_session_id is not None
            or not same_project(header.project, project)):
        return f"Failure {failure_id} remains recorded. Review unassessed: the original workspace scope could not be verified."
    floor = 0 if project else max(
        (row.seq for row in rows if row.seq < start and isinstance(row.event, SessionResumed)), default=0)
    examples = session_examples(session.base, session.id, before_seq=start, project=project,
                                same_project_only=True, after_run_seq=floor)
    model = kernel.loop.model
    catalog = getattr(kernel.provider, "catalog", None)
    enabled = not examples or explicit_model or bool(catalog and catalog.resolve(str(model)).local)
    matching, original_ids, unreadable = _original_candidates(session.blobs, earlier_selections, request.event.text)
    prepared = _prepared_examples(session.blobs, matching, receipts)
    previously_suggested = {identity for row in matching if isinstance(row.event.observation.result, ContextSelection)
                            for identity in row.event.observation.result.selected_ids}
    omitted = sorted(original_ids - {example.id for example in examples})
    origin = FailureReviewOrigin(failure_evidence_id=failure_id, run_start_seq=start, request_seq=request.seq,
                                 run_finish_seq=finish_seq, eligible_examples=len(examples),
                                 history_floor_seq=floor, workspace_key=project_key(project),
                                 original_selection_seqs=tuple(row.seq for row in matching),
                                 original_context_seqs=tuple(sorted(set(prepared.values()))),
                                 omitted_original_ids=tuple(omitted), unreadable_original_inputs=tuple(unreadable))
    observation, selected = await select_examples(kernel.semantics, model=model, query=request.event.text,
                                                 examples=examples, failure_review=origin, enabled=enabled)
    submitted = (len(ContextSelectionInput.model_validate_json(session.blobs.get(observation.input)).candidates)
                 if observation.input is not None else None)
    location = "original checkout" if project else f"original process lifetime after event {floor}"
    scope = (f"Eligible examples from this session's {location} before event {start}: {len(examples)}. "
             f"Examples in the recorded assessment input: {submitted if submitted is not None else 'unavailable'}.")
    lines = [f"Failure {failure_id} remains recorded. Semantic observation {observation.id}.", scope]
    lines.extend(_safe(line) for line in failure_tool_choices(rows, finish_seq))
    if unreadable:
        lines.append("The original candidate input could not be verified at event(s) " + ", ".join(map(str, unreadable)) + ".")
    for identity in omitted:
        if identity in prepared:
            status = f"prepared as original task context at event {prepared[identity]}"
        elif identity in previously_suggested:
            status = "selected in the original turn; delivery unverified"
        else:
            status = "present in the original assessment input"
        lines.append(f"Original candidate {identity} was {status}. Its source is outside this session-local review.")
    if observation.status != "ok":
        lines.append(f"Review unassessed ({observation.reason}).")
        if not enabled:
            lines.append("For a hosted review, explicitly name the permitted inference model with harness improve --model MODEL SESSION review-failure ID.")
        return "\n".join(lines)
    if not selected:
        if omitted or unreadable:
            lines.append("No match in the rechecked input. Coverage of the original candidate pool is incomplete.")
        elif submitted == len(examples):
            lines.append("No matching example in this session’s eligible pre-turn evidence.")
        else:
            lines.append("No match in the bounded input. Some eligible examples were omitted, so the search is incomplete.")
        return "\n".join(lines)
    for example in selected:
        if example.id in prepared:
            earlier = f"prepared as original task context at event {prepared[example.id]}"
        elif example.id in previously_suggested:
            earlier = "also selected during the original turn; delivery unverified"
        else:
            earlier = "no matching selection recorded during the original turn"
        lines.append(_safe(f"Possible guidance from {example.evidence_id} (event {example.evidence_seq}, {earlier}): "
                           f"{example.action}. Recorded outcome: {example.outcome}"))
    lines.append("These examples identify guidance to investigate. An improved answer would need a comparison.")
    return "\n".join(lines)
