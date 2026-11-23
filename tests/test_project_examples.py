import subprocess

import pytest

from harness.agent import AgentTask
from harness.cli import build_kernel
from harness.improvement_cli import main, perform
from harness.improvement_examples import session_examples
from harness.improvement_feedback import record_feedback
from harness.improvement_journal import ImprovementJournal
from harness.project_binding import current_project, read_project_session
from harness.project_examples import index_prefix, project_examples
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.resume import resume_session
from harness.session import Session
from harness.types import ModelId, SessionId, ToolName
from tests.test_improvement_examples import record_lookup


def checkout(root, name, origin):
    workspace = root / name
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", origin], check=True)
    return workspace


async def test_scope_requires_a_local_repository_identity_and_never_consults_vault(tmp_path, monkeypatch):
    # An inaccessible/nonexistent vault has no bearing on Saoirse's private episodes.
    monkeypatch.setenv("MEMORY_VAULT_DIR", str(tmp_path / "missing-vault"))
    project = checkout(tmp_path, "alpha", "git@github.com:Acme/Alpha.git")
    (project / "subdir").mkdir()
    expected = await current_project(project)
    assert len(expected["git_common_dir"]) == 64
    assert await current_project(project / "subdir") == expected
    monkeypatch.setenv("GIT_DIR", str(project / ".git"))
    assert await current_project(None) is None
    assert await current_project(tmp_path) is None
    subprocess.run(["git", "-C", str(project), "remote", "remove", "origin"], check=True, env={})
    assert await current_project(project) == expected


async def test_native_same_project_recall_reuses_original_evidence_without_plugin_or_vault(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    vault = tmp_path / "absent-vault"
    monkeypatch.setenv("MEMORY_VAULT_DIR", str(vault))
    first = checkout(tmp_path, "first", "https://github.com/acme/alpha")
    subprocess.run(["git", "-C", str(first), "-c", "user.name=Test", "-c", "user.email=test@local",
                    "commit", "--allow-empty", "-qm", "Initial"], check=True)
    second = tmp_path / "linked"
    subprocess.run(["git", "-C", str(first), "worktree", "add", "--detach", "-q", str(second)], check=True)
    other = checkout(tmp_path, "other", "https://github.com/acme/alpha")
    (first / "register.txt").write_text("PRIVATE CONTENT")
    steps = [tool_call_turn("", ToolName("read_file"), {"file_path": "register.txt"}), text_turn("PRIVATE ANSWER")]
    source = build_kernel(provider=FakeProvider(steps), base_dir=state, model=ModelId("fake"), workspace_root=first,
                          native_tools=True)
    try:
        await source.loop.start()
        await source.loop.run_task(AgentTask(prompt="Which file governs the assignment?"))
        example, = session_examples(state, source.session.id)
        assert example.id in await perform(source, ["examples"])
        main(["--base-dir", str(state), source.session.id, "examples"])
        assert example.id in capsys.readouterr().out
        with pytest.raises(SystemExit):
            main(["--base-dir", str(state), source.session.id, "publish-example", example.id])
        assert "invalid choice" in capsys.readouterr().err
        # A second, still-open session can use the committed prefix.
        for path, expected in ((second, (example,)), (other, ()), (tmp_path, ())):
            target = build_kernel(provider=FakeProvider([]), base_dir=state, model=ModelId("fake"), workspace_root=path)
            try:
                await target.loop.start()
                assert await project_examples(target.loop) == expected
            finally:
                target.session.close()
        assert not vault.exists()
        index, = (state / "project-sessions").iterdir()
        assert all(line.split()[0] == source.session.id for line in index.read_text().splitlines())
        assert "PRIVATE" not in index.read_text() and "register" not in index.read_text()
    finally:
        source.session.close()


async def test_raw_cross_project_turn_is_never_read_through_a_forged_or_resumed_source(tmp_path, monkeypatch):
    root = checkout(tmp_path, "alpha", "https://github.com/acme/alpha")
    alpha = await current_project(root)
    beta = {"git_common_dir": "b" * 64}
    state = tmp_path / "state"
    with Session(state, SessionId("source")) as session:
        session.start(project=alpha)
        record_lookup(session, run="old", project=alpha)
    expected, = session_examples(state, session.id)
    resumed, _ = resume_session(state, session.id)
    try:
        record_lookup(resumed, run="private-beta", project=beta)
    finally:
        resumed.close()
    target = build_kernel(provider=FakeProvider([]), base_dir=state, model=ModelId("fake"), workspace_root=root)
    try:
        await target.loop.start()
        assert await project_examples(target.loop) == (expected,)
        # Deliberately extend the hint into a foreign run; even its first UserMessage
        # must never reach the decoder. No incomplete or foreign example may escape.
        from harness import project_binding
        parse = project_binding.parse_envelope_line
        def private_guard(line):
            if '"type":"user_message"' in line:
                assert "private-beta" not in line
            return parse(line)
        monkeypatch.setattr(project_binding, "parse_envelope_line", private_guard)
        path = state / "sessions" / "source.jsonl"
        size = path.stat().st_size
        index_prefix(state, alpha, "source", size, resumed.last_seq)
        assert not await project_examples(target.loop)
        assert read_project_session(state, "source", beta, prefix=(size, resumed.last_seq)) == []
    finally:
        target.session.close()


async def test_native_failure_receipt_is_available_after_a_same_project_resume(tmp_path):
    state = tmp_path / "state"
    root = checkout(tmp_path, "alpha", "https://github.com/acme/alpha")
    alpha = await current_project(root)
    with Session(state, SessionId("source")) as source:
        source.start(project=alpha)
        record_lookup(source, run="first", project=alpha)
    resumed, _ = resume_session(state, source.id)
    try:
        _, _, _, finish = record_lookup(resumed, run="last", project=alpha)
        record_feedback(ImprovementJournal(resumed), "The output did not establish which register governed.",
                        turn=finish, failure=True)
        target = build_kernel(provider=FakeProvider([]), base_dir=state, model=ModelId("fake"), workspace_root=root)
        try:
            await target.loop.start()
            examples = await project_examples(target.loop)
            assert any(e.evidence_id.startswith("failure:") and "which register" in e.outcome for e in examples)
        finally:
            target.session.close()
    finally:
        resumed.close()


async def test_legacy_origin_only_and_corrupt_indexes_cannot_grant_access(tmp_path):
    state = tmp_path / "state"
    root = checkout(tmp_path, "alpha", "https://github.com/acme/alpha")
    current = await current_project(root)
    legacy = {"repository": "acme/alpha", "entity": "10-projects/alpha"}
    with Session(state, SessionId("legacy")) as source:
        source.start(project=legacy)
        record_lookup(source, run="private-legacy", project=legacy)
    assert session_examples(state, source.id)  # Still available for explicit native session inspection.
    assert not (state / "project-sessions").exists()
    size = (state / "sessions/legacy.jsonl").stat().st_size
    index_prefix(state, current, "legacy", size, source.last_seq)
    index, = (state / "project-sessions").iterdir()
    with index.open("ab") as stream:
        stream.write(b"../../outside 42 10\nunfinished")
    target = build_kernel(provider=FakeProvider([]), base_dir=state, model=ModelId("fake"), workspace_root=root)
    try:
        await target.loop.start()
        assert not await project_examples(target.loop)
    finally:
        target.session.close()
