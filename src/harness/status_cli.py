"""Inspect saved task, context, and local readiness without opening a provider."""

import argparse
from pathlib import Path

from harness.log import read_session
from harness.resident import render_status
from harness.types import SessionId


def main(argv):
    parser = argparse.ArgumentParser(prog="harness status", description=__doc__)
    parser.add_argument("session_id")
    parser.add_argument("--base-dir", type=Path, default=Path.home() / ".local/share/harness")
    args = parser.parse_args(argv)
    try:
        text = render_status(read_session(args.base_dir, SessionId(args.session_id), repair=False))
        print("".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32 and not 127 <= ord(ch) <= 159))
    except (OSError, ValueError) as exc:
        parser.error(f"status inspection failed ({type(exc).__name__})")
