"""Small reproducible feasibility probe; no tools, project data, or credentials.

Uses llama.cpp's documented chat schema and template controls. This measures
an already-running service, not cold startup, agent ability, or a held-out eval.
"""

import argparse
import json
import time
import urllib.request


def request_json(url, data=None, timeout=30):
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if not 1 <= args.samples <= 20 or args.timeout <= 0:
        parser.error("samples must be 1..20 and timeout must be positive")
    base = args.base_url.rstrip("/")
    model = request_json(base + "/models", timeout=args.timeout)["data"][0]["id"]
    cases = [
        {
            "name": "acknowledgement",
            "system": "Classify the user's message: question, acknowledgement, stop, or uncertain. "
                      "An acknowledgement does not prove that the task is complete.",
            "user": "Thanks!",
            "schema": {"type": "object", "properties": {"kind": {"type": "string", "enum":
                       ["question", "acknowledgement", "stop", "uncertain"]}},
                       "required": ["kind"], "additionalProperties": False},
            "expected": {"kind": "acknowledgement"},
        },
        {
            "name": "provided_context",
            "system": "Use only the supplied project facts. Return the requested facts as JSON.",
            "user": "Project facts: the local endpoint listens on port 8080. Its cached model "
                    "works without internet. Return port and offline (a boolean).",
            "schema": {"type": "object", "properties": {"port": {"type": "integer"},
                       "offline": {"type": "boolean"}}, "required": ["port", "offline"],
                       "additionalProperties": False},
            "expected": {"port": 8080, "offline": True},
        },
    ]
    rows = []
    for case in cases:
        for sample in range(args.samples):
            started = time.monotonic()
            row = {"case": case["name"], "sample": sample}
            try:
                response = request_json(base + "/chat/completions", {
                    "model": model,
                    "messages": [{"role": "system", "content": case["system"]},
                                 {"role": "user", "content": case["user"]}],
                    "max_tokens": 128, "temperature": 0, "stream": False,
                    "response_format": {"type": "json_schema", "json_schema": {
                        "name": case["name"], "strict": True, "schema": case["schema"],
                    }},
                    "chat_template_kwargs": {"enable_thinking": False},
                    "reasoning_effort": "none",
                }, timeout=args.timeout)
                choice = response["choices"][0]
                row.update({"finish_reason": choice.get("finish_reason"),
                            "content_chars": len(choice["message"].get("content") or ""),
                            "reasoning_chars": len(choice["message"].get("reasoning_content") or ""),
                            "usage": response.get("usage"), "timings": response.get("timings")})
                row["result"] = json.loads(choice["message"]["content"])
                row["passed"] = row["result"] == case["expected"]
            except Exception as exc:
                row.update({"passed": False, "error_type": type(exc).__name__})
            row["wall_ms"] = round((time.monotonic() - started) * 1000, 2)
            rows.append(row)
    print(json.dumps({"schema_version": 1, "model": model, "observed_at": time.time(),
                      "request_profile": "nested_json_schema_reasoning_none_v1",
                      "mode": "existing_service_feasibility", "samples": rows,
                      "qualified": False}, indent=2))
    return 0 if all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
