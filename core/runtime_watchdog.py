"""Runtime job watchdog — stall detection for jobs executed by the runtime.

Jobs are executed by worker threads inside the ai-runtime service; there is no
local PID Owner OS can check, and the seo-backend delegator only polls. What IS
authoritative is the runtime job store itself: a live worker heartbeats
`heartbeat_at` every ~5s (job_executor._pulse), and every stage move touches
`updated_at`. This watchdog polls that authoritative state and emits
`runtime_job_stalled` only on hard evidence:

  * a job in an execution stage whose heartbeat AND updated_at are both older
    than RUNTIME_WATCH_STALL_SECS — the worker is gone or wedged;
  * a job sitting in `queued` longer than RUNTIME_WATCH_QUEUED_STALL_SECS with
    no heartbeat — it was never picked up (execute_async is only invoked at
    create/approve time, so a missed hand-off strands the row forever).

`waiting_approval` is NEVER a stall: it is a true owner decision, announced
once by the lifecycle bridge (runtime_events), not re-announced here.

Anti-spam mirrors agent_watch: per-job state persisted in control_plane.db
(restart-safe), one emission per (job, status) stall episode, a deliberate
reminder per RUNTIME_WATCH_REMINDER_SECS, and re-arm the moment the job shows
life (fresh heartbeat or a status change).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable, Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

# Worker heartbeats every ~5s; the store's own stale window is 20s. 120s of
# silence across BOTH clocks is not jitter — it is evidence.
STALL_SECS = int(os.getenv("RUNTIME_WATCH_STALL_SECS", "120"))
QUEUED_STALL_SECS = int(os.getenv("RUNTIME_WATCH_QUEUED_STALL_SECS", "600"))
REMINDER_SECS = int(os.getenv("RUNTIME_WATCH_REMINDER_SECS", "3600"))

# Stages where a worker thread must be alive and pulsing.
_EXECUTION_STAGES = {"planning", "backing_up", "branching", "editing", "validating",
                     "testing", "committing", "pushing", "deploying"}
_TERMINAL = {"completed", "failed", "cancelled", "blocked", "rolled_back",
             "fallback_plan_only"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_watch_state (
    job_id TEXT PRIMARY KEY,
    status TEXT, at TEXT, ts REAL,
    notified_status TEXT, notified_at TEXT, notified_ts REAL,
    emissions INTEGER DEFAULT 0
)
"""


def _conn(conn=None):
    conn, own = _c(conn)
    conn.execute(_SCHEMA)
    return conn, own


def _age_secs(iso: str | None, now: float) -> Optional[float]:
    if not iso:
        return None
    try:
        return now - datetime.fromisoformat(iso).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def stall_evidence(job: dict, now: float) -> Optional[dict]:
    """The stall verdict for one job row, or None. Pure — fully testable."""
    status = job.get("status") or ""
    hb_age = _age_secs(job.get("heartbeat_at"), now)
    up_age = _age_secs(job.get("updated_at"), now)
    if status in _EXECUTION_STAGES:
        # Both clocks must be silent: updated_at moves on every log line, so a
        # slow-but-alive stage (long test run) still shows SOME recent signal
        # via the heartbeat; a dead worker shows neither.
        hb_stale = hb_age is None or hb_age > STALL_SECS
        up_stale = up_age is None or up_age > STALL_SECS
        if hb_stale and up_stale:
            return {"reason": "no_heartbeat_in_execution_stage",
                    "detail": (f"status={status}; heartbeat "
                               f"{'never' if hb_age is None else f'{int(hb_age)}s ago'}, "
                               f"updated {'never' if up_age is None else f'{int(up_age)}s ago'} "
                               f"(threshold {STALL_SECS}s)")}
        return None
    if status == "queued":
        if (up_age or 0) > QUEUED_STALL_SECS and hb_age is None:
            return {"reason": "queued_never_picked_up",
                    "detail": f"queued for {int(up_age)}s with no worker heartbeat "
                              f"(threshold {QUEUED_STALL_SECS}s)"}
        return None
    return None


def scan(*, jobs: Optional[list] = None, emit_fn: Optional[Callable] = None,
         conn=None, now: Optional[float] = None) -> dict:
    """One watchdog pass over non-terminal runtime jobs.

    Injectable for tests: `jobs` (job rows), `emit_fn`. Defaults read the live
    job store and emit through the CTO inbox — the same doorway agent_watch
    uses, so wake routing, dedup and notifications apply unchanged."""
    now = now if now is not None else now_ts()
    if jobs is None:
        from core import job_store
        jobs = [j for j in job_store.list_jobs(limit=200)
                if (j.get("status") or "") not in _TERMINAL]
    if emit_fn is None:
        from core.control_plane.cto import emit as emit_fn  # noqa: F811
    from core import runtime_events

    conn, own = _conn(conn)
    try:
        emitted, skipped = [], []
        for job in jobs:
            jid = job.get("id") or ""
            status = job.get("status") or ""
            if not jid:
                continue
            row = conn.execute(
                "SELECT status, notified_status, notified_ts FROM runtime_watch_state "
                "WHERE job_id=?", (jid,)).fetchone()
            verdict = stall_evidence(job, now)
            conn.execute(
                "INSERT INTO runtime_watch_state (job_id,status,at,ts) VALUES (?,?,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, "
                "at=excluded.at, ts=excluded.ts", (jid, status, now_iso(), now))
            if verdict is None:
                # Life re-arms: a fresh heartbeat or a status change consumes the
                # old stall episode, so a LATER genuine stall announces again.
                if row and row[1]:
                    conn.execute("UPDATE runtime_watch_state SET notified_status='' "
                                 "WHERE job_id=?", (jid,))
                conn.commit()
                skipped.append({"job_id": jid, "why": "no_stall_evidence"})
                continue
            n_status, n_ts = (row[1], row[2]) if row else ("", 0)
            if n_status == status:
                overdue = REMINDER_SECS and (now - float(n_ts or 0)) >= REMINDER_SECS
                if not overdue:
                    conn.commit()
                    skipped.append({"job_id": jid, "why": "already_notified"})
                    continue
            route = runtime_events.route_key_for(job)
            summary = (f"runtime job {jid[:8]} (task OWNER-{job.get('task_id')}) STALLED "
                       f"in '{status}': {verdict['detail']}")
            ev = emit_fn(
                "runtime_watchdog", "runtime_job_stalled",
                project_id=route, agent_id=f"runtimejob:{jid[:8]}",
                severity="high", owner_action_required=True,
                payload={"job_id": jid, "task_id": job.get("task_id"),
                         "project_path": job.get("project_path"), "status": status,
                         "reason": verdict["reason"], "detail": verdict["detail"],
                         "goal": (job.get("goal") or "")[:200]},
                action_taken=summary[:300],
                correlation_id=f"runtimejob:{jid}",
                dedup_key=f"runtimejob:{jid}:stalled:{status}",
                dedup_window_secs=(REMINDER_SECS or 86400), conn=conn)
            conn.execute(
                "UPDATE runtime_watch_state SET notified_status=?, notified_at=?, "
                "notified_ts=?, emissions=emissions+1 WHERE job_id=?",
                (status, now_iso(), now, jid))
            conn.commit()
            emitted.append({"job_id": jid, "status": status, "reason": verdict["reason"],
                            "event_id": (ev or {}).get("event_id")})
        return {"emitted": emitted, "skipped": skipped, "jobs_seen": len(jobs)}
    finally:
        if own:
            conn.close()
