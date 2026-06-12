"""Import a Claude Code (CC) format plugin and emit a native plugin + IMPORT-REPORT.md.

A converter, not a compatibility layer. Recognized CC primitives translate to native ones;
everything the importer cannot or will not translate is skipped-with-report (kitchen-sink
reality) or flagged for hand-port (hooks). The parity table (parity.py) is the rewrite
contract; where parity is missing the artifact is flagged DEGRADED so a name-mapping success
cannot hide a missing capability. Secrets are never echoed (the mcp_import.py guarantee is
inherited). Output is deterministic: no timestamps, sorted everything.

This module is structured in halves: read_cc_plugin (this file, reader) builds a typed CcPlugin
from disk; the converters (added in later tasks) are pure functions over that model.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from harness.frontmatter import FrontmatterError, split_frontmatter

# Recognized primitive subdirs and metadata locations.
_FOREIGN_HARNESS = {".opencode", ".codex-plugin"}
_HOUSEKEEPING_NAMES = {".in_use", ".gitignore", ".gitattributes", "RELEASE-NOTES.md"}
_BUILD_DIRS = {"tests", "test", "logs", "bin", "node_modules", "__pycache__", ".git"}
_BUILD_FILES = {"pyproject.toml", "poetry.lock", "package-lock.json", "uv.lock", "err.txt"}
_TEXT_EXTS = {".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".cfg", ".rst"}
_BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".so", ".bin"}


class CcImportError(Exception):
    """Teaching error: what failed / why / what to do. Never echoes a secret."""


@dataclass(frozen=True)
class RawDef:
    """A skill/command/agent as read from disk: frontmatter dict + body, unconverted."""

    name: str
    meta: dict
    body: str
    source_path: Path
    # for dir-per-skill: relpath-within-dir -> absolute path of sibling reference files
    assets: dict[str, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class Skip:
    """A file/dir the importer will not convert, recorded by category for the report."""

    relpath: str
    category: str  # foreign-harness | housekeeping | build | binary | malformed | unknown


@dataclass
class CcPlugin:
    root: Path
    name: str
    version: str
    description: str
    author: dict | None = None
    homepage: str | None = None
    repository: object | None = None
    license: str | None = None
    keywords: list = field(default_factory=list)
    skills: tuple[RawDef, ...] = ()
    commands: tuple[RawDef, ...] = ()
    agents: tuple[RawDef, ...] = ()
    mcp_json_text: str | None = None
    hooks_json_text: str | None = None
    skips: tuple[Skip, ...] = ()


def _read_meta(root: Path) -> dict:
    meta_path = root / ".claude-plugin" / "plugin.json"
    if not meta_path.is_file():
        raise CcImportError(
            f"no .claude-plugin/plugin.json under {root}: this does not look like a Claude"
            " Code plugin root (expected the version dir that holds .claude-plugin/)"
        )
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CcImportError(f"{meta_path}: not valid JSON ({exc}); fix it upstream") from exc
    if not isinstance(data, dict):
        raise CcImportError(f"{meta_path}: top level must be a JSON object")
    return data


def _parse_def(path: Path, *, name: str, skips: list[Skip], root: Path) -> RawDef | None:
    """Split frontmatter; record a malformed skip instead of raising on bad files."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        skips.append(Skip(relpath=str(path.relative_to(root)), category="malformed"))
        return None
    try:
        meta, body = split_frontmatter(text)
    except FrontmatterError:
        skips.append(Skip(relpath=str(path.relative_to(root)), category="malformed"))
        return None
    return RawDef(name=name, meta=meta, body=body, source_path=path)


def _read_skills(root: Path, skips: list[Skip]) -> tuple[RawDef, ...]:
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return ()
    out: list[RawDef] = []
    for sub in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = sub / "SKILL.md"
        if not skill_md.is_file():
            skips.append(Skip(relpath=str(sub.relative_to(root)), category="malformed"))
            continue
        raw = _parse_def(skill_md, name=sub.name, skips=skips, root=root)
        if raw is None:
            continue
        assets: dict[str, Path] = {}
        for asset in sorted(sub.rglob("*")):
            if asset.is_file() and asset != skill_md:
                assets[str(asset.relative_to(sub))] = asset
        out.append(
            RawDef(
                name=raw.name,
                meta=raw.meta,
                body=raw.body,
                source_path=raw.source_path,
                assets=assets,
            )
        )
    return tuple(out)


