"""Blocker-resolution events.

When a SYSTEM blocker clears (an actuation that was blocked now verifies, an agent that
was stuck resumes), emit a correlated `blocker_resolved` event (with a `resolves` link),
close the system gate(s), and mark related notifications resolved — so the CTO inbox and
owner see the all-clear, not just the alarm.

NEVER auto-resolves an owner-DECISION gate (scope / business / unverified_owner_decision) —
those require a verified owner_decision (see provenance).
"""
from __future__ import annotations

from core.control_plane import api
from core.control_plane.cto import emit

# system blocker gates that may auto-clear when their condition resolves
SYSTEM_KINDS = ("actuation_failed", "continuation", "actuation", "recovery_blocked")


def resolve_blocker(agent_id: str, *, reason: str = "", resolves: int = 0,
                    correlation_id: str = "", conn=None) -> dict:
    """Close any open SYSTEM blocker gates for `agent_id` and emit a resolution event."""
    closed = api.close_gates(agent_id, SYSTEM_KINDS, conn=conn)
    ev = emit("resolution", "blocker_resolved", agent_id=agent_id, severity="info",
              owner_action_required=False,
              payload={"reason": reason, "closed_gates": closed},
              action_taken="system blocker cleared", resolves=resolves,
              correlation_id=correlation_id,
              dedup_key=f"resolved:{agent_id}:{resolves or reason}", push=False, conn=conn)
    return {"agent_id": agent_id, "closed_gates": closed, "event_id": ev["event_id"]}
