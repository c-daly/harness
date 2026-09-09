"""Export one tracked task for continuation outside Harness, without inference."""

import argparse
from pathlib import Path

from harness.blobs import BlobIntegrityError, MissingBlobError
from harness.log import TornLogError
from harness.portable import ExportError, export_task


def failure_message(exc):
    if isinstance(exc, ExportError):
        return str(exc)
    if isinstance(exc, FileExistsError):
        return "destination already exists; choose a new file path"
    if isinstance(exc, (BlobIntegrityError, MissingBlobError)):
        return "a recorded artifact is missing or corrupt; inspect the source session"
    if isinstance(exc, TornLogError):
        return "the session log is damaged; recover it before exporting"
    if isinstance(exc, FileNotFoundError):
        return "source session or destination parent is missing"
    return f"check source records and destination ({type(exc).__name__})"


def main(argv):
    parser = argparse.ArgumentParser(prog="harness export", description=__doc__)
    parser.add_argument("session_id")
    parser.add_argument("output", type=Path, help="New ZIP path; its parent must already exist.")
    parser.add_argument("--base-dir", type=Path, default=Path.home() / ".local/share/harness")
    parser.add_argument("--task", help="Task ID or unique prefix; defaults to the selected task.")
    args = parser.parse_args(argv)
    try:
        export_task(args.base_dir, args.session_id, args.output, task_id=args.task)
    except (OSError, ValueError, BlobIntegrityError, MissingBlobError, TornLogError) as exc:
        parser.error("export failed: " + failure_message(exc))
    print("Exported CONTINUE.md, continuation.json and recorded artifacts. Source session unchanged.")
