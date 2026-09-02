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
    # `failed` is a TERMINAL status — a failed job never un-fails, so the total only grows
    # monotonically. Any external snapshot (e.g. a stale "15") is therefore an earlier point
    # in the same series and reconciles to the current authoritative total; only the ACTIVE
    # (recent) count reflects current health.
    rep["monotonic_terminal"] = True
    rep["reconcile_note"] = ("total is a monotonic terminal series; a smaller external "
                             f"snapshot is stale — current authoritative total={rep['total']}, "
                             f"active={rep['active']}")
    rep["note"] = ("no active job failures — all failed jobs are historical/stale"
                   if rep["active"] == 0 else "ACTIVE job failures in window")
    return rep


def runtime_blockers_report(*, now: Optional[float] = None,
                            recent_window_secs: float = 86400,
                            jobs_db: str = None, conn=None) -> dict:
    """Runtime job blockers, mission-control shaped: every non-terminal job with
    its liveness evidence, stall verdicts from the runtime watchdog's persisted
    state, and recent terminal failures with their causes — so runtime blockers
    sit beside tmux agent blockers in one view. Read-only."""
    now = now if now is not None else time.time()
    path = jobs_db or _jobs_db()
    terminal = ("completed", "failed", "cancelled", "blocked", "rolled_back",
                "fallback_plan_only")
    jc = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    jc.row_factory = sqlite3.Row
    try:
        active_rows = jc.execute(
            "SELECT id, task_id, project_path, status, goal, heartbeat_at, updated_at "
            "FROM jobs WHERE status NOT IN (%s) ORDER BY created_at DESC LIMIT 50"
            % ",".join("?" * len(terminal)), terminal).fetchall()
        failed_rows = jc.execute(
            "SELECT id, task_id, project_path, status, error, finished_at "
            "FROM jobs WHERE status IN ('failed','blocked') "
            "ORDER BY coalesce(finished_at, updated_at) DESC LIMIT 20").fetchall()
    finally:
        jc.close()

    from core import runtime_watchdog
    def _age(iso):
        try:
            return round(now - datetime.fromisoformat(iso).timestamp()) if iso else None
        except ValueError:
            return None
    active = []
    for r in active_rows:
        j = dict(r)
        verdict = runtime_watchdog.stall_evidence(j, now)
        active.append({
            "job_id": j["id"], "task_id": j["task_id"], "status": j["status"],
            "project_path": j["project_path"], "goal": (j["goal"] or "")[:120],
            "heartbeat_age_secs": _age(j["heartbeat_at"]),
            "updated_age_secs": _age(j["updated_at"]),
            "stalled": bool(verdict), "stall_detail": (verdict or {}).get("detail", ""),
        })
    recent_failed = []
    for r in failed_rows:
        age = _age(r["finished_at"])
        if age is not None and age > recent_window_secs:
            continue
        recent_failed.append({
            "job_id": r["id"], "task_id": r["task_id"], "status": r["status"],
            "project_path": r["project_path"], "error": (r["error"] or "")[:300],
            "finished_age_secs": age,
        })
    stalled = [a for a in active if a["stalled"]]
    return {
        "metric": "runtime_blockers",
        "active_jobs": active, "stalled_jobs": stalled,
        "stalled_count": len(stalled),
        "waiting_approval": [a for a in active if a["status"] == "waiting_approval"],
        "recent_failed": recent_failed,
        "note": ("no runtime blockers" if not stalled and not recent_failed
                 else "runtime jobs need attention"),
    }


def notification_history_report(*, now: Optional[float] = None,
                                active_window_secs: float = 3600, conn=None) -> dict:
    """Distinguish current failure STATE from cumulative failure HISTORY. A raw history
    counter (retry attempts, logged dead-letter/red events) is cumulative and grows; it does
    NOT mean the system is currently failing. Read-only."""
    now = now if now is not None else time.time()
    own = conn is None
    if conn is None:
        from core.control_plane.store import connect, init_db
        conn = connect()
        init_db(conn)
    try:
        by_state = {r[0]: r[1] for r in
                    conn.execute("SELECT state,count(*) FROM notification GROUP BY state").fetchall()}
        cumulative_attempts = conn.execute("SELECT coalesce(sum(attempts),0) FROM notification").fetchone()[0]
        dl_events = conn.execute(
            "SELECT count(*) FROM event WHERE type='notification_dead_letter'").fetchone()[0]
        red_events = conn.execute(
            "SELECT count(*) FROM event WHERE type='notifications_red'").fetchone()[0]
        dl_created = [r[0] for r in conn.execute(
            "SELECT created_at FROM notification WHERE state='dead_letter'").fetchall()]
    finally:
        if own:
            conn.close()
    split = _split(dl_created, now, active_window_secs)
    return {
        "metric": "notification_history",
        "current_state": by_state,
        "current_dead_letter": by_state.get("dead_letter", 0),
        "active_dead_letter": split["active"],
        "historical_dead_letter": split["historical"],
        "newest_dead_letter_age_secs": split["newest_age_secs"],
        # cumulative HISTORY (monotonic) — informational, not a current-failure signal
        "cumulative_failure_attempts": cumulative_attempts,
        "dead_letter_events_logged": dl_events,
        "notifications_red_events": red_events,
        "status": "green" if split["active"] == 0 else "red",
        "note": ("current dead-letters are historical (owner-push RED, gate G4); the "
                 "cumulative attempt/event counters are monotonic history, not active failures"
                 if split["active"] == 0 else "ACTIVE notification failures in window"),
    }


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


