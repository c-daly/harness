"""The native-inventory parity table Phase 9 hardcodes. Freezing these breaks Phase 9."""

from harness.parity import CC_TOOL_MAP, NO_NATIVE_PARITY


def test_nine_confident_mappings():
    assert CC_TOOL_MAP == {
        "Read": "read_file",
        "Write": "write_file",
        "Edit": "edit_file",
        "Glob": "glob",
        "Grep": "grep",
        "Bash": "bash",
        "TodoWrite": "todo",
        "Task": "dispatch_agent",
        "Skill": "invoke_skill",
    }


def test_no_parity_set_flags_degraded_tools():
    for name in ("WebFetch", "WebSearch", "NotebookEdit", "AskUserQuestion", "BashOutput"):
        assert name in NO_NATIVE_PARITY
    # nothing in the confident map is also flagged degraded
    assert not (set(CC_TOOL_MAP) & NO_NATIVE_PARITY)


def test_every_target_is_a_real_native_name():
    # the rewrite targets must be exactly the shipped inventory
    assert set(CC_TOOL_MAP.values()) == {
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "bash",
        "todo",
        "dispatch_agent",
        "invoke_skill",
    }
