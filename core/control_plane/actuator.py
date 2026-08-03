"""Canonical lease-gated Actuator (P2).

The SINGLE path that may command an agent pane. Every command goes through:

  1. LEASE + FENCE guard — the caller must hold the current lease for `agent:<target>`
     with the current monotonic fence token. A stale fence (e.g. a queued/retried action
     from before a service restart, after which the controller re-acquired at a higher
     fence) is REJECTED → no duplicate command. This is the restart-no-duplicate guarantee.
  2. POLICY gate — autonomous_safe proceeds; prohibited / owner_approval_required are
     blocked and raise a correlated owner gate. Deny-by-default.
  3. IDEMPOTENCY — keyed by (target, conversation_id, action_hash); a verified action is
     never re-issued.
  4. VERIFIED DELIVERY — folded from the accepted continuation watchdog: submitted +
     pane_changed + prompt_consumed + conversation_modified + state_transitioned, with one
     robust clear+paste+Enter retry, else a durable blocker + owner gate.

Gated behind CONTROL_PLANE_ACTUATOR_ENABLED (default OFF). P2 is shadow/canary only — it
does NOT cut over the legacy controllers (that is P4, owner-gated G1). Until the flag is
on, `actuate()` is a no-op.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Optional

from core.control_plane import api as cp
from core.control_plane.cto import emit
from core.control_plane.store import connect, init_db, now_iso
from core import agent_continuation_watchdog as cw

ENABLED = os.getenv("CONTROL_PLANE_ACTUATOR_ENABLED", "0") not in ("0", "false", "no", "")

AUTONOMOUS_SAFE = "autonomous_safe"
OWNER_APPROVAL = "owner_approval_required"
PROHIBITED = "prohibited"


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def action_hash(conversation_id: Optional[str], action_text: str, kind: str) -> str:
    return hashlib.sha256(
        f"{conversation_id or ''}\x1f{_norm(action_text)}\x1f{kind}".encode()).hexdigest()[:16]


# autonomous_safe is NARROW by design (deny-by-default): only a documented continuation
# meta-instruction. Arbitrary free-form text is owner_approval, never auto-actuated.
_SAFE_CONTINUATION_RE = re.compile(
    r"^\s*(continue|proceed|resume|carry on|keep going|go on|next safe step)\b", re.I)


def classify_action(action_text: str) -> str:
    """Machine-readable policy class. Deny-by-default: destructive/live/payment/credential/
    publication → prohibited; a documented safe continuation → autonomous_safe; ANYTHING
    else → owner_approval_required (never auto-actuated)."""
    if cw._FORBIDDEN_RE.search(action_text or ""):
        return PROHIBITED
    if cw.is_safe_continuation(action_text) and _SAFE_CONTINUATION_RE.search(action_text or ""):
        return AUTONOMOUS_SAFE
    return OWNER_APPROVAL


# ── durable action ledger ────────────────────────────────────────────────────
def _get_action(idkey: str, conn=None):
    own = conn is None
    conn = conn or (init_db(connect()))
    try:
        r = conn.execute("SELECT attempts,verified,blocked,outcome,fence_token FROM cp_action "
                         "WHERE idkey=?", (idkey,)).fetchone()
        if not r:
            return None
        return {"attempts": r[0], "verified": bool(r[1]), "blocked": bool(r[2]),
                "outcome": r[3], "fence_token": r[4]}
    finally:
        if own:
            conn.close()


def _save_action(idkey, target, conv, ah, controller, lease, kind, policy_class, *,
                 attempts, submitted, verified, blocked, outcome, conn=None):
    own = conn is None
    conn = conn or (init_db(connect()))
    try:
        now = now_iso()
        exists = conn.execute("SELECT 1 FROM cp_action WHERE idkey=?", (idkey,)).fetchone()
        if exists:
            conn.execute("UPDATE cp_action SET attempts=?,submitted=?,verified=?,blocked=?,"
                         "outcome=?,lease_id=?,fence_token=?,updated_at=? WHERE idkey=?",
                         (attempts, int(submitted), int(verified), int(blocked), outcome,
                          lease["lease_id"], lease["fence_token"], now, idkey))
        else:
            conn.execute(
                "INSERT INTO cp_action(idkey,target,conversation_id,action_hash,controller,"
                "lease_id,fence_token,kind,policy_class,submitted,verified,blocked,attempts,"
                "outcome,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (idkey, target, conv, ah, controller, lease["lease_id"], lease["fence_token"],
                 kind, policy_class, int(submitted), int(verified), int(blocked), attempts,
                 outcome, now, now))
        conn.commit()
    finally:
        if own:
            conn.close()


def actuate(*, target: str, action_text: str, controller: str, conversation_id: str = "",
            kind: str = "continuation", lease: Optional[dict] = None, cwd: str = "",
            policy_class: Optional[str] = None, ctrl=None, sleep=time.sleep) -> dict:
    """Issue ONE verified command to `target` under the caller's lease. Returns a result
    dict; never raises for control-flow. No pane is touched unless ENABLED, the lease is
    current, and policy is autonomous_safe."""
    if not ENABLED:
        return {"acted": False, "reason": "actuator_disabled"}

    resource = f"agent:{target}"
    # 1) lease + fence guard (restart-safe single-owner)
    if not lease or not cp.lease_is_current(resource, lease.get("lease_id"),
                                            lease.get("fence_token")):
        return {"acted": False, "reason": "stale_or_no_lease", "blocked": True}

    # 2) policy gate (deny-by-default)
    pc = policy_class or classify_action(action_text)
    ah = action_hash(conversation_id, action_text, kind)
    idkey = f"{target}|{conversation_id}|{ah}"
    if pc != AUTONOMOUS_SAFE:
        g = cp.open_gate(agent_id=target, reason=f"{pc}: {action_text[:120]}", kind=pc,
                         correlation_id=f"act:{idkey}")
        emit("actuator", "action_blocked", agent_id=target, severity="high",
             owner_action_required=True, payload={"policy_class": pc, "gate_id": g["id"]},
             action_taken="BLOCKED — owner gate opened", dedup_key=f"actblock:{idkey}")
        _save_action(idkey, target, conversation_id, ah, controller, lease, kind, pc,
                     attempts=0, submitted=False, verified=False, blocked=True,
                     outcome=f"blocked:{pc}")
        return {"acted": False, "reason": pc, "blocked": True, "gate_id": g["id"]}

    # 3) idempotency — never re-issue a verified action
    prior = _get_action(idkey)
    if prior and prior["verified"]:
        return {"acted": False, "reason": "already_verified", "idempotent": True}

    # 3b) FALSE-IDLE GUARD — never command a truly-working agent. Re-read the pane and,
    # if active-execution evidence is present (spinner timer / thinking / tokens / esc), or
    # the agent is working/shell_running, refuse and record a correlated correction event.
    ctrl = ctrl or cw.Controller()
    cwd = cwd or (cp.get_agent(target) or {}).get("cwd") or ""
    from core.control_plane import state_estimator as se
    pre = ctrl.snapshot(target, cwd)
    if se.has_active_marker(pre.get("tail") or "") or pre.get("state") in ("working", "shell_running"):
        emit("actuator", "false_idle_corrected", agent_id=target, severity="info",
             payload={"observed_state": pre.get("state"), "tail_tip": (pre.get("tail") or "")[-120:],
                      "note": "agent is actively working — continuation suppressed"},
             action_taken="suppressed continuation (target working)",
             dedup_key=f"falseidle:{idkey}")
        return {"acted": False, "reason": "target_working", "false_idle_corrected": True}

    # 4) verified delivery (folded from the continuation watchdog)
    out = cw.deliver_and_verify(ctrl, target=target, cwd=cwd, action="deliver",
                                step_text=action_text, expected_pending=action_text,
                                sleep=sleep)
    v = out.get("verify") or {}
    # re-assert the fence AFTER delivery — if a restart re-leased mid-action, do not record
    # this as our success (the new holder owns the agent now).
    still_ours = cp.lease_is_current(resource, lease.get("lease_id"), lease.get("fence_token"))
    attempts = (prior["attempts"] if prior else 0) + 1 + (1 if out.get("retried") else 0)

    if v.get("ok") and still_ours:
        _save_action(idkey, target, conversation_id, ah, controller, lease, kind, pc,
                     attempts=attempts, submitted=True, verified=True, blocked=False,
                     outcome="verified")
        cp.set_agent_state(target, "working", controller=controller,
                           evidence_ref=f"actuate:{ah}", conversation_id=conversation_id,
                           last_action=action_text[:120])
        cp.record_decision("agent", target, pc, "actuate", f"verified continuation via {controller}")
        ok_ev = emit("actuator", "action_verified", agent_id=target, severity="info",
                     payload={"kind": kind, "retried": out.get("retried"), "verify": v},
                     action_taken="delivered + verified", dedup_key=f"actok:{idkey}")
        # if this action was blocked before and now verifies, clear the blocker + emit a
        # correlated resolution event (the all-clear, not just the alarm).
        if prior and prior.get("blocked"):
            from core.control_plane import resolutions
            resolutions.resolve_blocker(target, reason="continuation verified after prior block",
                                        resolves=ok_ev["event_id"], correlation_id=f"act:{idkey}")
        return {"acted": True, "verified": True, "retried": out.get("retried"), "verify": v}

    if not still_ours:
        return {"acted": False, "reason": "lease_lost_midaction", "verify": v}

    # verify failed → durable blocker + owner gate
    _save_action(idkey, target, conversation_id, ah, controller, lease, kind, pc,
                 attempts=attempts, submitted=bool(v.get("submitted")), verified=False,
                 blocked=True, outcome="verify_failed")
    g = cp.open_gate(agent_id=target, reason="continuation not verified after retry",
                     kind="actuation_failed", correlation_id=f"act:{idkey}")
    emit("actuator", "action_blocked", agent_id=target, severity="high",
         owner_action_required=True, payload={"verify": v, "gate_id": g["id"]},
         action_taken="BLOCKED — not verified; owner gate opened", dedup_key=f"actfail:{idkey}")
    return {"acted": False, "reason": "not_verified", "blocked": True, "verify": v}
