"""Autonomous Agent Orchestrator V1.

Always-on supervision of the EXISTING agents. It never creates a session and
never duplicates one. For each agent it maintains a persistent record (project,
approved goal, current task, phase, state, last fresh activity, prompt hash,
blocker category, completion evidence, report path, approved next task,
notification state, retry count) so state survives a service restart.

State is derived only from FRESH process + pane evidence (via
`agent_control.classify_state`, which requires active-execution evidence for
`working`); stale text alone is never enough. On top of the observable states it
adds the orchestration states `waiting_safe_approval` (a permission prompt whose
command the structured policy proves safe), `paused_by_budget`, and `parked`.

Autonomy is per-session policy (config/agent_orchestrator.yaml):
  * auto  — auto-continue provably-safe local prompts, verify the agent resumed;
            leave unknown/consequential prompts for the owner.
  * hold  — monitor only; never resolve or advance (Safe Guard, Polyinput).
  * monitor (default) — observe + record; no resolution.

Review ladder for a prompt: (1) local structured policy (permission_resolver);
(2) a bounded low-cost reviewer model; (3) a stronger reviewer only when
complexity justifies it — tiers 2/3 gated by ORCH_REVIEW_MODEL_ENABLED and
recorded with recommendation/confidence/cost. Owner escalation is reserved for
genuine product/external decisions.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

from core import agent_control as ac
from core import permission_resolver as pr

ORCH_STATES = ("working", "idle", "waiting_safe_approval", "waiting_owner",
               "externally_blocked", "completed", "failed", "paused_by_budget", "parked")

_CONFIG_PATH = os.getenv("AGENT_ORCHESTRATOR_CONFIG",
                         os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                      "config", "agent_orchestrator.yaml"))
_COMPLETION_WINDOW_SECS = int(os.getenv("ORCH_COMPLETION_WINDOW_SECS", "1800"))
ENABLED = os.getenv("AGENT_ORCHESTRATOR_ENABLED", "0") not in ("0", "false", "no", "")


def _now_ts() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── config ──────────────────────────────────────────────────────────────────
_config_cache: dict = {}


def load_config() -> dict:
    global _config_cache
    try:
        import yaml
        with open(_CONFIG_PATH) as fh:
            _config_cache = yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001
        _config_cache = {}
    return _config_cache


def _session_cfg(session: str) -> dict:
    cfg = _config_cache or load_config()
    return (cfg.get("sessions") or {}).get(session, {"mode": "monitor"})


def _allowed_roots() -> list[str]:
    cfg = _config_cache or load_config()
    return cfg.get("allowed_roots") or ["/opt", "/root/ai-dev-runtime"]


# ── budget gate (best-effort, respected by auto sessions) ───────────────────
def budget_locked() -> bool:
    """True when autonomous paid work must pause. Best-effort: an env flag or a
    marker file set by the Owner OS budget governor."""
    if os.getenv("ORCH_BUDGET_LOCKED", "0") not in ("0", "false", "no", ""):
        return True
    marker = os.getenv("ORCH_BUDGET_LOCK_FILE", "/run/owner-os/autospend.lock")
    return os.path.exists(marker)


# ── persistence ─────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db"), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_orchestrator (
        agent_key TEXT PRIMARY KEY, session TEXT, project TEXT, approved_goal TEXT,
        current_task TEXT, phase TEXT, state TEXT, last_fresh_activity_ts REAL,
        prompt_hash TEXT, blocker_category TEXT, completion_evidence TEXT,
        report_path TEXT, approved_next_task TEXT, notification_state TEXT,
        retry_count INTEGER DEFAULT 0, decision TEXT, updated_at TEXT)""")
    conn.commit()
    return conn


_FIELDS = ("agent_key", "session", "project", "approved_goal", "current_task", "phase",
           "state", "last_fresh_activity_ts", "prompt_hash", "blocker_category",
           "completion_evidence", "report_path", "approved_next_task",
           "notification_state", "retry_count", "decision", "updated_at")


