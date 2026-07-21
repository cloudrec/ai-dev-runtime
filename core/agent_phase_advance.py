"""Cross-phase auto-progress.

After a phase completes with verified evidence and no failed acceptance checks,
this dispatches the EXACT owner-approved next-phase task text into the SAME agent
session, once, and verifies the agent returns to working.

Hard rules:
  * Never invent product direction — a next phase is dispatched ONLY when its
    exact `approved_task_text` is recorded in config. No text ⇒ no dispatch.
  * Only existing sessions, never create or duplicate an agent.
  * Never advance a session that is hold / externally_blocked / waiting for real
    credentials / an owner-only decision, or when the budget/resource gate is
    closed.
  * Idempotent: a (session, phase, task-hash) is dispatched at most once.
  * Audited; a failed advancement (dispatched but the agent did not enter
    working) is recorded for owner escalation. A silent successful advance emits
    nothing.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

from core import agent_control as ac

ENABLED = os.getenv("AGENT_PHASE_ADVANCE_ENABLED", "0") not in ("0", "false", "no", "")
_VERIFY_TIMEOUT = int(os.getenv("PHASE_ADVANCE_VERIFY_SECS", "10"))
_MAX_ADVANCES_PER_TICK = int(os.getenv("PHASE_ADVANCE_MAX_PER_TICK", "2"))

# Blocker categories that must never be auto-advanced.
_BLOCK_ADVANCE = {"external", "credential", "credentials", "owner", "owner_only", "denied"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:32]


# ── persistence ─────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db"), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_phase_advance (
        session TEXT, phase_id TEXT, task_hash TEXT, status TEXT, verified_working INTEGER,
        error TEXT, dispatched_at TEXT, updated_ts REAL,
        PRIMARY KEY (session, phase_id))""")
    conn.commit()
    return conn


def get_advance(session: str, phase_id: str) -> Optional[dict]:
    conn = _db()
    try:
        row = conn.execute("SELECT session,phase_id,task_hash,status,verified_working,error,dispatched_at "
                           "FROM agent_phase_advance WHERE session=? AND phase_id=?",
                           (session, phase_id)).fetchone()
        if not row:
            return None
        keys = ("session", "phase_id", "task_hash", "status", "verified_working", "error", "dispatched_at")
        return dict(zip(keys, row))
    finally:
        conn.close()


def _record(session, phase_id, task_hash, status, verified, error=None) -> None:
    conn = _db()
    try:
        conn.execute("INSERT OR REPLACE INTO agent_phase_advance VALUES (?,?,?,?,?,?,?,?)",
                     (session, phase_id, task_hash, status, 1 if verified else 0, error, _now_iso(), time.time()))
        conn.commit()
    finally:
        conn.close()


def rollback(session: str, phase_id: str) -> dict:
    """Mark a phase advance rolled_back (dispatched text cannot be un-sent, but the
    engine will not treat the phase as dispatched and the audit records the undo)."""
    prev = get_advance(session, phase_id)
    if not prev:
        return {"rolled_back": False, "reason": "no advance record"}
    _record(session, phase_id, prev["task_hash"], "rolled_back", False, "owner rollback")
    ac.audit("phase_rollback", f"{session}:{phase_id}", status="rolled_back")
    return {"rolled_back": True, "session": session, "phase_id": phase_id}


# ── acceptance ──────────────────────────────────────────────────────────────
def _acceptance_ok(cfg_phase: dict, report_text: str) -> tuple[bool, str]:
    """Verify the just-completed phase's acceptance from its report. Fail-closed:
    any configured must_not_contain marker present ⇒ not accepted."""
    acc = (cfg_phase or {}).get("acceptance") or {}
    text = report_text or ""
    for bad in acc.get("must_not_contain", ["FAILED", "acceptance failed", "duplicate_created=true"]):
        if bad.lower() in text.lower():
            return False, f"acceptance marker present: {bad!r}"
    need = acc.get("report_contains")
    if need and need.lower() not in text.lower():
        return False, f"required acceptance marker missing: {need!r}"
    return True, "acceptance passed"


def _phases(cfg: dict) -> list[dict]:
    return cfg.get("phases") or []


def _completed_and_next(cfg: dict, record: dict) -> tuple[Optional[dict], Optional[dict]]:
    """Map the record's current phase to the completed phase and the next one."""
    phases = _phases(cfg)
    if not phases:
        return None, None
    cur_id = record.get("phase")
    idx = next((i for i, p in enumerate(phases) if p.get("id") == cur_id), 0)
    completed = phases[idx]
    nxt = phases[idx + 1] if idx + 1 < len(phases) else None
    return completed, nxt


