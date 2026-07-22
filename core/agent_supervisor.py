"""Always-on Agent Supervisor.

Runs inside ai-runtime.service (the existing always-on daemon), independent of
any ChatGPT/MCP client. Every cycle it inspects the existing agent panes and,
for owner-approved sessions, auto-confirms permission prompts that are provably
local read-only checks / tests / dry-runs — then verifies the agent actually
resumed. Everything unclear or consequential is left for the owner.

Design guarantees:
  * Deny-by-default. A prompt is auto-confirmed only when
    `permission_resolver.classify_command` proves it safe AND the session is on
    the auto-resolve allowlist. Anything else stays waiting_owner.
  * Idempotent + restart-safe. Each (target, prompt-hash) decision is persisted
    (`supervisor_prompts`), so a prompt is never re-processed or re-alerted after
    a restart, and a safe prompt is confirmed at most once per appearance.
  * Verifies resume and records latency; a confirmed-but-not-resumed agent is
    recorded and left in waiting_owner so the Owner OS notifier raises the owner
    alert.
  * It never starts, stops, or restarts a session or a service, and never sends
    anything but the single approval keystroke to an allowlisted safe prompt.
"""
from __future__ import annotations

import os
import time

from core import agent_control as ac
from core import permission_resolver as pr

POLL_INTERVAL = int(os.getenv("AGENT_SUPERVISOR_INTERVAL_SECS", "45"))
RESUME_TIMEOUT = int(os.getenv("AGENT_SUPERVISOR_RESUME_TIMEOUT_SECS", "8"))
# A prompt already approved within this window is never answered again — makes
# resolution exactly-once across both supervision loops and across restarts.
DEDUP_WINDOW = int(os.getenv("AGENT_SUPERVISOR_DEDUP_SECS", "900"))
ENABLED = os.getenv("AGENT_SUPERVISOR_ENABLED", "0") not in ("0", "false", "no", "")


def _allowlisted_sessions() -> set[str]:
    """Sessions eligible for unattended safe-resolution, discovered DYNAMICALLY:
    every `mode: auto` session in the orchestrator config (the managed-auto set),
    plus any explicit `AGENT_AUTORESOLVE_SESSIONS` override. No project name is
    hard-coded — adding an `auto` session to the config covers it automatically;
    `hold`/`monitor`/unlisted sessions are never eligible (detection-only)."""
    raw = os.getenv("AGENT_AUTORESOLVE_SESSIONS", "").strip()
    sessions = {s.strip() for s in raw.split(",") if s.strip()}
    try:
        from core import agent_orchestrator as _orch
        cfg = _orch.load_config()
        sessions |= {name for name, sc in (cfg.get("sessions") or {}).items()
                     if isinstance(sc, dict) and sc.get("mode") == "auto"}
    except Exception:  # noqa: BLE001 — config unavailable → fall back to env only
        pass
    return sessions


def _session_of(target: str) -> str:
    return target.split(":", 1)[0]


