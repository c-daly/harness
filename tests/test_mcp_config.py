"""mcp.toml parsing: validation, layering (project shadows user), env references."""

from dataclasses import FrozenInstanceError

import pytest

from harness.mcp_config import (
    MANAGED_HEADER,
    McpConfigError,
    McpServerSpec,
    emit_mcp_toml,
    load_mcp_config,
    load_mcp_file,
    resolve_env,
    write_scope_file,
)

STDIO_TOML = """
[servers.github]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
[servers.github.env]
GITHUB_TOKEN = "GITHUB_PAT"
"""

HTTP_TOML = """
[servers.remote]
url = "https://api.example.com/mcp"
[servers.remote.headers]
Authorization = "MYAPI_AUTH"
"""


def test_stdio_spec_parses_with_inferred_transport(tmp_path):
    path = tmp_path / "mcp.toml"
    path.write_text(STDIO_TOML)
    (spec,) = load_mcp_file(path, source="user")
    assert spec.name == "github"
    assert spec.transport == "stdio"
    assert spec.command == "npx"
    assert spec.args == ("-y", "@modelcontextprotocol/server-github")
    assert spec.env == {"GITHUB_TOKEN": "GITHUB_PAT"}
    assert spec.restart == "on_failure"
    assert spec.tool_timeout_s == 60.0
    assert spec.source == "user"


def test_http_spec_parses_with_inferred_transport(tmp_path):
    path = tmp_path / "mcp.toml"
    path.write_text(HTTP_TOML)
    (spec,) = load_mcp_file(path, source="project")
    assert spec.transport == "http"
    assert spec.url == "https://api.example.com/mcp"
    assert spec.headers == {"Authorization": "MYAPI_AUTH"}


@pytest.mark.parametrize(
    "toml_body,fragment",
    [
        ('[servers."bad__name"]\ncommand = "x"\n', "__"),
        ('[servers."bad name"]\ncommand = "x"\n', "name"),
        ("[servers.s]\ncommand = \"x\"\nurl = \"http://y\"\n", "both"),
        ("[servers.s]\nrestart = \"never\"\n", "command or url"),
        ('[servers.s]\ncommand = "x"\nrestart = "sometimes"\n', "restart"),
        ('[servers.s]\ncommand = "x"\ntool_timeout_s = 0\n', "tool_timeout_s"),
        ('[servers.s]\ncommand = "x"\ntransport = "http"\n', "transport"),
        ('[servers.s]\ncommand = "x"\n[servers.s.env]\nK = "not a var!"\n', "environment variable"),
        ('[servers.s]\nurl = "http://y"\n[servers.s.headers]\nA = "Bearer xyz"\n',
         "environment variable"),
        ('[servers.s]\ncommand = "x"\nargs = "not-a-list"\n', "args"),
        ('[servers.s]\ncommand = "x"\nargs = [1, 2]\n', "args"),
        ('[servers.s]\ncommand = 5\n', "command"),
        ('[servers.s]\nurl = 42\n', "url"),
        ('[servers.s]\ncommand = "x"\ncwd = 99\n', "cwd"),
        ('[servers]\ns = "not a dict"\n', "table"),
        ('[servers.s]\ncommand = "x"\nrestrat = "never"\n', "unknown keys"),
    ],
)
def test_validation_errors(tmp_path, toml_body, fragment):
    path = tmp_path / "mcp.toml"
    path.write_text(toml_body)
    with pytest.raises(McpConfigError) as exc:
        load_mcp_file(path, source="user")
    assert fragment in str(exc.value)


def test_toml_decode_error_wraps(tmp_path):
    path = tmp_path / "mcp.toml"
    path.write_text("[servers.s\ncommand = ")
    with pytest.raises(McpConfigError) as exc:
        load_mcp_file(path, source="user")
    assert "mcp.toml" in str(exc.value)


