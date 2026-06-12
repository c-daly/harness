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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from harness.frontmatter import FrontmatterError, split_frontmatter
from harness.parity import CC_TOOL_MAP, NO_NATIVE_PARITY
from harness.catalog import Catalog, UnknownAliasError

# Recognized primitive subdirs and metadata locations.
_FOREIGN_HARNESS = {".opencode", ".codex-plugin"}
_HOUSEKEEPING_NAMES = {".in_use", ".gitignore", ".gitattributes", "RELEASE-NOTES.md"}
_BUILD_DIRS = {"tests", "test", "logs", "bin", "node_modules", "__pycache__", ".git"}
_BUILD_FILES = {"pyproject.toml", "poetry.lock", "package-lock.json", "uv.lock", "err.txt"}
_TEXT_EXTS = {".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".cfg", ".rst"}
_BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".so", ".bin"}
# Oversize guard: a def larger than this is recorded, never read into memory (DoS guard).
_MAX_DEF_BYTES = 1 * 1024 * 1024

# The skip taxonomy; the seven converters (later tasks) filter on these strings.
SkipCategory = Literal[
    "foreign-harness",
    "housekeeping",
    "build",
    "binary",
    "malformed",
    "oversize",
    "unknown",
]


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
    category: SkipCategory


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
    # None = file absent; "" = present but empty. Converters must check
    # `is not None`, not truthiness, to tell the two cases apart.
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
    """Split frontmatter; record a malformed/oversize skip instead of raising on bad files."""
    try:
        size = path.stat().st_size
    except OSError:
        skips.append(Skip(relpath=str(path.relative_to(root)), category="malformed"))
        return None
    if size > _MAX_DEF_BYTES:
        # Record without reading: a multi-megabyte def is never pulled into memory (DoS guard).
        skips.append(Skip(relpath=str(path.relative_to(root)), category="oversize"))
        return None
    try:
        # utf-8-sig strips a leading BOM so a Windows-authored def parses instead of skipping.
        text = path.read_text(encoding="utf-8-sig")
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
            # Never follow symlinks (Phase 8 walk law, file links included): a link
            # like data.md -> /etc/passwd would exfiltrate external content downstream.
            if asset.is_file() and asset != skill_md and not asset.is_symlink():
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


def _categorize_skip(rel: str, path: Path) -> SkipCategory:
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
        # Never follow symlinks (Phase 8 walk law, file links included): a symlinked
        # top-level dir is recorded as one Skip, never traversed, so rglob cannot
        # enumerate (and _looks_binary cannot read) an external tree it points at.
        if entry.is_dir() and not entry.is_symlink():
            for child in sorted(entry.rglob("*")):
                if child.is_file() and not child.is_symlink():
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


# ---------------------------------------------------------------------------
# Task 2: Skills + commands converter
# ---------------------------------------------------------------------------

_ASSET_CAP = 5 * 1024 * 1024  # bytes; binaries larger than this are refused, not copied

# A backticked exact tool name: `Read`, `TodoWrite`. The capture group is the bare name.
_BACKTICKED = re.compile(r"`([A-Za-z][A-Za-z0-9_]*)`")
# Markdown link/image targets: [text](target) and ![alt](target). Group 2 is the target.
_MD_LINK = re.compile(r"(!?\[[^\]]*\])\(([^)]+)\)")
# $1-style positionals (not $ARGUMENTS, not ${...}).
_POSITIONAL = re.compile(r"\$(\d+)\b")
_ALL_CC_NAMES = set(CC_TOOL_MAP) | set(NO_NATIVE_PARITY)

# The report-kind taxonomy. Tasks 6-7 filter on these strings; keep the set closed.
ReportEntryKind = Literal[
    "rewrite",
    "mention",
    "drop",
    "degraded",
    "asset",
    "refused",
    "skip",
    "hook",
    "mcp",
    "meta",
]


