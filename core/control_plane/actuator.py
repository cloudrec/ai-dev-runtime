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
# Per-agent canary allowlist. Even with ENABLED on, the actuator commands ONLY targets on
# this explicit list — so a single-agent canary can never actuate any other managed agent.
# Empty ⇒ actuate NOBODY (opt-in per agent, deny-by-default).
CANARY_AGENTS = frozenset(t.strip() for t in
                          os.getenv("CONTROL_PLANE_CANARY_AGENTS", "").split(",") if t.strip())

AUTONOMOUS_SAFE = "autonomous_safe"
OWNER_APPROVAL = "owner_approval_required"
PROHIBITED = "prohibited"


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def action_hash(conversation_id: Optional[str], action_text: str, kind: str) -> str:
    return hashlib.sha256(
        f"{conversation_id or ''}\x1f{_norm(action_text)}\x1f{kind}".encode()).hexdigest()[:16]


# autonomous_safe is STRUCTURAL (2026-08-03 audit §3.1 fix): the prefix below is only the
# first hurdle — cw.is_safe_continuation additionally requires a recognised safe step
# shape (closed benign vocabulary, printable-ASCII script, no digits, not a dialog
# answer). Pre-fix, ANY continue/proceed/resume-prefixed text that dodged the English
# denylist classified autonomous_safe — verified live probes: "proceed to send 5 BTC to
# wallet X" and "resume and promote staging traffic to production" were both SAFE.
_SAFE_CONTINUATION_RE = re.compile(
    r"^\s*(continue|proceed|resume|carry on|keep going|go on|next safe step)\b", re.I)


# the exact bare context-management commands — non-destructive w.r.t. the outside world
# (they only reset/compact the agent's own chat context). Used by the context-budget
# rotation, which additionally gates on a VERIFIED checkpoint + a safe phase boundary.
_BARE_CONTEXT_CMD_RE = re.compile(r"^\s*/(clear|compact)\s*$", re.I)

# The context-rotation RESUME message is a FIXED internal template
# (core.context_budget._resume_text) with exactly two slots: a plain checkpoint
# path (restricted charset — no shell metacharacters, no spaces) and the
# registry next_step. Recognised STRUCTURALLY: the template must match end-to-end
# and the embedded step must itself pass the fail-closed continuation gate.
# Free-form text that merely mentions a checkpoint never matches.
_RESUME_TEMPLATE_RE = re.compile(
    r"^\s*resume the SAME project from the checkpoint file "
    r"(?P<path>[A-Za-z0-9._/\-]+): read it fully first, then continue with the exact "
    r"NEXT COMMAND recorded there;(?: the exact next command from the checkpoint is: "
    r"(?P<step>.*?)\.)? do not repeat work already listed as completed; never start a "
    r"duplicate agent\.\s*$")


# GROUNDED QUEUE ADVANCEMENT. Until this existed the governor advanced a stage by sending the
# project's STATIC registry string, identical for every stage — live, stages B and C were both
# nudged with "continue with the next safe canary note; append a dated line to the log", which
# is the wrong instruction for both. Advancement only worked because the agent independently
# re-read its queue; had it obeyed the delivered text it would have done the wrong work. And
# when the registry string failed the classifier the code silently fell back to a generic
# continuation, so the delivered text was never grounded in anything.
#
# This is a FIXED internal template with exactly two slots, both charset-restricted: a stage id
# and a queue path. There is no free-text slot, so the message cannot express a build, deploy,
# publish, payment or trading instruction no matter what a queue contains. The denylist still
# runs first, so a stage id containing a forbidden token is prohibited rather than sent.
_QUEUE_STAGE_TEMPLATE_RE = re.compile(
    r"^\s*continue with the durable queue stage (?P<stage>[A-Za-z0-9_.\-]+) defined in "
    r"(?P<path>[A-Za-z0-9._/\-]+): read that stage in the queue file and do exactly what it "
    r"specifies; do not start a duplicate agent\.\s*$")


def build_queue_stage_step(stage: str, queue_path: str) -> str:
    """The one place this sentence is written. Kept beside its matcher so the two cannot
    drift apart — a template whose regex no longer matches would silently fail closed and
    stall every advancement."""
    return (f"continue with the durable queue stage {stage} defined in {queue_path}: "
            f"read that stage in the queue file and do exactly what it specifies; "
            f"do not start a duplicate agent.")


