import json
import sys

from harness.cli import Kernel, build_kernel, main, run_once
from harness.mcp_config import McpServerSpec, load_mcp_file
from harness.provider import FakeProvider, text_turn
from harness.types import ModelId
from tests.conftest import FIXTURE_SERVER_PATH


async def test_build_kernel_wires_dispatch_agent_tool(tmp_path):
    kernel = build_kernel(
        provider=FakeProvider([text_turn("ok")]),
        base_dir=tmp_path, model=ModelId("fake"),
    )
    assert isinstance(kernel, Kernel)
    assert "dispatch_agent" in [s.name for s in kernel.registry.specs()]


async def test_run_once_returns_final_text_and_closes_session(tmp_path):
    kernel = build_kernel(
        provider=FakeProvider([text_turn("the answer")]),
        base_dir=tmp_path, model=ModelId("fake"),
    )
    assert await run_once(kernel, "question?") == "the answer"
    # session closed: log readable, ends with SessionEnded
    # without MCP; an MCP-active session appends mcp/server_stopped after session_ended
    from harness.log import read_session
    envs = read_session(tmp_path, kernel.session.id)
    assert envs[-1].event.type == "session_ended"


async def test_build_kernel_with_catalog_pricing(tmp_path):
    catalog_toml = tmp_path / "models.toml"
    catalog_toml.write_text(
        "[models.fake]\nroute = \"fake:echo\"\n"
        "input_cost_per_token = 1e-6\noutput_cost_per_token = 2e-6\nverified = true\n"
    )
    from harness.catalog import Catalog
    from harness.events import ModelCallCompleted
    from harness.log import read_session

    resolved = Catalog.load(catalog_toml).resolve("fake")
    kernel = build_kernel(
        provider=FakeProvider([text_turn("ok")]), base_dir=tmp_path,
        model=resolved.route, pricing=resolved.pricing_dict(),
    )
    sid = kernel.session.id
    await run_once(kernel, "hi")
    envs = read_session(tmp_path, sid)
    completed = [e.event for e in envs if isinstance(e.event, ModelCallCompleted)]
    assert completed[0].pricing["input_cost_per_token"] == 1e-6


async def test_resume_flag_continues_session(tmp_path):
    from harness.log import read_session

    kernel = build_kernel(
        provider=FakeProvider([text_turn("first answer")]), base_dir=tmp_path, model=ModelId("fake")
    )
    sid = kernel.session.id
    await run_once(kernel, "first question")

    resumed = build_kernel(
        provider=FakeProvider([text_turn("second answer")]), base_dir=tmp_path,
        model=ModelId("fake"), resume_session_id=sid,
    )
    assert resumed.session.id == sid
    assert len(resumed.loop.history) >= 2  # prior user + assistant turns seeded
    result = await resumed.loop.run_turn("second question")
    await resumed.loop.end()
    resumed.session.close()
    assert result == "second answer"
    envs = read_session(tmp_path, sid)
    seqs = [e.seq for e in envs]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


async def test_permission_engine_denies_through_kernel(tmp_path):
    from harness.events import HookDecided
    from harness.log import read_session
    from harness.permissions import PermissionEngine, PermissionRule, RuleSet
    from harness.provider import tool_call_turn
    from harness.types import ToolName

    engine = PermissionEngine([RuleSet(
        rules=[PermissionRule(action="deny", tool="dispatch_agent")], default="allow"
    )])
    kernel = build_kernel(
        provider=FakeProvider([
            tool_call_turn("delegating", ToolName("dispatch_agent"), {"prompt": "x"}),
            text_turn("gave up"),
        ]),
        base_dir=tmp_path, model=ModelId("fake"), permissions=engine,
    )
    sid = kernel.session.id
    result = await run_once(kernel, "delegate something")
    assert result == "gave up"
    events = [e.event for e in read_session(tmp_path, sid)]
    denied = [e for e in events if isinstance(e, HookDecided) and e.hook == "permissions"
              and e.decision["kind"] == "block"]
    assert denied  # the engine deny was recorded like any hook decision


