"""Read-only offline plumbing check against the installed memory plugin.

Uses a scripted provider, never a real model or memory writes. Run inside an
isolated network namespace to establish offline transport. Reports metadata only;
temporary session logs containing retrieved memory are removed on completion.
"""

import argparse
import asyncio
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from harness.cli import build_kernel, run_once
from harness.context import ContextPolicy
from harness.fold import fold
from harness.log import read_session
from harness.mcp_config import McpServerSpec
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.types import ModelId, ToolName


def _validate_subject(subject: str) -> None:
    if not subject.strip():
        raise ValueError("subject must contain non-whitespace characters")


async def check(root, memory_root, subject):
    _validate_subject(subject)
    (root / "FACTS.txt").write_text("project-marker=local-context-check\n")
    rows = []
    for memory_enabled in (False, True):
        tools = ("read_file",) + (("mcp__memory__memory_list",) if memory_enabled else ())
        specs = (McpServerSpec(name="memory", transport="stdio",
            command=str(memory_root / ".venv/bin/python"),
            args=("-B", str(memory_root / "lib/server.py")),
            tools_allow=("memory_list",), restart="never", tool_timeout_s=10),) if memory_enabled else ()
        script = [tool_call_turn("read fixture", ToolName("read_file"), {"file_path": "FACTS.txt"})]
        if memory_enabled:
            script.append(tool_call_turn("read scoped memory", ToolName("mcp__memory__memory_list"),
                                         {"subject": subject}))
        script.append(text_turn("plumbing check complete"))
        provider = FakeProvider(script)
        permissions = PermissionEngine([RuleSet(rules=[
            PermissionRule("allow", "model:scripted"), PermissionRule("allow", "read_file"),
            PermissionRule("allow", "mcp__memory__memory_list", {"subject": subject}),
        ], default="deny")])
        base = root / ("memory" if memory_enabled else "no-plugins")
        kernel = build_kernel(base_dir=base, provider=provider, model=ModelId("scripted"),
            native_tools=True, workspace_root=root, permissions=permissions, mcp=specs,
            context_policy=ContextPolicy(history_turns=1, tools=tools))
        await run_once(kernel, "Check the local project fixture and configured scoped memory.")
        events = read_session(base, kernel.session.id)
        outcomes = [e.event for e in events if e.event.type == "tool_call_completed"]
        assert len(outcomes) == len(tools) and all(not e.is_error for e in outcomes)
        assert "project-marker=local-context-check" in outcomes[0].result_text
        memory_text = outcomes[-1].result_text if memory_enabled else ""
        if memory_enabled:
            assert memory_text.startswith("- type:"), "normal vault has no scoped memory entries"
        configured = {s.name for s in kernel.loop.registry.specs()}
        assert configured == set(tools)
        resumed = build_kernel(base_dir=base, provider=FakeProvider([text_turn("resumed")]),
            model=ModelId("scripted"), resume_session_id=kernel.session.id,
            native_tools=True, workspace_root=root, permissions=permissions, mcp=specs)
        assert resumed.context_policy == kernel.context_policy
        await run_once(resumed, "Continue the same task.")
        state = fold(read_session(base, kernel.session.id))
        assert not state.open_intents and not state.open_model_intents and not state.open_agent_runs
        assert len(state.messages) == 2 * len(tools) + 4
        assert state.messages[0].text() == "Check the local project fixture and configured scoped memory."
        assert state.messages[-1].text() == "resumed"
        prepared = [e.event for e in read_session(base, kernel.session.id)
                    if e.event.type == "context_prepared"]
        assert prepared[-1].omitted_turns == 1
        rows.append({"memory_enabled": memory_enabled, "passed": True,
            "completed_tool_calls": len(outcomes), "memory_bytes": len(memory_text.encode()),
            "memory_sha256": hashlib.sha256(memory_text.encode()).hexdigest() if memory_enabled else None,
            "max_prepared_input_bytes": max(e.input_bytes for e in prepared),
            "resume_preserved_profile": True, "full_history_retained": True})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-root", type=Path, required=True)
    parser.add_argument("--subject", default="harness")
    parser.add_argument("--output", type=Path, help="Write the metadata-only report to this file.")
    args = parser.parse_args()
    try:
        _validate_subject(args.subject)
    except ValueError as exc:
        parser.error(f"--subject: {exc}")
    # No provisioning/bootstrap: these must already exist in the installed plugin.
    if not (args.memory_root / ".venv/bin/python").is_file():
        parser.error("memory plugin's preinstalled Python is unavailable")
    if not (args.memory_root / "lib/server.py").is_file():
        parser.error("memory plugin server is unavailable")
    routes = Path("/proc/net/route").read_text().splitlines()[1:]
    with tempfile.TemporaryDirectory(prefix="harness-context-check-") as temp:
        rows = asyncio.run(check(Path(temp), args.memory_root.resolve(), args.subject))
    report = json.dumps({"schema_version": 1, "observed_at": datetime.now(timezone.utc).isoformat(),
        "provider": "scripted", "qualified_model": False, "ipv4_routes_present": bool(routes),
        "memory_writes": 0, "memory_server_sha256": hashlib.sha256(
            (args.memory_root / "lib/server.py").read_bytes()).hexdigest(), "cases": rows}, indent=2)
    if args.output:
        args.output.write_text(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
