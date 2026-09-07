"""The opt-in gate must reject plausible-looking but incorrect results."""

import pytest

from scripts.qualify_m3 import contains_facts, exact_artifact, profile

FACTS = {"project": "harbor", "retry_limit": 3}


@pytest.mark.parametrize("text", [
    "harbor: 13", "harbor: -3", "harbor-next: 3", "harbor: 3.5", "harbor: retry_limit3",
])
def test_answer_grading_rejects_other_values(text):
    assert not contains_facts(text, FACTS)


def test_answer_grading_accepts_plain_or_markdown_facts():
    assert contains_facts('Project **harbor** has retry limit **3**.', FACTS)
    assert contains_facts('harbor has retry limit 3.', FACTS)


@pytest.mark.parametrize("value", [
    {"project": "harbor", "retry_limit": "3"}, {"project": "harbor", "retry_limit": 3.0},
    {"project": "harbor", "retry_limit": 3, "extra": 1}, None,
])
def test_artifact_grading_requires_the_exact_typed_object(value):
    assert not exact_artifact(value, FACTS)
    assert exact_artifact(FACTS, FACTS)


def test_shipped_profiles_share_task_bounds_and_keep_memory_optional_to_core():
    project, memory = profile(False), profile(True)
    assert project.sources == memory.sources[:1]
    assert project.response == memory.response
    assert project.tools == ("read_file", "write_file")
    assert memory.sources[1].tool == "mcp__memory__memory_list"
    assert project.max_input_bytes == memory.max_input_bytes == 32768
