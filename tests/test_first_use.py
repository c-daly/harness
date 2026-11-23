"""First-session setup, diagnosis, and recovery through the normal CLI."""

import json
import shlex
from pathlib import Path

import pytest

from harness import cli
from harness.catalog import Catalog


@pytest.fixture
def first_use(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)

    def run(*argv):
        monkeypatch.setattr("sys.argv", ["harness", *map(str, argv)])
        cli.main()

    return run, home / ".config/harness"


def test_offline_diagnosis_setup_and_preservation(first_use, capsys):
    run, config = first_use
    with pytest.raises(SystemExit) as ex:
        run("doctor", "--json")
    assert ex.value.code == 1 and not config.exists()
    assert "harness setup" in json.loads(capsys.readouterr().out)["checks"][0]["detail"]
    args = ("setup", "local", "desk", "--model", "𐐀-q4", "--url", "http://localhost:8000/v1")
    run(*args)
    path = config / "models.toml"
    original = path.read_bytes()
    resolved = Catalog.load(path).resolve("desk")
    assert resolved.route == "openai/𐐀-q4" and resolved.local.auto_start is False
    run(*args)
    assert original == path.read_bytes()
    (config / "resident.toml").write_text('# keep this comment\nmodel = "desk"\n')
    run("setup", "api", "strong", "--route", "provider/model", "--key-env", "HARNESS_TEST_API_KEY")
    assert (config / "resident.toml").read_text().startswith("# keep this comment")
    capsys.readouterr()
    run("doctor", "--json")
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and any("desk" in c["detail"] for c in report["checks"])


def test_launch_command_uses_the_new_connection_with_existing_defaults(first_use, monkeypatch, capsys):
    run, config = first_use
    launches = []

    async def inspect_session(kernel, **kwargs):
        launches.append((str(kernel.loop.model), kernel.loop.model_pinned))
        kernel.session.close()

    def launch(label):
        output = capsys.readouterr().out.splitlines()
        line, = [line.removeprefix(label) for line in output if line.startswith(label)]
        executable, *args = shlex.split(line)
        assert executable == "harness"
        run(*args)

    monkeypatch.setattr("harness.tui.run_tui", inspect_session)
    run("setup", "local", "desk", "--model", "model", "--url", "http://localhost:8000/v1")
    launch("Start a session: ")
    # An existing alias can contain shell punctuation even though new names are restricted.
    alias = "--other model; $HOME"
    with (config / "models.toml").open("a") as catalog:
        catalog.write(f'\n[models.{json.dumps(alias)}]\nroute = "openai/other"\n')
    run("setup", "existing", "--", alias)
    launch("Pin this model for one session: ")
    (config / "routing.toml").write_text(f"default = {json.dumps(alias)}\n")
    run("setup", "existing", "desk")
    launch("Pin this model for one session: ")
    assert launches == [("desk", False), (alias, True), ("desk", True)]


@pytest.mark.parametrize("initial_key", [None, "   "], ids=["unset", "blank"])
def test_doctor_key_presence_and_routing_precedence(first_use, monkeypatch, capsys, initial_key):
    run, config = first_use
    if initial_key is None:
        monkeypatch.delenv("HARNESS_TEST_API_KEY", raising=False)
    else:
        monkeypatch.setenv("HARNESS_TEST_API_KEY", initial_key)
    run("setup", "api", "remote", "--route", "provider/model", "--key-env", "HARNESS_TEST_API_KEY")
    run("setup", "local", "small", "--model", "model", "--url", "http://localhost:8000/v1")
    (config / "routing.toml").write_text('default = "small"\n')
    with pytest.raises(SystemExit):
        run("doctor", "--model", "remote")
    assert "environment variable" in capsys.readouterr().out
    monkeypatch.setenv("HARNESS_TEST_API_KEY", "secret-credential-in-test")
    run("doctor", "--model", "remote")
    inspected = capsys.readouterr().out
    assert "Environment variable set" in inspected and "secret-credential" not in inspected
    assert "secret-credential" not in (config / "models.toml").read_text()
    run("doctor")
    assert "'small' (routing default" in capsys.readouterr().out


