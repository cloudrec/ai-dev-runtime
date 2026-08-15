"""Bounded runtime job recovery — retry a failed lineage, never a decision.

Scope is deliberately narrow. The supervisor may retry exactly the failure
classes where the retry is KNOWN-SAFE and the cause is environmental, and it
must do so through the runtime's own HTTP API so execution stays inside the
ai-runtime service process (this module runs in the wake companion, which must
never execute jobs itself):

  * dirty_checkout — `branch failed: ... would be overwritten by checkout`.
    Deterministic environment failure. Retryable ONLY when the isolated
    worktree execution model is active, because retrying into the same shared
    tree would fail identically and burn the retry budget for nothing.
  * worker_crash / orphaned — the worker thread died (service restart, crash);
    the job's own work was rolled back, so re-running preserves idempotency.

Never retried: test failures, policy blocks, planner failures, secret aborts —
those are results, not transport. Owner gates are never auto-approved: the
retry carries the ORIGINAL job's approval_required verbatim, so work the owner
never approved comes back as waiting_approval and wakes the owner instead of
running.

Idempotency / lineage: one retry per failed job (PK on failed_job_id), a
bounded number per task lineage, and no retry at all while ANY other job for
the same task is non-terminal or was created after the failure — that newer job
IS the recovery, whoever created it (the seo-backend delegator has its own
retry machinery; two supervisors must not double-submit one task).
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

ENABLED = os.getenv("RUNTIME_SUPERVISOR_ENABLED", "1") not in ("0", "", "false", "no")
MAX_RETRIES_PER_TASK = int(os.getenv("RUNTIME_SUPERVISOR_MAX_RETRIES", "1"))
# Only failures this recent are recovery candidates; older ones are history.
RECENT_SECS = int(os.getenv("RUNTIME_SUPERVISOR_RECENT_SECS", "21600"))
API_URL = os.getenv("RUNTIME_API_URL", "http://172.17.0.1:8199/api/v1")

_TERMINAL = {"completed", "failed", "cancelled", "blocked", "rolled_back",
             "fallback_plan_only"}

_CLASSES = (
    ("dirty_checkout", re.compile(r"would be overwritten by checkout", re.I)),
    ("worker_crash", re.compile(r"worker crashed during", re.I)),
    ("orphaned", re.compile(r"orphaned: no heartbeat", re.I)),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_recovery (
    failed_job_id TEXT PRIMARY KEY,
    retry_job_id TEXT, task_id INTEGER, class TEXT, at TEXT, ts REAL, reason TEXT
)
"""


def _conn(conn=None):
    conn, own = _c(conn)
    conn.execute(_SCHEMA)
    return conn, own


def classify_failure(error: str) -> str:
    for name, rx in _CLASSES:
        if rx.search(error or ""):
            return name
    return ""


def _ts_of(iso: str | None) -> float:
    try:
        return datetime.fromisoformat(iso).astimezone(timezone.utc).timestamp() if iso else 0.0
    except ValueError:
        return 0.0


def _default_create(job: dict, marker: str) -> dict:
    """Create the retry through the runtime API (bearer auth from the shared
    env file), so the ai-runtime service — not this process — executes it."""
    token = os.getenv("RUNTIME_TOKEN", "").strip()
    if not token:
        return {"ok": False, "reason": "runtime_token_unset"}
    body = {
        "project_path": job.get("project_path"),
        "goal": job.get("goal"),
        "instructions": (job.get("instructions") or job.get("goal") or "") + marker,
        "project_id": job.get("project_id"), "task_id": job.get("task_id"),
        "autonomy_level": job.get("autonomy_level"),
        "allowed_paths": job.get("allowed_paths") or None,
        "forbidden_paths": job.get("forbidden_paths") or None,
        "base_branch": job.get("base_branch") or "master",
        "auto_commit": bool(job.get("auto_commit", True)),
        "auto_push": bool(job.get("auto_push", False)),
        # Verbatim from the original: an unapproved job comes back as
        # waiting_approval and wakes the owner; it is never silently promoted.
        "approval_required": bool(job.get("approval_required")),
    }
    req = urllib.request.Request(
        f"{API_URL}/jobs", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read().decode())
        return {"ok": True, "job": out}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"api_unreachable:{type(e).__name__}:{str(e)[:120]}"}


