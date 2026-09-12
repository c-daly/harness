"""Improvement facts live in core session records, not a replacement memory store."""

from copy import deepcopy
from pathlib import Path

from harness.events import EvaluationRunStarted, EvaluationRunFinished, ImprovementRecorded
from harness.improvement import (
    Candidate, EvaluationPlan, Evidence, ExperimentResult, ImprovementRecord, ImprovementState, PromptChange, SourceChange,
)
from harness.log import read_session
from harness.session import Session
from harness.types import SessionId


def read_improvements(base: Path, session_id: SessionId) -> ImprovementState:
    state = ImprovementState()
    for envelope in read_session(base, session_id, repair=False):
        if isinstance(envelope.event, ImprovementRecorded):
            state.apply(envelope.event.record)
        elif isinstance(envelope.event, EvaluationRunStarted):
            state.runs[envelope.event.run_id] = "running"
        elif isinstance(envelope.event, EvaluationRunFinished):
            state.runs[envelope.event.run_id] = envelope.event.status
    return state


def render_improvements(state: ImprovementState) -> str:
    from harness.improvement import verdict
    from harness.telemetry import _safe
    lines = [f"Improvements: {len(state.evidence)} evidence records, "
             f"{len(state.candidates)} candidates, {len(state.results)} experiments"]
    if len(state.evidence) > 10:
        lines.append(f"Showing the latest 10 of {len(state.evidence)} evidence records.")
    for evidence in list(state.evidence.values())[-10:]:
        lines.append(_safe(f"Evidence {evidence.id}: {evidence.category}; "
                           f"source={evidence.source_session}:{evidence.source_seq}"))
    for candidate in state.candidates.values():
        results = [r for r in state.results.values()
                   if state.plans[r.plan_id].candidate_id == candidate.id]
        status = verdict(state.plans[results[-1].plan_id], results[-1]) if results else "pending"
        lines.append(_safe(f"{candidate.id}: {candidate.target} {candidate.artifact.sha256[:12]} "
                           f"evaluation={status}; {candidate.hypothesis}"))
        if results:
            lines.append(f"  Run: {results[-1].completion}; result={results[-1].id}")
        if candidate.target == "code":
            plans = [p for p in state.plans.values() if p.candidate_id == candidate.id]
            if plans:
                lines.append(_safe(f"  Latest of {len(plans)} source plans: {plans[-1].id}"))
    if len(state.runs) > 10:
        lines.append(f"Showing the latest 10 of {len(state.runs)} evaluation runs.")
    for run_id, status in list(state.runs.items())[-10:]:
        lines.append(_safe(f"Evaluation run {run_id}: {status}"))
    for change_id in (*state.active_prompts.values(), *state.active_assessment_prompts.values()):
        change = state.prompt_changes[change_id]
        function = "message" if change.function == "message_kind" else change.function
        lines.append(_safe(f"Selected {function} prompt for {change.model}: {change.prompt.sha256[:12]} "
                           f"({change.action}; change={change.id}); shadow only. Live compatibility not checked."))
    for change_id in state.active_sources.values():
        change = state.source_changes[change_id]
        lines.append(_safe(f"Selected source {change.slot}: {change.source.sha256[:12]} "
                           f"({change.action}; change={change.id}); next process only. "
                           f"Launch: harness run-source SESSION {change.slot} -- ARGS"))
    lines.append("Evaluation records do not activate changes. Adoption and rollback require explicit controls.")
    return "\n".join(lines)


