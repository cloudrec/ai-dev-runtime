"""Significant-event pipeline — the same-chat pinger producer (P-pinger).

ONE entry point turns a significant agent transition into the full owner-notification
contract, honestly:

  * a correlated CTO **event id** (the durable, append-only CTO-inbox record — never lost,
    consumed on the next CTO invocation);
  * agent + project + a concise **factual** summary (no speculation);
  * a **delivery attempt** through the fail-closed capability matrix (`delivery.deliver`),
    **retried once** on failure;
  * **receipt** evidence on a proven proactive send, or an honest `cto_inbox` floor when NO
    proactive channel is available — success is NEVER claimed from queued text or email;
  * **dedupe** (same agent+kind collapses within a window, in BOTH stores);
  * a mirror into the legacy `commander_events` log — the only channel that currently reaches
    an owner surface (drained + ack'd by the seo-backend `agent_notifier`); its ack is the
    real delivery receipt, verified out-of-band via `commander_delivery_report`.

Emit/observe only: this NEVER touches a pane, starts/stops nothing, and performs no external
or destructive action.

FALSE-IDLE INVARIANT (owner-mandated): a live shell/tool run must never be classified idle. An
`idle`/`completed` event carrying active-execution evidence (spinner timer / `· thinking` /
token counter / `esc to interrupt` / `· N shell ·`, or an observed state of working/
shell_running) is REFUSED and a correlated `false_idle_corrected` event is recorded instead.
"""
from __future__ import annotations

from typing import Optional

from core.control_plane import cto
from core.control_plane import delivery as _delivery
from core.control_plane import state_estimator as _se
from core.control_plane.api import enqueue_notification, get_notification, _c

# kind → (severity, owner_action_required, default factual summary, legacy commander type)
SIGNIFICANT_KINDS = {
    "completed":     ("info",     False, "finished its run and went idle",       "agent_completed"),
    "waiting_owner": ("high",     True,  "is waiting for an owner decision",     "agent_waiting_input"),
    "failure":       ("critical", True,  "failed during its run",                "agent_process_failed"),
    "dead":          ("high",     True,  "process died unexpectedly",            "agent_process_failed"),
    "blocker":       ("high",     True,  "hit a significant blocker",            "agent_externally_blocked"),
}
# kinds whose meaning is "not working anymore" — subject to the false-idle guard.
_IDLE_GUARDED = ("completed", "idle")


def _factual_summary(agent: str, project: str, kind: str, override: str, evidence: Optional[dict]) -> str:
    base = override.strip() if override else SIGNIFICANT_KINDS.get(kind, ("", "", "", ""))[2]
    proj = f" [{project}]" if project else ""
    line = f"{agent}{proj}: {base}"
    if evidence:
        tip = evidence.get("summary") or evidence.get("last_line") or ""
        if tip:
            line += f" — {str(tip)[:160]}"
    return line[:400]