def test_project_shadows_user(tmp_path):
    home = tmp_path / "confighome"
    home.mkdir()
    (home / "mcp.toml").write_text(STDIO_TOML + HTTP_TOML)
    project = tmp_path / "proj"
    (project / ".harness").mkdir(parents=True)
    (project / ".harness" / "mcp.toml").write_text(
        '[servers.github]\ncommand = "project-version"\n'
    )
    specs = load_mcp_config(project_dir=project, config_home=home)
    by_name = {s.name: s for s in specs}
    assert by_name["github"].command == "project-version"
    assert by_name["github"].source == "project"
    assert by_name["remote"].source == "user"


def test_no_config_files_means_no_servers(tmp_path):
    assert load_mcp_config(project_dir=tmp_path / "nope", config_home=tmp_path / "alsono") == ()


def test_resolve_env_reads_named_variables(monkeypatch):
    monkeypatch.setenv("GITHUB_PAT", "tok-123")
    assert resolve_env({"GITHUB_TOKEN": "GITHUB_PAT"}) == {"GITHUB_TOKEN": "tok-123"}


def test_resolve_env_missing_variable_is_loud(monkeypatch):
    monkeypatch.delenv("NOPE_VAR", raising=False)
    with pytest.raises(McpConfigError) as exc:
        resolve_env({"K": "NOPE_VAR"})
    assert "NOPE_VAR" in str(exc.value)


def test_resolve_env_names_all_missing_sorted(monkeypatch):
    monkeypatch.delenv("ZZ_VAR", raising=False)
    monkeypatch.delenv("AA_VAR", raising=False)
    with pytest.raises(McpConfigError) as exc:
        resolve_env({"K1": "ZZ_VAR", "K2": "AA_VAR"})
    assert "AA_VAR, ZZ_VAR" in str(exc.value)


def test_spec_is_frozen():
    spec = McpServerSpec(name="s", transport="stdio", command="x")
    with pytest.raises(FrozenInstanceError):
        spec.name = "other"  # type: ignore[misc]



def test_emit_round_trips_through_loader(tmp_path):
    specs = (
        McpServerSpec(name="github", transport="stdio", command="npx",
                      args=("-y", "pkg"), env={"TOKEN": "GH_PAT"}),
        McpServerSpec(name="remote", transport="http", url="https://x/mcp",
                      headers={"Authorization": "X_AUTH"}, restart="never",
                      tool_timeout_s=30.0),
    )
    text = emit_mcp_toml(specs)
    assert text.startswith(MANAGED_HEADER)
    path = tmp_path / "mcp.toml"
    path.write_text(text)
    loaded = load_mcp_file(path, source="user")
    assert {s.name for s in loaded} == {"github", "remote"}
    github = next(s for s in loaded if s.name == "github")
    assert github.args == ("-y", "pkg")
    assert github.env == {"TOKEN": "GH_PAT"}
    remote = next(s for s in loaded if s.name == "remote")
    assert remote.restart == "never" and remote.tool_timeout_s == 30.0


def test_emit_escapes_hostile_strings(tmp_path):
    dq = chr(34)
    spec = McpServerSpec(
        name="evil", transport="stdio", command="x"+dq+" ]]\n[[rules", args=("a"+dq+"b", "new\nline"),
    )
    path = tmp_path / "mcp.toml"
    path.write_text(emit_mcp_toml((spec,)))
    (loaded,) = load_mcp_file(path, source="user")
    assert loaded.command == "x"+dq+" ]]\n[[rules"
    assert loaded.args == ("a"+dq+"b", "new\nline")


def test_write_scope_file_refuses_unmanaged(tmp_path):
    path = tmp_path / "mcp.toml"
    dq = chr(34)
    toml_content = "# hand-written\n[servers.mine]\n"+dq+"command"+dq+" = "+dq+"x"+dq+"\n"
    path.write_text(toml_content)
    with pytest.raises(McpConfigError) as exc:
        write_scope_file(path, (McpServerSpec(name="s", transport="stdio", command="y"),))
    assert "managed" in str(exc.value)


def test_write_scope_file_creates_parents(tmp_path):
    path = tmp_path / "deep" / "mcp.toml"
    write_scope_file(path, (McpServerSpec(name="s", transport="stdio", command="y"),))
    (loaded,) = load_mcp_file(path, source="user")
    assert loaded.name == "s"