@dataclass(frozen=True)
class ReportEntry:
    """One line of the import report. kind drives the section and the summary counts."""

    kind: ReportEntryKind
    artifact: str  # emitted relpath or source relpath; sorts the report deterministically
    detail: str
    line: int | None = None


@dataclass(frozen=True)
class ConvertedDef:
    """Result of converting one skill/command/agent. Pure: no disk writes here."""

    relpath: str  # emitted path under the plugin root, e.g. skills/remembering.md
    text: str  # the full emitted markdown (frontmatter + body)
    report: tuple[ReportEntry, ...] = ()
    assets: tuple[tuple[Path, str], ...] = ()  # (source abs path, dest relpath under plugin)
    degraded: bool = False


def _emit_frontmatter(meta: dict, body: str) -> str:
    """Re-emit clean frontmatter. Keys are sorted for determinism; body trailing-newline kept."""
    dumped = yaml.safe_dump(meta, sort_keys=True, default_flow_style=False).rstrip("\n")
    body = body.rstrip("\n")
    return f"---\n{dumped}\n---\n\n{body}\n"


def rewrite_prose(body: str, *, source: str) -> tuple[str, list[ReportEntry]]:
    """L4b: rewrite ONLY backticked exact tool names; count bare-word mentions; flag
    NO_NATIVE_PARITY references. Deterministic and idempotent (native text is unchanged).
    Returns (new_body, report_entries). Degraded flagging is the callers job (any entry
    with kind==degraded means the artifact is degraded).

    Fenced code blocks are NOT excluded: backticked tool names inside examples are
    intentionally rewritten, and double-backtick verbatim spans are also matched (the
    regex does not track fence state). This is the high-precision policy: anything in
    backticks is treated as an exact reference, fenced or not."""
    entries: list[ReportEntry] = []
    out_lines: list[str] = []
    for lineno, line in enumerate(body.splitlines(), start=1):

        def _sub(match: re.Match) -> str:
            name = match.group(1)
            if name in CC_TOOL_MAP:
                target = CC_TOOL_MAP[name]
                entries.append(
                    ReportEntry(
                        kind="rewrite",
                        artifact=source,
                        line=lineno,
                        detail=f"`{name}` -> `{target}`",
                    )
                )
                return f"`{target}`"
            if name in NO_NATIVE_PARITY:
                entries.append(
                    ReportEntry(
                        kind="degraded",
                        artifact=source,
                        line=lineno,
                        detail=f"`{name}` has no native parity; capability is missing",
                    )
                )
                return match.group(0)  # leave the name; the artifact is flagged degraded
            return match.group(0)

        new_line = _BACKTICKED.sub(_sub, line)
        # bare-word mentions (unbackticked exact CC names) are only counted, never rewritten.
        # Precompute the set of backticked names on this line once (O(line)); the per-word
        # membership check is then O(1). A naive `f"`{word}`" not in line` rescans the whole
        # line per word -> O(n^2), which never completes on a megabyte-wide line.
        backticked_on_line = {m.group(1) for m in _BACKTICKED.finditer(line)}
        for word in re.findall(r"\b([A-Za-z][A-Za-z0-9_]*)\b", line):
            if word in _ALL_CC_NAMES and word not in backticked_on_line:
                entries.append(
                    ReportEntry(
                        kind="mention",
                        artifact=source,
                        line=lineno,
                        detail=f"possible bare mention of {word} (not rewritten)",
                    )
                )
        out_lines.append(new_line)
    new_body = "\n".join(out_lines)
    if body.endswith("\n"):
        new_body += "\n"
    return new_body, entries


