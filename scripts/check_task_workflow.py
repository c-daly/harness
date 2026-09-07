"""Export a synthetic task journey and warm task-preparation timings.

Run from the checkout: uv run python -m scripts.check_task_workflow --output /tmp/task-probe
No models, network, plugins, private context or user workspace files are used.
"""

import argparse
import asyncio
import hashlib
import json
import platform
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from textual.widgets import Input

from harness.cli import build_kernel
from harness.events import CustomEvent
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.tools import ToolSpec
from harness.tui import HarnessApp
from harness.types import ModelId, ToolName


class VerifyFixture:
    spec = ToolSpec(name=ToolName("verify_fixture"), description="Fixed public fixture",
                    parameters={"type": "object"})

    async def __call__(self, args):
        return "PASS"


async def probe(output):
    report = {"provider": "scripted synthetic fixture", "plugins": False,
              "python": platform.python_version(), "size": [120, 36], "views": {}}
    with TemporaryDirectory(prefix="harness-task-ui-session-") as directory:
        kernel = build_kernel(base_dir=Path(directory), model=ModelId("scripted"),
            provider=FakeProvider([
                tool_call_turn("fixture", ToolName("verify_fixture"), {"target": "fixture"}),
                text_turn("The fixture returned PASS. The result is ready for your review."),
                text_turn("I changed the result. It needs another check and review."),
            ]))
        kernel.registry.register(VerifyFixture())
        app = HarnessApp(kernel)
        try:
            async with app.run_test(size=(120, 36)) as pilot:
                await pilot.pause(0.1)

                async def command(text):
                    app.query_one("#prompt", Input).value = text
                    await pilot.click("#prompt")
                    await pilot.press("enter")
                    await pilot.pause(0.15)

                def capture(label):
                    text = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
                    (output / f"{label}.txt").write_text(text)
                    (output / f"{label}.svg").write_text(app.export_screenshot(
                        title="Harness task evidence — synthetic provider"))
                    state = kernel.tasks.selected()
                    report["views"][label] = {"unresolved": list(state.unresolved),
                                             "accepted": state.accepted, "execution": state.execution}

                await command("/task new Verify a parser change")
                await command("/task require Review the behavior and error messages")
                await command("/task require-json " + json.dumps({
                    "id": "fixture", "description": "The fixed fixture returns PASS",
                    "check": {"kind": "tool_result", "tool": "verify_fixture", "args": {"target": "fixture"},
                              "sha256": hashlib.sha256(b"PASS").hexdigest()}}))
                await command("Run the fixture check.")
                await command("/task check")
                capture("unresolved")
                await command("/task confirm r1 I reviewed the behavior and error messages")
                await command("/task accept Reviewed and accepted")
                capture("accepted")
                await command("Change the result.")
                app.query_one("#prompt", Input).value = "unsent follow-up draft"
                await pilot.pause(0.1)
                capture("changed")
                report["draft_preserved"] = app.query_one("#prompt", Input).value == "unsent follow-up draft"
                for index in range(5000):
                    kernel.session.append(CustomEvent(namespace="synthetic", name="progress",
                        data={"i": index, "text": "x" * 256}))
                samples = []
                for _ in range(5):
                    started = time.perf_counter()
                    kernel.tasks.prepare("continue")
                    samples.append(round((time.perf_counter() - started) * 1000, 3))
                report["warm_preparation"] = {"extra_events": 5000, "milliseconds": samples}
        finally:
            kernel.session.close()
    report["passed"] = (report["draft_preserved"] and report["views"]["accepted"]["accepted"]
        and report["views"]["unresolved"]["unresolved"] == ["r1"]
        and report["views"]["changed"]["unresolved"] == ["r1", "fixture"]
        and not report["views"]["changed"]["accepted"])
    root = Path(__file__).resolve().parent.parent
    report["source_sha256"] = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [*sorted((root / "src/harness").glob("*.py")), Path(__file__).resolve()]}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = asyncio.run(probe(args.output))
    (args.output / "probe.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("passed", "views", "warm_preparation", "draft_preserved")}, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