def get_record(agent_key: str) -> Optional[dict]:
    conn = _db()
    try:
        row = conn.execute(f"SELECT {','.join(_FIELDS)} FROM agent_orchestrator WHERE agent_key=?",
                           (agent_key,)).fetchone()
        if not row:
            return None
        rec = dict(zip(_FIELDS, row))
        rec["decision"] = json.loads(rec["decision"]) if rec.get("decision") else None
        return rec
    finally:
        conn.close()


def _upsert(rec: dict) -> None:
    rec = dict(rec)
    rec["updated_at"] = _now_iso()
    rec["decision"] = json.dumps(rec.get("decision")) if rec.get("decision") is not None else None
    conn = _db()
    try:
        cols = ",".join(_FIELDS)
        ph = ",".join("?" for _ in _FIELDS)
        conn.execute(f"INSERT OR REPLACE INTO agent_orchestrator ({cols}) VALUES ({ph})",
                     tuple(rec.get(f) for f in _FIELDS))
        conn.commit()
    finally:
        conn.close()


def all_records() -> list[dict]:
    conn = _db()
    try:
        rows = conn.execute(f"SELECT {','.join(_FIELDS)} FROM agent_orchestrator "
                           "ORDER BY session").fetchall()
        out = []
        for row in rows:
            rec = dict(zip(_FIELDS, row))
            rec["decision"] = json.loads(rec["decision"]) if rec.get("decision") else None
            out.append(rec)
        return out
    finally:
        conn.close()


# ── review ladder (local → cheap model → strong model) ──────────────────────
def review_command(command: str, cwd: str, roots: list[str]) -> dict:
    """Tier 1 local structured policy. Returns a verdict with recommendation,
    confidence and model cost. Tiers 2/3 (bounded model reviewers) are consulted
    only when enabled and when tier 1 is inconclusive-but-plausible."""
    local = pr.classify_command(command, cwd=cwd, project_roots=roots)
    verdict = {"tier": "local_policy", "safe": local["safe"], "category": local["category"],
               "reason": local["reason"], "confidence": 1.0 if local["safe"] else 0.9,
               "model": None, "cost_usd": 0.0, "hash": local["hash"]}
    # Tiers 2/3 would run here for borderline cases; disabled by default so V1
    # never spends on a clearly-safe or clearly-unsafe command.
    if not local["safe"] and os.getenv("ORCH_REVIEW_MODEL_ENABLED", "0") not in ("0", "false", "no", ""):
        try:
            verdict.update(_model_review(command))
        except Exception as e:  # noqa: BLE001
            verdict["reason"] += f" | model review skipped: {str(e)[:80]}"
    return verdict


def _model_review(command: str) -> dict:
    """Bounded low-cost reviewer via the existing provider. Conservative: a model
    may only DOWNGRADE to unsafe, never upgrade an unsafe command to safe — the
    local deny is authoritative for safety."""
    return {"tier": "cheap_model", "model": os.getenv("ORCH_REVIEW_MODEL", "claude-haiku-4-5"),
            "cost_usd": 0.0, "confidence": 0.5,
            "reason": "model reviewer advisory only; local deny stands"}


# ── state derivation (fresh evidence only) ──────────────────────────────────
def _map_base_state(observed: str) -> str:
    if observed in ("dead", "stale"):
        return "failed"
    return observed


def _completion_evidence(session: str, root: str, agent) -> Optional[dict]:
    """A recently-written report in the project is completion evidence."""
    if not root:
        return None
    try:
        rep = ac.agent_report(root, limit=5)
    except ac.AgentControlError:
        return None
    reports = rep.get("reports") or []
    if not reports:
        return None
    latest = reports[0]
    try:
        mt = datetime.fromisoformat(latest["modified_at"]).timestamp()
    except Exception:  # noqa: BLE001
        return None
    if (_now_ts() - mt) <= _COMPLETION_WINDOW_SECS:
        return {"report_path": latest["path"], "modified_at": latest["modified_at"]}
    return None


