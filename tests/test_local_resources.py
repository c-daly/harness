"""Readiness evidence and process ownership are core, not provider guesses."""

import asyncio
import os
import signal
import sys
from pathlib import Path

import httpx
import pytest

from harness.catalog import Catalog
from harness.resources import LocalResources


def catalog(**profile):
    return Catalog({"local": {"route": "openai/test-model", "api_base": "http://127.0.0.1:8080/v1",
                               "local": profile}})


@pytest.mark.parametrize("status, body, expected", [
    (200, {"data": [{"id": "test-model"}]}, "ready"),
    (200, {"data": [{"id": "different-model"}]}, "missing_configuration"),
    (401, {"secret": "do not record"}, "authentication_failed"),
    (403, {}, "denied"),
    (429, {}, "busy"),
    (503, {"error": {"type": "loading"}}, "loading"),
    (503, {}, "unknown"),
    (200, {"not": "an inventory"}, "unknown"),
])
async def test_inventory_observation_is_bounded_and_does_not_certify_features(status, body, expected):
    async def respond(request):
        assert str(request.url) == "http://127.0.0.1:8080/v1/models"
        return httpx.Response(status, json=body)

    resources = LocalResources(transport=httpx.MockTransport(respond))
    observed = []
    result = await resources.check(catalog().resolve("local"), emit=observed.append)
    assert result.status == expected and result.evidence == "model_inventory"
    assert result.observed_at > 0 and result.expires_at >= result.observed_at
    assert result.tool_support is None and result.structured_output is None
    assert result.context_window is None and result.ownership == "external"
    assert "do not record" not in str([e.model_dump() for e in observed])
    assert observed[-1].observation == result


async def test_stale_config_and_changed_credentials_force_a_new_check(monkeypatch):
    calls = []

    async def respond(request):
        calls.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"data": [{"id": "test-model"}]})

    resources = LocalResources(transport=httpx.MockTransport(respond))
    cat = catalog(ttl_seconds=30)
    cat.entries["local"]["api_key_env"] = "LOCAL_TEST_KEY"
    resolved = cat.resolve("local")
    monkeypatch.setenv("LOCAL_TEST_KEY", "first-secret")
    async with resources.use(resolved, emit=lambda e: None):
        pass
    async with resources.use(resolved, emit=lambda e: None):
        pass
    assert calls == ["Bearer first-secret"]
    monkeypatch.setenv("LOCAL_TEST_KEY", "second-secret")
    async with resources.use(resolved, emit=lambda e: None):
        pass
    cat.entries["local"]["local"]["ttl_seconds"] = 0
    async with resources.use(cat.resolve("local"), emit=lambda e: None):
        pass
    async with resources.use(cat.resolve("local"), emit=lambda e: None):
        pass
    assert len(calls) == 4


async def test_checks_cancel_promptly_without_caching_ready():
    entered = asyncio.Event()

    async def respond(request):
        entered.set()
        await asyncio.Event().wait()

    resources = LocalResources(transport=httpx.MockTransport(respond))
    resolved = catalog().resolve("local")
    task = asyncio.create_task(resources.check(resolved, emit=lambda e: None))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert resources.snapshot(resolved).status == "unknown"


@pytest.mark.parametrize("url", ["https://example.com/v1", "http://127.0.0.1:8080/v1?token=secret",
                                "http://user:secret@127.0.0.1:8080/v1", "file:///tmp/model"])
def test_local_profiles_require_an_explicit_loopback_endpoint(url):
    cat = catalog()
    cat.entries["local"]["api_base"] = url
    with pytest.raises(ValueError, match="local"):
        cat.resolve("local")


def owned_catalog(tmp_path, port, *, delay=0, **profile):
    command = (sys.executable, str(Path(__file__).parent / "fixtures/local_runtime_server.py"),
               "--port", str(port), "--pid-file", str(tmp_path / "runtime.pid"), "--delay", str(delay))
    cat = catalog(command=command, auto_start=True, ttl_seconds=60, startup_seconds=5, **profile)
    cat.entries["local"]["api_base"] = f"http://127.0.0.1:{port}/v1"
    return cat


