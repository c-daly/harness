"""Markdown + YAML frontmatter, pydantic-validated.

The native format for skills/commands/agents - and deliberately the same
surface Claude Code uses, so the importer (build item 9) converts by
re-validating, not rewriting.
"""

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
_VALID_STRATEGIES = frozenset({"ensemble", "panel", "draft_refine", "escalate"})


class FrontmatterError(Exception):
    pass


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    text = text.replace("\r\n", "\n")  # Windows/autocrlf files must parse identically
    if not text.startswith("---\n"):
        raise FrontmatterError("missing frontmatter (file must start with '---')")
    # limitation: an unindented '---' line inside a block scalar splits early (YAML itself
    # treats it as a document marker, so honest authors never hit this)
    end = text.find("\n---", 3)
    if end == -1:
        raise FrontmatterError("missing closing '---' for frontmatter")
    raw, body = text[4:end], text[end + 4 :].lstrip("\n")
    try:
        meta = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"YAML error in frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise FrontmatterError("frontmatter must be a YAML mapping")
    return meta, body


class _Def(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    description: str
    body: str = ""

    @field_validator("name")
    @classmethod
    def _name_ok(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value) or "__" in value:
            raise ValueError("name must match [A-Za-z0-9_-]+ and not contain '__'")
        return value


class SkillDef(_Def):
    pass


class CommandDef(_Def):
    pass


class AgentDef(_Def):
    tools: tuple[str, ...] | None = None  # None = all tools
    model: str | None = None
    # Mixture-of-Models coordination def: when `strategy` is set the agent fans
    # out to `experts` (positional aliases) via mixture.run_strategy instead of
    # running a single child loop.
    strategy: str | None = None  # ensemble | panel | draft_refine | escalate
    experts: tuple[str, ...] | None = None
    require_checks: bool = Field(default=False, strict=True)
    # None = unbounded. Bounds what the child RETURNS to its parent, not what
    # the child's own model produced.
    max_output_chars: int | None = None

    @field_validator("max_output_chars")
    @classmethod
    def _max_output_chars_positive(cls, value):
        if value is not None and value < 1:
            raise ValueError("max_output_chars must be a positive integer")
        return value

    @field_validator("tools", mode="before")
    @classmethod
    def _tools_scalar_ok(cls, value):
        if isinstance(value, str):
            return (value,)  # a bare scalar means a one-tool list
        return value

    @model_validator(mode="after")
    def _strategy_requires_valid_name_and_experts(self) -> "AgentDef":
        if "require_checks" in self.model_fields_set and self.strategy not in ("escalate", "ensemble"):
            raise ValueError("require_checks is only supported by the escalate and ensemble strategies")
        if self.strategy is None:
            return self
        if self.strategy not in _VALID_STRATEGIES:
            raise ValueError(
                f"strategy {self.strategy!r} must be one of {sorted(_VALID_STRATEGIES)}"
            )
        if not self.experts:
            raise ValueError(
                f"strategy {self.strategy!r} requires a non-empty 'experts' list"
            )
        return self


def _load(path: Path, model: type[_Def]):
    try:
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FrontmatterError(f"{path}: {exc}") from exc
    except FrontmatterError as exc:
        raise FrontmatterError(f"{path}: {exc}") from exc
    try:
        return model(**meta, body=body)
    except ValidationError as exc:
        raise FrontmatterError(f"{path}: {exc}") from exc
    except TypeError as exc:
        raise FrontmatterError(f"{path}: {exc}") from exc


def load_skill(path: Path) -> SkillDef:
    return _load(path, SkillDef)


def load_command(path: Path) -> CommandDef:
    return _load(path, CommandDef)


def load_agent(path: Path) -> AgentDef:
    return _load(path, AgentDef)
