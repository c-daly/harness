"""Ordinary restart and session switching must retain the user's catalog choice."""

import asyncio

import pytest
from textual.widgets import Input

from harness.catalog import Catalog
from harness.cli import build_kernel, main
from harness.events import ModelCallCompleted, ModelSelected
from harness.fold import fold
from harness.log import read_session
from harness.provider import StreamStop, TextDelta
from harness.provider_litellm import CatalogProvider
from harness.types import CallId, ModelId, new_session_id
from tests.test_tui import MODELS_TOML_TWO_ALIASES, make_app
from tests.test_tui_queue import screen_text


@pytest.fixture
def catalog_path(tmp_path):
    path = tmp_path / "models.toml"
    path.write_text(MODELS_TOML_TWO_ALIASES)
    return path


async def seed_session(base, catalog_path, *, pinned=True):
    kernel = build_kernel(base_dir=base, provider=CatalogProvider(Catalog.load(catalog_path)),
                          model=ModelId("alias-a"), model_pinned=pinned)
    await kernel.loop.start()
    await kernel.loop.end()
    kernel.session.close()
    return kernel.session.id


def seed_legacy_session(base):
    from harness.events import SessionEnded
    from harness.session import Session

    # Real pre-selection event shape, including a historical default that
    # cannot establish pin intent or describe later unrecorded /model changes.
    with Session(base, new_session_id(), default_model=ModelId("historical")) as session:
        session.start()
        session.append(SessionEnded())
    return session.id


@pytest.mark.parametrize("resume_flag", ["--continue", "--resume"])
@pytest.mark.parametrize("headless", [False, True])
def test_legacy_cli_resume_keeps_defaults_until_explicit_selection(
    tmp_path, catalog_path, monkeypatch, resume_flag, headless,
):
    from harness.routing import RoutingRuleSet
    sid = seed_legacy_session(tmp_path)
    routing = RoutingRuleSet(default="alias-a")
    captured = []

    async def launch(kernel, *args, **kwargs):
        captured.append((str(kernel.loop.model), kernel.loop.model_pinned))
        kernel.session.close()
        return "inspected"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("harness.routing.load_routing", lambda **kwargs: routing)
    monkeypatch.setattr("harness.tui.run_tui", launch)
    monkeypatch.setattr("harness.cli._amain", launch)
    resume_args = [resume_flag] + ([str(sid)] if resume_flag == "--resume" else [])
    argv = ["harness", *resume_args, "--base-dir", str(tmp_path), "--catalog", str(catalog_path),
            "--no-mcp", "--no-plugins"]
    if headless:
        argv += ["-p", "continue"]
    monkeypatch.setattr("sys.argv", argv)
    main()
    assert captured == [("alias-a", False)]
    assert fold(read_session(tmp_path, sid)).model_selection is None

    # An incidental default can disappear without poisoning the next resume.
    catalog_path.write_text('[models.alias-b]\nroute = "local/model-b"\n')
    routing.default = "alias-b"
    main()
    assert captured[-1] == ("alias-b", False)
    assert fold(read_session(tmp_path, sid)).model_selection is None

    # Only an explicit conversational choice establishes durable preference.
    monkeypatch.setattr("sys.argv", [*argv, "--model", "alias-b"])
    main()
    selection = ModelSelected(model=ModelId("alias-b"), pinned=True)
    assert fold(read_session(tmp_path, sid)).model_selection == selection
    monkeypatch.setattr("sys.argv", argv)
    routing.default = "missing"
    main()
    assert captured[-1] == ("alias-b", True)
    assert fold(read_session(tmp_path, sid)).model_selection == selection


