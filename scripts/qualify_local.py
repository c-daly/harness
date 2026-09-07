"""Opt-in real-model offline journey; requires provisioned assets and Linux isolation.

Run using the pinned Docker recipe in docs/local-model-qualification.md. This is
a public fixture smoke test, not a held-out semantic or full product qualification.
Reports contain metadata only; temporary sessions (including memory) are removed.
"""

import argparse
import asyncio
import copy
import hashlib
import json
import os
import subprocess
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from harness.agent import AgentTask, TaskLimits
from harness.catalog import Catalog
from harness.cli import build_kernel
from harness.context import ContextPolicy, ResponsePolicy
from harness.fold import fold
from harness.errors import MalformedStreamError, ToolCallLimitExceeded
from harness.log import read_session
from harness.mcp_config import McpServerSpec
from harness.messages import Message
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import TextDelta
from harness.provider_litellm import CatalogProvider
from harness.semantics import SemanticLimits
from harness.types import ModelId

IMAGE = "sha256:841b199aed2649a748875b043b32fed2e8c2d4d87e1d563556817fb7fa44b72b"
WEIGHTS_SHA256 = "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597"
WEIGHTS_BYTES = 2497281120
MODEL_PROFILES = {
    "qwen3-4b-instruct": {"repo": "unsloth/Qwen3-4B-Instruct-2507-GGUF",
        "filename": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        "revision": "a06e946bb6b655725eafa393f4a9745d460374c9", "bytes": WEIGHTS_BYTES,
        "sha256": WEIGHTS_SHA256, "runtime_args": []},
    "qwen3-8b": {"repo": "Qwen/Qwen3-8B-GGUF",
        "filename": "Qwen3-8B-Q4_K_M.gguf",
        "revision": "7c41481f57cb95916b40956ab2f0b139b296d974", "bytes": 5027783488,
        "sha256": "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785",
        "runtime_args": ["--chat-template-kwargs", '{"enable_thinking":false}',
                         "--temp", "0.7", "--top-p", "0.8", "--top-k", "20", "--min-p", "0",
                         "--presence-penalty", "1.5"]},
}
FACTS = {"project": "harbor", "retry_limit": 3}
THRESHOLDS = {"project_seconds": 45, "cold_start_seconds": 30,
              "warm_semantic_ms": 2000, "cancel_seconds": 2, "ui_mount_seconds": 3}
PUBLIC_CASES = (("Is the test passing?", "question"), ("stop", "stop_request"),
                ("hold on", "pause"))


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def catalog(model_file, profile="qwen3-4b-instruct"):
    return Catalog(entries={"local-small": {
        "route": "openai/local-small", "api_base": "http://127.0.0.1:8080/v1",
        "max_input_tokens": 8192, "verified": False,
        "local": {"auto_start": True, "cwd": "/app", "probe_kind": "llamacpp",
                  "startup_seconds": THRESHOLDS["cold_start_seconds"], "probe_seconds": 2,
                  "required_files": ["/app/llama-server", str(model_file)],
                  "env_names": ["LD_LIBRARY_PATH"],
                  "command": ["/app/llama-server", "--model", str(model_file),
                              "--alias", "local-small", "--host", "127.0.0.1", "--port", "8080",
                              "--n-gpu-layers", "99", "--ctx-size", "8192", "--parallel", "1",
                              "--threads", "4", "--threads-batch", "4", "--flash-attn", "on",
                              "--jinja", "--cache-ram", "0", "--fit", "off",
                              *MODEL_PROFILES[profile]["runtime_args"]]}}})


def isolation():
    """Fail closed unless the worker has only loopback and finite resource caps."""
    interfaces = sorted(p.name for p in Path("/sys/class/net").iterdir())
    memory = Path("/sys/fs/cgroup/memory.max").read_text().strip()
    swap = Path("/sys/fs/cgroup/memory.swap.max").read_text().strip()
    cpu = Path("/sys/fs/cgroup/cpu.max").read_text().split()
    if (interfaces != ["lo"] or memory == "max" or not 0 < int(memory) <= 4 * 1024**3
            or swap != "0" or cpu[0] == "max" or not 0 < int(cpu[0]) / int(cpu[1]) <= 4):
        raise ValueError("requires loopback-only network, <=4 GiB RAM, zero swap, <=4 CPUs")
    return {"interfaces": interfaces, "memory_max_bytes": int(memory),
            "swap_max_bytes": int(swap), "cpu_max": cpu}


