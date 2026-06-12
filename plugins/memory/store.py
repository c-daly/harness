"""Flat append-only memory store.

Layout: <root>/<subject>/<YYYY-MM-DD>-<name>.md
Frontmatter fields: name, description, type, subject
Valid types: user, feedback, project, reference

This module runs inside the harness venv (imported by hooks.py) AND inside
the MCP server subprocess; keep imports to stdlib + yaml only.
"""

import datetime
import re
from pathlib import Path

import yaml

_VALID_TYPES = frozenset({"user", "feedback", "project", "reference"})
_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


def _validate_name(name: str) -> None:
    if not name or not _NAME_RE.fullmatch(name):
        raise ValueError(f"name {name!r} must be non-empty and match [A-Za-z0-9_-]+")


def _entry_path(root: Path, subject: str, name: str, date_str: str) -> Path:
    return root / subject / f"{date_str}-{name}.md"


def _find_entry(root: Path, name: str, type_: str) -> Path | None:
    """Locate an entry by name+type across all subject dirs."""
    if not root.is_dir():
        return None
    for subject_dir in root.iterdir():
        if not subject_dir.is_dir():
            continue
        for path in subject_dir.glob(f"*-{name}.md"):
            try:
                meta, _ = _parse_file(path)
                if meta.get("name") == name and meta.get("type") == type_:
                    return path
            except Exception:
                continue
    return None


def _parse_file(path: Path) -> tuple[dict, str]:
    """Parse a frontmatter markdown file; raises on any parse error."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: missing frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError(f"{path}: unclosed frontmatter")
    raw_yaml = text[4:end]
    body = text[end + 5 :].lstrip("\n")
    meta = yaml.safe_load(raw_yaml)
    if not isinstance(meta, dict):
        raise ValueError(f"{path}: frontmatter must be a YAML mapping")
    return meta, body


def _render(*, name: str, description: str, type_: str, subject: str, body: str) -> str:
    """Render a frontmatter markdown string."""
    fm = yaml.dump(
        {"name": name, "description": description, "type": type_, "subject": subject},
        default_flow_style=False,
        allow_unicode=True,
    ).rstrip()
    return f"---\n{fm}\n---\n\n{body}\n"


def write(
    root: Path,
    *,
    type: str,
    name: str,
    subject: str,
    description: str,
    body: str,
) -> str:
    """Write a new memory entry; returns its relative path string.

    Raises ValueError on invalid type, bad name, or name+type collision.
    Append-only: existing name+type pairs are never overwritten.
    """
    if type not in _VALID_TYPES:
        raise ValueError(f"type {type!r} is not valid; must be one of {sorted(_VALID_TYPES)}")
    _validate_name(name)
    # Collision check
    existing = _find_entry(root, name, type)
    if existing is not None:
        raise ValueError(
            f"entry name={name!r} type={type!r} already exists at {existing.relative_to(root)}"
        )
    date_str = datetime.date.today().isoformat()
    path = _entry_path(root, subject, name, date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _render(name=name, description=description, type_=type, subject=subject, body=body)
    path.write_text(content, encoding="utf-8")
    return str(path.relative_to(root))


def list_entries(
    root: Path,
    type: str | None = None,
    subject: str | None = None,
) -> list[dict]:
    """Return a list of frontmatter dicts for all entries matching the filters."""
    if not root.is_dir():
        return []
    results = []
    for subject_dir in sorted(root.iterdir()):
        if not subject_dir.is_dir():
            continue
        for path in sorted(subject_dir.glob("*.md")):
            try:
                meta, _ = _parse_file(path)
            except Exception:
                continue
            if type is not None and meta.get("type") != type:
                continue
            if subject is not None and meta.get("subject") != subject:
                continue
            results.append(dict(meta))
    return results


def get(root: Path, name: str, type: str) -> str | None:
    """Return the full round-trippable markdown for name+type, or None."""
    path = _find_entry(root, name, type)
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def brief(root: Path) -> str:
    """Return a markdown brief of the memory store.

    Format:
      # Memory

      ## User-level
      - <description> (<name>)
      ...

      ## Subjects
      - <subject>: <count> entries
      ...

    Empty store -> "# Memory\n\n_No entries._"
    ANY internal error -> empty-store brief (fail-open, never raises).
    """
    try:
        return _brief_inner(root)
    except Exception:
        return "# Memory\n\n_No entries._"


def _brief_inner(root: Path) -> str:
    entries = list_entries(root)
    if not entries:
        return "# Memory\n\n_No entries._"

    user_entries = [e for e in entries if e.get("subject") == "user"]
    subject_counts: dict[str, int] = {}
    for e in entries:
        s = e.get("subject", "unknown")
        subject_counts[s] = subject_counts.get(s, 0) + 1

    lines = ["# Memory"]
    if user_entries:
        lines.append("")
        lines.append("## User-level")
        for e in user_entries:
            desc = e.get("description", "")
            nm = e.get("name", "")
            lines.append(f"- {desc} ({nm})")

    if subject_counts:
        lines.append("")
        lines.append("## Subjects")
        for subj, count in sorted(subject_counts.items()):
            word = "entry" if count == 1 else "entries"
            lines.append(f"- {subj}: {count} {word}")

    return "\n".join(lines)


def rebuild_index(root: Path) -> int:
    """Regenerate root/MEMORY.md from a full scan; return entry count."""
    entries = list_entries(root)
    content = brief(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "MEMORY.md").write_text(content, encoding="utf-8")
    return len(entries)
