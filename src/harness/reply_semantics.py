"""Bind native shadow message interpretation to the conversation the operator saw."""

import asyncio
from dataclasses import dataclass
import hashlib
import json

from harness.agent import AgentResult
from harness.blobs import BlobIntegrityError, MissingBlobError
from harness.events import AgentRunFinished, AgentRunStarted, SessionStarted, UserMessage
from harness.semantics import ReplyOrigin


@dataclass(frozen=True)
class DeliveredReply:
    request_seq: int
    request: str
    finish_seq: int
    result: AgentResult
    agent: str | None


class ReplyHistory:
    """Small projection shared by live delivery and resume, including historical journals."""

    def __init__(self):
        self.last = None
        self.pending_prior = None
        self.root = None
        self.request = None
        self.ambiguous = False
        self.child_session = False

    def apply(self, row):
        event = row.event
        if isinstance(event, SessionStarted):
            self.child_session = event.parent_session_id is not None
        elif isinstance(event, AgentRunStarted):
            if event.parent_run_id is None:
                self.pending_prior = self.last
                self.last = self.request = None
                self.root = event
                self.ambiguous = self.child_session or event.handoff_id is not None or event.runtime != "harness"
            elif self.root:
                # UserMessage has no run ID in historical journals.
                self.ambiguous |= self.request is None
        elif isinstance(event, UserMessage) and self.root:
            if self.request is None:
                self.request = row
            else:
                self.ambiguous = True
        elif isinstance(event, AgentRunFinished) and self.root and self.root.run_id == event.result.run_id:
            result = event.result
            if (not self.ambiguous and self.request and self.root.task_id == result.task_id
                    and event.purpose == "conversation" and result.output):
                self.last = DeliveredReply(self.request.seq, self.request.event.text, row.seq, result, self.root.agent)
            self.root = None


def visible_answer(result, blobs):
    # Older ordinary turns publish only the output blob. The optional inline
    # assistant message can include reasoning, which is never part of this input.
    if result.output is None or result.output.size > 16384:
        return None
    inline = None
    if result.response is not None:
        if (result.response.role != "assistant"
                or sum(len(b.text) for b in result.response.blocks if b.kind == "text") > result.output.size):
            return None
        inline = result.response.text()
        if hashlib.sha256(inline.encode()).hexdigest() != result.output.sha256:
            return None
    if blobs is None:
        return inline
    try:
        return blobs.get(result.output).decode("utf-8")
    except (UnicodeError, OSError, BlobIntegrityError, MissingBlobError):
        return None


def exchange_text(request, answer, followup):
    if answer is None or max(len(request), len(answer), len(followup)) > 16384:
        return None
    return json.dumps(dict(previous_request=request, assistant_reply=answer, followup=followup),
                      ensure_ascii=False, separators=(",", ":"))


def exchange_for_origin(by_seq, origin, blobs=None):
    """Reconstruct a source-addressed exchange when projecting a semantic finding."""
    request = by_seq.get(origin.request_seq)
    finish = by_seq.get(origin.run_finish_seq)
    followup = by_seq.get(origin.followup_seq)
    if not (request and finish and followup and isinstance(request.event, UserMessage)
            and isinstance(followup.event, UserMessage) and isinstance(finish.event, AgentRunFinished)
            and followup.event.reply_to_seq == finish.seq and finish.event.purpose == "conversation"):
        return None
    result = finish.event.result
    return exchange_text(request.event.text, visible_answer(result, blobs), followup.event.text)


def _local_alias(catalog, primary, resources):
    """Prefer the local primary, otherwise use an unambiguous configured local resource."""
    from harness.catalog import UnknownAliasError, UnknownBackendError
    if catalog is None:
        return None, "no_local_model"
    aliases = [alias for alias in catalog.aliases() if "local" in catalog.entries[alias]]
    if primary in aliases:
        return primary, None
    if len(aliases) == 1:
        return aliases[0], None
    ready = []
    for alias in aliases:
        try:
            status = resources.snapshot(catalog.resolve(alias))
        except (KeyError, ValueError, UnknownAliasError, UnknownBackendError):
            continue
        if status.status == "ready" and not status.stale:
            ready.append(alias)
    if len(ready) == 1:
        return ready[0], None
    return None, "ambiguous_local_model" if aliases else "no_local_model"


async def observe_reply(loop, prior, followup, remaining_time, *, abstain_reason=None):
    from harness.catalog import UnknownAliasError, UnknownBackendError
    from harness.events import ErrorRaised
    from harness.log import TornLogError
    session = loop.session
    origin = ReplyOrigin(request_seq=prior.request_seq, run_finish_seq=prior.finish_seq, followup_seq=followup.seq)
    answer = visible_answer(prior.result, session.blobs)
    text = exchange_text(prior.request, answer, followup.event.text)
    catalog = getattr(loop.provider, "catalog", None)
    alias, reason = _local_alias(catalog, loop.model, loop.dispatcher.scope.resources)
    budget = loop.dispatcher.scope.budget
    if abstain_reason is not None:
        reason = abstain_reason
    elif text is None:
        reason = "input_limit" if prior.result.output.size > 16384 or answer is not None else "integrity"
    elif budget.model_calls >= budget.limits.max_model_calls or remaining_time <= 0:
        reason = "budget"
    elif alias is not None:
        try:
            resolved = catalog.resolve(alias)
            if resolved.execution_kind != "inference" or resolved.local is None:
                reason = "no_local_model"
        except (UnknownAliasError, UnknownBackendError, KeyError, ValueError):
            reason = "unavailable"
    try:
        async with asyncio.timeout(max(0, remaining_time) if not reason else None):
            await loop.semantics.interpret(text or "", model=alias or loop.model, reply_origin=origin,
                                           abstain_reason=reason)
    except TimeoutError:
        # The semantic service records the interrupted observation. The delivered
        # primary answer already has its own terminal event.
        pass
    except (OSError, ValueError, KeyError, BlobIntegrityError, MissingBlobError, TornLogError) as exc:
        # Loading a selected prompt can fail before the service has a valid
        # prompt identity for an observation. Preserve the primary delivery.
        session.append(ErrorRaised(where="semantic:reply", message=(
            f"Shadow interpretation unavailable for reply {origin.followup_seq} to event "
            f"{origin.run_finish_seq}: {type(exc).__name__}")))