def project_checks(result, outcomes, artifact, *, expected=FACTS):
    """Grade external evidence; neither a model's claim nor completion is acceptance."""
    return {"execution_completed": result.status == "completed",
            "acceptance_remains_unverified": result.acceptance == "unverified",
            "read_succeeded": any(e["tool"] == "read_file" and e["path"] == "FACTS.json"
                                  and not e["is_error"] for e in outcomes),
            "write_succeeded": any(e["tool"] == "write_file" and e["path"] == "RESULT.json"
                                   and not e["is_error"] for e in outcomes),
            "artifact_exact": artifact == expected}


def artifact_metadata(artifact, *, expected=FACTS):
    """Explain mismatches without publishing model output derived from private memory."""
    obj = artifact if isinstance(artifact, dict) else {}
    return {"object": isinstance(artifact, dict), "keys_exact": set(obj) == set(expected),
            "project_matches": obj.get("project") == expected["project"],
            "retry_limit_matches": obj.get("retry_limit") == expected["retry_limit"],
            "retry_limit_type": type(obj.get("retry_limit")).__name__,
            "sha256": hashlib.sha256(json.dumps(artifact, sort_keys=True).encode()).hexdigest()}


def events(kernel, base):
    return [e.event for e in read_session(base, kernel.session.id)]


def tool_outcomes(kernel, base):
    log = events(kernel, base)
    resolved = {e.call_id: e for e in log if e.type == "dispatch_resolved" and e.kind == "tool"}
    return [{"tool": resolved[e.call_id].tool, "is_error": e.is_error,
             "path": Path((resolved[e.call_id].args or {}).get("file_path", "")).name,
             "text": kernel.session.blobs.get(e.result_blob).decode() if e.result_blob
                     else e.result_text or ""}
            for e in log if e.type == "tool_call_completed" and e.call_id in resolved]


def settled(kernel, base):
    state = fold(read_session(base, kernel.session.id))
    return not (state.open_intents or state.open_model_intents or state.open_agent_runs)


def failure_reason(error):
    """Keep actionable error categories without copying private tool arguments."""
    if isinstance(error, ToolCallLimitExceeded):
        return "multiple_tool_proposals"
    if isinstance(error, MalformedStreamError) and "unparseable arguments" in str(error):
        return "invalid_tool_arguments"
    return "other_failure"


def start_latency(kernel, base):
    log = read_session(base, kernel.session.id)
    starts = [e.ts for e in log if e.event.type == "local_runtime_requested"
              and e.event.action == "start"]
    ready = [e.ts for e in log if e.event.type == "resource_observed"
             and e.event.observation.status == "ready"
             and e.event.observation.ownership == "harness"]
    return ready[0] - starts[0] if ready and starts else None


def make_kernel(base, workspace, models, memory_root=None, resume=None, *, parallel_tool_calls=None,
                tool_recovery_attempts=0, response=None):
    names = ("read_file", "write_file")
    specs = ()
    if memory_root:
        names += ("mcp__memory__memory_list",)
        specs = (McpServerSpec(name="memory", transport="stdio",
            command=str(memory_root / ".venv/bin/python"),
            args=("-B", str(memory_root / "lib/server.py")),
            env={"MEMORY_VAULT_DIR": "MEMORY_VAULT_DIR"},
            tools_allow=("memory_list",), restart="never", tool_timeout_s=10),)
    permissions = PermissionEngine([RuleSet(rules=[
        PermissionRule("allow", "model:local-small"), PermissionRule("allow", "read_file"),
        PermissionRule("allow", "write_file"),
        PermissionRule("allow", "mcp__memory__memory_list", {"subject": "harness"}),
    ], default="deny")])
    return build_kernel(base_dir=base, provider=CatalogProvider(models), model=ModelId("local-small"),
        system_prompt="Use the available tools to inspect the project. Be brief and factual.",
        native_tools=True, workspace_root=workspace, permissions=permissions, mcp=specs,
        resume_session_id=resume,
        context_policy=None if resume else ContextPolicy(history_turns=1, tools=names,
                                                         parallel_tool_calls=parallel_tool_calls,
                                                         tool_recovery_attempts=tool_recovery_attempts,
                                                         response=response))


async def close(kernel):
    try:
        if kernel.mcp:
            await kernel.mcp.stop()
            kernel.mcp.flush_events()
    finally:
        try:
            await kernel.resources.close(emit=kernel.session.append)
        finally:
            try:
                await kernel.loop.end()
            finally:
                kernel.session.close()


