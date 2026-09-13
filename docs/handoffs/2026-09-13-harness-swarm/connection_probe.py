import asyncio, json
from pathlib import Path
from harness.cli import build_kernel
from harness.mcp_config import load_mcp_file
from harness.provider import FakeProvider
from harness.permissions import PermissionEngine, RuleSet, PermissionRule
from harness.hooks import ProposedToolCall
from harness.types import ModelId, ToolName, CallId

async def main():
    root=Path(__file__).parent
    kernel=build_kernel(provider=FakeProvider([]),base_dir=root/"connection-only",model=ModelId("no-inference"),mcp=load_mcp_file(root/"mcp.toml",source="adhoc"),permissions=PermissionEngine([RuleSet(rules=[PermissionRule(action="allow",tool="mcp__router__workflow__workflow_get_state")])]))
    try:
        warnings=await kernel.mcp.start()
        await kernel.loop.start()
        kernel.mcp.flush_events()
        names=[str(s.name) for s in kernel.loop.dispatcher.registry.specs()]
        outcome=await kernel.loop.dispatcher.dispatch_tool(ProposedToolCall(call_id=CallId("workflow-state-probe"),tool=ToolName("mcp__router__workflow__workflow_get_state"),args={"workflow_id":"orchestrate"}))
        record={"session_id":str(kernel.session.id),"warnings":warnings,"registered":names,"result":outcome.read_text()}
        (root/"connection-report.json").write_text(json.dumps(record,indent=2)+"\n")
        print(json.dumps(record))
    finally:
        await kernel.mcp.stop()
        kernel.mcp.flush_events()
        await kernel.resources.close(emit=kernel.session.append)
        kernel.session.close()

asyncio.run(main())