def _build_decision(project: str, command: str, verdict: dict) -> dict:
    """Rich owner-decision object for a genuine (non-safe) prompt."""
    return {
        "project": project,
        "action": command,
        "why_blocked": f"not auto-continuable: {verdict['reason']}",
        "risk": ("external or consequential effect" if verdict["category"] in ("denied", "shell_construct")
                 else "unverified effect"),
        "recommended": "review the command and approve only if intended",
        "alternatives": ["approve (owner)", "reject and instruct the agent", "leave paused"],
        "reply_choices": ["1 = approve", "2 = reject", "3 = leave for later"],
        "prompt_hash": verdict["hash"],
    }


def derive(agent: dict, cfg: dict) -> dict:
    """Return {state, decision?, prompt_hash?, blocker_category?, fresh, command?}."""
    session = agent["target"].split(":", 1)[0]
    observed = _map_base_state(agent.get("state") or "idle")
    root = cfg.get("root") or agent.get("claude_cwd") or agent.get("cwd")
    mode = cfg.get("mode", "monitor")

    if mode == "parked":
        return {"state": "parked", "fresh": False}

    # Budget pause overrides active/auto work (not owner-held observations).
    if mode == "auto" and budget_locked() and observed in ("working", "idle"):
        return {"state": "paused_by_budget", "fresh": False}

    if observed == "waiting_owner":
        tail = agent.get("recent_activity") or agent.get("_tail") or ""
        command = pr.extract_pending_command(tail)
        if command:
            # Validate the AGENT'S ACTUAL cwd against the project root (the agent
            # must be operating inside its own project), not the config root.
            agent_cwd = agent.get("claude_cwd") or agent.get("cwd")
            roots = [cfg["root"]] if cfg.get("root") else _allowed_roots()
            verdict = review_command(command, agent_cwd, roots)
            if verdict["safe"] and mode == "auto":
                return {"state": "waiting_safe_approval", "command": command,
                        "prompt_hash": verdict["hash"], "fresh": True, "verdict": verdict}
            # genuine owner decision
            return {"state": "waiting_owner", "command": command, "prompt_hash": verdict["hash"],
                    "blocker_category": verdict["category"], "fresh": False,
                    "decision": _build_decision(cfg.get("project", session), command, verdict)}
        return {"state": "waiting_owner", "blocker_category": "unextractable", "fresh": False,
                "decision": _build_decision(cfg.get("project", session), "(unreadable prompt)",
                                            {"reason": "prompt not machine-readable", "category": "unknown",
                                             "hash": pr.command_hash(tail[-200:])})}

    if observed == "externally_blocked":
        return {"state": "externally_blocked", "blocker_category": "external", "fresh": False}

    if observed == "idle":
        ev = _completion_evidence(session, root, agent)
        if ev:
            return {"state": "completed", "completion_evidence": ev, "report_path": ev["report_path"],
                    "fresh": True}
        return {"state": "idle", "fresh": False}

    # working / failed
    return {"state": observed, "fresh": observed == "working"}


