"""read_file: cat -n output, offset/limit windowing, caps, raise-on-failure (L1)."""

import pytest

from harness.native_tools import ReadFileTool, ToolError
from harness.types import ToolName


def _tool(tmp_path):
    return ReadFileTool(workspace_root=tmp_path)


async def test_happy_round_trip_is_cat_n_numbered(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\nbeta\ngamma\n")
    out = await _tool(tmp_path)({"file_path": "a.txt"})
    assert out.splitlines()[0] == "     1\talpha"
    assert out.splitlines()[2] == "     3\tgamma"


async def test_offset_and_limit_window_with_absolute_numbers(tmp_path):
    (tmp_path / "a.txt").write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    out = await _tool(tmp_path)({"file_path": "a.txt", "offset": 3, "limit": 2})
    lines = out.splitlines()
    assert lines[0] == "     3\tline3"
    assert lines[1] == "     4\tline4"
    assert "showing lines 3" in out  # windowed footer present


async def test_missing_file_raises_with_path(tmp_path):
    with pytest.raises(ToolError) as exc:
        await _tool(tmp_path)({"file_path": "nope.txt"})
    assert "does not exist" in str(exc.value)
    assert "nope.txt" in str(exc.value)


async def test_directory_target_raises_distinctly(tmp_path):
    (tmp_path / "d").mkdir()
    with pytest.raises(ToolError) as exc:
        await _tool(tmp_path)({"file_path": "d"})
    assert "is a directory" in str(exc.value)
    assert "glob" in str(exc.value)


async def test_empty_file_returns_sentinel_not_error(tmp_path):
    (tmp_path / "e.txt").write_text("")
    out = await _tool(tmp_path)({"file_path": "e.txt"})
    assert out == "File exists but is empty."


@pytest.mark.parametrize(("contents", "window", "expected"), [
    ("", {}, ""),
    ("\tfirst\r\n\n  last", {}, "\tfirst\r\n\n  last"),
    ("first\nsecond\nlast", {"offset": 2, "limit": 1}, "second\n"),
    ("x" * 2400 + "\n", {}, "x" * 2400 + "\n"),
])
async def test_display_preserves_source_text(tmp_path, contents, window, expected):
    (tmp_path / "doc").write_bytes(contents.encode())
    assert await _tool(tmp_path)({"file_path": "doc", "display": True, **window}) == expected


async def test_display_never_silently_truncates_a_whole_file(tmp_path):
    path = tmp_path / "doc"
    path.write_text("x\n" * 2001)
    with pytest.raises(ToolError, match="range"):
        await _tool(tmp_path)({"file_path": "doc", "display": True})
    path.write_text("x" * 50_001)
    with pytest.raises(ToolError, match="over"):
        await _tool(tmp_path)({"file_path": "doc", "display": True})


async def test_display_rejects_a_named_pipe_without_opening_it(tmp_path):
    import os

    if not hasattr(os, "mkfifo"):
        pytest.skip("named pipes require POSIX")
    os.mkfifo(tmp_path / "stream")
    with pytest.raises(ToolError, match="regular text file"):
        await _tool(tmp_path)({"file_path": "stream", "display": True})


async def test_sparse_read_hashes_the_entire_file_with_bounded_reads(tmp_path, monkeypatch):
    import hashlib
    import io
    from pathlib import Path
    from harness.native_tools import ReadState

    path = tmp_path / "sparse.txt"
    with path.open("wb") as source:
        source.write(b"first\n")
        source.seek(8 * 1024 * 1024)
        source.write("\n  café\r\nunshown".encode())
    with path.open("rb") as source:
        version = hashlib.file_digest(source, "sha256").hexdigest()

    class BoundedReader(io.BufferedReader):
        def read(self, size=-1):
            assert 0 < size <= 256 * 1024, "unbounded file read"
            return super().read(size)

        def readline(self, size=-1):
            assert 0 < size <= 256 * 1024, "unbounded line read"
            return super().readline(size)

    opened = []
    original_open = Path.open

    def checked_open(target, *args, **kwargs):
        if target == path:
            opened.append(target)
            return BoundedReader(io.FileIO(target, "rb"))
        return original_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    state = ReadState()
    reader = ReadFileTool(workspace_root=tmp_path, read_state=state)
    assert await reader({"file_path": path.name, "offset": 3, "limit": 1, "display": True}) == "  café\r\n"
    assert opened == [path] and state.version(str(path)) == version


async def test_offset_past_eof_raises(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    with pytest.raises(ToolError) as exc:
        await _tool(tmp_path)({"file_path": "a.txt", "offset": 50})
    assert "beyond the end" in str(exc.value)


async def test_escape_raises(tmp_path):
    sub = tmp_path / "proj"
    sub.mkdir()
    with pytest.raises(ToolError) as exc:
        await ReadFileTool(workspace_root=sub)({"file_path": "../outside.txt"})
    assert "outside the workspace root" in str(exc.value)


async def test_non_utf8_decodes_with_replacement(tmp_path):
    (tmp_path / "b.bin").write_bytes(b"ok\xff\xfetext\n")
    out = await _tool(tmp_path)({"file_path": "b.bin"})
    assert "ok" in out and "text" in out  # replacement chars, no crash
    with pytest.raises(ToolError, match="invalid UTF-8"):
        await _tool(tmp_path)({"file_path": "b.bin", "display": True})


async def test_oversize_file_refuses_with_guidance(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x\n" * 200_000)  # > 256 KiB, no offset/limit
    with pytest.raises(ToolError) as exc:
        await _tool(tmp_path)({"file_path": "big.txt"})
    msg = str(exc.value)
    assert "offset" in msg and "limit" in msg


def test_spec_name_and_required_param(tmp_path):
    spec = _tool(tmp_path).spec
    assert spec.name == ToolName("read_file")
    assert spec.parameters["required"] == ["file_path"]


async def test_permission_error_becomes_tool_error(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret\n")
    secret.chmod(0o000)
    try:
        with pytest.raises(ToolError) as exc:
            await _tool(tmp_path)({"file_path": "secret.txt"})
        assert "permission" in str(exc.value).lower()
    finally:
        secret.chmod(0o644)


async def test_read_rejects_bad_offset_and_limit(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    tool = _tool(tmp_path)
    with pytest.raises(ToolError) as exc:
        await tool({"file_path": "x.txt", "offset": "abc"})
    assert "positive integers" in str(exc.value)
    with pytest.raises(ToolError) as exc:
        await tool({"file_path": "x.txt", "offset": -1})
    assert ">= 1" in str(exc.value)
    with pytest.raises(ToolError) as exc:
        await tool({"file_path": "x.txt", "limit": 0})
    assert ">= 1" in str(exc.value)
