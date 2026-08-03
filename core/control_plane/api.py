"""Control Plane V2 — durable operations over the single source of truth.

Every function is small, restart-safe, and records evidence/events where relevant.
No tmux/actuation here (P2). Health is NEVER inferred from absence: an agent with no
fresh evidence is EXPLICITLY stale (`is_stale`), and its `actual_state` defaults to
`unknown`, not `ok`.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from core.control_plane.store import connect, init_db, now_iso, now_ts

_UNKNOWN = "unknown"


def _uid() -> str:
    return uuid.uuid4().hex[:16]


def _c(conn):
    """Return (conn, own?) ensuring schema exists."""
    if conn is not None:
        return conn, False
    conn = connect()
    init_db(conn)
    return conn, True


# ── event log ────────────────────────────────────────────────────────────────
def append_event(source: str, type: str, *, entity_type: str = "", entity_id: str = "",
                 payload: Optional[dict] = None, evidence_ref: str = "",
                 correlation_id: str = "", project_id: str = "", agent_id: str = "",
                 severity: str = "info", owner_action_required: bool = False,
                 action_taken: str = "", dedup_key: str = "", supersedes: int = 0,
                 resolves: int = 0, dedup_window_secs: int = 0, conn=None) -> int:
    """Append one event to the append-only log / CTO inbox. When `dedup_key` +
    `dedup_window_secs` are given, an identical recent event is not re-appended
    (returns the existing id) — so repeated alerts collapse."""
    conn, own = _c(conn)
    try:
        if dedup_key and dedup_window_secs > 0:
            r = conn.execute(
                "SELECT id,ts_epoch FROM event WHERE dedup_key=? ORDER BY id DESC LIMIT 1",
                (dedup_key,)).fetchone()
            if r and (now_ts() - float(r[1])) < dedup_window_secs:
                return r[0]
        cur = conn.execute(
            "INSERT INTO event(ts,ts_epoch,source,type,entity_type,entity_id,payload,"
            "evidence_ref,correlation_id,project_id,agent_id,severity,owner_action_required,"
            "action_taken,dedup_key,supersedes,resolves) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), now_ts(), source, type, entity_type, entity_id,
             json.dumps(payload or {}, default=str), evidence_ref, correlation_id,
             project_id, agent_id or (entity_id if entity_type == "agent" else ""),
             severity, 1 if owner_action_required else 0, action_taken, dedup_key,
             supersedes or None, resolves or None))
        conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


def get_events(*, entity_type: str = "", entity_id: str = "", type: str = "",
               correlation_id: str = "", since_id: int = 0, limit: int = 100, conn=None) -> list:
    conn, own = _c(conn)
    try:
        q = "SELECT id,ts,source,type,entity_type,entity_id,payload,evidence_ref,correlation_id " \
            "FROM event WHERE id>?"
        args = [since_id]
        for col, val in (("entity_type", entity_type), ("entity_id", entity_id),
                         ("type", type), ("correlation_id", correlation_id)):
            if val:
                q += f" AND {col}=?"
                args.append(val)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in conn.execute(q, args).fetchall():
            out.append({"id": r[0], "ts": r[1], "source": r[2], "type": r[3],
                        "entity_type": r[4], "entity_id": r[5],
                        "payload": json.loads(r[6]) if r[6] else {},
                        "evidence_ref": r[7], "correlation_id": r[8]})
        return out
    finally:
        if own:
            conn.close()


# ── project / work_item ──────────────────────────────────────────────────────
def upsert_project(id: str, name: str, root: str, *, priority: int = 100,
                   definition_of_done: str = "", conn=None) -> str:
    conn, own = _c(conn)
    try:
        conn.execute(
            "INSERT INTO project(id,name,root,priority,status,definition_of_done,updated_at) "
            "VALUES(?,?,?,?,'active',?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
            "root=excluded.root,priority=excluded.priority,"
            "definition_of_done=excluded.definition_of_done,updated_at=excluded.updated_at",
            (id, name, root, priority, definition_of_done, now_iso()))
        conn.commit()
        return id
    finally:
        if own:
            conn.close()


def upsert_work_item(id: str, *, goal_id: str = "", project_id: str = "", title: str = "",
                     kind: str = "task", desired_state: str = "", next_safe_action: str = "",
                     status: str = "planned", depends_on: Optional[list] = None,
                     artifact_refs: Optional[list] = None, actual_state: str = _UNKNOWN,
                     conn=None) -> str:
    conn, own = _c(conn)
    try:
        conn.execute(
            "INSERT INTO work_item(id,goal_id,project_id,title,kind,desired_state,actual_state,"
            "next_safe_action,status,depends_on,artifact_refs,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "title=excluded.title,kind=excluded.kind,desired_state=excluded.desired_state,"
            "actual_state=excluded.actual_state,next_safe_action=excluded.next_safe_action,"
            "status=excluded.status,depends_on=excluded.depends_on,"
            "artifact_refs=excluded.artifact_refs,updated_at=excluded.updated_at",
            (id, goal_id, project_id, title, kind, desired_state, actual_state,
             next_safe_action, status, json.dumps(depends_on or []),
             json.dumps(artifact_refs or []), now_iso()))
        conn.commit()
        return id
    finally:
        if own:
            conn.close()


# ── agent (single source of truth) ───────────────────────────────────────────
def upsert_agent(target: str, *, session: str = "", project_id: str = "",
                 conversation_id: str = "", desired_state: str = "", conn=None) -> str:
    conn, own = _c(conn)
    try:
        row = conn.execute("SELECT id FROM agent WHERE target=?", (target,)).fetchone()
        if row:
            conn.execute(
                "UPDATE agent SET session=?,project_id=?,conversation_id=COALESCE(NULLIF(?,''),"
                "conversation_id),desired_state=COALESCE(NULLIF(?,''),desired_state),updated_at=? "
                "WHERE target=?",
                (session, project_id, conversation_id, desired_state, now_iso(), target))
            aid = row[0]
        else:
            aid = _uid()
            conn.execute(
                "INSERT INTO agent(id,target,session,project_id,conversation_id,desired_state,"
                "actual_state,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (aid, target, session, project_id, conversation_id, desired_state,
                 _UNKNOWN, now_iso()))
        conn.commit()
        return aid
    finally:
        if own:
            conn.close()


_AGENT_COLS = ("id", "target", "session", "project_id", "conversation_id", "desired_state",
               "actual_state", "evidence_fresh_at", "responsible_controller", "lease_id",
               "last_action", "updated_at", "lifecycle_state", "first_seen_at", "pid",
               "command", "cwd", "duplicate_of")


def get_agent(target: str, conn=None) -> Optional[dict]:
    conn, own = _c(conn)
    try:
        r = conn.execute(f"SELECT {','.join(_AGENT_COLS)} FROM agent WHERE target=?",
                         (target,)).fetchone()
        return dict(zip(_AGENT_COLS, r)) if r else None
    finally:
        if own:
            conn.close()


def get_registry(conn=None) -> list:
    conn, own = _c(conn)
    try:
        rows = conn.execute(f"SELECT {','.join(_AGENT_COLS)} FROM agent").fetchall()
        return [dict(zip(_AGENT_COLS, r)) for r in rows]
    finally:
        if own:
            conn.close()


def find_agent_by_conversation(conversation_id: str, conn=None) -> Optional[dict]:
    if not conversation_id:
        return None
    conn, own = _c(conn)
    try:
        r = conn.execute(f"SELECT {','.join(_AGENT_COLS)} FROM agent WHERE conversation_id=? "
                         "ORDER BY updated_at DESC LIMIT 1", (conversation_id,)).fetchone()
        return dict(zip(_AGENT_COLS, r)) if r else None
    finally:
        if own:
            conn.close()


def register_agent(target: str, *, session: str = "", project_id: str = "", cwd: str = "",
                   pid: Optional[int] = None, command: str = "", conversation_id: str = "",
                   lifecycle_state: str = "discovered", conn=None) -> dict:
    """Idempotent AgentRegistry upsert. Sets first_seen_at once; updates volatile
    fields. Returns {id, is_new}. Visibility does NOT depend on any allowlist."""
    conn, own = _c(conn)
    try:
        existing = get_agent(target, conn=conn)
        aid = upsert_agent(target, session=session, project_id=project_id,
                           conversation_id=conversation_id, conn=conn)
        conn.execute(
            "UPDATE agent SET pid=?,command=?,cwd=?,"
            "first_seen_at=COALESCE(first_seen_at,?),"
            "lifecycle_state=CASE WHEN lifecycle_state IN ('dead') THEN ? ELSE "
            "COALESCE(lifecycle_state, ?) END, updated_at=? WHERE target=?",
            (pid, command, cwd, now_iso(), lifecycle_state, lifecycle_state, now_iso(), target))
        conn.commit()
        return {"id": aid, "is_new": existing is None}
    finally:
        if own:
            conn.close()


def set_lifecycle(target: str, lifecycle_state: str, *, duplicate_of: str = "",
                  project_id: str = "", responsible_controller: str = "", conn=None) -> None:
    conn, own = _c(conn)
    try:
        conn.execute(
            "UPDATE agent SET lifecycle_state=?,duplicate_of=COALESCE(NULLIF(?,''),duplicate_of),"
            "project_id=COALESCE(NULLIF(?,''),project_id),"
            "responsible_controller=COALESCE(NULLIF(?,''),responsible_controller),updated_at=? "
            "WHERE target=?",
            (lifecycle_state, duplicate_of, project_id, responsible_controller, now_iso(), target))
        conn.commit()
    finally:
        if own:
            conn.close()


def list_agents(conn=None) -> list:
    conn, own = _c(conn)
    try:
        return [row[0] for row in conn.execute("SELECT target FROM agent").fetchall()]
    finally:
        if own:
            conn.close()


def set_agent_state(target: str, actual_state: str, *, controller: str = "",
                    evidence_kind: str = "pane", evidence_ref: str = "", conversation_id: str = "",
                    last_action: str = "", conn=None) -> None:
    """Update the authoritative agent state WITH an evidence row + an event. State is
    only ever advanced from evidence, so `evidence_fresh_at` is the freshness anchor
    that `is_stale` reads (health is never inferred from absence)."""
    conn, own = _c(conn)
    try:
        upsert_agent(target, conversation_id=conversation_id, conn=conn)
        conn.execute(
            "UPDATE agent SET actual_state=?,evidence_fresh_at=?,responsible_controller=?,"
            "last_action=COALESCE(NULLIF(?,''),last_action),"
            "conversation_id=COALESCE(NULLIF(?,''),conversation_id),updated_at=? WHERE target=?",
            (actual_state, now_iso(), controller, last_action, conversation_id, now_iso(), target))
        conn.commit()
        add_evidence("agent", target, evidence_kind, evidence_ref or actual_state, conn=conn)
        append_event(controller or "collector", "agent_state", entity_type="agent",
                     entity_id=target, payload={"actual_state": actual_state}, conn=conn)
    finally:
        if own:
            conn.close()


def is_stale(target: str, *, ttl_secs: int = 120, now: Optional[float] = None, conn=None) -> bool:
    """EXPLICIT staleness: no fresh evidence within ttl → stale/unknown, never 'ok'."""
    from datetime import datetime
    now = now if now is not None else now_ts()
    a = get_agent(target, conn=conn)
    if not a or not a.get("evidence_fresh_at"):
        return True
    try:
        fresh = datetime.fromisoformat(a["evidence_fresh_at"]).timestamp()
    except Exception:  # noqa: BLE001
        return True
    return (now - fresh) > ttl_secs


# ── evidence ─────────────────────────────────────────────────────────────────
def add_evidence(entity_type: str, entity_id: str, kind: str, ref: str, *,
                 hash: str = "", conn=None) -> int:
    conn, own = _c(conn)
    try:
        cur = conn.execute(
            "INSERT INTO evidence(entity_type,entity_id,kind,ref,hash,observed_at,observed_ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (entity_type, entity_id, kind, ref, hash, now_iso(), now_ts()))
        conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


def latest_evidence(entity_type: str, entity_id: str, *, kind: str = "", conn=None) -> Optional[dict]:
    conn, own = _c(conn)
    try:
        q = "SELECT id,kind,ref,hash,observed_at FROM evidence WHERE entity_type=? AND entity_id=?"
        args = [entity_type, entity_id]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY id DESC LIMIT 1"
        r = conn.execute(q, args).fetchone()
        return {"id": r[0], "kind": r[1], "ref": r[2], "hash": r[3], "observed_at": r[4]} if r else None
    finally:
        if own:
            conn.close()


# ── resource lease (arbitration: one holder, fence token, restart-safe) ──────
def acquire_lease(resource: str, holder: str, *, ttl_secs: int = 120, now: Optional[float] = None,
                  conn=None) -> Optional[dict]:
    """Acquire/renew the lease for `resource`. Returns the lease (with a MONOTONIC
    fence_token) or None if another holder holds a non-expired lease. The fence token
    strictly increases per acquisition, so an action carrying an old token (e.g. from
    before a restart) is rejected by `lease_is_current`."""
    now = now if now is not None else now_ts()
    conn, own = _c(conn)
    try:
        r = conn.execute("SELECT lease_id,holder_controller,fence_token,expires_ts "
                         "FROM resource_lease WHERE resource=?", (resource,)).fetchone()
        if r and r[3] is not None and r[3] > now and r[1] != holder:
            return None                                   # held by someone else, not expired
        fence = (r[2] + 1) if r else 1                    # monotonic, always increments
        lease_id = _uid()
        conn.execute(
            "INSERT INTO resource_lease(resource,lease_id,holder_controller,fence_token,"
            "acquired_at,expires_ts) VALUES(?,?,?,?,?,?) ON CONFLICT(resource) DO UPDATE SET "
            "lease_id=excluded.lease_id,holder_controller=excluded.holder_controller,"
            "fence_token=excluded.fence_token,acquired_at=excluded.acquired_at,"
            "expires_ts=excluded.expires_ts",
            (resource, lease_id, holder, fence, now_iso(), now + ttl_secs))
        conn.commit()
        return {"resource": resource, "lease_id": lease_id, "holder": holder,
                "fence_token": fence, "expires_ts": now + ttl_secs}
    finally:
        if own:
            conn.close()


def lease_holder(resource: str, conn=None) -> Optional[dict]:
    conn, own = _c(conn)
    try:
        r = conn.execute("SELECT lease_id,holder_controller,fence_token,expires_ts "
                         "FROM resource_lease WHERE resource=?", (resource,)).fetchone()
        return {"lease_id": r[0], "holder": r[1], "fence_token": r[2], "expires_ts": r[3]} if r else None
    finally:
        if own:
            conn.close()


def lease_is_current(resource: str, lease_id: str, fence_token: int, conn=None) -> bool:
    """An action may proceed only if it carries the current lease_id AND fence token —
    the guard that makes actuation restart-safe and single-owner."""
    h = lease_holder(resource, conn=conn)
    return bool(h and h["lease_id"] == lease_id and h["fence_token"] == fence_token)


def release_lease(resource: str, lease_id: str, conn=None) -> bool:
    conn, own = _c(conn)
    try:
        r = conn.execute("SELECT lease_id FROM resource_lease WHERE resource=?", (resource,)).fetchone()
        if not r or r[0] != lease_id:
            return False
        conn.execute("UPDATE resource_lease SET expires_ts=0 WHERE resource=?", (resource,))
        conn.commit()
        return True
    finally:
        if own:
            conn.close()


# ── owner gate (correlated stop → notify → answer → resume) ──────────────────
def open_gate(*, work_item_id: str = "", agent_id: str = "", reason: str = "", kind: str = "",
              correlation_id: str = "", conn=None) -> dict:
    conn, own = _c(conn)
    try:
        gid = _uid()
        corr = correlation_id or gid
        conn.execute(
            "INSERT INTO owner_gate(id,work_item_id,agent_id,reason,kind,state,correlation_id,"
            "opened_at) VALUES(?,?,?,?,?,'open',?,?)",
            (gid, work_item_id, agent_id, reason, kind, corr, now_iso()))
        conn.commit()
        append_event("controller", "owner_gate_opened", entity_type="owner_gate",
                     entity_id=gid, payload={"reason": reason, "agent_id": agent_id},
                     correlation_id=corr, conn=conn)
        return {"id": gid, "correlation_id": corr, "state": "open"}
    finally:
        if own:
            conn.close()


def answer_gate(gate_id: str, answer: str, conn=None) -> Optional[dict]:
    conn, own = _c(conn)
    try:
        r = conn.execute("SELECT agent_id,work_item_id,correlation_id,state FROM owner_gate "
                         "WHERE id=?", (gate_id,)).fetchone()
        if not r or r[3] not in ("open", "notified"):
            return None
        conn.execute("UPDATE owner_gate SET state='answered',answer=?,answered_at=? WHERE id=?",
                     (answer, now_iso(), gate_id))
        conn.commit()
        append_event("owner", "owner_gate_answered", entity_type="owner_gate", entity_id=gate_id,
                     payload={"answer": answer}, correlation_id=r[2], conn=conn)
        return {"id": gate_id, "agent_id": r[0], "work_item_id": r[1],
                "correlation_id": r[2], "answer": answer}
    finally:
        if own:
            conn.close()


def close_gates(agent_id: str, kinds, *, state: str = "resolved", conn=None) -> list:
    """Close OPEN gates for an agent whose kind is in `kinds`. Callers must pass only
    SYSTEM gate kinds (e.g. actuation_failed) — owner-DECISION gates (scope/business/
    unverified_owner_decision) are never auto-closed; those require a verified
    owner_decision (see provenance)."""
    conn, own = _c(conn)
    try:
        kinds = tuple(kinds)
        if not kinds:
            return []
        ph = ",".join("?" for _ in kinds)
        rows = conn.execute(
            f"SELECT id FROM owner_gate WHERE agent_id=? AND state IN ('open','notified') "
            f"AND kind IN ({ph})", (agent_id, *kinds)).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            conn.executemany("UPDATE owner_gate SET state=?, answered_at=? WHERE id=?",
                             [(state, now_iso(), i) for i in ids])
            conn.commit()
        return ids
    finally:
        if own:
            conn.close()


def get_open_gates(conn=None) -> list:
    conn, own = _c(conn)
    try:
        rows = conn.execute("SELECT id,work_item_id,agent_id,reason,kind,correlation_id,opened_at "
                            "FROM owner_gate WHERE state IN ('open','notified') ORDER BY opened_at").fetchall()
        return [{"id": r[0], "work_item_id": r[1], "agent_id": r[2], "reason": r[3],
                 "kind": r[4], "correlation_id": r[5], "opened_at": r[6]} for r in rows]
    finally:
        if own:
            conn.close()


# ── notification outbox (durable, deduped, with delivery state) ──────────────
def enqueue_notification(*, event_id: int = 0, channel: str, dedup_key: str,
                         correlation_id: str = "", conn=None) -> dict:
    conn, own = _c(conn)
    try:
        r = conn.execute("SELECT id,state FROM notification WHERE channel=? AND dedup_key=? "
                         "AND state IN ('pending','sending','sent','acked') ORDER BY id DESC LIMIT 1",
                         (channel, dedup_key)).fetchone()
        if r:
            return {"id": r[0], "state": r[1], "deduped": True}
        cur = conn.execute(
            "INSERT INTO notification(event_id,channel,dedup_key,state,attempts,correlation_id,"
            "created_at) VALUES(?,?,?,'pending',0,?,?)",
            (event_id, channel, dedup_key, correlation_id, now_iso()))
        conn.commit()
        return {"id": cur.lastrowid, "state": "pending", "deduped": False}
    finally:
        if own:
            conn.close()


def mark_notification(notif_id: int, state: str, *, receipt: str = "", conn=None) -> None:
    conn, own = _c(conn)
    try:
        conn.execute("UPDATE notification SET state=?,attempts=attempts+?,last_attempt_at=?,"
                     "receipt=COALESCE(NULLIF(?,''),receipt) WHERE id=?",
                     (state, 1 if state in ("sending", "failed") else 0, now_iso(), receipt, notif_id))
        conn.commit()
    finally:
        if own:
            conn.close()


def pending_notifications(conn=None) -> list:
    conn, own = _c(conn)
    try:
        rows = conn.execute("SELECT id,event_id,channel,dedup_key,attempts,correlation_id "
                            "FROM notification WHERE state IN ('pending','failed') ORDER BY id").fetchall()
        return [{"id": r[0], "event_id": r[1], "channel": r[2], "dedup_key": r[3],
                 "attempts": r[4], "correlation_id": r[5]} for r in rows]
    finally:
        if own:
            conn.close()


# ── decision + budget ────────────────────────────────────────────────────────
def record_decision(entity_type: str, entity_id: str, policy_class: str, action: str,
                    rationale: str, *, model_tier: str = "deterministic", conn=None) -> int:
    conn, own = _c(conn)
    try:
        cur = conn.execute(
            "INSERT INTO decision(ts,entity_type,entity_id,policy_class,action,rationale,model_tier) "
            "VALUES(?,?,?,?,?,?,?)",
            (now_iso(), entity_type, entity_id, policy_class, action, rationale, model_tier))
        conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


def upsert_budget(scope: str, *, model: str = "", tokens: int = 0, cost_usd: float = 0.0,
                  cpu: Optional[float] = None, ram: Optional[float] = None,
                  disk: Optional[float] = None, window: str = "", conn=None) -> None:
    conn, own = _c(conn)
    try:
        conn.execute(
            "INSERT INTO budget(scope,model,tokens,cost_usd,cpu,ram,disk,window,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET model=excluded.model,"
            "tokens=excluded.tokens,cost_usd=excluded.cost_usd,cpu=excluded.cpu,ram=excluded.ram,"
            "disk=excluded.disk,window=excluded.window,updated_at=excluded.updated_at",
            (scope, model, tokens, cost_usd, cpu, ram, disk, window, now_iso()))
        conn.commit()
    finally:
        if own:
            conn.close()


def upsert_channel(name: str, *, enabled: bool, kind: str = "", config_ref: str = "",
                   healthy: Optional[bool] = None, last_error: str = "", conn=None) -> None:
    conn, own = _c(conn)
    try:
        ok_at = now_iso() if healthy else None
        conn.execute(
            "INSERT INTO channel(name,enabled,kind,config_ref,healthy,last_ok_at,last_error,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "enabled=excluded.enabled,kind=excluded.kind,config_ref=excluded.config_ref,"
            "healthy=excluded.healthy,last_ok_at=COALESCE(excluded.last_ok_at,channel.last_ok_at),"
            "last_error=excluded.last_error,updated_at=excluded.updated_at",
            (name, 1 if enabled else 0, kind, config_ref, 1 if healthy else 0, ok_at,
             last_error, now_iso()))
        conn.commit()
    finally:
        if own:
            conn.close()


def get_channel(name: str, conn=None) -> Optional[dict]:
    conn, own = _c(conn)
    try:
        r = conn.execute("SELECT name,enabled,kind,config_ref,healthy,last_ok_at,last_error,"
                         "updated_at FROM channel WHERE name=?", (name,)).fetchone()
        cols = ("name", "enabled", "kind", "config_ref", "healthy", "last_ok_at",
                "last_error", "updated_at")
        if not r:
            return None
        d = dict(zip(cols, r))
        d["enabled"] = bool(d["enabled"])
        d["healthy"] = bool(d["healthy"])
        return d
    finally:
        if own:
            conn.close()


def list_channels(conn=None) -> list:
    conn, own = _c(conn)
    try:
        rows = conn.execute("SELECT name FROM channel").fetchall()
        return [get_channel(r[0], conn=conn) for r in rows]
    finally:
        if own:
            conn.close()


def get_budget(scope: str, conn=None) -> Optional[dict]:
    conn, own = _c(conn)
    try:
        r = conn.execute("SELECT scope,model,tokens,cost_usd,cpu,ram,disk,window,updated_at "
                         "FROM budget WHERE scope=?", (scope,)).fetchone()
        cols = ("scope", "model", "tokens", "cost_usd", "cpu", "ram", "disk", "window", "updated_at")
        return dict(zip(cols, r)) if r else None
    finally:
        if own:
            conn.close()
