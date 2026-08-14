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
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts
from core import wake_routes

# Opt-in. Nothing wakes anything until the owner turns this on.
ENABLED = os.getenv("WAKE_BRIDGE_ENABLED", "0") not in ("0", "", "false", "no")
# Overrides everything, including an explicit enable.
KILL_SWITCH = os.getenv("WAKE_BRIDGE_KILL_SWITCH", "0") not in ("0", "", "false", "no")
# Minimum gap between ANY two GENERIC wakes, however distinct the events.
COOLDOWN_SECS = int(os.getenv("WAKE_BRIDGE_COOLDOWN_SECS", "900"))
# Minimum gap between two ACTIONABLE wakes — a much shorter floor, for the reason given
# under ACTIONABLE_EVENT_TYPES below. Short is not absent: a flapping agent still cannot
# turn into a burst of pokes.
ACTIONABLE_COOLDOWN_SECS = int(os.getenv("WAKE_BRIDGE_ACTIONABLE_COOLDOWN_SECS", "60"))
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
    # an existing live agent that stopped and is waiting for a response RIGHT NOW
    "agent_waiting_input", "agent_needs_response", "agent_prompt_needs_response",
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


# ── the actionable class ────────────────────────────────────────────────────
# An EXISTING live agent that has stopped and is waiting for a response right now. This is a
# different KIND of thing from everything above, and the distinction is the whole point of
# this patch:
#
#   * the generic classes are HISTORY — a durable record the assistant reads whenever it next
#     looks. Delivering one 40 minutes late costs nothing, which is why they share a 900s
#     floor and drain oldest-first.
#   * an actionable event is a pane BLOCKED right now. Every minute it queues behind a
#     multi-day backlog is a minute of work not happening.
#
# On 2026-08-13 03:58 payorch-sbp-resumed entered waiting_input and Owner OS had no event for
# it at all: the only trace was `new_agent_discovered` at info severity, skipped for
# `cooldown_active`. Meanwhile event 3746 — from Aug 11 — was delivered at 04:09:57, having
# consumed the one send the global cooldown allows. The owner pinged the chat by hand.
ACTIONABLE_EVENT_TYPES = frozenset({
    "agent_waiting_input", "agent_needs_response", "agent_prompt_needs_response",
})


def is_actionable(event_type: str = "") -> bool:
    """A live agent waiting for a response now, as opposed to a durable record of history."""
    return (event_type or "").strip() in ACTIONABLE_EVENT_TYPES


def is_significant(*, event_type: str = "", severity: str = "",
                   owner_action_required: bool = False) -> dict:
    """Is this event worth interrupting a human for? Returns the reason either way.

    Order matters: the two pre-existing authorities are consulted first, so this function can
    only ever ADD eligibility. `ROUTINE_EVENT_TYPES` is checked last for exactly that reason —
    a routine type that somehow arrives at critical severity still wakes.

    The actionable class is named ahead of them purely so the AUDIT says which authority
    spoke. It admits nothing that `WAKE_EVENT_TYPES` would not have admitted anyway.
    """
    t = (event_type or "").strip()
    if is_actionable(t):
        return {"significant": True, "reason": "actionable_waiting_transition",
                "actionable": True}
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

# Columns added after the table shipped. A live control-plane DB already holds the old
# shape, so they are applied by migration rather than by editing _SCHEMA — which only ever
# runs for a database that does not exist yet.
_AUDIT_COLUMNS = (
    ("event_type", "TEXT"),
    ("actionable", "INTEGER DEFAULT 0"),
    # Coalescing: a superseded row is retired from selection but never deleted.
    ("superseded_by", "INTEGER"),
    ("superseded_at", "TEXT"),
    ("superseded_reason", "TEXT"),
    # Routing: the event's project, kept so the target can be re-resolved FRESH at
    # delivery time. Rows from before the column exist as NULL and route to owner-os.
    ("project_id", "TEXT"),
)