async def project_case(root, models, memory_root, *, parallel_tool_calls=None, facts=FACTS,
                       tool_recovery_attempts=0, response=None):
    workspace, base = root / "project", root / "sessions"
    workspace.mkdir(parents=True)
    (workspace / "FACTS.json").write_text(json.dumps(facts))
    kernel = make_kernel(base, workspace, models, memory_root, parallel_tool_calls=parallel_tool_calls,
                         tool_recovery_attempts=tool_recovery_attempts, response=response)
    row = {"mode": "normal-memory" if memory_root else "no-plugins", "checks": {}, "stage": "start"}
    checks = row["checks"]
    try:
        if kernel.mcp:
            warnings = await kernel.mcp.start(only={"memory"})
            if warnings:
                raise RuntimeError("memory startup failed")
        await kernel.loop.start()
        if kernel.mcp:
            kernel.mcp.flush_events()
        row["stage"] = "project-task"
        prompt = ("Read FACTS.json and write RESULT.json as a JSON object containing only the "
                  "project and retry_limit values from that file. Then briefly confirm the values.")
        if memory_root:
            prompt += " Also call mcp__memory__memory_list with subject exactly 'harness'."
        started = time.monotonic()
        result = await kernel.loop.run_task(AgentTask(prompt=prompt,
            acceptance_criteria=("RESULT.json contains the two source facts",),
            limits=TaskLimits(max_iterations=6, timeout_seconds=THRESHOLDS["project_seconds"],
                              max_input_bytes=32768, max_output_tokens=512)))
        row["project_seconds"] = time.monotonic() - started
        outcomes = tool_outcomes(kernel, base)
        row["tool_sequence"] = [e["tool"] for e in outcomes]
        row["project_model_calls"] = sum(e.type == "model_call_completed" for e in events(kernel, base))
        row["tool_batches"] = [[str(c.tool) for c in Message.model_validate(e.message).tool_calls()]
                               for e in events(kernel, base) if e.type == "model_call_completed"]
        try:
            artifact = json.loads((workspace / "RESULT.json").read_text())
            checks["artifact_is_json"] = True
        except (OSError, ValueError) as exc:
            artifact = None
            checks["artifact_is_json"] = False
            row["artifact_error_type"] = type(exc).__name__
        row["artifact"] = artifact_metadata(artifact, expected=facts)
        checks.update(project_checks(result, outcomes, artifact, expected=facts))
        checks["project_deadline"] = row["project_seconds"] <= THRESHOLDS["project_seconds"]
        checks["owned_cold_start"] = any(e.type == "resource_observed"
            and e.observation.status == "ready" and e.observation.ownership == "harness"
            for e in events(kernel, base))
        row["owned_start_to_ready_seconds"] = start_latency(kernel, base)
        if memory_root:
            memory = [e for e in outcomes if e["tool"] == "mcp__memory__memory_list" and not e["is_error"]]
            checks["normal_memory_used"] = bool(memory and memory[-1]["text"].startswith("- type:"))
            text = memory[-1]["text"] if memory else ""
            row["memory_bytes"] = len(text.encode())
            row["memory_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        row["stage"] = "warm-semantic-samples"
        samples = []
        for repeat in range(3):
            for text, expected in PUBLIC_CASES:
                obs = await kernel.semantics.interpret(text, model=ModelId("local-small"),
                                                       limits=SemanticLimits(timeout_seconds=5))
                samples.append({"repeat": repeat, "expected": expected, "observed": obs.kind,
                                "status": obs.status, "reason": obs.reason,
                                "duration_ms": obs.duration_ms})
        row["semantic_samples"] = samples
        checks["public_semantic_examples"] = all(s["observed"] == s["expected"]
                                                 and s["status"] == "ok" for s in samples)
        checks["warm_semantic_deadline"] = all(s["duration_ms"] <= THRESHOLDS["warm_semantic_ms"]
                                               for s in samples)
        checks["settled_before_close"] = settled(kernel, base)
        row["stage"] = "complete"
    except Exception as exc:
        row["error_type"] = type(exc).__name__  # never include retrieved memory in an error report
        row["failure_reason"] = failure_reason(exc)
    finally:
        try:
            await close(kernel)
            checks["cleanup_succeeded"] = True
        except Exception as exc:
            row["cleanup_error_type"] = type(exc).__name__
            checks["cleanup_succeeded"] = False
    checks["owned_runtime_stopped"] = any(e.type == "resource_observed"
        and e.observation.status == "stopped" for e in events(kernel, base))
    row["correction_attempts"] = sum(e.type == "model_correction_requested" for e in events(kernel, base))
    row["model_failures"] = [e.error_type for e in events(kernel, base) if e.type == "model_call_failed"]
    row["passed"] = row["stage"] == "complete" and all(checks.values())
    return row


async def until(predicate, seconds=45):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.02)


