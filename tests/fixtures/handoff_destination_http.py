"""Public scripted HTTP stream for destination process-loss tests; no inference."""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

project, mode, port = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        data = json.dumps({"data": [{"id": "local-small"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send(delta, finish=None):
            data = {"id": "public-fixture", "object": "chat.completion.chunk", "created": 1,
                    "model": "local-small", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            payload = ("data: " + json.dumps(data) + "\n\n").encode()
            self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
            self.wfile.flush()

        try:
            prompt = next(m["content"] for m in reversed(request["messages"]) if m["role"] == "user")
            if isinstance(prompt, list):
                prompt = " ".join(p.get("text", "") for p in prompt)
            letter = "C" if prompt.startswith("Write C.txt") else "B"
            if not prompt.startswith("List the integers") and not (project / f"{letter}.txt").exists():
                send({"tool_calls": [{"index": 0, "id": "write-" + letter, "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"file_path": str(project / f"{letter}.txt"),
                                                                  "content": f"stage {letter}\n"})}}]})
                send({}, "tool_calls")
            elif prompt.startswith("List the integers") or (mode != "busy" and letter == "B"):
                for number in range(10000):
                    send({"content": str(number) + "\n"})
                    time.sleep(.02)
                send({}, "stop")
            else:
                send({"content": "Stage completed"})
                send({}, "stop")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
