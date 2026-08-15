"""CTO inbox / push contract.

The `event` table IS the durable canonical CTO inbox. A ChatGPT/CTO consumer keeps a
persistent cursor; on its next invocation it reads exactly the verified deltas since
its last acknowledged cursor (`cto_brief_since`), then acks. This is restart-safe: the
cursor lives in the DB, so a service restart loses nothing and never re-delivers.

Separation of concerns (explicit — no impossible claims):
  (a) server-side Owner OS continuous control (collectors/controllers) — always on;
  (b) owner PUSH via the notification outbox for urgent/high-value events;
  (c) this durable CTO inbox, consumed by ChatGPT immediately on its NEXT invocation;
  (d) optional scheduled ChatGPT wake checks where supported.
We do NOT claim instant awakening of ChatGPT — (c) guarantees no missed context.
"""
from __future__ import annotations

import json
from typing import Optional

from core.control_plane.store import now_iso
from core.control_plane.api import _c, append_event, enqueue_notification


# severities that must also be PUSHED to the owner (not just wait in the inbox)
_PUSH_SEVERITIES = ("critical", "high")


def emit(source: str, type: str, *, project_id: str = "", agent_id: str = "",
         severity: str = "info", owner_action_required: bool = False,
         payload: Optional[dict] = None, evidence_ref: str = "", action_taken: str = "",
         correlation_id: str = "", dedup_key: str = "", dedup_window_secs: int = 900,
         supersedes: int = 0, resolves: int = 0, push_channel: str = "telegram",
         push: Optional[bool] = None, conn=None) -> dict:
    """Record a CTO event and, for high/critical severity or owner_action_required,
    enqueue a durable owner push. Returns {event_id, pushed, notification_id?}.
    `push=False` forces inbox-only (used for channel-health meta-events, which must not
    recurse by pushing through the very channel that is down)."""
    conn, own = _c(conn)
    try:
        eid = append_event(source, type, entity_type="agent" if agent_id else "",
                           entity_id=agent_id, payload=payload, evidence_ref=evidence_ref,
                           correlation_id=correlation_id, project_id=project_id,
                           agent_id=agent_id, severity=severity,
                           owner_action_required=owner_action_required,
                           action_taken=action_taken, dedup_key=dedup_key,
                           dedup_window_secs=dedup_window_secs, supersedes=supersedes,
                           resolves=resolves, conn=conn)
        pushed = None
        want_push = push if push is not None else (severity in _PUSH_SEVERITIES or owner_action_required)
        if want_push:
            pushed = enqueue_notification(event_id=eid, channel=push_channel,
                                          dedup_key=dedup_key or f"evt:{eid}",
                                          correlation_id=correlation_id, conn=conn)
        # Wake the Night Shift executive at once for anything that matters. This is an
        # ACCELERATOR only: the bounded tick still runs, so a dropped signal costs latency,
        # never correctness. Deliberately best-effort — event recording must never fail
        # because the executive's table is unavailable.
        if severity in _PUSH_SEVERITIES or owner_action_required:
            try:
                from core import night_shift as _ns
                _ns.signal(source, type, target=agent_id or project_id,
                           payload={"event_id": eid, "severity": severity,
                                    "owner_action_required": bool(owner_action_required)},
                           conn=conn)
            except Exception:  # noqa: BLE001
                pass
        # Consult the wake bridge. Only CONSULTED when it is enabled, so a disabled bridge
        # writes no audit rows at all — otherwise every urgent event would leave a "skip:
        # bridge_disabled" row and the audit trail would be noise before it was ever used.
        try:
            from core import wake_bridge as _wb
            # Eligibility lives in the bridge, not here. The old inline test was severity or
            # owner_action_required, which meant an `owner_gate_opened` — info severity, no
            # owner_action flag — could never reach the bridge at all, however long it waited.
            if _wb._enabled()[0] and _wb.is_significant(
                    event_type=type, severity=severity,
                    owner_action_required=owner_action_required)["significant"]:
                _d = _wb.should_wake(event_id=eid, severity=severity, event_type=type,
                                     correlation_id=correlation_id,
                                     owner_action_required=owner_action_required,
                                     project_id=project_id, agent_id=agent_id, conn=conn)
                _wb.record(_d, event_id=eid, severity=severity, event_type=type,
                           correlation_id=correlation_id, project_id=project_id,
                           agent_id=agent_id, conn=conn)
        except Exception:  # noqa: BLE001 — the bridge must never break event recording
            pass
        return {"event_id": eid, "pushed": bool(pushed), "notification": pushed}
    finally:
        if own:
            conn.close()


def get_cursor(consumer: str, conn=None) -> int:
    conn, own = _c(conn)
    try:
        r = conn.execute("SELECT last_event_id FROM cto_cursor WHERE consumer=?",
                         (consumer,)).fetchone()
        return r[0] if r else 0
    finally:
        if own:
            conn.close()


def cto_brief_since(consumer: str, *, limit: int = 200, ack: bool = False, conn=None) -> dict:
    """Return the verified event deltas since `consumer`'s cursor, newest last, with a
    `next_cursor`. If `ack=True`, advance the cursor to the latest returned id (do this
    only after the consumer has durably processed the batch). Never returns cached
    prose — only rows from the event log."""
    conn, own = _c(conn)
    try:
        cur_id = get_cursor(consumer, conn=conn)
        rows = conn.execute(
            "SELECT id,ts,source,type,project_id,agent_id,severity,owner_action_required,"
            "action_taken,payload,evidence_ref,correlation_id,dedup_key,supersedes,resolves "
            "FROM event WHERE id>? ORDER BY id ASC LIMIT ?", (cur_id, limit)).fetchall()
        events = [{
            "event_id": r[0], "ts": r[1], "source": r[2], "type": r[3], "project_id": r[4],
            "agent_id": r[5], "severity": r[6], "owner_action_required": bool(r[7]),
            "action_taken": r[8], "payload": json.loads(r[9]) if r[9] else {},
            "evidence_ref": r[10], "correlation_id": r[11], "dedup_key": r[12],
            "supersedes": r[13], "resolves": r[14],
        } for r in rows]
        next_cursor = events[-1]["event_id"] if events else cur_id
        if ack and events:
            set_cursor(consumer, next_cursor, conn=conn)
        return {"consumer": consumer, "from_cursor": cur_id, "next_cursor": next_cursor,
                "count": len(events), "events": events}
    finally:
        if own:
            conn.close()


def set_cursor(consumer: str, last_event_id: int, conn=None) -> None:
    conn, own = _c(conn)
    try:
        conn.execute(
            "INSERT INTO cto_cursor(consumer,last_event_id,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(consumer) DO UPDATE SET last_event_id=excluded.last_event_id,"
            "updated_at=excluded.updated_at", (consumer, last_event_id, now_iso()))
        conn.commit()
    finally:
        if own:
            conn.close()


def ack_through(consumer: str, last_event_id: int, conn=None) -> dict:
    """Acknowledge up to a specific event id (monotonic — never moves backward)."""
    cur = get_cursor(consumer, conn=conn)
    if last_event_id > cur:
        set_cursor(consumer, last_event_id, conn=conn)
    return {"consumer": consumer, "cursor": max(cur, last_event_id)}