@pytest.mark.parametrize("departing_pin", [True, False])
async def test_legacy_tui_resume_does_not_save_departing_choice(tmp_path, catalog_path, departing_pin):
    sid = seed_legacy_session(tmp_path)
    app = make_app(tmp_path, catalog_path=catalog_path,
                   provider=CatalogProvider(Catalog.load(catalog_path)),
                   model=ModelId("alias-b"), model_pinned=departing_pin)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert fold(read_session(tmp_path, app.kernel.session.id)).model_selection is not None
        await app._rebuild_kernel(resume_session_id=sid)
        await pilot.pause(0.05)
        assert app.kernel.loop.model == "alias-b"
        assert app.kernel.loop.model_pinned is departing_pin
        assert fold(read_session(tmp_path, sid)).model_selection is None
        # Reopening the same legacy log must remain free of inferred intent.
        await app._rebuild_kernel(resume_session_id=sid)
        await pilot.pause(0.05)
        assert fold(read_session(tmp_path, sid)).model_selection is None
        await app._switch_model("alias-a")
        selection = ModelSelected(model=ModelId("alias-a"), pinned=True)
        assert fold(read_session(tmp_path, sid)).model_selection == selection
        await app._rebuild_kernel(resume_session_id=sid)
        await pilot.pause(0.05)
        assert app.kernel.loop.model == "alias-a" and app.kernel.loop.model_pinned
        assert fold(read_session(tmp_path, sid)).model_selection == selection


@pytest.mark.parametrize("legacy", [False, True])
async def test_administrative_model_override_is_not_a_conversation_preference(tmp_path, catalog_path, legacy):
    sid = seed_legacy_session(tmp_path) if legacy else await seed_session(tmp_path, catalog_path)
    selection = fold(read_session(tmp_path, sid)).model_selection
    # The shape used by improvement/semantic/handoff commands: a provider and
    # model to operate with, sometimes pinned, but no conversation selection.
    kernel = build_kernel(base_dir=tmp_path, provider=CatalogProvider(Catalog.load(catalog_path)),
                          model=ModelId("alias-b"), model_pinned=True, resume_session_id=sid)
    try:
        assert kernel.loop.model == "alias-b"
        assert fold(read_session(tmp_path, sid)).model_selection == selection
    finally:
        kernel.session.close()


@pytest.mark.parametrize("pinned", [True, False])
@pytest.mark.parametrize("resume_flag", ["--continue", "--resume"])
def test_continue_restores_catalog_choice_before_any_model_call(
    tmp_path, catalog_path, monkeypatch, pinned, resume_flag,
):
    from harness.routing import RoutingRuleSet
    sid = asyncio.run(seed_session(tmp_path, catalog_path, pinned=pinned))
    captured = []

    async def launch(kernel, **kwargs):
        captured.append(kernel)
        kernel.session.close()

    monkeypatch.chdir(tmp_path)
    # The saved preference wins even over a now-invalid startup default.
    monkeypatch.setattr("harness.routing.load_routing",
                        lambda **kwargs: RoutingRuleSet(default="missing"))
    monkeypatch.setattr("harness.tui.run_tui", launch)
    resume_args = [resume_flag] + ([str(sid)] if resume_flag == "--resume" else [])
    monkeypatch.setattr("sys.argv", ["harness", *resume_args, "--base-dir", str(tmp_path),
                                     "--catalog", str(catalog_path), "--no-mcp", "--no-plugins"])
    main()
    kernel = captured[0]
    assert kernel.session.id == sid
    assert kernel.loop.model == "alias-a"
    assert kernel.loop.model_pinned is pinned
    assert isinstance(kernel.provider, CatalogProvider)
    assert kernel.runner.default_model == "alias-a"