def _read_report(root: str, report_path: str) -> str:
    try:
        rep = ac.agent_report_read(root, report_path)
        return rep.get("content") or ""
    except Exception:  # noqa: BLE001
        return ""


# ── the engine ──────────────────────────────────────────────────────────────
def advance_if_ready(session: str, cfg: dict, record: dict, dispatch: bool = True,
                     budget_locked=None, _sleep=time.sleep) -> dict:
    """Consider one agent for cross-phase advancement. Returns a structured
    decision; only dispatches when every guard passes."""
    base = {"session": session, "action": "none"}
    if not cfg.get("advance_phases"):
        return {**base, "reason": "advance_phases disabled for this project"}
    if cfg.get("mode") != "auto":
        return {**base, "reason": f"session mode is {cfg.get('mode')} (hold/monitor) — never advance"}
    if record.get("state") != "completed":
        return {**base, "reason": f"state is {record.get('state')}, not completed"}
    if record.get("blocker_category") in _BLOCK_ADVANCE:
        return {**base, "reason": f"blocked: {record.get('blocker_category')}"}
    if budget_locked is None:
        from core.agent_orchestrator import budget_locked as _bl
        budget_locked = _bl()
    if budget_locked:
        return {**base, "reason": "budget/resource gate closed"}

    completed_phase, nxt = _completed_and_next(cfg, record)
    if not nxt:
        return {**base, "reason": "no next phase recorded"}
    approved = (nxt.get("approved_task_text") or "").strip()
    if not approved:
        # NEVER invent direction.
        return {**base, "action": "no_recorded_next_phase", "next_phase": nxt.get("id"),
                "reason": "next phase has no exact approved_task_text — not dispatching"}

    # Acceptance of the just-completed phase.
    root = cfg.get("root")
    report_text = _read_report(root, record.get("report_path")) if (root and record.get("report_path")) else ""
    ok, why = _acceptance_ok(completed_phase, report_text)
    if not ok:
        return {**base, "action": "acceptance_failed", "reason": why, "phase": completed_phase.get("id")}

    task_hash = _hash(approved)
    prior = get_advance(session, nxt["id"])
    if prior and prior.get("task_hash") == task_hash and prior.get("status") in ("dispatched", "verified"):
        return {**base, "action": "already_dispatched", "idempotent": True, "next_phase": nxt["id"]}

    if not dispatch:
        return {**base, "action": "would_dispatch", "next_phase": nxt["id"], "task_hash": task_hash}

    # Dispatch ONCE into the SAME session.
    target = record.get("agent_key") or f"{session}:0.0"
    try:
        res = ac.agent_send(target, approved, idempotency_key=f"phase-{session}-{nxt['id']}-{task_hash}")
    except ac.AgentControlError as e:
        _record(session, nxt["id"], task_hash, "dispatch_error", False, str(e)[:200])
        return {**base, "action": "dispatch_error", "error": str(e)[:200], "escalate": True}
    _record(session, nxt["id"], task_hash, "dispatched", False)
    ac.audit("phase_advance_dispatch", target, phase=nxt["id"], task_hash=task_hash,
             delivered=res.get("delivered"))

    # Verify the agent entered working.
    working, deadline = False, time.time() + _VERIFY_TIMEOUT
    while time.time() < deadline:
        _sleep(1)
        if ac.agent_status(target).get("state") == "working":
            working = True
            break
    _record(session, nxt["id"], task_hash, "verified" if working else "dispatched_no_working", working)
    ac.audit("phase_advance_verify", target, phase=nxt["id"], verified=working)
    return {**base, "action": "advanced", "next_phase": nxt["id"], "verified_working": working,
            "delivered": res.get("delivered"), "escalate": (not working)}


def sweep(records: list[dict], cfg_of, dispatch: bool = True) -> dict:
    """Advance eligible completed agents (bounded per tick)."""
    if not ENABLED:
        return {"enabled": False, "advanced": []}
    from core.agent_orchestrator import budget_locked
    bl = budget_locked()
    out, advanced = [], 0
    for rec in records:
        if advanced >= _MAX_ADVANCES_PER_TICK:
            break
        session = rec.get("session")
        cfg = cfg_of(session)
        r = advance_if_ready(session, cfg, rec, dispatch=dispatch, budget_locked=bl)
        if r.get("action") not in ("none",):
            out.append(r)
        if r.get("action") == "advanced":
            advanced += 1
    return {"enabled": True, "budget_locked": bl, "results": out}
