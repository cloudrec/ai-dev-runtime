"""Edge-triggered actionable event: an EXISTING live agent is waiting for a response.

Owner OS could see an agent APPEAR (`new_agent_discovered`), FINISH (`completed`), DIE
(`agent_dead`) and it could see the owner being asked for a decision (`waiting_owner`). What
it could not see was the most common stall of all: an agent that is already running, already
known, already registered, which stops mid-task and waits for someone to answer its prompt.

On 2026-08-13 03:58 `payorch-sbp-resumed` did exactly that, repeatedly, and the owner pinged
the chat by hand every time. The nearest event in the log was id 3920,
`new_agent_discovered`, severity info — skipped for `cooldown_active` and below the wake
threshold anyway. `agent_watcher.transition_event` did produce an `agent_waiting_input`
descriptor, but the orchestrator routed it only to the legacy commander log, so it never
became a CTO event and the wake bridge was never consulted about it.

This module is that missing event, and the whole design problem it carries is EDGE vs LEVEL:

  * LEVEL — "the agent is waiting" — is true on every tick for as long as it waits. Emitting
    on level would put the owner's chat into a poke loop.
  * EDGE — "the agent has just BECOME blocked on something new" — is true once.

The edge is defined by a fingerprint of the agent's PROGRESS, not by the waiting state:

    fingerprint = H(target, conversation_id, progress evidence)

An agent that sits waiting keeps the same fingerprint and emits once. An agent that receives
an answer, does more work, and then waits again has new progress, so a new fingerprint, so a
new event — which is correct, because that is a genuinely new thing to answer. The last
fingerprint per target is durable, so a restart mid-wait does not re-emit.

Emit/observe only: never touches a pane, starts or stops nothing.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from core.control_plane import cto
from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

# States that mean "stopped, waiting for a response from outside". `waiting_owner` is
# deliberately NOT here: it is an owner decision gate, already carried by the event pipeline,
# and duplicating it would double-notify.
WAITING_STATES = frozenset({"waiting_input", "prompt_needs_response", "needs_response"})
# Transitioning FROM one of these is a real edge — the agent was making progress and stopped.
# waiting → waiting is not an edge, and is filtered by the state check before the fingerprint
# is ever consulted.
PROGRESS_STATES = frozenset({"working", "shell_running", "idle", "unknown", "recovered"})

EVENT_TYPE = "agent_waiting_input"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_waiting_fingerprint (
    target TEXT PRIMARY KEY, fingerprint TEXT, conversation_id TEXT,
    event_id INTEGER, at TEXT, ts REAL, emissions INTEGER DEFAULT 0
)
"""


def _conn(conn=None):
    conn, own = _c(conn)
    conn.execute(_SCHEMA)
    return conn, own


def fingerprint(target: str, conversation_id: str = "", progress: str = "") -> str:
    """Identity of "what this agent is now blocked on".

    The progress component is the agent's own evidence of forward motion — a conversation
    mtime, a turn count, a pane tail. Any of them works; what matters is that it CHANGES when
    the agent does more work and does NOT change while it sits still. The tail is truncated
    so that a redraw of the same content is not mistaken for progress.
    """
    material = f"{target}\x1f{conversation_id or ''}\x1f{(progress or '').strip()[-240:]}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def is_waiting(state: str) -> bool:
    return (state or "").strip() in WAITING_STATES


def is_edge(prev_state: str, cur_state: str) -> bool:
    """A transition INTO waiting from a state that was not waiting.

    A missing prior state is NOT an edge. On the first sweep after a restart every agent
    looks "new", and treating that as a transition would poke the chat for every agent that
    happened to be waiting at the time — a restart storm, not a stall.
    """
    if not is_waiting(cur_state):
        return False
    if not prev_state:
        return False
    return not is_waiting(prev_state)


def observe(*, target: str, prev_state: str, cur_state: str, project: str = "",
            conversation_id: str = "", progress: str = "", evidence: str = "",
            emit_fn=None, conn=None, now: Optional[float] = None) -> dict:
    """Record one observed state transition; emit the actionable event only on a fresh edge.

    Returns {"emitted": bool, "reason": str, ...}. Never raises for control flow — a
    monitoring path that can break the sweep is worse than one that misses a wake.
    """
    now = now if now is not None else now_ts()
    if not is_waiting(cur_state):
        # Not waiting. Do NOT clear the fingerprint: the record is what makes the next
        # waiting edge comparable, and it is superseded by fingerprint, not by absence.
        return {"emitted": False, "reason": "not_waiting", "target": target}
    if not is_edge(prev_state, cur_state):
        return {"emitted": False, "reason": "not_a_transition_into_waiting",
                "target": target, "prev_state": prev_state}

    fp = fingerprint(target, conversation_id, progress)
    emit = emit_fn or cto.emit
    conn, own = _conn(conn)
    try:
        prior = conn.execute(
            "SELECT fingerprint,event_id,emissions FROM agent_waiting_fingerprint "
            "WHERE target=?", (target,)).fetchone()
        if prior and prior[0] == fp:
            # Steady waiting: same block, already announced. This is the anti-spam invariant —
            # it holds across restarts because the fingerprint is in the database, not in a
            # process that dies.
            return {"emitted": False, "reason": "unchanged_waiting_fingerprint",
                    "target": target, "fingerprint": fp,
                    "event_id": (int(prior[1]) if prior[1] else None)}

        ev = emit("waiting_transitions", EVENT_TYPE, project_id=project, agent_id=target,
                  severity="high", owner_action_required=True,
                  payload={"target": target, "from_state": prev_state, "to_state": cur_state,
                           "conversation_id": conversation_id, "fingerprint": fp,
                           "evidence": (evidence or "")[-300:],
                           "note": "live agent stopped and is waiting for a response"},
                  action_taken=f"{target}: {prev_state} → {cur_state}, waiting for a response",
                  correlation_id=f"waiting:{target}",
                  # Keyed by the fingerprint, so the dedupe WINDOW never has to be guessed:
                  # a distinct block is a distinct key and is never collapsed by time, while
                  # an identical one is refused by the fingerprint check above regardless.
                  dedup_key=f"waiting:{target}:{fp}", dedup_window_secs=86400, conn=conn)
        eid = ev["event_id"]
        conn.execute(
            "INSERT INTO agent_waiting_fingerprint (target,fingerprint,conversation_id,"
            "event_id,at,ts,emissions) VALUES (?,?,?,?,?,?,1) "
            "ON CONFLICT(target) DO UPDATE SET fingerprint=excluded.fingerprint, "
            "conversation_id=excluded.conversation_id, event_id=excluded.event_id, "
            "at=excluded.at, ts=excluded.ts, emissions=emissions+1",
            (target, fp, conversation_id, int(eid), now_iso(), now))
        conn.commit()
        return {"emitted": True, "reason": "actionable_waiting_transition", "target": target,
                "event_id": int(eid), "fingerprint": fp, "from_state": prev_state,
                "to_state": cur_state,
                "superseded_fingerprint": (prior[0] if prior else None)}
    finally:
        if own:
            conn.close()


def last_seen(target: str, conn=None) -> Optional[dict]:
    """The durable per-target record — what this agent was last announced as blocked on."""
    conn, own = _conn(conn)
    try:
        r = conn.execute("SELECT target,fingerprint,conversation_id,event_id,at,emissions "
                         "FROM agent_waiting_fingerprint WHERE target=?", (target,)).fetchone()
        if not r:
            return None
        return {"target": r[0], "fingerprint": r[1], "conversation_id": r[2],
                "event_id": r[3], "at": r[4], "emissions": r[5]}
    finally:
        if own:
            conn.close()
