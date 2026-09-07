"""Opt-in transport lifecycle probe using the pinned offline local model.

Use the container recipe in docs/inference-client-lifecycle.md. This checks
resource ownership, not model quality. All prompts are synthetic; logs are
temporary and the saved report contains counts, timings and source hashes.
"""

import argparse
import asyncio
import json
import os
import tempfile
import time
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

from harness.inference import InferenceRequest
from harness.messages import Message
from harness.provider import TextDelta
from harness.types import ModelId
from scripts.qualify_local import (IMAGE, WEIGHTS_BYTES, WEIGHTS_SHA256, catalog, close,
                                   events, isolation, make_kernel, settled, sha256)


async def probe(root, models, report, clients, sessions):
    root.mkdir(parents=True, exist_ok=True)
    kernel = make_kernel(root / "sessions", root, models)
    report["observations"] = rows = []

    def checkpoint(name):
        rows.append({"name": name, "http_clients_created": len(clients),
                     "http_clients_open": sum(not c.is_closed for c in clients),
                     "aiohttp_sessions_created": len(sessions),
                     "aiohttp_sessions_open": sum(not s.closed for s in sessions)})

    async def call(text, timeout=45, on_chunk=None, tokens=32):
        return await kernel.loop.dispatcher.dispatch_inference(provider=kernel.provider,
            request=InferenceRequest(model=ModelId("local-small"), purpose="client-probe",
                messages=(Message.user_text(text),), timeout_seconds=timeout,
                max_output_tokens=tokens, temperature=0), on_chunk=on_chunk)

    task = None
    try:
        await kernel.loop.start()
        async with asyncio.timeout(120):
            for index in range(24):
                await call("Reply with exactly OK.", timeout=45 + index / 100)
                checkpoint(f"request-{index}")
            seen = asyncio.Event()
            task = asyncio.create_task(call("Count from 1 to 1000, one number per line.",
                on_chunk=lambda chunk: seen.set() if isinstance(chunk, TextDelta) else None,
                tokens=2048))
            await asyncio.wait_for(seen.wait(), 15)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                report["stream_cancelled"] = True
            checkpoint("cancelled-stream")
            await call("Reply with exactly OK.")
            checkpoint("after-cancellation")
            report["session_settled"] = settled(kernel, root / "sessions")
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await close(kernel)
        checkpoint("kernel-closed")
        report["owned_runtime_stopped"] = any(e.type == "resource_observed"
            and e.observation.status == "stopped" for e in events(kernel, root / "sessions"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-file", type=Path, default=Path("/models/local.gguf"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    caps = isolation()
    if args.model_file.stat().st_size != WEIGHTS_BYTES or sha256(args.model_file) != WEIGHTS_SHA256:
        parser.error("model differs from the pinned local fixture")
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    import httpx
    import litellm
    from aiohttp import ClientSession

    cache_before = {id(value) for value in litellm.in_memory_llm_clients_cache.cache_dict.values()}
    # Import the SDK before observing request-created clients. Retain references
    # so GC cannot hide missing explicit close calls; never close clients here.
    clients, sessions = [], []
    client_init, session_init = httpx.AsyncClient.__init__, ClientSession.__init__

    def track_client(self, *a, **kw):
        client_init(self, *a, **kw)
        clients.append(self)

    def track_session(self, *a, **kw):
        session_init(self, *a, **kw)
        sessions.append(self)

    repo = Path(__file__).resolve().parents[1]
    report = {"schema_version": 1, "kind": "local-client-lifecycle", "isolation": caps,
        "image": IMAGE, "weights_sha256": WEIGHTS_SHA256,
        "source": {str(p.relative_to(repo)): sha256(p) for p in
                   [Path(__file__), repo / "scripts/qualify_local.py", *sorted((repo / "src/harness").glob("*.py"))]},
        "dependencies": {name: version(name) for name in ("litellm", "openai", "httpx", "aiohttp")},
        "model_quality_qualified": False}
    started = time.monotonic()
    with patch.object(httpx.AsyncClient, "__init__", track_client), \
            patch.object(ClientSession, "__init__", track_session), \
            tempfile.TemporaryDirectory(prefix="harness-client-probe-") as temp:
        asyncio.run(probe(Path(temp), catalog(args.model_file), report, clients, sessions))
    report["elapsed_seconds"] = time.monotonic() - started
    cache_after = {id(value) for value in litellm.in_memory_llm_clients_cache.cache_dict.values()}
    report["sdk_clients_cached_before_requests"] = len(cache_before)
    report["sdk_clients_cached_after_requests"] = len(cache_after)
    report["new_cached_sdk_clients"] = len(cache_after - cache_before)
    report["passed"] = (len(report["observations"]) == 27 and "error_type" not in report
        and report.get("stream_cancelled") and report.get("session_settled")
        and report["owned_runtime_stopped"] and not report["new_cached_sdk_clients"]
        and all(r["http_clients_open"] == r["aiohttp_sessions_open"] == 0
                for r in report["observations"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("passed", "elapsed_seconds", "new_cached_sdk_clients")}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
