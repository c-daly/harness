"""Fault and cross-boundary regressions for the log/blob unit of truth."""

import os
import subprocess
import sys

import pytest

from harness.blobs import BlobIntegrityError, BlobRef, BlobStore, MissingBlobError
from harness.dispatcher import Dispatcher
from harness.events import UserMessage
from harness.hooks import HookBus, ProposedToolCall
from harness.interaction import HeadlessResolver
from harness.log import SessionLockedError, TornLogError, read_session
from harness.messages import ImageBlock, Message, Role, ToolCallBlock
from harness.errors import ProviderError
from harness.provider import text_turn
from harness.provider_claude_code import _render_prompt
from harness.provider_litellm import _messages_to_openai
from harness.resume import resume_session
from harness.session import Session
from harness.tools import ToolRegistry, ToolSpec
from harness.types import CallId, ModelId, SessionId, ToolName


def test_modified_blob_is_rejected(tmp_path):
    store = BlobStore(tmp_path)
    ref = store.put(b"original")
    (tmp_path / ref.sha256).write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="integrity"):
        store.get(ref)
    with pytest.raises(RuntimeError, match="integrity"):
        store.put(b"original")


def test_wrong_blob_size_is_rejected(tmp_path):
    store = BlobStore(tmp_path)
    ref = store.put(b"original")
    with pytest.raises(RuntimeError, match="integrity"):
        store.get(ref.model_copy(update={"size": 1}))


def test_blob_digest_cannot_escape_store(tmp_path):
    with pytest.raises(ValueError):
        BlobRef(sha256="../outside", size=1)


def test_blob_symlink_is_rejected_even_if_target_bytes_match(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    ref = store.put(b"valid bytes")
    target = tmp_path / "outside"
    target.write_bytes(b"valid bytes")
    path = tmp_path / "blobs" / ref.sha256
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(BlobIntegrityError):
        store.get(ref)
    with pytest.raises(BlobIntegrityError):
        store.put(b"valid bytes")


def test_bypassed_blob_validation_still_cannot_escape_store(tmp_path):
    ref = BlobRef.model_construct(sha256="../outside", size=1)
    with pytest.raises(ValueError):
        BlobStore(tmp_path / "blobs").get(ref)


def test_partial_utf8_tail_is_quarantined_as_bytes(tmp_path):
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        session.append(UserMessage(text="valid λ prefix"))
    path = tmp_path / "sessions/s.jsonl"
    prefix = path.read_bytes()
    tail = b'{"event":{"text":"\xe2\x82'
    path.write_bytes(prefix + tail)
    events = read_session(tmp_path, SessionId("s"), repair=True)
    assert events[-1].event.text == "valid λ prefix"
    assert path.read_bytes() == prefix
    assert (tmp_path / "sessions/s.torn").read_bytes() == tail


def test_interrupted_repair_keeps_original_log(tmp_path, monkeypatch):
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
    path = tmp_path / "sessions/s.jsonl"
    original = path.read_bytes() + b'{"torn'
    path.write_bytes(original)
    real_replace = os.replace

    def interrupted(source, target, **kwargs):
        if os.fspath(target) == os.fspath(path):
            raise OSError("injected before publish")
        return real_replace(source, target, **kwargs)

    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(OSError, match="injected"):
        read_session(tmp_path, SessionId("s"), repair=True)
    assert path.read_bytes() == original
    assert (tmp_path / "sessions/s.torn").read_bytes() == b'{"torn'


def test_resume_holds_ownership_across_read_and_sequence_selection(tmp_path, monkeypatch):
    import harness.resume as module

    with Session(tmp_path, SessionId("s")) as session:
        session.start()
    real_read = module.read_session
    blocked = []

    def racing_read(*args, **kwargs):
        events = real_read(*args, **kwargs)
        try:
            with Session(tmp_path, SessionId("s"), start_seq=events[-1].seq) as other:
                other.append(UserMessage(text="racing append"))
        except SessionLockedError:
            blocked.append(True)
        return events

    monkeypatch.setattr(module, "read_session", racing_read)
    resumed, _ = resume_session(tmp_path, SessionId("s"))
    resumed.close()
    assert blocked == [True]
    events = real_read(tmp_path, SessionId("s"))
    assert len({event.seq for event in events}) == len(events)


def test_real_process_crash_releases_guard_and_resume_repairs_tail(tmp_path):
    code = """
import os, sys
from pathlib import Path
from harness.session import Session
from harness.types import SessionId
s = Session(Path(sys.argv[1]), SessionId('s'))
s.start()
s._writer._fh.write('{"partial')
s._writer._fh.flush()
os._exit(9)
"""
    crashed = subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=False)
    assert crashed.returncode == 9
    session, _ = resume_session(tmp_path, SessionId("s"))
    with session:
        with pytest.raises(SessionLockedError):
            resume_session(tmp_path, SessionId("s"))
    assert [event.seq for event in read_session(tmp_path, SessionId("s"))] == [1, 2]
    assert (tmp_path / "sessions/s.torn").read_bytes() == b'{"partial'


