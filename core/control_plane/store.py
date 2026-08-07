"""Control Plane V2 durable store — schema + connection + migrations.

A dedicated SQLite database (`control_plane.db`), separate from the legacy
`agent_control.db` so P0 is fully additive and reversible (drop the file to roll
back). Schema is versioned; `init_db()` is idempotent and forward-only.
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = 9

# Identity of THIS process run. Delivery health is evidence-scoped to a runtime: a proof
# recorded by a PREVIOUS process (before a restart/redeploy) is history, not a live claim
# that the channel works now. See `channel.proof_epoch` and delivery.refresh_channel_health.
_RUNTIME_EPOCH = ""


def new_runtime_epoch() -> str:
    """Start a new runtime epoch and return it. Called once at import (process start);
    tests call it to honestly simulate a service restart."""
    global _RUNTIME_EPOCH
    _RUNTIME_EPOCH = f"{os.getpid()}:{int(time.time() * 1000)}:{uuid.uuid4().hex[:8]}"
    return _RUNTIME_EPOCH


def runtime_epoch() -> str:
    """The current process' epoch. Stable for the life of the process."""
    return _RUNTIME_EPOCH or new_runtime_epoch()


def db_path() -> str:
    return os.getenv("CONTROL_PLANE_DB", "/root/ai-dev-runtime/control_plane.db")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts() -> float:
    return time.time()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    id INTEGER PRIMARY KEY CHECK (id=1), version INTEGER, updated_at TEXT);

-- Forward-only enforced by the DATABASE, not only by the writer. Fixing init_db protects
-- processes running the fixed code; it cannot protect against a process that imported an
-- OLDER build and is still alive (a daemon polling every 20s keeps rewriting the version
-- backwards from memory, and restarting that daemon is not always available). The trigger
-- silently ignores any UPDATE that would lower the version, so the recorded number can
-- never regress below the schema that is actually present.
CREATE TRIGGER IF NOT EXISTS trg_schema_meta_forward_only
    BEFORE UPDATE ON schema_meta
    WHEN NEW.version < OLD.version
    BEGIN SELECT RAISE(IGNORE); END;

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
-- `state` is the tri-state truth: 'unverified' (no proof in THIS runtime — not green),
-- 'healthy' (a delivery was proven), 'unhealthy' (a send was proven to fail / channel is
-- misconfigured). `healthy` is the legacy mirror (1 only when state='healthy').
-- `proof_epoch` is the runtime epoch that produced the current state; a row whose proof
-- comes from an earlier epoch degrades to 'unverified' on the next health refresh.
-- `last_proof` keeps the receipt/evidence of the last proven delivery.
CREATE TABLE IF NOT EXISTS channel (
    name TEXT PRIMARY KEY, enabled INTEGER DEFAULT 0, kind TEXT, config_ref TEXT,
    healthy INTEGER DEFAULT 0, last_ok_at TEXT, last_error TEXT, updated_at TEXT,
    state TEXT DEFAULT 'unverified', proof_epoch TEXT, last_proof TEXT);

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

