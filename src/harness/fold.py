"""Fold facts into state. Never executes side effects; never runs hooks.

Assumes a well-formed log: seqs strictly increasing and unique (the writer's
single monotonic counter guarantees this; the reader's repair path truncates,
never duplicates). Unknown EVENT types are skipped (UnknownEvent falls through
the dispatch); unknown BLOCK kinds inside ModelCallCompleted.message fail
loudly in Message.model_validate — a conscious asymmetry: new block kinds
require a binary upgrade, new event types must not break old readers.
"""

from dataclasses import dataclass, field

from harness.events import (
    CompactionApplied,
    Envelope,
    ModelCallCompleted,
    ToolCallAborted,
    ToolCallCancelled,
    ToolCallCompleted,
    ToolCallProposed,
    UserMessage,
)
from harness.messages import Message
from harness.types import CallId


@dataclass
class FoldedState:
    messages: list[Message] = field(default_factory=list)
    # call_id -> seq of the proposing event; an intent with no terminal fact
    open_intents: dict[CallId, int] = field(default_factory=dict)
    last_seq: int = 0
    # seq -> index range bookkeeping for compaction
    _msg_seqs: list[int] = field(default_factory=list)

    def _append(self, seq: int, message: Message) -> None:
        self.messages.append(message)
        self._msg_seqs.append(seq)


def fold(envelopes: list[Envelope]) -> FoldedState:
    state = FoldedState()
    for env in envelopes:
        ev = env.event
        state.last_seq = max(state.last_seq, env.seq)
        if isinstance(ev, UserMessage):
            state._append(env.seq, Message.user_text(ev.text))
        elif isinstance(ev, ModelCallCompleted):
            state._append(env.seq, Message.model_validate(ev.message))
        elif isinstance(ev, ToolCallProposed):
            state.open_intents[ev.call_id] = env.seq
        elif isinstance(ev, ToolCallCompleted):
            state.open_intents.pop(ev.call_id, None)
            state._append(
                env.seq,
                Message.tool_result(
                    ev.call_id, text=ev.result_text, blob=ev.result_blob, is_error=ev.is_error
                ),
            )
        elif isinstance(ev, (ToolCallCancelled, ToolCallAborted)):
            state.open_intents.pop(ev.call_id, None)
            state._append(
                env.seq,
                Message.tool_result(ev.call_id, text="(call did not complete)", is_error=True),
            )
        elif isinstance(ev, CompactionApplied):
            kept_msgs, kept_seqs = [], []
            for msg, seq in zip(state.messages, state._msg_seqs):
                if not (ev.from_seq <= seq <= ev.to_seq):
                    kept_msgs.append(msg)
                    kept_seqs.append(seq)
            summary = Message.system_text(f"Summary of earlier conversation: {ev.summary}")
            state.messages = [summary, *kept_msgs]
            state._msg_seqs = [env.seq, *kept_seqs]
    return state


def resume_repairs(state: FoldedState) -> list[ToolCallAborted]:
    """One ToolCallAborted per dangling intent. The fold cannot know whether the
    side effect ran, so it surfaces the uncertainty instead of guessing.

    Caller contract: append these to the session log (EventLogWriter.append)
    before the next fold — that closes the intents and renders the aborted
    calls as error tool-results in the rebuilt transcript.

    Only TOOL intents are repaired: an incomplete model call terminates by
    exception and its turn never enters the transcript."""
    return [
        ToolCallAborted(call_id=call_id, reason="dangling intent at resume (crash?)")
        for call_id in sorted(state.open_intents)
    ]
