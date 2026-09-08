"""Pilot the actual CLI startup path with a real local runtime; only display is virtual."""
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from textual.widgets import Input

import harness.cli
import harness.tui
from harness.fold import fold
from harness.log import read_session

OUT = Path(__file__).parent
MODE = sys.argv[1]
REPORT = {"stage": MODE}
USER_CATALOG = Path.home() / ".config/harness/models.toml"
BEFORE = hashlib.sha256(USER_CATALOG.read_bytes()).hexdigest()


def visible(app):
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


async def launch(kernel, **kwargs):
    app = harness.tui.HarnessApp(kernel, **kwargs)
    try:
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause(0.1)
            prompt = app.query_one("#prompt", Input)
            if MODE == "select":
                assert kernel.loop.model == "echo"
                prompt.value = "/model continuity-cpu"
                await pilot.press("enter")
                async with asyncio.timeout(10):
                    while kernel.loop.model != "continuity-cpu":
                        await asyncio.sleep(0.05)
                question = ("For this session, the project codename is amberfern. "
                            "Reply with exactly CONTINUITY_SET and no other text. Do not use tools.")
                expected = "CONTINUITY_SET"
            else:
                assert kernel.resumed and kernel.loop.model == "continuity-cpu"
                question = ("What project codename did I give you? Reply with exactly that "
                            "codename and no other text. Do not use tools.")
                expected = "amberfern"
            assert kernel.loop.model_pinned
            assert kernel.runner.default_model == "continuity-cpu"
            REPORT["restored_before_dispatch"] = MODE != "select"
            start = time.monotonic()
            prompt.value = question
            await pilot.press("enter")
            prompt.value = "unsent continuation draft"
            async with asyncio.timeout(240):
                while app.controller.active is not None or (app._turn_worker is not None and not app._turn_worker.is_finished):
                    await asyncio.sleep(0.1)
            await pilot.pause(0.2)
            REPORT["turn_seconds"] = round(time.monotonic() - start, 3)
            events = read_session(kernel.session.base, kernel.session.id)
            calls = [e.event for e in events if e.event.type == "model_call_completed"]
            answer = "".join(b.get("text", "") for b in calls[-1].message["blocks"])
            assert answer == expected, repr(answer)
            assert app.controller.last_result.status == "completed"
            assert expected in visible(app) and "continuity-cpu" in visible(app)
            assert prompt.value == "unsent continuation draft"
            assert "unsent continuation draft" in visible(app)
            (OUT / f"{MODE}-terminal.svg").write_text(app.export_screenshot())
            REPORT.update(session_id=str(kernel.session.id), model=str(kernel.loop.model),
                          answer=answer, draft_preserved=True, compositor_verified=True)
            await app._finish()
    finally:
        app.kernel.session.close()
    events = read_session(kernel.session.base, kernel.session.id)
    REPORT["runtime_actions"] = [e.event.action for e in events if e.event.type == "local_runtime_requested"]
    REPORT["selection"] = fold(events).model_selection.model_dump()
    REPORT["user_catalog_unchanged"] = BEFORE == hashlib.sha256(USER_CATALOG.read_bytes()).hexdigest()
    assert REPORT["user_catalog_unchanged"]
    (OUT / f"{MODE}-report.json").write_text(json.dumps(REPORT, indent=2) + "\n")


harness.tui.run_tui = launch
os.chdir(OUT)
sys.argv = ["harness", "--base-dir", str(OUT / "sessions"), "--catalog", str(OUT / "models.toml"),
            "--workspace", str(OUT), "--no-mcp", "--no-plugins"]
if MODE == "resume":
    sys.argv.append("--continue")
harness.cli.main()
print(json.dumps(REPORT, indent=2))
