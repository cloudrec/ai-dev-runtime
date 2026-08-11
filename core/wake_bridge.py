"""Wake bridge — server half.

Decides WHETHER a wake is warranted and records it. It never opens a browser, never holds a
ChatGPT credential, and never carries event content: the companion submits one fixed phrase
and nothing else, so this module's entire job is to answer "should the companion wake the
chat right now?" and to make that answer auditable.

Deliberate design choices, each from a failure this session already produced elsewhere:

  * DISABLED BY DEFAULT. An automation that can poke a chat must be opt-in, not something
    that starts working because a module got imported.
  * Exactly once per event. The same event can never wake twice — that is the duplicate-poke
    failure in a new costume.
  * Acknowledgement STOPS further wakes for that event. Once the assistant has been told,
    telling it again is noise.
  * A cooldown floor independent of dedupe, so a burst of DISTINCT events cannot become a
    burst of wakes.
  * Kill switch that overrides everything, checked at decision time rather than at import.

Owner OS and the MCP inbox remain the source of truth. This bridge is an accelerator: if it
is disabled, broken, or never installed, autonomy is unaffected — the inbox still holds every
event and the Telegram tier still carries the urgent ones.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

# Opt-in. Nothing wakes anything until the owner turns this on.
ENABLED = os.getenv("WAKE_BRIDGE_ENABLED", "0") not in ("0", "", "false", "no")
# Overrides everything, including an explicit enable.
KILL_SWITCH = os.getenv("WAKE_BRIDGE_KILL_SWITCH", "0") not in ("0", "", "false", "no")
# Minimum gap between ANY two wakes, however distinct the events.
COOLDOWN_SECS = int(os.getenv("WAKE_BRIDGE_COOLDOWN_SECS", "900"))
# Only these severities are ever worth a wake.
WAKE_SEVERITIES = {"critical", "high"}

# ── event eligibility ───────────────────────────────────────────────────────
# Severity and `owner_action_required` remain the two independent authorities they always
# were. What they missed is a class of event that is unmistakably significant yet carries
# neither: `owner_gate_opened` is emitted at info severity with owner_action_required=0, so
# the owner was never woken for a gate that exists precisely to ask them something.
#
# These sets are ADDITIVE. Nothing that woke before stops waking; a type listed here becomes
# eligible on its own, and a type listed as routine is only ever refused when severity and
# owner_action_required have already declined to speak for it.
WAKE_EVENT_TYPES = frozenset({
    # waiting on the owner / a decision is required
    "owner_gate_opened", "agent_owner_decision", "agent_waiting_owner",
    "owner_decision_required", "agent_blocked_on_owner", "needs_owner_payload",
    # failure, death, blocker
    "agent_dead", "agent_process_failed", "agent_crash_loop", "session_quarantined",
    "governor_blocker", "stage_blocked_external", "task_failed", "action_blocked",
    "notification_dead_letter", "notification_channel_down", "notifications_red",
    # an owner-directed task reaching its end
    "task_completed", "work_stopped_incomplete",
})

# Routine traffic: progress chatter, verification echoes and no-change reports. Naming them
# turns "not_significant" into an auditable reason instead of a silent fallthrough.
ROUTINE_EVENT_TYPES = frozenset({
    "agent_state", "action_verified", "action_deferred_pending_input",
    "work_partial_completion", "work_commits_without_stage_progress",
    "work_report_published", "owner_gate_answered", "blocker_resolved",
    "context_rotated", "false_idle_corrected", "new_agent_discovered",
    "verified_record_contradicted",
})


def is_significant(*, event_type: str = "", severity: str = "",
                   owner_action_required: bool = False) -> dict:
    """Is this event worth interrupting a human for? Returns the reason either way.

    Order matters: the two pre-existing authorities are consulted first, so this function can
    only ever ADD eligibility. `ROUTINE_EVENT_TYPES` is checked last for exactly that reason —
    a routine type that somehow arrives at critical severity still wakes.
    """
    t = (event_type or "").strip()
    if severity in WAKE_SEVERITIES:
        return {"significant": True, "reason": "severity_at_wake_threshold"}
    if owner_action_required:
        return {"significant": True, "reason": "owner_action_required"}
    if t in WAKE_EVENT_TYPES:
        return {"significant": True, "reason": "significant_event_type"}
    if t in ROUTINE_EVENT_TYPES:
        return {"significant": False, "reason": "routine_event_type"}
    return {"significant": False, "reason": "severity_below_wake_threshold"}
# The ONLY text the companion may submit. No event content, ever.
WAKE_PHRASE = os.getenv(
    "WAKE_BRIDGE_PHRASE",
    "Проверь новые события Owner OS через MCP и продолжи разрешённую работу.")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wake_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, at TEXT, event_id INTEGER, correlation_id TEXT, severity TEXT,
    decision TEXT, reason TEXT, acknowledged INTEGER DEFAULT 0, acknowledged_at TEXT
)
"""


