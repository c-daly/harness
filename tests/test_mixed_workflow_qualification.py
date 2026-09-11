"""The opt-in journey's oracle must distinguish completion from correctness."""

import copy
import json

import pytest

from harness.agent import current_agent_run
from harness.catalog import Catalog
from harness.dispatcher import current_dispatch_tool
from harness.hooks import ProposedToolCall
from harness.provider import StreamStop, TextDelta, Usage, UsageReport, text_turn, tool_call_turn
from harness.provider_codex import CodexProvider
from harness.provider_litellm import CatalogProvider
from harness.types import new_call_id
from scripts.qualify_mixed_workflow import EXPECTED, ROLES, assess, journey, validate_aliases


ALIASES = dict(zip(ROLES, ("local-test", "remote-test", "external-test")))


def catalog():
    return Catalog({
        "local-test": {"route": "openai/local-test", "api_base": "http://127.0.0.1:8080/v1", "local": {}},
        "remote-test": {"route": "openai/remote-test"},
        "external-test": {"route": "codex/default", "backend": "codex"},
    })


class FixtureCodex(CodexProvider):
    async def complete(self, **kwargs):
        dispatch = current_dispatch_tool.get()
        for tool, args in (("read_file", {"file_path": "CASE.json"}),
                           ("write_file", {"file_path": "external.json", "content": json.dumps(EXPECTED)})):
            result = await dispatch(ProposedToolCall(call_id=new_call_id(), tool=tool, args=args))
            assert not result.is_error
        yield TextDelta(json.dumps(EXPECTED))
        yield UsageReport(Usage(input_tokens=10, output_tokens=5))
        yield StreamStop("end_turn")


class FixtureProvider(CatalogProvider):
    def __init__(self, *, wrong=False, no_write=False):
        super().__init__(catalog(), codex=FixtureCodex())
        self.calls = {}
        self.wrong, self.no_write = wrong, no_write

    async def infer(self, request):
        active = current_agent_run.get()
        role = active.task.agent
        count = self.calls.get(role, 0)
        self.calls[role] = count + 1
        answer = {**EXPECTED, "remaining_attempts": 99} if self.wrong and role == "local" else EXPECTED
        if count == 0:
            chunks = tool_call_turn(new_call_id(), "read_file", {"file_path": "CASE.json"})
        elif count == 1 and not self.no_write:
            chunks = tool_call_turn(new_call_id(), "write_file",
                                   {"file_path": f"{role}.json", "content": json.dumps(answer)})
        else:
            chunks = text_turn(json.dumps(answer))
        for chunk in chunks:
            yield chunk


@pytest.fixture
async def completed(tmp_path):
    return await journey(tmp_path, FixtureProvider(), ALIASES, seconds=10)


async def test_journey_stops_typed_runtime_keeps_siblings_and_continues_after_restart(completed, tmp_path):
    assert completed["passed"], completed
    first, second = completed["phases"]
    assert first["accounting"]["children"] == 4  # one coordinator plus three agents
    assert second["accounting"]["children"] == 6  # fresh coordinator and external attempt
    assert second["accounting"]["model_calls"] == first["accounting"]["model_calls"] + 1
    assert second["accounting"]["unknown_input_attempts"] >= 1
    assert (tmp_path / "continuation.zip").is_file()
    assert list((tmp_path / "sessions/sessions").glob("*.jsonl"))
    assert completed["cancel_seconds"] < 3


@pytest.mark.parametrize("mode", ["wrong", "no_write"])
async def test_completed_text_and_successful_calls_do_not_qualify_incorrect_or_missing_artifacts(tmp_path, mode):
    report = await journey(tmp_path, FixtureProvider(**{mode: True}), ALIASES, seconds=10)
    assert report["stage"] == "finished"
    assert not report["passed"]
    assert not report["checks"]["native_partial_work"]
    assert report["checks"]["external_completed"]
    if mode == "wrong":
        member = report["phases"][0]["members"][0]
        assert member["status"] == "completed" and member["artifact_written"]
        assert not member["artifact"]["exact"]


@pytest.mark.parametrize("mutation", ["phase", "member", "codex", "parent", "cycle", "terminal", "check", "error"])
async def test_oracle_refuses_missing_evidence(completed, mutation):
    report = copy.deepcopy(completed)
    if mutation == "phase":
        report["phases"].pop()
    elif mutation == "member":
        report["phases"][0]["members"].pop(0)
    elif mutation == "codex":
        for run in report["phases"][1]["members"][0]["runs"]:
            run["runtime"] = "harness"
    elif mutation == "parent":
        report["phases"][1]["members"][0]["runs"][0]["parent_run_id"] = "unrelated"
    elif mutation == "terminal":
        report["phases"][1]["members"][0]["runs"][0]["terminal"] = None
    elif mutation == "cycle":
        run = report["phases"][1]["members"][0]["runs"][1]
        run["parent_run_id"] = run["id"]
    elif mutation == "error":
        report["error_type"] = "OSError"
    else:
        del report["checks"]["restart_counts_exact"]
    assert not assess(report)["passed"]


def test_empty_report_cannot_pass():
    assert not assess({})["passed"]


async def test_failed_restart_retains_first_phase_and_saves_failure(tmp_path, monkeypatch):
    from scripts import qualify_mixed_workflow as module
    original = module.make_kernel
    saved = []
    report = {}

    def fail_restart(*args, **kwargs):
        if kwargs.get("resume"):
            raise RuntimeError("private error must not appear in report")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "make_kernel", fail_restart)
    await journey(tmp_path, FixtureProvider(), ALIASES, seconds=10, report=report,
                  save=lambda: saved.append(copy.deepcopy(report)))
    assert saved[-1] == report and not report["passed"]
    assert report["stage"] == "restart" and report["error_type"] == "RuntimeError"
    assert len(report["phases"]) == 1 and report["phases"][0]["settled"]
    assert "private error" not in json.dumps(report)
    assert json.loads((tmp_path / "project/local.json").read_text()) == EXPECTED


@pytest.mark.parametrize("role,entry", [
    ("local-test", {"route": "openai/local"}),
    ("remote-test", {"route": "openai/local", "api_base": "http://localhost:8080/v1", "local": {}}),
    ("external-test", {"route": "claude-code/default", "backend": "claude-code"}),
])
def test_live_command_requires_distinct_runtime_classes(role, entry):
    models = catalog()
    models.entries[role] = entry
    with pytest.raises(ValueError):
        validate_aliases(models, ALIASES)


def test_duplicate_aliases_refused():
    with pytest.raises(ValueError, match="distinct"):
        validate_aliases(catalog(), {role: "local-test" for role in ROLES})


def test_cli_does_not_overwrite_an_existing_output(tmp_path, monkeypatch):
    from scripts.qualify_mixed_workflow import main
    config = tmp_path / "models.toml"
    config.write_text('[models.local-test]\nroute="openai/local"\napi_base="http://localhost:8080/v1"\n'
        '[models.local-test.local]\n[models.remote-test]\nroute="openai/remote"\n'
        '[models.external-test]\nroute="codex/default"\nbackend="codex"\n')
    marker = tmp_path / "retained"
    marker.write_text("keep")
    monkeypatch.setattr("sys.argv", ["qualify", "--catalog", str(config), "--output", str(tmp_path),
        *[item for role, alias in ALIASES.items() for item in (f"--{role}", alias)]])
    with pytest.raises(FileExistsError):
        main()
    assert marker.read_text() == "keep"
    assert not (tmp_path / "report.json").exists()