def resolve_target(target: str, approve: bool = True, _sleep=time.sleep) -> dict:
    """Inspect one agent and, if it is waiting on a provably-safe prompt in an
    allowlisted session, confirm it. Returns a structured decision."""
    ac.validate_target(target)
    status = ac.agent_status(target)
    state = status.get("state")
    if state != "waiting_owner":
        return {"target": target, "action": "none", "state": state}

    tail = status.get("recent_activity") or ""
    command = pr.extract_pending_command(tail)
    if not command:
        return {"target": target, "action": "left_for_owner", "state": state,
                "reason": "no command could be extracted from the prompt"}

    cls = pr.classify_command(command)
    phash = cls["hash"]
    session = _session_of(status["target"])
    allowlisted = session in _allowlisted_sessions()

    if not cls["safe"]:
        ac.record_prompt_decision(status["target"], phash, "left_for_owner", cls["category"], cls["reason"])
        return {"target": status["target"], "action": "left_for_owner", "safe": False,
                "command": command, "category": cls["category"], "reason": cls["reason"],
                "hash": phash}
    if not allowlisted:
        ac.record_prompt_decision(status["target"], phash, "left_not_allowlisted", cls["category"], cls["reason"])
        return {"target": status["target"], "action": "left_for_owner", "safe": True,
                "command": command, "category": cls["category"],
                "reason": f"session {session!r} is not on the auto-resolve allowlist", "hash": phash}

    # Detection timestamp — a provably-safe, allowlisted prompt is now recognised.
    t_detect = time.time()

    # Idempotency: if THIS exact prompt was already answered recently (by this loop,
    # the orchestrator loop, or a pre-restart run), never send the key again.
    prior = ac.get_prompt_decision(status["target"], phash)
    if prior and str(prior.get("decision", "")).startswith("approved") \
            and (time.time() - (prior.get("updated_ts") or 0)) < DEDUP_WINDOW:
        return {"target": status["target"], "action": "already_resolved", "safe": True,
                "command": command, "category": cls["category"], "hash": phash,
                "prior_decision": prior.get("decision"), "prior_ts": prior.get("updated_ts")}

    if not approve:
        return {"target": status["target"], "action": "would_approve", "safe": True,
                "command": command, "category": cls["category"], "hash": phash}

    # Confirm this once (decision → deliver), then verify the agent resumed. Each
    # phase is timestamped so the acceptance canary can prove reaction time.
    t_decision = time.time()
    ac.approve_prompt(status["target"])          # audits the '1' keystroke (delivery)
    t_delivered = time.time()
    resumed, new_state, t_resumed = False, state, None
    deadline = t_delivered + RESUME_TIMEOUT
    while time.time() < deadline:
        _sleep(1)
        new_state = ac.agent_status(status["target"]).get("state")
        if new_state != "waiting_owner":
            resumed = True
            t_resumed = time.time()
            break
    latency = round(time.time() - t_decision, 2)
    decision = "approved" if resumed else "approved_no_resume"
    ac.record_prompt_decision(status["target"], phash, decision, cls["category"], cls["reason"], latency)
    # First-class evidence trail: detection → decision → delivery → resumed.
    ac.audit("supervisor_resolved", status["target"], hash=phash, command=command,
             category=cls["category"], decision=decision, resumed=resumed,
             detect_ts=round(t_detect, 3), decision_ts=round(t_decision, 3),
             delivered_ts=round(t_delivered, 3),
             resumed_ts=round(t_resumed, 3) if t_resumed else None,
             reaction_s=round((t_resumed - t_detect), 2) if t_resumed else None,
             latency_s=latency)
    return {"target": status["target"], "action": decision, "safe": True, "resumed": resumed,
            "command": command, "category": cls["category"], "latency_s": latency,
            "new_state": new_state, "hash": phash,
            "detect_ts": t_detect, "decision_ts": t_decision, "resumed_ts": t_resumed}


def poll_once(approve: bool = True) -> dict:
    """One supervision sweep over the allowlisted sessions' agents."""
    allow = _allowlisted_sessions()
    try:
        inventory = ac.agent_list()
    except ac.AgentControlError as e:
        return {"ok": False, "error": str(e)[:200], "resolved": []}
    results = []
    for a in inventory.get("agents", []):
        if not (a.get("is_agent") and a.get("alive")):
            continue
        if _session_of(a["target"]) not in allow:
            continue
        if a.get("state") != "waiting_owner":
            continue
        try:
            results.append(resolve_target(a["target"], approve=approve))
        except ac.AgentControlError as e:
            results.append({"target": a["target"], "action": "error", "error": str(e)[:200]})
    return {"ok": True, "sessions": sorted(allow), "resolved": results,
            "approved": sum(1 for r in results if r.get("action") == "approved"),
            "no_resume": sum(1 for r in results if r.get("action") == "approved_no_resume")}


async def run_loop() -> None:
    """Background supervision loop for the always-on daemon."""
    import asyncio
    import logging
    log = logging.getLogger("agent_supervisor")
    if not ENABLED:
        log.info("agent supervisor disabled (AGENT_SUPERVISOR_ENABLED unset)")
        return
    log.info(f"agent supervisor started (interval {POLL_INTERVAL}s, sessions={sorted(_allowlisted_sessions())})")
    while True:
        try:
            res = await asyncio.to_thread(poll_once, True)
            if res.get("approved") or res.get("no_resume"):
                log.info(f"supervisor: approved={res.get('approved')} no_resume={res.get('no_resume')}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"supervisor tick error: {e}")
        await asyncio.sleep(POLL_INTERVAL)
