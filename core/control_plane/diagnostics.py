"""Read-only observability diagnostics — distinguish HISTORICAL from ACTIVE failures.

A non-zero total-failure counter (failed runtime jobs, dead-lettered notifications) does
NOT mean the system is currently failing — most are stale one-time events. Health must
report GREEN when there are no ACTIVE (recent) failures, while still surfacing the historical
totals. These helpers are strictly read-only (SELECT only) and never mutate any store.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional


def _epoch(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    for v in (s, s.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(v).timestamp()
        except Exception:  # noqa: BLE001
            continue
    return None


def _split(timestamps, now: float, window: float) -> dict:
    """Split a list of ISO timestamps into active (within `window` of now) vs historical."""
    epochs = [e for e in (_epoch(t) for t in timestamps) if e is not None]
    active = sum(1 for e in epochs if (now - e) < window)
    total = len(timestamps)
    newest = max(epochs) if epochs else None
    return {
        "total": total,
        "active": active,
        "historical": total - active,
        "newest_at": (datetime.fromtimestamp(newest, timezone.utc).isoformat()) if newest else None,
        "newest_age_secs": (round(now - newest) if newest else None),
        "window_secs": window,
        "status": "green" if active == 0 else "red",
        "classification": "historical" if (total and active == 0) else
                          ("active" if active else "clean"),
    }


def notification_failure_report(*, now: Optional[float] = None,
                                active_window_secs: float = 3600, conn=None) -> dict:
    """Dead-lettered notifications split historical vs active (read-only)."""
    now = now if now is not None else time.time()
    own = conn is None
    if conn is None:
        from core.control_plane.store import connect, init_db
        conn = connect()
        init_db(conn)
    try:
        rows = conn.execute("SELECT created_at FROM notification WHERE state='dead_letter'").fetchall()
    finally:
        if own:
            conn.close()
    rep = _split([r[0] for r in rows], now, active_window_secs)
    rep["metric"] = "notification_dead_letter"
    rep["note"] = ("no active delivery failures — dead-letters are historical "
                   "(proactive owner-push disabled = RED, gate G4)" if rep["active"] == 0
                   else "ACTIVE notification failures in window")
    return rep


def _jobs_db() -> str:
    return os.getenv("RUNTIME_JOBS_DB", "/root/ai-dev-runtime/runtime_jobs.db")


def runtime_job_failure_report(*, now: Optional[float] = None,
                               active_window_secs: float = 86400, jobs_db: str = None) -> dict:
    """Failed runtime jobs split historical vs active (read-only)."""
    now = now if now is not None else time.time()
    path = jobs_db or _jobs_db()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute(
            "SELECT coalesce(finished_at, updated_at, created_at) FROM jobs "
            "WHERE status='failed'").fetchall()
    finally:
        conn.close()
    rep = _split([r[0] for r in rows], now, active_window_secs)
    rep["metric"] = "runtime_job_failed"
    rep["note"] = ("no active job failures — all failed jobs are historical/stale"
                   if rep["active"] == 0 else "ACTIVE job failures in window")
    return rep


def registry_health_report(*, now: Optional[float] = None, fresh_within_secs: float = 120,
                           conn=None) -> dict:
    """Agent registry freshness by OBSERVATION recency (`agent.updated_at`, refreshed every
    discovery tick) — NOT by `evidence_fresh_at` (which only the actuator refreshes, so it
    reads stale for every observe-only agent and is a poor liveness signal). If no agent has
    been observed recently the discovery ENGINE is likely stalled → red. Read-only."""
    now = now if now is not None else time.time()
    from core.control_plane import api as _cp
    regs = _cp.get_registry(conn=conn)
    ages = [(now - _epoch(r.get("updated_at"))) for r in regs if _epoch(r.get("updated_at")) is not None]
    fresh = sum(1 for a in ages if a < fresh_within_secs)
    by_life: dict = {}
    for r in regs:
        by_life[r.get("lifecycle_state") or "unknown"] = by_life.get(r.get("lifecycle_state") or "unknown", 0) + 1
    duplicates = sum(1 for r in regs if r.get("duplicate_of"))
    dead = sum(1 for r in regs if r.get("lifecycle_state") == "dead")
    newest_age = min(ages) if ages else None
    engine_alive = newest_age is not None and newest_age < (fresh_within_secs * 2)
    return {
        "metric": "registry_health",
        "agents_total": len(regs),
        "observed_fresh": fresh,
        "observed_stale": len(regs) - fresh,
        "by_lifecycle": by_life,
        "duplicates_flagged": duplicates,
        "dead": dead,
        "newest_observation_age_secs": (round(newest_age) if newest_age is not None else None),
        "fresh_within_secs": fresh_within_secs,
        "engine_alive": engine_alive,
        "status": "green" if engine_alive else "red",
        "note": ("discovery engine observed an agent recently"
                 if engine_alive else "no recent observation — discovery engine may be stalled"),
    }


def owner_gate_report(*, now: Optional[float] = None, conn=None) -> dict:
    """Open owner gates (pending decisions) by kind + age. Read-only. Not a failure — a
    backlog signal; `oldest_age_secs` surfaces a decision left unanswered too long."""
    now = now if now is not None else time.time()
    from core.control_plane import api as _cp
    gates = _cp.get_open_gates(conn=conn)
    by_kind: dict = {}
    oldest_age = None
    oldest_gate = None
    for g in gates:
        by_kind[g.get("kind") or "unknown"] = by_kind.get(g.get("kind") or "unknown", 0) + 1
        e = _epoch(g.get("opened_at"))
        if e is not None:
            age = now - e
            if oldest_age is None or age > oldest_age:
                oldest_age, oldest_gate = age, g.get("id")
    return {
        "metric": "open_owner_gates",
        "open_total": len(gates),
        "by_kind": by_kind,
        "oldest_age_secs": (round(oldest_age) if oldest_age is not None else None),
        "oldest_gate_id": oldest_gate,
        "status": "green",   # pending decisions are backlog, never a failure
        "note": "pending owner decisions (backlog); requires owner action, not a system failure",
    }


def lease_report(*, now: Optional[float] = None, conn=None) -> dict:
    """Resource leases: live vs expired. Read-only. Expired-but-present rows are harmless
    (re-acquired on next use) but surfaced so they are not mistaken for active ownership."""
    now = now if now is not None else time.time()
    own = conn is None
    if conn is None:
        from core.control_plane.store import connect, init_db
        conn = connect()
        init_db(conn)
    try:
        rows = conn.execute("SELECT resource,holder_controller,expires_ts FROM resource_lease").fetchall()
    finally:
        if own:
            conn.close()
    live = [r for r in rows if r[2] is not None and r[2] > now]
    expired = [r for r in rows if not (r[2] is not None and r[2] > now)]
    return {
        "metric": "resource_leases",
        "total": len(rows),
        "live": len(live),
        "expired_stale": len(expired),
        "live_holders": [{"resource": r[0], "holder": r[1]} for r in live],
        "status": "green",
        "note": "expired leases are harmless (re-acquired on next actuation)",
    }


def observability_summary(*, now: Optional[float] = None) -> dict:
    """Combined read-only view. `all_clear` is true when there are no ACTIVE failures,
    regardless of historical totals — so a green system is not flagged by stale counters."""
    now = now if now is not None else time.time()
    notif = notification_failure_report(now=now)
    jobs = runtime_job_failure_report(now=now)
    registry = registry_health_report(now=now)
    gates = owner_gate_report(now=now)
    leases = lease_report(now=now)
    active = notif["active"] + jobs["active"]
    # overall red if there are ACTIVE failures OR the discovery engine looks stalled.
    healthy = (active == 0) and registry["engine_alive"]
    return {
        "notifications": notif,
        "runtime_jobs": jobs,
        "registry_health": registry,
        "open_owner_gates": gates,
        "resource_leases": leases,
        "active_failures_total": active,
        "historical_failures_total": notif["historical"] + jobs["historical"],
        "engine_alive": registry["engine_alive"],
        "open_gate_backlog": gates["open_total"],
        "all_clear": healthy,
        "status": "green" if healthy else "red",
        "checked_at": (datetime.fromtimestamp(now, timezone.utc).isoformat()),
    }
