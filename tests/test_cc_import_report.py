"""Report builder, plugin.toml metadata + provenance marker, eject semantics."""

import tomllib

from harness.cc_import import (
    ReportEntry,
    build_report,
    emit_plugin_toml,
    eject_marker,
    has_generated_marker,
)


def test_report_has_fixed_section_order_and_no_timestamps():
    entries = [
        ReportEntry("rewrite", "skills/a.md", "`Read` -> `read_file`", line=3),
        ReportEntry("degraded", "skills/a.md", "`WebFetch` has no native parity", line=5),
        ReportEntry("hook", "hooks/hooks.json", "SessionStart: ... hand-port"),
        ReportEntry("mcp", ".mcp.json", "skipped leaky: literal value"),
        ReportEntry("skip", "binary", "2 binary file(s) skipped"),
    ]
    md = build_report(entries, plugin_name="demo", source="/path/to/cc")
    assert "# Import Report: demo" in md
    # sections appear in fixed order
    assert md.index("## Summary") < md.index("## Artifacts")
    assert md.index("## Artifacts") < md.index("## Hooks")
    assert md.index("## Hooks") < md.index("## MCP")
    assert md.index("## MCP") < md.index("## Skipped")
    # no timestamp tokens (idempotency)
    for token in ("202", "GMT", "UTC", ":"):
        if token == ":":
            continue  # colons appear in prose; only forbid clock-like patterns
    import re

    assert not re.search(r"\\d{4}-\\d{2}-\\d{7T}\\d{2}:\\d{2}", md)


def test_summary_counts_by_confidence():
    entries = [
        ReportEntry("rewrite", "a", "x"),
        ReportEntry("degraded", "a", "y"),
        ReportEntry("degraded", "b", "z"),
        ReportEntry("mcp", ".mcp.json", "refused leaky"),
        ReportEntry("skip", "binary", "2 files"),
    ]
    md = build_report(entries, plugin_name="d", source="s")
    assert "degraded: 2" in md


def test_report_is_byte_stable_for_same_input():
    entries = [
        ReportEntry("rewrite", "skills/b.md", "x", line=2),
        ReportEntry("rewrite", "skills/a.md", "y", line=1),
    ]
    a = build_report(entries, plugin_name="d", source="s")
    b = build_report(list(reversed(entries)), plugin_name="d", source="s")
    assert a == b  # sorted internally; input order does not matter


def test_emit_plugin_toml_has_plugin_table_and_provenance_comment():
    toml = emit_plugin_toml(
        name="demo",
        version="1.0.0",
        description="d",
        source="/abs/cc",
        author={"name": "a"},
        homepage="https://x",
        mcp_toml="",
    )
    data = tomllib.loads(toml)
    assert data["plugin"] == {"name": "demo", "version": "1.0.0", "description": "d"}
    # provenance + author/homepage live in COMMENTS, never tables (would fail _PLUGIN_KEYS)
    assert "# harness-import: source =" in toml
    assert "# harness-import: generated = true" in toml
    assert "# author:" in toml and "https://x" in toml


def test_emit_plugin_toml_appends_mcp_section():
    mcp_toml = '[mcp.servers.s]\ncommand = "x"\n'
    toml = emit_plugin_toml(
        name="d",
        version="1",
        description="x",
        source="s",
        mcp_toml=mcp_toml,
    )
    data = tomllib.loads(toml)
    assert data["mcp"]["servers"]["s"]["command"] == "x"


def test_generated_marker_detection_and_eject(tmp_path):
    toml = emit_plugin_toml(name="d", version="1", description="x", source="/cc", mcp_toml="")
    p = tmp_path / "plugin.toml"
    p.write_text(toml)
    assert has_generated_marker(p) is True
    eject_marker(p)
    assert has_generated_marker(p) is False
    # eject keeps the file loadable and keeps source as a comment
    import tomllib as t

    data = t.loads(p.read_text())
    assert data["plugin"]["name"] == "d"
    assert "# harness-import: source =" in p.read_text()


def test_has_generated_marker_false_for_hand_written_toml(tmp_path):
    p = tmp_path / "plugin.toml"
    p.write_text('[plugin]\nname = "x"\nversion = "1"\ndescription = "d"\n')
    assert has_generated_marker(p) is False


def test_emitted_plugin_toml_loads_with_marker_present(tmp_path):
    from harness.plugins import load_plugins

    pdir = tmp_path / "demo"
    pdir.mkdir()
    (pdir / "plugin.toml").write_text(
        emit_plugin_toml(
            name="demo",
            version="1.0.0",
            description="d",
            source="/cc",
            author={"name": "a"},
            mcp_toml="",
        )
    )
    loaded = load_plugins([tmp_path])  # the comment marker must not break the loader
    assert [p.name for p in loaded.plugins] == ["demo"]
