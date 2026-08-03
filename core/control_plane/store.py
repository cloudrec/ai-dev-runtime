"""Control Plane V2 durable store — schema + connection + migrations.

A dedicated SQLite database (`control_plane.db`), separate from the legacy
`agent_control.db` so P0 is fully additive and reversible (drop the file to roll
back). Schema is versioned; `init_db()` is idempotent and forward-only.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone

SCHEMA_VERSION = 2


def db_path() -> str:
    return os.getenv("CONTROL_PLANE_DB", "/root/ai-dev-runtime/control_plane.db")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts() -> float:
    return time.time()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    id INTEGER PRIMARY KEY CHECK (id=1), version INTEGER, updated_at TEXT);

-- append-only event bus/log = the ONE event table AND the canonical CTO inbox.
-- (v2) enriched with the CTO push contract fields.
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ts_epoch REAL, source TEXT,
    type TEXT, entity_type TEXT, entity_id TEXT, payload TEXT,
    evidence_ref TEXT, correlation_id TEXT,
    project_id TEXT, agent_id TEXT, severity TEXT DEFAULT 'info',
    owner_action_required INTEGER DEFAULT 0, action_taken TEXT,
    dedup_key TEXT, supersedes INTEGER, resolves INTEGER);
CREATE INDEX IF NOT EXISTS ix_event_entity ON event(entity_type, entity_id, id);
CREATE INDEX IF NOT EXISTS ix_event_corr ON event(correlation_id);
CREATE INDEX IF NOT EXISTS ix_event_dedup ON event(dedup_key);

-- per-CTO-consumer cursor: what a ChatGPT/CTO consumer has already acknowledged, so
-- the next invocation reads exactly the deltas since last time (restart-safe).
CREATE TABLE IF NOT EXISTS cto_cursor (
    consumer TEXT PRIMARY KEY, last_event_id INTEGER DEFAULT 0, updated_at TEXT);

-- notification channel registry + health (a disabled/failed channel is a BLOCKER,
-- never a silent healthy state).
CREATE TABLE IF NOT EXISTS channel (
    name TEXT PRIMARY KEY, enabled INTEGER DEFAULT 0, kind TEXT, config_ref TEXT,
    healthy INTEGER DEFAULT 0, last_ok_at TEXT, last_error TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS project (
    id TEXT PRIMARY KEY, name TEXT, root TEXT, priority INTEGER DEFAULT 100,
    status TEXT DEFAULT 'active', definition_of_done TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS goal (
    id TEXT PRIMARY KEY, project_id TEXT, text TEXT, priority INTEGER DEFAULT 100,
    status TEXT DEFAULT 'open', dod TEXT, stop_conditions TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS work_item (
    id TEXT PRIMARY KEY, goal_id TEXT, project_id TEXT, title TEXT, kind TEXT,
    desired_state TEXT, actual_state TEXT DEFAULT 'unknown',
    next_safe_action TEXT, status TEXT DEFAULT 'planned', depends_on TEXT,
    artifact_refs TEXT, updated_at TEXT);

-- one row = authoritative per-agent truth (desired vs actual, EXPLICIT unknown/stale).
-- (v2) AgentRegistry lifecycle + discovery fields — visibility never depends on a
-- static allowlist; static policy limits ACTIONS only.
CREATE TABLE IF NOT EXISTS agent (
    id TEXT PRIMARY KEY, target TEXT UNIQUE, session TEXT, project_id TEXT,
    conversation_id TEXT, desired_state TEXT, actual_state TEXT DEFAULT 'unknown',
    evidence_fresh_at TEXT, responsible_controller TEXT, lease_id TEXT,
    last_action TEXT, updated_at TEXT,
    lifecycle_state TEXT DEFAULT 'discovered', first_seen_at TEXT, pid INTEGER,
    command TEXT, cwd TEXT, duplicate_of TEXT);

CREATE TABLE IF NOT EXISTS agent_turn (
    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT, conversation_id TEXT,
    started_at TEXT, ended_at TEXT, summary_ref TEXT, tokens INTEGER, outcome TEXT);

CREATE TABLE IF NOT EXISTS decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, entity_type TEXT, entity_id TEXT,
    policy_class TEXT, action TEXT, rationale TEXT, model_tier TEXT);

CREATE TABLE IF NOT EXISTS owner_gate (
    id TEXT PRIMARY KEY, work_item_id TEXT, agent_id TEXT, reason TEXT, kind TEXT,
    state TEXT DEFAULT 'open', correlation_id TEXT, answer TEXT,
    opened_at TEXT, notified_at TEXT, answered_at TEXT);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT, entity_id TEXT,
    kind TEXT, ref TEXT, hash TEXT, observed_at TEXT, observed_ts REAL);
CREATE INDEX IF NOT EXISTS ix_evidence_entity ON evidence(entity_type, entity_id, id);

CREATE TABLE IF NOT EXISTS notification (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER, channel TEXT,
    dedup_key TEXT, state TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
    last_attempt_at TEXT, receipt TEXT, correlation_id TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_notif_state ON notification(state);

CREATE TABLE IF NOT EXISTS budget (
    scope TEXT PRIMARY KEY, model TEXT, tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0, cpu REAL, ram REAL, disk REAL, window TEXT, updated_at TEXT);

-- the arbitration primitive: at most ONE holder per resource, with a fence token
CREATE TABLE IF NOT EXISTS resource_lease (
    resource TEXT PRIMARY KEY, lease_id TEXT, holder_controller TEXT,
    fence_token INTEGER, acquired_at TEXT, expires_ts REAL);

CREATE TABLE IF NOT EXISTS policy (
    id INTEGER PRIMARY KEY AUTOINCREMENT, action_pattern TEXT, policy_class TEXT,
    scope TEXT, rationale TEXT);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    own = conn is None
    conn = conn or connect()
    conn.executescript(_SCHEMA)
    # Forward-only additive migrations for a DB created at an earlier version. New
    # DBs already have every column from _SCHEMA; these ALTERs are no-ops there.
    _V2_COLS = {
        "agent": ["lifecycle_state TEXT DEFAULT 'discovered'", "first_seen_at TEXT",
                  "pid INTEGER", "command TEXT", "cwd TEXT", "duplicate_of TEXT"],
        "event": ["project_id TEXT", "agent_id TEXT", "severity TEXT DEFAULT 'info'",
                  "owner_action_required INTEGER DEFAULT 0", "action_taken TEXT",
                  "dedup_key TEXT", "supersedes INTEGER", "resolves INTEGER"],
    }
    for table, cols in _V2_COLS.items():
        for coldef in cols:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
            except sqlite3.OperationalError:
                pass                                      # column already exists
    row = conn.execute("SELECT version FROM schema_meta WHERE id=1").fetchone()
    if not row:
        conn.execute("INSERT INTO schema_meta(id,version,updated_at) VALUES(1,?,?)",
                     (SCHEMA_VERSION, now_iso()))
    elif row[0] != SCHEMA_VERSION:
        # forward-only migrations would run here; P0 is version 1
        conn.execute("UPDATE schema_meta SET version=?, updated_at=? WHERE id=1",
                     (SCHEMA_VERSION, now_iso()))
    conn.commit()
    if own:
        return conn
    return conn


def schema_version(conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or connect()
    try:
        init_db(conn)
        r = conn.execute("SELECT version FROM schema_meta WHERE id=1").fetchone()
        return r[0] if r else 0
    finally:
        if own:
            conn.close()