async def test_tui_resume_uses_target_choice_and_dispatches_with_it(tmp_path, catalog_path, monkeypatch):
    sid = await seed_session(tmp_path, catalog_path)
    calls = []

    async def infer(self, request):
        calls.append(request.model)
        yield TextDelta("selection restored")
        yield StreamStop("end_turn")

    monkeypatch.setattr(CatalogProvider, "infer", infer)
    app = make_app(tmp_path, catalog_path=catalog_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await app._switch_model("alias-b")
        await app._rebuild_kernel(resume_session_id=sid)
        composer = app.query_one("#prompt", Input)
        composer.value = "continue this project"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert calls == ["alias-a"]
        assert app.kernel.loop.model_pinned
        assert app.kernel.runner.default_model == "alias-a"
        assert "alias-a" in screen_text(app)
        assert "selection restored" in screen_text(app)
        assert [e.event.text for e in read_session(tmp_path, sid)
                if e.event.type == "user_message"] == ["continue this project"]


async def test_missing_target_alias_refuses_resume_before_teardown(tmp_path, catalog_path):
    sid = await seed_session(tmp_path, catalog_path)
    catalog_path.write_text('[models.alias-b]\nroute = "local/model-b"\n')
    app = make_app(tmp_path, catalog_path=catalog_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        current = app.kernel
        composer = app.query_one("#prompt", Input)
        composer.value = "/resume"
        await pilot.press("enter")
        await pilot.pause(0.2)
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.kernel is current and not current.session.closed
        assert "cannot resume" in screen_text(app)
        assert "alias-a" in screen_text(app)
        assert not any(e.event.type == "session_resumed" for e in read_session(tmp_path, sid))


@pytest.mark.parametrize("catalog_failure", ["missing_alias", "missing_catalog", "invalid_catalog"])
def test_cli_unavailable_selection_needs_explicit_override(
    tmp_path, catalog_path, monkeypatch, catalog_failure,
):
    sid = asyncio.run(seed_session(tmp_path, catalog_path))
    log = tmp_path / "sessions" / f"{sid}.jsonl"
    before = log.read_bytes()
    if catalog_failure == "missing_alias":
        catalog_path.write_text('[models.alias-b]\nroute = "local/model-b"\n')
    elif catalog_failure == "missing_catalog":
        catalog_path.unlink()
    else:
        catalog_path.write_text("invalid = [secret-catalog-value")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("harness.routing.load_routing", lambda **kwargs: None)
    argv = ["harness", "--continue", "--base-dir", str(tmp_path), "--catalog", str(catalog_path),
            "--no-mcp", "--no-plugins"]
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit, match="--model") as error:
        main()
    assert "secret-catalog-value" not in str(error.value)
    assert log.read_bytes() == before
    assert not log.with_suffix(".lock").exists()
    catalog_path.write_text('[models.alias-b]\nroute = "local/model-b"\n')
    captured = []

    async def launch(kernel, **kwargs):
        captured.append(kernel.loop.model)
        assert kernel.loop.model_pinned
        kernel.session.close()

    monkeypatch.setattr("harness.tui.run_tui", launch)
    monkeypatch.setattr("sys.argv", [*argv, "--model", "alias-b"])
    main()
    assert captured == ["alias-b"]
    # A later ordinary resume must remember the override too.
    monkeypatch.setattr("sys.argv", argv)
    main()
    assert captured == ["alias-b", "alias-b"]


async def test_switch_without_turn_and_clear_keep_durable_selection(tmp_path, catalog_path):
    app = make_app(tmp_path, catalog_path=catalog_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._switch_model("alias-b")
        sid = app.kernel.session.id
        state = fold(read_session(tmp_path, sid))
        assert state.model_selection == ModelSelected(model=ModelId("alias-b"), pinned=True)
        assert state.messages == []
        await app._switch_model("missing")
        assert fold(read_session(tmp_path, sid)).model_selection == state.model_selection
        await app._rebuild_kernel()
        assert app.kernel.session.id != sid
        assert fold(read_session(tmp_path, app.kernel.session.id)).model_selection == state.model_selection


async def test_failed_selection_write_leaves_live_dispatch_unchanged(tmp_path, catalog_path, monkeypatch):
    app = make_app(tmp_path, catalog_path=catalog_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._switch_model("alias-a")
        old_provider = app.kernel.provider
        old_catalog = old_provider.catalog
        real_append = app.kernel.session.append

        def fail_selection(event):
            if isinstance(event, ModelSelected):
                raise OSError("disk full")
            return real_append(event)

        monkeypatch.setattr(app.kernel.session, "append", fail_selection)
        with pytest.raises(OSError, match="disk full"):
            await app._switch_model("alias-b")
        assert app.kernel.loop.model == app.kernel.runner.default_model == "alias-a"
        assert app.kernel.provider is old_provider and old_provider.catalog is old_catalog
        assert fold(read_session(tmp_path, app.kernel.session.id)).model_selection.model == "alias-a"


@pytest.mark.parametrize("pinned", [True, False])
async def test_resume_keeps_pin_routing_and_current_pricing(tmp_path, catalog_path, monkeypatch, pinned):
    from harness.messages import Message, TextBlock
    from harness.resume import append_events
    from harness.routing import RoutingRule, RoutingRuleSet
    sid = await seed_session(tmp_path, catalog_path, pinned=pinned)
    # Completed routed/fallback calls and internal assessments are observations,
    # not preferences. None may change the alias or pin restored on resume.
    append_events(tmp_path, sid, [ModelCallCompleted(call_id=CallId("past"),
        model=ModelId("alias-b"), message=Message(role="assistant", blocks=(TextBlock(text="past"),)).model_dump(), usage={}),
        ModelCallCompleted(call_id=CallId("internal"), model=ModelId("missing"),
            message=Message(role="assistant", blocks=(TextBlock(text="assessment"),)).model_dump(), usage={}, purpose="semantic")])
    catalog_path.write_text(catalog_path.read_text().replace("local/model-a", "local/new-a").replace(
        "input_cost_per_token = 0.0", "input_cost_per_token = 0.001", 1).replace(
        "output_cost_per_token = 0.0", "output_cost_per_token = 0.002", 1))
    calls = []

    async def infer(self, request):
        calls.append(request.model)
        yield TextDelta("routed")
        yield StreamStop("end_turn")

    monkeypatch.setattr(CatalogProvider, "infer", infer)
    kernel = build_kernel(base_dir=tmp_path, provider=CatalogProvider(Catalog.load(catalog_path)),
        model=ModelId("alias-b"), model_pinned=True, resume_session_id=sid,
        inherit_model_selection=True, catalog_path=catalog_path,
        routing_rules=RoutingRuleSet(rules=[RoutingRule(target="alias-b", prompt_contains="route")]))
    try:
        assert kernel.loop.model == kernel.runner.default_model == "alias-a"
        assert kernel.loop.model_pinned is pinned
        assert kernel.provider.catalog.resolve("alias-a").route == "local/new-a"
        assert kernel.loop.pricing == kernel.runner.pricing == {
            "input_cost_per_token": 0.001, "output_cost_per_token": 0.002}
        await kernel.loop.run_turn("route this")
        assert calls == ["alias-a" if pinned else "alias-b"]
        assert fold(read_session(tmp_path, sid)).model_selection == ModelSelected(
            model=ModelId("alias-a"), pinned=pinned)
    finally:
        kernel.session.close()


async def test_resume_revalidates_current_selection_under_lock_and_releases_on_failure(tmp_path, catalog_path):
    from harness.model_selection import ModelSelectionError, read_model_selection
    from harness.resume import append_events
    sid = await seed_session(tmp_path, catalog_path)
    assert read_model_selection(tmp_path, sid).model == "alias-a"
    # A separate process finishes an update after a successful preflight.
    append_events(tmp_path, sid, [ModelSelected(model=ModelId("missing"), pinned=True)])
    log = tmp_path / "sessions" / f"{sid}.jsonl"
    before = log.read_bytes()
    with pytest.raises(ModelSelectionError, match="missing"):
        build_kernel(base_dir=tmp_path, provider=CatalogProvider(Catalog.load(catalog_path)),
            model=ModelId("alias-a"), resume_session_id=sid,
            inherit_model_selection=True, catalog_path=catalog_path)
    assert log.read_bytes() == before
    assert not log.with_suffix(".lock").exists()


async def test_selection_preflight_repairs_torn_tail_but_refuses_live_writer(tmp_path, catalog_path):
    from harness.log import SessionLockedError
    from harness.model_selection import read_model_selection
    sid = await seed_session(tmp_path, catalog_path)
    log = tmp_path / "sessions" / f"{sid}.jsonl"
    with log.open("ab") as output:
        output.write(b'{"uncommitted":')
    assert read_model_selection(tmp_path, sid).model == "alias-a"
    assert log.with_suffix(".torn").read_bytes() == b'{"uncommitted":'
    kernel = build_kernel(base_dir=tmp_path, provider=CatalogProvider(Catalog.load(catalog_path)),
        model=ModelId("alias-a"), resume_session_id=sid)
    try:
        with pytest.raises(SessionLockedError):
            read_model_selection(tmp_path, sid)
    finally:
        kernel.session.close()