def test_complete_json_without_delimiter_is_quarantined_before_append(tmp_path):
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        session.append(UserMessage(text="uncommitted line"))
    path = tmp_path / "sessions/s.jsonl"
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    with pytest.raises(TornLogError):
        read_session(tmp_path, SessionId("s"))
    resumed, messages = resume_session(tmp_path, SessionId("s"))
    resumed.close()
    assert messages == []
    assert len(read_session(tmp_path, SessionId("s"))) == 2


def test_failed_recovery_releases_lock_for_retry(tmp_path):
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
    path = tmp_path / "sessions/s.jsonl"
    with path.open("ab") as output:
        output.write(b'{"partial')
    quarantine = tmp_path / "sessions/s.torn"
    quarantine.write_bytes(b"prior evidence")
    with pytest.raises(TornLogError):
        resume_session(tmp_path, SessionId("s"))
    quarantine.rename(quarantine.with_suffix(".previous"))
    resumed, _ = resume_session(tmp_path, SessionId("s"))
    resumed.close()


def test_text_bridge_preserves_call_identity_errors_and_empty_results():
    messages = [
        Message(role=Role.ASSISTANT, blocks=(ToolCallBlock(
            call_id=CallId("c"), tool=ToolName("read"), args={"path": "example"},
        ),)),
        Message.tool_result(CallId("c"), text="", is_error=True),
    ]
    rendered = _render_prompt(messages)
    assert '[tool call c read]: {"path": "example"}' in rendered
    assert "[tool result c error]: " in rendered
    assert _messages_to_openai(messages)[1]["content"] == ""


@pytest.mark.parametrize("render", [_render_prompt, _messages_to_openai])
def test_consumers_reject_unresolved_blobs_and_images(render):
    ref = BlobRef(sha256="a" * 64, size=1)
    with pytest.raises(ProviderError, match="resolved"):
        render([Message.tool_result(CallId("c"), blob=ref)])
    with pytest.raises(ProviderError, match="image"):
        render([Message(role=Role.USER, blocks=(ImageBlock(media_type="image/png", blob=ref),))])


async def test_missing_result_stops_before_provider_and_records_failure(tmp_path):
    class Consumer:
        async def complete(self, **kwargs):
            pytest.fail("provider received incomplete history")
            yield

    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        dispatcher = Dispatcher(session=session, registry=ToolRegistry(), hooks=HookBus(),
                                resolver=HeadlessResolver())
        with pytest.raises(MissingBlobError):
            await dispatcher.dispatch_model(
                provider=Consumer(), model=ModelId("fake"), tools=(),
                messages=[Message.tool_result(CallId("c"), blob=BlobRef(sha256="a" * 64, size=1))],
            )
    assert read_session(tmp_path, SessionId("s"))[-1].event.error_type == "MissingBlobError"


async def test_large_result_reaches_inference_and_external_agent_transcript(tmp_path):
    payload = "important result λ\n" * 2000

    class Tool:
        spec = ToolSpec(name=ToolName("large"), description="", parameters={})

        async def __call__(self, args):
            return payload

    class Consumer:
        async def complete(self, *, messages, **kwargs):
            wire = _messages_to_openai(messages)
            assert wire[0]["content"] == payload
            assert payload in _render_prompt(messages)
            for chunk in text_turn("received"):
                yield chunk

    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        registry = ToolRegistry()
        registry.register(Tool())
        dispatcher = Dispatcher(session=session, registry=registry, hooks=HookBus(),
                                resolver=HeadlessResolver())
        outcome = await dispatcher.dispatch_tool(
            ProposedToolCall(call_id=CallId("c"), tool=ToolName("large"), args={})
        )
        assert outcome.blob is not None
        assert outcome.read_text() == payload
        message, _ = await dispatcher.dispatch_model(
            provider=Consumer(), model=ModelId("fake"), tools=(),
            messages=[Message.tool_result(CallId("c"), blob=outcome.blob)],
        )
        assert message.text() == "received"