def publish_significant_event(*, agent: str, project: str = "", kind: str, summary: str = "",
                              evidence: Optional[dict] = None, observed_state: str = "",
                              tail: str = "", dedup_key: str = "", dedup_window_secs: int = 900,
                              mirror_commander: bool = True, deliver_fn=None, conn=None) -> dict:
    """Publish one significant agent event with the full owner-notification contract. Returns a
    receipt dict. Idempotent per (agent, kind, dedup_key) within the window. Never actuates."""
    spec = SIGNIFICANT_KINDS.get(kind)
    if spec is None:
        return {"ok": False, "reason": "unknown_kind", "kind": kind}
    severity, owner_action, _default_sum, commander_type = spec

    conn, own = _c(conn)
    try:
        # 1) FALSE-IDLE GUARD — a live shell/tool run is never idle/completed.
        if kind in _IDLE_GUARDED and (_se.has_active_marker(tail)
                                      or observed_state in ("working", "shell_running")):
            corr = f"sig:{agent}:{kind}"
            ev = cto.emit("event_pipeline", "false_idle_corrected", agent_id=agent,
                          project_id=project, severity="info", owner_action_required=False,
                          payload={"observed_state": observed_state, "tail_tip": (tail or "")[-120:],
                                   "note": "active execution evidence — NOT idle; event suppressed"},
                          action_taken="suppressed idle/completed (agent is working)",
                          correlation_id=corr, dedup_key=f"falseidle:{corr}",
                          dedup_window_secs=dedup_window_secs, push=False, conn=conn)
            return {"ok": False, "reason": "false_idle_suppressed", "false_idle_corrected": True,
                    "event_id": ev["event_id"], "agent": agent, "kind": kind}

        # 1b) ACCESS-RECOVERY RECLASSIFY — a server-access failure by an agent that already has
        #     installed keys (owner truth) is INTERNAL key-selection/connection-mapping recovery,
        #     NOT an owner gate. Reclassify + track it; do NOT repeatedly notify the owner. Only
        #     an exhaustive absence/revocation proof escalates.
        if kind in ("blocker", "waiting_owner"):
            from core.control_plane import access_recovery as _ar
            blob = " ".join(str(x) for x in (summary, tail,
                            (evidence or {}).get("summary"), (evidence or {}).get("last_line"),
                            (evidence or {}).get("detail")) if x)
            _cls = _ar.classify(agent, blob)
            if _cls["class"] == "internal_recovery":
                task = _ar.note_recovery(agent, host=_cls.get("host") or "", detail=blob, conn=conn)
                return {"ok": False, "reason": "reclassified_internal_recovery",
                        "agent": agent, "kind": kind, "host": _cls.get("host"),
                        "recovery_task": task, "owner_notified": False}
            # _cls == 'escalate' (exhaustive proof) falls through → normal owner-actionable event.

        # 2) durable CTO inbox record (correlated event id). emit auto-pushes high/critical/owner.
        dk = dedup_key or f"sig:{agent}:{kind}"
        line = _factual_summary(agent, project, kind, summary, evidence)
        corr = f"sig:{agent}:{kind}"
        emitted = cto.emit("event_pipeline", kind, agent_id=agent, project_id=project,
                           severity=severity, owner_action_required=owner_action,
                           payload={"agent": agent, "project": project, "kind": kind,
                                    "summary": line, "evidence": evidence or {}},
                           action_taken=line, correlation_id=corr, dedup_key=dk,
                           dedup_window_secs=dedup_window_secs, conn=conn)
        eid = emitted["event_id"]

        # 3) ensure a notification exists (even for info 'completed' — every significant event
        #    gets a delivery attempt), then attempt delivery with ONE retry on failure.
        notif = emitted.get("notification") or enqueue_notification(
            event_id=eid, channel="owner_push", dedup_key=dk, correlation_id=corr, conn=conn)
        nid = notif["id"]
        deduped = bool(notif.get("deduped"))

        dfn = deliver_fn or _delivery.deliver
        attempts = []
        res = dfn(nid, severity=severity, conn=conn)
        attempts.extend(res.get("attempts", []))
        retried = False
        if not res.get("delivered"):
            retried = True                    # retry once — same-tick, deterministic
            res = dfn(nid, severity=severity, conn=conn)
            attempts.extend(res.get("attempts", []))

        delivered = bool(res.get("delivered"))
        # receipt only from a PROVEN proactive send; otherwise honest durable-inbox floor.
        receipt = None
        if delivered:
            n = get_notification(nid, conn=conn)
            receipt = (n or {}).get("receipt")

        # 4) mirror to the legacy commander_events log (the live owner surface). Its ack is the
        #    real same-chat receipt, verified out-of-band (commander_delivery_report).
        commander_recorded = None
        if mirror_commander:
            try:
                from core import agent_control as ac
                commander_recorded = ac.record_commander_event(
                    agent, project, commander_type,
                    {"summary": line, "correlated_event_id": eid, "severity": severity,
                     "owner_action_required": owner_action, "evidence": evidence or {}},
                    dedup_key=dk, dedup_window_secs=dedup_window_secs)
            except Exception as e:  # noqa: BLE001 — mirror is best-effort, never blocks the record
                commander_recorded = f"error:{e}"

        return {
            "ok": True,
            "event_id": eid,
            "correlation_id": corr,
            "agent": agent,
            "project": project,
            "kind": kind,
            "severity": severity,
            "owner_action_required": owner_action,
            "summary": line,
            "notification_id": nid,
            "deduped": deduped,
            "delivered": delivered,                 # proactive send proven?
            "receipt": receipt,                     # None unless a real proactive receipt exists
            "delivery_floor": (None if delivered else "cto_inbox"),
            "attempts": attempts,
            "retried": retried,
            "inbox_recorded": True,                 # durable CTO event always written
            "commander_event_recorded": commander_recorded,
            "blocker": res.get("blocker") if not delivered else None,
        }
    finally:
        if own:
            conn.close()
