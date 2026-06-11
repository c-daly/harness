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


async def test_build_kernel_with_catalog_pricing(tmp_path):
    catalog_toml = tmp_path / "models.toml"
    catalog_toml.write_text(
        "[models.fake]\nroute = \"fake:echo\"\n"
        "input_cost_per_token = 1e-6\noutput_cost_per_token = 2e-6\nverified = true\n"
    )
    from harness.catalog import Catalog
    from harness.events import ModelCallCompleted
    from harness.log import read_session

    resolved = Catalog.load(catalog_toml).resolve("fake")
    kernel = build_kernel(
        provider=FakeProvider([text_turn("ok")]), base_dir=tmp_path,
        model=resolved.route, pricing=resolved.pricing_dict(),
    )
    sid = kernel.session.id
    await run_once(kernel, "hi")
    envs = read_session(tmp_path, sid)
    completed = [e.event for e in envs if isinstance(e.event, ModelCallCompleted)]
    assert completed[0].pricing["input_cost_per_token"] == 1e-6


async def test_resume_flag_continues_session(tmp_path):
    from harness.log import read_session

    kernel = build_kernel(
        provider=FakeProvider([text_turn("first answer")]), base_dir=tmp_path, model=ModelId("fake")
    )
    sid = kernel.session.id
    await run_once(kernel, "first question")

    resumed = build_kernel(
        provider=FakeProvider([text_turn("second answer")]), base_dir=tmp_path,
        model=ModelId("fake"), resume_session_id=sid,
    )
    assert resumed.session.id == sid
    assert len(resumed.loop.history) >= 2  # prior user + assistant turns seeded
    result = await resumed.loop.run_turn("second question")
    await resumed.loop.end()
    resumed.session.close()
    assert result == "second answer"
    envs = read_session(tmp_path, sid)
    seqs = [e.seq for e in envs]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