# Why a generic wake stopped being offered, kept as its own append-only record. The audit
# has to survive independently of the row it retired: "the queue got shorter" must always
# be answerable with which ids were folded into which, and when.
_COALESCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS wake_coalesce_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, at TEXT, wake_audit_id INTEGER, event_id INTEGER,
    superseded_by_audit_id INTEGER, superseded_by_event_id INTEGER, reason TEXT
)
"""


def _migrate(conn) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(wake_audit)")}
    for name, decl in _AUDIT_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE wake_audit ADD COLUMN {name} {decl}")


def _conn(conn=None):
    conn, own = _c(conn)
    conn.execute(_SCHEMA)
    _migrate(conn)
    conn.execute(_COALESCE_SCHEMA)
    return conn, own


def _enabled() -> tuple:
    """Read the switches at DECISION time, so flipping them takes effect without a restart."""
    enabled = os.getenv("WAKE_BRIDGE_ENABLED", "0") not in ("0", "", "false", "no")
    kill = os.getenv("WAKE_BRIDGE_KILL_SWITCH", "0") not in ("0", "", "false", "no")
    return enabled, kill


def should_wake(*, event_id: int, severity: str, correlation_id: str = "",
                owner_action_required: bool = False, event_type: str = "",
                project_id: str = "", agent_id: str = "", conn=None,
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
    # FAIL CLOSED on the target, resolved for THIS event's route — a MESS event is judged
    # against the MESS chat, never against whatever chat happens to be the owner-os one.
    # Guessing a conversation would be exactly the arbitrary behaviour this design forbids.
    target = wake_routes.resolve(project_id=project_id, agent_id=agent_id, conn=conn)
    if not target.get("bound"):
        return {"wake": False, "reason": target.get("reason", "no_route_bound"),
                "route_key": target.get("route_key")}

    conn, own = _conn(conn)
    try:
        actionable = is_actionable(event_type)
        # Exactly-once per event, unchanged and checked FIRST for both classes. Bypassing the
        # generic cooldown below must never become bypassing dedupe.
        prior = conn.execute(
            "SELECT id,acknowledged FROM wake_audit WHERE event_id=? AND decision='wake' "
            "ORDER BY id DESC LIMIT 1", (int(event_id),)).fetchone()
        if prior:
            return {"wake": False, "reason": "already_woke_for_this_event",
                    "acknowledged": bool(prior[1]), "actionable": actionable}
        if actionable:
            # The generic floor is deliberately NOT consulted. A blocked pane waiting out a
            # cooldown earned by a two-day-old backlog entry is the exact stall this fixes.
            # Its own floor still applies, so distinct actionable events cannot burst.
            last_a = conn.execute(
                "SELECT ts FROM wake_audit WHERE decision='wake' AND actionable=1 "
                "ORDER BY id DESC LIMIT 1").fetchone()
            if last_a and (now - float(last_a[0] or 0)) < ACTIONABLE_COOLDOWN_SECS:
                wait = int(ACTIONABLE_COOLDOWN_SECS - (now - float(last_a[0])))
                return {"wake": False, "reason": "actionable_cooldown_active",
                        "wait_secs": wait, "actionable": True}
            return {"wake": True, "reason": "actionable_waiting_transition",
                    "actionable": True, "phrase": WAKE_PHRASE,
                    "conversation": target["conversation"],
                    "route_key": target["route_key"],
                    "route_reason": target["route_reason"]}
        last = conn.execute(
            "SELECT ts FROM wake_audit WHERE decision='wake' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last and (now - float(last[0] or 0)) < COOLDOWN_SECS:
            wait = int(COOLDOWN_SECS - (now - float(last[0])))
            return {"wake": False, "reason": "cooldown_active", "wait_secs": wait,
                    "actionable": False}
        return {"wake": True, "reason": "urgent_event_not_yet_signalled",
                "actionable": False,
                "phrase": WAKE_PHRASE, "conversation": target["conversation"],
                "route_key": target["route_key"], "route_reason": target["route_reason"]}
    finally:
        if own:
            conn.close()


def record(decision: dict, *, event_id: int, severity: str = "", event_type: str = "",
           correlation_id: str = "", project_id: str = "", conn=None,
           now: Optional[float] = None) -> int:
    """Every decision is audited, including the refusals — a bridge that only records its
    successes cannot be debugged when it stays silent.

    The class is persisted alongside the decision, because selection later has to rank by it
    and a class recomputed at read time would drift the moment the type sets changed. The
    project is persisted for the same reason a target URL is NOT: the route key must survive
    to delivery time, and the conversation must be re-resolved there — a URL frozen at
    decision time is exactly the stale-target hijack this design forbids.
    """
    now = now if now is not None else now_ts()
    actionable = bool(decision.get("actionable", is_actionable(event_type)))
    conn, own = _conn(conn)
    try:
        cur = conn.execute(
            "INSERT INTO wake_audit (ts,at,event_id,correlation_id,severity,decision,reason,"
            "event_type,actionable,project_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now, now_iso(), int(event_id), correlation_id, severity,
             "wake" if decision.get("wake") else "skip", str(decision.get("reason"))[:160],
             (event_type or "").strip(), 1 if actionable else 0,
             (project_id or "").strip()))
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
        _migrate_delivery(conn)
        d = conn.execute("SELECT at,delivered,reason,conversation FROM wake_delivery "
                         "ORDER BY id DESC LIMIT 1").fetchone()
        failed = conn.execute(
            "SELECT COUNT(*) FROM wake_delivery WHERE delivered=0").fetchone()[0]
        return {"enabled": enabled, "kill_switch": kill,
                "last_delivery_at": (d[0] if d else None),
                "last_delivery_ok": (bool(d[1]) if d else None),
                "last_delivery_reason": (d[2] if d else None),
                "last_delivery_conversation": (d[3] if d else None),
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

def _chat_conn(conn=None):
    conn, own = _c(conn)
    for stmt in _CHAT_SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


def valid_conversation(url: str) -> bool:
    """A conversation URL, not an arbitrary page. Fail closed on anything else. One rule,
    owned by the route registry; re-exported here so every old caller keeps the same door."""
    return wake_routes.valid_conversation(url)


def active_chat(conn=None) -> dict:
    """The OWNER-OS control chat. Read fresh on every wake — never cached in code or a unit.

    Registry first: the owner-os route in `wake_route` is canonical. The single
    `wake_target` row remains as a migration bridge for a database the registry has not
    touched yet; `bind_chat` keeps both in lockstep, so they cannot diverge through any
    supported path.
    """
    conn, own = _chat_conn(conn)
    try:
        r = wake_routes.get_route(wake_routes.FALLBACK_ROUTE, conn=conn)
        if r and valid_conversation(r["conversation"]):
            return {"bound": True, "conversation": r["conversation"],
                    "bound_at": r["bound_at"], "bound_by": r["bound_by"], "note": r["note"]}
        row = conn.execute("SELECT conversation,bound_at,bound_by,note FROM wake_target "
                           "WHERE id=1").fetchone()
        if not row or not (row[0] or "").strip():
            return {"bound": False, "reason": "no_active_control_chat"}
        if not valid_conversation(row[0]):
            return {"bound": False, "reason": "active_chat_invalid", "conversation": row[0]}
        return {"bound": True, "conversation": row[0], "bound_at": row[1], "bound_by": row[2],
                "note": row[3]}
    finally:
        if own:
            conn.close()


def bind_chat(conversation: str, *, by: str = "owner", note: str = "", conn=None,
              now: Optional[float] = None) -> dict:
    """Point the OWNER-OS route at a different conversation. Atomic, audited, no content
    stored. Writes the canonical registry row AND the legacy wake_target row in the same
    transaction, so a reader of either sees the same pointer — one procedure, no divergence."""
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
        # The canonical registry row, same transaction (bind_route commits on this conn).
        wake_routes.bind_route(wake_routes.FALLBACK_ROUTE, url, by=by, note=note, conn=conn,
                               now=now)
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


def pending_wake(conn=None, now: Optional[float] = None) -> dict:
    """The oldest decided-but-unacknowledged wake, with the target resolved for ITS route.

    The companion asks this; it never decides for itself. The conversation is resolved at
    read time from the route registry — per event, from the project persisted at decision
    time — so a rebind between decision and submission sends to the new chat rather than a
    stale one, and a MESS wake can never ride to the payments chat.
    """
    enabled, kill = _enabled()
    if kill or not enabled:
        return {"pending": False, "reason": "kill_switch_engaged" if kill else "bridge_disabled"}
    conn, own = _conn(conn)
    try:
        # A phrase already fired for this event is never offered again, even if the
        # verification that followed was inconclusive. Unacknowledged means "we never got
        # proof", not "it definitely did not arrive" — and only the latter would justify
        # sending a second copy into the owner's chat.
        conn.execute(_SUBMIT_SCHEMA)
        # Fold the generic backlog down to its newest member PER ROUTE first: N generic
        # wakes for one chat are one instruction, but wakes for different chats are not
        # copies of each other and are never folded across routes.
        coalesced = coalesce_generic_backlog(conn=conn, now=now)
        # Actionable first, then oldest — the only ordering change. Within a class the old
        # oldest-first behaviour is untouched, so nothing is starved, it is merely outranked.
        r = conn.execute("SELECT a.event_id, COALESCE(a.actionable,0), "
                         "COALESCE(a.project_id,'') FROM wake_audit a "
                         "WHERE a.decision='wake' AND a.acknowledged=0 "
                         "AND a.superseded_by IS NULL AND NOT EXISTS "
                         "(SELECT 1 FROM wake_submitted s WHERE s.event_id=a.event_id) "
                         "ORDER BY COALESCE(a.actionable,0) DESC, a.id ASC LIMIT 1").fetchone()
        if not r:
            return {"pending": False, "reason": "nothing_to_wake_for",
                    "coalesced": coalesced["superseded_event_ids"]}
        # FAIL CLOSED per event: an event whose route cannot be resolved offers nothing,
        # rather than borrowing whichever chat another event would have used.
        target = wake_routes.resolve(project_id=r[2], conn=conn)
        if not target.get("bound"):
            return {"pending": False, "reason": target.get("reason", "no_route_bound"),
                    "event_id": int(r[0]), "route_key": target.get("route_key")}
        return {"pending": True, "event_id": int(r[0]), "actionable": bool(r[1]),
                "conversation": target["conversation"], "phrase": WAKE_PHRASE,
                "route_key": target["route_key"], "route_reason": target["route_reason"],
                "coalesced": coalesced["superseded_event_ids"]}
    finally:
        if own:
            conn.close()


def coalesce_generic_backlog(conn=None, now: Optional[float] = None) -> dict:
    """Fold every pending GENERIC wake but the newest into that newest one, PER ROUTE.

    The phrase is fixed and carries no event content — it says "go read Owner OS". So N
    queued generic wakes FOR THE SAME CHAT are N copies of one identical instruction, and
    draining them at one per 900s is how a two-day-old event came to be delivered at
    04:09:57 ahead of everything that mattered. Collapsing them costs nothing: the CTO
    inbox still holds every event, and the surviving wake tells the assistant to read all
    of them.

    Grouping is by ROUTE KEY, never globally: a MESS wake and a payments wake go to
    different conversations, so folding one into the other would silently drop a chat's
    only doorbell ring. Within a route the old behaviour is unchanged.

    Superseded rows are RETIRED, never deleted — the row keeps its supersedes pointer and a
    second append-only record names which id absorbed it. Actionable wakes are never touched:
    each one is a distinct blocked pane, not a duplicate of a generic instruction.
    """
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        conn.execute(_SUBMIT_SCHEMA)
        rows = conn.execute(
            "SELECT a.id, a.event_id, COALESCE(a.project_id,'') FROM wake_audit a "
            "WHERE a.decision='wake' "
            "AND a.acknowledged=0 AND a.superseded_by IS NULL AND COALESCE(a.actionable,0)=0 "
            "AND NOT EXISTS (SELECT 1 FROM wake_submitted s WHERE s.event_id=a.event_id) "
            "ORDER BY a.id ASC").fetchall()
        groups: dict = {}
        for aid, eid, project in rows:
            groups.setdefault(wake_routes.route_key_for_event(project), []).append((aid, eid))
        superseded, kept = [], []
        reason = "coalesced_into_newest_generic_wake"
        for key, members in groups.items():
            if len(members) < 2:
                kept.append(int(members[0][1]))
                continue
            keep_audit_id, keep_event_id = int(members[-1][0]), int(members[-1][1])
            kept.append(keep_event_id)
            for aid, eid in members[:-1]:
                conn.execute("UPDATE wake_audit SET superseded_by=?, superseded_at=?, "
                             "superseded_reason=? WHERE id=?",
                             (keep_audit_id, now_iso(), reason, int(aid)))
                conn.execute(
                    "INSERT INTO wake_coalesce_audit (ts,at,wake_audit_id,event_id,"
                    "superseded_by_audit_id,superseded_by_event_id,reason) VALUES (?,?,?,?,?,?,?)",
                    (now, now_iso(), int(aid), int(eid), keep_audit_id, keep_event_id, reason))
                superseded.append(int(eid))
        if superseded:
            conn.commit()
        return {"superseded": len(superseded), "superseded_event_ids": superseded,
                "kept_event_id": (kept[-1] if kept else None), "kept_event_ids": kept,
                "reason": reason}
    finally:
        if own:
            conn.close()


def coalesce_history(limit: int = 50, conn=None) -> list:
    """Which generic wakes were folded into which, and when. The durable half of the audit."""
    conn, own = _conn(conn)
    try:
        rows = conn.execute(
            "SELECT at,wake_audit_id,event_id,superseded_by_audit_id,superseded_by_event_id,"
            "reason FROM wake_coalesce_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"at": r[0], "wake_audit_id": r[1], "event_id": r[2],
                 "superseded_by_audit_id": r[3], "superseded_by_event_id": r[4],
                 "reason": r[5]} for r in rows]
    finally:
        if own:
            conn.close()


_SEND_SCHEMA = """
CREATE TABLE IF NOT EXISTS wake_send (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, at TEXT, source TEXT, event_id INTEGER, allowed INTEGER, reason TEXT
)
"""


def _migrate_send(conn) -> None:
    """As with wake_audit: a live DB predates the column, so add it rather than assume it."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(wake_send)")}
    if "actionable" not in have:
        conn.execute("ALTER TABLE wake_send ADD COLUMN actionable INTEGER DEFAULT 0")


