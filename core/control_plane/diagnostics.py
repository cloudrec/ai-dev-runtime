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


def _read_canary_allowlist(dropin_path: str = None) -> set:
    """Read the LIVE actuation canary allowlist from the systemd drop-in (read-only, no
    secrets — the value is an agent target). Falls back to the in-process env global."""
    path = dropin_path or "/etc/systemd/system/ai-runtime.service.d/canary.conf"
    try:
        for line in open(path):
            if "CONTROL_PLANE_CANARY_AGENTS=" in line:
                val = line.split("CONTROL_PLANE_CANARY_AGENTS=", 1)[1].strip()
                return {t.strip() for t in val.split(",") if t.strip()}
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.control_plane import actuator
        return set(actuator.CANARY_AGENTS)
    except Exception:  # noqa: BLE001
        return set()


def actuation_scope_report(*, now: Optional[float] = None, conn=None, allowlist: set = None,
                           dropin_path: str = None,
                           synthetic_prefixes=("canary-synthetic",)) -> dict:
    """Verify the actuator never broadened beyond the canary: every target that appears in the
    durable `cp_action` ledger must be on the canary allowlist (or a known synthetic test
    target). Any REAL non-canary agent that was actuated is a scope BREACH → red. Read-only —
    the strongest safety check while the actuator is armed."""
    allow = set(allowlist) if allowlist is not None else _read_canary_allowlist(dropin_path)
    own = conn is None
    if conn is None:
        from core.control_plane.store import connect, init_db
        conn = connect()
        init_db(conn)
    try:
        actuated = {r[0] for r in conn.execute(
            "SELECT DISTINCT target FROM cp_action").fetchall()}
    finally:
        if own:
            conn.close()
    synthetic = {t for t in actuated if any(t.startswith(p) for p in synthetic_prefixes)}
    unexpected = actuated - allow - synthetic
    return {
        "metric": "actuation_scope",
        "canary_allowlist": sorted(allow),
        "actuated_targets": sorted(actuated),
        "synthetic_test_targets": sorted(synthetic),
        "unexpected_actuated": sorted(unexpected),
        "status": "red" if unexpected else "green",
        "note": ("SCOPE BREACH — a non-canary agent was actuated" if unexpected
                 else "actuation confined to the canary allowlist (+ synthetic tests)"),
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


def observability_summary(*, now: Optional[float] = None) -> dict:
    """Combined read-only view. `all_clear` is true when there are no ACTIVE failures,
    regardless of historical totals — so a green system is not flagged by stale counters."""
    now = now if now is not None else time.time()
    notif = notification_failure_report(now=now)
    notif_hist = notification_history_report(now=now)
    jobs = runtime_job_failure_report(now=now)
    registry = registry_health_report(now=now)
    gates = owner_gate_report(now=now)
    leases = lease_report(now=now)
    cto = cto_cursor_report(now=now)
    commander = commander_delivery_report(now=now)
    loops = loop_liveness_report(now=now)
    scope = actuation_scope_report(now=now)
    consistency = consistency_report(now=now)
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
    if scope["unexpected_actuated"]:
        red_reasons.append(f"actuation_scope_breach={scope['unexpected_actuated']}")
    if not consistency["consistent"]:
        red_reasons.append("consistency_violation")
    healthy = not red_reasons
    return {
        "notifications": notif,
        "notification_history": notif_hist,
        "runtime_jobs": jobs,
        "registry_health": registry,
        "open_owner_gates": gates,
        "resource_leases": leases,
        "cto_cursor": cto,
        "commander_same_chat_delivery": commander,
        "loop_liveness": loops,
        "stalled_loops": loops["stalled_loops"],
        "actuation_scope": scope,
        "actuation_scope_breach": bool(scope["unexpected_actuated"]),
        "consistency": consistency,
        "consistent": consistency["consistent"],
        "red_reasons": red_reasons,
        "active_failures_total": active,
        "historical_failures_total": notif["historical"] + jobs["historical"],
        "engine_alive": registry["engine_alive"],
        "same_chat_drain_alive": commander["drain_alive"],
        "stale_cto_cursors": cto["stale_consumers"],
        "open_gate_backlog": gates["open_total"],
        "all_clear": healthy,
        "status": "green" if healthy else "red",
        "checked_at": (datetime.fromtimestamp(now, timezone.utc).isoformat()),
    }
