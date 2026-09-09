"""Portability preserves obligations and bytes without re-executing source work."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from harness.agent import AgentResult
from harness.blobs import BlobIntegrityError, MissingBlobError
from harness.cli import build_kernel
from harness.context import ContextPolicy
from harness.events import (
    AgentRunFinished, AgentRunStarted, ContextPolicyConfigured, DispatchResolved,
    ModelCallProposed, ModelCallStarted, SubagentFinished, SubagentSpawned,
    ToolCallAborted, ToolCallCompleted, ToolCallProposed, UnknownEvent,
)
from harness.log import TornLogError, read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.portable import export_task, prepare_export, task_package
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.types import ModelId, ToolName
from tests.test_resident_context import Lookup


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
async def kernel(tmp_path):
    kernel = build_kernel(base_dir=tmp_path / "source", provider=FakeProvider([text_turn("done")]),
                          model=ModelId("fake"))
    await kernel.loop.start()
    kernel.tasks.create("Finish the project")
    kernel.tasks.add_requirement({"id": "output", "description": "Exact result",
        "check": {"kind": "output", "sha256": digest("done")}})
    kernel.tasks.add_requirement({"id": "review", "description": "Operator reviews the result"})
    try:
        yield kernel
    finally:
        kernel.session.close()


async def completed(kernel):
    await kernel.loop.run_task(kernel.tasks.prepare("Produce the expected result"))
    kernel.tasks.check()


def unpack(path):
    with ZipFile(path) as archive:
        return json.loads(archive.read("continuation.json")), {
            name: archive.read(name) for name in archive.namelist()}


async def test_export_is_reproducible_read_only_and_preserves_evidence(kernel, tmp_path):
    await completed(kernel)
    session = kernel.session
    before, calls = read_session(session.base, session.id), len(kernel.provider.calls)
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    export_task(session.base, session.id, first)
    export_task(session.base, session.id, second, task_id=kernel.tasks.selected().definition.id[:8])
    assert first.read_bytes() == second.read_bytes()
    package, files = unpack(first)
    assert set(package["task"]["unresolved"]) == {"review"}
    assert package["task"]["execution"] == "completed" and not package["task"]["accepted"]
    check = package["task"]["requirements"][0]
    assert check["status"] == "passed" and check["checked_seq"] == before[-1].seq
    assert check["evidence"]["source_seq"] < check["checked_seq"]
    assert files[check["evidence"]["artifact"]["path"]] == b"done"
    assert package["runs"][0]["messages"][0]["text"] == "Produce the expected result"
    assert package["source"]["through_seq"] == before[-1].seq
    assert len(package["artifacts"]) == 1  # Same output/evidence is copied once.
    assert first.stat().st_mode & 0o777 == 0o600
    assert read_session(session.base, session.id) == before and len(kernel.provider.calls) == calls


async def test_acceptance_and_invalidation_are_distinct_from_execution(kernel, tmp_path):
    await completed(kernel)
    kernel.tasks.confirm("review", "I inspected it")
    kernel.tasks.accept("Approved")
    package, _ = task_package(kernel.session.base, kernel.session.id)
    assert package["task"]["accepted"] and package["task"]["acceptance"]["note"] == "Approved"
    kernel.provider.script.append(text_turn("changed"))
    await kernel.loop.run_task(kernel.tasks.prepare("Revise the result"))
    package, _ = task_package(kernel.session.base, kernel.session.id)
    assert not package["task"]["accepted"] and package["task"]["acceptance"] is None
    assert package["task"]["unresolved"] == ["output", "review"]
    assert all(row["evidence"] is None and row["confirmation"] is None for row in package["task"]["requirements"])
    assert len(package["runs"]) == 2


@pytest.mark.parametrize("damage", ["missing", "corrupt", "symlink", "oversize"])
async def test_bad_artifact_prevents_publication(kernel, tmp_path, monkeypatch, damage):
    await completed(kernel)
    ref = kernel.tasks.selected().evidence["output"].artifact
    path = kernel.session.blobs._root / ref.sha256
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"fake")
    elif damage == "symlink":
        target = tmp_path / "external"
        target.write_bytes(b"done")
        path.unlink()
        path.symlink_to(target)
    else:
        monkeypatch.setattr("harness.portable.MAX_EXPORT_BYTES", 3)
    output = tmp_path / "bad.zip"
    with pytest.raises((MissingBlobError, BlobIntegrityError, ValueError)):
        export_task(kernel.session.base, kernel.session.id, output)
    assert not output.exists()


@pytest.mark.parametrize("kind", ["file", "symlink", "race"])
async def test_export_never_overwrites_destination(kernel, tmp_path, monkeypatch, kind):
    import harness.portable as portable
    output = tmp_path / "existing.zip"
    original = b"keep this work"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(original)
        output.symlink_to(target)
    elif kind == "file":
        output.write_bytes(original)
    else:
        prepare = portable.prepare_export

        def concurrent_publish(*args, **kwargs):
            data = prepare(*args, **kwargs)
            output.write_bytes(original)
            return data

        monkeypatch.setattr(portable, "prepare_export", concurrent_publish)
    with pytest.raises(FileExistsError):
        export_task(kernel.session.base, kernel.session.id, output)
    assert output.read_bytes() == original
    assert not list(tmp_path.glob(".*.tmp"))


async def test_other_task_content_and_provider_configuration_are_excluded(kernel):
    await completed(kernel)
    wanted = kernel.tasks.selected().definition.id
    kernel.tasks.create("unrelated private objective")
    kernel.provider.script.append(text_turn("unrelated private output"))
    await kernel.loop.run_task(kernel.tasks.prepare("unrelated private request"))
    data = prepare_export(kernel.session.base, kernel.session.id, task_id=wanted)
    assert b"unrelated private" not in data
    assert b"handoff_scope" not in data and b"permissions" not in json.dumps(task_package(
        kernel.session.base, kernel.session.id, task_id=wanted)[0]["runs"]).encode()


@pytest.mark.parametrize("damage", ["active", "unknown", "torn"])
async def test_unsettled_or_unreadable_records_are_not_repaired_by_export(kernel, tmp_path, damage):
    session = kernel.session
    if damage == "active":
        session.append(AgentRunStarted(task_id=kernel.tasks.selected().definition.id,
                                      run_id="running", runtime="fixture"))
    elif damage == "unknown":
        session.append(UnknownEvent(raw={"type": "future_acceptance"}))
    else:
        with (session.base / "sessions" / f"{session.id}.jsonl").open("ab") as output:
            output.write(b'{"partial":')
    log = session.base / "sessions" / f"{session.id}.jsonl"
    before = log.read_bytes()
    with pytest.raises((ValueError, TornLogError)):
        export_task(session.base, session.id, tmp_path / "refused.zip")
    assert log.read_bytes() == before and not log.with_suffix(".torn").exists()


async def test_effects_and_child_references_are_explicitly_uninspected(kernel):
    session, task_id = kernel.session, kernel.tasks.selected().definition.id
    session.append(AgentRunStarted(task_id=task_id, run_id="external", runtime="fixture",
                                  capabilities={"credentials": "private-config"}))
    session.append(ModelCallProposed(call_id="model", model="external", agent_run_id="external"))
    session.append(ModelCallStarted(call_id="model", model="external", execution_kind="agent"))
    session.append(ToolCallProposed(call_id="write", tool="write_file", args={"file_path": "wrong"},
                                   agent_run_id="external"))
    session.append(DispatchResolved(call_id="write", kind="tool", tool="write_file", args={"file_path": "A"}))
    session.append(ToolCallAborted(call_id="write", reason="process died"))
    session.append(ToolCallProposed(call_id="child", tool="dispatch_agent", args={}, agent_run_id="external"))
    session.append(SubagentSpawned(call_id="child", child_session_id="child-session"))
    session.append(SubagentFinished(child_session_id="child-session", status="incomplete"))
    session.append(AgentRunFinished(result=AgentResult(task_id=task_id, run_id="external", status="failed")))
    package, _ = task_package(session.base, session.id)
    assert package["tool_calls"][0]["args"] == {"file_path": "A"}
    assert package["tool_calls"][0]["outcome"] == "aborted"
    assert package["tool_calls"][0]["effects"] == "inspect"
    assert package["external_executions"][0]["effects"] == "uninspected"
    assert package["child_sessions"][0]["contents"] == "not_exported"
    assert package["child_sessions"][0]["status"] == "incomplete"
    assert "private-config" not in json.dumps(package)


@pytest.mark.parametrize("ambiguity", ["proposal", "resolution", "terminal"])
async def test_ambiguous_effect_provenance_refuses_export(kernel, ambiguity):
    session, task_id = kernel.session, kernel.tasks.selected().definition.id
    session.append(AgentRunStarted(task_id=task_id, run_id="run", runtime="fixture"))
    proposal = ToolCallProposed(call_id="tool", tool="write_file", args={}, agent_run_id="run")
    resolution = DispatchResolved(call_id="tool", kind="tool", tool="write_file", args={})
    terminal = ToolCallCompleted(call_id="tool", result_text="done")
    for event in (proposal, resolution, terminal):
        session.append(event)
    session.append({"proposal": proposal.model_copy(update={"agent_run_id": "unrelated"}),
                    "resolution": resolution, "terminal": terminal}[ambiguity])
    session.append(AgentRunFinished(result=AgentResult(task_id=task_id, run_id="run", status="completed")))
    with pytest.raises(ValueError, match="ambiguous"):
        prepare_export(session.base, session.id)


async def test_cli_routes_without_loading_provider_and_sanitizes_failure(kernel, tmp_path, monkeypatch, capsys):
    from harness.cli import main
    output = tmp_path / "export.zip"
    monkeypatch.setattr(sys, "argv", ["harness", "export", kernel.session.id, str(output),
                                    "--base-dir", str(kernel.session.base)])
    main()
    assert output.exists() and "Source session unchanged" in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2 and "choose a new file path" in capsys.readouterr().err
    assert not kernel.provider.calls


async def test_missing_blob_directory_is_not_created_by_inspection(kernel):
    root = kernel.session.blobs._root
    root.rmdir()
    with pytest.raises(ValueError, match="artifact directory is missing"):
        prepare_export(kernel.session.base, kernel.session.id)
    assert not root.exists()


async def test_markdown_keeps_recorded_markup_and_controls_inside_quoted_data(kernel):
    from markdown_it import MarkdownIt
    kernel.tasks.create("Title\n# Spoofed heading\n\x1b[2J\n```\n![remote](https://invalid.example/pixel)")
    kernel.tasks.add_requirement({"id": "review", "description": "# Fake criterion heading\n\n![remote](https://invalid.example/pixel)"})
    data = prepare_export(kernel.session.base, kernel.session.id)
    import io
    with ZipFile(io.BytesIO(data)) as archive:
        markdown = archive.read("CONTINUE.md").decode()
    assert "Spoofed heading" in markdown and "\x1b" not in markdown
    tokens = MarkdownIt().parse(markdown)
    assert [t.tag for t in tokens if t.type == "heading_open"] == ["h1", "h2", "h2", "h2", "h2"]
    assert not any(child.type == "image" for token in tokens for child in token.children or [])


async def test_handoff_inspection_notes_travel_without_execution_grants(tmp_path):
    from tests.test_handoff import source, specification
    kernel, _ = await source(tmp_path)
    try:
        record = kernel.handoffs.record(specification(kernel))
        package, _ = task_package(kernel.session.base, kernel.session.id)
        exported = package["reconciliations"][0]
        assert exported["id"] == record.id and exported["source_run_id"] == record.source_run_id
        assert exported["resolutions"] and exported["effects"]
        assert {r["status"] for r in exported["resolutions"]} <= {"completed", "not_applied", "uncertain"}
        assert "allowed_calls" not in exported and "scope" not in exported
        assert "destination_digest" not in exported
    finally:
        kernel.session.close()


async def test_stdlib_destination_continues_with_no_source_database(tmp_path):
    source, workspace = tmp_path / "source", tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "PROJECT.md").write_text("Stages A and B; keep an operator review outstanding.\n")
    lookup = Lookup("Project memory: preserve completed stage A.\n")
    profile = ContextPolicy.model_validate({"sources": [
        {"id": "project", "tool": "read_file", "args": {"file_path": "PROJECT.md"}, "required": True},
        {"id": "normal-memory", "tool": "memory_lookup", "args": {"subject": "project"}, "required": True}]})
    provider = FakeProvider([tool_call_turn("A", ToolName("write_file"),
                            {"file_path": "A.txt", "content": "stage A\n"}), text_turn("Stage A is written; B and review remain.")])
    kernel = build_kernel(base_dir=source, provider=provider, model=ModelId("fake"), native_tools=True,
        workspace_root=workspace, context_policy=profile,
        permissions=PermissionEngine([RuleSet(rules=[PermissionRule("allow", "*")], default="deny")]))
    kernel.registry.register(lookup)
    try:
        await kernel.loop.start()
        task = kernel.tasks.create("Write A and B, then obtain operator review")
        for stage in ("A", "B"):
            kernel.tasks.add_requirement({"id": stage, "description": f"Write stage {stage}", "check": {
                "kind": "tool_result", "tool": "write_file", "args": {"file_path": str(workspace / f"{stage}.txt"), "content": f"stage {stage}\n"},
                "sha256": digest(f"Created {workspace / (stage + '.txt')} (1 lines).")}})
        kernel.tasks.add_requirement({"id": "review", "description": "Operator reviews both files"})
        await kernel.loop.run_task(kernel.tasks.prepare("Complete stage A, leaving B for the next interface"))
        assert kernel.tasks.check().unresolved == ("B", "review")
        # Later configuration is distinct from the historical source reference.
        kernel.session.append(ContextPolicyConfigured(policy=ContextPolicy()))
        package_path = tmp_path / "continuation.zip"
        export_task(source, kernel.session.id, package_path)
        package, files = unpack(package_path)
        assert package["configured_sources"] == []
        ready = [row for row in package["context"] if row["status"] == "ready"]
        assert len(ready) == 2 and ready[1]["reference"]["args"] == {"subject": "project"}
        assert files[ready[1]["snapshot"]["path"]] == lookup.text.encode()
        assert len(provider.calls) == 2 and lookup.calls == [{"subject": "project"}]
        assert package["task"]["id"] == task.id
    finally:
        kernel.session.close()
    shutil.rmtree(source)  # Only the archive and copied workspace reach the destination.
    destination = tmp_path / "destination"
    shutil.copytree(workspace, destination)
    before_a = (destination / "A.txt").stat().st_mtime_ns
    script = Path(__file__).parent / "fixtures/continue_export.py"
    result = subprocess.run([sys.executable, "-I", "-S", str(script), str(package_path), str(destination),
                             "--write-stage-b"], text=True, capture_output=True, timeout=10, cwd=destination)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["source_task_id"] == task.id and receipt["remaining"] == ["review"]
    assert (destination / "B.txt").read_text() == "stage B\n"
    assert (destination / "A.txt").read_text() == "stage A\n"
    assert (destination / "A.txt").stat().st_mtime_ns == before_a
    assert not (workspace / "B.txt").exists() and not source.exists()
    assert not receipt["accepted"] and receipt["harness_available"] is False
    assert unpack(package_path)[0]["task"]["unresolved"] == ["B", "review"]
