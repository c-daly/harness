"""Queued work makes real capture progress while fresh input stays interruptible."""

import asyncio

import pytest
from textual.widgets import Input

from harness.permissions import PermissionEngine, RuleSet
from harness.provider import text_turn
from harness.types import ModelId
from tests.test_resident_capture import Adapter, facts, prepare, profile, receipt, state
from tests.test_tui import make_app
from tests.test_tui_queue import screen_text


def capture_app(root, provider, *, save_fn=receipt, catalog_path=None):
    app = make_app(root, provider=provider, model=ModelId("resident"), catalog_path=catalog_path,
        native_tools=True, workspace_root=root, context_policy=profile(root, timeout_seconds=120),
        permissions=PermissionEngine([RuleSet(default="allow")]))
    for name, callback in (("prepare", prepare), ("save", save_fn)):
        app.kernel.registry.register(Adapter(name, callback))
    return app


async def test_continuously_replenished_queue_saves_capture_between_turns(tmp_path):
    class Provider:
        turns = 0

        async def complete(self, **kwargs):
            if kwargs["model"] == "resident":
                self.turns += 1
                assert sum(o.status == "saved" for o in state(app.kernel)[2].values()) >= self.turns - 1
                if self.turns < 5:
                    # Always leave another prompt queued before this turn ends.
                    app._enqueue_prompt(f"next {self.turns}")
            await asyncio.sleep(0)
            for chunk in text_turn("answer" if kwargs["model"] == "resident" else "record"):
                yield chunk

    provider = Provider()
    app = capture_app(tmp_path, provider)
    async with app.run_test() as pilot:
        app._enqueue_prompt("first")
        worker = app._turn_worker
        await asyncio.wait_for(worker.wait(), 5)
        await app.kernel.loop.captures.wait()
        await pilot.pause()
        assert provider.turns == 5 and len(state(app.kernel)[2]) == 5
        assert all(o.status == "saved" for o in state(app.kernel)[2].values())
        assert not app.controller.paused and not app.controller.pending


@pytest.mark.parametrize("action", ["input", "escape", "pause", "clear"])
async def test_queued_capture_opportunity_is_visible_and_interruptible(tmp_path, action):
    entered, settled = asyncio.Event(), asyncio.Event()
    writes = 0

    async def save(args):
        nonlocal writes
        writes += 1
        if writes == 1:
            entered.set()
            try:
                await asyncio.Future()
            finally:
                settled.set()
        return receipt(args)

    class Provider:
        async def complete(self, **kwargs):
            if kwargs["model"] == "resident" and entered.is_set():
                assert settled.is_set(), "foreground overlapped unfinished capture cleanup"
            for chunk in text_turn("answer" if kwargs["model"] == "resident" else "record"):
                yield chunk

    app = capture_app(tmp_path, Provider(), save_fn=save)
    async with app.run_test(size=(140, 45)) as pilot:
        app._enqueue_prompt("first")
        app._enqueue_prompt("already queued")
        worker = app._turn_worker
        await asyncio.wait_for(entered.wait(), 3)
        await pilot.pause()
        assert app.controller.active is None
        assert app.controller.last_result.status == "completed"
        assert "Saving continuity before queued work" in screen_text(app)
        assert "answer" in screen_text(app)
        app.query_one("#prompt", Input).value = "unsent draft"
        if action == "input":
            app._enqueue_prompt("fresh input")
        elif action == "escape":
            await pilot.press("escape")
        else:
            app._queue_command(action)
        await asyncio.wait_for(settled.wait(), 3)
        if action == "escape":
            # The Esc handler owns worker settlement; wait for its UI update.
            for _ in range(30):
                if not app._interrupting:
                    break
                await pilot.pause(.05)
            assert not app._interrupting
        else:
            await asyncio.wait_for(worker.wait(), 3)
        assert app.query_one("#prompt", Input).value == "unsent draft"
        assert app.controller.last_failed is None
        users = [e.event.text for e in facts(app.kernel) if e.event.type == "user_message"]
        assert users == (["first", "already queued", "fresh input"] if action == "input" else ["first"])
        if action in {"escape", "pause"}:
            assert app.controller.paused
            assert [p.text for p in app.controller.pending] == ["already queued"]
        if action == "clear":
            assert not app.controller.pending
        assert not any(e.event.type == "user_interrupt" for e in facts(app.kernel))


async def test_model_selected_during_capture_applies_to_the_next_queued_prompt(tmp_path):
    from harness.catalog import Catalog
    from harness.provider_litellm import CatalogProvider

    entered = asyncio.Event()
    models = []
    writes = 0
    catalog = tmp_path / "models.toml"
    catalog.write_text("\n".join(
        f"[models.{name}]\nroute = 'fake/{name}'\ninput_cost_per_token = 0.0\noutput_cost_per_token = 0.0\n"
        for name in ("resident", "replacement", "recorder")))

    class Provider(CatalogProvider):
        async def infer(self, request):
            if request.model != "recorder":
                models.append(str(request.model))
            for chunk in text_turn("answer" if request.model != "recorder" else "record"):
                yield chunk

    async def save(args):
        nonlocal writes
        writes += 1
        if writes == 1:
            entered.set()
            await asyncio.Future()
        return receipt(args)

    app = capture_app(tmp_path, Provider(Catalog.load(catalog)), save_fn=save, catalog_path=catalog)
    async with app.run_test():
        app._enqueue_prompt("first")
        app._enqueue_prompt("second")
        worker = app._turn_worker
        await asyncio.wait_for(entered.wait(), 3)
        assert app._capture_waiting and app.controller.last_result.status == "completed"
        await app._switch_model("replacement")
        await asyncio.wait_for(worker.wait(), 3)
        await app.kernel.loop.captures.wait()
        assert models == ["resident", "replacement"]
        assert app.kernel.loop.model == "replacement" and app._pending_model is None
        assert not app.controller.paused


async def test_capture_failure_is_visible_but_does_not_pause_the_queue(tmp_path, monkeypatch):
    class Provider:
        async def complete(self, **kwargs):
            for chunk in text_turn("answer" if kwargs["model"] == "resident" else "record"):
                yield chunk

    app = capture_app(tmp_path, Provider())
    append = app.kernel.session.append

    def fail_record(event):
        if event.type == "capture_prepared":
            raise OSError("capture artifact unavailable")
        return append(event)

    monkeypatch.setattr(app.kernel.session, "append", fail_record)
    async with app.run_test(size=(140, 45)) as pilot:
        app._enqueue_prompt("first")
        app._enqueue_prompt("second")
        worker = app._turn_worker
        await asyncio.wait_for(worker.wait(), 3)
        await app.kernel.loop.captures.pause()
        await pilot.pause()
        assert [e.event.text for e in facts(app.kernel) if e.event.type == "user_message"] == ["first", "second"]
        assert not app.controller.paused and app.controller.last_failed is None
        assert app.controller.last_result.status == "completed"
        assert "capture worker failed: OSError" in screen_text(app)
        assert all(o.status == "pending" for o in state(app.kernel)[2].values())
