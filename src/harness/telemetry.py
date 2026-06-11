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

import json  # noqa: F401  (used by rebuild/_read_lenient in Task 2)
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
    parse_envelope_line,  # noqa: F401  (used by _read_lenient in Task 2)
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
            conn.execute(
                "INSERT OR IGNORE INTO tool_calls (session_id, call_id, tool, ts) VALUES (?,?,?,?)",
                (sid, ev.call_id, ev.tool, env.ts),
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
