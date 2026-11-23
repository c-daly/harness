"""Offline qualification of a Harness executable using a Linux PTY, no model service."""

import argparse
import base64
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import tempfile
import termios
import time


CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
OSC = re.compile(rb"\x1b\].*?(?:\x07|\x1b\\)", re.S)
URL = "https://example.org/terminal?guide=1#help"
CODE = b'  printf "%s\\n" "one"  \n\tprintf "%s\\n" "two"\n'
LAST_LINE = "LastReadmeLineReached"


class Terminal:
    def __init__(self, args, cwd, env, width):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 32, width, 0, 0))
                os.chdir(cwd)
                os.execve(args[0], args, env)
            finally:
                os._exit(127)
        self.buffer = b""
        self.status = None

    def read(self, timeout):
        if select.select([self.fd], [], [], timeout)[0]:
            try:
                chunk = os.read(self.fd, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                chunk = b""
            if chunk:
                self.buffer = (self.buffer + chunk)[-1024 * 1024:]
            else:
                time.sleep(0.01)

    def expect(self, *markers, raw=False, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            observed = self.buffer if raw else CSI.sub(b"", OSC.sub(b"", self.buffer))
            if all(marker in observed for marker in markers):
                return
            self.read(0.1)
        tail = CSI.sub(b"", OSC.sub(b"", self.buffer))[-2500:].decode(errors="replace")
        raise ValueError(f"PTY did not show {markers!r}. Last terminal output:\n{tail}")

    def expect_picker(self, destination):
        self.expect(b"Copy (c)", b"Back (Esc)", destination)

    def send(self, data):
        self.buffer = b""
        os.write(self.fd, data)

    def finish(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            child, status = os.waitpid(self.pid, os.WNOHANG)
            if child:
                self.status = status
                if os.waitstatus_to_exitcode(status) != 0:
                    raise ValueError(f"Harness exited with {os.waitstatus_to_exitcode(status)}")
                return
            self.read(0.05)
        raise TimeoutError("Harness did not exit after /quit")

    def close(self):
        os.close(self.fd)
        if self.status is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(self.pid, 0)


def events(path):
    return [json.loads(line)["event"] for line in path.read_text().splitlines()]


def exercise(binary, work, width, color):
    home = work / "home"
    home.mkdir()
    project = work / "project"
    docs = project / "docs"
    chapters = docs / "chapters"
    chapters.mkdir(parents=True)
    index, shelf = docs / "index.md", chapters / "README.md"
    index.write_text(f"# EntryIndex\n\n[Shelf](chapters/README.md#details)\n\n[Online]({URL})\n")
    steps = "\n\n".join(
        f"Check {i:03d}. Read the release log, verify the recorded inventory count, and compare "
        f"the signed delivery receipt with sample {hashlib.sha256(str(i).encode()).hexdigest()[:20]}."
        for i in range(150)
    )
    shelf.write_text("# Shelf\n\n" + "Shelf inventory.\n\n" * 38
                     + "## Details\n\nTerminalLandingMarker\n\n```bash\n" + CODE.decode()
                     + "```\n\n" + steps + "\n\n" + LAST_LINE + "\n")
    before = [path.read_bytes() for path in (index, shelf)]
    if len(before[1].split()) < 3000 or len(before[1]) < 16385:
        raise ValueError("README fixture must exceed 3,000 words and the inline journal limit")
    browser = work / "record-browser"
    browser.write_text("#!/usr/bin/python3\nimport os, sys\nfrom pathlib import Path\n"
                       "with Path(os.environ['HARNESS_BROWSER_RECEIPT']).open('a') as out:\n"
                       "    print(sys.argv[1], file=out)\n")
    browser.chmod(0o700)
    handoffs = work / "browser-receipt"
    env = {
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CACHE_HOME": str(home / ".cache"),
        "PATH": f"{binary.parent}:/usr/bin:/bin", "TERM": "xterm-256color", "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "BROWSER": f"{browser} %s", "HARNESS_BROWSER_RECEIPT": str(handoffs),
    }
    if not color:
        env["NO_COLOR"] = "1"
    state = work / "state"
    args = [str(binary), "--demo", "--no-mcp", "--no-plugins", "--no-context-profile",
            "--base-dir", str(state), "--workspace", str(project)]
    prompt = "See [BookEntry](docs/index.md)."
    seed = subprocess.run([*args, "-p", prompt],
                          cwd=project, env=env, text=True, capture_output=True, timeout=45)
    if seed.returncode:
        raise ValueError(f"Demo seed failed: {seed.stderr[-1000:]}")
    if seed.stdout.strip() != f"echo: {prompt}":
        raise ValueError("Demo seed did not deliver the expected link")
    journal, = (state / "sessions").glob("*.jsonl")
    sid = journal.stem
    from_count = len(events(journal))
    terminal = Terminal([*args, "--resume", sid], str(project), env, width)
    try:
        terminal.expect(b"BookEntry", seconds=40)
        terminal.send(b"/links\r")
        terminal.expect_picker(b"docs/index.md")
        terminal.send(b"\r")
        terminal.expect(b"EntryIndex", b"Links (l)")
        terminal.send(b"l")
        terminal.expect_picker(b"chapters/README.md#details")
        terminal.send(b"\r")
        terminal.expect(b"TerminalLandingMarker", b"Copy code (y)")
        terminal.send(b"y")
        terminal.expect(b"\x1b]52;c;" + base64.b64encode(CODE) + b"\x07", raw=True)
        terminal.send(b"\x1b[F")
        terminal.expect(LAST_LINE.encode())
        terminal.send(b"\x1b")
        terminal.expect(b"EntryIndex", b"Links (l)")
        terminal.send(b"l")
        terminal.expect_picker(b"chapters/README.md#details")
        terminal.send(b"\x1b[B")
        terminal.expect(URL.encode())
        terminal.send(b"c")
        terminal.expect(b"\x1b]52;c;" + base64.b64encode(URL.encode()) + b"\x07", raw=True)
        if handoffs.exists():
            raise ValueError("Copy triggered the browser")
        terminal.send(b"\r")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (handoffs.is_file() and handoffs.stat().st_size):
            terminal.read(0.05)
        if not handoffs.exists() or handoffs.read_text().splitlines() != [URL]:
            raise ValueError("Wrong browser handoff count or destination")
        terminal.send(b"\x1b")
        terminal.expect(b"BookEntry")
        terminal.send(b"/quit\r")
        terminal.finish()
    finally:
        terminal.close()
    if handoffs.read_text().splitlines() != [URL]:
        raise ValueError("More than one browser request was emitted")
    observed = events(journal)[from_count:]
    calls = [e for e in observed if e["type"] == "tool_call_proposed"]
    if len(calls) != 2 or [path.read_bytes() for path in (index, shelf)] != before:
        raise ValueError("Unexpected tool count or changed source")
    for call, path in zip(calls, (index, shelf), strict=True):
        trace = [e for e in observed if e.get("call_id") == call["call_id"]]
        resolved = [e for e in trace if e["type"] == "dispatch_resolved"]
        if (call["tool"] != "read_file" or call["purpose"] != "context" or len(resolved) != 1
                or resolved[0]["args"]["file_path"] != str(path)
                or not any(e["type"] == "hook_decided" and e["hook"] == "permissions"
                           and e["decision"]["kind"] == "allow" for e in trace[:trace.index(resolved[0])])
                or trace[-1]["type"] != "tool_call_completed" or trace[-1]["is_error"]
                or not trace[-1].get("operator_view")):
            raise ValueError(f"Missing ordered read authorization for {path.name}")
        if path == shelf:
            blob = trace[-1].get("result_blob") or {}
            if (blob.get("sha256") != hashlib.sha256(before[1]).hexdigest()
                    or blob.get("size") != len(before[1])):
                raise ValueError("The native read receipt did not cover the full README")
    if any(e["type"] == "model_call_proposed" for e in observed):
        raise ValueError("Navigating the resumed session invoked inference")
    return dict(terminal=f"Linux PTY {width}x32", no_color=not color, resume="echo demo",
                section="TerminalLandingMarker", readme_bytes=len(before[1]), readme_words=len(before[1].split()),
                readme_sha256=hashlib.sha256(before[1]).hexdigest(), last_line=LAST_LINE,
                code_clipboard_bytes=len(CODE), code_clipboard_sha256=hashlib.sha256(CODE).hexdigest(),
                reads=["docs/index.md", "docs/chapters/README.md"], copied_osc52=URL,
                browser_handoffs=[URL], navigation_model_calls=0, source_unchanged=True,
                journal_sha256=hashlib.sha256(journal.read_bytes()).hexdigest())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, help="Installed Harness console executable")
    parser.add_argument("--width", type=int, default=52)
    parser.add_argument("--color", action="store_true")
    args = parser.parse_args()
    if not 48 <= args.width <= 180:
        parser.error("Choose a terminal width from 48 to 180.")
    with tempfile.TemporaryDirectory(prefix="harness-links-pty-") as directory:
        receipt = exercise(args.binary.absolute(), Path(directory), args.width, args.color)
    print(json.dumps(receipt, indent=2))
