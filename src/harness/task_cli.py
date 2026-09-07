"""Read durable task obligations without opening a provider or changing the log."""

import argparse
from pathlib import Path

from harness.log import read_session
from harness.tasks import project_tasks, render_task
from harness.types import SessionId


def main(argv):
    parser = argparse.ArgumentParser(prog="harness tasks", description=__doc__)
    parser.add_argument("session_id")
    parser.add_argument("--base-dir", type=Path, default=Path.home() / ".local/share/harness")
    parser.add_argument("--task", help="One task ID or unique ID prefix; default shows all tasks.")
    args = parser.parse_args(argv)
    try:
        state = project_tasks(read_session(args.base_dir, SessionId(args.session_id), repair=False))
        tasks = list(state.items.values())
        if args.task is not None:
            tasks = [task for task in tasks if args.task and task.definition.id.startswith(args.task)]
            if len(tasks) != 1:
                parser.error("--task must identify exactly one task")
        if not tasks:
            print("No tracked tasks in this session.")
        for task in tasks:
            text = ("Selected task: " if task.definition.id == state.selected_id else "Task: ") + render_task(task)
            # Plain output: strip C0/C1 terminal controls while retaining line breaks.
            print("".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32 and not 127 <= ord(ch) <= 159))
    except (OSError, ValueError) as exc:
        parser.error(f"task inspection failed ({type(exc).__name__})")
