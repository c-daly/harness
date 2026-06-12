"""Phase-9 parity table. The native inventory exists so this mapping has real targets.
Where a Claude Code tool has NO native target, the importer flags the skill degraded (so a
name-mapping 'success' cannot hide a missing capability). Phase 9 imports these two names.
"""

# CC tool name -> native inventory name. Frozen once Phase 9 ships (Law L9).
CC_TOOL_MAP: dict[str, str] = {
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

# CC tool names with NO native parity: a rewrite referencing these must flag the skill
# degraded rather than silently 'succeed'. Not exhaustive of all CC history; the named set the
# design doc implies plus the common background-bash trio.
NO_NATIVE_PARITY: frozenset[str] = frozenset(
    {
        "WebFetch",
        "WebSearch",
        "NotebookEdit",
        "AskUserQuestion",
        "BashOutput",
        "KillShell",
        "EnterWorktree",
    }
)
