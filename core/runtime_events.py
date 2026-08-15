"""Runtime job lifecycle -> Owner OS event pipeline bridge.

Before this module, a runtime job died silently in runtime_jobs.db: job 888f5266
(task OWNER-193, Venture Radar) failed on a dirty-tree checkout at 07:05Z and no
event, no notification and no wake ever reached the project chat — the owner
found out by asking. Every lifecycle transition now lands in the same durable
CTO event log every tmux agent already uses, so wake routing, dedup, coalescing
and the notification outbox all apply unchanged.

Mapping discipline:
  * failed / blocked            -> high severity, owner_action_required (wakes)
  * waiting_approval            -> a true owner decision (wakes)
  * completed / fallback-only   -> completion record (significant event type)
  * every other stage move      -> routine `runtime_job_state` (durable, no wake)

Dedup key is `runtimejob:<job_id>:<status>` so a replayed transition cannot
double-announce, and correlation id `runtimejob:<job_id>` groups a job's whole
history without ever colliding with the agent-watch actionable namespace.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

# Kill switch for the whole bridge. ON by default: the silent path is the bug.
ENABLED = os.getenv("RUNTIME_EVENTS_ENABLED", "1") not in ("0", "", "false", "no")

_DEDUP_WINDOW_SECS = int(os.getenv("RUNTIME_EVENTS_DEDUP_WINDOW_SECS", "86400"))

# status -> (event type, severity, owner_action_required)
# Types are chosen from wake_bridge.WAKE_EVENT_TYPES / ROUTINE_EVENT_TYPES so the
# bridge needs no new eligibility rules for the terminal states.
EVENT_FOR_STATUS = {
    "failed": ("task_failed", "high", True),
    "blocked": ("action_blocked", "high", True),
    "waiting_approval": ("owner_decision_required", "high", True),
    "completed": ("task_completed", "info", False),
    "fallback_plan_only": ("work_stopped_incomplete", "info", False),
    "rolled_back": ("task_failed", "high", True),
    "cancelled": ("runtime_job_state", "info", False),
    "superseded": ("runtime_job_state", "info", False),
}
_DEFAULT_EVENT = ("runtime_job_state", "info", False)

# The control repo hosts Owner OS itself; jobs against it route to the owner-os
# chat explicitly instead of relying on the unmapped fallback label.
_CONTROL_REPO = "/root/ai-dev-runtime"


def route_key_for(job: dict) -> str:
    """Project route key for a runtime job: the normalized repo basename, with
    the control repo pinned to `owner-os`. Unmapped keys still deliver — the
    wake router folds them into the owner-os fallback, labelled, never dropped."""
    from core import wake_routes
    path = (job.get("project_path") or "").rstrip("/")
    if not path:
        return ""
    if path == _CONTROL_REPO:
        return "owner-os"
    return wake_routes.normalize_key(path.rsplit("/", 1)[-1])


def _summary(job: dict, status: str) -> str:
    jid = (job.get("id") or "")[:8]
    task = job.get("task_id")
    head = f"runtime job {jid} (task OWNER-{task}) -> {status}"
    err = (job.get("error") or "").strip()
    if status in ("failed", "blocked", "rolled_back") and err:
        return f"{head}: {err}"[:400]
    goal = (job.get("goal") or "").strip()
    return f"{head}: {goal}"[:400] if goal else head


def emit_transition(job: dict, status: str, *, prev_status: str = "",
                    emit_fn: Optional[Callable] = None, conn=None) -> Optional[dict]:
    """Record one lifecycle transition as a durable CTO event.

    Best-effort by contract: the job store must never fail a job write because
    the control plane is unavailable — callers wrap this in try/except and this
    function itself refuses only when disabled or the transition is a no-op."""
    if not ENABLED or not job or not status or status == prev_status:
        return None
    if emit_fn is None:
        from core.control_plane.cto import emit as emit_fn  # noqa: F811
    etype, severity, oar = EVENT_FOR_STATUS.get(status, _DEFAULT_EVENT)
    jid = job.get("id") or ""
    route = route_key_for(job)
    return emit_fn(
        "runtime_jobs", etype,
        project_id=route,
        agent_id=f"runtimejob:{jid[:8]}",
        severity=severity, owner_action_required=oar,
        payload={
            "job_id": jid, "task_id": job.get("task_id"),
            "project_path": job.get("project_path"), "status": status,
            "prev_status": prev_status, "kind": job.get("kind"),
            "outcome": job.get("outcome"), "goal": (job.get("goal") or "")[:200],
            "error": (job.get("error") or "")[:400],
        },
        action_taken=_summary(job, status),
        correlation_id=f"runtimejob:{jid}",
        dedup_key=f"runtimejob:{jid}:{status}",
        dedup_window_secs=_DEDUP_WINDOW_SECS,
        conn=conn)


def _pytest_without_sandbox() -> bool:
    """True when we are inside a pytest run that has NOT redirected the control
    plane DB. Emitting there would write test-fixture jobs into the LIVE event
    log — which actually happened on 2026-08-15: a runtime job's repo-suite run
    inside a worktree used an old conftest (no CONTROL_PLANE_DB pin) while its
    hardcoded sys.path imported the live hooked modules, and 126 debris events
    for project 'repo' landed in production and queued wakes."""
    return bool(os.getenv("PYTEST_CURRENT_TEST")) and not os.getenv("CONTROL_PLANE_DB")


def safe_emit_transition(job: dict, status: str, *, prev_status: str = "") -> None:
    """The swallow-everything wrapper the job store calls inline."""
    try:
        if _pytest_without_sandbox():
            return
        emit_transition(job, status, prev_status=prev_status)
    except Exception:  # noqa: BLE001 — event emission must never break a job write
        pass
