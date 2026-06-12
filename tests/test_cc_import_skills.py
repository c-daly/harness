"""Skill/command conversion: dir->flat, asset rewrite, the high-precision prose rewriter."""

import time
from pathlib import Path

from harness.cc_import import (
    RawDef,
    convert_command,
    convert_skill,
    detect_relpath_collisions,
    rewrite_prose,
)
from harness.frontmatter import split_frontmatter


def _skill(body, *, name="s", meta=None, assets=None):
    return RawDef(
        name=name,
        meta=meta or {"name": name, "description": "d"},
        body=body,
        source_path=Path("/x/skills") / name / "SKILL.md",
        assets=assets or {},
    )


def test_skill_dir_becomes_flat_md_with_clean_frontmatter():
    conv = convert_skill(
        _skill(
            "# Body\n",
            name="remembering",
            meta={"name": "remembering", "description": "d", "when": "x"},
        )
    )
    assert conv.relpath == "skills/remembering.md"
    meta, body = split_frontmatter(conv.text)
    assert meta == {"name": "remembering", "description": "d"}  # unknown key `when` dropped
    assert body.strip() == "# Body"
    assert any("when" in e.detail and "dropped" in e.detail for e in conv.report)


def test_backticked_tool_names_rewritten_with_report_lines():
    body = "Use `Read` to load and `TodoWrite` to plan.\n"
    conv = convert_skill(_skill(body))
    _, out_body = split_frontmatter(conv.text)
    assert "`read_file`" in out_body
    assert "`todo`" in out_body
    rewrites = [e for e in conv.report if e.kind == "rewrite"]
    assert any("Read" in e.detail and "read_file" in e.detail for e in rewrites)
    assert any("TodoWrite" in e.detail and "todo" in e.detail for e in rewrites)
    # report lines carry a line number
    assert all(e.line is not None for e in rewrites)


def test_bare_word_mentions_are_counted_not_rewritten():
    body = "Read the docs and Task the agent.\n"  # English words, unbackticked
    conv = convert_skill(_skill(body))
    _, out_body = split_frontmatter(conv.text)
    assert out_body.strip() == "Read the docs and Task the agent."  # untouched
    assert any(e.kind == "mention" for e in conv.report)
    assert not any(e.kind == "rewrite" for e in conv.report)


def test_no_parity_backticked_reference_flags_degraded():
    conv = convert_skill(_skill("Fetch with `WebFetch` then summarize.\n"))
    assert conv.degraded is True
    assert any(e.kind == "degraded" and "WebFetch" in e.detail for e in conv.report)


def test_rewrite_is_idempotent_on_native_text():
    body = "Use `read_file` and `todo`.\n"
    once, _ = rewrite_prose(body, source="skills/s.md")
    twice, entries = rewrite_prose(once, source="skills/s.md")
    assert once == twice == body
    assert entries == []


def test_skill_assets_copied_and_relative_links_rewritten(tmp_path):
    ref = tmp_path / "reference.md"
    ref.write_text("# ref\n")
    body = "See [the ref](reference.md) and ![img](img/logo.png).\n"
    conv = convert_skill(_skill(body, name="big", assets={"reference.md": ref}))
    _, out_body = split_frontmatter(conv.text)
    # link rewritten to point at the assets dir
    assert "big.assets/reference.md" in out_body
    # the asset copy is planned (src -> dest relpath under the emitted plugin)
    assert any(dest == "skills/big.assets/reference.md" for _src, dest in conv.assets)
    assert any(e.kind == "asset" for e in conv.report)


def test_oversize_binary_asset_refused_and_link_flagged(tmp_path):
    big = tmp_path / "huge.bin"
    big.write_bytes(b"\x00" * (5 * 1024 * 1024 + 1))
    body = "![big](huge.bin)\n"
    conv = convert_skill(_skill(body, name="b", assets={"huge.bin": big}))
    assert not any(dest.endswith("huge.bin") for _src, dest in conv.assets)  # not copied
    assert any(e.kind == "refused" and "huge.bin" in e.detail for e in conv.report)


