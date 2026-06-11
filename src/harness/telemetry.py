"""Telemetry: a derived SQLite store folded from session logs.

DERIVED means disposable: rebuild_index() deletes the DB and re-reads the
JSONL source of truth. Reading is LENIENT -- structurally incomplete lines
(live sessions are mid-write) are skipped without mutation; repair belongs
to the log reader, never to telemetry. A "run" is a top-level session plus
its descendant subagent sessions, followed recursively through
SessionStarted.parent_session_id.

Cost = stamped per-call pricing x input/output tokens; NULL when the call
carried no pricing. Cache tokens are reported but not separately priced
(cache-aware costing waits on fixture-verified Anthropic semantics).
"""

import json
import re
import sqlite3
from pathlib import Path

from harness.events import (
    CustomEvent,
    Envelope,
    HookDecided,
    ModelCallCompleted,
    RetryAttempted,
    SessionEnded,
    SessionOutcome,
    SessionResumed,
    SessionStarted,
    TaskOutcome,
    ToolCallAborted,
    ToolCallCancelled,
    ToolCallCompleted,
    ToolCallProposed,
    parse_envelope_line,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    parent_session_id TEXT,
    default_model TEXT,
    started_ts REAL,
    ended_ts REAL,
    resumed_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS model_calls (
    session_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,
    duration_ms INTEGER,
    cost REAL,
    ts REAL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS tool_calls (
    session_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    tool TEXT,
    origin TEXT,
    is_error INTEGER,
    blocked INTEGER NOT NULL DEFAULT 0,
    asked INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    ts REAL,
    PRIMARY KEY (session_id, call_id)
);
CREATE TABLE IF NOT EXISTS hook_decisions (
    session_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    hook TEXT NOT NULL,
    kind TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS tags (
    session_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (session_id, tag)
);
CREATE TABLE IF NOT EXISTS outcomes (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    score REAL,
    note TEXT,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS retries (
    session_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    reason TEXT,
    seq INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq)
);
"""


def open_store(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    return conn


def _origin(tool: str) -> str | None:
    """mcp__<server>__<tool> -> <server>; None for builtin/native tools.
    Server names are validated to never contain '__', so the first split wins."""
    if not tool.startswith("mcp__"):
        return None
    rest = tool[len("mcp__"):]
    server, sep, _ = rest.partition("__")
    return server if sep and server else None


def index_envelopes(conn: sqlite3.Connection, envelopes: list[Envelope]) -> None:
    """Fold envelopes into rows. INSERT OR REPLACE / OR IGNORE keys make
    re-indexing the same log idempotent (rebuild semantics)."""
    for env in envelopes:
        ev = env.event
        sid = str(env.session_id)
        if isinstance(ev, SessionStarted):
            conn.execute(
                "INSERT OR REPLACE INTO sessions"
                " (session_id, parent_session_id, default_model, started_ts)"
                " VALUES (?,?,?,?)",
                (sid, ev.parent_session_id, ev.default_model, env.ts),
            )
        elif isinstance(ev, SessionResumed):
            conn.execute(
                "UPDATE sessions SET resumed_count = resumed_count + 1 WHERE session_id = ?",
                (sid,),
            )
        elif isinstance(ev, SessionEnded):
            conn.execute("UPDATE sessions SET ended_ts = ? WHERE session_id = ?", (env.ts, sid))
        elif isinstance(ev, ModelCallCompleted):
            usage, pricing = ev.usage, ev.pricing
            cost = None
            if pricing:
                cost = (
                    usage.get("input_tokens", 0) * pricing.get("input_cost_per_token", 0.0)
                    + usage.get("output_tokens", 0) * pricing.get("output_cost_per_token", 0.0)
                )
            conn.execute(
                "INSERT OR REPLACE INTO model_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, ev.call_id, env.seq, ev.model,
                 usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                 usage.get("cache_read_tokens", 0), usage.get("cache_write_tokens", 0),
                 ev.stop_reason, ev.duration_ms, cost, env.ts),
            )
        elif isinstance(ev, ToolCallProposed):
            # tool/origin reflect the PROPOSED name; a hook rewrite to a different tool
            # keeps the original attribution (DispatchResolved is deliberately not folded)
            conn.execute(
                "INSERT OR IGNORE INTO tool_calls (session_id, call_id, tool, origin, ts) VALUES (?,?,?,?,?)",
                (sid, ev.call_id, ev.tool, _origin(str(ev.tool)), env.ts),
            )
        elif isinstance(ev, ToolCallCompleted):
            conn.execute(
                "UPDATE tool_calls SET is_error = ?, duration_ms = ?"
                " WHERE session_id = ? AND call_id = ?",
                (int(ev.is_error), ev.duration_ms, sid, ev.call_id),
            )
        elif isinstance(ev, (ToolCallAborted, ToolCallCancelled)):
            conn.execute(
                "UPDATE tool_calls SET is_error = 1 WHERE session_id = ? AND call_id = ?",
                (sid, ev.call_id),
            )
        elif isinstance(ev, HookDecided):
            kind = ev.decision.get("kind", "?")
            conn.execute(
                "INSERT OR REPLACE INTO hook_decisions VALUES (?,?,?,?,?)",
                (sid, ev.call_id, ev.hook, kind, env.seq),
            )
            if kind == "block":
                conn.execute(
                    "UPDATE tool_calls SET blocked = 1 WHERE session_id = ? AND call_id = ?",
                    (sid, ev.call_id),
                )
            elif kind == "ask":
                conn.execute(
                    "UPDATE tool_calls SET asked = 1 WHERE session_id = ? AND call_id = ?",
                    (sid, ev.call_id),
                )
        elif isinstance(ev, RetryAttempted):
            conn.execute(
                "INSERT OR REPLACE INTO retries VALUES (?,?,?,?,?)",
                (sid, ev.call_id, ev.attempt, ev.reason, env.seq),
            )
        elif isinstance(ev, CustomEvent) and ev.namespace == "harness" and ev.name == "tag":
            tag = str(ev.data.get("tag", ""))
            if tag:
                conn.execute("INSERT OR IGNORE INTO tags VALUES (?,?)", (sid, tag))
        elif isinstance(ev, (TaskOutcome, SessionOutcome)):
            scope = "session" if isinstance(ev, SessionOutcome) else "task"
            conn.execute(
                "INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?,?,?)",
                (sid, env.seq, scope, ev.status, ev.score, ev.note),
            )
    conn.commit()


def _read_lenient(path: Path) -> list[Envelope]:
    '''Complete lines only; a torn tail means a live writer, not corruption.
    Envelope-level corruption still raises -- rebuild_index handles per-session.'''
    envelopes: list[Envelope] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            continue
        envelopes.append(parse_envelope_line(stripped))
    return envelopes


def rebuild_index(base: Path) -> tuple[sqlite3.Connection, list[str]]:
    '''Delete and rebuild the derived store from every session log under base.
    Returns (connection, warnings) -- a session that cannot be indexed is
    skipped with a warning, never fatal (one bad log must not hide the rest).'''
    db = base / "telemetry.db"
    if db.exists():
        db.unlink()
    conn = open_store(db)
    warnings: list[str] = []
    sessions_dir = base / "sessions"
    if not sessions_dir.exists():
        return conn, warnings
    for log in sorted(sessions_dir.glob("*.jsonl")):
        try:
            index_envelopes(conn, _read_lenient(log))
        except Exception as exc:
            warnings.append(f"{log.name}: {type(exc).__name__}: {exc} (skipped)")
    return conn, warnings


def run_sessions(conn: sqlite3.Connection, root: str) -> list[str]:
    '''root plus all descendants, breadth-first.'''
    found = [root]
    frontier = [root]
    while frontier:
        marks = ",".join("?" * len(frontier))
        rows = conn.execute(
            f"SELECT session_id FROM sessions WHERE parent_session_id IN ({marks})", frontier
        ).fetchall()
        frontier = [r[0] for r in rows if r[0] not in found]
        found.extend(frontier)
    return found


def run_rollup(conn: sqlite3.Connection, root: str) -> dict:
    exists = conn.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (root,)
    ).fetchone()
    if exists is None:
        raise KeyError(f"no such session: {root}")
    sids = run_sessions(conn, root)
    marks = ",".join("?" * len(sids))
    mc = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),"
        f" COALESCE(SUM(cache_read_tokens),0), SUM(cost), COALESCE(SUM(duration_ms),0)"
        f" FROM model_calls WHERE session_id IN ({marks})", sids
    ).fetchone()
    tc = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(is_error),0), COALESCE(SUM(blocked),0),"
        f" COALESCE(SUM(asked),0) FROM tool_calls WHERE session_id IN ({marks})", sids
    ).fetchone()
    # outcomes are recorded against the run's root via `harness outcome <root-sid>`;
    # descendants' outcomes are sub-task bookkeeping, not the run verdict
    outcome = conn.execute(
        "SELECT status, score FROM outcomes WHERE session_id = ?"
        " ORDER BY seq DESC LIMIT 1", (root,)
    ).fetchone()
    retries = conn.execute(
        f"SELECT COUNT(*) FROM retries WHERE session_id IN ({marks})", sids
    ).fetchone()[0]
    return {
        "root": root, "sessions": len(sids),
        "model_calls": mc[0], "input_tokens": mc[1], "output_tokens": mc[2],
        "cache_read_tokens": mc[3], "cost": mc[4], "model_ms": mc[5],
        "tool_calls": tc[0], "tool_errors": tc[1], "blocked": tc[2], "asked": tc[3],
        "retries": retries,
        "outcome": outcome[0] if outcome else None,
        "score": outcome[1] if outcome else None,
    }


def stats_summary(conn: sqlite3.Connection, tag: str | None = None) -> dict:
    """Per-model and per-tool aggregates, optionally filtered by tag.

    Tags attach per-session only: child sessions spawned by dispatch_agent do
    NOT inherit the parent's tags — a tag filter shows the tagged session's own
    counts. Use run_rollup for whole-run (parent + descendants) aggregation."""
    where, params = "", []
    if tag:
        where = "WHERE session_id IN (SELECT session_id FROM tags WHERE tag = ?)"
        params = [tag]
    models = conn.execute(
        f"SELECT model, COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),"
        f" COALESCE(SUM(cache_read_tokens),0), SUM(cost)"
        f" FROM model_calls {where} GROUP BY model ORDER BY model", params
    ).fetchall()
    tools = conn.execute(
        f"SELECT tool, COUNT(*), COALESCE(SUM(is_error),0), COALESCE(SUM(blocked),0),"
        f" COALESCE(SUM(asked),0) FROM tool_calls {where} GROUP BY tool ORDER BY tool", params
    ).fetchall()
    sessions = conn.execute(f"SELECT COUNT(*) FROM sessions {where}", params).fetchone()[0]
    retries = conn.execute(f"SELECT COUNT(*) FROM retries {where}", params).fetchone()[0]
    mcp_and = (" AND " + where[len("WHERE "):]) if where else ""
    mcp_servers = conn.execute(
        f"SELECT origin, COUNT(*), COALESCE(SUM(is_error),0), COALESCE(SUM(blocked),0)"
        f" FROM tool_calls WHERE origin IS NOT NULL{mcp_and} GROUP BY origin ORDER BY origin", params
    ).fetchall()
    return {"sessions": sessions, "retries": retries, "models": models, "tools": tools,
            "mcp_servers": mcp_servers}


def _money(cost) -> str:
    return f"${cost:.6f}" if cost is not None else "n/a"


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _safe(text) -> str:
    """Render-side guard: strip terminal escape sequences from log-derived
    strings (tool/model names are model- or server-controlled)."""
    return _ANSI.sub("", str(text))


def render_stats(summary: dict) -> str:
    lines = [
        f"sessions: {summary['sessions']}",
        f"retries: {summary['retries']}",
        "",
        "models:",
    ]
    for model, calls, inp, out, cached, cost in summary["models"]:
        lines.append(
            f"  {_safe(model)}: calls={calls} in={inp} out={out} cached={cached} cost={_money(cost)}"
        )
    lines.append("")
    lines.append("tools:")
    for tool, calls, errors, blocked, asked in summary["tools"]:
        lines.append(
            f"  {_safe(tool)}: calls={calls} errors={errors} blocked={blocked} asked={asked}"
        )
    if summary.get("mcp_servers"):
        lines.append("")
        lines.append("mcp servers:")
        for origin, calls, errors, blocked in summary["mcp_servers"]:
            lines.append(f"  {_safe(origin)}: {calls} calls, {errors} errors, {blocked} blocked")
    return "\n".join(lines)


_COMPARE_FIELDS = (
    "sessions", "model_calls", "input_tokens", "output_tokens", "cache_read_tokens",
    "cost", "model_ms", "tool_calls", "tool_errors", "blocked", "asked", "retries",
    "outcome", "score",
)


def render_compare(a: dict, b: dict) -> str:
    def fmt(value):
        if value is None:
            return "-"
        if isinstance(value, float):
            return f"{value:.6f}".rstrip("0").rstrip(".") or "0"
        return _safe(value)

    width = max(len(f) for f in _COMPARE_FIELDS)
    lines = [f"{'':<{width}}  {a['root'][:12]:>14}  {b['root'][:12]:>14}"]
    for field in _COMPARE_FIELDS:
        lines.append(f"{field:<{width}}  {fmt(a[field]):>14}  {fmt(b[field]):>14}")
    return "\n".join(lines)


class TelemetrySubscriber:
    """Live incremental indexing: drain a Session.bus queue into a store.

    Drain-style (caller decides when to pump — the TUI phase owns scheduling).
    The store stays disposable: rebuild_index() remains the authoritative path;
    a drop-oldest queue overflow loses LIVE rows only, never logged truth."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def drain(self, queue) -> int:
        import asyncio

        envelopes = []
        while True:
            try:
                envelopes.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if envelopes:
            index_envelopes(self._conn, envelopes)
        return len(envelopes)
