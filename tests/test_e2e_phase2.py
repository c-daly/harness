# tests/test_e2e_phase2.py
"""Phase 2 milestone: same conversation, switchable models, pricing stamped.

Providers are FakeProviders standing in for two distinct models — the
switching mechanics (per-call model param, transcript carry-over, per-model
events) are kernel behavior and provider-independent; real-provider behavior
is covered by the conformance suites."""

from harness.cli import build_kernel, run_once  # noqa: F401  (build_kernel used)
from harness.events import ModelCallCompleted
from harness.log import read_session
from harness.provider import FakeProvider, text_turn
from harness.types import ModelId


async def test_mid_session_model_switch_carries_transcript(tmp_path):
    provider = FakeProvider([
        text_turn("answer from alpha"),
        text_turn("answer from beta, recalling context"),
    ])
    kernel = build_kernel(provider=provider, base_dir=tmp_path, model=ModelId("model-alpha"))
    sid = kernel.session.id
    await kernel.loop.start()
    first = await kernel.loop.run_turn("remember the number 42")
    assert first == "answer from alpha"

    kernel.loop.model = ModelId("model-beta")  # /model switch: per-call param, loop-held default
    second = await kernel.loop.run_turn("what number did I say?")
    await kernel.loop.end()
    kernel.session.close()
    assert second == "answer from beta, recalling context"

    # the second model SAW the full prior transcript
    second_call_messages = provider.calls[-1]
    texts = " ".join(
        b.text for m in second_call_messages for b in m.blocks if hasattr(b, "text")
    )
    assert "42" in texts and "answer from alpha" in texts

    # events record the switch
    envs = read_session(tmp_path, sid)
    models = [e.event.model for e in envs if isinstance(e.event, ModelCallCompleted)]
    assert models == ["model-alpha", "model-beta"]
