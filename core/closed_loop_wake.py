"""Closed-loop wake watchdog (task 211) — the two pieces the wake bridge and the
stall doctor cannot cover on their own:

  * SLO watchdog: a wake was delivered (a REAL ChatGPT user turn landed) and no
    progress followed. Re-wake once; if that also gets no progress, escalate as
    actionable so the owner is never left believing the loop is still running.
  * owner_intervention metric: the closed loop's whole point is that the OWNER never
    has to type into an agent pane. When a pane that was notified owner_prompt/blocker
    resumes to working WITHOUT any proof the companion ever delivered a wake for it,
    that is conservative, positive evidence a human typed there directly — the loop
    failed the owner once. This is a METRIC, not itself a wake trigger (waking the
    owner to tell them they already acted would be noise).

Both pieces are additive: nothing here changes when or whether the bridge/doctor/watch
modules fire; this module only observes their durable state (wake_delivery, wake_audit,
the event log) and adds its own small, restart-safe table.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

# How long a delivered wake gets before "no progress" becomes worth acting on.
# Env-tunable per the task-211 spec (`WAKE_LOOP_SLO_SECS`).
WAKE_LOOP_SLO_SECS = int(os.getenv("WAKE_LOOP_SLO_SECS", "900"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wake_loop_watch (
    event_id INTEGER PRIMARY KEY,
    target TEXT, project_id TEXT, delivered_ts REAL, delivered_at TEXT,
    rewoken INTEGER DEFAULT 0, rewoken_ts REAL, rewoken_event_id INTEGER,
    escalated INTEGER DEFAULT 0, escalated_ts REAL, escalated_event_id INTEGER
);
CREATE TABLE IF NOT EXISTS owner_intervention_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, at TEXT, target TEXT, project_id TEXT, notified_event_id INTEGER,
    prev_notified_cls TEXT, event_id INTEGER
)
"""


