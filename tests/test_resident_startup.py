"""Startup must enter the resident, preserve model intent, and never fake a reply."""

from pathlib import Path

import pytest

from harness.cli import main
from harness.provider import FakeProvider, text_turn
from harness.resident import RESIDENT_SYSTEM_PROMPT, ResidentConfig, UnconfiguredResidentProvider


@pytest.fixture
def startup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config = home / ".config" / "harness"
    config.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)
    catalog = config / "models.toml"
    catalog.write_text('[models.local]\nroute = "openai/local"\n'
                       '[models.alternate]\nroute = "openai/alternate"\n')
    captured = {}

    async def tui(kernel, **kwargs):
        captured["kernel"] = kernel
        kernel.session.close()

    monkeypatch.setattr("harness.tui.run_tui", tui)

    def launch(*args):
        monkeypatch.setattr("sys.argv", ["harness", "--base-dir", str(tmp_path / "state"),
                                        "--no-mcp", "--no-plugins", *args])
        main()
        return captured.get("kernel")

    return config, launch


def test_fresh_start_is_resident_with_visible_missing_model(startup):
    _, launch = startup
    kernel = launch()
    assert isinstance(kernel.provider, UnconfiguredResidentProvider)
    assert kernel.loop.model == "unconfigured"
    assert kernel.loop.system_prompt == RESIDENT_SYSTEM_PROMPT


def test_unconfigured_headless_does_not_claim_success(startup, capsys):
    _, launch = startup
    with pytest.raises(SystemExit, match="Saoirse has no model configured"):
        launch("-p", "hello")
    assert "echo:" not in capsys.readouterr().out


@pytest.mark.parametrize("override,expected,pinned", [
    ((), "local", False), (("--model", "alternate"), "alternate", True),
])
def test_configured_resident_model_and_explicit_override(startup, override, expected, pinned):
    config, launch = startup
    (config / "resident.toml").write_text('model = "local"\n')
    kernel = launch(*override)
    assert kernel.loop.model == expected
    assert kernel.loop.model_pinned is pinned
    assert kernel.provider.catalog.resolve(expected).route == f"openai/{expected}"
    assert kernel.loop.system_prompt == RESIDENT_SYSTEM_PROMPT


def test_routing_default_retains_precedence(startup):
    config, launch = startup
    (config / "resident.toml").write_text('model = "local"\n')
    (config / "routing.toml").write_text('default = "alternate"\n')
    kernel = launch()
    assert kernel.loop.model == "alternate"
    assert not kernel.loop.model_pinned


def test_headless_resident_uses_selected_provider(startup, monkeypatch, capsys):
    config, launch = startup
    (config / "resident.toml").write_text('model = "local"\n')
    provider = FakeProvider([text_turn("provider reply")])
    monkeypatch.setattr("harness.cli._catalog_provider", lambda catalog: provider)
    launch("-p", "hello")
    assert capsys.readouterr().out.strip() == "provider reply"


def test_default_context_profile_and_explicit_clear(startup):
    config, launch = startup
    (config / "resident.toml").write_text('model = "local"\ncontext_profile = "context.toml"\n')
    (config / "context.toml").write_text('history_turns = 12\nmax_input_bytes = 65536\n')
    assert launch().context_policy.history_turns == 12
    assert launch("--no-context-profile").context_policy is None


def test_bad_default_alias_does_not_fall_back_to_echo(startup):
    config, launch = startup
    (config / "resident.toml").write_text('model = "missing"\n')
    with pytest.raises(SystemExit, match="unknown model alias 'missing'"):
        launch()


@pytest.fixture
def saved_resident(startup):
    from harness.events import ContextPolicyConfigured, ModelSelected, SessionEnded
    from harness.context import ContextPolicy
    from harness.session import Session
    from harness.types import ModelId, new_session_id

    config, launch = startup
    base = config.parents[2] / "state"
    with Session(base, new_session_id(), default_model=ModelId("alternate")) as session:
        session.start()
        session.append(ModelSelected(model=ModelId("alternate"), pinned=True))
        session.append(ContextPolicyConfigured(policy=ContextPolicy(history_turns=9)))
        session.append(SessionEnded())
        sid = session.id
    return config, launch, sid