def _plan_assets(
    raw: "RawDef", *, kind: str
) -> tuple[list[tuple[Path, str]], dict[str, str], list[ReportEntry]]:
    """Decide which sibling assets copy and how their relative links rewrite.
    Returns (copies, link_map old_rel->new_rel, report). Oversize files are refused."""
    copies: list[tuple[Path, str]] = []
    link_map: dict[str, str] = {}
    report: list[ReportEntry] = []
    base = f"{kind}/{raw.name}.assets"
    for rel, src in sorted(raw.assets.items()):
        # Path-traversal guard runs first: a non-normalized rel (e.g. ../../etc/passwd) would
        # plan a write outside the .assets dir. Refuse on the structural rel, before any stat
        # (a traversal rel is rejected whether or not the file it names exists).
        dest_path = Path(base) / rel
        if ".." in dest_path.parts:
            report.append(
                ReportEntry(
                    kind="refused",
                    artifact=f"{kind}/{raw.name}.md",
                    detail=f"asset {rel} escapes the assets dir; refused",
                )
            )
            continue
        try:
            size = src.stat().st_size
        except OSError:
            report.append(
                ReportEntry(
                    kind="refused",
                    artifact=f"{kind}/{raw.name}.md",
                    detail=f"asset {rel} unreadable; not copied",
                )
            )
            continue
        if size > _ASSET_CAP:
            report.append(
                ReportEntry(
                    kind="refused",
                    artifact=f"{kind}/{raw.name}.md",
                    detail=f"asset {rel} is {size} bytes (> cap); not copied",
                )
            )
            continue
        dest = f"{base}/{rel}"
        copies.append((src, dest))
        link_map[rel] = dest
        report.append(
            ReportEntry(kind="asset", artifact=f"{kind}/{raw.name}.md", detail=f"{rel} -> {dest}")
        )
    return copies, link_map, report


def _rewrite_links(
    body: str, link_map: dict[str, str], *, source: str
) -> tuple[str, list[ReportEntry]]:
    """Rewrite markdown link/image targets that point at copied assets to the new path.
    A relative-looking link to a target that is not in the asset plan is flagged broken
    (it may point at a refused or absent asset); the body text is left unchanged."""
    report: list[ReportEntry] = []
    if not link_map and "](" not in body:
        return body, report

    def _sub(match: re.Match) -> str:
        label, target = match.group(1), match.group(2)
        bare = target.split("#", 1)[0]
        if bare in link_map:
            return f"{label}({link_map[bare]})"
        # An unmatched, relative-looking target may be a broken link (refused/absent asset).
        # Absolute URLs and pure in-page anchors are never assets, so they are not flagged.
        if bare and "://" not in bare and not bare.startswith(("#", "/", "mailto:")):
            report.append(
                ReportEntry(
                    kind="refused",
                    artifact=source,
                    detail=f"link to {bare!r} not in asset plan; may be broken",
                )
            )
        return match.group(0)

    return _MD_LINK.sub(_sub, body), report


def _clean_meta(
    meta: dict, *, keep: set[str], name: str, source: str
) -> tuple[dict, list[ReportEntry]]:
    """Keep only allowed frontmatter keys; report every dropped key."""
    out = {"name": name}
    report: list[ReportEntry] = []
    if isinstance(meta.get("description"), str):
        out["description"] = meta["description"]
    else:
        out["description"] = f"Imported {name}"
        report.append(
            ReportEntry(
                kind="meta", artifact=source, detail="missing description; generated a placeholder"
            )
        )
    for key in sorted(meta):
        if key not in keep and key != "description":
            report.append(
                ReportEntry(
                    kind="drop",
                    artifact=source,
                    detail=f"frontmatter key `{key}` dropped (no native slot)",
                )
            )
    return out, report


