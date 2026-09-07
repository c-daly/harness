"""Offline test process: OpenAI inventory and streamed tool/answer protocol."""

import argparse
import json
import os
import signal
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--pid-file", type=Path, required=True)
parser.add_argument("--delay", type=float, default=0)
parser.add_argument("--model-id", default="test-model")
parser.add_argument("--ignore-term", action="store_true")
args = parser.parse_args()
if args.ignore_term:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
started = time.monotonic()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        loading = time.monotonic() - started < args.delay
        data = {"error": {"type": "unavailable_error", "message": "Loading model"}} if loading else (
            {"status": "ok"} if self.path.endswith("/health") else {"data": [{"id": args.model_id}]})
        payload = json.dumps(data).encode()
        self.send_response(503 if loading else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        tools = [m for m in body["messages"] if m["role"] == "tool"]
        if tools:
            assert "answer=42" in tools[-1]["content"]
            delta, finish = {"content": "The project answer is 42."}, "stop"
        else:
            delta = {"tool_calls": [{"index": 0, "id": "read-fact", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"file_path": "FACTS.txt"})}}]}
            finish = "tool_calls"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in [
            {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 8}},
        ]:
            chunk.update(id="local-fixture", object="chat.completion.chunk", model=args.model_id, created=1)
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
args.pid_file.write_text(str(os.getpid()))
server.serve_forever()