def test_resume_preserves_model_and_context_over_new_resident_defaults(saved_resident):
    config, launch, sid = saved_resident
    (config / "resident.toml").write_text('model = "local"\ncontext_profile = "missing.toml"\n')
    kernel = launch("--resume", str(sid))
    assert kernel.loop.model == "alternate"
    assert kernel.loop.model_pinned
    assert kernel.context_policy.history_turns == 9


@pytest.mark.parametrize("resume_flag", ["--resume", "--continue"])
@pytest.mark.parametrize("content", ['model = [', 'model = 7'])
def test_resume_ignores_broken_implicit_defaults(saved_resident, resume_flag, content):
    config, launch, sid = saved_resident
    (config / "resident.toml").write_text(content)
    args = [resume_flag, str(sid)] if resume_flag == "--resume" else [resume_flag]
    kernel = launch(*args)
    assert kernel.session.id == sid
    assert kernel.loop.model == "alternate" and kernel.loop.model_pinned
    assert kernel.context_policy.history_turns == 9


@pytest.mark.parametrize("resume_flag", ["--resume", "--continue"])
@pytest.mark.parametrize("missing", [False, True])
def test_resume_still_validates_explicit_resident_config(saved_resident, resume_flag, missing):
    config, launch, sid = saved_resident
    path = config / "explicit.toml"
    if not missing:
        path.write_text('model = [')
    args = [resume_flag, str(sid)] if resume_flag == "--resume" else [resume_flag]
    with pytest.raises(SystemExit, match="resident config unavailable or invalid"):
        launch(*args, "--resident-config", str(path))


@pytest.mark.parametrize("args", [(), ("--model", "alternate")])
def test_fresh_session_still_validates_applicable_defaults(startup, args):
    config, launch = startup
    (config / "resident.toml").write_text('model = [')
    with pytest.raises(SystemExit, match="resident config unavailable or invalid"):
        launch(*args)


@pytest.mark.parametrize("routed", [False, True])
def test_fully_selected_startup_ignores_unused_implicit_defaults(startup, routed):
    config, launch = startup
    (config / "resident.toml").write_text('model = [')
    if routed:
        (config / "routing.toml").write_text('default = "alternate"\n')
    args = () if routed else ("--model", "alternate")
    kernel = launch(*args, "--no-context-profile")
    assert kernel.loop.model == "alternate"
    assert kernel.context_policy is None


@pytest.mark.parametrize("content", ['model = 7', 'unknown = "x"', 'model = " "'])
def test_invalid_resident_config_is_actionable(tmp_path, content):
    path = tmp_path / "resident.toml"
    path.write_text(content)
    with pytest.raises(ValueError):
        ResidentConfig.load(path)


def test_missing_explicit_config_is_not_ignored(tmp_path):
    with pytest.raises(FileNotFoundError):
        ResidentConfig.load(tmp_path / "missing.toml")


async def test_unconfigured_interface_shows_resident_and_allows_model_selection(tmp_path):
    from textual.widgets import RichLog
    from harness.cli import build_kernel
    from harness.tui import HarnessApp
    from harness.types import ModelId

    catalog = tmp_path / "models.toml"
    catalog.write_text('[models.local]\nroute = "openai/local"\n')
    kernel = build_kernel(provider=UnconfiguredResidentProvider(), base_dir=tmp_path,
                          model=ModelId("unconfigured"))
    app = HarnessApp(kernel, catalog_path=catalog)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            visible = "\n".join(str(line) for line in app.query_one(RichLog).lines)
            assert "Saoirse" in visible and "no model configured" in visible
            await app._switch_model("local")
            assert app.kernel.loop.model == "local"
            assert app.kernel.loop.system_prompt == RESIDENT_SYSTEM_PROMPT
            assert app.kernel.runner.provider is app.kernel.provider
    finally:
        kernel.session.close()
