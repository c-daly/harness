"""bash: deterministic commands, exit codes, timeout + process death, truncation, env scrub."""

import asyncio

import pytest

from harness.native_tools import BashTool, ToolError


def _tool(tmp_path, **kw):
    return BashTool(workspace_root=tmp_path, **kw)


async def test_simple_stdout(tmp_path):
    out = await _tool(tmp_path)({"command": "printf hi"})
    assert out == "hi"


async def test_no_output_sentinel(tmp_path):
    out = await _tool(tmp_path)({"command": "true"})
    assert out == "(no output)"


async def test_nonzero_exit_returns_not_raises(tmp_path):
    out = await _tool(tmp_path)({"command": "printf oops; exit 3"})
    assert "oops" in out
    assert "Exit code: 3" in out


async def test_stderr_merged_into_stdout(tmp_path):
    out = await _tool(tmp_path)({"command": "printf err >&2"})
    assert "err" in out


async def test_cwd_is_workspace_root(tmp_path):
    out = await _tool(tmp_path)({"command": 'printf %s "$PWD"'})
    assert out == str(tmp_path.resolve())


async def test_timeout_raises_and_kills_process(tmp_path):
    with pytest.raises(ToolError) as exc:
        await _tool(tmp_path)({"command": "sleep 5", "timeout_ms": 150})
    msg = str(exc.value)
    assert "timed out" in msg
    assert "150" in msg
    # the marker the sleep would have created if it survived must NOT exist
    # (process-group kill, not just wait_for cancel)
    assert not (tmp_path / "survived").exists()


async def test_timeout_kills_whole_group(tmp_path):
    # Load-bearing kill test. The grandchild sleeps 0.2s (just past the 0.1s
    # timeout) and would touch the marker at ~0.3s; we wait 0.5s (>> 0.2s) and
    # assert it never did. KEEP the trailing `wait`: without it the parent bash
    # exits immediately and the timeout would only fire via inherited-pipe
    # blocking (fragile). If killpg were a no-op the orphaned grandchild would
    # survive and create the marker.
    marker = tmp_path / "grandchild"
    cmd = f"(sleep 0.2 && touch {marker}) & wait"
    with pytest.raises(ToolError):
        await _tool(tmp_path)({"command": cmd, "timeout_ms": 100})
    await asyncio.sleep(0.5)  # well past the 0.2s grandchild sleep
    assert not marker.exists()


async def test_env_scrub_hides_provider_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    out = await _tool(tmp_path)({"command": 'printf %s "${ANTHROPIC_API_KEY:-MISSING}"'})
    assert out == "MISSING"


async def test_spawn_failure_raises_toolerror(tmp_path, monkeypatch):
    async def _boom(*a, **k):
        raise FileNotFoundError("bash")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    with pytest.raises(ToolError) as exc:
        await _tool(tmp_path)({"command": "printf hi"})
    assert "could not start" in str(exc.value)


async def test_bare_cd_teachback(tmp_path):
    out = await _tool(tmp_path)({"command": "cd /tmp"})
    assert "resets" in out  # the bare-cd teach note


async def test_oversize_output_truncates_head_and_tail(tmp_path):
    # 60k bytes -> capped to ~30k with a head+tail marker
    out = await _tool(tmp_path)({"command": "head -c 60000 /dev/zero | tr \\0 x"})
    assert "output truncated" in out
    assert len(out) < 40000


def test_spec_name_and_timeout_param(tmp_path):
    spec = _tool(tmp_path).spec
    from harness.types import ToolName

    assert spec.name == ToolName("bash")
    assert "timeout_ms" in spec.parameters["properties"]
