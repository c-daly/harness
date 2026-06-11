# tests/test_e2e_phase3.py
"""Phase 3 milestone: rules deny/ask/allow through a live kernel, a grant
persisting across KERNELS (the 'always allow' future), deny staying absolute."""

from pathlib import Path

from harness.cli import build_kernel, run_once
from harness.events import PermissionRequested
from harness.interaction import ScriptedResolver
from harness.log import read_session
from harness.permissions import PermissionEngine, RuleSet, default_engine  # noqa: F401
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.types import ModelId, ToolName


def _config(tmp_path) -> tuple[Path, Path]:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    project = tmp_path / "proj"
    (project / ".harness").mkdir(parents=True)
    (project / ".harness" / "permissions.toml").write_text(
        'default = "allow"\n\n'
        "[[rules]]\n"
        'action = "deny"\ntool = "dispatch_agent"\nmatch = { prompt = "*secret*" }\n\n'
        "[[rules]]\n"
        'action = "ask"\ntool = "dispatch_agent"\n'
    )
    return project, cfg


async def test_permission_lifecycle_across_kernels(tmp_path):
    project, cfg = _config(tmp_path)

    # Kernel 1: ask -> approved -> persisted grant
    engine1 = default_engine(project_dir=project, config_home=cfg)
    assert engine1 is not None

    class PersistingResolver(ScriptedResolver):
        def __init__(self, engine):
            super().__init__([True])
            self._engine = engine

        async def resolve(self, request):
            answer = await super().resolve(request)
            if answer:
                self._engine.grant("dispatch_agent", persist=True)
            return answer

    kernel1 = build_kernel(
        provider=FakeProvider([
            tool_call_turn("delegating", ToolName("dispatch_agent"), {"prompt": "harmless"}),
            text_turn("child done"),
            text_turn("run one complete"),
        ]),
        base_dir=tmp_path / "base1", model=ModelId("fake"),
        permissions=engine1, resolver=PersistingResolver(engine1),
    )
    sid1 = kernel1.session.id
    assert await run_once(kernel1, "go") == "run one complete"
    asks1 = [e for e in read_session(tmp_path / "base1", sid1)
             if isinstance(e.event, PermissionRequested)]
    assert len(asks1) == 1

    # Kernel 2 (fresh discovery): the persisted grant silences the ask
    engine2 = default_engine(project_dir=project, config_home=cfg)
    kernel2 = build_kernel(
        provider=FakeProvider([
            tool_call_turn("again", ToolName("dispatch_agent"), {"prompt": "harmless"}),
            text_turn("child done"),
            text_turn("run two complete"),
        ]),
        base_dir=tmp_path / "base2", model=ModelId("fake"),
        permissions=engine2, resolver=ScriptedResolver([]),  # NO answers available
    )
    sid2 = kernel2.session.id
    assert await run_once(kernel2, "go again") == "run two complete"
    asks2 = [e for e in read_session(tmp_path / "base2", sid2)
             if isinstance(e.event, PermissionRequested)]
    assert asks2 == []

    # Deny stays absolute even after the grant
    assert engine2.decide("dispatch_agent", {"prompt": "the secret plans"}) == "deny"


async def test_no_config_means_no_engine_means_phase2_behavior(tmp_path):
    assert default_engine(project_dir=tmp_path, config_home=tmp_path / "cfg") is None
    kernel = build_kernel(
        provider=FakeProvider([text_turn("plain")]), base_dir=tmp_path, model=ModelId("fake"),
    )
    assert await run_once(kernel, "hi") == "plain"