def test_command_description_maps_and_argument_hint_appended_as_comment():
    raw = RawDef(
        name="greet",
        meta={"description": "Greet", "argument-hint": "<name>"},
        body="Hello $ARGUMENTS\n",
        source_path=Path("/x/commands/greet.md"),
    )
    conv = convert_command(raw)
    meta, body = split_frontmatter(conv.text)
    assert meta == {"name": "greet", "description": "Greet"}
    assert "$ARGUMENTS" in body  # passes through
    assert "<!-- argument-hint: <name> -->" in body  # survives as a comment
    assert any("argument-hint" in e.detail for e in conv.report)


def test_command_single_positional_rewritten_to_arguments():
    raw = RawDef(
        name="g",
        meta={"description": "d"},
        body="Run with $1 only.\n",
        source_path=Path("/x/commands/g.md"),
    )
    conv = convert_command(raw)
    _, body = split_frontmatter(conv.text)
    assert "$ARGUMENTS" in body and "$1" not in body
    assert any(e.kind == "rewrite" and "$1" in e.detail for e in conv.report)


def test_command_multi_positional_flagged_degraded_body_verbatim():
    raw = RawDef(
        name="g",
        meta={"description": "d"},
        body="Use $1 and $2.\n",
        source_path=Path("/x/commands/g.md"),
    )
    conv = convert_command(raw)
    _, body = split_frontmatter(conv.text)
    assert "$1" in body and "$2" in body  # left verbatim
    assert conv.degraded is True
    assert any(e.kind == "degraded" and "positional" in e.detail for e in conv.report)


def test_converted_skill_loads_as_native_skilldef(tmp_path):
    from harness.frontmatter import load_skill

    conv = convert_skill(
        _skill(
            "Use `Read`.\n",
            name="remembering",
            meta={"name": "remembering", "description": "d", "model": "x"},
        )
    )
    out = tmp_path / "remembering.md"
    out.write_text(conv.text)
    sk = load_skill(out)  # must not raise (unknown `model` key was stripped)
    assert sk.name == "remembering"


def test_rewrite_prose_long_line_completes():
    # A single 100k-char line of bare `Read ` words. The naive O(n^2) scan never finished;
    # the linear fix runs in milliseconds. Bound is generous (< 2s) so it is not flaky.
    body = "Read " * 100_000 + "\n"
    start = time.monotonic()
    out, entries = rewrite_prose(body, source="skills/s.md")
    elapsed = time.monotonic() - start
    assert elapsed < 2.0
    assert out == body  # bare words are never rewritten
    assert all(e.kind == "mention" for e in entries)


def test_broken_relative_link_reported():
    # A relative link whose target is not in the asset plan is flagged as possibly broken.
    body = "See [the missing ref](does-not-exist.md) for details.\n"
    conv = convert_skill(_skill(body, name="s"))
    _, out_body = split_frontmatter(conv.text)
    assert "does-not-exist.md" in out_body  # text left unchanged
    assert any(
        e.kind == "refused" and "does-not-exist.md" in e.detail and "broken" in e.detail
        for e in conv.report
    )


def test_asset_rel_traversal_refused():
    # A hand-crafted RawDef with a traversal rel must be refused, never planned for copy.
    raw = RawDef(
        name="s",
        meta={"name": "s", "description": "d"},
        body="# body\n",
        source_path=Path("/x/skills/s/SKILL.md"),
        assets={"../../x": Path("/x/skills/s/x")},
    )
    conv = convert_skill(raw)
    assert conv.assets == ()  # nothing planned
    assert any(
        e.kind == "refused" and "escapes" in e.detail and "../../x" in e.detail for e in conv.report
    )


def test_relpath_collision_detected():
    # `a.b` and `a-b` both sanitize to `a-b`, so both emit skills/a-b.md. One drop entry.
    one = convert_skill(_skill("# x\n", name="a.b", meta={"name": "a.b", "description": "d"}))
    two = convert_skill(_skill("# y\n", name="a-b", meta={"name": "a-b", "description": "d"}))
    assert one.relpath == two.relpath == "skills/a-b.md"
    drops = detect_relpath_collisions([one, two])
    assert len(drops) == 1
    assert drops[0].kind == "drop"
    assert drops[0].artifact == "skills/a-b.md"
    assert "skills/a-b.md" in drops[0].detail
    # deterministic: no collision for a single def, and order-stable
    assert detect_relpath_collisions([one]) == []
    assert detect_relpath_collisions([two, one]) == drops
