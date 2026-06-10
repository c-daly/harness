from harness.blobs import BlobRef
from harness.messages import (
    ImageBlock,
    Message,
    Role,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from harness.types import CallId, ToolName


def test_user_text_helper():
    msg = Message.user_text("hello")
    assert msg.role == Role.USER
    assert msg.blocks == (TextBlock(text="hello"),)


def test_assistant_message_with_tool_calls():
    msg = Message(
        role=Role.ASSISTANT,
        blocks=(
            TextBlock(text="running it"),
            ToolCallBlock(call_id=CallId("c1"), tool=ToolName("bash"), args={"command": "ls"}),
        ),
    )
    assert msg.tool_calls()[0].tool == "bash"


def test_tool_result_message():
    msg = Message.tool_result(CallId("c1"), text="file.txt", is_error=False)
    assert msg.role == Role.TOOL
    block = msg.blocks[0]
    assert isinstance(block, ToolResultBlock) and block.call_id == "c1"


def test_provider_extras_round_trip_through_serialization():
    msg = Message(
        role=Role.ASSISTANT,
        blocks=(TextBlock(text="x", provider_extras={"signature": "abc123"}),),
        provider_extras={"reasoning_id": "r1"},
    )
    restored = Message.model_validate(msg.model_dump())
    assert restored.provider_extras == {"reasoning_id": "r1"}
    assert restored.blocks[0].provider_extras == {"signature": "abc123"}


def test_image_block_references_blob():
    img = ImageBlock(media_type="image/png", blob=BlobRef(sha256="a" * 64, size=10))
    msg = Message(role=Role.USER, blocks=(img,))
    assert Message.model_validate(msg.model_dump()).blocks[0].media_type == "image/png"


def test_cache_hint_flag():
    assert Message.user_text("x", cache_hint=True).cache_hint is True


def test_stored_event_dicts_are_isolated_from_live_mutation():
    msg = Message(
        role=Role.ASSISTANT,
        blocks=(
            ToolCallBlock(call_id=CallId("c1"), tool=ToolName("bash"), args={"command": "ls"}),
        ),
    )
    dumped = msg.model_dump()
    msg.blocks[0].args["command"] = "changed"  # frozen does not deep-freeze dicts
    assert dumped["blocks"][0]["args"]["command"] == "ls"
