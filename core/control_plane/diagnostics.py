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


def observability_summary(*, now: Optional[float] = None) -> dict:
    """Combined read-only view. `all_clear` is true when there are no ACTIVE failures,
    regardless of historical totals — so a green system is not flagged by stale counters."""
    now = now if now is not None else time.time()
    notif = notification_failure_report(now=now)
    jobs = runtime_job_failure_report(now=now)
    active = notif["active"] + jobs["active"]
    return {
        "notifications": notif,
        "runtime_jobs": jobs,
        "active_failures_total": active,
        "historical_failures_total": notif["historical"] + jobs["historical"],
        "all_clear": active == 0,
        "status": "green" if active == 0 else "red",
        "checked_at": (datetime.fromtimestamp(now, timezone.utc).isoformat()),
    }
