"""Operator setup remains usable without running a model turn."""

import asyncio
import shlex
from types import SimpleNamespace

import pytest
from textual.widgets import Input

from harness.catalog import Catalog
from harness.log import read_session
from harness.models_cli import perform
from tests.test_model_management import setup_files  # noqa: F401 -- shared file fixture
from tests.test_tui import make_app
from tests.test_tui_queue import screen_text


async def test_add_then_select_new_alias_through_terminal(tmp_path, setup_files):  # noqa: F811
    app = make_app(tmp_path, catalog_path=setup_files["catalog_path"])
    async with app.run_test(size=(130, 42)) as pilot:
        await pilot.pause(0.1)
        composer = app.query_one("#prompt", Input)
        composer.value = shlex.join(["/models", "add", "new-local", "--file", str(setup_files["model_file"]),
                                   "--runtime", str(setup_files["runtime"])])
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert "Registered new-local" in screen_text(app)
        composer.value = "/model new-local"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.kernel.loop.model == "new-local"
        assert app.kernel.provider.catalog.resolve("new-local").local.auto_start
        assert app.kernel.runner.default_model == "new-local"
        assert "model → new-local" in screen_text(app)
        events = read_session(tmp_path, app.kernel.session.id)
        assert not any(e.event.type in ("user_message", "model_call_proposed", "local_runtime_requested") for e in events)


@pytest.mark.parametrize("cancel", ["escape", "clear"])
async def test_setup_progress_and_cancellation_preserve_draft(tmp_path, setup_files, monkeypatch, cancel):  # noqa: F811
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending(words, *, progress, **kwargs):
        progress("Verifying installed model: 4/5000 MiB")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("harness.models_cli.perform", pending)
    app = make_app(tmp_path, catalog_path=setup_files["catalog_path"])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/models add pending", "enter")
        await asyncio.wait_for(entered.wait(), 2)
        assert "Verifying installed model" in screen_text(app)
        if cancel == "escape":
            await pilot.press(*"unsent draft", "escape")
        else:
            await pilot.press(*"/clear", "enter")
        await asyncio.wait_for(cancelled.wait(), 2)
        await pilot.pause(0.2)
        assert not app.query_one("#model-progress").display
        if cancel == "escape":
            assert "Model operation cancelled" in screen_text(app)
            assert app.query_one("#prompt", Input).value == "unsent draft"
        assert not setup_files["catalog_path"].exists()


async def test_core_models_command_cannot_expand_plugin_prompt(tmp_path, setup_files):  # noqa: F811
    app = make_app(tmp_path, catalog_path=setup_files["catalog_path"])
    app._plugin_commands["models"] = SimpleNamespace(body="plugin hijack")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/models", "enter")
        await pilot.pause(0.2)
        assert "No models configured" in screen_text(app)
        assert "plugin hijack" not in screen_text(app)
        await pilot.press(*"/models --cat /tmp/different list", "enter")
        await pilot.pause(0.2)
        assert "Model operation failed" in screen_text(app)


async def test_hub_setup_does_not_change_active_catalog(tmp_path, setup_files):  # noqa: F811
    # Operator registration affects selection only at the normal /model boundary.
    from harness.provider_litellm import CatalogProvider
    path = setup_files["catalog_path"]
    path.write_text("[models.original]\nroute='openai/original'\n")
    provider = CatalogProvider(Catalog.load(path))
    app = make_app(tmp_path, catalog_path=path, provider=provider)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await perform(["add", "new-local", "--file", str(setup_files["model_file"]),
                       "--runtime", str(setup_files["runtime"])], catalog_path=path)
        assert provider.catalog.aliases() == ("original",)
        assert Catalog.load(path).aliases() == ("original", "new-local")
