"""Agent-defined task limits reach the child without widening its parent scope."""

import pytest

from harness.cli import build_kernel
from harness.events import AgentRunStarted
from harness.execution import ExecutionLimits
from harness.frontmatter import AgentDef
from harness.log import read_session
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.tools import ToolSpec
from harness.types import ModelId, ToolName


class CaptureProvider(FakeProvider):
    def infer(self, request):
        self.requests = getattr(self, "requests", []) + [request]
        return super().infer(request)


class Ping:
    spec = ToolSpec(
        name=ToolName("ping"), description="A harmless step", parameters={"type": "object"}
    )

    async def __call__(self, args):
        return "pong"


@pytest.mark.parametrize("checked", [False, True])
async def test_explicit_agent_limits_reach_inference_and_recorded_task(tmp_path, checked):
    provider = CaptureProvider(
        [tool_call_turn("", "ping", {}) for _ in range(21)] + [text_turn("done")]
    )
    kernel = build_kernel(provider=provider, base_dir=tmp_path, model=ModelId("fake"))
    kernel.registry.register(Ping())
    kernel.runner.agents["worker"] = AgentDef.model_validate(
        {
            "name": "worker",
            "description": "d",
            "limits": {"max_iterations": 24, "max_output_tokens": 16384},
        }
    )
    result = await kernel.runner.run_result(
        prompt="work",
        model=None,
        parent=kernel.session,
        agent="worker",
        requirements=() if checked else None,
    )
    kernel.session.close()
    assert result.status == "completed" and result.text == "done"
    assert len(provider.requests) == 22
    assert all(request.max_output_tokens == 16384 for request in provider.requests)
    events = read_session(tmp_path, result.child_session_id)
    limits = next(e.event.limits for e in events if isinstance(e.event, AgentRunStarted))
    assert limits["max_iterations"] == 24
    assert limits["max_output_tokens"] == 16384
    assert limits["timeout_seconds"] == 600


async def test_agent_limits_cannot_raise_shared_model_call_budget(tmp_path):
    provider = CaptureProvider([tool_call_turn("", "ping", {}) for _ in range(4)])
    kernel = build_kernel(
        provider=provider,
        base_dir=tmp_path,
        model=ModelId("fake"),
        execution_limits=ExecutionLimits(max_model_calls=2),
    )
    kernel.registry.register(Ping())
    kernel.runner.agents["worker"] = AgentDef.model_validate(
        {
            "name": "worker",
            "description": "d",
            "limits": {"max_iterations": 24, "max_output_tokens": 16384},
        }
    )
    result = await kernel.runner.run_result(
        prompt="work", model=None, parent=kernel.session, agent="worker"
    )
    kernel.session.close()
    assert result.status == "incomplete" and result.reason == "budget"
    assert len(provider.requests) == 2


def test_coordination_limits_refused_instead_of_silently_ignored():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="limits.*direct"):
        AgentDef.model_validate(
            {
                "name": "team",
                "description": "d",
                "strategy": "ensemble",
                "experts": ["fake"],
                "limits": {"max_output_tokens": 16384},
            }
        )


async def test_context_and_session_limits_still_narrow_agent_settings(tmp_path):
    from harness.context import ContextPolicy, ResponsePolicy

    provider = CaptureProvider([text_turn("done")])
    kernel = build_kernel(
        provider=provider,
        base_dir=tmp_path,
        model=ModelId("fake"),
        execution_limits=ExecutionLimits(task_timeout_seconds=45),
        context_policy=ContextPolicy(response=ResponsePolicy(max_output_tokens=1024)),
    )
    kernel.runner.agents["worker"] = AgentDef.model_validate(
        {"name": "worker", "description": "d", "limits": {"max_output_tokens": 16384}}
    )
    result = await kernel.runner.run_result(
        prompt="work", model=None, parent=kernel.session, agent="worker"
    )
    kernel.session.close()
    assert result.status == "completed"
    assert provider.requests[0].max_output_tokens == 1024
    limits = next(
        e.event.limits
        for e in read_session(tmp_path, result.child_session_id)
        if isinstance(e.event, AgentRunStarted)
    )
    assert limits["timeout_seconds"] == 45


@pytest.mark.parametrize(
    "limits", [{"max_output_tokens": -1}, {"max_iterations": True}, {"unknown_cap": 10}]
)
def test_invalid_agent_limits_rejected_at_definition_load(limits):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentDef.model_validate({"name": "worker", "description": "d", "limits": limits})
