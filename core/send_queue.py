"""Durable queue for messages to a busy agent (2026-11-17).

`agent_send` refuses while an agent is mid-turn, because Claude Code would queue the
text behind the active turn and report nothing. The caller (the GPT tech lead) then has
to remember to retry, and the instruction is lost when it does not. This queue keeps
such a message durably and delivers it once the agent is idle — through the SAME
`agent_send`, so every check runs again at delivery time.

Guarantees:
  * idempotent: one row per idempotency key; the same key with other text is refused;
    delivery reuses the key, so agent_control's own delivery record can never send twice
    (a crash between claim and receipt re-queues the row, and the replay is a no-op);
  * bound to the agent it was meant for: the pane's tmux id and process id are recorded
    at enqueue time, and a queued message is never delivered to a pane that is no longer
    that process (a renamed/reused session name, a restarted agent) — it is refused;
  * policy twice: the automation policy (payment hard-deny included) is checked when the
    message is queued AND again by agent_send when it is delivered, so a right revoked in
    between, or a target that became payment work, refuses it;
  * no stale approvals: an owner proof is never queued (proofs are single-use and expire
    in minutes); queued messages carry no authority beyond the caller's own;
  * bounded: every row expires (default SEND_QUEUE_TTL_SECS) and can be cancelled;
  * auditable: every row ends delivered | refused | expired | cancelled with a receipt.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Callable, Optional

TTL_SECS = int(os.getenv("SEND_QUEUE_TTL_SECS", "1800"))
MAX_TTL_SECS = 6 * 3600
CLAIM_STALE_SECS = 120          # a claim older than this was lost in a crash: re-queue
TERMINAL = ("delivered", "refused", "expired", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS send_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE NOT NULL,
    target TEXT NOT NULL, pane_id TEXT, pane_pid INTEGER,
    text TEXT NOT NULL, text_sha TEXT NOT NULL,
    actor TEXT, source TEXT,
    created_ts REAL, expires_ts REAL,
    status TEXT NOT NULL DEFAULT 'queued',
    claimed_ts REAL, attempts INTEGER DEFAULT 0, last_reason TEXT DEFAULT '',
    done_ts REAL, receipt TEXT DEFAULT ''
)
"""


def _conn():
    from core.control_db import control_db_path
    conn = sqlite3.connect(control_db_path(), timeout=10)
    conn.execute(_SCHEMA)
    return conn


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _row(conn, key: str) -> Optional[dict]:
    cur = conn.execute("SELECT * FROM send_queue WHERE idempotency_key=?", (key,))
    r = cur.fetchone()
    return dict(zip([d[0] for d in cur.description], r)) if r else None


def _public(row: dict) -> dict:
    out = {k: row[k] for k in ("id", "idempotency_key", "target", "status", "attempts",
                               "last_reason", "created_ts", "expires_ts", "done_ts")}
    out["queued"] = row["status"] == "queued"
    try:
        out["receipt"] = json.loads(row.get("receipt") or "{}")
    except ValueError:
        out["receipt"] = {}
    return out


def enqueue(target: str, text: str, *, idempotency_key: str, actor: Optional[str] = None,
            source: Optional[str] = None, ttl_secs: Optional[int] = None,
            now: Optional[float] = None) -> dict:
    """Queue `text` for `target`. Refuses (never queues) what policy refuses now."""
    from core import agent_control as ac
    now = time.time() if now is None else now
    key = (idempotency_key or "").strip()
    if not key:
        return {"queued": False, "refused": "idempotency_key_required"}
    if not (text or "").strip():
        return {"queued": False, "refused": "empty_text"}
    ttl = max(1, min(int(ttl_secs or TTL_SECS), MAX_TTL_SECS))
    conn = _conn()
    try:
        prior = _row(conn, key)
        if prior:
            if prior["text_sha"] != _sha(text):
                return {"queued": False, "refused": "idempotency_key_reused_with_other_text"}
            return {**_public(prior), "duplicate": True}
        try:
            pane = ac._pane_is_live_agent(target)
        except ac.AgentControlError as e:
            return {"queued": False, "refused": "no_such_agent", "reason": str(e)[:200]}
        resolved = pane["target"]
        refusal, _ = ac.automation_decision(
            pane.get("session") or resolved.split(":")[0], resolved, actor,
            action="agent_send", cwd=pane.get("claude_cwd") or pane.get("cwd"))
        if refusal:
            return {"queued": False, "refused": refusal}
        try:
            conn.execute(
                "INSERT INTO send_queue (idempotency_key,target,pane_id,pane_pid,text,"
                "text_sha,actor,source,created_ts,expires_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (key, resolved, pane.get("pane_id") or "", pane.get("pid"), text, _sha(text),
                 actor or "", source or "", now, now + ttl))
            conn.commit()
        except sqlite3.IntegrityError:            # a concurrent enqueue of the same key
            prior = _row(conn, key)
            return {**_public(prior), "duplicate": True}
        ac.audit("send_queued", resolved, key, actor=actor, source=source)
        return _public(_row(conn, key))
    finally:
        conn.close()


