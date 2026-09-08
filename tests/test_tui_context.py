"""The same context profile governs execution, inspection, and session changes."""

from textual.widgets import Input

from harness.context import ContextPolicy
from harness.log import read_session
from tests.test_tui import make_app
from tests.test_tui_queue import screen_text


async def test_context_cap_is_visible_before_the_first_stats_tick(tmp_path, monkeypatch):
    app = make_app(tmp_path, context_policy=ContextPolicy(history_turns=1, tools=()))
    set_interval = app.set_interval

    def pause_stats(interval, callback, *args, **kwargs):
        timer = set_interval(interval, callback, *args, **kwargs)
        if callback == app.refresh_stats:
            timer.pause()
        return timer

    monkeypatch.setattr(app, "set_interval", pause_stats)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.1)
        assert "ctx cap 32,768B" in screen_text(app)
        assert not app.kernel.loop.history


async def test_profile_inspection_omission_notice_and_clear_preserve_draft_and_policy(tmp_path):
    policy = ContextPolicy(history_turns=1, tools=("read_file",))
    app = make_app(tmp_path, context_policy=policy, native_tools=True, workspace_root=tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/context", "enter")
        await pilot.pause(0.1)
        assert "up to 1 recent turns" in screen_text(app)
        assert "Tools: read_file" in screen_text(app)
        assert "ctx cap 32,768B" in screen_text(app)
        await pilot.press(*"first", "enter")
        await pilot.pause(0.2)
        await pilot.press(*"second", "enter")
        await pilot.pause(0.2)
        assert "1 earlier turn(s) omitted" in screen_text(app)
        assert len(app.kernel.loop.history) == 4
        old_id = app.kernel.session.id
        await pilot.press(*"/clear", "enter")
        await pilot.pause(0.3)
        assert app.kernel.session.id != old_id and app.kernel.context_policy == policy
        assert [s.name for s in app.kernel.loop.registry.specs()] == ["read_file"]
        await pilot.press(*"unsent draft")
        assert app.query_one("#prompt", Input).value == "unsent draft"
        assert not any(e.event.type == "user_message"
                       for e in read_session(tmp_path, app.kernel.session.id))


async def test_resuming_another_session_uses_its_own_profile(tmp_path):
    from harness.cli import build_kernel
    from harness.provider import EchoProvider
    from harness.types import ModelId
    target_policy = ContextPolicy(history_turns=2, tools=())
    target = build_kernel(base_dir=tmp_path, provider=EchoProvider(), model=ModelId("echo"),
                          context_policy=target_policy)
    await target.loop.start()
    await target.loop.end()
    target.session.close()
    app = make_app(tmp_path, context_policy=ContextPolicy(history_turns=4, tools=("read_file",)))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._rebuild_kernel(resume_session_id=target.session.id)
        assert app.kernel.context_policy == target_policy
        assert app.kernel.loop.registry.specs() == ()
