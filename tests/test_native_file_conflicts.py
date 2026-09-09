"""A successful read is evidence for one agent and one version of file bytes."""

import asyncio
from dataclasses import replace
import os
import threading

import pytest

from harness.execution import ExecutionScope, current_scope
from harness.native_tools import EditFileTool, ReadFileTool, ReadState, ToolError, WriteFileTool
from harness.session import Session
from harness.tools import ToolRegistry
from harness.types import SessionId


def file_tools(root, state=None):
    state = state or ReadState()
    return (ReadFileTool(workspace_root=root, read_state=state),
            WriteFileTool(workspace_root=root, read_state=state),
            EditFileTool(workspace_root=root, read_state=state))


@pytest.mark.parametrize("kind", ["write", "edit"])
@pytest.mark.parametrize("change", ["bytes", "deleted", "outside-window"])
async def test_changed_file_requires_reread_before_mutation(tmp_path, kind, change):
    path = tmp_path / "shared.txt"
    path.write_text("target\nunseen original\n")
    read, write, edit = file_tools(tmp_path)
    await read({"file_path": path.name, "limit": 1})
    if change == "deleted":
        path.unlink()
    else:
        path.write_text("target\nunseen changed\n" if change == "outside-window" else "target\nnew work\n")
    args = ({"file_path": path.name, "content": "replacement"} if kind == "write" else
            {"file_path": path.name, "old_string": "target", "new_string": "edited"})
    expected = path.read_bytes() if path.exists() else None
    with pytest.raises(ToolError, match="changed|does not exist"):
        await (write if kind == "write" else edit)(args)
    assert (path.read_bytes() if path.exists() else None) == expected
    if expected is not None:
        await read({"file_path": path.name})
        await (write if kind == "write" else edit)(args)
        assert path.read_text() == ("replacement" if kind == "write" else "edited\n" + expected.decode().split("\n", 1)[1])
    else:
        with pytest.raises(ToolError, match="does not exist"):
            await read({"file_path": path.name})
        # A delivered missing-file observation permits a deliberate recreation.
        await write({"file_path": path.name, "content": "recreated"})
        assert path.read_text() == "recreated"


@pytest.mark.parametrize("kind", ["write", "edit"])
async def test_shared_tools_keep_each_session_observation_separate(tmp_path, kind):
    path = tmp_path / "shared.txt"
    path.write_text("target\n")
    read, write, edit = file_tools(tmp_path)
    parent = Session(tmp_path / "data", SessionId("parent"))
    child = Session(tmp_path / "data", SessionId("child"))
    scope = ExecutionScope(parent, ToolRegistry())
    token = current_scope.set(scope)
    try:
        await read({"file_path": path.name})
        current_scope.set(replace(scope, session=child, depth=1))
        args = ({"file_path": path.name, "content": "child"} if kind == "write" else
                {"file_path": path.name, "old_string": "target", "new_string": "child"})
        with pytest.raises(ToolError, match="has not been read"):
            await (write if kind == "write" else edit)(args)
        await read({"file_path": path.name})
        await (write if kind == "write" else edit)(args)
        current_scope.set(scope)
        with pytest.raises(ToolError, match="changed"):
            await write({"file_path": path.name, "content": "parent stale overwrite"})
        assert path.read_text().startswith("child")
    finally:
        current_scope.reset(token)
        parent.close()
        child.close()


async def test_observation_is_from_delivered_bytes_not_a_second_path_read(tmp_path, monkeypatch):
    path = tmp_path / "shared.txt"
    path.write_text("old")
    read, write, _ = file_tools(tmp_path)
    original = read._read

    def raced(*args):
        result = original(*args)
        path.write_text("newer work")
        return result

    monkeypatch.setattr(read, "_read", raced)
    assert "old" in await read({"file_path": path.name})
    with pytest.raises(ToolError, match="changed"):
        await write({"file_path": path.name, "content": "overwrite"})
    assert path.read_text() == "newer work"


async def test_queued_call_keeps_its_original_observation(tmp_path, monkeypatch):
    path = tmp_path / "shared.txt"
    path.write_text("old")
    read, write, _ = file_tools(tmp_path)
    await read({"file_path": path.name})
    entered, release = threading.Event(), threading.Event()
    original = write._write

    def paused(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(write, "_write", paused)
    first = asyncio.create_task(write({"file_path": path.name, "content": "first"}))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        second = asyncio.create_task(write({"file_path": path.name, "content": "stale second"}))
        await asyncio.sleep(0)
        assert not second.done()
        release.set()
        await first
        with pytest.raises(ToolError, match="changed"):
            await second
        assert path.read_text() == "first"
        # Sequential changes by the same caller may build on a delivered write.
        await write({"file_path": path.name, "content": "deliberate second"})
        assert path.read_text() == "deliberate second"
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)


