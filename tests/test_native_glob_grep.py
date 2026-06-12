"""glob + grep over a shared ignore walk; absolute results, deterministic ordering."""

import pytest

from harness.native_tools import GlobTool, GrepTool, ToolError


def _tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("import os\ndef foo():\n    return 1\n")
    (tmp_path / "src" / "b.py").write_text("def bar():\n    return foo()\n")
    (tmp_path / "README.md").write_text("# title\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("foo\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_text("foo\n")
    return tmp_path


async def test_glob_recursive_returns_absolute_paths(tmp_path):
    root = _tree(tmp_path)
    out = await GlobTool(workspace_root=root)({"pattern": "**/*.py"})
    assert str(root / "src" / "a.py") in out
    assert str(root / "src" / "b.py") in out


async def test_glob_excludes_git_and_node_modules(tmp_path):
    root = _tree(tmp_path)
    out = await GlobTool(workspace_root=root)({"pattern": "**/*.py"})
    assert "node_modules" not in out
    assert ".git" not in out


async def test_glob_no_match_is_not_an_error(tmp_path):
    root = _tree(tmp_path)
    out = await GlobTool(workspace_root=root)({"pattern": "**/*.zig"})
    assert "No files matched" in out


async def test_glob_bad_search_path_raises(tmp_path):
    with pytest.raises(ToolError) as exc:
        await GlobTool(workspace_root=tmp_path)({"pattern": "*.py", "path": "nope"})
    assert "does not exist" in str(exc.value)


async def test_grep_files_with_matches_default(tmp_path):
    root = _tree(tmp_path)
    out = await GrepTool(workspace_root=root)({"pattern": "def foo"})
    assert str(root / "src" / "a.py") in out
    assert str(root / "src" / "b.py") not in out


async def test_grep_content_mode_has_line_numbers(tmp_path):
    root = _tree(tmp_path)
    out = await GrepTool(workspace_root=root)({"pattern": "return", "output_mode": "content"})
    assert ":3:" in out or ":2:" in out  # path:lineno:line shape


async def test_grep_glob_filter(tmp_path):
    root = _tree(tmp_path)
    out = await GrepTool(workspace_root=root)({"pattern": "title", "glob": "*.md"})
    assert "README.md" in out


async def test_grep_no_match_not_error_mentions_ignore(tmp_path):
    root = _tree(tmp_path)
    out = await GrepTool(workspace_root=root)({"pattern": "zzzznotfound"})
    assert "No matches found" in out


async def test_grep_invalid_regex_raises(tmp_path):
    with pytest.raises(ToolError) as exc:
        await GrepTool(workspace_root=tmp_path)({"pattern": "(unclosed"})
    assert "invalid regex" in str(exc.value)


async def test_grep_excludes_git(tmp_path):
    root = _tree(tmp_path)
    out = await GrepTool(workspace_root=root)({"pattern": "foo"})
    assert ".git" not in out
    assert "node_modules" not in out


async def test_glob_and_grep_share_ignore_set(tmp_path):
    # the exact property R-C6 demands: a file under an ignored dir is invisible to BOTH
    root = _tree(tmp_path)
    g = await GlobTool(workspace_root=root)({"pattern": "**/x.py"})
    r = await GrepTool(workspace_root=root)({"pattern": "foo", "glob": "x.py"})
    assert "node_modules" not in g and "No files matched" in g
    assert "node_modules" not in r and "No matches found" in r


async def test_walk_skips_symlinked_dir_outside(tmp_path):
    # a symlinked DIRECTORY inside the workspace pointing outside must not be traversed
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("secret_token\n")
    (root / "link").symlink_to(outside, target_is_directory=True)
    g = await GlobTool(workspace_root=root)({"pattern": "**/*.py"})
    assert str(outside / "secret.py") not in g
    assert "No files matched" in g
    r = await GrepTool(workspace_root=root)({"pattern": "secret_token"})
    # grep never reached the symlinked dir: the out-of-root file is not a hit
    assert str(outside / "secret.py") not in r
    assert "No matches found" in r


async def test_walk_skips_symlinked_file_outside(tmp_path):
    # a symlinked FILE inside the workspace pointing outside must not be read or listed
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("secret_token\n")
    (root / "secret.txt").symlink_to(target)
    g = await GlobTool(workspace_root=root)({"pattern": "**/*.txt"})
    assert "secret.txt" not in g
    assert "No files matched" in g
    r = await GrepTool(workspace_root=root)({"pattern": "secret_token"})
    # grep never read the symlinked file: its path is not listed as a hit
    assert str(root / "secret.txt") not in r
    assert "No matches found" in r


async def test_grep_files_cap_notice(tmp_path):
    # more than the file cap of matching files -> files_with_matches output carries a notice
    from harness.native_tools import _GREP_FILE_CAP

    root = tmp_path
    for i in range(_GREP_FILE_CAP + 1):
        (root / f"m{i}.txt").write_text("needle\n")
    out = await GrepTool(workspace_root=root)({"pattern": "needle"})
    assert f"Showing first {_GREP_FILE_CAP} files with matches" in out
