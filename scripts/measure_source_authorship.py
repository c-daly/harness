"""Opt-in live source authoring on three small, operator-seeded maintenance defects.

Models author patches. The operator owns fixtures, fixed checks and supervised
selection/rollback. Each case gets an exclusive repository and journal; failures
are retained. This is integration evidence, not general coding qualification.
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import subprocess
import time
from contextlib import aclosing
from pathlib import Path

from harness.catalog import Catalog
from harness.cli import build_kernel
from harness.events import AgentRunFinished, AgentRunStarted, ErrorRaised, ModelCallCompleted
from harness.improvement import Evidence, SourceEntrypoint, verdict
from harness.improvement_journal import read_improvements
from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import TextDelta
from harness.provider_litellm import CatalogProvider
from harness.source_authorship import SourceAuthorSpec, author_source
from harness.source_improvement import _process, _run_check, _snapshot, run_source_evaluation
from harness.source_promotion import adopt_source, launch_source, rollback_source

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    dict(id="utf8-prefix", request="Fix clip(text, limit): return the longest Unicode prefix whose UTF-8 "
         "encoding fits limit bytes. limit is a nonnegative integer. Preserve all characters that fit. "
         "Keep main() working and make no unrelated changes.",
         source="def clip(text, limit):\n    return text[:limit]\n\ndef main():\n    print(clip('aé終', 4))\n",
         regression="assert clip('abc', 2) == 'ab'\nassert clip('', 0) == ''\n",
         held_out="assert clip('aé終', 4) == 'aé'\nassert clip('終x', 2) == ''\nassert clip('é', 2) == 'é'\n",
         launch_incumbent="aé終", launch_candidate="aé"),
    dict(id="retry-boundary", request="Fix retryable(jobs): include only failed jobs with attempts strictly "
         "less than max_attempts, sorted by ID. Keep main() working and make no unrelated changes.",
         source="def retryable(jobs):\n    return sorted(j['id'] for j in jobs if j['status'] == 'failed' "
         "and j['attempts'] <= j['max_attempts'])\n\ndef main():\n    print(retryable([{'id': 'done', "
         "'status': 'failed', 'attempts': 3, 'max_attempts': 3}]))\n",
         regression="assert retryable([]) == []\nassert retryable([dict(id='b', status='failed', attempts=1, "
         "max_attempts=3), dict(id='a', status='succeeded', attempts=1, max_attempts=3)]) == ['b']\n",
         held_out="assert retryable([dict(id='a', status='failed', attempts=3, max_attempts=3)]) == []\n"
         "assert retryable([dict(id='z', status='failed', attempts=1, max_attempts=2), dict(id='b', "
         "status='failed', attempts=0, max_attempts=1)]) == ['b', 'z']\n",
         launch_incumbent="['done']", launch_candidate="[]"),
    dict(id="interpreter-identity", request="Fix interpreter_key(path): normalize redundant dot/parent path "
         "components to an absolute path, while preserving which virtual environment's interpreter path "
         "was specified. Two different venv paths must remain distinct even if they symlink to the same "
         "binary. Keep main() working and make no unrelated changes.",
         source="from pathlib import Path\n\ndef interpreter_key(path):\n    return str(Path(path).resolve())\n"
         "\ndef main():\n    print('interpreter-key')\n",
         regression="from pathlib import Path\nassert interpreter_key('env/../env/bin/python') == "
         "str(Path.cwd() / 'env/bin/python')\n",
         held_out="from pathlib import Path\nreal = Path.cwd() / 'real'\nreal.write_text('binary')\n"
         "for name in ('one', 'two'):\n    Path(name).mkdir()\n    Path(name, 'python').symlink_to(real)\n"
         "assert interpreter_key('one/python') != interpreter_key('two/python')\n"
         "assert interpreter_key('one/../one/python') == str(Path.cwd() / 'one/python')\n",
         launch_incumbent="interpreter-key", launch_candidate="interpreter-key"),
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.STDOUT).decode().strip()


async def run_case(root, catalog, alias, case):
    root.mkdir()
    repo = root / "repository"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Source authoring measurement")
    git(repo, "config", "user.email", "measurement@example.invalid")
    (repo / "entry.py").write_text(case["source"])
    git(repo, "add", "entry.py")
    git(repo, "commit", "-qm", "Operator-seeded maintenance defect")
    revision = git(repo, "rev-parse", "HEAD")
    (repo / "private.txt").write_text("Untracked source work must be preserved.\n")
    status = git(repo, "status", "--porcelain")
    provider = CatalogProvider(catalog)
    captured, streams = [], []
    infer = provider.infer

    async def observe(request):
        captured.append(request.model_dump(mode="json"))
        preview = {"text": "", "truncated": False}
        streams.append(preview)
        received = 0
        async with aclosing(infer(request)) as stream:
            async for chunk in stream:
                if isinstance(chunk, TextDelta):
                    data = chunk.text.encode()
                    preview["text"] += data[:max(0, request.max_output_bytes - received)].decode(errors="replace")
                    received += len(data)
                    preview["truncated"] = received > request.max_output_bytes
                yield chunk

    provider.infer = observe
    kernel = build_kernel(base_dir=root / "data", model=alias, provider=provider,
        permissions=PermissionEngine([RuleSet(rules=[PermissionRule("allow", f"model:{alias}")], default="deny")]),
        pricing_for=lambda model: catalog.resolve(model).pricing_dict(),
        execution_overrides={"max_model_calls": 1, "max_tool_calls": 0})
    prefix = "import sys\nsys.path.insert(0, '.')\nfrom entry import *\n"
    spec = SourceAuthorSpec(repository=str(repo), incumbent_revision=revision,
        request=case["request"], editable_paths=("entry.py",), evidence_ids=("observed-defect",),
        suite=dict(cases=[dict(id=p, partition=p, critical=True, script=prefix + case[p])
                         for p in ("regression", "held_out")],
                   max_latency_ratio=5, max_case_latency_ms=5000))
    report = {"case": case["id"], "model": alias, "session_id": str(kernel.session.id),
        "specification": spec.model_dump(mode="json"), "seeded_source": case["source"],
        "authorship": "not_started", "evaluation": "not_run", "activation_qualified": False}
    began = time.monotonic()
    try:
        await kernel.loop.start()
        baseline = await _run_check(await _snapshot(repo, revision), spec.suite.cases[1], spec.suite)
        report["baseline"] = baseline
        if baseline["status"] != "completed" or baseline["returncode"] == 0:
            raise ValueError("seeded defect did not reproduce as a completed failing check")
        event = kernel.session.append(ErrorRaised(where="source-authoring-measurement",
            message=f"The fixed {case['id']} behavioral check failed with exit {baseline['returncode']}."))
        kernel.improvements.record(Evidence(id="observed-defect", source_session=kernel.session.id,
            source_seq=event.seq, category="failure", observation=event.event.message))
        plan = await author_source(kernel, spec)
        report["authorship_seconds"] = time.monotonic() - began
        report["authorship"] = "candidate_recorded"
        report["plan_id"] = plan.id
        result = await run_source_evaluation(kernel.improvements, plan.id)
        report["evaluation"] = verdict(plan, result)
        report["result"] = result.model_dump(mode="json")
        report["checks"] = json.loads(kernel.session.blobs.get(result.artifact))
        if report["evaluation"] == "passed":
            selected = adopt_source(kernel.improvements, result.id, "trial", SourceEntrypoint(module="entry", function="main"))
            report["adoption"] = selected.model_dump(mode="json")
            report["launches"] = []
            for phase in ("candidate", "incumbent"):
                if phase == "incumbent":
                    report["rollback"] = rollback_source(kernel.improvements, "trial").model_dump(mode="json")
                command, tree = launch_source(kernel.session.base, kernel.session.id, "trial", [], execute=False)
                launch = await _process(command, cwd=repo, env=dict(os.environ), timeout=30, limit=65536)
                report["launches"].append(dict(phase=phase, returncode=launch["returncode"], status=launch["status"],
                    stdout=launch["stdout"].decode(), stderr=launch["stderr"].decode(), tree=str(tree),
                    expected=case["launch_" + phase], matches=launch["returncode"] == 0 and launch["status"] == "completed"
                    and launch["stdout"].decode().strip() == case["launch_" + phase]))
    except Exception as exc:
        report["error_type"] = type(exc).__name__  # no credential-bearing provider bodies
    finally:
        await kernel.resources.close(emit=kernel.session.append)
        await kernel.loop.end()
        events = read_session(kernel.session.base, kernel.session.id)
        report["runs"] = [e.event.model_dump(mode="json") for e in events
                          if isinstance(e.event, (AgentRunStarted, AgentRunFinished, ModelCallCompleted))]
        state = read_improvements(kernel.session.base, kernel.session.id)
        if report["authorship"] != "candidate_recorded":
            report["authorship"] = ("response_refused" if "completed" in state.source_author_runs.values()
                                     else next(iter(state.source_author_runs.values()), "not_started"))
        report["intents"] = [v.model_dump(mode="json") for v in state.source_authorings.values()]
        report["requests"] = captured
        report["response_previews"] = streams
        report["source_preserved"] = (git(repo, "status", "--porcelain") == status
                                      and (repo / "entry.py").read_text() == case["source"])
        report["duration_seconds"] = time.monotonic() - began
        kernel.session.close()
        (root / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return {k: report.get(k) for k in ("case", "model", "authorship", "evaluation", "error_type", "source_preserved")}


async def run(args):
    catalog = Catalog.load(args.catalog)
    for alias in args.model:
        resolved = catalog.resolve(alias)
        if resolved.execution_kind != "inference" or resolved.local is None:
            raise ValueError("this local measurement requires explicitly configured local inference aliases")
    args.output.mkdir(parents=True, exist_ok=False)
    hashes = {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in
              [*sorted((ROOT / "src/harness").glob("*.py")), Path(__file__).resolve()]}
    summary = {"python": platform.python_version(), "source_hashes": hashes,
        "scope": "Three operator-seeded maintenance defects; one proposal per model/case; no normal-memory qualification.",
        "cases": []}
    for index, alias in enumerate(args.model):
        for case in CASES:
            item = await run_case(args.output / f"{index}-{case['id']}", catalog, alias, case)
            summary["cases"].append(item)
            print(json.dumps(item), flush=True)
            (args.output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    assert all(sha((ROOT / p).read_bytes()) == value for p, value in hashes.items()), "source changed during measurement"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
