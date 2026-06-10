from harness.hooks import ProposedToolCall
from harness.interaction import HeadlessResolver, PermissionRequest, ScriptedResolver
from harness.types import CallId, ToolName

REQ = PermissionRequest(
    call_id=CallId("c1"),
    action=ProposedToolCall(call_id=CallId("c1"), tool=ToolName("bash"), args={}),
    reason="hook asked",
)


async def test_headless_resolver_denies_by_default():
    resolver = HeadlessResolver()
    assert await resolver.resolve(REQ) is False
    assert resolver.name == "headless-deny"


async def test_scripted_resolver_returns_scripted_answers_in_order():
    resolver = ScriptedResolver([True, False])
    assert await resolver.resolve(REQ) is True
    assert await resolver.resolve(REQ) is False


async def test_scripted_resolver_records_requests():
    resolver = ScriptedResolver([True])
    await resolver.resolve(REQ)
    assert resolver.seen[0].reason == "hook asked"