def claim_send(source: str, event_id: Optional[int] = None, conn=None,
               actionable: bool = False, now: Optional[float] = None) -> dict:
    """The single choke point every submission must pass, whatever called it.

    The owner saw the wake phrase twice. Neither was a duplicate of the same event: one came
    from the companion and one from a DIRECT out-of-band call that bypassed the bridge
    entirely — recording nothing and consuming no cooldown, so the next legitimate wake fired
    55 seconds later unimpeded. Per-event dedupe cannot prevent that; only a global claim can.

    Every attempt is recorded, allowed or not, so an out-of-band send is visible even when
    it is refused.

    `actionable` claims are measured against the actionable floor and against PRIOR
    ACTIONABLE SENDS only. Deciding to wake for a blocked pane and then refusing the send on
    a generic cooldown would move the stall from the decision to the delivery and fix
    nothing; the choke point itself has to know the two classes apart. It remains a choke
    point — the actionable floor still applies, and every attempt is still recorded.
    """
    now = now if now is not None else now_ts()
    enabled, kill = _enabled()
    conn, own = _c(conn)
    try:
        conn.execute(_SEND_SCHEMA)
        _migrate_send(conn)
        if kill:
            res = (False, "kill_switch_engaged")
        elif not enabled:
            res = (False, "bridge_disabled")
        elif actionable:
            r = conn.execute("SELECT ts FROM wake_send WHERE allowed=1 AND "
                             "COALESCE(actionable,0)=1 ORDER BY id DESC LIMIT 1").fetchone()
            if r and (now - float(r[0] or 0)) < ACTIONABLE_COOLDOWN_SECS:
                res = (False, f"actionable_cooldown_active:"
                              f"{int(ACTIONABLE_COOLDOWN_SECS - (now - float(r[0])))}s")
            else:
                res = (True, "claimed_actionable")
        else:
            r = conn.execute("SELECT ts FROM wake_send WHERE allowed=1 "
                             "ORDER BY id DESC LIMIT 1").fetchone()
            if r and (now - float(r[0] or 0)) < COOLDOWN_SECS:
                res = (False, f"global_cooldown_active:"
                              f"{int(COOLDOWN_SECS - (now - float(r[0])))}s")
            else:
                res = (True, "claimed")
        conn.execute("INSERT INTO wake_send (ts,at,source,event_id,allowed,reason,actionable) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (now, now_iso(), source, int(event_id or 0), int(res[0]), res[1],
                      1 if actionable else 0))
        conn.commit()
        return {"allowed": res[0], "reason": res[1], "source": source,
                "actionable": bool(actionable)}
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