-- (v3) owner-decision PROVENANCE: an owner-gated action may ONLY proceed from a durable,
-- authenticated owner_decision — never from raw pane text / UI answer summaries / model
-- defaults / resumed transcript / automation prose. Records the source channel,
-- authenticated actor, timestamp, the question/gate it answers, the exact answer, and a
-- consumption state (so a duplicate answer or an answer to the wrong question is rejected).
CREATE TABLE IF NOT EXISTS owner_decision (
    id TEXT PRIMARY KEY, question_id TEXT, gate_id TEXT, source_channel TEXT,
    actor TEXT, authenticated INTEGER DEFAULT 0, answer TEXT, decided_at TEXT,
    consumption_state TEXT DEFAULT 'pending', created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_decision_q ON owner_decision(question_id);

-- (v4) canonical Actuator ledger: idempotency + attempt record for every command, keyed
-- by (target, conversation, action) and STAMPED with the lease/fence it acted under, so a
-- stale-fence (post-restart) actuation is rejected and never duplicated.
CREATE TABLE IF NOT EXISTS cp_action (
    idkey TEXT PRIMARY KEY, target TEXT, conversation_id TEXT, action_hash TEXT,
    controller TEXT, lease_id TEXT, fence_token INTEGER, kind TEXT, policy_class TEXT,
    submitted INTEGER DEFAULT 0, verified INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0,
    attempts INTEGER DEFAULT 0, outcome TEXT, created_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_cp_action_target ON cp_action(target, action_hash);

-- (v6) Owner OS Operating Constitution enforcement. Every policy evaluation — preflight
-- before an action and the completion gate before DONE — writes a row here, allowed or
-- blocked. An action that produced no row was never evaluated, which is itself a finding.
CREATE TABLE IF NOT EXISTS policy_decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, phase TEXT, actor TEXT, project TEXT,
    task_id TEXT, action TEXT, risk_class TEXT, decision TEXT, rules TEXT,
    missing_evidence TEXT, evidence TEXT, override_id TEXT, idem_key TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS ix_policy_decision_task ON policy_decision(task_id, id);
CREATE INDEX IF NOT EXISTS ix_policy_decision_dec ON policy_decision(decision, id);

-- Emergency override: owner-scoped, expiring, single-purpose. It can permit an action the
-- policy blocks — it can never hide that it did (`used`/`decision` rows keep the trail).
CREATE TABLE IF NOT EXISTS policy_override (
    id TEXT PRIMARY KEY, created_at TEXT, actor TEXT, scope TEXT, rules TEXT, reason TEXT,
    task_id TEXT, expires_at TEXT, expires_ts REAL, uses INTEGER DEFAULT 0,
    revoked_at TEXT);

-- One live claim per (project, idempotency key): the duplicate-agent / repeated-
-- irreversible-action guard. A claim is released when its task reaches a terminal state.
CREATE TABLE IF NOT EXISTS policy_claim (
    idem_key TEXT PRIMARY KEY, task_id TEXT, actor TEXT, project TEXT, action TEXT,
    risk_class TEXT, state TEXT DEFAULT 'active', created_at TEXT, created_ts REAL,
    released_at TEXT);
CREATE INDEX IF NOT EXISTS ix_policy_claim_project ON policy_claim(project, state);

-- (v7) WORK EVIDENCE cursor. Owner OS used to notice work only when an agent's observable
-- STATE changed or a stage pointer moved. A finished report, a partial completion, an
-- abandoned goal and fresh commits were all invisible if the pointer stood still (live
-- 2026-08-06: MESS shipped goal 2, declared goal 1 not started, went idle — zero events).
-- One row per piece of evidence already seen, so the same report is never re-announced.
CREATE TABLE IF NOT EXISTS work_evidence (
    id TEXT PRIMARY KEY, project TEXT, target TEXT, kind TEXT, ref TEXT,
    fingerprint TEXT, first_seen_at TEXT, last_seen_at TEXT, event_id INTEGER);
CREATE INDEX IF NOT EXISTS ix_work_evidence_project ON work_evidence(project, kind);

-- (v8) One row per owner-action MEANING — not per set of bytes. `work_evidence` already
-- fingerprints content so a rewritten report is news again; that is correct for the audit
-- trail and wrong for the owner, who was woken three times in sixteen minutes for one piece
-- of work being saved three times. This table is what makes a repeat interruption
-- suppressible while every material change still reaches the inbox.
--
-- (v9) The MEANING is `project + report + semantic reason` — `meaning_key` is
-- `<evidence_key>|<class_sig>`, and `evidence_key` keeps the report it belongs to. v8 kept a
-- single row per REPORT holding its latest classification, which bounded a stream of
-- identical saves but not a report that FLAPS: partial → blocked → partial → blocked each
-- read as "the classification changed" and woke the owner every time — four interruptions in
-- fifteen minutes for two decisions he had already been told. Giving every meaning its own
-- window keeps the invariant that matters: nothing is ever suppressed unless the owner was
-- told THAT SAME THING within the cooldown.
--
-- Durable ON PURPOSE: an in-memory timer would forget the cooldown across the 5-minute tick
-- and, worse, across a restart — turning a redeploy into a fresh round of owner wakes. The
-- comparison columns (not just the timestamp) are stored so a change within one meaning — a
-- different task or stage, a rise in severity — can re-open delivery immediately instead of
-- waiting out a cooldown that no longer describes the situation.
CREATE TABLE IF NOT EXISTS work_evidence_push (
    meaning_key TEXT PRIMARY KEY, evidence_key TEXT, project TEXT, target TEXT, ref TEXT,
    kind TEXT, class_sig TEXT, task_id TEXT, stage_pointer TEXT, severity TEXT,
    last_push_at TEXT, last_event_id INTEGER DEFAULT 0,
    suppressed_count INTEGER DEFAULT 0, last_seen_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_work_evidence_push_project
    ON work_evidence_push(project, kind);
-- NOTE: the index on `evidence_key` is created AFTER the ALTERs below, not here. This
-- script runs first, and on an existing DB `CREATE TABLE IF NOT EXISTS` is a no-op — so a
-- v8 table is still missing the column an index here would name, and `init_db` would raise
-- `no such column: evidence_key` on every start against a live database.
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
        # v5: evidence-scoped channel health. Existing rows adopt 'unverified' — a DB that
        # was written by an older build carries no proof for this runtime, so it must not
        # come back green on upgrade.
        "channel": ["state TEXT DEFAULT 'unverified'", "proof_epoch TEXT", "last_proof TEXT"],
        # v9: the owner-action window is per MEANING, so the row needs to remember which
        # report it belongs to, and when this meaning was last observed (not just delivered).
        "work_evidence_push": ["evidence_key TEXT", "last_seen_at TEXT"],
    }
    added = set()
    for table, cols in _V2_COLS.items():
        for coldef in cols:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
                added.add((table, coldef.split()[0]))
            except sqlite3.OperationalError:
                pass                                      # column already exists
    if ("channel", "last_proof") in added:
        # One-time v5 hygiene, on the upgrade write only. Pre-v5 `owner_push.last_ok_at`
        # was stamped by the CONFIG probe, not by a delivery, so it is indistinguishable
        # from a real receipt and must not be carried forward as proof (it is what made
        # `verified` true for a channel that had never delivered anything).
        conn.execute("UPDATE channel SET last_ok_at=NULL "
                     "WHERE name='owner_push' AND last_proof IS NULL")
    # v8 → v9 re-key. A v8 row is keyed by the report alone; the v9 key is
    # `<report>|<class_sig>`. Rewriting those rows (rather than dropping them) is what keeps
    # a LIVE cooldown alive across the deploy — a fresh key would read as `first_time` and
    # let the upgrade itself wake the owner for work he was told about minutes earlier: the
    # exact interruption this table exists to prevent, caused by the fix for it.
    #
    # Conditioned on the DATA, not on whether the ALTER above happened to fire, so it is
    # idempotent and still correct if the column arrived by any other route. `evidence_key`
    # is NOT NULL on every row v9 writes, so a converged DB matches nothing.
    conn.execute("UPDATE work_evidence_push SET evidence_key=meaning_key, "
                 "last_seen_at=COALESCE(last_seen_at, updated_at), "
                 "meaning_key=meaning_key || '|' || COALESCE(class_sig,'') "
                 "WHERE evidence_key IS NULL")
    # Safe only now that the column is guaranteed to exist on old and new DBs alike.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_work_evidence_push_evidence "
                 "ON work_evidence_push(evidence_key)")
    row = conn.execute("SELECT version FROM schema_meta WHERE id=1").fetchone()
    if not row:
        conn.execute("INSERT INTO schema_meta(id,version,updated_at) VALUES(1,?,?)",
                     (SCHEMA_VERSION, now_iso()))
    elif row[0] < SCHEMA_VERSION:
        # FORWARD-ONLY, as the module contract says. The old `!=` also fired when the
        # stored version was NEWER than this process' constant, so any long-lived process
        # that imported an older build kept rewriting the number BACKWARDS — live
        # 2026-08-06: the wake companion (started 20:05, constant 4) and the restarted
        # runtime (constant 7) flipped `schema_meta` every 20-30s while every v5/v6/v7
        # object was actually present. A downgrade write is never a migration.
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
