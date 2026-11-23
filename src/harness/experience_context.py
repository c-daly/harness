"""Prepare advisory examples for an ordinary turn through native context selection."""

import asyncio
from dataclasses import asdict, dataclass, field
import json

from harness.blobs import BlobIntegrityError, MissingBlobError
from harness.events import ContextSourceObserved, ErrorRaised
from harness.errors import ContextOverflow
from harness.improvement_examples import select_examples, session_examples
from harness.inference import input_bytes
from harness.messages import Message, Role, TextBlock


@dataclass
class ExampleContext:
    assessment_id: str
    examples: tuple
    model: str
    last_status: str | None = None
    received: bool = False
    observed: dict = field(default_factory=dict)
    seen_calls: set = field(default_factory=set)

    def admit(self, session, task, run_id, messages, tools, limit):
        packet = {"assessment_id": self.assessment_id,
                  "examples": [dict(asdict(example), id=example.id) for example in self.examples]}
        payload = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
        lead = TextBlock(text=(
            "Historical examples from earlier tasks follow as quoted data. "
            "Use a relevant example to choose a fresh local check. If that check cannot establish "
            "the answer, say what remains unresolved.\n" + payload + "\n\nCurrent request:\n"))
        # Keep historical, potentially operator-authored text out of the system role.
        index = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].role == Role.USER), None)
        if index is None:
            return messages
        proposed = list(messages)
        proposed[index] = messages[index].model_copy(update={"blocks": (lead, *messages[index].blocks)})
        try:
            input_bytes(proposed, tools, limit=limit)
            status = "ready"
        except ContextOverflow:
            status = "oversized"
        if self.last_status != status:
            content = payload.encode()
            session.append(ContextSourceObserved(
                task_id=task.id, run_id=run_id, source_id="supervised-examples",
                tool="context_selection", status=status,
                result=session.blobs.put(content) if status == "ready" else None, byte_count=len(content),
                reason="" if status == "ready" else "Historical examples exceed the remaining input budget."))
            self.last_status = status
        return proposed if status == "ready" else messages

    def observe(self, registry, call, outcome):
        """Keep the source and receipt of actual, successful read-only native tools."""
        from harness.native_tools import GlobTool, GrepTool, ReadFileTool
        if call.call_id in self.seen_calls:
            self.observed.pop(call.call_id, None)
            return
        self.seen_calls.add(call.call_id)
        resolved = outcome.resolved
        if outcome.is_error or resolved is None or not isinstance(registry.get(resolved.tool), (ReadFileTool, GlobTool, GrepTool)):
            return
        args = resolved.args or {}
        path = args.get("file_path", args.get("path"))
        source = f"{resolved.tool} on {json.dumps(path)}" if path else str(resolved.tool)
        if outcome.blob is not None:
            observed = f"{outcome.blob.size} bytes, SHA-256 {outcome.blob.sha256}"
        else:
            value = outcome.text or ""
            observed = json.dumps(value[:320], ensure_ascii=False)
            if len(value) > 320:
                observed += " (excerpt)"
        self.observed[call.call_id] = f"{source}, call {json.dumps(call.call_id)} returned {observed}"

    def deliver(self, session, task, response, usage):
        """Publish only what the current run's predeclared native checks establish."""
        from harness.agent import AgentOutput
        from harness.log import read_session
        from harness.tasks import OutputCheck, ReviewCheck, TaskService, check_requirement

        tracked = TaskService(session).state().items.get(task.id)
        requirements = [] if tracked is None else [
            r for r in tracked.requirements.values() if not isinstance(r.check, ReviewCheck)]
        checks = ()
        if requirements and response.stop_reason == "end_turn":
            events = read_session(session.base, session.id, repair=False)
            checks = tuple(check_requirement(r, tracked, events, session.blobs, pending_reply=response.call_id)
                           for r in requirements)
        verified = (bool(checks) and all(check.status == "passed" for check in checks)
                    and any(isinstance(r.check, OutputCheck) for r in requirements))
        if verified:
            return AgentOutput(response.message.text(), usage=usage, response=response.message,
                               draft_call_id=response.call_id, delivery_checks=checks,
                               observed_calls=tuple(self.observed))

        text = "The answer remains unresolved. "
        if checks:
            text += "Current task checks: " + ", ".join(f"{c.requirement_id} {c.status}" for c in checks) + ". "
        passed_tools = [c for c in checks if c.call_id is not None and c.status == "passed"]
        failed_output = any(isinstance(r.check, OutputCheck) and c.status != "passed"
                            for r, c in zip(requirements, checks))
        if self.observed:
            text += "\n\nCurrent local observations\n" + "\n".join(f"- {v}" for v in self.observed.values())
        for check in passed_tools:
            text += f"\nRequirement {check.requirement_id} matched the tool result at event {check.source_seq}."
        if response.stop_reason != "end_turn":
            text += "The model did not finish an answer."
        elif (self.observed or passed_tools) and not failed_output:
            text += "\n\nProvisional interpretation (unverified)\n\n"
            text += "\n".join("> " + line for line in response.message.text().splitlines())
        else:
            text += ("\nThe proposed answer did not satisfy the current requirements." if checks else
                     "No successful current check supports the proposed answer.")
        text = text.encode()[:task.limits.max_response_bytes].decode(errors="ignore")
        message = Message(role=Role.ASSISTANT, blocks=(TextBlock(text=text),))
        return AgentOutput(text, "incomplete", "answer_unverified", usage, response=message,
                           draft_call_id=response.call_id, delivery_checks=checks,
                           observed_calls=tuple(self.observed))


async def ordinary_examples(loop, task, project):
    dispatcher = loop.dispatcher
    if not dispatcher.owns_primary_coordination() or task.handoff_id or len(task.prompt) > 2048:
        return None
    budget = dispatcher.scope.budget
    if budget.model_calls + 2 > budget.limits.max_model_calls or budget.usage.limits.enabled:
        return None
    catalog = getattr(loop.provider, "catalog", None)
    if catalog is None:
        return None
    from harness.catalog import UnknownAliasError, UnknownBackendError
    try:
        model = catalog.resolve(loop.model)
    except (ValueError, UnknownAliasError, UnknownBackendError):
        return None
    if model.execution_kind != "inference" or not model.local or dispatcher.scope.resources is None:
        return None
    resource = dispatcher.scope.resources.snapshot(model)
    if resource.status != "ready" or resource.stale:
        return None

    # Automatic review reads only bounded history. Explicit review can inspect a larger session.
    session = loop.session
    from harness.project_examples import project_examples
    from harness.log import TornLogError
    try:
        examples = await asyncio.to_thread(session_examples, session.base, session.id, before_seq=session.last_seq,
            project=project, same_project_only=True, after_run_seq=loop.session_entry_seq if project is None else 0,
            max_bytes=2 * 1024 * 1024)
    except (ValueError, OSError, TornLogError):
        examples = ()
    if project:
        examples += await project_examples(loop, project=project)
    if not examples:
        return None
    try:
        observation, selected = await select_examples(loop.semantics, model=loop.model,
                                                     query=task.prompt, examples=examples)
    except (OSError, ValueError, KeyError, BlobIntegrityError, MissingBlobError, TornLogError) as exc:
        session.append(ErrorRaised(where="semantic:context_selection",
            message=f"Historical example selection unavailable for this turn: {type(exc).__name__}."))
        return None
    return ExampleContext(observation.id, selected, loop.model) if selected else None