def inspect_improvement(state: ImprovementState, blobs, record_id: str) -> str:
    """Show immutable claims, prompt bytes, gates and measurements without inference."""
    from harness.prompt_improvement import load_function_prompt
    from harness.telemetry import _safe
    record = next((records[record_id] for records in (
        state.evidence, state.candidates, state.plans, state.results, state.prompt_changes, state.source_changes,
    ) if record_id in records), None)
    if record is None:
        raise ValueError(f"unknown improvement record: {record_id}")
    lines = [record.model_dump_json(indent=2)]
    from harness.source_improvement import is_source_patch

    def source_candidate(candidate):
        return candidate.target == "code" and is_source_patch(blobs, candidate.artifact)

    if isinstance(record, EvaluationPlan) and source_candidate(state.candidates[record.candidate_id]):
        from harness.source_improvement import SourceSuite, _load
        lines += ["Frozen source checks (data):", _load(blobs, record.suite, SourceSuite).model_dump_json(indent=2)]
    if isinstance(record, ExperimentResult):
        plan = state.plans[record.plan_id]
        lines += ["Frozen evaluation plan:", plan.model_dump_json(indent=2)]
        if source_candidate(state.candidates[plan.candidate_id]):
            from harness.source_improvement import SourceSuite, _load
            lines += ["Frozen source checks (data):",
                      _load(blobs, plan.suite, SourceSuite).model_dump_json(indent=2)]
            if record.artifact.size <= 1024 * 1024:
                lines += ["Recorded source check outcomes (data):",
                          blobs.get(record.artifact).decode("utf-8")]
            else:
                lines.append("Source check report exceeds 1 MiB; inspect its retained blob for full output.")
        record = state.candidates[plan.candidate_id]
    if isinstance(record, Candidate) and source_candidate(record):
        from harness.source_improvement import inspect_patch
        lines.append(inspect_patch(blobs, record.artifact))
    if isinstance(record, Candidate) and record.target == "prompt":
        from pydantic import TypeAdapter
        from harness.semantic_assessment import AssessmentPrompt
        from harness.semantics import MessagePrompt
        if record.artifact.size > 32768:
            raise ValueError("semantic prompt artifact exceeds 32768 bytes")
        prompt = TypeAdapter(MessagePrompt | AssessmentPrompt).validate_json(blobs.get(record.artifact))
        lines += ["Candidate prompt (data):", prompt.model_dump_json(indent=2)]
    elif isinstance(record, PromptChange):
        lines += ["Previous prompt (data):", load_function_prompt(blobs, record.previous, record.function).model_dump_json(indent=2),
                  "Selected prompt (data):", load_function_prompt(blobs, record.prompt, record.function).model_dump_json(indent=2)]
    return _safe("\n".join(lines))


class ImprovementJournal:
    def __init__(self, session: Session):
        self.session = session
        self.state = read_improvements(session.base, session.id)

    def record(self, record: ImprovementRecord) -> None:
        """Validate references before publishing; replay never reruns experiments."""
        # A second producer may have appended since this view was constructed.
        # Session owns synchronous writes; no await separates validation/publication.
        updated = deepcopy(read_improvements(self.session.base, self.session.id))
        updated.apply(record)
        if isinstance(record, Evidence):
            source = read_session(self.session.base, record.source_session, repair=False)
            if not any(e.seq == record.source_seq for e in source):
                raise ValueError("improvement evidence source event is missing")
        if isinstance(record, (Candidate, ExperimentResult)):
            self.session.blobs.get(record.artifact)
        if isinstance(record, ExperimentResult) and record.run_id is not None:
            events = read_session(self.session.base, self.session.id, repair=False)
            starts = [e.event for e in events if isinstance(e.event, EvaluationRunStarted)
                      and e.event.run_id == record.run_id]
            if (len(starts) != 1 or starts[0].plan_id != record.plan_id
                    or starts[0].incumbent.sha256 != record.incumbent_version):
                raise ValueError("experiment result requires its matching recorded run")
            if (any(r.run_id == record.run_id for r in updated.results.values() if r.id != record.id)
                    or any(isinstance(e.event, EvaluationRunFinished) and e.event.run_id == record.run_id
                           for e in events)):
                raise ValueError("experiment run already has a terminal result")
        if isinstance(record, EvaluationPlan):
            self.session.blobs.get(record.suite)
        if isinstance(record, SourceChange):
            from harness.source_promotion import validate_change
            validate_change(self.session, updated, record)
        if isinstance(record, PromptChange):
            from harness.semantic_evaluation import EvaluatorConfig
            from harness.prompt_improvement import load_function_prompt
            load_function_prompt(self.session.blobs, record.previous, record.function)
            load_function_prompt(self.session.blobs, record.prompt, record.function)
            if record.configuration is not None:
                if record.configuration.size > 16384:
                    raise ValueError("prompt evaluation configuration is too large")
                config = EvaluatorConfig.model_validate_json(self.session.blobs.get(record.configuration))
                if config.model != record.model:
                    raise ValueError("prompt selection model differs from its evaluation configuration")
            if record.action == "adopt":
                result = updated.results[record.result_id]
                events = read_session(self.session.base, self.session.id, repair=False)
                starts = [e.event for e in events if isinstance(e.event, EvaluationRunStarted)
                          and e.event.run_id == result.run_id]
                finishes = [e.event for e in events if isinstance(e.event, EvaluationRunFinished)
                            and e.event.run_id == result.run_id]
                if (len(starts) != 1 or len(finishes) != 1
                        or starts[0].plan_id != result.plan_id or starts[0].incumbent != record.previous
                        or starts[0].configuration != record.configuration
                        or finishes[0].status != "completed" or finishes[0].result_id != result.id):
                    raise ValueError("adoption requires its completed paired evaluation run")
                self.session.blobs.get(result.artifact)
        self.session.append(ImprovementRecorded(record=record))
        self.state = updated
