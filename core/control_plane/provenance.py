"""Owner-decision PROVENANCE invariant.

HARD RULE (P2/P3): no owner-gated action may proceed from raw pane text, a Claude UI
`User answered …` summary, a model default, a resumed transcript, or automation prose.
It may proceed ONLY from a durable, correlated `owner_decision` carrying:
  source_channel · authenticated actor · timestamp · question/gate id · exact answer ·
  consumption state.

Unknown / missing / untrusted / mismatched / already-consumed provenance ⇒ the owner_gate
stays OPEN, the action is BLOCKED, and a critical inbox event is raised. This is what the
2026-08-03 `Stop selling, waitlist instead` incident requires: that string lived only in
the resumed pane transcript with no authenticated owner_decision, so it must never drive an
action.
"""
from __future__ import annotations

import uuid
from typing import Optional

from core.control_plane.api import _c, answer_gate, get_open_gates
from core.control_plane.cto import emit
from core.control_plane.store import now_iso

# Channels whose decisions can be trusted — an authenticated, out-of-band owner reply.
# Explicitly EXCLUDES pane/transcript/UI-summary/automation sources.
TRUSTED_CHANNELS = {"owner_api", "telegram_verified", "signed_owner_reply", "cto_authenticated"}
UNTRUSTED_SOURCES = {"pane_text", "ui_answer_summary", "resumed_transcript", "model_default",
                     "automation_prose"}


def _uid() -> str:
    return uuid.uuid4().hex[:16]


def record_owner_decision(*, question_id: str, source_channel: str, actor: str, answer: str,
                          authenticated: bool, gate_id: str = "", decided_at: str = "",
                          conn=None) -> dict:
    """Persist an owner decision with full provenance. Returns {id, trusted, reason}.
    A record from an untrusted source or without authentication is STORED (for audit)
    but marked not-trusted, so it can never resolve a gate."""
    conn, own = _c(conn)
    try:
        did = _uid()
        trusted = bool(authenticated) and source_channel in TRUSTED_CHANNELS \
            and source_channel not in UNTRUSTED_SOURCES and bool(answer)
        conn.execute(
            "INSERT INTO owner_decision(id,question_id,gate_id,source_channel,actor,"
            "authenticated,answer,decided_at,consumption_state,created_at) "
            "VALUES(?,?,?,?,?,?,?,?, 'pending', ?)",
            (did, question_id, gate_id, source_channel, actor, 1 if authenticated else 0,
             answer, decided_at or now_iso(), now_iso()))
        conn.commit()
        reason = "trusted" if trusted else (
            "unauthenticated" if not authenticated else
            f"untrusted_channel:{source_channel}" if source_channel not in TRUSTED_CHANNELS else
            "empty_answer")
        return {"id": did, "trusted": trusted, "reason": reason}
    finally:
        if own:
            conn.close()


def get_owner_decision(decision_id: str, conn=None) -> Optional[dict]:
    conn, own = _c(conn)
    try:
        r = conn.execute(
            "SELECT id,question_id,gate_id,source_channel,actor,authenticated,answer,"
            "decided_at,consumption_state FROM owner_decision WHERE id=?", (decision_id,)).fetchone()
        cols = ("id", "question_id", "gate_id", "source_channel", "actor", "authenticated",
                "answer", "decided_at", "consumption_state")
        if not r:
            return None
        d = dict(zip(cols, r))
        d["authenticated"] = bool(d["authenticated"])
        return d
    finally:
        if own:
            conn.close()


def verify_provenance(gate: dict, decision: Optional[dict]) -> dict:
    """Pure check: may `decision` resolve `gate`? Returns {ok, reason}."""
    if decision is None:
        return {"ok": False, "reason": "no_owner_decision"}
    if not decision.get("authenticated"):
        return {"ok": False, "reason": "unauthenticated_actor"}
    if decision.get("source_channel") not in TRUSTED_CHANNELS:
        return {"ok": False, "reason": f"untrusted_source:{decision.get('source_channel')}"}
    if not decision.get("answer"):
        return {"ok": False, "reason": "empty_answer"}
    # the decision must answer THIS gate's question (correlation id or gate id)
    q = gate.get("correlation_id") or gate.get("id")
    if decision.get("question_id") not in (q, gate.get("id")):
        return {"ok": False, "reason": "answer_to_wrong_question"}
    if decision.get("consumption_state") == "consumed":
        return {"ok": False, "reason": "duplicate_answer_already_consumed"}
    return {"ok": True, "reason": "verified"}


def resolve_gate_with_decision(gate_id: str, decision_id: str, conn=None) -> dict:
    """Resolve an owner gate ONLY with a verified, authenticated, correlated,
    not-yet-consumed owner_decision. Otherwise the gate stays OPEN, the action is
    BLOCKED, and a critical event is raised — never resolved from raw text."""
    conn, own = _c(conn)
    try:
        gate = next((g for g in get_open_gates(conn=conn) if g["id"] == gate_id), None)
        if gate is None:
            return {"resolved": False, "reason": "gate_not_open"}
        decision = get_owner_decision(decision_id, conn=conn)
        v = verify_provenance(gate, decision)
        if not v["ok"]:
            emit("provenance", "owner_gate_blocked", agent_id=gate.get("agent_id") or "",
                 severity="critical", owner_action_required=True,
                 payload={"gate_id": gate_id, "decision_id": decision_id, "reason": v["reason"]},
                 action_taken="BLOCKED — gate stays open (unverified provenance)",
                 correlation_id=gate.get("correlation_id"),
                 dedup_key=f"gateblock:{gate_id}:{v['reason']}", conn=conn)
            return {"resolved": False, "reason": v["reason"], "blocked": True}
        # verified → consume the decision (idempotent, no duplicate) and answer the gate
        conn.execute("UPDATE owner_decision SET consumption_state='consumed' WHERE id=?",
                     (decision_id,))
        conn.commit()
        ans = answer_gate(gate_id, decision["answer"], conn=conn)
        emit("provenance", "owner_gate_resolved", agent_id=gate.get("agent_id") or "",
             severity="high", payload={"gate_id": gate_id, "decision_id": decision_id,
                                       "answer": decision["answer"],
                                       "source": decision["source_channel"], "actor": decision["actor"]},
             action_taken="resolved from authenticated owner_decision",
             correlation_id=gate.get("correlation_id"), conn=conn)
        return {"resolved": True, "answer": decision["answer"], "gate": ans}
    finally:
        if own:
            conn.close()
