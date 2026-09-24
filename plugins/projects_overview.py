"""Compact read-only overview for the existing projects MCP integration.

Loaded by its server with the existing inventory/git_read/encoded helpers.
Storage, root resolution and single-project inspection stay with that plugin.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import subprocess


def register(mcp, *, inventory, git_read, encoded):
    @mcp.tool()
    def projects_overview(offset: int = 0, limit: int = 12) -> str:
        """Read a compact portfolio snapshot before answering broad project-status
        questions. Returns exact project paths, latest commit, dirty-file count
        and available status-document paths; no full document excerpts. Paginated,
        with recently changed directories first (a filesystem activity hint only).
        Summarize the covered projects and state whether more remain. Use
        projects_status for deeper inspection of a particular returned project.
        Read-only: these observations do not prove task completion or test results.
        """
        if offset < 0 or not 1 <= limit <= 20:
            raise ValueError("Invalid page")
        entries, unavailable = inventory()

        def activity(entry):
            root = Path(entry["path"])
            times = []
            for p in (root, root / ".git/index", root / ".git/logs/HEAD"):
                try:
                    times.append(p.stat().st_mtime)
                except OSError:
                    pass
            return max(times, default=0)

        entries.sort(key=lambda e: (-activity(e), e["path"]))

        def inspect(entry):
            root = Path(entry["path"])
            result = {"project": entry, "git": None, "status_documents": []}
            if entry["git"]:
                try:
                    latest = git_read(root, "log", "-1", "--format=%h %cs %s").strip()
                    # Porcelain -z has one additional path for renames/copies.
                    records = iter(git_read(root, "status", "--porcelain=v1", "-z",
                                            "--untracked-files=normal").split("\0"))
                    count = 0
                    for record in records:
                        if not record:
                            continue
                        count += 1
                        if "R" in record[:2] or "C" in record[:2]:
                            next(records, None)
                    result["git"] = {"working_tree": "clean" if count == 0 else "uncommitted changes",
                                     "latest_commit": latest[:220],
                                     "uncommitted_count": count}
                except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                    result["git"] = {"error": str(exc)[:160]}
            for name in ("STATUS.md", "README.md", "docs/STATUS.md", "docs/roadmap.md"):
                p = root / name
                if p.is_file() and p.resolve().is_relative_to(root):
                    result["status_documents"].append(name)
            return result

        page = entries[offset:offset + limit]
        with ThreadPoolExecutor(max_workers=4) as executor:
            observations = list(executor.map(inspect, page))
        result = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "projects": [], "total_matches": len(entries),
            "next_offset": min(offset, len(entries)), "more": False,
            "unavailable_roots": unavailable,
            "order": "filesystem_activity_hint",
            "notice": ("Partial live checkout overview of configured directories; state coverage. "
                       "No activity/completion classification is supplied. A null git field is not "
                       "a judgement that a directory is or is not a project. Commit titles and dirty "
                       "counts are not verified milestone, test or agent status. "
                       "Use projects_status only where deeper detail is needed."),
        }
        for observation in observations:
            result["projects"].append(observation)
            result["next_offset"] += 1
            result["more"] = result["next_offset"] < len(entries)
            if len(encoded(result).encode()) > 8192:
                result["projects"].pop()
                result["next_offset"] -= 1
                break
        result["more"] = result["next_offset"] < len(entries)
        if not result["projects"] and observations:
            raise ValueError("Project metadata exceeds the overview page limit")
        if len(encoded(result).encode()) > 8192:
            raise ValueError("Overview metadata exceeds the page limit")
        return encoded(result)

    return projects_overview