def consider_recovery(job: dict, *, all_jobs: Optional[list] = None,
                      create_fn: Optional[Callable] = None,
                      isolation_active: Optional[bool] = None,
                      emit_fn: Optional[Callable] = None,
                      conn=None, now: Optional[float] = None) -> dict:
    """Decide-and-act for one failed job. Every refusal returns its reason."""
    now = now if now is not None else now_ts()
    if not ENABLED:
        return {"retried": False, "reason": "supervisor_disabled"}
    if (job.get("status") or "") != "failed":
        return {"retried": False, "reason": "not_failed"}
    cls = classify_failure(job.get("error") or "")
    if not cls:
        return {"retried": False, "reason": "failure_class_not_retryable"}
    fin = _ts_of(job.get("finished_at") or job.get("updated_at"))
    if now - fin > RECENT_SECS:
        return {"retried": False, "reason": "failure_too_old"}
    if isolation_active is None:
        from core import job_executor
        isolation_active = job_executor._isolated_workspaces()
    if cls == "dirty_checkout" and not isolation_active:
        return {"retried": False, "reason": "isolation_not_active_retry_would_repeat_failure"}

    task_id = job.get("task_id")
    if all_jobs is None:
        from core import job_store
        all_jobs = job_store.list_jobs(limit=200)
    peers = [j for j in all_jobs
             if j.get("task_id") == task_id and j.get("id") != job.get("id")]
    if any((p.get("status") or "") not in _TERMINAL for p in peers):
        return {"retried": False, "reason": "task_has_active_job"}
    if any(_ts_of(p.get("created_at")) > fin for p in peers):
        return {"retried": False, "reason": "newer_job_supersedes_failure"}

    conn, own = _conn(conn)
    try:
        if conn.execute("SELECT 1 FROM runtime_recovery WHERE failed_job_id=?",
                        (job["id"],)).fetchone():
            return {"retried": False, "reason": "already_retried_this_failure"}
        used = conn.execute("SELECT COUNT(*) FROM runtime_recovery WHERE task_id=?",
                            (task_id,)).fetchone()[0]
        if used >= MAX_RETRIES_PER_TASK:
            return {"retried": False, "reason": "task_retry_budget_exhausted"}

        marker = (f"\n\n[supervisor-retry of runtime job {job['id']}; "
                  f"failure class: {cls}]")
        res = (create_fn or _default_create)(job, marker)
        if not res.get("ok"):
            return {"retried": False, "reason": res.get("reason", "create_failed")}
        retry_id = (res.get("job") or {}).get("id", "")
        # The claim row goes in ONLY after the create succeeded, keyed by the
        # failed job — a second scan can never double-submit.
        conn.execute("INSERT OR IGNORE INTO runtime_recovery "
                     "(failed_job_id, retry_job_id, task_id, class, at, ts, reason) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (job["id"], retry_id, task_id, cls, now_iso(), now,
                      (job.get("error") or "")[:300]))
        conn.commit()
        if emit_fn is None:
            from core.control_plane.cto import emit as emit_fn  # noqa: F811
        emit_fn("runtime_supervisor", "runtime_job_retried",
                project_id="", agent_id=f"runtimejob:{job['id'][:8]}",
                severity="info", owner_action_required=False,
                payload={"failed_job_id": job["id"], "retry_job_id": retry_id,
                         "task_id": task_id, "class": cls},
                action_taken=(f"supervisor retried runtime job {job['id'][:8]} "
                              f"(task OWNER-{task_id}, {cls}) as {retry_id[:8]}"),
                correlation_id=f"runtimejob:{job['id']}",
                dedup_key=f"runtimejob:{job['id']}:retried",
                dedup_window_secs=86400, conn=conn)
        return {"retried": True, "retry_job_id": retry_id, "class": cls}
    finally:
        if own:
            conn.close()


def scan(*, jobs: Optional[list] = None, create_fn: Optional[Callable] = None,
         isolation_active: Optional[bool] = None, emit_fn: Optional[Callable] = None,
         conn=None, now: Optional[float] = None) -> dict:
    """One recovery pass over recent failed jobs."""
    if not ENABLED:
        return {"retried": [], "skipped": [], "reason": "supervisor_disabled"}
    if jobs is None:
        from core import job_store
        jobs = job_store.list_jobs(limit=200)
    retried, skipped = [], []
    for job in jobs:
        if (job.get("status") or "") != "failed":
            continue
        r = consider_recovery(job, all_jobs=jobs, create_fn=create_fn,
                              isolation_active=isolation_active, emit_fn=emit_fn,
                              conn=conn, now=now)
        (retried if r.get("retried") else skipped).append(
            {"job_id": job.get("id"), **r})
    return {"retried": retried, "skipped": skipped}
