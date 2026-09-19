"""Controls for the compact adapter over the existing projects readers."""

import importlib.util
import json
from pathlib import Path

import pytest


def make_overview(entries, git_read, unavailable=()):
    source = Path(__file__).parents[1] / "plugins/projects_overview.py"
    spec = importlib.util.spec_from_file_location("overview_under_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Mcp:
        def tool(self):
            return lambda function: function

    return module.register(Mcp(), inventory=lambda: (list(entries), list(unavailable)),
                           git_read=git_read, encoded=lambda value: json.dumps(value, separators=(",", ":")))


def entry(root, name, git=True):
    p = root / name
    p.mkdir()
    return {"name": name, "path": str(p), "git": git, "environment": "test", "root": "test"}


def test_compact_page_uses_exact_paths_and_counts_rename_once(tmp_path):
    project = entry(tmp_path, "project")
    (Path(project["path"]) / "STATUS.md").write_text("not copied into overview")
    calls = []

    def read(path, *args):
        calls.append((path, args))
        return "abc123 2026-09-18 Fix startup" if args[0] == "log" else "R  new\0old\0 M changed\0?? newfile\0"

    result = json.loads(make_overview([project], read)())
    observed = result["projects"][0]
    assert observed["project"] == project
    assert observed["git"]["uncommitted_count"] == 3
    assert observed["git"]["working_tree"] == "uncommitted changes"
    assert observed["status_documents"] == ["STATUS.md"]
    assert "not copied" not in json.dumps(result)
    assert len(calls) == 2
    assert result["next_offset"] == 1 and not result["more"]


def test_pagination_and_non_git_directories(tmp_path):
    entries = [entry(tmp_path, str(i), git=False) for i in range(5)]

    def no_git(*args):
        raise AssertionError("non-repository must not run git")

    overview = make_overview(entries, no_git, unavailable=["/missing"])
    first = json.loads(overview(limit=2))
    second = json.loads(overview(offset=first["next_offset"], limit=3))
    paths = [p["project"]["path"] for page in [first, second] for p in page["projects"]]
    assert len(set(paths)) == 5
    assert first["more"] and not second["more"]
    assert first["unavailable_roots"] == ["/missing"]
    assert all(p["git"] is None for p in first["projects"])


def test_git_failure_is_visible_not_clean_status(tmp_path):
    project = entry(tmp_path, "broken")

    def broken(*args):
        raise ValueError("cannot read repository")

    result = json.loads(make_overview([project], broken)())
    assert result["projects"][0]["git"] == {"error": "cannot read repository"}


def test_byte_limit_pages_without_losing_entries(tmp_path):
    entries = [entry(tmp_path, str(i), git=False) for i in range(20)]
    for e in entries:
        e["root"] = "x" * 500
    overview = make_overview(entries, lambda *a: "")
    first_text = overview(limit=20)
    first = json.loads(first_text)
    assert len(first_text.encode()) <= 8192 and first["more"]
    second = json.loads(overview(offset=first["next_offset"], limit=20))
    assert len(first["projects"]) + len(second["projects"]) == 20


@pytest.mark.parametrize("offset,limit", [(-1, 12), (0, 0), (0, 21)])
def test_invalid_page_rejected(offset, limit):
    with pytest.raises(ValueError, match="Invalid page"):
        make_overview([], lambda *a: "")(offset=offset, limit=limit)
