from harness.cli import Kernel, build_kernel, run_once
from harness.provider import FakeProvider, text_turn
from harness.types import ModelId


async def test_build_kernel_wires_dispatch_agent_tool(tmp_path):
    kernel = build_kernel(
        provider=FakeProvider([text_turn("ok")]),
        base_dir=tmp_path, model=ModelId("fake"),
    )
    assert isinstance(kernel, Kernel)
    assert "dispatch_agent" in [s.name for s in kernel.registry.specs()]


async def test_run_once_returns_final_text_and_closes_session(tmp_path):
    kernel = build_kernel(
        provider=FakeProvider([text_turn("the answer")]),
        base_dir=tmp_path, model=ModelId("fake"),
    )
    assert await run_once(kernel, "question?") == "the answer"
    # session closed: log readable, ends with SessionEnded
    from harness.log import read_session
    envs = read_session(tmp_path, kernel.session.id)
    assert envs[-1].event.type == "session_ended"
