"""Exercise document navigation through the terminal compositor and session journal."""

import asyncio
import webbrowser
from pathlib import Path

import pytest
from rich.cells import cell_len

from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import FakeProvider, tool_call_turn
from harness.types import ModelId, ToolName
from tests.test_tui import make_app


def screen_lines(app):
    return [strip.text for strip in app.screen._compositor.render_strips()]


async def see(app, pilot, label):
    """Find the cell occupied by the visible label, after any worker finishes."""
    for _ in range(50):
        await pilot.pause(0.03)
        lines = screen_lines(app)
        for row, line in enumerate(lines):
            index = line.find(label)
            if index >= 0:
                return cell_len(line[:index]) + cell_len(label) // 2, row
    raise AssertionError(f"{label!r} was not visible:\n" + "\n".join(lines))


async def submit(pilot, text):
    await pilot.click("#prompt")
    await pilot.press(*text, "enter")


def show_files(*paths):
    return FakeProvider([
        tool_call_turn("", ToolName("read_file"), {"file_path": path, "display": True})[1:]
        for path in paths
    ])


def make_session(storage, workspace, provider, **kwargs):
    return make_app(storage, workspace_root=workspace, native_tools=True,
                    model=ModelId("fake:echo"), provider=provider, **kwargs)


def journal(app):
    return [item.event for item in read_session(app.kernel.session.base, app.kernel.session.id)]


def assert_authorized_read(events, path):
    [resolved] = [event for event in events if event.type == "dispatch_resolved"
                  and event.tool == "read_file" and Path(event.args["file_path"]) == path]
    call = [event for event in events if getattr(event, "call_id", None) == resolved.call_id]
    assert call[0].type == "tool_call_proposed"
    assert any(event.type == "hook_decided" and event.hook == "permissions"
               and event.decision["kind"] == "allow" for event in call[:call.index(resolved)])
    assert call[-1].type == "tool_call_completed" and not call[-1].is_error
    assert call[-1].operator_view
    assert call[-1].presented_file.file_path == str(path)


async def test_nested_document_link_and_section_still_work_from_an_older_replayed_source(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("NO_COLOR", "1")
    workspace = tmp_path / "workspace"
    old = workspace / "manual"
    new = workspace / "release"
    old.mkdir(parents=True)
    new.mkdir()
    source = old / "index.md"
    source.write_text("[Original manual](guide.md)\n")
    (new / "index.md").write_text("[Release manual](guide.md)\n")
    (new / "guide.md").write_text("Different manual in the newer directory.\n")
    guide = old / "guide.md"
    guide.write_text(
        "# Field guide\n\nOriginal opening paragraph.\n\n[Jump to repairs](#repairs)\n\n"
        + "".join(f"Checklist item {n}.\n\n" for n in range(65))
        + "## Repairs\n\nVerified repair instructions.\n"
    )
    provider = show_files("manual/index.md", "release/index.md")
    storage = tmp_path / "state"
    app = make_session(storage, workspace, provider)
    assert app.no_color

    async def open_original(current, pilot):
        previous = len(journal(current))
        await pilot.click(offset=await see(current, pilot, "Original manual"))
        await see(current, pilot, "Original opening paragraph.")
        assert current.screen.query("#document-scroll")
        assert "Verified repair instructions." not in "\n".join(screen_lines(current))
        assert_authorized_read(journal(current)[previous:], guide)
        await pilot.press("l")
        await see(current, pilot, "#repairs")
        await pilot.press("enter")
        await see(current, pilot, "Verified repair instructions.")
        assert "Original opening paragraph." not in "\n".join(screen_lines(current))
        assert not any(event.type == "model_call_proposed" for event in journal(current)[previous:])

    async with app.run_test(size=(52, 38)) as pilot:
        await submit(pilot, "Show the original index.")
        await see(app, pilot, "Original manual")
        await submit(pilot, "Show the release index.")
        await see(app, pilot, "Release manual")
        await open_original(app, pilot)
    session_id = app.kernel.session.id
    app.kernel.session.close()
    source.write_text("The saved original index has been replaced.\n")

    resumed = make_session(storage, workspace, provider, resume_session_id=session_id)
    async with resumed.run_test(size=(52, 38)) as pilot:
        await see(resumed, pilot, "Release manual")
        await open_original(resumed, pilot)
    resumed.kernel.session.close()
    assert len(provider.calls) == 2


@pytest.mark.parametrize("opener_failure", ["returns_false", "raises"])
async def test_keyboard_link_picker_exposes_destination_and_respects_denied_reads(
    tmp_path, monkeypatch, opener_failure,
):
    monkeypatch.setenv("NO_COLOR", "1")
    url = "https://example.org/guide?q=one#help"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    index = workspace / "index.md"
    restricted = workspace / "restricted.txt"
    markdown = f"[Private notes](restricted.txt)\n\n[Online manual]({url})\n"
    index.write_text(markdown)
    restricted.write_text("RESTRICTED CONTENT")
    engine = PermissionEngine([RuleSet(rules=[
        PermissionRule(action="ask", tool="read_file", match={"file_path": "*restricted.txt"}),
    ])])
    provider = show_files("index.md")
    app = make_session(tmp_path / "state", workspace, provider, engine=engine)
    attempts = []
    loop = asyncio.get_running_loop()
    retry_handed_off = asyncio.Event()

    def browser_handoff(target, *args, **kwargs):
        attempts.append(target)
        if len(attempts) == 1:
            if opener_failure == "raises":
                raise OSError("browser unavailable")
            return False
        loop.call_soon_threadsafe(retry_handed_off.set)
        return True

    monkeypatch.setattr(webbrowser, "open", browser_handoff)
    async with app.run_test(size=(52, 36)) as pilot:
        await submit(pilot, "Show the index.")
        await see(app, pilot, "Online manual")
        await submit(pilot, "/links")
        await see(app, pilot, "restricted.txt")
        await pilot.press("down")
        await see(app, pilot, url)
        assert attempts == []
        previous = len(journal(app))
        await pilot.press("enter")
        await see(app, pilot, url)
        assert attempts == [url]
        # A failed browser handoff leaves the destination available for copying.
        assert app.screen.query("#link-options")
        await pilot.press("c")
        assert app.clipboard == url
        assert attempts == [url]
        await pilot.press("enter")
        await asyncio.wait_for(retry_handed_off.wait(), 2)
        assert attempts == [url, url]

        await submit(pilot, "/links")
        await see(app, pilot, "restricted.txt")
        await pilot.press("enter")
        await see(app, pilot, "Permission: tool read_file")
        assert "RESTRICTED CONTENT" not in "\n".join(screen_lines(app))
        await pilot.press("n")
        await see(app, pilot, "denied")
        assert "RESTRICTED CONTENT" not in "\n".join(screen_lines(app))
        events = journal(app)[previous:]
        [read] = [event for event in events if event.type == "tool_call_proposed"]
        assert read.tool == "read_file"
        assert Path(read.args["file_path"]).name == restricted.name
        call = [event for event in events if getattr(event, "call_id", None) == read.call_id]
        assert any(event.type == "permission_requested" for event in call)
        assert any(event.type == "permission_resolved" and not event.allowed for event in call)
        assert all(event.type != "dispatch_resolved" for event in call)
        assert call[-1].type == "tool_call_completed" and call[-1].is_error
        assert all(event.type != "model_call_proposed" for event in events)
    app.kernel.session.close()
    assert len(provider.calls) == 1
    assert index.read_text() == markdown
    assert restricted.read_text() == "RESTRICTED CONTENT"
