"""write_file + edit_file: create/overwrite gate, edit uniqueness, per-path lock."""

import asyncio
import errno
import hashlib
import os
import stat
import subprocess
import sys

import pytest

from harness.native_tools import EditFileTool, ReadFileTool, ReadState, ToolError, WriteFileTool


def _rs():
    return ReadState()


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o750])
@pytest.mark.parametrize("kind", ["write", "edit"])
async def test_replacing_workspace_file_preserves_permissions(tmp_path, mode, kind):
    path = tmp_path / "target"
    path.write_text("old")
    path.chmod(mode)
    rs = _rs()
    await ReadFileTool(workspace_root=tmp_path, read_state=rs)({"file_path": path.name})
    if kind == "write":
        await WriteFileTool(workspace_root=tmp_path, read_state=rs)({"file_path": path.name, "content": "new"})
    else:
        await EditFileTool(workspace_root=tmp_path, read_state=rs)(
            {"file_path": path.name, "old_string": "old", "new_string": "new"})
    assert path.read_text() == "new"
    assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.parametrize("mask,expected", [(0o022, 0o644), (0o077, 0o600), (0o002, 0o664)])
def test_workspace_creation_respects_umask_without_widening_private_state(tmp_path, mask, expected):
    # umask is process-wide: do not modify it in pytest's threaded event loop.
    script = '''import os, sys
from pathlib import Path
from harness.native_tools import WriteFileTool, ReadState
from harness.persistence import atomic_write
root=Path(sys.argv[1])
os.umask(int(sys.argv[2]))
WriteFileTool(workspace_root=root, read_state=ReadState())._write(root/'workspace', 'new', None)
atomic_write(root/'private', b'state')
'''
    subprocess.run([sys.executable, "-c", script, str(tmp_path), str(mask)], check=True)
    assert stat.S_IMODE((tmp_path / "workspace").stat().st_mode) == expected
    assert stat.S_IMODE((tmp_path / "private").stat().st_mode) == 0o600


@pytest.mark.parametrize("kind", ["create", "write", "edit"])
@pytest.mark.parametrize("error", [errno.EINVAL, errno.ENOTSUP, errno.EIO])
async def test_directory_sync_failure_reports_published_bytes_and_updates_read_state(tmp_path, monkeypatch, kind, error):
    from harness import persistence

    def fail(_):
        raise OSError(error, "directory sync unavailable")

    path = tmp_path / "target"
    rs = _rs()
    if kind != "create":
        path.write_text("old")
        path.chmod(0o750)
        await ReadFileTool(workspace_root=tmp_path, read_state=rs)({"file_path": path.name})
    monkeypatch.setattr(persistence, "sync_directory", fail)
    if kind == "edit":
        result = await EditFileTool(workspace_root=tmp_path, read_state=rs)(
            {"file_path": path.name, "old_string": "old", "new_string": "new"})
    else:
        result = await WriteFileTool(workspace_root=tmp_path, read_state=rs)(
            {"file_path": path.name, "content": "new"})
    assert path.read_text() == "new"
    assert "bytes were published" in result and "Durability" in result
    assert rs.version(str(path)) == hashlib.sha256(b"new").hexdigest()
    # The next legitimate edit uses the new observation without a forced read.
    await EditFileTool(workspace_root=tmp_path, read_state=rs)(
        {"file_path": path.name, "old_string": "new", "new_string": "final"})
    assert path.read_text() == "final"
    if kind != "create":
        assert stat.S_IMODE(path.stat().st_mode) == 0o750
    # Private state still treats loss of durability as an error.
    with pytest.raises(persistence.PublishedWriteError) as raised:
        persistence.atomic_write(tmp_path / "private", b"state")
    assert raised.value.errno == error


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


async def test_concurrent_edits_require_a_current_observation(tmp_path):
    rs = _rs()
    (tmp_path / "c.txt").write_text("v0")
    await ReadFileTool(workspace_root=tmp_path, read_state=rs)({"file_path": "c.txt"})
    tool = EditFileTool(workspace_root=tmp_path, read_state=rs)
    # Both calls are based on v0; the second cannot adopt the first call's write
    # while waiting for the lock. A deliberate sequential edit can use that result.
    results = await asyncio.gather(
        tool({"file_path": "c.txt", "old_string": "v0", "new_string": "v1"}),
        tool({"file_path": "c.txt", "old_string": "v1", "new_string": "v2"}),
        return_exceptions=True,
    )
    assert "Edited" in results[0]
    assert isinstance(results[1], ToolError) and "changed" in str(results[1])
    assert (tmp_path / "c.txt").read_text() == "v1"
    await tool({"file_path": "c.txt", "old_string": "v1", "new_string": "v2"})
    assert (tmp_path / "c.txt").read_text() == "v2"


@pytest.mark.parametrize("kind", ["write", "edit"])
async def test_failed_replace_leaves_no_tmp(tmp_path, monkeypatch, kind):
    rs = _rs()
    real_replace = os.replace
    path = tmp_path / "doomed.txt"
    if kind == "edit":
        path.write_text("original")
        await ReadFileTool(workspace_root=tmp_path, read_state=rs)({"file_path": path.name})

    def boom(source, target):
        # Fail only the atomic rename onto the real target, not unrelated replaces.
        if target == path:
            raise OSError("simulated rename failure")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(ToolError):
        if kind == "write":
            await WriteFileTool(workspace_root=tmp_path, read_state=rs)(
                {"file_path": path.name, "content": "data"}
            )
        else:
            await EditFileTool(workspace_root=tmp_path, read_state=rs)(
                {"file_path": path.name, "old_string": "original", "new_string": "data"}
            )
    assert sorted(tmp_path.iterdir()) == ([path] if kind == "edit" else [])
    if kind == "edit":
        assert path.read_text() == "original"


async def test_edit_returns_snippet(tmp_path):
    rs = _rs()
    (tmp_path / "s.txt").write_text("l1\nTARGET\nl3\n")
    await ReadFileTool(workspace_root=tmp_path, read_state=rs)({"file_path": "s.txt"})
    out = await EditFileTool(workspace_root=tmp_path, read_state=rs)(
        {"file_path": "s.txt", "old_string": "TARGET", "new_string": "DONE"}
    )
    assert "DONE" in out and "\t" in out  # cat -n snippet of the edited region