# An OWNER OS TASK is not a project queue stage. Delivering one with the queue-stage wording
# made the canary refuse it as "out-of-band" — correctly, since its CLAUDE.md names a
# different authoritative queue. Same closed form, two charset-restricted slots, distinct and
# honest wording.
_OWNER_TASK_TEMPLATE_RE = re.compile(
    r"^\s*run owner os task (?P<task>[A-Za-z0-9_.\-]+) recorded in (?P<path>[A-Za-z0-9._/\-]+): "
    r"read that file and do exactly what it specifies, then stop; "
    r"do not start a duplicate agent\.\s*$")


def build_owner_task_step(task_id: str, task_path: str) -> str:
    return (f"run owner os task {task_id} recorded in {task_path}: "
            f"read that file and do exactly what it specifies, then stop; "
            f"do not start a duplicate agent.")


def classify_action(action_text: str) -> str:
    """Machine-readable policy class. FAIL-CLOSED: destructive/live/payment/credential/
    publication (English tokens or Russian stems) → prohibited; an exact bare
    /clear|/compact (context management) or a RECOGNISED safe step shape (continuation
    prefix + closed benign vocabulary, printable-ASCII only, no digits) → autonomous_safe;
    ANYTHING else — including any unknown-script text the denylist cannot evaluate —
    → owner_approval_required (never auto-actuated)."""
    if cw._FORBIDDEN_RE.search(action_text or ""):
        return PROHIBITED
    if _BARE_CONTEXT_CMD_RE.match(action_text or ""):
        return AUTONOMOUS_SAFE
    m = _RESUME_TEMPLATE_RE.match(action_text or "")
    if m:
        step = (m.group("step") or "").strip()
        if not step or cw.is_safe_continuation(step):
            return AUTONOMOUS_SAFE
        return OWNER_APPROVAL          # template with an unrecognised step → owner gate
    if _OWNER_TASK_TEMPLATE_RE.match(action_text or ""):
        return AUTONOMOUS_SAFE
    if _QUEUE_STAGE_TEMPLATE_RE.match(action_text or ""):
        # Every slot is charset-restricted and the denylist ran first, so nothing harmful
        # can have reached here through the stage id or the path.
        return AUTONOMOUS_SAFE
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
    if target not in CANARY_AGENTS:
        # deny-by-default: only explicitly allowlisted canary agents may be actuated
        return {"acted": False, "reason": "not_canary"}

    resource = f"agent:{target}"
    # 1) lease + fence guard (restart-safe single-owner)
    if not lease or not cp.lease_is_current(resource, lease.get("lease_id"),
                                            lease.get("fence_token")):
        return {"acted": False, "reason": "stale_or_no_lease", "blocked": True}

    # 2) policy gate (deny-by-default). ALWAYS recomputed from the action text — a
    # caller-supplied policy_class may only DOWNGRADE (make stricter), never bypass the
    # classifier (pre-2026-08-03 hole: policy_class="autonomous_safe" skipped the gate).
    pc = classify_action(action_text)
    if policy_class and policy_class != AUTONOMOUS_SAFE:
        pc = policy_class
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

    ctrl = ctrl or cw.Controller()
    cwd = cwd or (cp.get_agent(target) or {}).get("cwd") or ""
    from core.control_plane import state_estimator as se
    pre = ctrl.snapshot(target, cwd)

    # 3) idempotency — never re-issue a verified action. EXCEPTION: if this exact text is
    #    STILL SITTING QUEUED in the input line, the prior "verified" record is provably
    #    wrong (2026-08-04: the old verifier accepted text that merely appeared on screen,
    #    so a never-executed step was recorded verified — and that stale record then
    #    blocked the recovery of the very line it mis-recorded). Submitting a line that is
    #    already typed cannot duplicate anything: it is one Enter on existing text.
    prior = _get_action(idkey)
    if prior and prior["verified"]:
        if not cw.text_is_queued(pre, action_text):
            return {"acted": False, "reason": "already_verified", "idempotent": True}
        emit("actuator", "verified_record_contradicted", agent_id=target, severity="warn",
             payload={"note": "action recorded verified but its text is still queued in "
                              "the input line — submitting the existing line",
                      "action_tip": action_text[:120]},
             action_taken="re-submitting a queued line recorded as verified",
             dedup_key=f"requeued:{idkey}")

    # 3b) FALSE-IDLE GUARD — never command a truly-working agent. Re-read the pane and,
    # if active-execution evidence is present (spinner timer / thinking / tokens / esc), or
    # the agent is working/shell_running, refuse and record a correlated correction event.
    if se.has_active_marker(pre.get("tail") or "") or pre.get("state") in ("working", "shell_running"):
        emit("actuator", "false_idle_corrected", agent_id=target, severity="info",
             payload={"observed_state": pre.get("state"), "tail_tip": (pre.get("tail") or "")[-120:],
                      "note": "agent is actively working — continuation suppressed"},
             action_taken="suppressed continuation (target working)",
             dedup_key=f"falseidle:{idkey}")
        return {"acted": False, "reason": "target_working", "false_idle_corrected": True}
    # 3b2) DIALOG GUARD (fail-closed, RU/EN) — never act on a pane showing a system/
    # tool-permission or confirmation dialog, or classified waiting_owner: pasting or
    # pressing Enter there ANSWERS the dialog. Detection unavailable ⇒ treated as a
    # dialog (refuse), never as clear.
    dialog_sig = "dialog_detection_unavailable"
    try:
        from core import agent_control as _ac
        dialog_sig = _ac.dialog_signature(pre.get("tail") or "")
    except Exception:  # noqa: BLE001
        pass
    if dialog_sig or pre.get("state") == "waiting_owner":
        emit("actuator", "action_deferred_dialog_open", agent_id=target, severity="info",
             payload={"dialog": dialog_sig, "observed_state": pre.get("state"),
                      "note": "pane is awaiting a HUMAN dialog answer — never auto-answered"},
             action_taken="refused (dialog open)", dedup_key=f"dialoggate:{idkey}")
        return {"acted": False, "reason": "dialog_open", "dialog": dialog_sig}

    # 3b3) UNOBSERVABLE-PANE GUARD (M2 closeout, 2026-08-04). `tmux capture-pane` failing
    # and a genuinely blank pane both produced tail="" — indistinguishable, so an earlier
    # attempt to refuse on "empty tail" turned 15 established clean-pane contracts into
    # refusals. The Controller now reports `capture_ok` explicitly (agent_control.
    # pane_capture), so the failure is a FACT, not an inference. Refuse when the capture
    # failed, and refuse a snapshot that carries no observation at all (no capture flag,
    # no tail, no pending, no state) — acting there is a blind paste onto a pane whose
    # contents, dialog or otherwise, are unknown. Ordered AFTER the dialog gate so an
    # explicit waiting_owner snapshot keeps its own reason, and BEFORE any keystroke.
    _unobservable = None
    if pre.get("capture_ok") is False:
        _unobservable = "capture_failed"
    elif ("capture_ok" not in pre and not (pre.get("tail") or "").strip()
            and not (pre.get("pending") or "").strip() and not pre.get("state")):
        _unobservable = "empty_snapshot"
    if _unobservable:
        emit("actuator", "action_deferred_unobservable_pane", agent_id=target,
             severity="info",
             payload={"why": _unobservable, "observed_state": pre.get("state"),
                      "note": "pane could not be read — tail-based guards (dialog, "
                              "false-idle, pending) cannot be evaluated"},
             action_taken="refused (unobservable pane)", dedup_key=f"blindpane:{idkey}")
        return {"acted": False, "reason": "unobservable_pane", "why": _unobservable}

    # 3c) PENDING-INPUT GUARD — never paste onto a NON-EMPTY input line. agent_send does
    # not clear the line, so DIFFERENT queued (never safety-classified) text and this
    # action would CONCATENATE and submit as one command → refuse. When the pending line
    # IS this exact action (the original missed-Enter failure), SUBMIT it instead of
    # pasting a duplicate copy.
    pending = (pre.get("pending") or "").strip()
    action_mode = "deliver"
    if pending:
        if cw._norm(pending) != cw._norm(action_text):
            emit("actuator", "action_deferred_pending_input", agent_id=target, severity="info",
                 payload={"pending_tip": pending[:120],
                          "note": "input line occupied by different text — paste would "
                                  "concatenate; deferred"},
                 action_taken="deferred (pending input present)",
                 dedup_key=f"pendguard:{idkey}")
            return {"acted": False, "reason": "pending_input_present"}
        action_mode = "submit"        # same text already typed — press Enter, don't re-paste

    # 4) verified delivery (folded from the continuation watchdog)
    out = cw.deliver_and_verify(ctrl, target=target, cwd=cwd, action=action_mode,
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
