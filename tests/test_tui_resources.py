"""Local readiness work must not block the composer or corrupt task history."""

import asyncio
import os

import httpx
import pytest
from textual.widgets import Input

from harness.log import read_session
from harness.provider_litellm import CatalogProvider
from harness.resources import LocalResources
from harness.types import ModelId
from tests.test_local_resources import catalog, owned_catalog
from tests.test_tui import make_app
from tests.test_tui_queue import screen_text


async def test_resource_check_keeps_draft_responsive_and_cancels_without_a_turn(tmp_path):
    entered = asyncio.Event()

    async def probe(request):
        entered.set()
        await asyncio.Event().wait()

    resources = LocalResources(transport=httpx.MockTransport(probe))
    app = make_app(tmp_path, provider=CatalogProvider(catalog(probe_seconds=10)), resources=resources,
                   model=ModelId("local"))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/resources check local", "enter")
        await asyncio.wait_for(entered.wait(), 2)
        await pilot.press(*"unsent draft")
        assert "Checking local runtime local" in screen_text(app)
        assert "unsent draft" in screen_text(app)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert "Local resource check cancelled" in screen_text(app)
        assert app.query_one("#prompt", Input).value == "unsent draft"
        events = read_session(tmp_path, app.kernel.session.id)
        assert not any(e.event.type in ("user_message", "agent_run_started", "model_call_proposed") for e in events)


async def test_unavailable_local_task_pauses_followups_and_preserves_draft(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(request):
        entered.set()
        await release.wait()
        return httpx.Response(503, json={"error": {"type": "loading"}})

    resources = LocalResources(transport=httpx.MockTransport(probe))
    app = make_app(tmp_path, provider=CatalogProvider(catalog(probe_seconds=10)), resources=resources,
                   model=ModelId("local"))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"work", "enter")
        await asyncio.wait_for(entered.wait(), 2)
        await pilot.press(*"follow up", "enter")
        await pilot.press(*"unsent draft")
        assert "local model checking" in screen_text(app)
        release.set()
        await pilot.pause(0.3)
        assert "loading" in screen_text(app)
        assert "Queue paused" in screen_text(app) and "#2 follow up" in screen_text(app)
        assert app.query_one("#prompt", Input).value == "unsent draft"
        assert app.controller.last_failed.text == "work"


async def test_clear_keeps_resource_owner_but_cancels_old_session_probe(tmp_path):
    entered = asyncio.Event()

    async def probe(request):
        entered.set()
        await asyncio.Event().wait()

    resources = LocalResources(transport=httpx.MockTransport(probe))
    app = make_app(tmp_path, provider=CatalogProvider(catalog(probe_seconds=10)), resources=resources,
                   model=ModelId("local"))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        old_session = app.kernel.session.id
        await pilot.click("#prompt")
        await pilot.press(*"/resources check local", "enter")
        await asyncio.wait_for(entered.wait(), 2)
        await pilot.press(*"/clear", "enter")
        await pilot.pause(0.3)
        assert app.kernel.session.id != old_session
        assert app.kernel.resources is resources
        assert resources.snapshot(catalog(probe_seconds=10).resolve("local")).status == "unknown"


@pytest.mark.parametrize("failed_event", ["stop_intent", "stop_observation"])
async def test_finish_ends_session_after_owned_resource_journal_failure(
    tmp_path, unused_tcp_port, monkeypatch, failed_event,
):
    resources = LocalResources()
    app = make_app(tmp_path, resources=resources)
    append = app.kernel.session.append

    def record(event):
        if (failed_event == "stop_intent" and event.type == "local_runtime_requested"
                and event.action == "stop") or (
                failed_event == "stop_observation" and event.type == "resource_observed"
                and event.observation.status == "stopped"):
            raise OSError("stop journal failure")
        return append(event)

    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.1)
            async with resources.use(owned_catalog(tmp_path, unused_tcp_port).resolve("local"),
                                     emit=append):
                pass
            pid = int((tmp_path / "runtime.pid").read_text())
            monkeypatch.setattr(app.kernel.session, "append", record)
            await app._finish()
            await pilot.pause(0.1)
            assert "local resource cleanup failed" in screen_text(app)
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            await app._finish()  # repeated finish and unmount cannot duplicate the terminal
        events = read_session(tmp_path, app.kernel.session.id)
        assert sum(e.event.type == "session_ended" for e in events) == 1
    finally:
        await resources.close(emit=append)
        app.kernel.session.close()
