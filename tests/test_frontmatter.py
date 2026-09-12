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


def test_split_frontmatter_crlf_normalized():
    meta, body = split_frontmatter("---\r\nname: x\r\ndescription: d\r\n---\r\nbody\r\n")
    assert meta["name"] == "x"
    assert "body" in body


def test_split_frontmatter_empty_block_is_clear_error():
    with pytest.raises(FrontmatterError) as exc:
        split_frontmatter("---\n---\nbody")
    assert "mapping" in str(exc.value)


def test_load_agent_scalar_tools_coerces(tmp_path):
    path = tmp_path / "a.md"
    path.write_text("---\nname: a\ndescription: d\ntools: invoke_skill\n---\nbody")
    assert load_agent(path).tools == ("invoke_skill",)


def test_load_agent_bad_strategy_name_rejected(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text(
        "---\nname: bad\ndescription: d\nstrategy: typo\nexperts:\n  - a\n---\nbody"
    )
    with pytest.raises(FrontmatterError) as exc:
        load_agent(path)
    assert "strategy" in str(exc.value)


def test_load_agent_strategy_without_experts_rejected(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text("---\nname: bad\ndescription: d\nstrategy: ensemble\n---\nbody")
    with pytest.raises(FrontmatterError) as exc:
        load_agent(path)
    assert "experts" in str(exc.value)


def test_load_agent_valid_strategy_and_experts_loads(tmp_path):
    path = tmp_path / "coord.md"
    path.write_text(
        "---\nname: coord\ndescription: d\nstrategy: panel\nexperts:\n  - a\n  - b\n---\nbody"
    )
    agent = load_agent(path)
    assert agent.strategy == "panel"
    assert agent.experts == ("a", "b")


@pytest.mark.parametrize("strategy", [None, "panel", "draft_refine"])
@pytest.mark.parametrize("require_checks", ["true", "false"])
def test_load_agent_rejects_explicit_require_checks_outside_checked_strategies(
    tmp_path, strategy, require_checks
):
    path = tmp_path / "bad.md"
    strategy_fields = f"strategy: {strategy}\nexperts: [a, b]\n" if strategy else ""
    path.write_text(
        "---\nname: bad\ndescription: d\n"
        f"{strategy_fields}require_checks: {require_checks}\n---\nbody"
    )
    with pytest.raises(FrontmatterError, match="only supported by the escalate and ensemble strategies"):
        load_agent(path)


@pytest.mark.parametrize("strategy", [None, "ensemble", "panel", "draft_refine", "escalate"])
def test_load_agent_allows_omitted_require_checks(tmp_path, strategy):
    path = tmp_path / "default.md"
    strategy_fields = f"strategy: {strategy}\nexperts: [a, b]\n" if strategy else ""
    path.write_text(f"---\nname: default\ndescription: d\n{strategy_fields}---\nbody")
    assert load_agent(path).require_checks is False


@pytest.mark.parametrize("require_checks", [True, False])
@pytest.mark.parametrize("strategy", ["escalate", "ensemble"])
def test_load_agent_checked_strategies_accept_both_require_checks_values(tmp_path, require_checks, strategy):
    path = tmp_path / "checked.md"
    path.write_text(
        f"---\nname: checked\ndescription: d\nstrategy: {strategy}\nexperts: [a, b]\n"
        f"require_checks: {str(require_checks).lower()}\n---\nbody"
    )
    assert load_agent(path).require_checks is require_checks


def test_load_agent_rejects_non_positive_max_output_chars(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text("---\nname: bad\ndescription: d\nmax_output_chars: 0\n---\nbody")
    with pytest.raises(FrontmatterError) as exc:
        load_agent(path)
    assert "max_output_chars" in str(exc.value)


def test_load_agent_accepts_max_output_chars(tmp_path):
    path = tmp_path / "ok.md"
    path.write_text("---\nname: ok\ndescription: d\nmax_output_chars: 2000\n---\nbody")
    assert load_agent(path).max_output_chars == 2000


def test_load_agent_without_max_output_chars_is_unbounded(tmp_path):
    path = tmp_path / "plain.md"
    path.write_text("---\nname: plain\ndescription: d\n---\nbody")
    assert load_agent(path).max_output_chars is None
