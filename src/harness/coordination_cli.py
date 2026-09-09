"""Inspect saved coordination outcomes without reopening agents or providers."""

import argparse
from pathlib import Path

from harness.blobs import BlobIntegrityError, BlobStore, MissingBlobError
from harness.coordination import load_report
from harness.events import CoordinationFinished
from harness.log import TornLogError, read_session


def render_coordination(base, session_id):
    events = read_session(base, session_id, repair=False)
    blobs = BlobStore(base / "sessions" / session_id / "blobs", create=False)
    rows = []
    for env in events:
        event = env.event
        if not isinstance(event, CoordinationFinished):
            continue
        report = load_report(blobs, event, session_id).model_dump(mode="json")
        rows.append(f"{event.strategy} {event.id[:8]}: {event.status}; acceptance unverified; event {env.seq}")
        rows.append(f"  Selection: {report['gate']}; disagreement: {report['disagreement']}")
        for member in report["members"]:
            result = member["result"]
            detail = (f"; child {result['child_session_id']}" if result.get("child_session_id") else "")
            if result.get("run_id"):
                detail += f"; run {result['run_id']}"
            if result.get("truncated"):
                detail += "; truncated output"
            rows.append(f"  {member['role']} {member['model']}: {result['status']}{detail}")
        rows.extend(f"  Unresolved: {note}" for note in report["unresolved"])
        if report["output"]:
            rows.append(f"  Recorded answer: {report['output']['sha256']} (this session's blobs)")
        rows.append(f"  Report: {event.report.sha256}")
    text = "\n".join(rows) if rows else "No recorded coordination outcomes."
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32 and not 127 <= ord(c) <= 159)


def main(argv):
    parser = argparse.ArgumentParser(prog="harness coordination", description=__doc__)
    parser.add_argument("session_id")
    parser.add_argument("--base-dir", type=Path, default=Path.home() / ".local/share/harness")
    args = parser.parse_args(argv)
    try:
        print(render_coordination(args.base_dir, args.session_id))
    except (OSError, ValueError, KeyError, BlobIntegrityError, MissingBlobError, TornLogError) as exc:
        parser.error(f"coordination inspection failed ({type(exc).__name__})")
