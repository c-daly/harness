"""Opt-in offline resident journey with provisioned 4B weights and normal memory.

Use the pinned Docker recipe in docs/resident-workflow.md. Reports contain only
metadata and public-fixture checks; temporary sessions and private memory output
are deleted. This narrow journey does not qualify a fallback or improvement.
"""

import argparse
import asyncio
import hashlib
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from textual.widgets import Input

from harness.cli import build_kernel
from harness.context import ContextPolicy, ContextSource, ResponsePolicy
from harness.mcp_config import McpServerSpec
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import TextDelta
from harness.provider_litellm import CatalogProvider
from harness.tui import HarnessApp
from harness.types import ModelId
from scripts.qualify_local import (
    FACTS, IMAGE, WEIGHTS_BYTES, WEIGHTS_SHA256, artifact_metadata, catalog, events, isolation,
    settled, sha256, tool_outcomes, until,
)


def make_kernel(root, models, memory_root, resume=None):
    source_tools = ("read_file", "write_file", "mcp__memory__memory_list")
    permissions = PermissionEngine([RuleSet(rules=[
        PermissionRule("allow", "model:local-small"), PermissionRule("allow", "read_file"),
        PermissionRule("allow", "write_file"),
        PermissionRule("allow", "mcp__memory__memory_list", {"subject": "harness"}),
    ], default="deny")])
    return build_kernel(base_dir=root / "sessions", provider=CatalogProvider(models), model=ModelId("local-small"),
        native_tools=True, workspace_root=root / "project", permissions=permissions, resume_session_id=resume,
        system_prompt="Use the supplied project context and available tools. Be brief and factual.",
        mcp=(McpServerSpec(name="memory", transport="stdio", command=str(memory_root / ".venv/bin/python"),
            args=("-B", str(memory_root / "lib/server.py")), env={"MEMORY_VAULT_DIR": "MEMORY_VAULT_DIR"},
            tools_allow=("memory_list",), restart="never", tool_timeout_s=10),),
        context_policy=None if resume else ContextPolicy(history_turns=1, tools=source_tools,
            response=ResponsePolicy(max_output_tokens=512), sources=(
                ContextSource(id="project-facts", tool="read_file", args={"file_path": "FACTS.json"}, required=True),
                ContextSource(id="normal-memory", tool="mcp__memory__memory_list", args={"subject": "harness"},
                              max_bytes=16384, required=True),)))


