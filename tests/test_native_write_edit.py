"""write_file + edit_file: create/overwrite gate, edit uniqueness, per-path lock."""

import asyncio

import pytest

from harness.native_tools import EditFileTool, ReadFileTool, ReadState, ToolError, WriteFileTool


def _rs():
    return ReadState()


async def test_create_new_file_round_trips(tmp_path):
    rs = _rs()
    out = await WriteFileTool(workspace_root=tmp_path, read_state=rs)(
        {"file_path": "new.txt", "content": "hello\nworld\n"}
    )
    assert (tmp_path / "new.txt").read_text() == "hello\nworld\n"
    assert "Created" in out


async def test_overwrite_requires_prior_read(tmp_path):
    (tmp_path / "x.txt").write_text("old")
    rs = _rs()
    with pytest.raises(ToolError) as exc:
        await WriteFileTool(workspace_root=tmp_path, read_state=rs)(
            {"file_path": "x.txt", "content": "new"}
        )
    assert "has not been read" in str(exc.value)
    # after a read, overwrite is allowed
    await ReadFileTool(workspace_root=tmp_path, read_state=rs)({"file_path": "x.txt"})
    out = await WriteFileTool(workspace_root=tmp_path, read_state=rs)(
        {"file_path": "x.txt", "content": "new"}
    )
    assert (tmp_path / "x.txt").read_text() == "new"
    assert "Overwrote" in out


async def test_write_creates_parent_dirs(tmp_path):
    rs = _rs()
    await WriteFileTool(workspace_root=tmp_path, read_state=rs)(
        {"file_path": "a/b/c.txt", "content": "x"}
    )
    assert (tmp_path / "a" / "b" / "c.txt").read_text() == "x"


async def test_write_then_edit_without_reread(tmp_path):
    rs = _rs()
    await WriteFileTool(workspace_root=tmp_path, read_state=rs)(
        {"file_path": "f.txt", "content": "a b c\n"}
    )
    out = await EditFileTool(workspace_root=tmp_path, read_state=rs)(
        {"file_path": "f.txt", "old_string": "b", "new_string": "B"}
    )
    assert (tmp_path / "f.txt").read_text() == "a B c\n"
    assert "Edited" in out


async def test_edit_requires_prior_read(tmp_path):
    (tmp_path / "g.txt").write_text("abc")
    with pytest.raises(ToolError) as exc:
        await EditFileTool(workspace_root=tmp_path, read_state=_rs())(
            {"file_path": "g.txt", "old_string": "a", "new_string": "z"}
        )
    assert "has not been read" in str(exc.value)


async def test_edit_old_string_not_found(tmp_path):
    rs = _rs()
    (tmp_path / "h.txt").write_text("hello\n")
    await ReadFileTool(workspace_root=tmp_path, read_state=rs)({"file_path": "h.txt"})
    with pytest.raises(ToolError) as exc:
        await EditFileTool(workspace_root=tmp_path, read_state=rs)(
            {"file_path": "h.txt", "old_string": "goodbye", "new_string": "x"}
        )
    msg = str(exc.value)
    assert "not found" in msg and "line-number" in msg  # strip-the-prefix teach


async def test_edit_old_string_not_unique_reports_count(tmp_path):
    rs = _rs()
    (tmp_path / "i.txt").write_text("x\nx\nx\n")
    await ReadFileTool(workspace_root=tmp_path, read_state=rs)({"file_path": "i.txt"})
    with pytest.raises(ToolError) as exc:
        await EditFileTool(workspace_root=tmp_path, read_state=rs)(
            {"file_path": "i.txt", "old_string": "x", "new_string": "y"}
        )
    assert "3 locations" in str(exc.value) and "replace_all" in str(exc.value)


async def test_edit_replace_all(tmp_path):
    rs = _rs()
    (tmp_path / "j.txt").write_text("x x x")
    await ReadFileTool(workspace_root=tmp_path, read_state=rs)({"file_path": "j.txt"})
    await EditFileTool(workspace_root=tmp_path, read_state=rs)(
        {"file_path": "j.txt", "old_string": "x", "new_string": "y", "replace_all": True}
    )
    assert (tmp_path / "j.txt").read_text() == "y y y"


async def test_edit_identical_strings_rejected(tmp_path):
    rs = _rs()
    (tmp_path / "k.txt").write_text("a")
    await ReadFileTool(workspace_root=tmp_path, read_state=rs)({"file_path": "k.txt"})
    with pytest.raises(ToolError) as exc:
        await EditFileTool(workspace_root=tmp_path, read_state=rs)(
            {"file_path": "k.txt", "old_string": "a", "new_string": "a"}
        )
    assert "identical" in str(exc.value)


async def test_concurrent_edits_serialize_per_path(tmp_path):
    rs = _rs()
    (tmp_path / "c.txt").write_text("0")
    await ReadFileTool(workspace_root=tmp_path, read_state=rs)({"file_path": "c.txt"})
    tool = EditFileTool(workspace_root=tmp_path, read_state=rs)
    # two edits that each depend on the previous value; serialized -> deterministic
    await tool({"file_path": "c.txt", "old_string": "0", "new_string": "01"})
    await asyncio.gather(
        tool({"file_path": "c.txt", "old_string": "01", "new_string": "012"}),
    )
    assert (tmp_path / "c.txt").read_text() == "012"


async def test_edit_returns_snippet(tmp_path):
    rs = _rs()
    (tmp_path / "s.txt").write_text("l1\nTARGET\nl3\n")
    await ReadFileTool(workspace_root=tmp_path, read_state=rs)({"file_path": "s.txt"})
    out = await EditFileTool(workspace_root=tmp_path, read_state=rs)(
        {"file_path": "s.txt", "old_string": "TARGET", "new_string": "DONE"}
    )
    assert "DONE" in out and "\t" in out  # cat -n snippet of the edited region