async def test_permission_ask_resolves_and_grant_silences(tmp_path):
    from harness.events import PermissionRequested
    from harness.interaction import ScriptedResolver
    from harness.log import read_session
    from harness.permissions import PermissionEngine, PermissionRule, RuleSet
    from harness.provider import tool_call_turn
    from harness.types import ToolName

    engine = PermissionEngine([RuleSet(
        rules=[PermissionRule(action="ask", tool="dispatch_agent")], default="allow"
    )])
    class GrantingResolver(ScriptedResolver):
        """Approve once, then grant.
        One scripted answer: a second ask would IndexError,
        so asks2==1 is structurally enforced."""

        def __init__(self, engine):
            super().__init__([True])
            self._engine = engine

        async def resolve(self, request):
            answer = await super().resolve(request)
            if answer:
                self._engine.grant("dispatch_agent")
            return answer

    kernel = build_kernel(
        provider=FakeProvider([
            tool_call_turn("first", ToolName("dispatch_agent"), {"prompt": "a"}),
            text_turn("child one done"),
            tool_call_turn("second", ToolName("dispatch_agent"), {"prompt": "b"}),
            text_turn("child two done"),
            text_turn("all delegated"),
        ]),
        base_dir=tmp_path, model=ModelId("fake"), permissions=engine,
        resolver=GrantingResolver(engine),
    )
    sid = kernel.session.id
    result = await run_once(kernel, "delegate twice")
    assert result == "all delegated"
    events = [e.event for e in read_session(tmp_path, sid)]
    asks = [e for e in events if isinstance(e, PermissionRequested)]
    assert len(asks) == 1  # second call sailed through on the grant


