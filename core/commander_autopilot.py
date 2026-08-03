"""Commander Autopilot — production internal autonomy for critical projects.

Goal (owner priority): eliminate manual pokes. Each tick the autopilot evaluates every
registered critical agent — pane state, unfinished tasks, background subagents, and last proven
progress — and, when an agent is idle/waiting with documented pre-approved SAFE unfinished work,
delivers the exact next step to the EXISTING agent and confirms the transition to working, with
no duplicate. This runs independently of any ability to message ChatGPT (internal autonomy).

SAFETY MODEL (defence in depth):
  1. The next-step text must classify `autonomous_safe` — deny-by-default on destructive / live /
     payment / trading / traffic-or-DB promotion / credential / publication / push / deploy / ssh
     (core.control_plane.actuator.classify_action → core.agent_continuation_watchdog._FORBIDDEN_RE).
  2. Delivery is routed through the canonical lease-gated Actuator, which is CONFINED to
     CONTROL_PLANE_CANARY_AGENTS. An agent not in that allowlist is EVALUATED ONLY (read-only);
     enabling its live actuation is an explicit owner gate. So no scope expansion happens here.
  3. The Actuator adds a lease + monotonic fence (restart-safe, no duplicate), a false-idle guard
     (a working/shell/subagent pane is never poked), and idempotency (a verified action is never
     re-issued).

A background Fable/subagent counts as WORKING (never poked). Watchdogs flag a stuck shell, an
agent death (never creates a duplicate), and a false completion claimed on a single report.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

_CFG_PATH = os.getenv("COMMANDER_AUTOPILOT_CONFIG",
                      "/root/ai-dev-runtime/config/commander_autopilot.yaml")

# an agent in one of these observable states, with unfinished work, is a poke candidate.
POKE_STATES = ("idle", "waiting_input", "waiting_owner")
# these observable states mean the agent is already making progress → never poke.
PROGRESS_STATES = ("working", "shell_running")

# a background subagent that is RUNNING = work in progress (owner: count as working).
_SUBAGENT_RUNNING_RE = re.compile(
    r"((sub[- ]?agent|fable|background (task|agent|shell|job))\b.{0,40}"
    r"(running|active|in[- ]progress|working|spawned)"
    r"|·\s*\d+\s+agents?\s+(running|working|active)"
    r"|spawn(ed|ing) \d+ (sub)?agents?)", re.I)
# Claude Code task footer: "N tasks (X done, Y in progress, Z open)".
_TASK_FOOTER_RE = re.compile(
    r"(\d+)\s+tasks?\s*\((\d+)\s+done,\s*(\d+)\s+in progress,\s*(\d+)\s+open\)", re.I)


def load_registry(path: str = None) -> dict:
    """Load the persistent critical-project registry. Pure read of the yaml config."""
    import yaml
    path = path or _CFG_PATH
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("agents", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def parse_task_footer(tail: str) -> dict:
    """Parse the Claude Code task footer. Returns counts + has_unfinished (open or in-progress)."""
    m = _TASK_FOOTER_RE.search(tail or "")
    if not m:
        return {"present": False, "done": 0, "in_progress": 0, "open": 0, "has_unfinished": None}
    done, inprog, opn = int(m.group(2)), int(m.group(3)), int(m.group(4))
    return {"present": True, "done": done, "in_progress": inprog, "open": opn,
            "has_unfinished": (opn + inprog) > 0}


def has_background_subagent(tail: str) -> bool:
    return bool(_SUBAGENT_RUNNING_RE.search(tail or ""))


def is_progressing(state: str, tail: str) -> bool:
    """True when the agent is already doing real work — including a running background subagent
    or a live active-execution marker — so it must NOT be poked."""
    from core.agent_continuation_watchdog import _ACTIVE_EXEC_RE
    if state in PROGRESS_STATES:
        return True
    if has_background_subagent(tail):
        return True
    # active-execution markers only count in the CURRENT status (tail tip), never deep
    # scrollback — a past "running…" line must not read as live work.
    if _ACTIVE_EXEC_RE.search((tail or "")[-400:]):
        return True
    return False


def classify_safety(step_text: str) -> str:
    """autonomous_safe | owner_approval_required | prohibited (deny-by-default)."""
    from core.control_plane import actuator
    return actuator.classify_action(step_text or "")


def end_state_met(reg_entry: dict, tail: str) -> bool:
    """Best-effort: the documented end-state is only 'met' when there is NO unfinished task
    footer. A `completed` claim with open tasks is a FALSE completion (single-report claim)."""
    tf = parse_task_footer(tail)
    if tf["present"]:
        return not tf["has_unfinished"]
    return False   # no evidence of completion → do not accept a bare 'completed' tail


def evaluate(target: str, *, state: str, tail: str = "", conv_age_secs: Optional[float] = None,
             registry: Optional[dict] = None, now: Optional[float] = None) -> dict:
    """Assess one registered agent. Read-only. Returns the decision + rationale + the would-be
    next step and its safety class. Decisions: poke | skip_progressing | skip_no_work |
    skip_unsafe | skip_not_registered | watchdog_dead | watchdog_stuck_shell |
    watchdog_false_completion."""
    registry = registry if registry is not None else load_registry()
    entry = registry.get(target)
    if entry is None:
        return {"target": target, "decision": "skip_not_registered", "state": state}
    tf = parse_task_footer(tail)
    background = has_background_subagent(tail)
    progressing = is_progressing(state, tail)
    step = entry.get("next_step", "")
    safety = classify_safety(step)
    base = {"target": target, "state": state, "background_subagent": background,
            "progressing": progressing, "tasks": tf, "next_step": step, "safety": safety,
            "conv_age_secs": conv_age_secs, "end_state": entry.get("end_state", ""),
            "live_actuation": bool(entry.get("live_actuation", False))}

    # watchdogs first
    if state == "dead":
        return {**base, "decision": "watchdog_dead",
                "note": "agent pane dead — recorded; NO duplicate created"}
    if state == "shell_running" and conv_age_secs is not None and conv_age_secs > 1800:
        return {**base, "decision": "watchdog_stuck_shell",
                "note": "shell running but no proven progress > 30m — flagged"}
    if state == "completed" and not end_state_met(entry, tail):
        return {**base, "decision": "watchdog_false_completion",
                "note": "completed claimed but end-state not met (open tasks) — not accepted"}

    if progressing:
        return {**base, "decision": "skip_progressing"}
    if state not in POKE_STATES:
        return {**base, "decision": "skip_other_state"}
    # idle/waiting — is there unfinished pre-approved work?
    has_work = (tf["has_unfinished"] is True) or (tf["has_unfinished"] is None and bool(step))
    if not has_work:
        return {**base, "decision": "skip_no_work"}
    if safety != "autonomous_safe":
        return {**base, "decision": "skip_unsafe",
                "note": f"next step is not autonomous_safe ({safety}) — owner gate, not auto-poked"}
    return {**base, "decision": "poke"}


def _record_run(target: str, decision: str, detail: dict, conn=None) -> None:
    conn, own = _c(conn)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS autopilot_run ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, target TEXT, decision TEXT, "
                     "detail TEXT)")
        import json
        conn.execute("INSERT INTO autopilot_run(ts,target,decision,detail) VALUES(?,?,?,?)",
                     (now_iso(), target, decision, json.dumps(detail, default=str)[:2000]))
        conn.commit()
    finally:
        if own:
            conn.close()


def deliver_next_step(target: str, step_text: str, *, conversation_id: str = "", cwd: str = "",
                      ctrl=None, sleep=None) -> dict:
    """Deliver the next step to the EXISTING agent via the lease-gated Actuator (scope-confined
    to CANARY_AGENTS; a non-canary target returns not_canary = read-only/owner-gated). The
    Actuator supplies the lease+fence (no duplicate, restart-safe), the false-idle guard, and
    verified-submission with a receipt."""
    from core.control_plane import actuator
    from core.control_plane import api as cp
    from core import agent_continuation_watchdog as cw
    import time as _t
    # hard pre-gate: never deliver an unsafe step, even if somehow requested.
    if classify_safety(step_text) != "autonomous_safe":
        return {"acted": False, "reason": "unsafe_step_blocked"}
    ctrl = ctrl or cw.Controller()
    sleep = sleep or _t.sleep
    lease = cp.acquire_lease(f"agent:{target}", "commander_autopilot", ttl_secs=120)
    return actuator.actuate(target=target, action_text=step_text, controller="commander_autopilot",
                            conversation_id=conversation_id, kind="autopilot_next_step",
                            cwd=cwd, lease=lease, ctrl=ctrl, sleep=sleep)


def tick(*, inventory: Optional[dict] = None, registry: Optional[dict] = None,
         ctrl=None, evaluate_only: bool = False, conv_age_fn=None, now: Optional[float] = None,
         conn=None) -> dict:
    """One autopilot pass over the registry. For each registered agent: evaluate, then (unless
    evaluate_only) deliver the safe next step to a poke candidate via the Actuator. Returns the
    per-agent assessments + actions. Read-only for any agent whose actuation is owner-gated
    (not in CANARY_AGENTS → actuator returns not_canary)."""
    registry = registry if registry is not None else load_registry()
    if inventory is None:
        from core import agent_control as ac
        inventory = ac.agent_list()
    by_target = {a.get("target"): a for a in (inventory.get("agents") or [])}
    results = []
    for target, entry in registry.items():
        a = by_target.get(target)
        state = (a or {}).get("state") or "dead"        # not present → treat as dead (watchdog)
        tail = (a or {}).get("_tail") or ""
        conv_age = conv_age_fn(target) if conv_age_fn else None
        ev = evaluate(target, state=state, tail=tail, conv_age_secs=conv_age,
                      registry=registry, now=now)
        action = None
        if ev["decision"] == "poke" and not evaluate_only:
            cwd = entry.get("root", "")
            conv = (a or {}).get("claude_conversation") or ""
            action = deliver_next_step(target, ev["next_step"], conversation_id=conv,
                                       cwd=cwd, ctrl=ctrl)
            ev["actuation"] = action
            ev["delivered"] = bool(action.get("acted"))
            ev["actuation_reason"] = action.get("reason")
            if not action.get("acted") and action.get("reason") == "not_canary":
                ev["decision"] = "poke_owner_gated"      # would poke, but live actuation gated
        _record_run(target, ev["decision"], ev, conn=conn)
        results.append(ev)
    return {"evaluated": len(results), "results": results,
            "poked": sum(1 for r in results if r.get("delivered")),
            "owner_gated": sum(1 for r in results if r["decision"] == "poke_owner_gated")}


ENABLED = os.getenv("COMMANDER_AUTOPILOT_ENABLED", "0") not in ("0", "false", "no", "")
INTERVAL = int(os.getenv("COMMANDER_AUTOPILOT_INTERVAL_SECS", "60"))


async def run_loop() -> None:
    """Per-minute autopilot loop. DISABLED by default (COMMANDER_AUTOPILOT_ENABLED) — enabling
    it live is an owner gate. Even enabled, actuation stays confined to CANARY_AGENTS."""
    import asyncio
    import logging
    log = logging.getLogger("commander_autopilot")
    if not ENABLED:
        log.info("commander autopilot disabled (owner gate)")
        return
    log.info(f"commander autopilot started (interval {INTERVAL}s; actuation confined to CANARY_AGENTS)")
    while True:
        try:
            res = await asyncio.to_thread(tick)
            if res["poked"] or res["owner_gated"]:
                log.info(f"autopilot: poked={res['poked']} owner_gated={res['owner_gated']} "
                         f"evaluated={res['evaluated']}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"autopilot tick error: {e}")
        await asyncio.sleep(INTERVAL)
