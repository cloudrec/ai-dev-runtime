"""cp-canary SHADOW pinger — scope-confined significant-event emission (observe-only).

Runs inside the always-on Control Plane shadow loop. For ALLOWLISTED agents only (default: the
same canary set as actuation — `CONTROL_PLANE_PINGER_SHADOW_AGENTS`, else
`CONTROL_PLANE_CANARY_AGENTS`, i.e. `cp-canary:0.0`), it turns a transition INTO a significant
observable state (`completed` / `waiting_owner|waiting_input` / `externally_blocked` / `dead`)
into a pinger event via `event_pipeline.publish_significant_event`.

Guarantees:
  * SCOPE CONFINEMENT is hard — an agent not in the allowlist NEVER produces a pinger event
    here (a significant out-of-scope agent is only counted, never emitted).
  * OBSERVE-ONLY — no pane is touched, nothing is started/stopped.
  * NO PROACTIVE-DELIVERY CLAIM — the pipeline reports an honest `cto_inbox` floor unless a
    real receipt exists (owner_push / same_chat_wake remain unconfigured: G4/G5).
  * NO RE-EMIT / RESTART-SAFE — the last emitted kind per agent is persisted durably, so the
    same state does not re-ping every tick or after a service restart.
  * FALSE-IDLE — a `completed` candidate carrying active-execution evidence is suppressed by
    the pipeline guard (a live shell/tool run is never reported idle).
"""
from __future__ import annotations

import os
from typing import Optional

from core.control_plane import event_pipeline as _ep
from core.control_plane.api import _c
from core.control_plane.store import now_iso

# observable agent state → significant pinger kind. States absent here (working / idle /
# shell_running / stale / unknown) are NOT news and never ping.
STATE_TO_KIND = {
    "completed": "completed",
    "waiting_owner": "waiting_owner",
    "waiting_input": "waiting_owner",
    "externally_blocked": "blocker",
    "dead": "dead",
}


def _shadow_agents() -> frozenset:
    """Read the allowlist live each tick (env), so scope changes never require a reimport.
    Falls back to the actuation canary set — visibility of the pinger is confined to exactly
    the agent that actuation is confined to."""
    raw = (os.getenv("CONTROL_PLANE_PINGER_SHADOW_AGENTS", "").strip()
           or os.getenv("CONTROL_PLANE_CANARY_AGENTS", "").strip())
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def _ensure_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS pinger_shadow_state ("
                 "agent TEXT PRIMARY KEY, last_kind TEXT, last_state TEXT, updated_at TEXT)")


def _last_kind(conn, agent: str) -> Optional[str]:
    _ensure_table(conn)
    r = conn.execute("SELECT last_kind FROM pinger_shadow_state WHERE agent=?", (agent,)).fetchone()
    return r[0] if r else None


def _set_last_kind(conn, agent: str, kind: str, state: str) -> None:
    _ensure_table(conn)
    conn.execute("INSERT INTO pinger_shadow_state(agent,last_kind,last_state,updated_at) "
                 "VALUES(?,?,?,?) ON CONFLICT(agent) DO UPDATE SET last_kind=excluded.last_kind,"
                 "last_state=excluded.last_state,updated_at=excluded.updated_at",
                 (agent, kind, state, now_iso()))
    conn.commit()


def _project_of(target: str, conn) -> str:
    from core.control_plane import api
    a = api.get_agent(target, conn=conn)
    return (a or {}).get("project_id") or ""


def shadow_tick(inventory: Optional[dict] = None, *, publish_fn=None, tail_fn=None,
                conn=None) -> dict:
    """One scope-confined shadow pass. Emits a pinger event for each allowlisted agent that has
    TRANSITIONED into a significant state since the last recorded kind. Returns a summary."""
    allow = _shadow_agents()
    publish_fn = publish_fn or _ep.publish_significant_event
    conn, own = _c(conn)
    try:
        if inventory is None:
            from core import agent_control as ac
            inventory = ac.agent_list()
        agents = inventory.get("agents") or []
        emitted, suppressed = [], []
        out_of_scope_significant = 0
        for a in agents:
            target = a.get("target")
            kind = STATE_TO_KIND.get(a.get("state"))
            if not kind:
                continue                                  # not a significant state
            if target not in allow:
                out_of_scope_significant += 1             # confinement: count, never emit
                continue
            if _last_kind(conn, target) == kind:
                continue                                  # already pinged this state (no re-emit)
            tail = ""
            if kind == "completed" and tail_fn:
                try:
                    tail = tail_fn(target) or ""
                except Exception:  # noqa: BLE001
                    tail = ""
            res = publish_fn(agent=target, project=_project_of(target, conn), kind=kind,
                             observed_state=a.get("state") or "", tail=tail, conn=conn)
            if res.get("ok"):
                _set_last_kind(conn, target, kind, a.get("state") or "")
                emitted.append({"agent": target, "kind": kind, "event_id": res.get("event_id"),
                                "delivered": res.get("delivered"), "receipt": res.get("receipt"),
                                "delivery_floor": res.get("delivery_floor")})
            elif res.get("reason") == "false_idle_suppressed":
                # do NOT advance last_kind — the agent is still working, not completed
                suppressed.append(target)
        return {
            "scope": sorted(allow),
            "in_scope": True,
            "emitted": emitted,
            "emitted_count": len(emitted),
            "suppressed_false_idle": suppressed,
            "out_of_scope_significant": out_of_scope_significant,
        }
    finally:
        if own:
            conn.close()
