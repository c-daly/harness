from harness.types import (
    SCHEMA_VERSION,
    AgentId,
    CallId,
    ModelId,
    SessionId,
    ToolName,
    new_call_id,
    new_session_id,
)


def test_newtypes_are_strings():
    assert isinstance(ToolName("read_file"), str)
    assert isinstance(SessionId("s1"), str)
    assert isinstance(ModelId("fake:echo"), str)
    assert isinstance(AgentId("triage"), str)
    assert isinstance(CallId("c1"), str)


def test_schema_version_is_int():
    assert isinstance(SCHEMA_VERSION, int) and SCHEMA_VERSION >= 1


def test_id_generators_unique():
    assert new_call_id() != new_call_id()
    assert new_session_id() != new_session_id()
    assert len(new_call_id()) == 32  # uuid4 hex
