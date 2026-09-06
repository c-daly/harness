"""The opt-in real-model smoke report must grade external evidence conservatively."""

from types import SimpleNamespace

import pytest

from scripts.qualify_local import FACTS, artifact_metadata, project_checks


def checks(*, status="completed", outcomes=None, artifact=None):
    return project_checks(SimpleNamespace(status=status, acceptance="unverified"),
        outcomes if outcomes is not None else [
            {"tool": "read_file", "path": "FACTS.json", "is_error": False},
            {"tool": "write_file", "path": "RESULT.json", "is_error": False}],
        FACTS if artifact is None else artifact)


def test_report_requires_verified_artifact_and_successful_tools():
    assert all(checks().values())
    assert not all(checks(outcomes=[]).values())
    assert not all(checks(artifact={"project": "harbor", "retry_limit": 4}).values())
    assert not all(checks(artifact={**FACTS, "unrequested": True}).values())
    assert not all(checks(outcomes=[{"tool": "read_file", "path": "FACTS.json", "is_error": False},
            {"tool": "write_file", "path": "RESULT.json", "is_error": True}]).values())
    assert not all(checks(outcomes=[{"tool": "read_file", "path": "RESULT.json", "is_error": False},
            {"tool": "write_file", "path": "RESULT.json", "is_error": False}]).values())


@pytest.mark.parametrize("status", ["incomplete", "failed", "cancelled", "aborted"])
def test_existing_artifact_cannot_make_failed_execution_pass(status):
    assert not all(checks(status=status).values())


def test_artifact_diagnostic_does_not_publish_memory_derived_values():
    artifact = {"project": "PRIVATE PROJECT", "retry_limit": "PRIVATE VALUE", "PRIVATE KEY": True}
    metadata = artifact_metadata(artifact)
    assert "PRIVATE" not in str(metadata)
    assert metadata["object"] and not metadata["keys_exact"]
    assert not metadata["project_matches"] and not metadata["retry_limit_matches"]