def test_setup_recovers_after_cancelled_or_partial_setup(first_use, monkeypatch):
    run, config = first_use
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = iter(["local", "desk", "http://example.invalid", "model", "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    run("setup")
    assert not config.exists()
    config.mkdir(parents=True)
    (config / "models.toml").write_text('[models.desk]\nroute = "openai/local"\n')
    answers = iter(["existing", "absent", "y", "existing", "desk"])
    run("setup")
    assert (config / "resident.toml").read_text() == 'model = "desk"\n'


def test_invalid_routing_default_has_recoverable_diagnostic(first_use, capsys):
    run, config = first_use
    run("setup", "local", "desk", "--model", "model", "--url", "http://localhost:8000/v1")
    capsys.readouterr()
    (config / "routing.toml").write_text('default = ["desk"]\n')
    with pytest.raises(SystemExit) as exc:
        run("doctor", "--json")
    assert exc.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert any(c["check"] == "routing" and not c["ok"] for c in report["checks"])
    with pytest.raises(SystemExit, match="default must be a nonempty catalog alias"):
        run("-p", "Hello")


def test_setup_leaves_configuration_intact_when_routing_is_invalid(first_use):
    run, config = first_use
    config.mkdir(parents=True)
    (config / "routing.toml").write_text('default = ["public"]\n')
    before = {f.name: f.read_bytes() for f in config.iterdir()}
    with pytest.raises(SystemExit, match="routing"):
        run("setup", "local", "desk", "--model", "model", "--url", "http://localhost:8000/v1")
    assert {f.name: f.read_bytes() for f in config.iterdir()} == before


def test_subscription_setup_and_diagnosis_agree_on_model_identifiers(first_use, monkeypatch, capsys):
    run, config = first_use
    monkeypatch.setattr("shutil.which", lambda name: "/bin/claude" if name == "claude" else None)
    for alias, model in (("extended", "sonnet[1m]"), ("long", "a" * 181)):
        run("setup", "cli", alias, "--model", model)
        capsys.readouterr()
        run("doctor", "--model", alias, "--json")
        assert json.loads(capsys.readouterr().out)["ok"]
        assert Catalog.load(config / "models.toml").resolve(alias).route == f"claude-cli/{model}"
    before = (config / "models.toml").read_bytes()
    with pytest.raises(SystemExit):
        run("setup", "cli", "invalid", "--model", "a" * 182)
    assert (config / "models.toml").read_bytes() == before


def test_bad_catalog_recoverable_without_traceback(first_use):
    run, config = first_use
    config.mkdir(parents=True)
    path = config / "models.toml"
    path.write_text('[models.demo]\napi_key_env = "SENSITIVE"\n')
    with pytest.raises(SystemExit) as exc:
        run("--model", "demo", "-p", "Inspect the project")
    assert str(path) in str(exc.value) and "route" in str(exc.value)
    assert "SENSITIVE" not in str(exc.value)
    with pytest.raises(SystemExit):
        run("setup", "existing", "demo")
    assert not (config / "resident.toml").exists()


def test_initial_agent_setup_requires_binary_and_preserves_existing_route(first_use, monkeypatch, capsys):
    run, config = first_use
    monkeypatch.setattr("shutil.which", lambda exe: None)
    with pytest.raises(SystemExit, match="Install codex"):
        run("setup", "agent", "assistant", "--backend", "codex")
    assert not config.exists()
    monkeypatch.setattr("shutil.which", lambda exe: "/bin/codex")
    run("setup", "agent", "assistant", "--backend", "codex")
    assert not (config / "resident.toml").exists()
    assert "Run the external agent for one session: harness --model assistant" in capsys.readouterr().out
    run("setup", "existing", "assistant")
    assert not (config / "resident.toml").exists()
    assert "Run the external agent for one session: harness --model assistant" in capsys.readouterr().out
    before = (config / "models.toml").read_bytes()
    assert Catalog.load(config / "models.toml").resolve("assistant").execution_kind == "agent"
    with pytest.raises(SystemExit, match="already exists"):
        run("setup", "api", "assistant", "--route", "provider/model", "--key-env", "MODEL_KEY")
    assert (config / "models.toml").read_bytes() == before


def test_guided_values_are_literal_and_errors_allow_retry(first_use, monkeypatch, capsys):
    run, config = first_use
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = iter(["local", "--help", "", "first", "y", "local", "desk", "", "-dash"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    run("setup")
    output = capsys.readouterr()
    assert "Alias must start" in output.err
    assert "Start a session: harness" in output.out.splitlines()
    catalog = Catalog.load(config / "models.toml")
    assert catalog.aliases() == ("desk",)
    assert catalog.resolve("desk").route == "openai/-dash"