def _migrate_delivery(conn) -> None:
    """The live DB predates the column; add it rather than assume it. `conversation` is the
    bound target URL the attempt resolved to — the owner's rotatable pointer, never content —
    so every delivery row answers "which chat did this send actually go to"."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(wake_delivery)")}
    if "conversation" not in have:
        conn.execute("ALTER TABLE wake_delivery ADD COLUMN conversation TEXT DEFAULT ''")
    if "route_key" not in have:
        conn.execute("ALTER TABLE wake_delivery ADD COLUMN route_key TEXT DEFAULT ''")

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
                    reason: str = "", conversation: str = "", route_key: str = "",
                    conn=None, now: Optional[float] = None) -> int:
    """Persist what actually happened. A failure stays UNACKNOWLEDGED by construction — the
    caller only acknowledges on success — so the wake remains pending and is retried.
    `conversation` and `route_key` are what the attempt resolved to, kept so a wrong- or
    stale-chat delivery is provable (or refutable) from state alone."""
    now = now if now is not None else now_ts()
    conn, own = _c(conn)
    try:
        conn.execute(_DELIVERY_SCHEMA)
        _migrate_delivery(conn)
        cur = conn.execute(
            "INSERT INTO wake_delivery (ts,at,source,event_id,delivered,reason,conversation,"
            "route_key) VALUES (?,?,?,?,?,?,?,?)",
            (now, now_iso(), source, int(event_id or 0), 1 if delivered else 0,
             str(reason)[:160], (conversation or "").strip()[:200],
             (route_key or "").strip()[:64]))
        conn.commit()
        # Delivery outcomes are the strongest liveness evidence there is; feed them to the
        # chat registry. A verified delivery proves writable; a send that FIRED and was
        # refused by the page proves dead. Timeouts and pre-send refusals prove neither.
        try:
            from core import chat_registry as _cr
            if conversation:
                if delivered:
                    _cr.upsert_chat(conversation, source="delivery", writable=True,
                                    conn=conn, now=now)
                elif str(reason) == "composer_did_not_clear_after_send":
                    _cr.mark_dead(conversation, reason=str(reason)[:160], conn=conn,
                                  now=now)
        except Exception:  # noqa: BLE001 — registry bookkeeping must never break delivery
            pass
        return int(cur.lastrowid)
    finally:
        if own:
            conn.close()


def last_delivery(event_id: Optional[int] = None, conn=None) -> Optional[dict]:
    """The most recent attempt, overall or for one event."""
    conn, own = _c(conn)
    try:
        conn.execute(_DELIVERY_SCHEMA)
        _migrate_delivery(conn)
        sel = ("SELECT at,source,event_id,delivered,reason,conversation,route_key "
               "FROM wake_delivery ")
        if event_id is None:
            r = conn.execute(sel + "ORDER BY id DESC LIMIT 1").fetchone()
        else:
            r = conn.execute(sel + "WHERE event_id=? ORDER BY id DESC LIMIT 1",
                             (int(event_id),)).fetchone()
        if not r:
            return None
        return {"at": r[0], "source": r[1], "event_id": int(r[2]),
                "delivered": bool(r[3]), "reason": r[4], "conversation": r[5],
                "route_key": r[6]}
    finally:
        if own:
            conn.close()
