"""Queue journeys verified against the final terminal compositor and session log."""

import asyncio

from textual.widgets import Input

from harness.log import read_session
from harness.provider import text_turn, tool_call_turn
from harness.tools import ToolSpec
from harness.types import ModelId, ToolName
from tests.test_tui import GatedProvider, MODELS_TOML_TWO_ALIASES, make_app


def screen_text(app):
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


async def test_queue_visible_draft_preserved_and_cancel_requires_resume(tmp_path):
    provider = GatedProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("gated"))
    app.controller.max_pending = 1
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"first", "enter")
        await pilot.pause(0.1)
        await pilot.press(*"follow up", "enter")
        await pilot.pause(0.1)
        assert "Queue waiting (1, memory only)" in screen_text(app)
        assert "#2 follow up" in screen_text(app)
        events = read_session(tmp_path, app.kernel.session.id)
        assert [e.event.text for e in events if e.event.type == "user_message"] == ["first"]
        await pilot.press(*"unsent draft", "enter")
        await pilot.pause(0.1)
        assert app.query_one("#prompt", Input).value == "unsent draft"
        assert "unsent draft" in screen_text(app)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert "Queue paused" in screen_text(app)
        assert app.controller.pending[0].text == "follow up"
        assert app.query_one("#prompt", Input).value == "unsent draft"
        provider.release.set()
        await pilot.pause(0.1)
        assert len([m for m in app.kernel.loop.history if m.role == "user"]) == 1
        app.query_one("#prompt", Input).value = "/queue resume"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert [m.text() for m in app.kernel.loop.history if m.role == "user"] == ["first", "follow up"]


async def test_queue_edit_and_remove_do_not_fabricate_user_messages(tmp_path):
    app = make_app(tmp_path, provider=GatedProvider(), model=ModelId("gated"))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"first", "enter")
        await pilot.pause(0.1)
        await pilot.press(*"second", "enter")
        await pilot.press(*"/queue edit 2 revised", "enter")
        await pilot.pause(0.1)
        assert "#2 revised" in screen_text(app)
        await pilot.press(*"/queue edit 2", "enter")
        await pilot.pause(0.1)
        assert app.query_one("#prompt", Input).value == "revised"
        assert not app.controller.pending
        await pilot.press("enter")
        await pilot.press(*"/queue remove 3", "enter")
        await pilot.pause(0.1)
        assert not app.controller.pending
        events = read_session(tmp_path, app.kernel.session.id)
        assert [e.event.text for e in events if e.event.type == "user_message"] == ["first"]


async def test_model_switch_waits_for_all_iterations_of_current_turn(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []

    class WaitingTool:
        spec = ToolSpec(name=ToolName("wait"), description="", parameters={})

        async def __call__(self, args):
            entered.set()
            await release.wait()
            return "tool finished"

    class TwoStep:
        async def complete(self, *, model, messages, **kwargs):
            observed.append(str(model))
            chunks = (text_turn("done") if messages[-1].role == "tool" else
                      tool_call_turn("working", ToolName("wait"), {}))
            for chunk in chunks:
                yield chunk

    catalog = tmp_path / "models.toml"
    catalog.write_text(MODELS_TOML_TWO_ALIASES)
    app = make_app(tmp_path, provider=TwoStep(), model=ModelId("alias-a"), catalog_path=catalog)
    app.kernel.registry.register(WaitingTool())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await asyncio.wait_for(entered.wait(), 2)
        await app._switch_model("alias-b")
        await pilot.pause(0.1)
        assert app.kernel.loop.model == "alias-a"
        assert "Model alias-b will apply after this turn" in screen_text(app)
        release.set()
        await pilot.pause(0.3)
        assert observed == ["alias-a", "alias-a"]
        assert app.kernel.loop.model == "alias-b"
