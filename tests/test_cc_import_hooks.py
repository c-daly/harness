"""Hooks are flagged for hand-port, never converted; skips are reported by category."""

import json

from harness.cc_import import Skip, flag_hooks, skip_report


HOOKS = json.dumps(
    {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|clear|compact",
                    "hooks": [
                        {
                            "type": "command",
                            "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd" session-start',
                            "async": False,
                        },
                    ],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": "validate.sh"},
                    ],
                }
            ],
        }
    }
)


def test_every_hook_event_and_command_is_flagged_for_hand_port():
    entries = flag_hooks(HOOKS)
    assert all(e.kind == "hook" for e in entries)
    joined = " ".join(e.detail for e in entries)
    assert "SessionStart" in joined
    assert "PreToolUse" in joined
    assert "run-hook.cmd" in joined  # the command text survives in the report
    assert "startup|clear|compact" in joined  # the matcher survives
    # the design wording is present (hand-port / not converted)
    assert any(
        "hand-port" in e.detail.lower() or "not converted" in e.detail.lower() for e in entries
    )


def test_no_hooks_json_yields_no_hook_entries():
    assert flag_hooks(None) == ()


def test_malformed_hooks_json_is_flagged_not_crashed():
    entries = flag_hooks("{not json")
    assert len(entries) == 1
    assert entries[0].kind == "hook"
    assert "could not parse" in entries[0].detail.lower()


def test_skip_report_groups_by_category_and_counts():
    skips = (
        Skip("icon.png", "binary"),
        Skip("bin/tool", "binary"),
        Skip(".opencode/c.json", "foreign-harness"),
        Skip("tests/t.py", "build"),
    )
    entries = skip_report(skips)
    assert all(e.kind == "skip" for e in entries)
    detail = " ".join(e.detail for e in entries)
    assert "binary" in detail and "2" in detail  # count by category
    assert "foreign-harness" in detail
    # deterministic: sorted by category
    cats = [e.artifact for e in entries]
    assert cats == sorted(cats)


def test_empty_skips_yield_no_entries():
    assert skip_report(()) == ()