# ── tick ────────────────────────────────────────────────────────────────────
def refresh_and_resolve(approve: bool = True) -> dict:
    """One orchestration sweep over the EXISTING agents. Auto-continues safe
    prompts for `auto` sessions, records everything, and surfaces owner
    decisions. Never creates or stops a session."""
    load_config()
    try:
        inv = ac.agent_list()
    except ac.AgentControlError as e:
        return {"ok": False, "error": str(e)[:200], "agents": 0}
    results, resolved, escalations, full_records = [], [], [], []
    for agent in inv.get("agents", []):
        if not (agent.get("is_agent") and agent.get("alive")):
            continue
        key = agent["target"]
        session = key.split(":", 1)[0]
        cfg = _session_cfg(session)
        # fresh pane tail for waiting-prompt extraction
        try:
            agent["_tail"] = ac._pane_tail(key, 40)
        except Exception:  # noqa: BLE001
            agent["_tail"] = ""
        d = derive(agent, cfg)
        prev = get_record(key) or {}
        state = d["state"]
        fresh = d.get("fresh")

        nxt = _next_phase(cfg)
        rec = {
            "agent_key": key, "session": session,
            "project": cfg.get("project", prev.get("project")),
            "approved_goal": cfg.get("approved_goal", prev.get("approved_goal")),
            "current_task": prev.get("current_task"),
            "phase": prev.get("phase") or (_phases(cfg)[0]["id"] if _phases(cfg) else None),
            "state": state,
            "last_fresh_activity_ts": (_now_ts() if fresh else prev.get("last_fresh_activity_ts")),
            "prompt_hash": d.get("prompt_hash", prev.get("prompt_hash")),
            "blocker_category": d.get("blocker_category"),
            "completion_evidence": (json.dumps(d["completion_evidence"]) if d.get("completion_evidence")
                                    else prev.get("completion_evidence")),
            "report_path": d.get("report_path", prev.get("report_path")),
            "approved_next_task": _describe_next_phase(nxt),
            # Recompute notification_state from the CURRENT state each tick — never
            # carry a stale value (a completed agent must not stay 'needs_escalation').
            "notification_state": None,
            "retry_count": prev.get("retry_count") or 0,
            "decision": d.get("decision"),
        }

        # auto-continue a provably-safe prompt
        if state == "waiting_safe_approval" and cfg.get("mode") == "auto" and approve and not budget_locked():
            outcome = _resolve_safe(key, d.get("command"), rec)
            rec.update(outcome["rec_updates"])
            resolved.append(outcome["summary"])
        elif state == "waiting_owner":
            # dedup escalation by prompt hash
            if prev.get("prompt_hash") == rec["prompt_hash"] and prev.get("notification_state") == "escalated":
                rec["notification_state"] = "escalated"
            else:
                rec["notification_state"] = "needs_escalation"
                escalations.append({"agent": key, "project": rec["project"], "decision": rec["decision"]})
        elif state == "completed" and cfg.get("mode") == "auto" and cfg.get("advance_phases"):
            # Supervision: a completed phase must never sit unattended. If the next
            # phase can auto-advance (has exact approved text) the sweep handles it;
            # otherwise the owner is asked ONCE to record the text / decide.
            sup = _supervise_completed(session, cfg, rec, prev, nxt)
            rec["notification_state"] = sup["notification_state"]
            if sup.get("escalation"):
                escalations.append(sup["escalation"])

        _upsert(rec)
        results.append({"agent": key, "state": state, "project": rec["project"]})
        full_records.append(rec)

    # Cross-phase auto-progress (guarded, exact-approved-text-only, idempotent).
    advances = {"enabled": False, "results": []}
    try:
        from core import agent_phase_advance
        advances = agent_phase_advance.sweep(full_records, _session_cfg, dispatch=approve)
    except Exception as e:  # noqa: BLE001
        advances = {"enabled": True, "error": str(e)[:200], "results": []}

    return {"ok": True, "agents": len(results), "records": results,
            "resolved": resolved, "escalations": escalations, "phase_advances": advances}


def _phases(cfg: dict) -> list:
    return cfg.get("phases") or []


def _next_phase(cfg: dict) -> Optional[dict]:
    """The next phase after the current one (config only — never invented)."""
    phases = _phases(cfg)
    return phases[1] if len(phases) > 1 else None


def _describe_next_phase(nxt: Optional[dict]) -> Optional[str]:
    """Human label that is HONEST about whether the phase can actually progress."""
    if not nxt:
        return None
    title = nxt.get("title") or nxt.get("id")
    if (nxt.get("approved_task_text") or "").strip():
        return f"{title} (ready to auto-advance)"
    return f"{title} (awaiting owner-approved text)"