async def journey(root, models, memory_root):
    (root / "project").mkdir()
    (root / "project/FACTS.json").write_text(json.dumps(FACTS))
    report = {"checks": {}, "stage": "mount", "attempts": []}
    checks = report["checks"]
    session_id, task_id = None, None
    for resumed in (False, True):
        kernel = make_kernel(root, models, memory_root, session_id)
        kernel.loop.max_iterations = 6
        app = HarnessApp(kernel, native_tools=True, workspace_root=root / "project")
        try:
            async with app.run_test(size=(140, 45)) as pilot:
                await until(lambda: type(app.screen).__name__ == "ServerChecklistScreen")
                await pilot.press("enter")
                await until(lambda: app._bus_pump_worker is not None)
                composer = app.query_one("#prompt", Input)

                def screen():
                    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())

                async def submit(text):
                    composer.value = text
                    composer.post_message(Input.Submitted(composer, text))
                    await pilot.pause(0.05)

                if not resumed:
                    await submit("/task new Copy the current project facts")
                    await submit("/task require Review RESULT.json")
                    task_id = kernel.tasks.selected().definition.id
                    checks["composer_before_runtime_start"] = not composer.disabled and not any(
                        e.type == "local_runtime_requested" for e in events(kernel, root / "sessions"))
                else:
                    checks["task_and_sources_survive_restart"] = (
                        kernel.tasks.selected().definition.id == task_id and len(kernel.context_policy.sources) == 2)
                    checks["cancelled_attempt_visible_after_restart"] = "execution: cancelled" in screen()
                report["stage"] = "resumed-answer" if resumed else "project-task"
                prior_runs = sum(e.type == "agent_run_finished" for e in events(kernel, root / "sessions"))
                started = time.monotonic()
                await submit("Tell me the project name and retry limit from the supplied context. Do not use tools."
                    if resumed else "Using the supplied project-facts context, write RESULT.json containing only its "
                    "project and retry_limit fields. Then briefly confirm the values.")
                await until(lambda: sum(e.type == "agent_run_finished" for e in events(kernel, root / "sessions")) > prior_runs)
                await until(lambda: app.controller.active is None)
                await pilot.pause(0.1)
                log = events(kernel, root / "sessions")
                result = [e.result for e in log if e.type == "agent_run_finished"][-1]
                source_events = [e for e in log if e.type == "context_source_observed" and e.run_id == result.run_id]
                ready = [e for e in source_events if e.status == "ready"]
                memory = next((e for e in ready if e.source_id == "normal-memory"), None)
                report["attempts"].append({"resumed": resumed, "status": result.status,
                    "seconds": round(time.monotonic() - started, 3),
                    "sources": [{"id": e.source_id, "status": e.status, "bytes": e.byte_count,
                                 "sha256": e.result.sha256 if e.result else None} for e in source_events
                                if e.status != "fetching"]})
                label = "resumed" if resumed else "first"
                checks[label + "_sources_ready_once"] = len(ready) == 2 and len(source_events) == 4
                checks[label + "_normal_memory_nonempty"] = bool(memory and memory.result and
                    kernel.session.blobs.get(memory.result).decode().startswith("- type:"))
                checks[label + "_answer_visible"] = (result.status == "completed" and
                    "harbor" in screen() and "3" in screen())
                answer = result.read_text(kernel.session.blobs)
                checks[label + "_answer_contains_facts"] = "harbor" in answer and "3" in answer
                checks[label + "_settled"] = settled(kernel, root / "sessions")
                await submit("/status")
                await pilot.pause(0.1)
                checks[label + "_status_visible"] = ("Context normal-memory: ready" in screen() and
                    "Local runtimes:" in screen() and "local-small:" in screen() and "1/1 unresolved" in screen())
                if not resumed:
                    try:
                        artifact = json.loads((root / "project/RESULT.json").read_text())
                    except (OSError, ValueError):
                        artifact = None
                    checks["artifact_exact"] = artifact == FACTS
                    report["artifact"] = artifact_metadata(artifact)
                    report["tools"] = [{"tool": entry["tool"], "is_error": entry["is_error"]}
                                       for entry in tool_outcomes(kernel, root / "sessions")]
                    report["stage"] = "cancel-stream"
                    streamed = asyncio.Event()
                    previous = kernel.loop.on_chunk
                    def chunk(value):
                        previous(value)
                        if isinstance(value, TextDelta) and value.text:
                            streamed.set()
                    kernel.loop.on_chunk = chunk
                    composer.value = "Write 500 numbered sentences describing a harbor. Do not use tools."
                    composer.post_message(Input.Submitted(composer, composer.value))
                    await until(lambda: app.controller.active is not None)
                    composer.value = "unsent draft"
                    await asyncio.wait_for(streamed.wait(), 30)
                    started = time.monotonic()
                    app.action_interrupt()
                    await until(lambda: app.controller.active is None and not app._interrupting, seconds=2)
                    await pilot.pause(0.1)
                    report["cancel_seconds"] = round(time.monotonic() - started, 3)
                    checks["real_stream_cancelled"] = kernel.tasks.selected().execution == "cancelled"
                    checks["draft_preserved"] = composer.value == "unsent draft" and "unsent draft" in screen()
                    checks["cancel_settled"] = settled(kernel, root / "sessions")
                checks[label + "_unaccepted"] = not kernel.tasks.selected().accepted
                session_id = kernel.session.id
        except Exception as exc:
            report["error_type"] = type(exc).__name__
            checks["journey_completed"] = False
            break
        finally:
            kernel.session.close()
    else:
        checks["journey_completed"] = True
    report["passed"] = bool(checks) and all(checks.values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-root", type=Path, required=True)
    parser.add_argument("--model-file", type=Path, default=Path("/models/local.gguf"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bounds = isolation()
    if args.model_file.stat().st_size != WEIGHTS_BYTES or sha256(args.model_file) != WEIGHTS_SHA256:
        parser.error("requires the pinned provisioned 4B weights")
    if not (args.memory_root / ".venv/bin/python").is_file():
        parser.error("requires the preinstalled memory plugin")
    with tempfile.TemporaryDirectory(prefix="harness-resident-") as directory:
        report = asyncio.run(journey(Path(directory), catalog(args.model_file), args.memory_root))
    root = Path(__file__).resolve().parent.parent
    report.update(observed_at=datetime.now(timezone.utc).isoformat(), isolation=bounds, image=IMAGE,
        weights_sha256=WEIGHTS_SHA256, model_qualified=False, memory_writes=0,
        source_sha256={str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in [*sorted((root / "src/harness").glob("*.py")), Path(__file__).resolve()]})
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("passed", "stage", "checks", "attempts")}, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
