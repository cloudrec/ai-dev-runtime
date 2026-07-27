"""Watcher / stuchalka — detect an EXISTING agent that stalled on an unfinished
assigned task and choose the single safe action.

Conditions detected:
  * idle_unfinished   — agent is idle (past a dwell) while its assigned task is
                        still dispatched/in-progress → SAFELY RESUME the same
                        conversation/pane (bounded retries), never a duplicate.
  * exited_unfinished — agent pane exited/died with an unfinished task → cannot
                        resume without recreating (a duplicate), so raise ONE
                        owner blocker instead.

Alert discipline: every surfaced condition is keyed by (agent, condition,
evidence_hash). The evidence hash folds in task id, state, liveness and a tail
of the pane, so ANY real change (state change, new blocker, completion, rapid
context growth, repetition, unrelated work) yields a new key and notifies, while
an unchanged condition keeps the same key and is suppressed — it never re-alerts
every sweep/hour. All logic here is pure and injectable for tests; the caller
wires tmux/agent_control side effects.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

# An idle agent must stay idle at least this long PAST its task dispatch before it
# counts as stalled — below this it is just normal between-turn / tool latency.
STALL_DWELL_SECS = 180
# After this many auto-resumes with the task still unfinished, stop resuming and
# ask the owner once (avoids an infinite resume loop = the "repetition" signal).
MAX_RESUMES = 2

UNFINISHED_TASK_STATES = ("dispatched", "in_progress")

# Event types. A crash / failed-resume / idle-with-unfinished-work is an AGENT
# RECOVERY FAILURE (the agent could not make progress on its own) — it is NOT an
# owner product decision and must never be surfaced as one.
EVENT_RESUMED = "agent_resumed_same_conversation"
EVENT_RECOVERY_FAILURE = "agent_recovery_failure"


def event_type(decision: dict) -> str:
    """Map a decision to its Commander event type."""
    return EVENT_RESUMED if decision.get("action") == "resume" else EVENT_RECOVERY_FAILURE


def evidence_hash(agent_key: str, condition: str, evidence: str) -> str:
    return hashlib.sha256(
        f"{agent_key}\x1f{condition}\x1f{evidence}".encode()).hexdigest()[:16]


def _parse_ts(value) -> Optional[float]:
    """orchestrator_plan stores timestamps as ISO-8601 (UTC). Return epoch or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return None


def detect(*, agent_key: str, alive: bool, state: str, assigned_task: Optional[dict],
           now_ts: float, pane_tail: str = "", resume_count: int = 0,
           dwell_secs: int = STALL_DWELL_SECS) -> Optional[dict]:
    """Return a stall descriptor, or None when the agent is fine.

    `assigned_task` is the orchestrator task dict assigned to this agent (or None).
    Only an UNFINISHED (dispatched/in-progress) task can stall.
    """
    if not assigned_task or assigned_task.get("status") not in UNFINISHED_TASK_STATES:
        return None

    if not alive:
        condition = "exited_unfinished"
    elif state == "idle":
        dispatched = _parse_ts(assigned_task.get("dispatched_at"))
        # Require a real dwell after dispatch so a mid-turn lag never trips it.
        if dispatched is None or (now_ts - dispatched) < dwell_secs:
            return None
        condition = "idle_unfinished"
    else:
        return None  # working / waiting_* are handled elsewhere, not a stall

    ev = evidence_hash(
        agent_key, condition,
        f"{assigned_task.get('id')}|{state}|{alive}|{(pane_tail or '')[-160:]}")
    return {"agent": agent_key, "condition": condition,
            "task_id": assigned_task.get("id"), "task_title": assigned_task.get("title"),
            "task_text": assigned_task.get("task_text"), "evidence_hash": ev,
            "resume_count": resume_count, "state": state, "alive": alive}


