"""Phase 9 milestone: import a real-specimen-shaped CC plugin end to end.

The emitted plugin must LOAD and its primitives behave (behavioral corpus check, L14),
the report must carry every entry class, secrets must never leak, and two runs must be
byte-identical (idempotency).
"""

import json

from harness.catalog import Catalog
from harness.cc_import import convert_plugin
from harness.frontmatter import load_agent, load_skill
from harness.plugins import load_plugins

CATALOG = Catalog(entries={"sonnet": {"route": "anthropic/claude-sonnet"}})


def build_specimen(root):
    """A kitchen-sink CC plugin root shaped like the real superpowers/agent-swarm specimens."""
    meta = root / ".claude-plugin"
    meta.mkdir(parents=True)
    (meta / "plugin.json").write_text(
        json.dumps(
            {
                "name": "superpowers",
                "version": "5.1.0",
                "description": "A powers plugin",
                "author": {"name": "obra", "email": "a@b.c"},
                "homepage": "https://example",
                "keywords": ["workflow"],
            }
        )
    )
    (meta / "manifest.json").write_text("{}\n")  # stray, must be skipped
    # dir-per-skill with a sibling reference file and a backticked tool + a no-parity tool
    sk = root / "skills" / "remembering"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: remembering\ndescription: When to write memories\nwhen: always\n---\n\n"
        "Use `Read` then `WebFetch`. See [the guide](guide.md).\n"
    )
    (sk / "guide.md").write_text("# Guide\n")
    # flat command with argument-hint and a single positional
    cmd = root / "commands"
    cmd.mkdir()
    (cmd / "recall.md").write_text(
        "---\ndescription: Recall a memory\nargument-hint: <query>\n---\n\nRecall $1.\n"
    )
    # flat agent with paren-pattern tools + custom keys + model alias
    ag = root / "agents"
    ag.mkdir()
    (ag / "librarian.md").write_text(
        "---\nname: librarian\ndescription: curates\ntools: Read, Bash(mcp*), mcp__memory__write\n"
        "model: sonnet\nmax_output_chars: 4000\n---\n\nYou curate. Prefer `Grep`.\n"
    )
    # .mcp.json with a CLAUDE_PLUGIN_ROOT command, a reference env, and a LITERAL secret server
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "memory": {
                        "command": "${CLAUDE_PLUGIN_ROOT}/bin/memory",
                        "env": {"ROOT": "${MEMORY_ROOT}"},
                    },
                    "leaky": {"command": "x", "env": {"TOKEN": "sk-live-PLANTEDSECRET999"}},
                }
            }
        )
    )
    # hooks.json (never converted, flagged)
    hk = root / "hooks"
    hk.mkdir()
    (hk / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "${CLAUDE_PLUGIN_ROOT}/hooks/run.cmd start",
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )
    # kitchen-sink noise: foreign harness, build, binary, housekeeping
    (root / ".opencode").mkdir()
    (root / ".opencode" / "c.json").write_text("{}\n")
    (root / "tests").mkdir()
    (root / "tests" / "t.py").write_text("x\n")
    (root / "pyproject.toml").write_text("[tool]\n")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x00binary")
    (root / ".in_use").write_text("")
    return root


def test_specimen_imports_and_loads(tmp_path):
    src = build_specimen(tmp_path / "cc")
    out = tmp_path / "out" / "superpowers"
    convert_plugin(src, out=out, catalog=CATALOG)

    loaded = load_plugins([out.parent])  # THE GATE
    (plugin,) = loaded.plugins
    assert plugin.name == "superpowers"
    assert plugin.version == "5.1.0"

    # behavioral: the skill loads as a native SkillDef with the tool name rewritten in its body
    skill = load_skill(out / "skills" / "remembering.md")
    assert skill.name == "remembering"
    assert "`read_file`" in skill.body
    # asset copied + link rewritten so the body still resolves
    assert (out / "skills" / "remembering.assets" / "guide.md").is_file()
    assert "remembering.assets/guide.md" in skill.body

    # behavioral: the agent loads with tools mapped and the paren-pattern degraded to bare bash
    agent = load_agent(out / "agents" / "librarian.md")
    assert "read_file" in agent.tools and "bash" in agent.tools
    assert "mcp__memory__write" in agent.tools
    assert agent.model == "sonnet"

    # behavioral: only the clean MCP server emitted; the literal-secret server refused
    names = {s.name for s in loaded.mcp_servers}
    assert "memory" in names and "leaky" not in names

    # the command kept argument-hint and rewrote the single positional
    cmd_text = (out / "commands" / "recall.md").read_text()
    assert "<!-- argument-hint: <query> -->" in cmd_text
    assert "$ARGUMENTS" in cmd_text and "$1" not in cmd_text


def test_report_carries_every_entry_class_and_no_secret(tmp_path):
    src = build_specimen(tmp_path / "cc")
    out = tmp_path / "out" / "superpowers"
    convert_plugin(src, out=out, catalog=CATALOG)
    report = (out / "IMPORT-REPORT.md").read_text()
    # every confidence/class present
    assert "[rewrite]" in report  # `Read` -> `read_file`
    assert "[degraded]" in report  # WebFetch + Bash(mcp*)
    assert "[drop]" in report  # `when` key, max_output_chars
    assert "## Hooks" in report and "run.cmd" in report  # hook flagged
    assert "## MCP" in report and "leaky" in report  # refusal reported
    assert "## Skipped" in report
    assert "foreign-harness" in report and "binary" in report
    # secrets NEVER leak (report or any emitted file)
    assert "PLANTEDSECRET999" not in report
    for path in out.rglob("*"):
        if path.is_file():
            try:
                assert "PLANTEDSECRET999" not in path.read_text()
            except UnicodeDecodeError:
                pass  # binary asset, no secret to leak


def test_two_runs_are_byte_identical(tmp_path):
    src = build_specimen(tmp_path / "cc")
    a = tmp_path / "a" / "superpowers"
    b = tmp_path / "b" / "superpowers"
    convert_plugin(src, out=a, catalog=CATALOG)
    convert_plugin(src, out=b, catalog=CATALOG)
    a_files = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    b_files = sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    assert a_files == b_files
    for rel in a_files:
        assert (a / rel).read_bytes() == (b / rel).read_bytes()


def test_golden_memory_plugin_shape_is_importable_reference(tmp_path):
    """The hand-ported golden memory plugin is the behavioral reference: a CC plugin shaped
    like it imports to a plugin whose skills/commands match the golden names. We assert the
    importer produces the same primitive NAMES the golden plugin ships (behavioral corpus,
    not just report text)."""
    from pathlib import Path as _P

    golden = _P(__file__).parent.parent / "plugins" / "memory"
    golden_skills = {p.stem for p in (golden / "skills").glob("*.md")}
    # build a CC-shaped source carrying the same skill names (dir-per-skill form)
    cc = build_specimen(tmp_path / "cc")
    for name in golden_skills:
        d = cc / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nUse `Read`.\n")
    out = tmp_path / "out" / "superpowers"
    convert_plugin(cc, out=out, catalog=CATALOG)
    loaded = load_plugins([out.parent])
    imported_skills = {s.name for s in loaded.skills}
    assert golden_skills <= imported_skills  # every golden skill name round-trips