async def tui_case(root, models, memory_root=None, *, parallel_tool_calls=None, tool_recovery_attempts=0,
                   response=None):
    from textual.widgets import Input
    from harness.tui import HarnessApp

    models = Catalog(entries=copy.deepcopy(models.entries))
    workspace, base = root / "project", root / "sessions"
    workspace.mkdir(parents=True)
    (workspace / "FACTS.json").write_text(json.dumps(FACTS))
    kernel = make_kernel(base, workspace, models, memory_root, parallel_tool_calls=parallel_tool_calls,
                         tool_recovery_attempts=tool_recovery_attempts, response=response)
    app = HarnessApp(kernel, native_tools=True, workspace_root=workspace)
    row = {"mode": "tui-normal-memory" if memory_root else "tui-no-plugins", "checks": {}, "stage": "mount"}
    checks = row["checks"]

    def screen():
        return "\n".join(strip.text for strip in app.screen._compositor.render_strips())

    started = time.monotonic()
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.1)
            if memory_root:
                await until(lambda: type(app.screen).__name__ == "ServerChecklistScreen")
                await pilot.press("enter")  # accept the configured normal-memory server
                await until(lambda: app._bus_pump_worker is not None)
            composer = app.query_one("#prompt", Input)
            row["mount_seconds"] = time.monotonic() - started
            checks["composer_before_runtime_start"] = (not composer.disabled and not any(
                e.type == "local_runtime_requested" for e in events(kernel, base)))
            checks["mount_deadline"] = row["mount_seconds"] <= THRESHOLDS["ui_mount_seconds"]

            async def submit(text):
                composer.value = text
                composer.focus()
                await pilot.press("enter")

            row["stage"] = "project-answer"
            prompt = "Read FACTS.json and tell me the project name and retry limit."
            if memory_root:
                prompt = ("Read FACTS.json and write RESULT.json as a JSON object containing only the "
                          "project and retry_limit values from that file. Then briefly confirm the values. "
                          "Also call mcp__memory__memory_list with subject exactly 'harness'.")
            await submit(prompt)
            await until(lambda: any(e.type == "agent_run_finished" for e in events(app.kernel, base)))
            await until(lambda: app.controller.active is None)
            await pilot.pause(0.2)
            answer = [e.result for e in events(app.kernel, base)
                      if e.type == "agent_run_finished"][-1]
            answer_text = answer.read_text(app.kernel.session.blobs)
            checks["project_answer_visible"] = (answer.status == "completed" and
                "harbor" in answer_text and "3" in answer_text and
                "harbor" in screen() and "3" in screen())
            checks["real_read_succeeded"] = any(e["tool"] == "read_file" and not e["is_error"]
                                                for e in tool_outcomes(app.kernel, base))
            if memory_root:
                outcomes = tool_outcomes(app.kernel, base)
                try:
                    artifact = json.loads((workspace / "RESULT.json").read_text())
                except (OSError, ValueError):
                    artifact = None
                checks.update(project_checks(answer, outcomes, artifact))
                row["artifact"] = artifact_metadata(artifact)
                memory = [e for e in outcomes if e["tool"] == "mcp__memory__memory_list" and not e["is_error"]]
                checks["normal_memory_used"] = bool(memory and memory[-1]["text"].startswith("- type:"))
                text = memory[-1]["text"] if memory else ""
                row["memory_bytes"] = len(text.encode())
                row["memory_sha256"] = hashlib.sha256(text.encode()).hexdigest()
                memory_count = len(memory)
            row["stage"] = "cancel-stream"
            streamed = asyncio.Event()
            previous = app.kernel.loop.on_chunk

            def chunk(value):
                previous(value)
                if isinstance(value, TextDelta) and value.text:
                    streamed.set()

            app.kernel.loop.on_chunk = chunk
            # Pilot.press waits for screen idleness; continuous model output can
            # keep it waiting until generation ends. Dispatch through the normal
            # input event without waiting for that idle barrier before Escape.
            composer.value = "Write 500 numbered sentences describing a harbor. Do not use tools."
            composer.post_message(Input.Submitted(composer, composer.value))
            await until(lambda: app.controller.active is not None)
            composer.value = "unsent draft"
            await asyncio.wait_for(streamed.wait(), 30)
            started = time.monotonic()
            row["active_at_cancel"] = app.controller.active is not None
            await pilot.press("escape")
            await until(lambda: app.controller.active is None and not app._interrupting,
                        THRESHOLDS["cancel_seconds"])
            row["cancel_seconds"] = time.monotonic() - started
            await pilot.pause(0.1)
            checks["cancelled_real_stream"] = any(e.type == "agent_run_finished" and
                e.result.status == "cancelled" for e in events(app.kernel, base))
            row["cancel_outcomes"] = [e.result.status for e in events(app.kernel, base)
                                       if e.type == "agent_run_finished"]
            checks["draft_preserved"] = composer.value == "unsent draft" and "unsent draft" in screen()
            checks["cancel_settled"] = settled(app.kernel, base)
            row["stage"] = "resume"
            old_kernel = app.kernel
            session_id = old_kernel.session.id
            profile = old_kernel.context_policy
            await submit("/clear")
            await until(lambda: app.kernel is not old_kernel)
            await until(lambda: not app._rebuild_in_progress)
            await submit("/resume")
            await until(lambda: type(app.screen).__name__ == "SessionPickerScreen")
            await pilot.press("enter")
            await until(lambda: app.kernel.session.id == session_id and not app._rebuild_in_progress)
            await pilot.pause(0.2)
            composer = app.query_one("#prompt", Input)
            checks["resume_same_session"] = app.kernel.session.id == session_id
            checks["resume_profile"] = app.kernel.context_policy == profile
            checks["resume_history"] = any(m.text().startswith("Read FACTS.json")
                                            for m in app.kernel.loop.history)
            before = sum(e.type == "agent_run_finished" for e in events(app.kernel, base))
            prompt = "Read FACTS.json again and tell me the project name and retry limit."
            if memory_root:
                prompt += " Also call mcp__memory__memory_list with subject exactly 'harness'."
            await submit(prompt)
            await until(lambda: sum(e.type == "agent_run_finished"
                for e in events(app.kernel, base)) > before)
            await until(lambda: app.controller.active is None)
            await pilot.pause(0.2)
            last = [e for e in events(app.kernel, base) if e.type == "agent_run_finished"][-1]
            resume_text = last.result.read_text(app.kernel.session.blobs)
            row["resume_answer"] = {"status": last.result.status, "bytes": len(resume_text.encode()),
                "contains_project": "harbor" in resume_text, "project_visible": "harbor" in screen(),
                "sha256": hashlib.sha256(resume_text.encode()).hexdigest()}
            checks["resume_answer"] = (last.result.status == "completed" and
                row["resume_answer"]["contains_project"] and row["resume_answer"]["project_visible"])
            checks["resume_settled"] = settled(app.kernel, base)
            if memory_root:
                checks["resume_normal_memory_used"] = sum(e["tool"] == "mcp__memory__memory_list"
                    and not e["is_error"] for e in tool_outcomes(app.kernel, base)) > memory_count
            row["correction_attempts"] = sum(e.type == "model_correction_requested"
                                              for e in events(app.kernel, base))
            row["stage"] = "unavailable-runtime"
            await submit("/resources stop local-small")
            models.entries["local-small"]["local"]["required_files"].append(
                "/models/deliberately-missing.gguf")
            before += 1
            await submit("Read FACTS.json once more.")
            await until(lambda: sum(e.type == "agent_run_finished"
                for e in events(app.kernel, base)) > before)
            await until(lambda: app.controller.active is None)
            composer.value = "draft while unavailable"
            composer.focus()
            await pilot.pause(0.2)
            checks["missing_runtime_reported"] = any(e.type == "resource_observed" and
                e.observation.status == "missing_configuration" for e in events(app.kernel, base))
            checks["failure_recoverable"] = (app.controller.last_failed is not None
                and composer.value == "draft while unavailable" and not composer.disabled
                and "draft while unavailable" in screen() and settled(app.kernel, base))
            models.entries["local-small"]["local"]["required_files"].pop()
            row["stage"] = "complete"
    except Exception as exc:
        row["error_type"] = type(exc).__name__
    finally:
        try:
            # run_test/on_unmount already ends the loop. Mirror run_tui's
            # remaining MCP teardown without trying to end the same loop twice.
            if app.kernel.mcp:
                await app.kernel.mcp.stop()
                app.kernel.mcp.flush_events()
            await app.kernel.resources.close(emit=app.kernel.session.append)
            checks["cleanup_succeeded"] = True
        except Exception as exc:
            row["cleanup_error_type"] = type(exc).__name__
            checks["cleanup_succeeded"] = False
        finally:
            if app._mcp_errlog is not None:
                app._mcp_errlog.close()
            app.kernel.session.close()
    row["passed"] = row["stage"] == "complete" and all(checks.values())
    return row