def owner_gate_report(*, now: Optional[float] = None, sla_secs: float = 86400.0,
                      breach_limit: int = 20, conn=None) -> dict:
    """Open owner gates (pending decisions) by kind + age, with an SLA escalation dimension.
    Read-only. A pending gate is backlog, NOT a system failure — so `status` stays green even
    when overdue (the honest 'system healthy, owner action overdue' distinction). A gate open
    longer than `sla_secs` (default 24h) is an SLA BREACH → surfaced in `sla_breaches` with
    `escalate=True` so the owner/CTO sees a decision that has waited too long, without the
    system mislabelling itself as broken."""
    now = now if now is not None else time.time()
    from core.control_plane import api as _cp
    gates = _cp.get_open_gates(conn=conn)
    by_kind: dict = {}
    oldest_age = None
    oldest_gate = None
    breached = []
    for g in gates:
        by_kind[g.get("kind") or "unknown"] = by_kind.get(g.get("kind") or "unknown", 0) + 1
        e = _epoch(g.get("opened_at"))
        if e is not None:
            age = now - e
            if oldest_age is None or age > oldest_age:
                oldest_age, oldest_gate = age, g.get("id")
            if age > sla_secs:
                breached.append({"id": g.get("id"), "kind": g.get("kind") or "unknown",
                                 "age_secs": round(age)})
    breached.sort(key=lambda b: b["age_secs"], reverse=True)
    return {
        "metric": "open_owner_gates",
        "open_total": len(gates),
        "by_kind": by_kind,
        "oldest_age_secs": (round(oldest_age) if oldest_age is not None else None),
        "oldest_gate_id": oldest_gate,
        "sla_secs": sla_secs,
        "breached_count": len(breached),
        "sla_breaches": breached[:breach_limit],
        "escalate": bool(breached),
        "status": "green",   # pending decisions are backlog, never a failure
        "note": (f"{len(breached)} owner decision(s) past SLA ({round(sla_secs)}s) — escalate "
                 "to owner (still not a system failure)" if breached
                 else "pending owner decisions (backlog); requires owner action, not a system failure"),
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


def cto_cursor_report(*, now: Optional[float] = None, stale_after_secs: float = 3600,
                      conn=None) -> dict:
    """CTO consumer cursor lag. Per the CTO contract a STALE cursor (unread events exist but
    the cursor has not advanced within the window) is a health error — the consumer stopped
    reading. No registered consumer is informational, not an error. Read-only."""
    now = now if now is not None else time.time()
    own = conn is None
    if conn is None:
        from core.control_plane.store import connect, init_db
        conn = connect()
        init_db(conn)
    try:
        latest = conn.execute("SELECT coalesce(max(id),0) FROM event").fetchone()[0]
        rows = conn.execute("SELECT consumer,last_event_id,updated_at FROM cto_cursor").fetchall()
    finally:
        if own:
            conn.close()
    consumers = []
    stale = 0
    for consumer, last_id, updated_at in rows:
        lag = latest - (last_id or 0)
        age = (now - _epoch(updated_at)) if _epoch(updated_at) is not None else None
        is_stale = lag > 0 and age is not None and age > stale_after_secs
        stale += 1 if is_stale else 0
        consumers.append({"consumer": consumer, "last_event_id": last_id, "lag": lag,
                          "cursor_age_secs": (round(age) if age is not None else None),
                          "stale": is_stale})
    return {
        "metric": "cto_cursor",
        "latest_event_id": latest,
        "consumers": consumers,
        "consumer_count": len(rows),
        "stale_consumers": stale,
        "status": "red" if stale else "green",
        "note": ("no CTO consumer has registered a durable cursor (informational)" if not rows
                 else ("a CTO consumer cursor is stale — consumer stopped reading"
                       if stale else "all CTO consumers current")),
    }


def _ac_db() -> str:
    return os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db")


def commander_delivery_report(*, now: Optional[float] = None, stall_after_secs: float = 1800,
                              ac_db: str = None) -> dict:
    """Same-chat delivery health: unacked commander_events (drained + acked by agent_notifier).
    A growing unacked backlog with no recent ack = the drain stalled → the owner is silently
    NOT getting same-chat messages = health error. Read-only (mode=ro)."""
    now = now if now is not None else time.time()
    path = ac_db or _ac_db()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        return {"metric": "commander_same_chat_delivery", "total": 0, "unacked": 0,
                "oldest_unacked_age_secs": None, "newest_ack_age_secs": None,
                "drain_alive": True, "status": "green", "note": "commander store unavailable"}
    try:
        total = conn.execute("SELECT count(*) FROM commander_events").fetchone()[0]
        unacked = conn.execute("SELECT count(*) FROM commander_events WHERE acknowledged=0").fetchone()[0]
        oldest_unacked = conn.execute(
            "SELECT min(ts) FROM commander_events WHERE acknowledged=0").fetchone()[0]
        newest_acked = conn.execute(
            "SELECT max(ts) FROM commander_events WHERE acknowledged=1").fetchone()[0]
    except sqlite3.OperationalError:
        conn.close()
        return {"metric": "commander_same_chat_delivery", "total": 0, "unacked": 0,
                "oldest_unacked_age_secs": None, "newest_ack_age_secs": None,
                "drain_alive": True, "status": "green", "note": "no commander_events table"}
    finally:
        conn.close()
    oldest_age = (now - _epoch(oldest_unacked)) if _epoch(oldest_unacked) is not None else None
    newest_ack_age = (now - _epoch(newest_acked)) if _epoch(newest_acked) is not None else None
    stalled = (unacked > 0 and oldest_age is not None and oldest_age > stall_after_secs
               and (newest_ack_age is None or newest_ack_age > stall_after_secs))
    drain_alive = not stalled
    return {
        "metric": "commander_same_chat_delivery",
        "total": total,
        "unacked": unacked,
        "oldest_unacked_age_secs": (round(oldest_age) if oldest_age is not None else None),
        "newest_ack_age_secs": (round(newest_ack_age) if newest_ack_age is not None else None),
        "drain_alive": drain_alive,
        "status": "green" if drain_alive else "red",
        "note": ("agent_notifier drain keeping up (no stalled backlog)" if drain_alive
                 else "same-chat drain STALLED — unacked events not delivered; owner not notified"),
    }


def _read_marker(db_path: str, query: str):
    """Read a single scalar heartbeat marker read-only; None on any error/missing table."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        return None
    try:
        r = conn.execute(query).fetchone()
        return r[0] if r else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def _loop_specs() -> list:
    """Per-loop heartbeat markers (a fresh row each tick). The supervisor now has a dedicated
    periodic `supervisor_heartbeat` (added because its only prior marker, `supervisor_prompts`,
    is decision-driven, not periodic)."""
    from core.control_plane.store import db_path as _cp_path
    ac = _ac_db()
    return [
        {"name": "continuation_watchdog", "db": ac,
         "query": "SELECT last_run_at FROM cw_health WHERE id=1", "interval": 30},
        {"name": "orchestrator", "db": ac,
         "query": "SELECT max(updated_at) FROM agent_orchestrator", "interval": 45},
        {"name": "direct_agent_lifecycle", "db": ac,
         "query": "SELECT max(updated_at) FROM direct_agent_lifecycle", "interval": 45},
        {"name": "control_plane_engine", "db": _cp_path(),
         "query": "SELECT max(updated_at) FROM agent", "interval": 30},
        {"name": "supervisor", "db": ac,
         "query": "SELECT last_run_at FROM supervisor_heartbeat WHERE id=1", "interval": 45},
    ]


def loop_liveness_report(*, now: Optional[float] = None, stall_multiplier: float = 3.0) -> dict:
    """Heartbeat liveness for the Owner OS control loops. A loop whose last-activity marker is
    older than `stall_multiplier` × its interval is STALLED (the health_monitor-stall class,
    generalized). A loop with no marker yet is `unknown` (informational, not a failure).
    Read-only."""
    now = now if now is not None else time.time()
    loops = []
    stalled = 0
    for spec in _loop_specs():
        ts = _read_marker(spec["db"], spec["query"])
        age = (now - _epoch(ts)) if _epoch(ts) is not None else None
        if age is None:
            state = "unknown"
        elif age < spec["interval"] * stall_multiplier:
            state = "alive"
        else:
            state = "stalled"
            stalled += 1
        loops.append({"loop": spec["name"], "interval_secs": spec["interval"],
                      "last_activity_age_secs": (round(age) if age is not None else None),
                      "state": state})
    return {
        "metric": "loop_liveness",
        "loops": loops,
        "stalled_loops": stalled,
        "status": "red" if stalled else "green",
        "note": ("a control loop is STALLED — no recent tick" if stalled
                 else "all measurable control loops ticking"),
    }


_DROPIN_DIR = "/etc/systemd/system/ai-runtime.service.d"


def _read_canary_allowlist(dropin_path: str = None, dropin_dir: str = None) -> set:
    """Read the EFFECTIVE actuation canary allowlist the way systemd resolves it.

    This used to open one hardcoded file, `canary.conf`. systemd reads every `*.conf`
    in the drop-in directory in LEXICAL order and lets the last assignment win, and on
    this host there are two:

        canary.conf              (Aug 3)  …=cp-canary:0.0
        zz-actuation-scope.conf  (Aug 5)  …=cp-canary:0.0,mess-qa-automation:0.0

    `zz-` sorts last, so the live process really runs the wider list — confirmed
    against /proc. Reading only the first file made the safety report understate the
    actuator's scope: it declared the allowlist to be `cp-canary:0.0` alone and then
    counted `mess-qa-automation:0.0` as a BREACH, when that target had in fact been
    granted actuation on 2026-08-05.

    That is the failure mode a scope check must never have. Anyone widening the
    allowlist through a later-sorting drop-in would have been invisible to the very
    report whose job is to notice, and it would have gone on reporting the scope as
    confined. Whether the wider grant is correct is an owner's decision; the check
    only has to see the same list the actuator enforces.

    Read-only, no secrets — the value is a list of agent targets.
    """
    if dropin_path:
        paths = [dropin_path]
    else:
        import glob
        paths = sorted(glob.glob(os.path.join(dropin_dir or _DROPIN_DIR, "*.conf")))
    found = None
    for path in paths:                      # lexical order; LAST assignment wins
        try:
            for line in open(path):
                line = line.strip()
                if line.startswith("#") or "CONTROL_PLANE_CANARY_AGENTS=" not in line:
                    continue
                val = line.split("CONTROL_PLANE_CANARY_AGENTS=", 1)[1].strip().strip('"')
                found = {t.strip() for t in val.split(",") if t.strip()}
        except Exception:  # noqa: BLE001 — an unreadable drop-in is not an allowlist
            continue
    if found is not None:
        return found
    try:
        from core.control_plane import actuator
        return set(actuator.CANARY_AGENTS)
    except Exception:  # noqa: BLE001
        return set()


def actuation_scope_report(*, now: Optional[float] = None, conn=None, allowlist: set = None,
                           dropin_path: str = None, dropin_dir: str = None,
                           active_window_secs: float = 86400.0,
                           synthetic_prefixes=("canary-synthetic",)) -> dict:
    """Verify the actuator never broadened beyond the canary: every target in the durable
    `cp_action` ledger must be on the canary allowlist (or a known synthetic test target).
    Read-only — the strongest safety check while the actuator is armed.

    The breach set is asked of the WHOLE ledger and never shrinks: once the actuator has
    escaped its allowlist that is a permanent fact, and hiding it later would be the one
    change this check must never make.

    But `red` is asked of NOW. The query was unbounded in time, so a single historical
    breach pinned this report red forever: on this host `arbitrage2-opus:0.0` and
    `mess-qa-automation:0.0` were actuated by `autopilot_next_step` between 2026-08-04 and
    2026-08-07, the ledger has recorded nothing at all in the 26 days since, and the
    report still read red. An alarm in that state cannot distinguish "the actuator is
    escaping right now" from "it did, last month" — which is precisely how a real breach
    would arrive unnoticed, wearing the same colour the dashboard has shown all along.

    So: `red` when a breach is INSIDE the window, `amber` when the only breaches are
    historical. Deliberately never `green` while a breach is on record — the actuator did
    once escape, and that must not read as clean — and the same active/historical split
    `notification_failure_report` already uses.
    """
    now = now if now is not None else time.time()
    allow = (set(allowlist) if allowlist is not None
             else _read_canary_allowlist(dropin_path, dropin_dir))
    own = conn is None
    if conn is None:
        from core.control_plane.store import connect, init_db
        conn = connect()
        init_db(conn)
    try:
        rows = conn.execute("SELECT target, created_at FROM cp_action").fetchall()
    finally:
        if own:
            conn.close()
    actuated = {r[0] for r in rows}
    synthetic = {t for t in actuated if any(t.startswith(p) for p in synthetic_prefixes)}
    unexpected = actuated - allow - synthetic
    when = _split([r[1] for r in rows if r[0] in unexpected], now, active_window_secs)
    # An undated or unparseable breach row counts as ACTIVE. Unknown time must fail
    # safe here: treating "we cannot tell when this happened" as historical would let a
    # breach downgrade itself by writing a bad timestamp, which is the one direction a
    # safety check may never move.
    def _in_window(stamp) -> bool:
        e = _epoch(stamp)
        return True if e is None else (now - e) < active_window_secs

    active = {r[0] for r in rows if r[0] in unexpected and _in_window(r[1])}
    if not unexpected:
        status, note = "green", "actuation confined to the canary allowlist (+ synthetic tests)"
    elif active:
        status, note = "red", "SCOPE BREACH — a non-canary agent was actuated"
    else:
        status, note = "amber", ("scope breach on record but none in window — historical, "
                                 "not a live escape")
    return {
        "metric": "actuation_scope",
        "canary_allowlist": sorted(allow),
        "actuated_targets": sorted(actuated),
        "synthetic_test_targets": sorted(synthetic),
        "unexpected_actuated": sorted(unexpected),
        "unexpected_active": sorted(active),
        "breach_newest_at": when["newest_at"],
        "breach_newest_age_secs": when["newest_age_secs"],
        "active_window_secs": active_window_secs,
        "classification": when["classification"],
        "status": status,
        "note": note,
    }


_VALID_NOTIF_STATES = ("pending", "sending", "sent", "acked", "failed", "dead_letter", "resolved")


def consistency_report(*, now: Optional[float] = None, conn=None) -> dict:
    """Internal-consistency INVARIANTS (not just measurement). Read-only:

      * a CTO cursor must never point past the latest event (`last_event_id <= max(event.id)`);
      * every notification `state` must be a known state;
      * ledger/lease fence integrity: for each agent, `max(cp_action.fence_token) <=` its
        resource's current `resource_lease.fence_token` (an action recorded under a fence
        higher than the current lease is impossible — corruption, incl. across restart).

    Any violation → red. Orphan actions (a cp_action target with no lease row) are reported
    separately (informational — a lease row is never deleted, only expired, so this should be
    empty)."""
    own = conn is None
    if conn is None:
        from core.control_plane.store import connect, init_db
        conn = connect()
        init_db(conn)
    try:
        latest = conn.execute("SELECT coalesce(max(id),0) FROM event").fetchone()[0]
        cursor_ahead = [{"consumer": r[0], "last_event_id": r[1], "latest": latest}
                        for r in conn.execute("SELECT consumer,last_event_id FROM cto_cursor").fetchall()
                        if (r[1] or 0) > latest]
        invalid_states = [r[0] for r in conn.execute(
            "SELECT DISTINCT state FROM notification").fetchall() if r[0] not in _VALID_NOTIF_STATES]
        fence_violations = []
        orphan_actions = []
        for target, max_fence in conn.execute(
                "SELECT target, max(fence_token) FROM cp_action GROUP BY target").fetchall():
            row = conn.execute("SELECT fence_token FROM resource_lease WHERE resource=?",
                               (f"agent:{target}",)).fetchone()
            if row is None:
                orphan_actions.append(target)
            elif max_fence is not None and row[0] is not None and max_fence > row[0]:
                fence_violations.append({"target": target, "action_fence": max_fence,
                                         "lease_fence": row[0]})
    finally:
        if own:
            conn.close()
    violations = bool(cursor_ahead or invalid_states or fence_violations)
    return {
        "metric": "consistency",
        "latest_event_id": latest,
        "cursors_ahead_of_log": cursor_ahead,
        "invalid_notification_states": invalid_states,
        "fence_violations": fence_violations,
        "orphan_actions": orphan_actions,
        "consistent": not violations,
        "status": "red" if violations else "green",
        "note": ("INVARIANT VIOLATION — data inconsistency" if violations
                 else "all consistency invariants hold"),
    }


# terminal notification states — anything else a restart could leave orphaned or mid-flight.
_TERMINAL_NOTIF_STATES = ("sent", "acked", "dead_letter", "resolved")
# reclaimable by the notifier drain — api.pending_notifications() selects EXACTLY these.
_RECLAIMABLE_NOTIF_STATES = ("pending", "failed")


def restart_consistency_report(*, now: Optional[float] = None, stale_secs: float = 900.0,
                               supervisor_interval: int = 45, stall_multiplier: float = 3.0,
                               conn=None) -> dict:
    """RESTART DURABILITY: after a process restart every piece of in-flight durable state must
    be either terminal or reclaimable by a running loop, and no cursor may have moved past the
    log. Read-only. Flags, per store:

      * notification OUTBOX — a row in a non-terminal state the drain will NOT reclaim
        (`api.pending_notifications` selects only pending/failed) is restart-ORPHANED
        (e.g. stuck 'sending'); a reclaimable row (pending/failed) older than `stale_secs`
        means the drain loop isn't running.
      * continuation LEASE / action LEDGER — a cp_action submitted but neither verified nor
        blocked, last touched > `stale_secs` ago, is an actuation abandoned mid-flight (a
        restart landed between submit and verify); it must be re-verified, never re-issued.
      * CTO CURSOR — a cursor past the latest event id would re-deliver or skip after a
        restart (dup/loss); it must stay ≤ the log head.
      * SUPERVISOR HEARTBEAT — must resume ticking after a restart; a marker older than
        `supervisor_interval × stall_multiplier` means the supervisor loop didn't come back.
        No marker at all is `unknown` (informational), not unsafe.

    `restart_safe` is true only when every check is clean."""
    now = now if now is not None else time.time()
    own = conn is None
    if conn is None:
        from core.control_plane.store import connect, init_db
        conn = connect()
        init_db(conn)
    try:
        latest_event = conn.execute("SELECT coalesce(max(id),0) FROM event").fetchone()[0]
        orphaned_notifications = []
        stale_reclaimable = []
        for nid, state, created in conn.execute(
                "SELECT id,state,created_at FROM notification").fetchall():
            if state in _TERMINAL_NOTIF_STATES:
                continue
            age = (now - _epoch(created)) if _epoch(created) is not None else None
            if state not in _RECLAIMABLE_NOTIF_STATES:
                orphaned_notifications.append(
                    {"id": nid, "state": state, "age_secs": round(age) if age is not None else None})
            elif age is not None and age > stale_secs:
                stale_reclaimable.append({"id": nid, "state": state, "age_secs": round(age)})
        abandoned_actions = []
        for idkey, target, updated in conn.execute(
                "SELECT idkey,target,updated_at FROM cp_action "
                "WHERE submitted=1 AND verified=0 AND blocked=0").fetchall():
            age = (now - _epoch(updated)) if _epoch(updated) is not None else None
            if age is not None and age > stale_secs:
                abandoned_actions.append({"idkey": idkey, "target": target, "age_secs": round(age)})
        cursor_ahead = [{"consumer": r[0], "last_event_id": r[1], "latest": latest_event}
                        for r in conn.execute("SELECT consumer,last_event_id FROM cto_cursor").fetchall()
                        if (r[1] or 0) > latest_event]
    finally:
        if own:
            conn.close()
    hb_ts = _read_marker(_ac_db(), "SELECT last_run_at FROM supervisor_heartbeat WHERE id=1")
    hb_age = (now - _epoch(hb_ts)) if _epoch(hb_ts) is not None else None
    supervisor_stalled = hb_age is not None and hb_age > supervisor_interval * stall_multiplier
    supervisor_state = ("unknown" if hb_age is None
                        else "stalled" if supervisor_stalled else "alive")
    unsafe = bool(orphaned_notifications or stale_reclaimable or abandoned_actions
                  or cursor_ahead or supervisor_stalled)
    return {
        "metric": "restart_consistency",
        "latest_event_id": latest_event,
        "orphaned_notifications": orphaned_notifications,
        "stale_reclaimable_notifications": stale_reclaimable,
        "abandoned_inflight_actions": abandoned_actions,
        "cursors_ahead_of_log": cursor_ahead,
        "supervisor_heartbeat_age_secs": (round(hb_age) if hb_age is not None else None),
        "supervisor_state": supervisor_state,
        "stale_secs": stale_secs,
        "restart_safe": not unsafe,
        "status": "red" if unsafe else "green",
        "note": ("restart-unsafe durable state present — see fields" if unsafe
                 else "all in-flight durable state is terminal, reclaimable, or fresh"),
    }


def _log_stats(conn, table: str, ts_col: str, is_epoch: bool, now: float, window: float) -> dict:
    """Size + age-span + recent-rate for one append-only log. Read-only. `is_epoch` uses the
    numeric ts column directly in SQL (cheap for a big table); otherwise the ISO column is
    parsed in Python."""
    total = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    if total == 0:
        return {"table": table, "rows": 0, "oldest_age_secs": None, "newest_age_secs": None,
                "recent_rows": 0, "rate_per_hr": 0.0}
    if is_epoch:
        oldest = conn.execute(f"SELECT min({ts_col}) FROM {table}").fetchone()[0]
        newest = conn.execute(f"SELECT max({ts_col}) FROM {table}").fetchone()[0]
        recent = conn.execute(f"SELECT count(*) FROM {table} WHERE {ts_col} >= ?",
                              (now - window,)).fetchone()[0]
    else:
        epochs = [_epoch(r[0]) for r in conn.execute(f"SELECT {ts_col} FROM {table}").fetchall()]
        epochs = [e for e in epochs if e is not None]
        oldest = min(epochs) if epochs else None
        newest = max(epochs) if epochs else None
        recent = sum(1 for e in epochs if e >= now - window)
    return {
        "table": table, "rows": total,
        "oldest_age_secs": (round(now - oldest) if oldest is not None else None),
        "newest_age_secs": (round(now - newest) if newest is not None else None),
        "recent_rows": recent,
        "rate_per_hr": round(recent * 3600.0 / window, 2),
    }


def log_growth_report(*, now: Optional[float] = None, rate_window_secs: float = 3600.0,
                      advisory_rows: int = 50000, advisory_rate_per_hr: float = 2000.0,
                      conn=None) -> dict:
    """Append-only log growth + retention. Read-only — NEVER prunes/rotates. Reports, per
    durable log (`event` / `cp_action` / `notification`): row count, oldest & newest age (the
    retained span), and the recent creation rate (rows in the last `rate_window_secs` →
    per-hour). Advisory thresholds flag a log large enough (`advisory_rows`) or growing fast
    enough (`advisory_rate_per_hr`) to warrant an owner-approved retention policy.

    `status` stays green: unbounded growth is a CAPACITY/retention signal, not a correctness
    failure, and pruning is an owner-gated destructive action — this only measures and advises.
    A log crossing a threshold sets `advise` with the specific `advisory_reasons`."""
    now = now if now is not None else time.time()
    own = conn is None
    if conn is None:
        from core.control_plane.store import connect, init_db
        conn = connect()
        init_db(conn)
    try:
        logs = [
            _log_stats(conn, "event", "ts_epoch", True, now, rate_window_secs),
            _log_stats(conn, "cp_action", "created_at", False, now, rate_window_secs),
            _log_stats(conn, "notification", "created_at", False, now, rate_window_secs),
        ]
    finally:
        if own:
            conn.close()
    advise_tables = []
    for l in logs:
        reasons = []
        if l["rows"] > advisory_rows:
            reasons.append(f"rows>{advisory_rows}")
        if l["rate_per_hr"] > advisory_rate_per_hr:
            reasons.append(f"rate>{advisory_rate_per_hr}/hr")
        l["advise"] = bool(reasons)
        l["advisory_reasons"] = reasons
        if reasons:
            advise_tables.append(l["table"])
    return {
        "metric": "log_growth",
        "window_secs": rate_window_secs,
        "advisory_rows": advisory_rows,
        "advisory_rate_per_hr": advisory_rate_per_hr,
        "logs": logs,
        "total_rows": sum(l["rows"] for l in logs),
        "advise_tables": advise_tables,
        "advise": bool(advise_tables),
        "status": "green",   # capacity/retention advisory, never a correctness failure
        "note": (f"retention advisable (owner-gated) for: {advise_tables}" if advise_tables
                 else "log sizes and growth within advisory thresholds"),
    }


def closed_loop_wake_report(*, now: Optional[float] = None, conn=None) -> dict:
    """Task 211 status surface: wakes actually delivered (real ChatGPT user turns) by
    trigger class, the owner_intervention metric, and loop-SLO breach counts. Read-only,
    additive — a small wrapper over `core.closed_loop_wake.counters`."""
    now = now if now is not None else time.time()
    from core import closed_loop_wake as _clw
    own = conn is None
    if conn is None:
        from core.control_plane.store import connect, init_db
        conn = connect()
        init_db(conn)
    try:
        c = _clw.counters(conn=conn, now=now)
    finally:
        if own:
            conn.close()
    return {"metric": "closed_loop_wake", **c, "status": "green"}


def observability_summary(*, now: Optional[float] = None) -> dict:
    """Combined read-only view. `all_clear` is true when there are no ACTIVE failures,
    regardless of historical totals — so a green system is not flagged by stale counters."""
    now = now if now is not None else time.time()
    notif = notification_failure_report(now=now)
    notif_hist = notification_history_report(now=now)
    jobs = runtime_job_failure_report(now=now)
    try:
        blockers = runtime_blockers_report(now=now)
    except Exception as e:  # noqa: BLE001 — an unreadable jobs DB must not blind the rest
        blockers = {"metric": "runtime_blockers", "error": str(e)[:200],
                    "stalled_count": 0, "stalled_jobs": [], "active_jobs": [],
                    "waiting_approval": [], "recent_failed": []}
    registry = registry_health_report(now=now)
    gates = owner_gate_report(now=now)
    leases = lease_report(now=now)
    cto = cto_cursor_report(now=now)
    commander = commander_delivery_report(now=now)
    loops = loop_liveness_report(now=now)
    scope = actuation_scope_report(now=now)
    consistency = consistency_report(now=now)
    restartc = restart_consistency_report(now=now)
    growth = log_growth_report(now=now)
    try:
        closed_loop_wake = closed_loop_wake_report(now=now)
    except Exception as e:  # noqa: BLE001 — additive; must never blind the rest
        closed_loop_wake = {"metric": "closed_loop_wake", "error": str(e)[:200],
                            "wakes_delivered_by_trigger_class": {},
                            "wakes_delivered_total": 0, "owner_intervention_count": 0,
                            "loop_slo_rewoken": 0, "loop_slo_escalated": 0,
                            "loop_slo_resolved": 0}
    active = notif["active"] + jobs["active"]
    # consolidated reasons the aggregate is red (empty ⇒ green) — so a consumer sees WHICH
    # check failed without parsing every sub-report.
    red_reasons = []
    if active > 0:
        red_reasons.append(f"active_failures={active}")
    if not registry["engine_alive"]:
        red_reasons.append("discovery_engine_stalled")
    if loops["stalled_loops"]:
        red_reasons.append(f"stalled_loops={loops['stalled_loops']}")
    if not commander["drain_alive"]:
        red_reasons.append("same_chat_drain_stalled")
    if cto["stale_consumers"]:
        red_reasons.append(f"stale_cto_cursors={cto['stale_consumers']}")
    # Only a breach IN THE WINDOW makes the summary unhealthy. A historical one stays
    # visible on its own key below, because it must never disappear — but a permanently
    # unhealthy summary is one nobody can read a new fault out of.
    if scope["unexpected_active"]:
        red_reasons.append(f"actuation_scope_breach={scope['unexpected_active']}")
    if not consistency["consistent"]:
        red_reasons.append("consistency_violation")
    if not restartc["restart_safe"]:
        red_reasons.append("restart_unsafe")
    if blockers["stalled_count"]:
        red_reasons.append(f"runtime_jobs_stalled={blockers['stalled_count']}")
    healthy = not red_reasons
    return {
        "notifications": notif,
        "notification_history": notif_hist,
        "runtime_jobs": jobs,
        "runtime_blockers": blockers,
        "registry_health": registry,
        "open_owner_gates": gates,
        "resource_leases": leases,
        "cto_cursor": cto,
        "commander_same_chat_delivery": commander,
        "loop_liveness": loops,
        "stalled_loops": loops["stalled_loops"],
        "actuation_scope": scope,
        "actuation_scope_breach": bool(scope["unexpected_active"]),
        # The permanent record, independent of the window: the actuator escaped once.
        "actuation_scope_breach_ever": bool(scope["unexpected_actuated"]),
        "consistency": consistency,
        "consistent": consistency["consistent"],
        "restart_consistency": restartc,
        "restart_safe": restartc["restart_safe"],
        "log_growth": growth,
        "log_total_rows": growth["total_rows"],
        "log_retention_advise": growth["advise"],   # advisory: retention (owner-gated), not red
        "red_reasons": red_reasons,
        "active_failures_total": active,
        "historical_failures_total": notif["historical"] + jobs["historical"],
        "engine_alive": registry["engine_alive"],
        "same_chat_drain_alive": commander["drain_alive"],
        "stale_cto_cursors": cto["stale_consumers"],
        "open_gate_backlog": gates["open_total"],
        "owner_gate_sla_breaches": gates["breached_count"],   # advisory: overdue owner decisions
        "owner_gate_escalate": gates["escalate"],             # not red — owner action, not a fault
        "closed_loop_wake": closed_loop_wake,                 # task 211: additive, not red/green
        "all_clear": healthy,
        "status": "green" if healthy else "red",
        "checked_at": (datetime.fromtimestamp(now, timezone.utc).isoformat()),
    }
