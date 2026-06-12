"""Skills: loaded markdown behind a tiny protocol-shaped surface.

invoke_skill returns the skill body into the transcript (the model reads it
as a tool result — the dispatcher's blob spill applies to huge skills)."""

from typing import Any

from harness.frontmatter import SkillDef
from harness.hooks import Inject
from harness.tools import ToolSpec
from harness.types import ToolName


class SkillSet:
    def __init__(self, skills: list[SkillDef] | tuple[SkillDef, ...] = ()) -> None:
        self._skills = {s.name: s for s in skills}

    def get(self, name: str) -> SkillDef | None:
        return self._skills.get(name)

    def all(self) -> tuple[SkillDef, ...]:
        return tuple(self._skills.values())


class InvokeSkillTool:
    """Native tool: pull a skill's instructions into the conversation."""

    def __init__(self, skills: SkillSet) -> None:
        listing = "; ".join(f"{s.name}: {s.description}" for s in skills.all())
        self.spec = ToolSpec(
            name=ToolName("invoke_skill"),
            description=f"Load a skill's instructions. Available: {listing or '(none)'}",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        )
        self._skills = skills

    async def __call__(self, args: dict[str, Any]) -> str:
        skill = self._skills.get(str(args.get("name", "")))
        if skill is None:
            available = ", ".join(s.name for s in self._skills.all()) or "(none)"
            return f"unknown skill; available: {available}"
        return f"# Skill: {skill.name}\n\n{skill.body}"


def skills_inventory_hook(skills: SkillSet):
    """SESSION_START hook injecting the inventory (registered by the loader
    wiring when any skills exist — mirrors mcp-instructions)."""

    def hook(ctx) -> list[Inject]:
        if not skills.all():
            return []
        lines = "\n".join(f"- {s.name}: {s.description}" for s in skills.all())
        return [Inject(text=f"## Skills (load with invoke_skill)\n\n{lines}")]

    return hook
