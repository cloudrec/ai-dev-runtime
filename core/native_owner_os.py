"""Native Owner OS controller loop (DISABLED BY DEFAULT: NATIVE_OWNER_OS_ENABLED=0).

Wires the native primitives into the runtime. Each tick:
  1. state: `native_agents.list_sessions()` (`claude agents --json`, labelled fallback);
  2. goals: only when NATIVE_GOALS_ENABLED=1, reconcile the EXPLICIT `config/native_goals.yaml`
     entries with `native_goals.install` (idempotent, latched, idle-only, payorch hard-denied);
  3. CTO events: read control-plane event deltas through the existing durable `cto_cursor`
     (consumer `native_owner_os`), map explicit reasoning types directly and
     `owner_action_required` → `owner_needed`, hand each to `cto_bridge.handle_event`, and
     ack the cursor only AFTER that event was durably handled (a failure stops the batch
     without acking, so it is re-read next tick and deduped by its event key);
  4. retry decisions left `waiting_provider` / `provider_error` once a provider is configured;
  5. delivery: OWNER_OS_CTO_DELIVERY_MODE=agent_control → server-side fallback delivery of
     pending advice into idle sessions; the default `native_controller` leaves the outbox for a
     controller Claude session (SendMessage); `off` delivers nothing.

First start never replays history: with no cursor row the cursor is set to the bus head.
`NATIVE_OWNER_OS_AGENTS` (canary scope) limits CTO event handling to the named agents.
The loop never starts, restarts or creates an agent, and never sends routine "continue".
"""
from __future__ import annotations

import os
from typing import Optional

from core import cto_bridge as cb

CONSUMER = "native_owner_os"
INTERVAL = int(os.getenv("NATIVE_OWNER_OS_INTERVAL_SECS", "60"))
BATCH = 200
# events this stack emits itself are never fed back into it (no self-loops)
_OWN_SOURCES = frozenset({cb.SOURCE, CONSUMER, "native_goals"})
OWNER_NEEDED_TYPE = "owner_needed_by_cto"


def enabled() -> bool:
    return os.getenv("NATIVE_OWNER_OS_ENABLED", "0").strip().lower() in ("1", "true", "yes")