async def wait_pid(path):
    async with asyncio.timeout(3):
        while not path.exists():
            await asyncio.sleep(0.01)
    return int(path.read_text())


@pytest.mark.parametrize("stop_log_failure", [False, True])
async def test_owned_process_runs_a_native_project_task_then_shuts_down(
    tmp_path, unused_tcp_port, monkeypatch, stop_log_failure,
):
    from harness.cli import build_kernel, run_once
    from harness.fold import fold
    from harness.log import read_session
    from harness.provider_litellm import CatalogProvider
    from harness.types import ModelId

    (tmp_path / "FACTS.txt").write_text("answer=42\n")
    cat = owned_catalog(tmp_path, unused_tcp_port, delay=0.15, probe_kind="llamacpp")
    kernel = build_kernel(base_dir=tmp_path / "sessions", provider=CatalogProvider(cat),
                          model=ModelId("local"), native_tools=True, workspace_root=tmp_path)
    append = kernel.session.append

    def record(event):
        if stop_log_failure and event.type == "local_runtime_requested" and event.action == "stop":
            raise OSError("stop journal failure")
        return append(event)

    monkeypatch.setattr(kernel.session, "append", record)
    if stop_log_failure:
        with pytest.raises(OSError, match="stop journal failure"):
            await run_once(kernel, "Read FACTS.txt and give the project answer.")
    else:
        assert await run_once(kernel, "Read FACTS.txt and give the project answer.") == "The project answer is 42."
    pid = int((tmp_path / "runtime.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    events = read_session(tmp_path / "sessions", kernel.session.id)
    state = fold(events)
    assert kernel.loop.history == state.messages
    assert not state.open_agent_runs and not state.open_model_intents
    observations = [e.event.observation for e in events if e.event.type == "resource_observed"]
    assert any(o.status == "ready" and o.ownership == "harness" for o in observations)
    assert state.resources["local"].status == "stopped"
    actions = [e.event.action for e in events if e.event.type == "local_runtime_requested"]
    assert actions == (["start"] if stop_log_failure else ["start", "stop"])
    assert "command" not in str([e.event.model_dump() for e in events if e.event.type == "resource_observed"])
    # Shutdown must release the session guard even when process event logging failed.
    from harness.resume import resume_session
    resumed, _ = resume_session(tmp_path / "sessions", kernel.session.id)
    resumed.close()


async def test_existing_service_is_never_adopted_or_stopped(tmp_path, unused_tcp_port):
    cat = owned_catalog(tmp_path, unused_tcp_port)
    entry = cat.resolve("local")
    process = await asyncio.create_subprocess_exec(*entry.local.command, start_new_session=True)
    resources = LocalResources()
    try:
        await wait_pid(tmp_path / "runtime.pid")
        events = []
        async with resources.use(entry, emit=events.append) as observation:
            assert observation.ownership == "external"
        assert not await resources.stop("local", emit=events.append)
        await resources.close(emit=events.append)
        assert process.returncode is None
        os.kill(process.pid, 0)
        assert not any(e.type == "local_runtime_requested" for e in events)
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        await process.wait()


@pytest.mark.parametrize("mode", ["cancel", "deadline"])
async def test_loading_cancellation_and_deadline_reap_owned_process(tmp_path, unused_tcp_port, mode):
    from harness.errors import ProviderError
    cat = owned_catalog(tmp_path, unused_tcp_port, delay=60)
    if mode == "deadline":
        cat.entries["local"]["local"]["startup_seconds"] = 0.2
    resources = LocalResources()
    events = []

    async def run():
        async with resources.use(cat.resolve("local"), emit=events.append):
            raise AssertionError("loading server is not ready")

    work = asyncio.create_task(run())
    pid = await wait_pid(tmp_path / "runtime.pid")
    if mode == "cancel":
        work.cancel()
    with pytest.raises(asyncio.CancelledError if mode == "cancel" else (TimeoutError, ProviderError)):
        await work
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert events[-1].observation.status == "stopped"
    await resources.close(emit=events.append)


async def test_cancellation_during_spawn_recovers_handle_and_reaps_it(tmp_path, unused_tcp_port, monkeypatch):
    original = asyncio.create_subprocess_exec
    spawned, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def slow_spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        spawned.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_spawn)
    resources = LocalResources()

    async def run():
        async with resources.use(owned_catalog(tmp_path, unused_tcp_port).resolve("local"), emit=lambda e: None):
            pass

    work = asyncio.create_task(run())
    await asyncio.wait_for(spawned.wait(), 3)
    work.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert processes[0].returncode is not None


@pytest.mark.parametrize("deny", ["policy", "budget"])
async def test_dispatch_denial_precedes_readiness_and_startup(tmp_path, deny):
    from harness.agent import AgentTask
    from harness.cli import build_kernel
    from harness.execution import ExecutionLimits
    from harness.hooks import Block, HookBus
    from harness.provider_litellm import CatalogProvider
    from harness.types import ModelId

    probes = []
    hooks = HookBus()
    if deny == "policy":
        hooks.register_dispatch("deny", lambda call: Block(reason="denied"))
    resources = LocalResources(transport=httpx.MockTransport(lambda request: probes.append(request)))
    kernel = build_kernel(base_dir=tmp_path, provider=CatalogProvider(catalog()), model=ModelId("local"),
                          hooks=hooks, resources=resources,
                          execution_limits=ExecutionLimits(max_model_calls=0 if deny == "budget" else 1))
    try:
        await kernel.loop.start()
        with pytest.raises(Exception, match="denied|budget"):
            await kernel.loop.run_task(AgentTask(prompt="work"))
        assert not probes
    finally:
        await resources.close(emit=kernel.session.append)
        kernel.session.close()


async def test_probe_deadline_and_oversized_inventory_fail_closed():
    async def hanging(request):
        await asyncio.Event().wait()

    resources = LocalResources(transport=httpx.MockTransport(hanging))
    observation = await resources.check(catalog(probe_seconds=0.01).resolve("local"), emit=lambda e: None)
    assert observation.status == "unreachable" and observation.reason == "probe_timeout"
    resources = LocalResources(transport=httpx.MockTransport(lambda request:
        httpx.Response(200, content=b"x" * 65537)))
    observation = await resources.check(catalog().resolve("local"), emit=lambda e: None)
    assert observation.status == "unknown" and observation.reason == "inventory_too_large"


async def test_shutdown_reaps_owned_process_even_when_event_writes_fail(tmp_path, unused_tcp_port):
    resources = LocalResources()
    entry = owned_catalog(tmp_path, unused_tcp_port).resolve("local")
    async with resources.use(entry, emit=lambda e: None):
        pass
    pid = int((tmp_path / "runtime.pid").read_text())

    def broken_log(event):
        raise OSError("disk failure")

    with pytest.raises(OSError, match="disk failure"):
        await resources.close(emit=broken_log)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_route_rewrite_checks_effective_local_resource_and_records_improvement_evidence(tmp_path, monkeypatch):
    from harness.agent import AgentTask
    from harness.cli import build_kernel
    from harness.hooks import HookBus, ProposedModelCall, Rewrite
    from harness.log import read_session
    from harness.provider import StreamStop, TextDelta
    from harness.provider_litellm import CatalogProvider
    from harness.types import ModelId

    checks, calls = [], []

    async def probe(request):
        checks.append(request)
        return httpx.Response(200, json={"data": [{"id": "test-model"}]})

    async def infer(**kwargs):
        calls.append(kwargs)
        yield TextDelta("done")
        yield StreamStop("end_turn")

    monkeypatch.setattr("harness.provider_litellm._acomplete", infer)
    hooks = HookBus()
    hooks.register_dispatch("route", lambda action: Rewrite(action=ProposedModelCall(
        call_id=action.call_id, model=ModelId("local"))))
    resources = LocalResources(transport=httpx.MockTransport(probe))
    kernel = build_kernel(base_dir=tmp_path, provider=CatalogProvider(catalog()), model=ModelId("remote"),
                          hooks=hooks, resources=resources)
    try:
        await kernel.loop.start()
        result = await kernel.loop.run_task(AgentTask(prompt="work"))
        assert result.status == "completed"
        assert len(checks) == 1 and calls[0]["model"] == "openai/test-model"
        events = read_session(tmp_path, kernel.session.id)
        from harness.fold import fold
        assert fold(events).resources["local"].status == "ready"
        observation = next(e for e in events if e.event.type == "resource_observed"
                           and e.event.observation.status == "ready")
        assert observation.event.observation.alias == "local"
        from harness.improvement import Evidence
        kernel.improvements.record(Evidence(id="local-readiness", source_session=kernel.session.id,
                                            source_seq=observation.seq, category="task_outcome",
                                            observation="Local model inventory became ready; quality remains unverified."))
        assert "local-readiness" in kernel.improvements.state.evidence
        assert not kernel.plugins
    finally:
        await resources.close(emit=kernel.session.append)
        kernel.session.close()


def test_cli_resource_inspection_never_starts_inference_or_a_process(tmp_path, monkeypatch, capsys):
    from harness.cli import main
    config = tmp_path / "models.toml"
    config.write_text('[models.local]\nroute="openai/test-model"\napi_base="http://127.0.0.1:8080/v1"\n'
                      '[models.local.local]\nauto_start=true\ncommand=["must-not-run"]\n')
    monkeypatch.setattr(sys, "argv", ["harness", "resources", "--catalog", str(config), "--json"])
    main()
    import json
    observation, = json.loads(capsys.readouterr().out)
    assert observation["status"] == "unknown" and observation["stale"] is True
    assert "must-not-run" not in str(observation)


async def test_owned_capacity_does_not_launch_a_second_runtime(tmp_path, unused_tcp_port, unused_tcp_port_factory):
    from harness.errors import ProviderError
    resources = LocalResources()
    first = owned_catalog(tmp_path, unused_tcp_port).resolve("local")
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second_catalog = owned_catalog(second_dir, unused_tcp_port_factory())
    second_catalog.entries["second"] = second_catalog.entries.pop("local")
    second = second_catalog.resolve("second")
    try:
        async with resources.use(first, emit=lambda e: None):
            pass
        with pytest.raises(ProviderError, match="owned_process_capacity"):
            async with resources.use(second, emit=lambda e: None):
                pass
        assert not (second_dir / "runtime.pid").exists()
    finally:
        await resources.close(emit=lambda e: None)


@pytest.mark.parametrize("code, health, expected, count", [
    (503, {"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}}, "loading", 1),
    (503, {"error": {"message": "unavailable", "type": "unavailable_error"}}, "unknown", 1),
    (200, {"status": "ok"}, "ready", 2),
    (200, {"status": "maybe"}, "unknown", 1),
])
async def test_llamacpp_health_precedes_inventory(code, health, expected, count):
    calls = []

    async def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/health"):
            return httpx.Response(code, json=health)
        # llama.cpp can advertise the ID before loading has finished.
        return httpx.Response(200, json={"data": [{"id": "test-model", "meta": None}]})

    resources = LocalResources(transport=httpx.MockTransport(respond))
    result = await resources.check(catalog(probe_kind="llamacpp").resolve("local"), emit=lambda e: None)
    assert result.status == expected and result.evidence == "health_and_inventory"
    assert len(calls) == count and calls[0] == "/v1/health"


async def test_startup_cancel_terminates_a_worker_that_ignores_term(tmp_path, unused_tcp_port):
    child_pid_file = tmp_path / "worker.pid"
    child_code = ("import os, signal, time; from pathlib import Path; "
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                  f"Path({str(child_pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)")
    parent_code = ("import subprocess, sys, time; "
                   f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(60)")
    cat = owned_catalog(tmp_path, unused_tcp_port)
    cat.entries["local"]["local"]["command"] = [sys.executable, "-c", parent_code]
    resources = LocalResources()

    async def run():
        async with resources.use(cat.resolve("local"), emit=lambda e: None):
            pass

    work = asyncio.create_task(run())
    pid = await wait_pid(child_pid_file)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    async with asyncio.timeout(1):
        while True:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                break
            if state == "Z":  # dead grandchild awaiting its init/subreaper
                break
            await asyncio.sleep(0.01)