def _conn(conn=None):
    conn, own = _c(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


# ── SLO watchdog ─────────────────────────────────────────────────────────────
def register_delivery(*, event_id: int, target: str = "", project_id: str = "",
                      conn=None, now: Optional[float] = None) -> None:
    """Start SLO tracking for a wake that a companion delivery just confirmed landed
    (a real ChatGPT user turn). Idempotent — a re-delivery of the same event id (which
    should never happen past the submission latch, but this must never crash if it
    does) leaves the original tracking row alone."""
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO wake_loop_watch (event_id,target,project_id,"
            "delivered_ts,delivered_at) VALUES (?,?,?,?,?)",
            (int(event_id), target or "", project_id or "", now, now_iso()))
        conn.commit()
    finally:
        if own:
            conn.close()


def _progress_since(conn, target: str, since_ts: float) -> bool:
    """Has ANYTHING happened for this agent since the wake was delivered? Any newer CTO
    event correlated to this target — a fresh agent_watch class, a stall_doctor action,
    a second wake decision — counts as progress. Conservative on purpose: this only
    suppresses a re-wake/escalation, never suppresses one that is actually due.

    The watchdog's OWN bookkeeping events (source='closed_loop_wake' — the re-wake and
    the escalation it emits) are excluded: otherwise the re-wake's own event row would
    read as "progress" on the very next scan and the episode could never escalate."""
    row = conn.execute(
        "SELECT COUNT(*) FROM event WHERE agent_id=? AND ts_epoch > ? "
        "AND source != 'closed_loop_wake'",
        (target, since_ts)).fetchone()
    return bool(row and row[0])


def slo_scan(*, conn=None, now: Optional[float] = None,
            emit_fn: Optional[Callable] = None) -> dict:
    """One pass: for every tracked delivery with no progress past the SLO, re-wake
    once; for one still stalled a further SLO window after the re-wake, escalate."""
    now = now if now is not None else now_ts()
    if emit_fn is None:
        from core.control_plane.cto import emit as emit_fn  # noqa: F811
    conn, own = _conn(conn)
    try:
        rewoken, escalated = [], []
        rows = conn.execute(
            "SELECT event_id, target, project_id, delivered_ts, rewoken, rewoken_ts, "
            "escalated FROM wake_loop_watch WHERE escalated=0").fetchall()
        for eid, target, project_id, delivered_ts, is_rewoken, rewoken_ts, is_escalated in rows:
            if not target:
                continue
            if _progress_since(conn, target, delivered_ts):
                continue
            if not is_rewoken:
                if now - delivered_ts < WAKE_LOOP_SLO_SECS:
                    continue
                ev = emit_fn(
                    "closed_loop_wake", "wake_loop_no_progress", project_id=project_id,
                    agent_id=target, severity="high", owner_action_required=True,
                    payload={"target": target, "original_event_id": eid,
                             "slo_secs": WAKE_LOOP_SLO_SECS},
                    action_taken=(f"{target}: wake {eid} delivered but no progress in "
                                  f"{WAKE_LOOP_SLO_SECS}s — re-waking once"),
                    correlation_id=f"agentwatch:{target}",
                    dedup_key=f"wakeloop:{eid}:no_progress",
                    dedup_window_secs=86400, conn=conn)
                new_eid = (ev or {}).get("event_id")
                conn.execute(
                    "UPDATE wake_loop_watch SET rewoken=1, rewoken_ts=?, "
                    "rewoken_event_id=? WHERE event_id=?", (now, new_eid, eid))
                conn.commit()
                rewoken.append({"event_id": eid, "rewoken_event_id": new_eid,
                                "target": target})
                continue
            if now - (rewoken_ts or now) < WAKE_LOOP_SLO_SECS:
                continue
            ev = emit_fn(
                "closed_loop_wake", "wake_loop_stalled", project_id=project_id,
                agent_id=target, severity="critical", owner_action_required=True,
                payload={"target": target, "original_event_id": eid,
                         "slo_secs": WAKE_LOOP_SLO_SECS},
                action_taken=(f"{target}: wake {eid} re-woken with still no progress — "
                              "escalating"),
                correlation_id=f"agentwatch:{target}",
                dedup_key=f"wakeloop:{eid}:stalled",
                dedup_window_secs=86400, conn=conn)
            new_eid = (ev or {}).get("event_id")
            conn.execute(
                "UPDATE wake_loop_watch SET escalated=1, escalated_ts=?, "
                "escalated_event_id=? WHERE event_id=?", (now, new_eid, eid))
            conn.commit()
            escalated.append({"event_id": eid, "escalated_event_id": new_eid,
                              "target": target})
        return {"rewoken": rewoken, "escalated": escalated}
    finally:
        if own:
            conn.close()


# ── owner_intervention metric ───────────────────────────────────────────────
def detect_owner_intervention(*, target: str, prev_notified_cls: str, project_id: str = "",
                              conn=None, now: Optional[float] = None,
                              emit_fn: Optional[Callable] = None) -> Optional[int]:
    """Conservative: fires ONLY when the pane was notified owner_prompt/blocker AND
    there is positive proof (a row in `event`) that a wake for it was NEVER delivered
    by the companion. A wake that DID deliver, or no notification at all, is never
    misclassified as an intervention — this must never count a companion-submitted
    turn as the owner's."""
    if prev_notified_cls not in ("owner_prompt", "blocker"):
        return None
    now = now if now is not None else now_ts()
    if emit_fn is None:
        from core.control_plane.cto import emit as emit_fn  # noqa: F811
    from core import wake_bridge as wb
    conn, own = _conn(conn)
    try:
        # wake_delivery is created lazily by wake_bridge; ensure it exists before this
        # module — which may run first in a fresh DB — queries it.
        conn.execute(wb._DELIVERY_SCHEMA)
        wb._migrate_delivery(conn)
        corr = f"agentwatch:{target}"
        row = conn.execute(
            "SELECT id FROM event WHERE correlation_id=? ORDER BY id DESC LIMIT 1",
            (corr,)).fetchone()
        if not row:
            return None
        notified_event_id = int(row[0])
        delivered = conn.execute(
            "SELECT 1 FROM wake_delivery WHERE event_id=? AND delivered=1",
            (notified_event_id,)).fetchone()
        if delivered:
            return None  # the companion handled it — not an intervention
        already = conn.execute(
            "SELECT 1 FROM owner_intervention_log WHERE notified_event_id=?",
            (notified_event_id,)).fetchone()
        if already:
            return None
        ev = emit_fn(
            "closed_loop_wake", "owner_intervention", project_id=project_id,
            agent_id=target, severity="info", owner_action_required=False, push=False,
            payload={"target": target, "prev_notified_cls": prev_notified_cls,
                     "notified_event_id": notified_event_id},
            action_taken=(f"{target}: resumed without a delivered wake for event "
                          f"{notified_event_id} — the owner acted directly"),
            correlation_id=corr,
            dedup_key=f"ownerintervention:{notified_event_id}",
            dedup_window_secs=86400, conn=conn)
        eid = (ev or {}).get("event_id")
        conn.execute(
            "INSERT INTO owner_intervention_log (ts,at,target,project_id,"
            "notified_event_id,prev_notified_cls,event_id) VALUES (?,?,?,?,?,?,?)",
            (now, now_iso(), target, project_id or "", notified_event_id,
             prev_notified_cls, eid))
        conn.commit()
        return eid
    finally:
        if own:
            conn.close()


# ── observability counters (additive, for diagnostics.observability_summary) ─
def counters(*, conn=None, now: Optional[float] = None) -> dict:
    """Small additive read: wakes delivered by trigger class, owner_intervention
    count, and loop-SLO breach counts (re-woken / escalated)."""
    from core import wake_bridge as wb
    conn, own = _conn(conn)
    try:
        conn.execute(wb._DELIVERY_SCHEMA)
        wb._migrate_delivery(conn)
        rows = conn.execute(
            "SELECT COALESCE(a.event_type,''), COUNT(*) FROM wake_delivery d "
            "JOIN wake_audit a ON a.event_id = d.event_id "
            "WHERE d.delivered=1 GROUP BY 1").fetchall()
        by_trigger: dict = {}
        for etype, n in rows:
            tc = wb.trigger_class_for(etype)
            by_trigger[tc] = by_trigger.get(tc, 0) + int(n)
        owner_intervention_count = conn.execute(
            "SELECT COUNT(*) FROM owner_intervention_log").fetchone()[0]
        rewoken = conn.execute(
            "SELECT COUNT(*) FROM wake_loop_watch WHERE rewoken=1").fetchone()[0]
        escalated = conn.execute(
            "SELECT COUNT(*) FROM wake_loop_watch WHERE escalated=1").fetchone()[0]
        return {
            "wakes_delivered_by_trigger_class": by_trigger,
            "wakes_delivered_total": sum(by_trigger.values()),
            "owner_intervention_count": int(owner_intervention_count),
            "loop_slo_rewoken": int(rewoken),
            "loop_slo_escalated": int(escalated),
            "loop_slo_secs": WAKE_LOOP_SLO_SECS,
        }
    finally:
        if own:
            conn.close()