def _conn(conn=None):
    conn, own = _c(conn)
    conn.execute(_SCHEMA)
    return conn, own


def _enabled() -> tuple:
    """Read the switches at DECISION time, so flipping them takes effect without a restart."""
    enabled = os.getenv("WAKE_BRIDGE_ENABLED", "0") not in ("0", "", "false", "no")
    kill = os.getenv("WAKE_BRIDGE_KILL_SWITCH", "0") not in ("0", "", "false", "no")
    return enabled, kill


def should_wake(*, event_id: int, severity: str, correlation_id: str = "",
                owner_action_required: bool = False, event_type: str = "", conn=None,
                now: Optional[float] = None) -> dict:
    """Answer, with a recorded reason either way. Never raises for control flow."""
    now = now if now is not None else now_ts()
    enabled, kill = _enabled()
    if kill:
        return {"wake": False, "reason": "kill_switch_engaged"}
    if not enabled:
        return {"wake": False, "reason": "bridge_disabled"}
    sig = is_significant(event_type=event_type, severity=severity,
                         owner_action_required=owner_action_required)
    if not sig["significant"]:
        return {"wake": False, "reason": sig["reason"]}
    # FAIL CLOSED on the target. With no valid active chat there is nowhere to wake, and
    # guessing a conversation would be exactly the arbitrary behaviour this design forbids.
    target = active_chat(conn=conn)
    if not target.get("bound"):
        return {"wake": False, "reason": target.get("reason", "no_active_control_chat")}

    conn, own = _conn(conn)
    try:
        prior = conn.execute(
            "SELECT id,acknowledged FROM wake_audit WHERE event_id=? AND decision='wake' "
            "ORDER BY id DESC LIMIT 1", (int(event_id),)).fetchone()
        if prior:
            return {"wake": False, "reason": "already_woke_for_this_event",
                    "acknowledged": bool(prior[1])}
        last = conn.execute(
            "SELECT ts FROM wake_audit WHERE decision='wake' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last and (now - float(last[0] or 0)) < COOLDOWN_SECS:
            wait = int(COOLDOWN_SECS - (now - float(last[0])))
            return {"wake": False, "reason": "cooldown_active", "wait_secs": wait}
        return {"wake": True, "reason": "urgent_event_not_yet_signalled",
                "phrase": WAKE_PHRASE, "conversation": target["conversation"]}
    finally:
        if own:
            conn.close()


def record(decision: dict, *, event_id: int, severity: str = "",
           correlation_id: str = "", conn=None, now: Optional[float] = None) -> int:
    """Every decision is audited, including the refusals — a bridge that only records its
    successes cannot be debugged when it stays silent."""
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        cur = conn.execute(
            "INSERT INTO wake_audit (ts,at,event_id,correlation_id,severity,decision,reason) "
            "VALUES (?,?,?,?,?,?,?)",
            (now, now_iso(), int(event_id), correlation_id, severity,
             "wake" if decision.get("wake") else "skip", str(decision.get("reason"))[:160]))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        if own:
            conn.close()


def acknowledge(event_id: int, conn=None, now: Optional[float] = None) -> dict:
    """The assistant consumed it. Stop waking for this event."""
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        conn.execute("UPDATE wake_audit SET acknowledged=1, acknowledged_at=? "
                     "WHERE event_id=? AND decision='wake'", (now_iso(), int(event_id)))
        conn.commit()
        return {"event_id": int(event_id), "acknowledged": True}
    finally:
        if own:
            conn.close()


def health(conn=None, now: Optional[float] = None) -> dict:
    """Freshness the owner can check: is it on, when did it last wake, was that acknowledged?"""
    now = now if now is not None else now_ts()
    enabled, kill = _enabled()
    conn, own = _conn(conn)
    try:
        r = conn.execute("SELECT ts,at,event_id,acknowledged FROM wake_audit "
                         "WHERE decision='wake' ORDER BY id DESC LIMIT 1").fetchone()
        total = conn.execute("SELECT COUNT(*) FROM wake_audit WHERE decision='wake'"
                             ).fetchone()[0]
        last_age = (now - float(r[0])) if r else None
        # A wake that was decided but never delivered is the failure mode this reports on.
        conn.execute(_DELIVERY_SCHEMA)
        d = conn.execute("SELECT at,delivered,reason FROM wake_delivery "
                         "ORDER BY id DESC LIMIT 1").fetchone()
        failed = conn.execute(
            "SELECT COUNT(*) FROM wake_delivery WHERE delivered=0").fetchone()[0]
        return {"enabled": enabled, "kill_switch": kill,
                "last_delivery_at": (d[0] if d else None),
                "last_delivery_ok": (bool(d[1]) if d else None),
                "last_delivery_reason": (d[2] if d else None),
                "deliveries_failed_total": int(failed),
                "cooldown_secs": COOLDOWN_SECS,
                "wakes_total": int(total),
                "last_wake_at": (r[1] if r else None),
                "last_wake_event_id": (int(r[2]) if r else None),
                "last_wake_acknowledged": (bool(r[3]) if r else None),
                "last_wake_age_secs": (int(last_age) if last_age is not None else None),
                "phrase": WAKE_PHRASE,
                "note": "accelerator only — the CTO inbox holds every event regardless"}
    finally:
        if own:
            conn.close()


# ── the active control chat: a rotatable POINTER, never a hardcoded URL ──────
# A chat fills up and gets replaced. Binding the bridge to one conversation forever would
# mean a reinstall every time that happens, so the target lives in a durable record that the
# bridge reads on EVERY wake. Owner OS state stays in the CTO inbox; the chat is only a
# replaceable doorbell.
_CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS wake_target (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    conversation TEXT, bound_at TEXT, bound_ts REAL, bound_by TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS wake_bind_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, at TEXT, action TEXT, conversation TEXT, previous TEXT, by TEXT, note TEXT
)
"""

_CHAT_RE = re.compile(r"^https://chat(gpt)?\.(com|openai\.com)/(c/)?[A-Za-z0-9\-]+/?$")


def _chat_conn(conn=None):
    conn, own = _c(conn)
    for stmt in _CHAT_SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


def valid_conversation(url: str) -> bool:
    """A conversation URL, not an arbitrary page. Fail closed on anything else."""
    return bool(_CHAT_RE.match((url or "").strip()))


def active_chat(conn=None) -> dict:
    """The current wake target. Read fresh on every wake — never cached in code or a unit."""
    conn, own = _chat_conn(conn)
    try:
        r = conn.execute("SELECT conversation,bound_at,bound_by,note FROM wake_target "
                         "WHERE id=1").fetchone()
        if not r or not (r[0] or "").strip():
            return {"bound": False, "reason": "no_active_control_chat"}
        if not valid_conversation(r[0]):
            return {"bound": False, "reason": "active_chat_invalid", "conversation": r[0]}
        return {"bound": True, "conversation": r[0], "bound_at": r[1], "bound_by": r[2],
                "note": r[3]}
    finally:
        if own:
            conn.close()


def bind_chat(conversation: str, *, by: str = "owner", note: str = "", conn=None,
              now: Optional[float] = None) -> dict:
    """Point the bridge at a different conversation. Atomic, audited, no content stored."""
    now = now if now is not None else now_ts()
    url = (conversation or "").strip()
    if not valid_conversation(url):
        return {"ok": False, "reason": "not_a_conversation_url", "conversation": url[:120]}
    conn, own = _chat_conn(conn)
    try:
        prev = conn.execute("SELECT conversation FROM wake_target WHERE id=1").fetchone()
        previous = (prev[0] if prev else "") or ""
        conn.execute(
            "INSERT INTO wake_target (id,conversation,bound_at,bound_ts,bound_by,note) "
            "VALUES (1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET conversation=excluded.conversation,"
            "bound_at=excluded.bound_at, bound_ts=excluded.bound_ts, bound_by=excluded.bound_by,"
            "note=excluded.note", (url, now_iso(), now, by, note[:200]))
        # Audit records the POINTER moving. Never any conversation content.
        conn.execute("INSERT INTO wake_bind_audit (ts,at,action,conversation,previous,by,note) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (now, now_iso(), "rebind" if previous else "bind", url, previous, by,
                      note[:200]))
        conn.commit()
        return {"ok": True, "conversation": url, "previous": previous or None,
                "action": "rebind" if previous else "bind"}
    finally:
        if own:
            conn.close()


def bind_history(limit: int = 20, conn=None) -> list:
    conn, own = _chat_conn(conn)
    try:
        conn.row_factory = __import__("sqlite3").Row
        return [dict(r) for r in conn.execute(
            "SELECT at,action,conversation,previous,by,note FROM wake_bind_audit "
            "ORDER BY id DESC LIMIT ?", (limit,))]
    finally:
        if own:
            conn.close()


def pending_wake(conn=None) -> dict:
    """The oldest decided-but-unacknowledged wake, with the CURRENT target.

    The companion asks this; it never decides for itself. The conversation is resolved at
    read time from the rotatable pointer, so a rebind between decision and submission sends
    to the new chat rather than a stale one.
    """
    enabled, kill = _enabled()
    if kill or not enabled:
        return {"pending": False, "reason": "kill_switch_engaged" if kill else "bridge_disabled"}
    target = active_chat(conn=conn)
    if not target.get("bound"):
        return {"pending": False, "reason": target.get("reason", "no_active_control_chat")}
    conn, own = _conn(conn)
    try:
        # A phrase already fired for this event is never offered again, even if the
        # verification that followed was inconclusive. Unacknowledged means "we never got
        # proof", not "it definitely did not arrive" — and only the latter would justify
        # sending a second copy into the owner's chat.
        conn.execute(_SUBMIT_SCHEMA)
        r = conn.execute("SELECT a.event_id FROM wake_audit a WHERE a.decision='wake' AND "
                         "a.acknowledged=0 AND NOT EXISTS "
                         "(SELECT 1 FROM wake_submitted s WHERE s.event_id=a.event_id) "
                         "ORDER BY a.id ASC LIMIT 1").fetchone()
        if not r:
            return {"pending": False, "reason": "nothing_to_wake_for"}
        return {"pending": True, "event_id": int(r[0]),
                "conversation": target["conversation"], "phrase": WAKE_PHRASE}
    finally:
        if own:
            conn.close()


_SEND_SCHEMA = """
CREATE TABLE IF NOT EXISTS wake_send (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, at TEXT, source TEXT, event_id INTEGER, allowed INTEGER, reason TEXT
)
"""


def claim_send(source: str, event_id: Optional[int] = None, conn=None,
               now: Optional[float] = None) -> dict:
    """The single choke point every submission must pass, whatever called it.

    The owner saw the wake phrase twice. Neither was a duplicate of the same event: one came
    from the companion and one from a DIRECT out-of-band call that bypassed the bridge
    entirely — recording nothing and consuming no cooldown, so the next legitimate wake fired
    55 seconds later unimpeded. Per-event dedupe cannot prevent that; only a global claim can.

    Every attempt is recorded, allowed or not, so an out-of-band send is visible even when
    it is refused.
    """
    now = now if now is not None else now_ts()
    enabled, kill = _enabled()
    conn, own = _c(conn)
    try:
        conn.execute(_SEND_SCHEMA)
        if kill:
            res = (False, "kill_switch_engaged")
        elif not enabled:
            res = (False, "bridge_disabled")
        else:
            r = conn.execute("SELECT ts FROM wake_send WHERE allowed=1 "
                             "ORDER BY id DESC LIMIT 1").fetchone()
            if r and (now - float(r[0] or 0)) < COOLDOWN_SECS:
                res = (False, f"global_cooldown_active:"
                              f"{int(COOLDOWN_SECS - (now - float(r[0])))}s")
            else:
                res = (True, "claimed")
        conn.execute("INSERT INTO wake_send (ts,at,source,event_id,allowed,reason) "
                     "VALUES (?,?,?,?,?,?)",
                     (now, now_iso(), source, int(event_id or 0), int(res[0]), res[1]))
        conn.commit()
        return {"allowed": res[0], "reason": res[1], "source": source}
    finally:
        if own:
            conn.close()


# ── delivery outcomes ───────────────────────────────────────────────────────
# `wake_send` records that a slot was CLAIMED; it says nothing about whether the phrase then
# arrived. That gap is how a run of failures could look identical to a run of successes. This
# table closes it: one row per attempt past the claim, carrying the verdict.
_DELIVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS wake_delivery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, at TEXT, source TEXT, event_id INTEGER, delivered INTEGER, reason TEXT
)
"""

# The phrase left our hands. Recorded the moment the send is FIRED, before anything is known
# about whether the page kept it — because that is the only honest boundary for "may already
# be in the chat". Verification that comes later can say delivered or not; it can never make
# an already-submitted phrase un-sent.
_SUBMIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS wake_submitted (
    event_id INTEGER PRIMARY KEY, ts REAL, at TEXT, source TEXT
)
"""


def mark_submitted(event_id: Optional[int], source: str = "", conn=None,
                   now: Optional[float] = None) -> None:
    """Fail-closed idempotency latch. Called BEFORE the outcome is known.

    The verification added in the delivery patch false-negatived on messages that had in fact
    arrived: the event stayed unacknowledged, the companion retried, and the owner got the
    same wake twice — 27 of 49 events, ~60 duplicate sends. Ambiguity must resolve to "assume
    it went", never to "send it again". The CTO inbox still holds the event either way, so a
    genuinely lost wake costs latency, not correctness.
    """
    if not event_id:
        return
    now = now if now is not None else now_ts()
    conn, own = _c(conn)
    try:
        conn.execute(_SUBMIT_SCHEMA)
        conn.execute("INSERT OR IGNORE INTO wake_submitted (event_id,ts,at,source) "
                     "VALUES (?,?,?,?)", (int(event_id), now, now_iso(), source))
        conn.commit()
    finally:
        if own:
            conn.close()


def was_submitted(event_id: Optional[int], conn=None) -> bool:
    if not event_id:
        return False
    conn, own = _c(conn)
    try:
        conn.execute(_SUBMIT_SCHEMA)
        return conn.execute("SELECT 1 FROM wake_submitted WHERE event_id=?",
                            (int(event_id),)).fetchone() is not None
    finally:
        if own:
            conn.close()


def record_delivery(source: str, *, event_id: Optional[int] = None, delivered: bool = False,
                    reason: str = "", conn=None, now: Optional[float] = None) -> int:
    """Persist what actually happened. A failure stays UNACKNOWLEDGED by construction — the
    caller only acknowledges on success — so the wake remains pending and is retried."""
    now = now if now is not None else now_ts()
    conn, own = _c(conn)
    try:
        conn.execute(_DELIVERY_SCHEMA)
        cur = conn.execute(
            "INSERT INTO wake_delivery (ts,at,source,event_id,delivered,reason) "
            "VALUES (?,?,?,?,?,?)",
            (now, now_iso(), source, int(event_id or 0), 1 if delivered else 0,
             str(reason)[:160]))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        if own:
            conn.close()


def last_delivery(event_id: Optional[int] = None, conn=None) -> Optional[dict]:
    """The most recent attempt, overall or for one event."""
    conn, own = _c(conn)
    try:
        conn.execute(_DELIVERY_SCHEMA)
        if event_id is None:
            r = conn.execute("SELECT at,source,event_id,delivered,reason FROM wake_delivery "
                             "ORDER BY id DESC LIMIT 1").fetchone()
        else:
            r = conn.execute("SELECT at,source,event_id,delivered,reason FROM wake_delivery "
                             "WHERE event_id=? ORDER BY id DESC LIMIT 1",
                             (int(event_id),)).fetchone()
        if not r:
            return None
        return {"at": r[0], "source": r[1], "event_id": int(r[2]),
                "delivered": bool(r[3]), "reason": r[4]}
    finally:
        if own:
            conn.close()
