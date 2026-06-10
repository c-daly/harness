"""Owned transcript model. Provider formats are translation targets, never this."""

from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from harness.blobs import BlobRef
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
    Union[TextBlock, ImageBlock, ToolCallBlock, ToolResultBlock],
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
