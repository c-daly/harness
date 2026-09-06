"""Owned transcript model. Provider formats are translation targets, never this."""

from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from harness.blobs import BlobRef, BlobStore
from harness.types import CallId, ToolName


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class _Block(BaseModel):
    model_config = ConfigDict(frozen=True)
    # NOTE: frozen=True blocks attribute reassignment but does NOT deep-freeze
    # dict fields — in-place mutation of args/provider_extras is possible.
    # Never mutate them after construction. Stored event dicts stay safe only
    # because model_dump() copies nested dicts (regression-tested).
    provider_extras: dict[str, Any] = Field(default_factory=dict)


class TextBlock(_Block):
    kind: Literal["text"] = "text"
    text: str


class ThinkingBlock(_Block):
    """Model reasoning. Never user-visible prose; signature (when a provider
    issues one) rides in provider_extras and must round-trip verbatim."""

    kind: Literal["thinking"] = "thinking"
    text: str


class ImageBlock(_Block):
    kind: Literal["image"] = "image"
    media_type: str
    blob: BlobRef


class ToolCallBlock(_Block):
    kind: Literal["tool_call"] = "tool_call"
    call_id: CallId
    tool: ToolName
    args: dict[str, Any]


class ToolResultBlock(_Block):
    kind: Literal["tool_result"] = "tool_result"
    call_id: CallId
    text: str | None = None
    blob: BlobRef | None = None
    is_error: bool = False


Block = Annotated[
    Union[TextBlock, ThinkingBlock, ImageBlock, ToolCallBlock, ToolResultBlock],
    Field(discriminator="kind"),
]


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)
    role: Role
    blocks: tuple[Block, ...]
    provider_extras: dict[str, Any] = Field(default_factory=dict)
    cache_hint: bool = False

    @classmethod
    def user_text(cls, text: str, *, cache_hint: bool = False) -> "Message":
        return cls(role=Role.USER, blocks=(TextBlock(text=text),), cache_hint=cache_hint)

    @classmethod
    def system_text(cls, text: str) -> "Message":
        return cls(role=Role.SYSTEM, blocks=(TextBlock(text=text),))

    @classmethod
    def tool_result(
        cls, call_id: CallId, *, text: str | None = None,
        blob: BlobRef | None = None, is_error: bool = False,
    ) -> "Message":
        return cls(
            role=Role.TOOL,
            blocks=(ToolResultBlock(call_id=call_id, text=text, blob=blob, is_error=is_error),),
        )

    def tool_calls(self) -> tuple[ToolCallBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ToolCallBlock))

    def text(self) -> str:
        return "".join(b.text for b in self.blocks if isinstance(b, TextBlock))


def materialize_tool_results(
    messages: list[Message], blobs: BlobStore, *, max_bytes: int | None = None,
) -> list[Message]:
    """Resolve sidecars at the execution boundary, leaving stored history unchanged."""
    resolved = []
    blob_bytes = 0
    if max_bytes is not None:
        from harness.errors import ContextOverflow
        for message in messages:
            for block in message.blocks:
                if isinstance(block, ToolResultBlock) and block.blob is not None:
                    blob_bytes += block.blob.size
                    if blob_bytes > max_bytes:
                        raise ContextOverflow("tool result blobs exceed the inference input limit")
    for message in messages:
        blocks = []
        for block in message.blocks:
            if isinstance(block, ToolResultBlock) and block.blob is not None:
                text = blobs.get(block.blob).decode("utf-8")
                if block.text is not None and block.text != text:
                    raise ValueError("tool result text differs from its blob")
                block = block.model_copy(update={"text": text})
            blocks.append(block)
        resolved.append(message.model_copy(update={"blocks": tuple(blocks)}))
    return resolved
