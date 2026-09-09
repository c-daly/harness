"""Controlled destination frontend for the public A/B portability fixture.

Uses only Python's standard library, not Harness or its session store. The
explicit CLI flag authorizes precisely the fixture's B write; exported tool
arguments alone never authorize an action. This is not a general agent adapter.
"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--write-stage-b", action="store_true", required=True)
    args = parser.parse_args()
    assert importlib.util.find_spec("harness") is None
    with ZipFile(args.archive) as archive:
        package = json.loads(archive.read("continuation.json"))
        assert package["format"] == "harness-continuation" and package["version"] == 1
        for artifact in package["artifacts"]:
            data = archive.read(artifact["path"])
            assert len(data) == artifact["size"] and hashlib.sha256(data).hexdigest() == artifact["sha256"]
        assert "Continue this task" in archive.read("CONTINUE.md").decode()
        task = package["task"]
        assert task["unresolved"] == ["B", "review"] and not task["accepted"]
        requirements = {row["definition"]["id"]: row for row in task["requirements"]}
        assert requirements["A"]["status"] == "passed"
        assert requirements["review"]["definition"]["check"]["kind"] == "review"
        check = requirements["B"]["definition"]["check"]
        assert check["kind"] == "tool_result" and check["tool"] == "write_file"
        assert Path(check["args"]["file_path"]).name == "B.txt" and check["args"]["content"] == "stage B\n"
        ready = [row for row in package["context"] if row["status"] == "ready"]
        assert {row["source_id"] for row in ready} == {"project", "normal-memory"}
        memory = next(row for row in ready if row["source_id"] == "normal-memory")
        assert memory["reference"]["args"] == {"subject": "project"}
        assert archive.read(memory["snapshot"]["path"]) == b"Project memory: preserve completed stage A.\n"
    assert (args.workspace / "A.txt").read_text() == "stage A\n"
    with (args.workspace / "B.txt").open("x") as output:
        output.write("stage B\n")
    print(json.dumps({"source_task_id": task["id"], "remaining": ["review"],
                      "accepted": False, "harness_available": False}))


if __name__ == "__main__":
    main()
