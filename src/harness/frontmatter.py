"""Markdown + YAML frontmatter, pydantic-validated.

The native format for skills/commands/agents - and deliberately the same
surface Claude Code uses, so the importer (build item 9) converts by
re-validating, not rewriting.
"""

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


class FrontmatterError(Exception):
    pass


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise FrontmatterError("missing frontmatter (file must start with '---')")
    end = text.find("\n---", 4)
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


def _load(path: Path, model: type[_Def]):
    try:
        meta, body = split_frontmatter(path.read_text())
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
