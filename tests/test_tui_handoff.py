"""The final terminal exposes reconciliation and preserves interruption controls."""

import asyncio

import pytest
from textual.widgets import Input

from harness.handoff import render_handoff
from harness.log import read_session
from harness.tui import HarnessApp
from tests.test_handoff import source, specification
from tests.test_tui_queue import screen_text


async def command(app, pilot, text):
    app.query_one("#prompt", Input).value = text
    await pilot.press("enter")
    await pilot.pause(.1)
    worker = app._semantic_worker
    if worker is not None and not worker.is_finished:
        await asyncio.wait_for(worker.wait(), 3)
        await pilot.pause(.1)


@pytest.mark.parametrize("interrupt", [False, True])
async def test_terminal_handoff_inspection_execution_and_cancel_keep_draft_and_task(tmp_path, interrupt):
    kernel, provider = await source(tmp_path)
    kernel.resumed = True  # The test has already started and populated this live session.
    app = HarnessApp(kernel, native_tools=True, workspace_root=provider.root)
    path = tmp_path / "handoff.json"
    path.write_text(specification(kernel).model_dump_json())
    try:
        async with app.run_test(size=(160, 52)) as pilot:
            await command(app, pilot, "/handoff inspect")
            visible = " ".join(screen_text(app).split())
            assert "provider_native" in visible and "Snapshot:" in visible and "uncertain" in visible
            assert "operator inspection" in visible
            await command(app, pilot, f"/handoff record {path}")
            from harness.handoff import read_handoffs
            record = list(read_handoffs(kernel.session).values())[-1]
            assert "recorded; review before running" in " ".join(screen_text(app).split())
            if interrupt:
                provider.hang = True
                app.query_one("#prompt", Input).value = f"/handoff run {record.id}"
                await pilot.press("enter")
                await asyncio.wait_for(provider.entered.wait(), 3)
                app.query_one("#prompt", Input).value = "keep this draft"
                await pilot.press("escape")
                await pilot.pause(.2)
                assert app.query_one("#prompt", Input).value == "keep this draft"
                assert "Handoff interrupted" in screen_text(app)
                assert kernel.tasks.selected().execution == "cancelled"
            else:
                await command(app, pilot, f"/handoff run {record.id}")
                assert "Handoff completed" in screen_text(app)
                assert (provider.root / "B.txt").read_text() == "stage B\n"
            assert not kernel.tasks.selected().accepted
            assert kernel.tasks.selected().definition.id == record.task_id
    finally:
        kernel.session.close()


async def test_handoff_command_cannot_be_redirected_by_a_plugin(tmp_path):
    from types import SimpleNamespace
    kernel, provider = await source(tmp_path)
    kernel.resumed = True
    app = HarnessApp(kernel, native_tools=True, workspace_root=provider.root)
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            before = read_session(kernel.session.base, kernel.session.id)
            expected = render_handoff(kernel.session)
            app._plugin_commands["handoff"] = SimpleNamespace(body="Do the original assignment again")
            await command(app, pilot, "/handoff inspect")
            assert "Snapshot:" in expected and "Snapshot:" in screen_text(app)
            assert read_session(kernel.session.base, kernel.session.id) == before
            assert not provider.requests
    finally:
        kernel.session.close()