def agent_scope() -> set:
    """NATIVE_OWNER_OS_AGENTS: comma list of tmux sessions / targets. Set = only events for
    these agents reach the CTO bridge (others are acked and ignored). Empty = all agents."""
    raw = os.getenv("NATIVE_OWNER_OS_AGENTS", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def in_scope(agent: str) -> bool:
    sc = agent_scope()
    a = (agent or "").strip()
    return not sc or a in sc or a.split(":", 1)[0] in sc


def map_event(e: dict) -> Optional[str]:
    if e.get("source") in _OWN_SOURCES:
        return None
    if not in_scope(e.get("agent_id") or ""):
        return None
    if cb.is_controller(e.get("agent_id") or ""):
        return None              # the CTO's own session is never reviewed by itself
    if e.get("type") in cb.REASONING_EVENTS:
        return e["type"]
    if e.get("owner_action_required"):
        return "owner_needed"
    return None


def _question(e: dict) -> str:
    p = e.get("payload") or {}
    if isinstance(p, dict):
        for k in ("question", "reason", "summary", "message", "detail"):
            if isinstance(p.get(k), str) and p[k].strip():
                return p[k]
    return str(e.get("type") or "")


def _emit_owner(ctx: dict, why: str) -> None:
    from core.control_plane import cto
    cto.emit(cb.SOURCE, OWNER_NEEDED_TYPE, project_id=ctx.get("project") or "",
             agent_id=ctx.get("agent") or "", severity="high", owner_action_required=True,
             payload={"reason": why[:300], "event_type": ctx.get("event_type")},
             dedup_key=f"cto:owner:{ctx.get('project')}:{ctx.get('agent')}:{why[:80]}",
             dedup_window_secs=21600)


def _init_cursor(conn) -> bool:
    """No cursor row yet → start at the bus head (never replay history)."""
    from core.control_plane import cto
    if conn.execute("SELECT 1 FROM cto_cursor WHERE consumer=?", (CONSUMER,)).fetchone():
        return False
    head = conn.execute("SELECT COALESCE(MAX(id),0) FROM event").fetchone()[0]
    cto.set_cursor(CONSUMER, head, conn=conn)
    return True


def run_once(*, listing: Optional[dict] = None, provider=None, gate: str = "",
             conn=None) -> dict:
    from core import native_agents as na
    from core import native_goals as ng
    from core.control_plane import cto
    from core.control_plane.api import _c
    summary = {"enabled": True, "source": None, "goals": [], "events": 0, "handled": 0,
               "acked_to": None, "retried": 0, "delivered": [], "errors": 0}
    if listing is None:
        listing = na.list_sessions()
    summary["source"] = listing.get("source")
    if ng.enabled():
        for name in ng.load_config():
            try:
                r = ng.install(name, listing=listing, conn=conn)
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "reason": f"error:{type(e).__name__}"}
                summary["errors"] += 1
            summary["goals"].append({"session": name, "reason": r.get("reason")})
    if provider is None and not gate:
        provider, gate = cb.provider_from_env()
    conn, own = _c(conn)
    try:
        if _init_cursor(conn):
            summary["cursor_initialised"] = True
        else:
            brief = cto.cto_brief_since(CONSUMER, limit=BATCH, conn=conn)
            for e in brief["events"]:
                summary["events"] += 1
                kind = map_event(e)
                try:
                    if kind:
                        r = cb.handle_event({
                            "key": f"cp:{e['event_id']}", "type": kind,
                            "project": e.get("project_id") or "",
                            "agent": e.get("agent_id") or "",
                            "state": (na.find(e.get("agent_id") or "", listing) or {})
                            .get("state") or "", "question": _question(e)},
                            provider=provider, gate=gate, emit_owner=_emit_owner, conn=conn)
                        summary["handled"] += 1 if r.get("handled") else 0
                except Exception as exc:  # noqa: BLE001
                    summary["errors"] += 1
                    # A poison event must not wedge the bus: durably record the failure
                    # (that IS its handling) and move on. If even the record cannot be
                    # written, stop WITHOUT acking so the event is re-read next tick.
                    try:
                        conn.rollback()
                        cb._schema(conn)
                        conn.execute(
                            "INSERT OR IGNORE INTO cto_decision (event_key,event_type,project,"
                            "agent,status,error,latched_at) VALUES (?,?,?,?,?,?,?)",
                            (f"cp:{e['event_id']}", kind, e.get("project_id") or "",
                             e.get("agent_id") or "", "handler_error",
                             type(exc).__name__, e.get("ts") or ""))
                        conn.commit()
                    except Exception:  # noqa: BLE001
                        break
                cto.ack_through(CONSUMER, e["event_id"], conn=conn)
                summary["acked_to"] = e["event_id"]
        # later stages are independent: one failing must not take the tick down with it
        if provider is not None:
            try:
                summary["retried"] = len(cb.retry_waiting(provider=provider, gate=gate,
                                                          emit_owner=_emit_owner, conn=conn))
            except Exception:  # noqa: BLE001
                summary["errors"] += 1
        try:
            summary["delivered"] = cb.deliver_pending_agent_control(listing, conn=conn)
        except Exception:  # noqa: BLE001
            summary["errors"] += 1
    finally:
        if own:
            conn.close()
    return summary


async def run_loop() -> None:
    import asyncio
    import logging
    log = logging.getLogger(CONSUMER)
    if not enabled():
        log.info("native owner os controller disabled")
        return
    log.info(f"native owner os controller started (interval {INTERVAL}s)")
    while True:
        try:
            res = await asyncio.to_thread(run_once)
            if res.get("handled") or res.get("errors") or res.get("delivered"):
                log.info(f"native owner os: {res}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"native owner os tick error: {e}")
        await asyncio.sleep(INTERVAL)