@pytest.mark.parametrize("kind", ["write", "edit"])
async def test_historical_paths_are_not_current_content_evidence(tmp_path, kind):
    path = tmp_path / "shared.txt"
    path.write_text("target")
    read, write, edit = file_tools(tmp_path, ReadState({str(path)}))
    args = ({"file_path": path.name, "content": "new"} if kind == "write" else
            {"file_path": path.name, "old_string": "target", "new_string": "new"})
    with pytest.raises(ToolError, match="read_file"):
        await (write if kind == "write" else edit)(args)
    assert path.read_text() == "target"
    await read({"file_path": path.name})
    await (write if kind == "write" else edit)(args)
    assert path.read_text() == "new"


async def test_hash_uses_raw_bytes_even_with_same_size_mtime_and_rendered_text(tmp_path):
    path = tmp_path / "shared.txt"
    path.write_bytes(b"target\xff")
    timestamp = path.stat().st_mtime_ns
    read, write, _ = file_tools(tmp_path)
    await read({"file_path": path.name})
    path.write_bytes(b"target\xfe")  # Both decode to the same replacement character.
    os.utime(path, ns=(timestamp, timestamp))
    with pytest.raises(ToolError, match="changed"):
        await write({"file_path": path.name, "content": "overwrite"})
    assert path.read_bytes() == b"target\xfe"


async def test_competing_creates_do_not_silently_overwrite_the_first_result(tmp_path):
    _, write, _ = file_tools(tmp_path)
    results = await asyncio.gather(
        write({"file_path": "new.txt", "content": "first"}),
        write({"file_path": "new.txt", "content": "second"}), return_exceptions=True)
    assert "Created" in results[0]
    assert isinstance(results[1], ToolError) and "read_file" in str(results[1])
    assert (tmp_path / "new.txt").read_text() == "first"


@pytest.mark.parametrize("kind", ["write", "edit"])
async def test_conflict_rejection_does_not_touch_target_or_unrelated_files(tmp_path, kind):
    path = tmp_path / "shared.txt"
    path.write_text("target")
    read, write, edit = file_tools(tmp_path)
    await read({"file_path": path.name})
    path.write_text("target plus new work")
    (tmp_path / "shared.txt.harness.tmp").write_text("unrelated file")
    expected = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    args = ({"file_path": path.name, "content": "overwrite"} if kind == "write" else
            {"file_path": path.name, "old_string": "target", "new_string": "overwrite"})
    with pytest.raises(ToolError, match="changed"):
        await (write if kind == "write" else edit)(args)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == expected


async def test_unchanged_empty_file_and_timestamp_only_changes_allow_chained_edits(tmp_path):
    path = tmp_path / "shared.txt"
    path.write_bytes(b"")
    read, write, edit = file_tools(tmp_path)
    await read({"file_path": path.name})
    os.utime(path, ns=(1, 1))
    await write({"file_path": path.name, "content": "first\r\n"})
    await edit({"file_path": path.name, "old_string": "first\n", "new_string": "second\n"})
    await edit({"file_path": path.name, "old_string": "second", "new_string": "third"})
    assert path.read_bytes() == b"third\n"


@pytest.mark.parametrize("cancel", [False, True])
async def test_undelivered_read_cannot_refresh_a_stale_observation(tmp_path, monkeypatch, cancel):
    path = tmp_path / "shared.txt"
    path.write_text("old")
    read, write, _ = file_tools(tmp_path)
    await read({"file_path": path.name})
    path.write_text("new work")
    if cancel:
        entered, release, settled = (threading.Event() for _ in range(3))
        original = read._read

        def paused(*args):
            try:
                result = original(*args)
                entered.set()
                assert release.wait(5)
                return result
            finally:
                settled.set()

        monkeypatch.setattr(read, "_read", paused)
        work = asyncio.create_task(read({"file_path": path.name}))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
        finally:
            release.set()
            assert await asyncio.to_thread(settled.wait, 3)
            await asyncio.gather(work, return_exceptions=True)
    else:
        with pytest.raises(ToolError, match="beyond the end"):
            await read({"file_path": path.name, "offset": 10})
    with pytest.raises(ToolError, match="changed"):
        await write({"file_path": path.name, "content": "overwrite"})
    assert path.read_text() == "new work"


async def test_cancelled_write_cannot_refresh_its_owners_observation(tmp_path, monkeypatch):
    path = tmp_path / "shared.txt"
    path.write_text("old")
    read, write, _ = file_tools(tmp_path)
    await read({"file_path": path.name})
    entered, release = threading.Event(), threading.Event()
    original = write._write

    def paused(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(write, "_write", paused)
    work = asyncio.create_task(write({"file_path": path.name, "content": "cancelled but applied"}))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        work.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await work
        with pytest.raises(ToolError, match="changed"):
            await write({"file_path": path.name, "content": "overwrite"})
        assert path.read_text() == "cancelled but applied"
        await read({"file_path": path.name})
        await write({"file_path": path.name, "content": "reconciled"})
        assert path.read_text() == "reconciled"
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)