def cancel(idempotency_key: str, *, by: str = "", now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    conn = _conn()
    try:
        n = conn.execute("UPDATE send_queue SET status='cancelled', done_ts=?, last_reason=? "
                         "WHERE idempotency_key=? AND status='queued'",
                         (now, f"cancelled_by:{by}"[:200], idempotency_key)).rowcount
        conn.commit()
        row = _row(conn, idempotency_key)
        return {"cancelled": bool(n), **(_public(row) if row else {"status": "unknown"})}
    finally:
        conn.close()


def status(idempotency_key: str) -> dict:
    conn = _conn()
    try:
        row = _row(conn, idempotency_key)
        return _public(row) if row else {"status": "unknown"}
    finally:
        conn.close()


def _finish(conn, rid: int, st: str, reason: str, now: float, receipt: Optional[dict] = None):
    conn.execute("UPDATE send_queue SET status=?, last_reason=?, done_ts=?, receipt=?, "
                 "claimed_ts=NULL WHERE id=?",
                 (st, reason[:200], now, json.dumps(receipt or {})[:4000], rid))
    conn.commit()


def drain(now: Optional[float] = None, send: Optional[Callable] = None) -> list:
    """One pass: deliver what can be delivered now. Safe to run from several processes:
    a row is claimed atomically before anything is sent."""
    from core import agent_control as ac
    now = time.time() if now is None else now
    send = send or ac.agent_send
    conn = _conn()
    out = []
    try:
        conn.execute("UPDATE send_queue SET status='queued', claimed_ts=NULL "
                     "WHERE status='delivering' AND claimed_ts < ?", (now - CLAIM_STALE_SECS,))
        conn.execute("UPDATE send_queue SET status='expired', done_ts=?, "
                     "last_reason='expired_before_delivery' "
                     "WHERE status='queued' AND expires_ts <= ?", (now, now))
        conn.commit()
        rows = conn.execute("SELECT id, idempotency_key, target, pane_id, pane_pid, text, "
                            "actor, source FROM send_queue WHERE status='queued' "
                            "ORDER BY id").fetchall()
        busy_targets = set()
        for rid, key, target, pane_id, pane_pid, text, actor, source in rows:
            if target in busy_targets:
                continue                           # one at a time per agent, in order
            claimed = conn.execute("UPDATE send_queue SET status='delivering', claimed_ts=?, "
                                   "attempts=attempts+1 WHERE id=? AND status='queued'",
                                   (now, rid)).rowcount
            conn.commit()
            if not claimed:
                continue                           # another drainer took it
            try:
                pane = ac._pane_is_live_agent(target)
            except ac.AgentControlError as e:
                _finish(conn, rid, "refused", "target_gone", now, {"error": str(e)[:200]})
                out.append({"key": key, "status": "refused", "reason": "target_gone"})
                continue
            if (pane.get("pane_id") or "") != (pane_id or "") or pane.get("pid") != pane_pid:
                _finish(conn, rid, "refused", "target_identity_changed", now,
                        {"was": [pane_id, pane_pid],
                         "now": [pane.get("pane_id"), pane.get("pid")]})
                out.append({"key": key, "status": "refused",
                            "reason": "target_identity_changed"})
                continue
            if ac._busy_for_send(target):
                conn.execute("UPDATE send_queue SET status='queued', claimed_ts=NULL, "
                             "last_reason='agent_busy' WHERE id=?", (rid,))
                conn.commit()
                busy_targets.add(target)
                continue
            r = send(target, text, idempotency_key=key, actor=actor or None,
                     source=f"{source or ''}|send_queue"[:200])
            if r.get("refused") in ("working", "shell_running", "queued_messages_pending"):
                conn.execute("UPDATE send_queue SET status='queued', claimed_ts=NULL, "
                             "last_reason='agent_busy' WHERE id=?", (rid,))
                conn.commit()
                busy_targets.add(target)
                continue
            if r.get("refused"):
                _finish(conn, rid, "refused", str(r["refused"]), now, r)
                out.append({"key": key, "status": "refused", "reason": r["refused"]})
            elif r.get("delivered") or r.get("submitted") or r.get("duplicate"):
                _finish(conn, rid, "delivered", "delivered", now, r)
                out.append({"key": key, "status": "delivered"})
            else:
                conn.execute("UPDATE send_queue SET status='queued', claimed_ts=NULL, "
                             "last_reason=? WHERE id=?",
                             (str(r.get("reason") or "not_delivered")[:200], rid))
                conn.commit()
            busy_targets.add(target)
        return out
    finally:
        conn.close()


INTERVAL = int(os.getenv("SEND_QUEUE_INTERVAL_SECS", "20"))


async def run_loop() -> None:
    """Drain on a timer. Off unless SEND_QUEUE_ENABLED=1 (read per tick)."""
    import asyncio
    import logging
    log = logging.getLogger("send_queue")
    while True:
        try:
            if os.getenv("SEND_QUEUE_ENABLED", "") == "1":
                done = await asyncio.to_thread(drain)
                if done:
                    log.info(f"send queue: {done}")
        except Exception as e:  # noqa: BLE001 — the loop must survive one bad pass
            log.warning(f"send queue pass failed: {e}")
        await asyncio.sleep(INTERVAL)