def decide(stall: dict, *, mode: str, approve: bool, budget_locked: bool) -> dict:
    """Choose exactly ONE action for a detected stall.

    An idle agent with an unfinished task, under managed-auto at a safe boundary
    and within the resume budget, is RESUMED on the same pane. Everything else —
    an exited/dead pane, a non-auto session, a budget lock, or exhausted
    retries — becomes exactly one owner blocker (never a duplicate agent).
    """
    if (stall["condition"] == "idle_unfinished" and mode == "auto" and approve
            and not budget_locked and stall.get("resume_count", 0) < MAX_RESUMES):
        return {"action": "resume",
                "reason": "idle with unfinished assigned task — resume same conversation",
                "resume_count": stall.get("resume_count", 0) + 1}

    reason = {
        "exited_unfinished":
            "agent pane exited/died with an unfinished assigned task — cannot resume "
            "without creating a duplicate; owner must restart it manually or reassign.",
        "idle_unfinished":
            f"still unfinished after {MAX_RESUMES} auto-resumes (or resume not permitted) "
            "— owner input required.",
    }.get(stall["condition"], "unfinished task needs owner input")
    return {"action": "owner_blocker", "reason": reason}


def dedup_key(stall: dict) -> str:
    """Alert key: same (condition, evidence_hash) → suppressed; any change → new."""
    return f"watch:{stall['condition']}:{stall['evidence_hash']}"


# ── state-transition events ──────────────────────────────────────────────────
# A transition INTO one of these states is one deduped owner event (with evidence).
_ENTER_EVENTS = {
    "completed": "agent_completed",
    "waiting_input": "agent_waiting_input",
    "externally_blocked": "agent_externally_blocked",   # genuine blocker
    "waiting_owner": "agent_owner_decision",             # genuine blocker
    "failed": "agent_process_failed",                    # dead/exited/test process killed
    "dead": "agent_process_failed",
}
_ACTIVE_STATES = {"working", "shell_running"}
_STUCK_STATES = {"failed", "dead", "externally_blocked", "waiting_owner", "waiting_input"}

# Which transition events warrant an owner NOTIFICATION (not just a durable log).
# Recovery + completion + genuine blockers + stalls + process death all do;
# waiting_input does too (a staged command needs the owner to act).
NOTIFY_EVENTS = {
    "agent_completed", "agent_waiting_input", "agent_externally_blocked",
    "agent_owner_decision", "agent_process_failed", "agent_unexpected_idle",
    "agent_recovered",
}


def transition_event(prev_state, cur_state, *, agent: str, evidence: str = "") -> Optional[dict]:
    """Return a deduped transition-event descriptor when the agent moved into a
    notable state, else None. Handles: entering completed / waiting_input /
    genuine blocker / process-death; a stall (active → idle with no completion);
    and recovery (stuck → active). Keyed by (agent, event, evidence_hash) so an
    unchanged state never re-notifies and any real change makes a new key."""
    if not prev_state or prev_state == cur_state:
        return None
    event = _ENTER_EVENTS.get(cur_state)
    if event is None:
        if cur_state == "idle" and prev_state in _ACTIVE_STATES:
            event = "agent_unexpected_idle"          # active → idle, no completion = stall
        elif cur_state in _ACTIVE_STATES and prev_state in _STUCK_STATES:
            event = "agent_recovered"
    if event is None:
        return None
    # Anti-spam: a flapping agent (working↔idle) would make a NEW evidence hash every
    # poll if the volatile pane tail were folded in, spamming Telegram. For the flappy
    # stall/recovery events, key ONLY on the transition (coarse) so repeated flaps
    # collapse to one alert per window. Completion / blocker / waiting_input keep the
    # pane evidence so genuinely distinct events (a new report, a new blocker) each
    # deliver.
    _COARSE = {"agent_unexpected_idle", "agent_recovered"}
    material = (f"{prev_state}->{cur_state}" if event in _COARSE
               else f"{prev_state}->{cur_state}|{(evidence or '')[-160:]}")
    eh = evidence_hash(agent, event, material)
    return {"event_type": event, "from_state": prev_state, "to_state": cur_state,
            "agent": agent, "evidence_hash": eh, "notify": event in NOTIFY_EVENTS,
            "dedup_key": f"transition:{event}:{eh}"}
