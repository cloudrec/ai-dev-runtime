#!/usr/bin/env python3
"""Native Claude Code lifecycle hooks -> durable Owner OS events.

WHY THIS EXISTS. Owner OS learns that a managed agent stopped by SCRAPING its tmux pane:
capture the text, classify it, wait for a dwell, guess. That path is what most of
2026-08-30 was spent repairing — prose that matched no detector, a background shell
masking a finished turn, an inventory flicker re-announcing an unchanged pane. Claude
Code itself knows all of these facts exactly, and this build (2.1.251) exposes them as
hooks. A hook is ground truth; a scraped pane is an inference.

DESIGN RULES, each learned from a defect this session:

  * `Stop` fires at the END OF EVERY TURN, not when an agent finally goes idle. Mapping it
    to a wake would page the owner after every reply. It is recorded as a ROUTINE turn
    boundary — durable structure, never a doorbell — and the existing quiescence rule
    still decides what counts as a stop worth waking for.
  * Only three things wake: the agent asking for input, a task completing, and a failure.
    Those are the classes Owner OS already routes and rate-limits, so nothing new has to
    be taught to the wake bridge.
  * A hook must NEVER break the session it observes. Every path exits 0, writes nothing to
    stdout, and swallows every exception. A supervisor that can crash its worker is worse
    than no supervisor.
  * Dedupe is by (session, event, distinguishing payload) so a retried or duplicated hook
    delivery cannot double-wake.

This is ADDITIVE. Nothing here removes or disables the tmux/quiescence path, which stays
as the fallback for crashes, older Claude versions, a missing peer socket, or a hook that
fails to fire at all.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, "/root/ai-dev-runtime")

# Event mapping. LEFT: what Claude Code tells us. RIGHT: the Owner OS class that already
# has routing, a lane and a rate limit. Nothing invents a new wake class.
#
# `agent_turn_stopped` and `agent_subagent_stopped` are RECORDS, not doorbells — they are
# in ROUTINE_EVENT_TYPES so `is_significant` refuses them a wake by name.
_ROUTINE = "info"

def _map(ev: str, payload: dict):
    """(owner-os event type, severity, owner_action_required) or None to ignore."""
    if ev == "Stop":
        return ("agent_turn_stopped", _ROUTINE, False)
    if ev == "SubagentStop":
        return ("agent_subagent_stopped", _ROUTINE, False)
    if ev == "StopFailure":
        # The turn ended in an error the session could not recover from.
        return ("agent_process_failed", "critical", True)
    if ev == "TaskCompleted":
        return ("task_completed", "high", False)
    if ev == "TeammateIdle":
        return ("agent_turn_stopped", _ROUTINE, False)
    if ev == "Notification":
        # The only branch that can produce an actionable wake, and only for the
        # notification types that genuinely mean a human is being asked.
        t = (payload.get("notification_type") or "").strip()
        if t in ("agent_needs_input", "idle_prompt"):
            return ("agent_waiting_input", "high", True)
        if t == "agent_completed":
            return ("task_completed", "high", False)
        return None
    return None


def _identity(payload: dict) -> dict:
    """Who this is about, in Owner OS's own terms."""
    cwd = (payload.get("cwd") or os.getcwd() or "").rstrip("/")
    project = cwd.rsplit("/", 1)[-1] if cwd else ""
    # A hook knows its SESSION; the tmux world knows targets. Carry both, and let the
    # registry resolve the route — routing stays centralized, no per-worker URLs.
    agent = (payload.get("teammate_name") or os.environ.get("OWNEROS_AGENT_TARGET")
             or f"session:{(payload.get('session_id') or '')[:12]}")
    return {"project": project, "agent": agent, "cwd": cwd,
            "session_id": payload.get("session_id") or ""}


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001 — a malformed payload must not break the session
        return 0
    try:
        ev = payload.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "")
        mapped = _map(ev, payload)
        if not mapped:
            return 0
        etype, severity, oar = mapped
        ident = _identity(payload)

        # Distinguishing content per class, so a repeat of the SAME fact dedupes but a new
        # fact does not. Mirrors the digest discipline the pane watcher already uses.
        distinct = {
            "Stop": (payload.get("last_assistant_message") or "")[-400:],
            "SubagentStop": (payload.get("last_assistant_message") or "")[-400:],
            "StopFailure": json.dumps(payload.get("error_details") or {}, sort_keys=True)[:400],
            "TaskCompleted": f"{payload.get('task_id','')}:{payload.get('task_subject','')}",
            "TeammateIdle": payload.get("teammate_name") or "",
            "Notification": f"{payload.get('notification_type','')}:{(payload.get('message') or '')[:200]}",
        }.get(ev, "")
        digest = hashlib.sha256(f"{ident['session_id']}|{ev}|{distinct}".encode()).hexdigest()[:16]

        body = {
            "source": "claude_hook",
            "hook_event": ev,
            "session_id": ident["session_id"],
            "cwd": ident["cwd"],
            "digest": digest,
            # Structured lifecycle detail, straight from the runtime rather than scraped.
            "last_assistant_message": (payload.get("last_assistant_message") or "")[:600],
            "background_tasks": payload.get("background_tasks"),
            "session_crons": payload.get("session_crons"),
            "stop_hook_active": payload.get("stop_hook_active"),
            "notification_type": payload.get("notification_type"),
            "message": (payload.get("message") or "")[:300],
            "task_id": payload.get("task_id"),
            "task_subject": payload.get("task_subject"),
            "teammate_name": payload.get("teammate_name"),
            "error_details": payload.get("error_details"),
        }
        body = {k: v for k, v in body.items() if v not in (None, "", {})}

        from core.control_plane import cto
        cto.emit("claude_hook", etype, project_id=ident["project"], agent_id=ident["agent"],
                 severity=severity, owner_action_required=oar, payload=body,
                 action_taken=f"{ident['agent']} [{ident['project'] or 'unmapped'}]: {ev}",
                 correlation_id=f"claudehook:{ident['session_id'][:12]}",
                 dedup_key=f"claudehook:{ident['session_id'][:12]}:{ev}:{digest}",
                 dedup_window_secs=900)
    except Exception:  # noqa: BLE001 — observation must never break the observed session
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
