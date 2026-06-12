"""CC plugin reader: structure-only walk into a typed CcPlugin (no conversion yet)."""

import json
import os

import pytest

from harness.cc_import import CcImportError, read_cc_plugin


def write_cc(root, *, plugin_json=None, files=None):
    """Lay down a CC-format plugin tree. files maps relpath -> text."""
    meta_dir = root / ".claude-plugin"
    meta_dir.mkdir(parents=True)
    if plugin_json is None:
        plugin_json = {"name": "demo", "version": "1.0.0", "description": "A demo"}
    (meta_dir / "plugin.json").write_text(json.dumps(plugin_json))
    for rel, content in (files or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    return root


SKILL_MD = "---\nname: remembering\ndescription: When to write memories\n---\n\n# Remembering\n\nUse `Read` then write.\n"
CMD_MD = "---\ndescription: Greet\nargument-hint: <name>\n---\n\nHello $ARGUMENTS\n"
AGENT_MD = "---\nname: scout\ndescription: explores\ntools: Read, Bash(mcp*)\nmodel: sonnet\nmax_output_chars: 2000\n---\n\nYou explore.\n"


def test_reads_metadata(tmp_path):
    root = write_cc(
        tmp_path / "p",
        plugin_json={
            "name": "superpowers",
            "version": "5.1.0",
            "description": "d",
            "author": {"name": "a", "email": "e"},
            "homepage": "https://x",
            "keywords": ["k1"],
        },
    )
    cc = read_cc_plugin(root)
    assert cc.name == "superpowers"
    assert cc.version == "5.1.0"
    assert cc.description == "d"
    assert cc.author == {"name": "a", "email": "e"}
    assert cc.homepage == "https://x"
    assert cc.keywords == ["k1"]


def test_reads_dir_per_skill_with_body_and_assets(tmp_path):
    root = write_cc(
        tmp_path / "p",
        files={
            "skills/remembering/SKILL.md": SKILL_MD,
            "skills/remembering/reference.md": "# ref\n",
        },
    )
    cc = read_cc_plugin(root)
    (skill,) = cc.skills
    assert skill.name == "remembering"
    assert skill.meta["description"] == "When to write memories"
    assert "Use `Read`" in skill.body
    # sibling files in the skill dir are recorded as assets (relpath within the dir)
    assert "reference.md" in skill.assets


def test_reads_flat_commands_and_agents(tmp_path):
    root = write_cc(
        tmp_path / "p",
        files={
            "commands/greet.md": CMD_MD,
            "agents/scout.md": AGENT_MD,
        },
    )
    cc = read_cc_plugin(root)
    (cmd,) = cc.commands
    assert cmd.name == "greet"
    assert cmd.meta["argument-hint"] == "<name>"
    (agent,) = cc.agents
    assert agent.name == "scout"
    # raw frontmatter is preserved verbatim for the converter (tools as a string here)
    assert agent.meta["tools"] == "Read, Bash(mcp*)"
    assert agent.meta["max_output_chars"] == 2000


def test_reads_mcp_and_hooks_raw_text(tmp_path):
    root = write_cc(
        tmp_path / "p",
        files={
            ".mcp.json": '{"mcpServers": {"s": {"command": "x"}}}',
            "hooks/hooks.json": '{"hooks": {"SessionStart": []}}',
        },
    )
    cc = read_cc_plugin(root)
    assert cc.mcp_json_text is not None and "mcpServers" in cc.mcp_json_text
    assert cc.hooks_json_text is not None and "SessionStart" in cc.hooks_json_text


def test_kitchen_sink_files_recorded_as_skips_by_category(tmp_path):
    root = write_cc(
        tmp_path / "p",
        files={
            "tests/test_x.py": "x\n",
            "pyproject.toml": "[tool]\n",
            "logs/run.log": "noise\n",
            "bin/tool": b"\x7fELF\x00binary",
            "icon.png": b"\x89PNG\r\n",
            ".opencode/config.json": "{}\n",
            ".codex-plugin/x.md": "x\n",
            ".in_use": "",
            ".gitignore": "*.pyc\n",
            "RELEASE-NOTES.md": "notes\n",
            ".claude-plugin/manifest.json": "{}\n",
        },
    )
    cc = read_cc_plugin(root)
    cats = {s.category for s in cc.skips}
    assert "foreign-harness" in cats  # .opencode / .codex-plugin
    assert "housekeeping" in cats  # .in_use / .gitignore / RELEASE-NOTES / manifest.json
    assert "build" in cats  # tests/ / pyproject.toml / logs/ / bin/
    assert "binary" in cats  # icon.png / bin/tool
    # never reads binary content; just records the relpath
    assert any(s.relpath == "icon.png" for s in cc.skips)


def test_missing_plugin_json_is_teaching_error(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    with pytest.raises(CcImportError) as exc:
        read_cc_plugin(root)
    assert ".claude-plugin/plugin.json" in str(exc.value)


def test_path_not_a_dir_is_teaching_error(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(CcImportError) as exc:
        read_cc_plugin(f)
    assert "not a directory" in str(exc.value)


def test_malformed_plugin_json_is_teaching_error(tmp_path):
    root = tmp_path / "p"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text("{not json")
    with pytest.raises(CcImportError) as exc:
        read_cc_plugin(root)
    assert "plugin.json" in str(exc.value) and "JSON" in str(exc.value)


def test_skill_without_skill_md_is_skipped_not_fatal(tmp_path):
    root = write_cc(tmp_path / "p", files={"skills/empty/notes.txt": "x\n"})
    cc = read_cc_plugin(root)
    assert cc.skills == ()
    assert any(s.category == "malformed" and "empty" in s.relpath for s in cc.skips)


def test_binary_skill_body_does_not_crash_the_walk(tmp_path):
    # a SKILL.md that is not valid UTF-8 is recorded malformed, not raised
    root = write_cc(tmp_path / "p", files={"skills/bad/SKILL.md": b"\xff\xfe binary"})
    cc = read_cc_plugin(root)
    assert cc.skills == ()
    assert any(s.category == "malformed" for s in cc.skips)


def test_symlinked_asset_outside_root_not_collected(tmp_path):
    # A skill-dir asset that symlinks outside the root must not be collected (it would
    # exfiltrate external content when read downstream).
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET\n")
    root = write_cc(tmp_path / "p", files={"skills/remembering/SKILL.md": SKILL_MD})
    (root / "skills" / "remembering" / "data.md").symlink_to(secret)
    cc = read_cc_plugin(root)
    (skill,) = cc.skills
    assert "data.md" not in skill.assets
    # the legitimate SKILL.md still parsed; no external path leaked into assets
    assert all("secret" not in str(p) for p in skill.assets.values())


def test_symlinked_dir_outside_root_not_traversed(tmp_path):
    # A top-level symlinked dir pointing outside the root must not be traversed; the
    # symlink itself may appear as a single skip, but no external file is enumerated.
    external = tmp_path / "external"
    external.mkdir()
    (external / "leak.txt").write_text("LEAK\n")
    root = write_cc(tmp_path / "p")
    (root / "linked").symlink_to(external, target_is_directory=True)
    cc = read_cc_plugin(root)
    # the symlink was never traversed: no enumerated child of the external tree appears
    assert not any("leak.txt" in s.relpath for s in cc.skips)
    assert not any(s.relpath.startswith("linked/") for s in cc.skips)
    # the symlink itself is recorded as exactly one skip under its own name
    linked_skips = [s for s in cc.skips if s.relpath == "linked"]
    assert len(linked_skips) == 1


def test_oversize_def_skipped_without_reading(tmp_path):
    # A def larger than the cap is recorded as oversize and never read into memory.
    root = write_cc(tmp_path / "p", files={"skills/big/SKILL.md": "placeholder\n"})
    big = root / "skills" / "big" / "SKILL.md"
    os.truncate(big, 1 * 1024 * 1024 + 1)
    cc = read_cc_plugin(root)
    assert cc.skills == ()
    assert any(s.category == "oversize" and "big" in s.relpath for s in cc.skips)


def test_bom_skill_parses(tmp_path):
    # A SKILL.md with a UTF-8 BOM parses (utf-8-sig) instead of skipping as malformed.
    root = write_cc(tmp_path / "p", files={"skills/bom/SKILL.md": ("\ufeff" + SKILL_MD)})
    cc = read_cc_plugin(root)
    (skill,) = cc.skills
    assert skill.name == "bom"
    assert skill.meta["description"] == "When to write memories"


def test_symlinked_skill_dir_recorded_not_read(tmp_path):
    # A skills/<name> that is a symlink to an external dir must never be read.
    external = tmp_path / "external_skill"
    external.mkdir()
    (external / "SKILL.md").write_text(
        "---\nname: leak\ndescription: EXTERNAL_SECRET\n---\nbody\n", encoding="utf-8"
    )
    root = write_cc(tmp_path / "p", files={})
    (root / "skills").mkdir(exist_ok=True)
    (root / "skills" / "linked").symlink_to(external)
    cc = read_cc_plugin(root)
    assert cc.skills == ()
    assert any(s.relpath == "skills/linked" and s.category == "unknown" for s in cc.skips)


def test_symlinked_command_and_agent_files_recorded_not_read(tmp_path):
    external = tmp_path / "ext.md"
    external.write_text("---\nname: x\ndescription: EXTERNAL_SECRET\n---\nbody\n", encoding="utf-8")
    root = write_cc(tmp_path / "p", files={})
    (root / "commands").mkdir(exist_ok=True)
    (root / "agents").mkdir(exist_ok=True)
    (root / "commands" / "linked.md").symlink_to(external)
    (root / "agents" / "linked.md").symlink_to(external)
    cc = read_cc_plugin(root)
    assert cc.commands == ()
    assert cc.agents == ()
    assert any(s.relpath == "commands/linked.md" for s in cc.skips)
    assert any(s.relpath == "agents/linked.md" for s in cc.skips)


def test_symlinked_mcp_and_hooks_json_recorded_not_read(tmp_path):
    external = tmp_path / "outside.json"
    external.write_text('{"EXTERNAL": "SECRET"}', encoding="utf-8")
    root = write_cc(tmp_path / "p", files={})
    (root / ".mcp.json").symlink_to(external)
    (root / "hooks").mkdir(exist_ok=True)
    (root / "hooks" / "hooks.json").symlink_to(external)
    cc = read_cc_plugin(root)
    assert cc.mcp_json_text is None
    assert cc.hooks_json_text is None
    assert any(s.relpath == ".mcp.json" for s in cc.skips)
    assert any(s.relpath == "hooks/hooks.json" for s in cc.skips)


def test_bom_mcp_json_parses(tmp_path):
    # .mcp.json tolerates a Windows BOM the same way defs do.
    root = write_cc(tmp_path / "p", files={".mcp.json": "﻿" + '{"mcpServers": {}}'})
    cc = read_cc_plugin(root)
    assert cc.mcp_json_text is not None
    assert cc.mcp_json_text.startswith("{")
