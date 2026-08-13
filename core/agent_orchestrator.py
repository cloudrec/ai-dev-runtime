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
import re as _re
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
# Keep managed-auto agents in a non-stalling execution mode (`auto mode on`) so
# routine work does not stall on a permission prompt. Enforced ONLY for auto
# sessions and only at a task boundary (not mid-dialog); held/monitor untouched.
AUTO_MODE_ENFORCE = os.getenv("AGENT_AUTO_MODE_ENFORCE", "1") not in ("0", "false", "no", "")


def _now_ts() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── config ──────────────────────────────────────────────────────────────────
_config_cache: dict = {}


def load_config() -> dict:
    global _config_cache
    # Re-read the path from the env each call so a runtime/test config change is
    # honoured (the module-level default is only the fallback).
    path = os.getenv("AGENT_ORCHESTRATOR_CONFIG", _CONFIG_PATH)
    try:
        import yaml
        with open(path) as fh:
            _config_cache = yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001
        _config_cache = {}
    return _config_cache


def _session_cfg(session: str) -> dict:
    cfg = _config_cache or load_config()
    sc = dict((cfg.get("sessions") or {}).get(session, {"mode": "monitor"}))
    # Overlay owner-submitted exact next-phase text onto the config phases, so the
    # owner can enable auto-advance for a phase from the admin screen without a
    # config edit. Owner-recorded text is authoritative for that phase.
    phases = sc.get("phases")
    if phases:
        merged = []
        for p in phases:
            pt = get_phase_text(session, p.get("id"))
            merged.append({**p, "approved_task_text": pt} if pt else dict(p))
        sc["phases"] = merged
    return sc


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
    # Additive columns (exact blocker text + decision type + context budget + exec mode) — older rows get NULL.
    for col in ("blocker_text TEXT", "decision_type TEXT", "context_pct REAL",
                "context_tier TEXT", "exec_mode TEXT"):
        try:
            conn.execute(f"ALTER TABLE agent_orchestrator ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


_FIELDS = ("agent_key", "session", "project", "approved_goal", "current_task", "phase",
           "state", "last_fresh_activity_ts", "prompt_hash", "blocker_category",
           "completion_evidence", "report_path", "approved_next_task",
           "notification_state", "retry_count", "decision", "updated_at",
           "blocker_text", "decision_type", "context_pct", "context_tier", "exec_mode")


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


def _phase_text_db() -> sqlite3.Connection:
    conn = sqlite3.connect(os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db"), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_phase_text (
        session TEXT, phase_id TEXT, approved_task_text TEXT, updated_at TEXT,
        PRIMARY KEY (session, phase_id))""")
    conn.commit()
    return conn


def get_phase_text(session: str, phase_id: str) -> Optional[str]:
    conn = _phase_text_db()
    try:
        row = conn.execute("SELECT approved_task_text FROM agent_phase_text WHERE session=? AND phase_id=?",
                           (session, phase_id)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


_MAX_PHASE_TEXT = 8000
# Owner-submitted phase text must not silently authorise external side effects.
_PHASE_TEXT_FORBIDDEN = ("publish", "premium", "payment", "charge", "send email",
                         "sendmail", "credential", "rotate secret", "api key")


def set_phase_text(session: str, phase_id: str, text: str) -> dict:
    """Record the owner's exact approved next-phase text for a session/phase.

    Validates the session exists in config and the phase id is defined. Rejects
    text that would authorise external publishing / payments / email / credential
    changes (defence in depth — the safe-resolution policy still gates any command
    the agent then tries to run)."""
    load_config()
    scfg = (_config_cache.get("sessions") or {}).get(session)
    if not scfg:
        raise ValueError(f"unknown session: {session!r}")
    phase_ids = {p.get("id") for p in (scfg.get("phases") or [])}
    if phase_id not in phase_ids:
        raise ValueError(f"unknown phase {phase_id!r} for session {session!r}; defined: {sorted(phase_ids)}")
    text = (text or "").strip()
    if not text:
        raise ValueError("approved_task_text is required")
    if len(text.encode()) > _MAX_PHASE_TEXT:
        raise ValueError(f"text too large (> {_MAX_PHASE_TEXT} bytes)")
    low = text.lower()
    hit = next((w for w in _PHASE_TEXT_FORBIDDEN if w in low), None)
    if hit:
        raise ValueError(f"text mentions a forbidden external action ({hit!r}); "
                         "V3 canary/records must not authorise publishing/payments/email/credentials")
    conn = _phase_text_db()
    try:
        conn.execute("INSERT OR REPLACE INTO agent_phase_text VALUES (?,?,?,?)",
                     (session, phase_id, text, _now_iso()))
        conn.commit()
    finally:
        conn.close()
    ac_audit_phase_text(session, phase_id, len(text))
    return {"session": session, "phase_id": phase_id, "recorded": True, "bytes": len(text.encode())}


def ac_audit_phase_text(session, phase_id, size):
    from core import agent_control as _ac
    _ac.audit("phase_text_set", f"{session}:{phase_id}", bytes=size)


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


_REAPED_STATES = ("vanished", "ended")


def reap_vanished(live_sessions, emit=None) -> list:
    """Reconcile records whose tmux session has VANISHED (no live pane).

    Atomically transitions each such record to `vanished` (guarded so a concurrent
    sweep / restart can never double-process it) and, ONLY when it carried approved
    unfinished work, invokes `emit(agent_key, session, info)` exactly once. Never
    recreates an agent and never touches a live pane. `live_sessions` is the set of
    sessions that currently have a live agent pane.
    """
    live = set(live_sessions or [])
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT agent_key,session,state,approved_goal,current_task FROM agent_orchestrator"
        ).fetchall()
        reaped = []
        for agent_key, session, state, goal, task in rows:
            if session in live or state in _REAPED_STATES:
                continue
            # approved unfinished work = had a goal/task and was not already finished.
            had_work = bool(goal or task) and state not in ("completed", "failed")
            # ATOMIC + race/restart-safe: only the writer that flips it away from a
            # non-reaped state proceeds; a loser sees rowcount 0 and emits nothing.
            cur = conn.execute(
                "UPDATE agent_orchestrator SET state='vanished', notification_state='vanished', "
                "updated_at=? WHERE agent_key=? AND state NOT IN ('vanished','ended')",
                (_now_iso(), agent_key))
            conn.commit()
            if cur.rowcount == 0:
                continue
            info = {"agent": agent_key, "session": session, "prev_state": state,
                    "had_approved_unfinished_work": had_work,
                    "approved_goal": goal, "current_task": task}
            reaped.append(info)
            if had_work and emit:
                try:
                    emit(agent_key, session, info)
                except Exception:  # noqa: BLE001
                    pass
        return reaped
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


# Markers that prove the agent is still executing (Claude Code shows these while a
# turn/tool is running). If present, an "idle" base classification is a lag artefact
# and MUST NOT be read as completion, however fresh a report looks.
_ACTIVE_EXEC_RE = _re.compile(
    r"(esc to interrupt|\bthinking…|\bthinking\.\.\.|\brunning…|\brunning\.\.\.|"
    r"\bcompacting|\btool call|\bexecuting\b|✻|✽)", _re.I)


def _completion_evidence(session: str, root: str, agent) -> Optional[dict]:
    """Completion requires THREE things, so a stray recent report cannot fake it:
      1. the agent is genuinely idle (no active-execution markers in the pane),
      2. a report exists AND was written inside the completion window (freshness),
      3. the report was written at/after the agent's last observed fresh activity
         (so an unrelated OLD report re-touched by something else is not evidence)."""
    if not root:
        return None
    # 1. mid-run guard — a spinner / "esc to interrupt" means still working.
    tail = agent.get("recent_activity") or agent.get("_tail") or ""
    if _ACTIVE_EXEC_RE.search(tail):
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
    # 2. freshness window.
    if (_now_ts() - mt) > _COMPLETION_WINDOW_SECS:
        return None
    # 3. ordering — the report must not predate the agent's last real activity.
    prev = get_record(agent.get("target", f"{session}:")) or {}
    last_activity = prev.get("last_fresh_activity_ts")
    if last_activity and mt + 1 < float(last_activity):
        return None
    return {"report_path": latest["path"], "modified_at": latest["modified_at"]}


# Real external / product / financial actions that genuinely need the owner. An
# internal-but-unrecognised command is NOT one of these — it is left for the owner
# WITHOUT a Telegram alert (visible in the dashboard/brief only).
_EXTERNAL_FINANCIAL_RE = _re.compile(
    r"(curl|wget|http|ssh|scp|rsync\s+[^ ]+:|publish|deploy|release|npm\s+publish|"
    r"docker\s+push|git\s+push|payment|charge|stripe|invoice|billing|premium|subscribe|"
    r"send\s*mail|sendmail|smtp|email|outreach|dm\b|credential|secret|token|api[_-]?key|"
    r"rotate|\.env|prod(uction)?|"
    # consequential operations (destructive / service-affecting) also need the owner
    r"restart|reload|\bstop\b|\bkill\b|\bdown\b|\brm\b|\bdelete\b|\bdrop\b|truncate|"
    r"migrate|alembic\s+(up|down)|reset\s+--hard|force[- ]?push|chmod|chown)", _re.I)


def decision_type(command: str, category: str) -> str:
    """external | financial | internal. Only external/financial escalate to the owner."""
    c = command or ""
    if _re.search(r"(payment|charge|stripe|invoice|billing|premium|subscribe|refund|payout)", c, _re.I):
        return "financial"
    if _EXTERNAL_FINANCIAL_RE.search(c):
        return "external"
    return "internal"


def _exact_blocker_text(command: str, verdict: dict) -> str:
    """The EXACT blocker — the real command + concrete reason, never a bare category."""
    cmd = (command or "(unreadable prompt)").strip()
    reason = verdict.get("reason") or verdict.get("category") or "requires owner judgement"
    return f"{cmd} — {reason}"


def _build_decision(project: str, command: str, verdict: dict) -> dict:
    """Rich owner-decision object for a genuine (non-safe) prompt. Carries the
    EXACT blocker text and a decision type so escalation can be reserved for real
    external/product/financial decisions."""
    dtype = decision_type(command, verdict.get("category", ""))
    return {
        "project": project,
        "action": command,
        "blocker_text": _exact_blocker_text(command, verdict),
        "decision_type": dtype,
        "why_blocked": f"not auto-continuable: {verdict.get('reason')}",
        "risk": ("external / product / financial effect" if dtype in ("external", "financial")
                 else "internal — review; no external effect"),
        "recommended": "review the exact command and approve only if intended",
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
                # A proven read-only internal wait — NOT an owner blocker. Carry the
                # EXACT command so the brief's `waiting_safe_internal` section can
                # show it (never `denied` / category-only), and mark it internal.
                return {"state": "waiting_safe_approval", "command": command,
                        "prompt_hash": verdict["hash"], "fresh": True, "verdict": verdict,
                        "decision_type": "internal",
                        "blocker_text": f"{command} — proven read-only; auto-resolving (no owner action)"}
            # genuine owner decision — carry the EXACT blocker text + decision type
            dec = _build_decision(cfg.get("project", session), command, verdict)
            return {"state": "waiting_owner", "command": command, "prompt_hash": verdict["hash"],
                    "blocker_category": verdict["category"], "blocker_text": dec["blocker_text"],
                    "decision_type": dec["decision_type"], "fresh": False, "decision": dec}
        dec = _build_decision(cfg.get("project", session), "(unreadable prompt)",
                              {"reason": "prompt not machine-readable", "category": "unknown",
                               "hash": pr.command_hash(tail[-200:])})
        return {"state": "waiting_owner", "blocker_category": "unextractable",
                "blocker_text": dec["blocker_text"], "decision_type": "internal",
                "fresh": False, "decision": dec}

    if observed == "externally_blocked":
        # Exact blocker from the pane, not a bare "external" category.
        tail = agent.get("recent_activity") or agent.get("_tail") or ""
        m = _re.search(r"(input[_ ]required|verification key|waiting for [^\n]{0,80}|"
                       r"rate.?limit|quota|vendor[^\n]{0,60})", tail, _re.I)
        btext = m.group(0).strip() if m else "blocked on an external dependency"
        return {"state": "externally_blocked", "blocker_category": "external",
                "blocker_text": btext, "decision_type": "external", "fresh": False}

    if observed == "idle":
        ev = _completion_evidence(session, root, agent)
        if ev:
            return {"state": "completed", "completion_evidence": ev, "report_path": ev["report_path"],
                    "fresh": True}
        return {"state": "idle", "fresh": False}

    # working / shell_running / waiting_input / failed. A live shell command is
    # active work (fresh); waiting_input is at-rest (owner must submit).
    return {"state": observed, "fresh": observed in ("working", "shell_running")}


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
            "blocker_text": d.get("blocker_text"),
            "decision_type": d.get("decision_type"),
            "context_pct": None,
            "context_tier": None,
            "exec_mode": ac.detect_exec_mode(agent.get("_tail") or ""),
        }

        # Execution-mode control: keep a MANAGED-AUTO agent in `auto mode on` so
        # routine work never stalls on a permission prompt. Detection is recorded
        # for EVERY agent (above); the Shift+Tab restore fires only for an auto
        # session, only at a task boundary (working/idle — never mid-dialog so a
        # pending owner decision is untouched), and never for held/monitor. A mode
        # toggle is not a command approval — all resolver/owner gates still apply.
        if (AUTO_MODE_ENFORCE and cfg.get("mode") == "auto" and approve
                and state in ("working", "idle")
                and rec["exec_mode"] not in ac.NONSTALL_MODES + ("unknown",)):
            try:
                res = ac.ensure_auto_mode(key)
                rec["exec_mode"] = res.get("mode", rec["exec_mode"])
                if res.get("action") == "restored":
                    resolved.append({"agent": key, "action": "auto_mode_restored",
                                     "mode": res["mode"], "presses": res.get("presses")})
            except Exception as e:  # noqa: BLE001
                rec["exec_mode"] = f"error:{str(e)[:40]}"

        # A previously budget-paused agent whose limits have now reset. Verify it
        # actually resumed; never re-dispatch (that would duplicate in-flight work).
        budget_reset = (prev.get("state") == "paused_by_budget" and not budget_locked())

        # auto-continue a provably-safe prompt
        if state == "waiting_safe_approval" and cfg.get("mode") == "auto" and approve and not budget_locked():
            outcome = _resolve_safe(key, d.get("command"), rec)
            rec.update(outcome["rec_updates"])
            resolved.append(outcome["summary"])
        elif budget_reset and state in ("working", "idle"):
            res = _verify_after_budget_reset(state, rec, prev)
            rec["notification_state"] = res["notification_state"]
            if res.get("escalation"):
                escalations.append(res["escalation"])
        elif state == "waiting_owner":
            # Escalate to the owner ONLY for a real external/product/financial
            # decision. An internal, merely-unrecognised prompt is left for the
            # owner but does NOT raise a Telegram alert (visible in dashboard/brief).
            dtype = d.get("decision_type") or "internal"
            if dtype in ("external", "financial"):
                already_escalated = (prev.get("prompt_hash") == rec["prompt_hash"]
                                     and prev.get("notification_state") in ("needs_escalation", "escalated"))
                if already_escalated:
                    rec["notification_state"] = prev.get("notification_state")
                else:
                    rec["notification_state"] = "needs_escalation"
                    escalations.append({"agent": key, "project": rec["project"],
                                        "decision": rec["decision"], "decision_type": dtype})
            else:
                rec["notification_state"] = "owner_review_internal"   # no Telegram; shown in UI/brief
        elif state == "completed" and cfg.get("mode") == "auto" and cfg.get("advance_phases"):
            # Supervision: a completed phase must never sit unattended. If the next
            # phase can auto-advance (has exact approved text) the sweep handles it;
            # otherwise the owner is asked ONCE to record the text / decide.
            sup = _supervise_completed(session, cfg, rec, prev, nxt)
            rec["notification_state"] = sup["notification_state"]
            if sup.get("escalation"):
                escalations.append(sup["escalation"])

        # Context-budget control (Commander hardening). Detection + tiering runs for
        # every agent (visibility); the actual /clear rotation only dispatches for an
        # `auto` session at a safe boundary — held/monitor agents are never rotated.
        try:
            from core import agent_context_budget as ctxb
            ctx_act = (cfg.get("mode") == "auto")          # monitor/hold = detection-only
            # Detection/tiering always runs (visibility). The actual `/clear`
            # rotation stays OFF behind its own flag until a dry-run + live canary
            # prove it never clears unexpectedly — separate from safe-approval.
            ctx_rotate_enabled = os.getenv("AGENT_CONTEXT_ROTATE_ENABLED", "0") not in ("0", "false", "no", "")
            # Per-session opt-in scopes the autonomous /clear risk: dispatch fires
            # ONLY for a managed-auto session that explicitly sets `context_rotate:
            # true` in config (so a rollout can be limited to one agent at a time).
            session_rotate_opt_in = bool(cfg.get("context_rotate"))
            ctx_dispatch = (ctx_act and approve and not budget_locked()
                            and ctx_rotate_enabled and session_rotate_opt_in)
            # DELIVERY: surface the checkpoint/completion/waiting-external event on
            # DETECTION (independent of dispatch gates) into the durable event log,
            # so Owner OS/ChatGPT see it within one sweep even when /clear is
            # deferred or disabled. Deduped so it is not re-emitted every 45s.
            if ctx_act:
                try:
                    sev = ctxb.detect_surfaceable_event(agent, rec, cfg, agent.get("_tail") or "", prev=prev)
                    if sev:
                        # Project identity comes from the resolved active-task/command
                        # context (sev["project"]), NOT the stale session/record project.
                        ev_project = sev.get("project") or rec.get("project") or session
                        payload = {**sev, "project": ev_project, "exec_mode": rec.get("exec_mode"),
                                   "state": state, "detected_at": _now_iso()}
                        is_new = ac.record_commander_event(key, ev_project,
                                                           sev["event_type"], payload,
                                                           dedup_key=sev.get("dedup_key", ""))
                        if is_new:
                            escalations.append({"agent": key, "project": ev_project,
                                                "event": sev["event_type"], "commander_event": payload})
                    # RETRACTION: a completion was surfaced but the agent is active
                    # again → emit a correction so a false completion is walked back.
                    elif fresh and state == "working":
                        last = ac.latest_commander_event(key, within_secs=900)
                        if last and last["event_type"].startswith("task_completed"):
                            corr = {"corrects_event_id": last["id"], "corrected_event": last["event_type"],
                                    "reason": "agent is active again — prior completion was premature/false",
                                    "project": (last.get("payload") or {}).get("project"), "detected_at": _now_iso()}
                            if ac.record_commander_event(key, corr.get("project") or session,
                                                         "completion_retracted", corr,
                                                         dedup_key=f"retract:{last['id']}"):
                                escalations.append({"agent": key, "event": "completion_retracted",
                                                    "commander_event": corr})
                except Exception:  # noqa: BLE001
                    pass
            cres = ctxb.evaluate(agent, cfg, rec, prev, act=ctx_act, dispatch=ctx_dispatch)
            rec["context_pct"] = cres.get("context_pct")
            rec["context_tier"] = cres.get("context_tier")
            # Only fill notification_state if the state-machine left it empty, so a
            # waiting_owner / completed / budget-reset signal always wins.
            if cres.get("notification_state") and not rec.get("notification_state"):
                rec["notification_state"] = cres["notification_state"]
            cns = cres.get("notification_state", "")
            # Rotation / completion events are FIRST-CLASS owner/ChatGPT events —
            # surface them (project, completion_class, remaining subphase, handoff,
            # context, action) whether or not a /clear fired.
            _ROT_EVENTS = ("safe_rotation_due", "context_rotated_checkpoint",
                           "task_completed_waiting_external_action",
                           "task_completed_no_remaining_work")
            if cns in _ROT_EVENTS and cres.get("rotation"):
                r = dict(cres["rotation"])
                r.update({"agent": key, "event": cns,
                          "action_taken": r.get("action", "surfaced (dispatch gated off)"),
                          "exec_mode": rec.get("exec_mode")})
                escalations.append({"agent": key, "project": rec["project"],
                                    "event": cns, "rotation": r})
            if cns.startswith("context_rot") or cns in _ROT_EVENTS:
                if cns not in ("context_rotate_deferred", "context_rotate_deferred_unsettled"):
                    resolved.append({"agent": key, "context_tier": rec["context_tier"],
                                     "context_pct": rec["context_pct"],
                                     "action": cns, "handoff_path": cres.get("handoff_path"),
                                     "rotation": cres.get("rotation")})
        except Exception as e:  # noqa: BLE001
            rec["context_tier"] = f"error:{str(e)[:60]}"

        # Watcher / stuchalka: an idle agent sitting on an UNFINISHED assigned task
        # is safely resumed on the SAME pane (never duplicated), or — if resume is
        # not permitted / retries are exhausted — raised as ONE owner blocker.
        # Deduped by (agent, condition, evidence_hash) with a long window, so an
        # unchanged stall never re-alerts; any real change makes a new key.
        try:
            from core import agent_watcher as _watch
            from core import orchestrator_plan as _wplan
            assigned = _wplan.assigned_unfinished_task(key)
            stall = _watch.detect(agent_key=key, alive=True, state=state,
                                  assigned_task=assigned, now_ts=_now_ts(),
                                  pane_tail=agent.get("_tail") or "",
                                  resume_count=rec.get("retry_count") or 0)
            if stall:
                decision = _watch.decide(stall, mode=cfg.get("mode") or "monitor",
                                         approve=approve, budget_locked=budget_locked())
                dk = _watch.dedup_key(stall)
                if decision["action"] == "resume":
                    if approve:
                        skey = f"watch-resume:{stall['task_id']}:{decision['resume_count']}"
                        try:
                            ac.agent_send(key, stall["task_text"] or "", idempotency_key=skey)
                        except Exception:  # noqa: BLE001
                            pass
                        rec["retry_count"] = decision["resume_count"]
                    if ac.record_commander_event(key, rec["project"], "agent_resumed_same_conversation",
                                                 {**stall, **decision, "detected_at": _now_iso()},
                                                 dedup_key=dk, dedup_window_secs=86400):
                        resolved.append({"agent": key, "action": "watch_resume_same_conversation",
                                         "task_id": stall["task_id"], "attempt": decision["resume_count"]})
                else:
                    rec["notification_state"] = rec.get("notification_state") or "agent_recovery_failure"
                    ev_type = _watch.event_type(decision)   # agent_recovery_failure (NOT owner decision)
                    if ac.record_commander_event(key, rec["project"], ev_type,
                                                 {**stall, **decision, "classification": "agent_recovery_failure",
                                                  "detected_at": _now_iso()},
                                                 dedup_key=dk, dedup_window_secs=86400):
                        escalations.append({"agent": key, "project": rec["project"],
                                            "event": ev_type, "classification": "agent_recovery_failure",
                                            "condition": stall["condition"], "reason": decision["reason"]})
        except Exception:  # noqa: BLE001
            pass

        # Transition events: ONE deduped owner event when the agent enters a
        # notable state (completed / waiting_input / genuine blocker / stall /
        # process death / recovery), carrying pane evidence. Fires for EVERY agent
        # incl. those outside the orchestrator plan (ACAP / Mess), keyed by
        # (agent, event, evidence_hash) so an unchanged state never re-notifies.
        try:
            from core import agent_watcher as _watch
            _tev = _watch.transition_event(prev.get("state"), state, agent=key,
                                           evidence=agent.get("_tail") or "")
            if _tev and ac.record_commander_event(
                    key, rec["project"] or session, _tev["event_type"],
                    {**_tev, "detected_at": _now_iso(), "evidence": (agent.get("_tail") or "")[-300:]},
                    dedup_key=_tev["dedup_key"], dedup_window_secs=86400):
                (escalations if _tev.get("notify") else resolved).append(
                    {"agent": key, "project": rec["project"] or session,
                     "event": _tev["event_type"], "transition": _tev})
        except Exception:  # noqa: BLE001
            pass

        # Actionable waiting transition: the SAME edge as above, but published as a durable
        # CTO event so the wake bridge is consulted. The commander mirror above reaches the
        # legacy notifier only; a live agent that stopped and is waiting for a response had
        # no CTO event at all, so wake selection could not see the stall (2026-08-13 03:58).
        # Deduped by target + progress fingerprint inside the module: steady waiting is
        # announced once, and waiting again after new progress is a new event.
        try:
            from core.control_plane import waiting_transitions as _wt
            _wt.observe(target=key, prev_state=prev.get("state"), cur_state=state,
                        project=rec["project"] or session,
                        conversation_id=str(rec.get("conversation_id") or ""),
                        progress=agent.get("_tail") or "",
                        evidence=agent.get("_tail") or "")
        except Exception:  # noqa: BLE001 — observation must never break the sweep
            pass

        # Source-side retraction: an agent ACTIVE/COMPLETED again → retract its still-
        # unacked stale condition events so the notifier never delivers a contradicted
        # alert (the notifier's pre-delivery revalidation stays as a second barrier).
        try:
            _rids = ac.retract_stale_condition_events(key, state, reason=f"agent {state} again")
            if _rids:
                resolved.append({"agent": key, "action": "retracted_stale_events",
                                 "count": len(_rids), "ids": _rids})
        except Exception:  # noqa: BLE001
            pass

        _upsert(rec)
        results.append({"agent": key, "state": state, "project": rec["project"]})
        full_records.append(rec)

    # Watcher / stuchalka (dead agents): the live sweep skips exited panes. If one
    # died with an UNFINISHED assigned task, raise ONE owner blocker — never recreate
    # it (that would duplicate the agent). Deduped by evidence like the live path.
    try:
        from core import agent_watcher as _watch
        from core import orchestrator_plan as _wplan
        for agent in inv.get("agents", []):
            if not agent.get("is_agent") or agent.get("alive"):
                continue
            dkey = agent["target"]
            dsession = dkey.split(":", 1)[0]
            assigned = _wplan.assigned_unfinished_task(dkey)
            stall = _watch.detect(agent_key=dkey, alive=False, state="exited",
                                  assigned_task=assigned, now_ts=_now_ts())
            if not stall:
                continue
            dcfg = _session_cfg(dsession)
            decision = _watch.decide(stall, mode=dcfg.get("mode") or "monitor",
                                     approve=approve, budget_locked=budget_locked())
            if ac.record_commander_event(dkey, dcfg.get("project") or dsession,
                                         _watch.EVENT_RECOVERY_FAILURE,
                                         {**stall, **decision, "classification": "agent_recovery_failure",
                                          "detected_at": _now_iso()},
                                         dedup_key=_watch.dedup_key(stall), dedup_window_secs=86400):
                escalations.append({"agent": dkey, "project": dcfg.get("project") or dsession,
                                    "event": _watch.EVENT_RECOVERY_FAILURE,
                                    "classification": "agent_recovery_failure",
                                    "condition": "exited_unfinished", "reason": decision["reason"]})
    except Exception:  # noqa: BLE001
        pass

    # Reaper: reconcile records whose tmux session VANISHED (pane gone, stale record).
    # Atomically mark them vanished; emit ONE deduped owner event only if the record
    # carried approved unfinished work. Never recreates an agent or touches a pane.
    try:
        live_agents = inv.get("agents") or []
        # Derive live sessions from the actual panes; only reconcile when we have a
        # real inventory (never mass-reap on a transient empty/failed read).
        live_sessions = {a.get("session") for a in live_agents if a.get("session")}
        if not live_agents:
            raise RuntimeError("empty inventory — skip reaping")

        def _emit_vanished(agent_key, session, info):
            cfg = _session_cfg(session)
            ac.record_commander_event(
                agent_key, cfg.get("project") or session, "agent_vanished_unfinished",
                {**info, "classification": "vanished_unfinished", "detected_at": _now_iso()},
                dedup_key=f"vanished:{session}", dedup_window_secs=604800)
            escalations.append({"agent": agent_key, "project": cfg.get("project") or session,
                                "event": "agent_vanished_unfinished", "reason": info})

        reaped = reap_vanished(live_sessions, emit=_emit_vanished)
        if reaped:
            resolved.append({"event": "sessions_reaped", "count": len(reaped),
                             "sessions": [r["session"] for r in reaped]})
    except Exception:  # noqa: BLE001
        pass

    # Cross-phase auto-progress (guarded, exact-approved-text-only, idempotent).
    advances = {"enabled": False, "results": []}
    try:
        from core import agent_phase_advance
        advances = agent_phase_advance.sweep(full_records, _session_cfg, dispatch=approve)
    except Exception as e:  # noqa: BLE001
        advances = {"enabled": True, "error": str(e)[:200], "results": []}

    # THE ORCHESTRATOR: own the durable goal → plan → task queue; detect completion
    # and dispatch the next dependency-satisfied task to its assigned EXISTING agent.
    plan_tick = {}
    try:
        plan_tick = _orchestrate(full_records, approve)
        for c in plan_tick.get("completions", []):
            ac.record_commander_event(c.get("task_id") and f"task#{c['task_id']}" or "orchestrator",
                                      c.get("project"), "orchestrator_task_completed",
                                      c, dedup_key=f"orchtask:{c.get('task_id')}")
        for d in plan_tick.get("dispatches", []):
            escalations.append({"event": "orchestrator_dispatch", "commander_event": d})
    except Exception as e:  # noqa: BLE001
        plan_tick = {"error": str(e)[:200]}

    return {"ok": True, "agents": len(results), "records": results,
            "resolved": resolved, "escalations": escalations, "phase_advances": advances,
            "orchestrator": plan_tick}


def _orchestrate(full_records: list, approve: bool) -> dict:
    """Run one goal/plan/queue tick. Dispatch = send the exact approved task text to
    the ASSIGNED existing agent (never creates one) at a safe boundary."""
    from core import orchestrator_plan as plan
    by_session = {r["session"]: r for r in full_records}

    def agent_available(agent_ref: str) -> bool:
        # agent_ref is a session name (or session:pane). It must be an EXISTING,
        # at-rest agent (idle/completed) — never dispatch onto a working/waiting one.
        sess = agent_ref.split(":", 1)[0]
        rec = by_session.get(sess)
        return bool(rec) and rec.get("state") in ("idle", "completed")

    def send(agent_ref: str, task_text: str):
        if not approve:
            return None
        sess = agent_ref.split(":", 1)[0]
        target = agent_ref if ":" in agent_ref else f"{sess}:0.0"
        rec = by_session.get(sess)
        if not rec or rec.get("state") not in ("idle", "completed"):
            return None
        try:
            import hashlib
            key = "orchdispatch:" + hashlib.sha256(f"{target}\x1f{task_text}".encode()).hexdigest()[:20]
            return ac.agent_send(target, task_text, idempotency_key=key)
        except Exception:  # noqa: BLE001
            return None

    return plan.tick(dispatch=approve, agent_available=agent_available, send=send)


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


def _verify_after_budget_reset(state: str, rec: dict, prev: dict) -> dict:
    """A budget/rate-limit pause has cleared. Verify the previously-approved work
    continued on its own; do NOT re-issue any command (that would duplicate the
    in-flight phase). Only report status.
      * working → confirmed self-resumed (informational, no owner alert);
      * idle    → did NOT resume; ask the owner ONCE (internal, not a paid action)."""
    if state == "working":
        # clear any stale retry bookkeeping from the pause.
        rec["retry_count"] = 0
        return {"notification_state": "resumed_after_budget"}
    # idle: did NOT resume on its own. This is operational/internal, so per the
    # escalation rule it is surfaced in the dashboard/brief (blocker_text) WITHOUT a
    # Telegram alert. No command is re-issued — that would duplicate approved work.
    rec["blocker_text"] = ("paused_by_budget cleared but the agent stayed idle — "
                           "previously-approved work needs a manual nudge (no auto re-dispatch)")
    rec["decision_type"] = "internal"
    return {"notification_state": "stalled_after_budget"}


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
    """Read-only orchestrator status for all tracked agents, plus recent durable
    Commander events (checkpoint / completion / waiting-external / owner-decision)
    so Owner OS / ChatGPT can SEE them without missing a transient sweep."""
    try:
        events = ac.list_commander_events(since_epoch=_now_ts() - 86400, limit=50)
    except Exception:  # noqa: BLE001
        events = []
    try:
        from core import orchestrator_plan as _plan
        plan_status = _plan.status()
    except Exception as e:  # noqa: BLE001
        plan_status = {"state": "error", "error": str(e)[:120]}
    recs = all_records()
    # Direct-agent truth: existing tmux agents actively working OUTSIDE the plan
    # (e.g. ACAP/Capacity, Mess) so status/portfolio never reads running=0 /
    # queue=0 while a direct agent is working. Never dispatched to, never touched.
    _ACTIVE = {"working", "shell_running", "waiting_input"}
    direct_active = [{"session": r.get("session"), "state": r.get("state"),
                      "project": r.get("project"), "cwd": r.get("claude_cwd") or r.get("cwd")}
                     for r in recs if r.get("state") in _ACTIVE]
    if isinstance(plan_status, dict):
        plan_status["direct_active_count"] = len(direct_active)
        plan_status["direct_active"] = direct_active
    try:
        from core import agent_continuation_watchdog as _cw
        cw_health = _cw.health()
    except Exception:  # noqa: BLE001
        cw_health = {"enabled": None}
    return {"states": ORCH_STATES, "budget_locked": budget_locked(),
            "records": recs, "commander_events": events,
            "unacked_events": [e for e in events if not e["acknowledged"]],
            "orchestrator": plan_status,
            "direct_active_agents": direct_active,
            "direct_active_count": len(direct_active),
            "continuation_watchdog": cw_health,
            "checked_at": _now_iso()}


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
        # Direct-agent lifecycle: reliable completion / interruption events for
        # tmux agents OUTSIDE the plan (the inline path structurally cannot cover
        # baseline completion or dead panes). Additive + best-effort — a failure
        # here never breaks the orchestrator sweep.
        try:
            from core import direct_agent_lifecycle as _dal
            if _dal.ENABLED:
                inv = await asyncio.to_thread(ac.agent_list)
                dres = await asyncio.to_thread(_dal.sweep, inv)
                if dres.get("events"):
                    log.info(f"direct lifecycle: emitted={len(dres['events'])} "
                             f"{[e['event_type'] for e in dres['events']]}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"direct lifecycle sweep error: {e}")
        await asyncio.sleep(interval)