def _sanitize_name(name: str) -> str:
    """Coerce a name to the native regex [A-Za-z0-9_-]+ without `__`."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", name)
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("-") or "imported"
    return cleaned


def convert_skill(raw: "RawDef") -> ConvertedDef:
    source = f"skills/{raw.name}.md"
    name = _sanitize_name(str(raw.meta.get("name", raw.name)))
    report: list[ReportEntry] = []
    if name != raw.name:
        report.append(
            ReportEntry(
                kind="meta",
                artifact=source,
                detail=f"skill name {raw.name!r} sanitized to {name!r}",
            )
        )
    meta, meta_report = _clean_meta(
        raw.meta, keep={"name", "description"}, name=name, source=source
    )
    report += meta_report
    copies, link_map, asset_report = _plan_assets(raw, kind="skills")
    report += asset_report
    body, link_report = _rewrite_links(raw.body, link_map, source=source)
    report += link_report
    body, prose_report = rewrite_prose(body, source=source)
    report += prose_report
    degraded = any(e.kind == "degraded" for e in report)
    return ConvertedDef(
        relpath=f"skills/{name}.md",
        text=_emit_frontmatter(meta, body),
        report=tuple(report),
        assets=tuple(copies),
        degraded=degraded,
    )


def convert_command(raw: "RawDef") -> ConvertedDef:
    source = f"commands/{raw.name}.md"
    name = _sanitize_name(raw.name)
    report: list[ReportEntry] = []
    if name != raw.name:
        report.append(
            ReportEntry(
                kind="meta",
                artifact=source,
                detail=f"command name {raw.name!r} sanitized to {name!r}",
            )
        )
    meta, meta_report = _clean_meta(
        raw.meta, keep={"name", "description"}, name=name, source=source
    )
    report += meta_report
    body = raw.body
    # argument-hint survives as a body comment
    hint = raw.meta.get("argument-hint")
    if isinstance(hint, str) and hint:
        body = body.rstrip("\n") + f"\n\n<!-- argument-hint: {hint} -->\n"
        report.append(
            ReportEntry(
                kind="meta",
                artifact=source,
                detail=f"argument-hint {hint!r} preserved as a body comment",
            )
        )
    # positional args: only-$1 rewrites to $ARGUMENTS; $2+ degrades, body verbatim
    positionals = {int(m) for m in _POSITIONAL.findall(body)}
    degraded = False
    if positionals == {1}:
        body = _POSITIONAL.sub("$ARGUMENTS", body)
        report.append(
            ReportEntry(
                kind="rewrite",
                artifact=source,
                detail="single positional $1 rewritten to $ARGUMENTS",
            )
        )
    elif positionals - {1}:
        degraded = True
        report.append(
            ReportEntry(
                kind="degraded",
                artifact=source,
                detail="multiple positional args ($2+); native v1 has no"
                " positional support; body left verbatim",
            )
        )
    body, prose_report = rewrite_prose(body, source=source)
    report += prose_report
    degraded = degraded or any(e.kind == "degraded" for e in report)
    return ConvertedDef(
        relpath=f"commands/{name}.md",
        text=_emit_frontmatter(meta, body),
        report=tuple(report),
        degraded=degraded,
    )


def detect_relpath_collisions(defs: list[ConvertedDef]) -> list[ReportEntry]:
    """Two source defs can sanitize to the same emitted relpath (e.g. `a.b` and `a-b` both
    become `a-b`). Publishing both would clobber one file. Group by relpath; for each set of
    two or more, the first in input order wins and the rest are reported as dropped. Pure and
    deterministic: input order decides the winner, output is sorted by relpath. The orchestrator
    (Task 7) wires this in and drops the loser defs from the publish plan."""
    by_relpath: dict[str, list[ConvertedDef]] = {}
    for d in defs:
        by_relpath.setdefault(d.relpath, []).append(d)
    out: list[ReportEntry] = []
    for relpath in sorted(by_relpath):
        group = by_relpath[relpath]
        if len(group) < 2:
            continue
        out.append(
            ReportEntry(
                kind="drop",
                artifact=relpath,
                detail=(
                    f"{len(group)} defs both sanitize to {relpath!r}; keeping the first,"
                    f" dropping the remaining {len(group) - 1}"
                ),
            )
        )
    return out


# A paren-pattern tool entry like Bash(mcp*) -- group 1 is the bare tool name.
_PAREN_TOOL = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)[(].*[)]$")


def _normalize_tools(value) -> list[str]:
    """CC tools: may be a YAML list, a comma/space string, or a bare scalar."""
    if value is None:
        return []
    if isinstance(value, list):
        # YAML null elements (tools: [Read, ~]) would stringify to the noise
        # token 'None' — filter them before coercion
        return [str(v).strip() for v in value if v is not None and str(v).strip()]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,\s]+", value) if part.strip()]
    return [str(value).strip()]


def _map_tool(entry: str, *, source: str, report: list[ReportEntry]) -> str | None:
    """Map one CC tool entry to a native name, or None (dropped). Appends report lines;
    a degraded mapping appends a kind==degraded entry so the caller flags the agent."""
    paren = _PAREN_TOOL.match(entry)
    if paren:
        bare = paren.group(1)
        if bare in CC_TOOL_MAP:
            mapped = CC_TOOL_MAP[bare]
            report.append(
                ReportEntry(
                    kind="degraded",
                    artifact=source,
                    detail=f"{entry} arg-scope dropped -> `{mapped}` (arg-scoping lives in the"
                    f" permission engine natively, not the agent allowlist)",
                )
            )
            return mapped
        report.append(
            ReportEntry(
                kind="drop",
                artifact=source,
                detail=f"{entry} pattern over unknown tool {bare!r}; dropped",
            )
        )
        return None
    if entry in CC_TOOL_MAP:
        mapped = CC_TOOL_MAP[entry]
        report.append(
            ReportEntry(kind="rewrite", artifact=source, detail=f"tool {entry} -> {mapped}")
        )
        return mapped
    if entry in NO_NATIVE_PARITY:
        report.append(
            ReportEntry(
                kind="degraded",
                artifact=source,
                detail=f"tool {entry} has no native parity; dropped",
            )
        )
        return None
    if entry.startswith("mcp__"):
        return entry  # a plugin/mcp tool name is valid natively, kept verbatim
    report.append(
        ReportEntry(
            kind="drop",
            artifact=source,
            detail=f"unknown tool {entry!r} dropped (not in the parity table)",
        )
    )
    return None


def convert_agent(raw: "RawDef", *, catalog: Catalog) -> ConvertedDef:
    source = f"agents/{raw.name}.md"
    name = _sanitize_name(str(raw.meta.get("name", raw.name)))
    report: list[ReportEntry] = []
    if name != raw.name:
        report.append(
            ReportEntry(
                kind="meta",
                artifact=source,
                detail=f"agent name {raw.name!r} sanitized to {name!r}",
            )
        )
    meta: dict = {"name": name}
    meta["description"] = (
        raw.meta["description"]
        if isinstance(raw.meta.get("description"), str)
        else f"Imported {name}"
    )

    declared = _normalize_tools(raw.meta.get("tools"))
    if "tools" in raw.meta:
        mapped: list[str] = []
        for entry in declared:
            target = _map_tool(entry, source=source, report=report)
            if target is not None and target not in mapped:
                mapped.append(target)
        if mapped:
            meta["tools"] = mapped
        elif declared:
            report.append(
                ReportEntry(
                    kind="degraded",
                    artifact=source,
                    detail="all declared tools dropped; `tools` omitted (the"
                    " agent now sees ALL native tools) -- review needed",
                )
            )

    model = raw.meta.get("model")
    if isinstance(model, str) and model:
        try:
            catalog.resolve(model)
            meta["model"] = model
        except UnknownAliasError:
            report.append(
                ReportEntry(
                    kind="drop",
                    artifact=source,
                    detail=f"model alias {model!r} not in the catalog;"
                    " model dropped (defaults to the kernel model)",
                )
            )

    for key in sorted(raw.meta):
        if key not in {"name", "description", "tools", "model"}:
            report.append(
                ReportEntry(
                    kind="drop", artifact=source, detail=f"custom frontmatter key `{key}` dropped"
                )
            )

    body, prose_report = rewrite_prose(raw.body, source=source)
    report += prose_report
    degraded = any(e.kind == "degraded" for e in report)
    return ConvertedDef(
        relpath=f"agents/{name}.md",
        text=_emit_frontmatter(meta, body),
        report=tuple(report),
        degraded=degraded,
    )
