import asyncio
import json
import tempfile
import time
from pathlib import Path
from harness.agent import TaskLimits
from scripts.check_resident_workflow import make_kernel
from scripts.qualify_local import FACTS, catalog, close, events, isolation


async def run(root, repeat):
    (root / "project").mkdir(parents=True)
    (root / "project/FACTS.json").write_text(json.dumps(FACTS))
    k = make_kernel(
        root, catalog(Path("/models/local.gguf")), Path("/home/fearsidhe/.claude/plugins/memory")
    )
    row = {"repeat": repeat}
    try:
        await k.mcp.start(only={"memory"})
        await k.loop.start()
        k.mcp.flush_events()
        k.tasks.create("Copy the current project facts")
        task = k.tasks.prepare(
            "Using the supplied project-facts context, write RESULT.json containing only its project and retry_limit fields. Then briefly confirm the values."
        )
        task = task.model_copy(
            update={
                "limits": TaskLimits(
                    max_iterations=6,
                    timeout_seconds=45,
                    max_input_bytes=32768,
                    max_output_tokens=512,
                )
            }
        )
        started = time.monotonic()
        result = await k.loop.run_task(task)
        row.update(status=result.status, seconds=round(time.monotonic() - started, 3))
        answer = result.read_text(k.session.blobs)
        row["answer_facts"] = all(str(v) in answer for v in FACTS.values())
        log = events(k, root / "sessions")
        proposed = {e.call_id: e for e in log if e.type == "tool_call_proposed"}
        resolved = {e.call_id: e for e in log if e.type == "dispatch_resolved" and e.kind == "tool"}
        rows = []
        for e in log:
            if e.type != "tool_call_completed":
                continue
            r = resolved.get(e.call_id)
            p = proposed[e.call_id]
            args = r.args if r else p.args
            path = args.get("file_path")
            basename = Path(path).name if isinstance(path, str) else None
            payload = (
                k.session.blobs.get(e.result_blob).decode()
                if e.result_blob
                else e.result_text or ""
            )
            rows.append(
                {
                    "tool": str(r.tool if r else p.tool),
                    "purpose": p.purpose,
                    "keys": sorted(args),
                    "path_category": basename
                    if basename
                    in (
                        "FACTS.json",
                        "RESULT.json",
                        "project-facts",
                        "project-facts.json",
                        "normal-memory",
                    )
                    else "other"
                    if path
                    else None,
                    "error_categories": [
                        key
                        for key in (
                            "not found",
                            "does not exist",
                            "No such file",
                            "outside",
                            "file_path",
                            "Is a directory",
                            "requires",
                            "Permission",
                            "ENOENT",
                            "cannot read",
                        )
                        if e.is_error and key.lower() in payload.lower()
                    ],
                    "is_error": e.is_error,
                    "missing_file": e.is_error and "No such file" in payload,
                    "output_bytes": len(payload.encode()),
                }
            )
        row["tools"] = rows
        try:
            obj = json.loads((root / "project/RESULT.json").read_text())
        except (OSError, ValueError):
            obj = None
        row["artifact_exact"] = obj == FACTS
    except Exception as exc:
        row["error_type"] = type(exc).__name__
    finally:
        await close(k)
    return row


async def main():
    rows = []
    with tempfile.TemporaryDirectory() as d:
        for repeat in range(3):
            row = await run(Path(d) / str(repeat), repeat)
            rows.append(row)
            Path("/reports/m3-diagnosis-provenance.json").write_text(
                json.dumps({"isolation": isolation(), "cases": rows}, indent=2) + "\n"
            )


asyncio.run(main())
