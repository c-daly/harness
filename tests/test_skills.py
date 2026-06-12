"""Skills behind a protocol: invoke_skill tool + SESSION_START inventory."""

from harness.frontmatter import SkillDef
from harness.hooks import Inject
from harness.skills import InvokeSkillTool, SkillSet, skills_inventory_hook


def make_skills():
    return SkillSet(
        [
            SkillDef(
                name="remembering", description="When to write memories", body="Write when..."
            ),
            SkillDef(name="reviewing", description="How to review", body="Review by..."),
        ]
    )


async def test_invoke_skill_returns_body():
    tool = InvokeSkillTool(make_skills())
    assert str(tool.spec.name) == "invoke_skill"
    result = await tool({"name": "remembering"})
    assert "Write when..." in result
    assert "remembering" in result


async def test_invoke_skill_unknown_is_error_result():
    tool = InvokeSkillTool(make_skills())
    result = await tool({"name": "nope"})
    assert "unknown skill" in result
    assert "remembering" in result  # the error lists what IS available


async def test_invoke_skill_lists_available_in_description():
    tool = InvokeSkillTool(make_skills())
    assert "remembering" in tool.spec.description
    assert "reviewing" in tool.spec.description


def test_inventory_hook_injects_listing():
    hook = skills_inventory_hook(make_skills())
    contributions = hook({"session_id": "x"})
    (inject,) = contributions
    assert isinstance(inject, Inject)
    assert "invoke_skill" in inject.text
    assert "remembering" in inject.text and "When to write memories" in inject.text
