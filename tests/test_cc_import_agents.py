"""Agent conversion: tool mapping, paren-pattern degrade, catalog model, custom-key strip."""

from pathlib import Path

from harness.catalog import Catalog
from harness.cc_import import RawDef, convert_agent
from harness.frontmatter import load_agent


CATALOG = Catalog(entries={"sonnet": {"route": "anthropic/claude-sonnet"}})


def _agent(meta, body="You explore.\n"):
    return RawDef(
        name=meta.get("name", "a"),
        meta=meta,
        body=body,
        source_path=Path("/x/agents") / f"{meta.get('name', 'a')}.md",
    )


def test_plain_tools_mapped_through_cc_tool_map():
    conv = convert_agent(
        _agent({"name": "scout", "description": "d", "tools": "Read, Bash, Grep"}), catalog=CATALOG
    )
    meta, _ = _split(conv.text)
    assert meta["tools"] == ["read_file", "bash", "grep"]
    assert any(e.kind == "rewrite" for e in conv.report)


def test_tools_as_yaml_list_also_works():
    conv = convert_agent(
        _agent({"name": "s", "description": "d", "tools": ["Read", "Edit"]}), catalog=CATALOG
    )
    meta, _ = _split(conv.text)
    assert meta["tools"] == ["read_file", "edit_file"]


def test_paren_pattern_degrades_to_bare_mapped_name():
    conv = convert_agent(
        _agent({"name": "s", "description": "d", "tools": "Bash(mcp*)"}), catalog=CATALOG
    )
    meta, _ = _split(conv.text)
    assert meta["tools"] == ["bash"]  # arg-scope dropped
    assert conv.degraded is True
    assert any(e.kind == "degraded" and "Bash(mcp*)" in e.detail for e in conv.report)


def test_mcp_prefixed_tool_kept_verbatim():
    conv = convert_agent(
        _agent({"name": "s", "description": "d", "tools": "mcp__memory__write, Read"}),
        catalog=CATALOG,
    )
    meta, _ = _split(conv.text)
    assert "mcp__memory__write" in meta["tools"]
    assert "read_file" in meta["tools"]


def test_unknown_bare_tool_dropped_with_report():
    conv = convert_agent(
        _agent({"name": "s", "description": "d", "tools": "Frobnicate, Read"}), catalog=CATALOG
    )
    meta, _ = _split(conv.text)
    assert meta["tools"] == ["read_file"]
    assert any(e.kind == "drop" and "Frobnicate" in e.detail for e in conv.report)


def test_no_parity_tool_dropped_and_agent_degraded():
    conv = convert_agent(
        _agent({"name": "s", "description": "d", "tools": "WebFetch, Read"}), catalog=CATALOG
    )
    meta, _ = _split(conv.text)
    assert meta["tools"] == ["read_file"]
    assert conv.degraded is True
    assert any(e.kind == "degraded" and "WebFetch" in e.detail for e in conv.report)


def test_resolvable_model_alias_kept():
    conv = convert_agent(
        _agent({"name": "s", "description": "d", "model": "sonnet"}), catalog=CATALOG
    )
    meta, _ = _split(conv.text)
    assert meta["model"] == "sonnet"


def test_unresolvable_model_alias_dropped_with_report():
    conv = convert_agent(
        _agent({"name": "s", "description": "d", "model": "gpt-9"}), catalog=CATALOG
    )
    meta, _ = _split(conv.text)
    assert "model" not in meta
    assert any(e.kind == "drop" and "gpt-9" in e.detail for e in conv.report)


def test_custom_frontmatter_keys_dropped_with_report():
    conv = convert_agent(
        _agent(
            {"name": "s", "description": "d", "max_output_chars": 2000, "can_write_files": False}
        ),
        catalog=CATALOG,
    )
    meta, _ = _split(conv.text)
    assert "max_output_chars" not in meta and "can_write_files" not in meta
    assert any("max_output_chars" in e.detail for e in conv.report)
    assert any("can_write_files" in e.detail for e in conv.report)


def test_all_tools_dropped_omits_tools_key_not_empty_list():
    # if every declared tool drops, omit `tools` so the agent does not become a no-tool agent
    # by an empty list the validator would still accept but which means all-tools natively;
    # the report makes the loss explicit.
    conv = convert_agent(
        _agent({"name": "s", "description": "d", "tools": "WebSearch, WebFetch"}), catalog=CATALOG
    )
    meta, _ = _split(conv.text)
    assert "tools" not in meta
    assert conv.degraded is True
    assert any(
        e.kind == "degraded" and "all declared tools dropped" in e.detail for e in conv.report
    )


def test_converted_agent_loads_as_native_agentdef(tmp_path):
    conv = convert_agent(
        _agent(
            {
                "name": "scout",
                "description": "d",
                "tools": "Read, Bash",
                "model": "sonnet",
                "max_output_chars": 10,
            },
            body="Use `Read`.\n",
        ),
        catalog=CATALOG,
    )
    out = tmp_path / "scout.md"
    out.write_text(conv.text)
    agent = load_agent(out)  # must not raise
    assert agent.tools == ("read_file", "bash")
    assert agent.model == "sonnet"
    assert "`read_file`" in agent.body


def _split(text):
    from harness.frontmatter import split_frontmatter

    return split_frontmatter(text)


def test_scalar_bool_tools_degrades_without_crash():
    # YAML coerces bare `tools: true` to a bool — must route through the drop
    # path as an unknown token, never crash or emit a garbage AgentDef
    conv = convert_agent(_agent({"name": "s", "description": "d", "tools": True}), catalog=CATALOG)
    meta, _ = _split(conv.text)
    assert "tools" not in meta
    assert any(e.kind == "drop" for e in conv.report)
    assert any(e.kind == "degraded" for e in conv.report)


def test_yaml_null_in_tools_list_filtered_silently():
    # tools: [Read, ~] — the null element must not become a noise token 'None'
    conv = convert_agent(
        _agent({"name": "s", "description": "d", "tools": ["Read", None]}), catalog=CATALOG
    )
    meta, _ = _split(conv.text)
    assert meta["tools"] == ["read_file"]
    assert not any("None" in e.detail for e in conv.report)