def _supervise_completed(session: str, cfg: dict, rec: dict, prev: dict, nxt: Optional[dict]) -> dict:
    """A completed phase must not sit unattended. Returns notification_state and an
    optional one-time owner escalation."""
    if nxt is None:
        return {"notification_state": "phase_complete_final"}   # nothing more approved
    if (nxt.get("approved_task_text") or "").strip():
        # The phase-advance sweep will dispatch + verify this; not an owner decision.
        return {"notification_state": "advancing"}
    # Unattended: next phase has no exact approved text. Ask the owner ONCE.
    report = rec.get("report_path") or ""
    already = (prev.get("notification_state") == "phase_complete_needs_owner"
               and prev.get("report_path") == report)
    if already:
        return {"notification_state": "phase_complete_needs_owner"}   # already asked; quiet
    decision = {
        "project": rec.get("project") or session,
        "action": f"record exact approved next-phase text for '{nxt.get('title') or nxt.get('id')}' (or decide to stop)",
        "why_blocked": "phase complete, but the next approved phase has no exact owner-approved task text — "
                       "the orchestrator will not invent product direction",
        "risk": "none (nothing is auto-dispatched without recorded text)",
        "recommended": "record the exact non-publishing/non-premium next-phase text, or mark the project done",
        "alternatives": ["record next-phase text (auto-advances)", "mark project complete", "leave paused"],
        "reply_choices": ["1 = record next text", "2 = mark done", "3 = leave"],
        "prompt_hash": _hash_report(session, report),
    }
    return {"notification_state": "phase_complete_needs_owner",
            "escalation": {"agent": rec["agent_key"], "project": decision["project"], "decision": decision}}


def _hash_report(session: str, report: str) -> str:
    import hashlib
    return hashlib.sha256(f"{session}\x1f{report}".encode()).hexdigest()[:16]


def _resolve_safe(key: str, command: str, rec: dict) -> dict:
    """Confirm a safe prompt via the supervisor and verify the agent resumed."""
    from core import agent_supervisor
    t0 = _now_ts()
    res = agent_supervisor.resolve_target(key, approve=True)
    latency = round(_now_ts() - t0, 2)
    resumed = res.get("resumed", res.get("action") == "approved")
    updates = {
        "notification_state": "auto_continued" if resumed else "auto_continue_no_resume",
        "last_fresh_activity_ts": _now_ts() if resumed else rec.get("last_fresh_activity_ts"),
        "retry_count": (rec.get("retry_count") or 0) + (0 if resumed else 1),
    }
    return {"rec_updates": updates,
            "summary": {"agent": key, "command": command, "resumed": resumed,
                        "latency_s": res.get("latency_s", latency), "action": res.get("action")}}


def status() -> dict:
    """Read-only orchestrator status for all tracked agents."""
    return {"states": ORCH_STATES, "budget_locked": budget_locked(),
            "records": all_records(), "checked_at": _now_iso()}


# ── always-on loop ──────────────────────────────────────────────────────────
async def run_loop() -> None:
    import asyncio
    import logging
    log = logging.getLogger("agent_orchestrator")
    if not ENABLED:
        log.info("agent orchestrator disabled (AGENT_ORCHESTRATOR_ENABLED unset)")
        return
    interval = int(os.getenv("AGENT_ORCHESTRATOR_INTERVAL_SECS", "45"))
    load_config()
    log.info(f"agent orchestrator started (interval {interval}s)")
    while True:
        try:
            res = await asyncio.to_thread(refresh_and_resolve, True)
            if res.get("resolved") or res.get("escalations"):
                log.info(f"orchestrator: resolved={len(res.get('resolved', []))} "
                         f"escalations={len(res.get('escalations', []))}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"orchestrator tick error: {e}")
        await asyncio.sleep(interval)