async def test_real_coordination_records_conflict_and_child_reread_recovery(tmp_path):
    from harness.cli import build_kernel
    from harness.events import CoordinationFinished, ToolCallCompleted
    from harness.coordination import load_report
    from harness.fold import fold
    from harness.log import read_session
    from harness.mixture import Expert, run_strategy_result
    from harness.provider import FakeProvider, text_turn, tool_call_turn
    from harness.types import ModelId, ToolName
    from tests.test_handoff import permissions

    path = tmp_path / "shared.txt"
    path.write_text("old")
    ready = {name: asyncio.Event() for name in ("a", "b")}
    wrote = asyncio.Event()
    phases = {name: 0 for name in ready}

    class Peers(FakeProvider):
        async def complete(self, *, model, messages, **kwargs):
            name = str(model)
            step = phases[name]
            phases[name] += 1
            if step == 0:
                chunks = tool_call_turn("", ToolName("read_file"), {"file_path": path.name})
            elif step == 1:
                assert "old" in messages[-1].blocks[0].text
                ready[name].set()
                await ready["b" if name == "a" else "a"].wait()
                if name == "b":
                    await wrote.wait()
                chunks = tool_call_turn("", ToolName("write_file"), {"file_path": path.name, "content": name})
            elif name == "a":
                assert "Overwrote" in messages[-1].blocks[0].text
                wrote.set()
                chunks = text_turn("first change retained")
            elif step == 2:
                assert "changed since this agent" in messages[-1].blocks[0].text
                assert path.read_text() == "a"
                chunks = tool_call_turn("", ToolName("read_file"), {"file_path": path.name})
            elif step == 3:
                chunks = tool_call_turn("", ToolName("edit_file"), {
                    "file_path": path.name, "old_string": "a", "new_string": "a+b"})
            else:
                chunks = text_turn("combined after rereading")
            for chunk in chunks:
                yield chunk

    kernel = build_kernel(base_dir=tmp_path / "data", workspace_root=tmp_path, native_tools=True,
                          provider=Peers([]), model=ModelId("a"), permissions=permissions())
    try:
        await kernel.loop.start()
        result = await asyncio.wait_for(run_strategy_result("ensemble", kernel.runner, kernel.session,
            "Update the shared file", [Expert("a"), Expert("b")]), 5)
        assert result.status == "completed" and result.acceptance == "unverified"
        assert path.read_text() == "a+b"
        event = next(e.event for e in read_session(kernel.session.base, kernel.session.id)
                     if isinstance(e.event, CoordinationFinished))
        report = load_report(kernel.session.blobs, event, kernel.session.id)
        assert all(m.result.run_id and m.result.status == "completed" for m in report.members)
        second = report.members[1].result
        events = read_session(kernel.session.base, second.child_session_id)
        calls = [e.event for e in events if isinstance(e.event, ToolCallCompleted)]
        assert [e.is_error for e in calls] == [False, True, False, False]
        assert "read_file it again" in calls[1].result_text
        assert not fold(events).open_agent_runs and not fold(events).open_intents
        assert not kernel.loop.dispatcher.scope.budget.busy
    finally:
        kernel.session.close()


async def test_terminal_shows_conflict_recovery_and_keeps_unsent_draft(tmp_path):
    from textual.widgets import Input
    from harness.provider import FakeProvider, text_turn, tool_call_turn
    from harness.types import ToolName
    from tests.test_handoff import permissions
    from tests.test_tui import make_app
    from tests.test_tui_queue import screen_text

    path = tmp_path / "shared.txt"
    path.write_text("old")
    conflict, resume = asyncio.Event(), asyncio.Event()

    class Changed(FakeProvider):
        async def complete(self, **kwargs):
            if len(self.script) == 4:
                path.write_text("other work")
            elif len(self.script) == 3:
                conflict.set()
                await resume.wait()
            async for chunk in super().complete(**kwargs):
                yield chunk

    provider = Changed([
        tool_call_turn("", ToolName("read_file"), {"file_path": path.name}),
        tool_call_turn("", ToolName("write_file"), {"file_path": path.name, "content": "stale overwrite"}),
        tool_call_turn("", ToolName("read_file"), {"file_path": path.name}),
        tool_call_turn("", ToolName("edit_file"), {
            "file_path": path.name, "old_string": "other work", "new_string": "other work plus my edit"}),
        text_turn("Recovered after rereading the file."),
    ])
    app = make_app(tmp_path / "data", provider=provider, native_tools=True, workspace_root=tmp_path,
                   permissions=permissions())
    try:
        async with app.run_test(size=(150, 45)) as pilot:
            composer = app.query_one("#prompt", Input)
            composer.value = "Update the file"
            await pilot.press("enter")
            await asyncio.wait_for(conflict.wait(), 3)
            composer.value = "keep my draft"
            await pilot.pause(.1)
            visible = " ".join(screen_text(app).split())
            assert "File changed since this agent" in visible and "read_file it again" in visible
            assert path.read_text() == "other work" and composer.value == "keep my draft"
            resume.set()
            await asyncio.wait_for(app._turn_worker.wait(), 3)
            await pilot.pause(.1)
            assert "Recovered after rereading the file." in screen_text(app)
            assert path.read_text() == "other work plus my edit" and composer.value == "keep my draft"
    finally:
        resume.set()
        app.kernel.session.close()