async def run(root, models, memory_root, *, parallel_tool_calls=None, tool_recovery_attempts=0, response=None):
    policy = {"parallel_tool_calls": parallel_tool_calls, "tool_recovery_attempts": tool_recovery_attempts,
              "response": response}
    rows = [await project_case(root / "no-plugins", models, None,
                               **policy)]
    if memory_root:
        rows.append(await project_case(root / "memory", models, memory_root,
                                       **policy))
    rows.append(await tui_case(root / "tui", models, **policy))
    if memory_root:
        rows.append(await tui_case(root / "tui-memory", models, memory_root, **policy))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-file", type=Path, default=Path("/models/local.gguf"))
    parser.add_argument("--model-profile", choices=MODEL_PROFILES, default="qwen3-4b-instruct")
    parser.add_argument("--memory-root", type=Path)
    parser.add_argument("--runs", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--single-tool", action="store_true",
                        help="Opt in to one tool proposal per response through the context profile.")
    parser.add_argument("--tool-recovery-attempts", type=int, choices=range(3), default=0)
    parser.add_argument("--response-profile", type=Path, help="TOML response settings for this explicit experiment")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    response = (ResponsePolicy.model_validate(tomllib.loads(args.response_profile.read_text()))
                if args.response_profile else None)
    if args.tool_recovery_attempts and not args.single_tool:
        parser.error("tool recovery requires --single-tool")
    # Use installed SDK metadata; no import-time remote price-map request.
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    caps = isolation()
    pin = MODEL_PROFILES[args.model_profile]
    if args.model_file.stat().st_size != pin["bytes"] or sha256(args.model_file) != pin["sha256"]:
        parser.error("model artifact differs from the pinned qualification profile")
    if args.memory_root and not os.environ.get("MEMORY_VAULT_DIR"):
        parser.error("normal-memory mode requires an explicit, read-only mounted MEMORY_VAULT_DIR")
    models = catalog(args.model_file, args.model_profile)
    source = Path(__file__).resolve().parents[1] / "src/harness"
    report = {"schema_version": 1, "observed_at": datetime.now(timezone.utc).isoformat(),
        "driver_sha256": sha256(Path(__file__)), "weights_sha256": pin["sha256"],
        "model_profile": args.model_profile, "model_pin": pin,
        "source_tree_sha256": hashlib.sha256(json.dumps(
            {str(p.relative_to(source)): sha256(p) for p in sorted(source.rglob("*.py"))},
            sort_keys=True).encode()).hexdigest(),
        "dependencies": {name: version(name) for name in ("litellm", "textual", "mcp", "httpx")},
        "expected_image": IMAGE, "runtime_version": subprocess.check_output(
            ["/app/llama-server", "--version"], cwd="/app", stderr=subprocess.STDOUT,
            text=True, timeout=10).strip(), "isolation": caps, "thresholds": THRESHOLDS,
        "catalog": models.entries, "provider": "real-local-model", "automatic_adoption": False,
        "held_out_semantic_qualification": False, "full_m3_qualified": False,
        "memory_server_sha256": sha256(args.memory_root / "lib/server.py") if args.memory_root else None}
    report["gpu"] = subprocess.check_output([
        "nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
        text=True, timeout=10).strip()
    report["runs"] = args.runs
    report["parallel_tool_calls"] = False if args.single_tool else None
    report["tool_recovery_attempts"] = args.tool_recovery_attempts
    report["response"] = response.model_dump(mode="json") if response else None
    report["cases"] = []
    for repeat in range(args.runs):
        with tempfile.TemporaryDirectory(prefix="harness-real-local-") as temp:
            rows = asyncio.run(run(Path(temp), models, args.memory_root,
                                   parallel_tool_calls=report["parallel_tool_calls"],
                                   tool_recovery_attempts=args.tool_recovery_attempts,
                                   response=response))
        report["cases"].extend({"repeat": repeat, **row} for row in rows)
    report["memory_peak_bytes"] = int(Path("/sys/fs/cgroup/memory.peak").read_text())
    report["passed"] = all(row["passed"] for row in report["cases"])
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