def test_main_missing_catalog_is_actionable(tmp_path, capsys, monkeypatch):
    import pytest
    monkeypatch.setattr(
        "sys.argv",
        ["harness", "-p", "x", "--model", "gpt",
         "--catalog", str(tmp_path / "nope.toml"), "--base-dir", str(tmp_path)],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert "catalog not found" in str(exc.value)
    assert "--catalog" in str(exc.value)


def test_allow_flags_become_session_grants(tmp_path):
    from harness.permissions import PermissionEngine, PermissionRule, RuleSet
    from harness.cli import _apply_allow_flags
    engine = PermissionEngine([RuleSet(rules=[PermissionRule(action="ask", tool="*")])])
    _apply_allow_flags(engine, ["bash", "read_*"])
    assert engine.decide("bash", {}) == "allow"
    assert engine.decide("read_file", {}) == "allow"
    assert engine.decide("write_file", {}) == "ask"


def test_allow_without_config_warns(tmp_path, capsys, monkeypatch):
    import harness.cli as cli_mod
    monkeypatch.setattr(cli_mod, "default_engine", lambda project_dir=None: None)
    monkeypatch.setattr(
        "sys.argv",
        ["harness", "-p", "hi", "--allow", "bash", "--base-dir", str(tmp_path)],
    )
    cli_mod.main()
    captured = capsys.readouterr()
    assert "echo: hi" in captured.out
    assert "no permission config found" in captured.err


async def test_run_with_tags_lands_in_telemetry(tmp_path):
    from harness.telemetry import rebuild_index
    kernel = build_kernel(
        provider=FakeProvider([text_turn("ok")]), base_dir=tmp_path, model=ModelId("fake"),
        tags=["exp:cli"],
    )
    sid = kernel.session.id
    await run_once(kernel, "hi")
    conn, warnings = rebuild_index(tmp_path)
    assert warnings == []
    tags = conn.execute("SELECT session_id, tag FROM tags").fetchall()
    assert tags == [(str(sid), "exp:cli")]


def test_stats_subcommand_prints_summary(tmp_path, capsys, monkeypatch):
    import harness.cli as cli_mod
    # seed one session via the legacy run path
    monkeypatch.setattr(cli_mod, "default_engine", lambda project_dir=None: None)
    monkeypatch.setattr(
        "sys.argv", ["harness", "-p", "hello", "--base-dir", str(tmp_path)]
    )
    cli_mod.main()
    capsys.readouterr()
    monkeypatch.setattr("sys.argv", ["harness", "stats", "--base-dir", str(tmp_path)])
    cli_mod.main()
    out = capsys.readouterr().out
    assert "sessions: 1" in out and "fake:echo" in out


def test_outcome_then_compare_subcommands(tmp_path, capsys, monkeypatch):
    import harness.cli as cli_mod
    monkeypatch.setattr(cli_mod, "default_engine", lambda project_dir=None: None)
    sids = []
    for prompt in ("one", "two"):
        monkeypatch.setattr(
            "sys.argv", ["harness", "-p", prompt, "--base-dir", str(tmp_path)]
        )
        cli_mod.main()
    capsys.readouterr()
    sids = sorted(p.stem for p in (tmp_path / "sessions").glob("*.jsonl"))
    monkeypatch.setattr(
        "sys.argv",
        ["harness", "outcome", sids[0], "ok", "--score", "0.9", "--base-dir", str(tmp_path)],
    )
    cli_mod.main()
    out = capsys.readouterr().out
    assert "recorded" in out and sids[0][:8] in out
    monkeypatch.setattr(
        "sys.argv", ["harness", "compare", sids[0], sids[1], "--base-dir", str(tmp_path)]
    )
    cli_mod.main()
    out = capsys.readouterr().out
    assert "outcome" in out and "ok" in out and "input_tokens" in out


def test_outcome_on_live_session_exits_cleanly(tmp_path, capsys, monkeypatch):
    import pytest
    import harness.cli as cli_mod
    from harness.session import Session
    from harness.types import SessionId

    live = Session(tmp_path, SessionId("running"))
    live.start()
    try:
        monkeypatch.setattr(
            "sys.argv",
            ["harness", "outcome", "running", "ok", "--base-dir", str(tmp_path)],
        )
        with pytest.raises(SystemExit) as exc:
            cli_mod.main()
        assert "still running" in str(exc.value)
    finally:
        live.close()


def fixture_stdio_spec(**overrides) -> McpServerSpec:
    defaults = dict(
        name="fixture", transport="stdio",
        command=sys.executable, args=(str(FIXTURE_SERVER_PATH),),
    )
    defaults.update(overrides)
    return McpServerSpec(**defaults)


async def test_kernel_with_mcp_runs_tool_and_injects_instructions(tmp_path):
    from harness.log import read_session
    from harness.provider import tool_call_turn
    from harness.types import ToolName

    provider = FakeProvider([
        tool_call_turn("calling add", ToolName("mcp__fixture__add"), {"a": 19, "b": 23}),
        text_turn("done"),
    ])
    kernel = build_kernel(
        provider=provider, base_dir=tmp_path, model=ModelId("fake:echo"),
        mcp=[fixture_stdio_spec()],
    )
    reply = await run_once(kernel, "add the numbers")
    assert reply == "done"
    assert "Fixture server: use `add` for arithmetic." in kernel.loop.system_prompt
    envelopes = read_session(tmp_path, kernel.session.id)
    customs = [e.event for e in envelopes if getattr(e.event, "type", "") == "custom"]
    assert any(c.namespace == "mcp" and c.name == "server_started" for c in customs)
    assert any(c.namespace == "mcp" and c.name == "server_stopped" for c in customs)
    completed = [e.event for e in envelopes if getattr(e.event, "type", "") == "tool_call_completed"]
    assert any(e.result_text == "42" for e in completed)


async def test_mcp_server_stopped_lands_after_session_ended(tmp_path):
    # teardown is post-session: the tail is (session_ended, mcp/server_stopped).
    # server_stopped must NOT precede session_ended.
    from harness.log import read_session

    provider = FakeProvider([text_turn("ok")])
    kernel = build_kernel(
        provider=provider, base_dir=tmp_path, model=ModelId("fake:echo"),
        mcp=[fixture_stdio_spec()],
    )
    await run_once(kernel, "hi")
    envelopes = read_session(tmp_path, kernel.session.id)
    tail = envelopes[-2:]
    assert [e.event.type for e in tail] == ["session_ended", "custom"]
    last = tail[-1].event
    assert last.namespace == "mcp" and last.name == "server_stopped"


async def test_mcp_events_never_precede_session_started(tmp_path):
    from harness.log import read_session

    provider = FakeProvider([text_turn("ok")])
    kernel = build_kernel(
        provider=provider, base_dir=tmp_path, model=ModelId("fake:echo"),
        mcp=[fixture_stdio_spec()],
    )
    await run_once(kernel, "hi")
    envelopes = read_session(tmp_path, kernel.session.id)
    assert envelopes[0].event.type == "session_started"


async def test_kernel_without_mcp_unchanged(tmp_path):
    provider = FakeProvider([text_turn("plain")])
    kernel = build_kernel(provider=provider, base_dir=tmp_path, model=ModelId("fake:echo"))
    assert kernel.mcp is None
    assert await run_once(kernel, "hi") == "plain"


def test_mcp_config_flag_bad_path_exits_cleanly(tmp_path, capsys, monkeypatch):
    import pytest
    import harness.cli as cli_mod
    monkeypatch.setattr(
        "sys.argv",
        ["harness", "-p", "hi", "--mcp-config", str(tmp_path / "nope.toml"),
         "--base-dir", str(tmp_path)],
    )
    with pytest.raises(SystemExit):
        cli_mod.main()


def test_no_mcp_flag_skips_mcp(tmp_path, capsys, monkeypatch):
    import harness.cli as cli_mod
    monkeypatch.setattr(cli_mod, "default_engine", lambda project_dir=None: None)
    monkeypatch.setattr(
        "sys.argv",
        ["harness", "-p", "hi", "--no-mcp", "--base-dir", str(tmp_path)],
    )
    cli_mod.main()
    captured = capsys.readouterr()
    assert "echo: hi" in captured.out




def run_cli(*args):
    """Invoke the harness CLI with the given args, patching sys.argv.
    Propagates SystemExit (e.g. from argparse or validation errors).
    """
    import sys
    old_argv = sys.argv
    sys.argv = ["harness"] + list(args)
    try:
        main()
    finally:
        sys.argv = old_argv


def test_mcp_add_list_remove_roundtrip(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.chdir(tmp_path)
    run_cli("mcp", "add", "github", "--command", "npx", "--arg=-y", "--arg=pkg",
        "--env", "TOKEN=GH_PAT", "--scope", "user", "--config-home", str(home))
    run_cli("mcp", "list", "--config-home", str(home))
    out = capsys.readouterr().out
    assert "github" in out and "stdio" in out and "user" in out
    run_cli("mcp", "remove", "github", "--scope", "user", "--config-home", str(home))
    capsys.readouterr() # clear remove output
    run_cli("mcp", "list", "--config-home", str(home))
    assert "github" not in capsys.readouterr().out


def test_mcp_add_http_with_header(tmp_path, capsys):
    home = tmp_path / "home"
    run_cli("mcp", "add", "remote", "--url", "https://x/mcp",
        "--header", "Authorization=X_AUTH", "--config-home", str(home))
    loaded = load_mcp_file(home / "mcp.toml", source="user")
    assert loaded[0].headers == {"Authorization": "X_AUTH"}


def test_mcp_add_rejects_literal_looking_env(tmp_path):
    home = tmp_path / "home"
    import pytest
    with pytest.raises(SystemExit):
        run_cli("mcp", "add", "bad", "--command", "x",
            "--env", "TOKEN=ghp_abc123literal-token", "--config-home", str(home))


def test_mcp_add_rejects_malformed_env_pair(tmp_path):
    home = tmp_path / "home"
    import pytest
    with pytest.raises(SystemExit) as exc:
        run_cli("mcp", "add", "bad", "--command", "x",
            "--env", "NOEQUALS", "--config-home", str(home))
    assert "NAME=ENV_VAR_NAME" in str(exc.value)


def test_mcp_import_writes_scope_file_and_warns(tmp_path, capsys):
    home = tmp_path / "home"
    sample = tmp_path / ".mcp.json"
    sample.write_text(json.dumps({"mcpServers": {
        "clean": {"command": "/bin/clean"},
        "leaky": {"command": "x", "env": {"TOKEN": "ghp_literal123"}},
    }}))
    run_cli("mcp", "import", str(sample), "--write", "--config-home", str(home))
    captured = capsys.readouterr()
    assert "leaky" in captured.err and "literal" in captured.err
    loaded = load_mcp_file(home / "mcp.toml", source="user")
    assert [s.name for s in loaded] == ["clean"]
    assert "imported 1 server(s)" in captured.out
