"""The redaction seam: identity by default, applied before blob-spill and at append."""

from harness.dispatcher import Dispatcher
from harness.events import ToolCallCompleted
from harness.hooks import HookBus, ProposedToolCall
from harness.interaction import HeadlessResolver
from harness.log import read_session
from harness.session import Session
from harness.tools import ToolRegistry, ToolSpec
from harness.types import CallId, SessionId, ToolName


class Leaky:
    spec = ToolSpec(name=ToolName("leak"), description="", parameters={})

    async def __call__(self, args):
        return "token=SECRET123 done"


async def test_dispatcher_redact_runs_before_spill(tmp_path):
    session = Session(tmp_path, SessionId("s1"))
    session.start()
    reg = ToolRegistry()
    reg.register(Leaky())
    disp = Dispatcher(
        session=session, registry=reg, hooks=HookBus(), resolver=HeadlessResolver(),
        redact=lambda s: s.replace("SECRET123", "[REDACTED]"),
    )
    out = await disp.dispatch_tool(
        ProposedToolCall(call_id=CallId("c1"), tool=ToolName("leak"), args={})
    )
    session.close()
    assert "[REDACTED]" in out.text and "SECRET123" not in out.text


async def test_default_redact_is_identity(tmp_path):
    session = Session(tmp_path, SessionId("s2"))
    session.start()
    reg = ToolRegistry()
    reg.register(Leaky())
    disp = Dispatcher(session=session, registry=reg, hooks=HookBus(), resolver=HeadlessResolver())
    out = await disp.dispatch_tool(
        ProposedToolCall(call_id=CallId("c1"), tool=ToolName("leak"), args={})
    )
    session.close()
    assert out.text == "token=SECRET123 done"


async def test_session_redactor_rewrites_event_at_append(tmp_path):
    def mask(event):
        if isinstance(event, ToolCallCompleted) and event.result_text:
            return event.model_copy(update={"result_text": event.result_text.replace("X", "#")})
        return event
    session = Session(tmp_path, SessionId("s3"), redactors=[mask])
    session.start()
    session.append(ToolCallCompleted(call_id=CallId("c1"), result_text="XYZ", is_error=False))
    session.close()
    completed = [
        e.event for e in read_session(tmp_path, SessionId("s3"))
        if isinstance(e.event, ToolCallCompleted)
    ]
    assert completed[0].result_text == "#YZ"