def _read_flat(root: Path, subdir: str, skips: list[Skip]) -> tuple[RawDef, ...]:
    directory = root / subdir
    if not directory.is_dir():
        return ()
    out: list[RawDef] = []
    for path in sorted(directory.glob("*.md")):
        raw = _parse_def(path, name=path.stem, skips=skips, root=root)
        if raw is not None:
            out.append(raw)
    return tuple(out)


def _categorize_skip(rel: str, path: Path) -> str:
    parts = Path(rel).parts
    top = parts[0]
    if top in _FOREIGN_HARNESS:
        return "foreign-harness"
    if path.name in _HOUSEKEEPING_NAMES or rel == ".claude-plugin/manifest.json":
        return "housekeeping"
    if top in _BUILD_DIRS or path.name in _BUILD_FILES:
        return "build"
    if path.suffix.lower() in _BINARY_EXTS or _looks_binary(path):
        return "binary"
    return "unknown"


def _looks_binary(path: Path) -> bool:
    """Cheap binary sniff: a NUL byte in the first 1KiB. Never reads the whole file."""
    try:
        with path.open("rb") as fh:
            return b"\x00" in fh.read(1024)
    except OSError:
        return False


_RECOGNIZED_TOP = {"skills", "commands", "agents", "hooks", ".claude-plugin", ".mcp.json"}


def _read_skips(root: Path) -> tuple[Skip, ...]:
    """Record every top-level entry that is not a recognized primitive, by category.
    Recurses only enough to categorize; never reads skip-category file contents."""
    skips: list[Skip] = []
    for entry in sorted(root.iterdir()):
        rel = entry.name
        if rel in _RECOGNIZED_TOP:
            continue
        if entry.is_dir():
            for child in sorted(entry.rglob("*")):
                if child.is_file():
                    crel = str(child.relative_to(root))
                    skips.append(Skip(relpath=crel, category=_categorize_skip(crel, child)))
        else:
            skips.append(Skip(relpath=rel, category=_categorize_skip(rel, entry)))
    return tuple(skips)


def read_cc_plugin(root: Path) -> CcPlugin:
    """Walk a CC plugin root into a typed CcPlugin. Structure only; no conversion."""
    root = Path(root)
    if not root.is_dir():
        raise CcImportError(f"{root} is not a directory; pass the CC plugin root")
    meta = _read_meta(root)
    malformed_skips: list[Skip] = []
    skills = _read_skills(root, malformed_skips)
    commands = _read_flat(root, "commands", malformed_skips)
    agents = _read_flat(root, "agents", malformed_skips)

    mcp_path = root / ".mcp.json"
    mcp_text = mcp_path.read_text(encoding="utf-8") if mcp_path.is_file() else None
    hooks_path = root / "hooks" / "hooks.json"
    hooks_text = hooks_path.read_text(encoding="utf-8") if hooks_path.is_file() else None

    skips = tuple(
        sorted((*_read_skips(root), *malformed_skips), key=lambda s: (s.category, s.relpath))
    )
    name = meta.get("name")
    if not isinstance(name, str) or not name:
        raise CcImportError(
            f"{root}/.claude-plugin/plugin.json: missing a string name; cannot derive a"
            " native plugin name"
        )
    return CcPlugin(
        root=root,
        name=name,
        version=str(meta.get("version", "0.0.0")),
        description=str(meta.get("description", f"Imported from Claude Code plugin {name}")),
        author=meta.get("author") if isinstance(meta.get("author"), dict) else None,
        homepage=meta.get("homepage") if isinstance(meta.get("homepage"), str) else None,
        repository=meta.get("repository"),
        license=meta.get("license") if isinstance(meta.get("license"), str) else None,
        keywords=list(meta.get("keywords", [])) if isinstance(meta.get("keywords"), list) else [],
        skills=skills,
        commands=commands,
        agents=agents,
        mcp_json_text=mcp_text,
        hooks_json_text=hooks_text,
        skips=skips,
    )
