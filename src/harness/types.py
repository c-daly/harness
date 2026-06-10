"""Foundational typed identifiers. The harness never confuses a string for a tool name."""

import uuid
from typing import NewType

SCHEMA_VERSION = 1

ToolName = NewType("ToolName", str)
SessionId = NewType("SessionId", str)
ModelId = NewType("ModelId", str)
AgentId = NewType("AgentId", str)
CallId = NewType("CallId", str)


def new_call_id() -> CallId:
    return CallId(uuid.uuid4().hex)


def new_session_id() -> SessionId:
    return SessionId(uuid.uuid4().hex)
