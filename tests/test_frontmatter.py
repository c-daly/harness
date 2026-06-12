"""Markdown + YAML frontmatter → pydantic-validated definitions."""

import pytest

from harness.frontmatter import (
    FrontmatterError,
    load_agent,
    load_command,
    load_skill,
    split_frontmatter,
)

SKILL_MD = """---
name: remembering
description: When and how to write memories
---

# Remembering

Write a memory when the user states a durable preference.
"""


def test_split_frontmatter_happy():
    meta, body = split_frontmatter(SKILL_MD)
    assert meta == {"name": "remembering", "description": "When and how to write memories"}
    assert body.startswith("# Remembering")


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("no frontmatter at all", "frontmatter"),
        ("---\nname: x\n", "closing"),                      # unterminated
        ("---\n- just\n- a list\n---\nbody", "mapping"),    # non-dict yaml
        ("---\nname: [unclosed\n---\nbody", "YAML"),        # yaml parse error
    ],
)
def test_split_frontmatter_errors(text, fragment):
    with pytest.raises(FrontmatterError) as exc:
        split_frontmatter(text)
    assert fragment.lower() in str(exc.value).lower()


def test_load_skill(tmp_path):
    path = tmp_path / "remembering.md"
    path.write_text(SKILL_MD)
    skill = load_skill(path)
    assert skill.name == "remembering"
    assert "durable preference" in skill.body


def test_load_skill_invalid_name(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text("---\nname: \"bad name!\"\ndescription: d\n---\nbody")
    with pytest.raises(FrontmatterError) as exc:
        load_skill(path)
    assert "name" in str(exc.value)


def test_load_skill_missing_description(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text("---\nname: ok\n---\nbody")
    with pytest.raises(FrontmatterError) as exc:
        load_skill(path)
    assert "description" in str(exc.value)


def test_load_command(tmp_path):
    path = tmp_path / "brief.md"
    path.write_text("---\nname: brief\ndescription: Show the memory brief\n---\nShow me: $ARGUMENTS")
    command = load_command(path)
    assert command.name == "brief"
    assert "$ARGUMENTS" in command.body


def test_load_agent(tmp_path):
    path = tmp_path / "curator.md"
    path.write_text(
        "---\nname: curator\ndescription: Curates memories\n"
        "tools:\n  - invoke_skill\nmodel: fake:echo\n---\nYou are the curator."
    )
    agent = load_agent(path)
    assert agent.name == "curator"
    assert agent.tools == ("invoke_skill",)
    assert agent.model == "fake:echo"
    assert agent.body.startswith("You are the curator.")


def test_load_agent_defaults(tmp_path):
    path = tmp_path / "open.md"
    path.write_text("---\nname: open\ndescription: All tools\n---\nDo anything.")
    agent = load_agent(path)
    assert agent.tools is None           # None = all tools
    assert agent.model is None
