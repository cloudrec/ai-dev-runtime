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
# An event whose delivery just FAILED steps out of the selection line for this long. The
# 4214 incident: one event retried a wedged page 113 times over 2.5 hours, consuming an
# actionable claim each time, and the younger actionable behind it (4313) never got a
# single attempt. Backoff breaks the hot loop AND the head-of-line blockade at once.
RETRY_BACKOFF_SECS = int(os.getenv("WAKE_BRIDGE_RETRY_BACKOFF_SECS", "300"))
# A transient composer-lookup failure is not the 4214 shape. Event 10063
# (2026-08-27): the composer read 0 matches at 22:20:30 and 1 match at 22:26:03 — the
# page was mid-render, not wedged or dead — yet the standard 300s bench held a LIVE
# `agent_waiting_input` wake back for over five minutes because every failure reason
# shared one floor. This fast lane applies ONLY to that one reason class; every other
# failure (a wedged renderer, a dead chat, a refused send) keeps the original floor.
TRANSIENT_RETRY_BACKOFF_SECS = int(os.getenv("WAKE_BRIDGE_TRANSIENT_RETRY_BACKOFF_SECS", "30"))
# After this many CONSECUTIVE transient failures for the SAME event, the fast lane
# stands down and the event falls back to the standard floor above — a composer still
# ambiguous after ~3 minutes of fast retries is no longer "still loading", and a stuck
# page must never turn the fast lane into a CDP hot-loop. `expire_stale`'s
# MAX_WAKE_AGE_SECS ceiling below remains the final, unconditional stop either way.
TRANSIENT_RETRY_MAX_ATTEMPTS = int(os.getenv("WAKE_BRIDGE_TRANSIENT_RETRY_MAX_ATTEMPTS", "6"))
# Matched by prefix: cdp_composer.py appends the observed composer count
# (`composer_ambiguous_or_absent:0`, `:2`, …), never the bare string.
TRANSIENT_FAILURE_PREFIXES = ("composer_ambiguous_or_absent",)
# How many oldest-eligible candidates `pending_wake` will walk past a benched head of
# line before giving up for this tick. Bounded so a large backlog of benched events
# can never turn selection into an unbounded per-tick scan.
_CANDIDATE_SCAN_LIMIT = 200
# A decided-but-undelivered wake older than this is stale: "this agent is waiting NOW"
# stops being true after hours of silence, and "read Owner OS" stops being useful once
# the backlog it points at is ancient history. The 2026-08-15 incident this guards:
# events decided days earlier (a chronically broken delivery path, plus ~10 pytest-debris
# rows that leaked into the live event table before a sandbox guard landed) sat
# unacknowledged and were served FRESH the moment delivery started working again —
# real project chats got poked with hours- to days-old "wake up" pokes with no bearing
# on current pane state. Selection excludes anything older than this ceiling instead of
# ever delivering it late; `expire_stale` retires it the same audited way coalescing does.
MAX_WAKE_AGE_SECS = int(os.getenv("WAKE_BRIDGE_MAX_AGE_SECS", "10800"))
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
    # the control plane itself is down or split (2026-08-30: the tmux control socket was
    # deleted under a live server; managed-agent control was gone for 100 minutes and
    # nothing could wake anyone about it, because losing the ability to SEE agents was
    # not itself an event type). Emitted deduped per class per 30 min by core.tmux_control.
    "agent_control_plane_unreachable", "agent_control_plane_split",
    # an owner-directed task reaching its end
    "task_completed", "work_stopped_incomplete",
    # closed-loop wake watchdog (task 211): a delivered wake produced no observed
    # progress within the SLO window, and the terminal escalation after that.
    "wake_loop_no_progress", "wake_loop_stalled",
})

# Routine traffic: progress chatter, verification echoes and no-change reports. Naming them
# turns "not_significant" into an auditable reason instead of a silent fallthrough.
ROUTINE_EVENT_TYPES = frozenset({
    "agent_state", "action_verified", "action_deferred_pending_input",
    "work_partial_completion", "work_commits_without_stage_progress",
    "work_report_published", "owner_gate_answered", "blocker_resolved",
    "context_rotated", "false_idle_corrected", "new_agent_discovered",
    "verified_record_contradicted",
    # the control plane came back (self-healed by the guard). Durable, never a wake:
    # the outage already woke the owner, and the recovery is the good news.
    "agent_control_plane_recovered",
    # runtime job lifecycle chatter: durable history, never a wake on its own —
    # the terminal states that matter arrive as task_failed / action_blocked /
    # owner_decision_required / task_completed and wake through those.
    "runtime_job_state", "runtime_job_retried",
    # stall-doctor auto-actions: the whole point is that the owner is NOT woken;
    # genuine escalations arrive as agent_waiting_input and wake through that.
    "stall_doctor_action",
    # a metric, not a wake trigger: the closed loop already failed the owner once
    # (they typed directly into the pane) by the time this is recorded — waking
    # them again for the fact they already acted on would be noise, not help.
    "owner_intervention",
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
    # task 211: a decision only the owner can take, an agent stuck in repeated
    # failure, and the closed-loop watchdog's own re-wake/escalation — all three
    # are "a live agent needs a response right now", the exact definition above.
    "owner_decision_required", "agent_crash_loop",
    "wake_loop_no_progress", "wake_loop_stalled",
})


# Managed-agent LIFECYCLE terminals: the agent STOPPED, finished, or died. These were
# treated as generic history and shared the 900s floor, which is wrong for the same reason
# the actionable class exists at all. History is a record the assistant reads whenever it
# next looks; a managed agent that has stopped is a project standing still, and every
# minute it waits behind a generic backlog is a minute of work not happening.
#
# 2026-08-30, the case that forced this: event 15448 `work_stopped_incomplete` on
# `mess-opus:0.0` was detected correctly and immediately by the quiescence rule, then sat
# in the generic lane facing up to 900s before its project chat could be woken. Detection
# was never the problem; the lane was.
#
# Deliberately NARROW. `notification_dead_letter`, `notifications_red` and
# `notification_channel_down` are channel-health chatter, arrive constantly, and stay in
# the generic lane exactly as before — making those fast would be noise, not latency.
LIFECYCLE_EVENT_TYPES = frozenset({
    "work_stopped_incomplete",     # incl. the `quiescent` structural stop from agent_watch
    "task_completed",
    "agent_process_failed", "agent_dead",
})


def is_lifecycle(event_type: str = "") -> bool:
    """A managed agent that stopped, finished or died — a terminal state of real work."""
    return (event_type or "").strip() in LIFECYCLE_EVENT_TYPES


def is_actionable(event_type: str = "") -> bool:
    """Does this event get the FAST lane?

    Two kinds qualify, and they share one bounded floor rather than growing a third lane:
    a live agent waiting for a response now, and a managed agent whose work has stopped.
    A third lane would need its own lookback scope in BOTH the decision gate and the send
    gate, and getting exactly that scoping wrong is what starved the non-actionable lane
    twice already (`claim_send` and `should_wake`). One shared, already-correct floor is
    the safer shape.

    The cost is stated plainly: a `waiting_input` event can now queue behind a lifecycle
    event on the same route for at most ACTIONABLE_COOLDOWN_SECS. Its own floor is
    unchanged, and the alternative it replaces is a lifecycle stop waiting COOLDOWN_SECS.
    """
    t = (event_type or "").strip()
    return t in ACTIONABLE_EVENT_TYPES or t in LIFECYCLE_EVENT_TYPES


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
    if t in ACTIONABLE_EVENT_TYPES:
        return {"significant": True, "reason": "actionable_waiting_transition",
                "actionable": True}
    if is_lifecycle(t):
        # Same fast lane, distinct audit reason: the audit should say which authority
        # spoke, and "the agent stopped" is not "the agent is waiting for an answer".
        return {"significant": True, "reason": "lifecycle_terminal_transition",
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
# The base instruction. What used to be the ENTIRE submitted text (task 211 extends it
# with system-authored context fields below — never with anything read from a pane).
WAKE_PHRASE = os.getenv(
    "WAKE_BRIDGE_PHRASE",
    "Проверь новые события Owner OS через MCP и продолжи разрешённую работу.")


# ── contextual wake text (task 211) ─────────────────────────────────────────
# The companion used to submit ONE fixed phrase, carrying zero event content — by design,
# per the module docstring's injection defense: nothing typed into ChatGPT may ever be text
# that passed through a pane. That defense is preserved exactly as designed; what changes is
# that the phrase now also carries a handful of SYSTEM-composed identifiers — the event id,
# a closed-vocabulary trigger class, the route's project key, and a sanitized agent
# reference — so ChatGPT's own next turn (via MCP) can go straight to the right event
# instead of re-reading the whole inbox. None of these fields is ever pane free text: the
# event id is an integer, the trigger class comes from a fixed lookup table, the project key
# is validated by `wake_routes.normalize_key`, and the agent ref is stripped to a narrow
# identifier charset below.
TRIGGER_CLASS_COMPLETION = "completion"
TRIGGER_CLASS_BLOCKER = "blocker"
TRIGGER_CLASS_OWNER_DECISION = "owner_decision"
TRIGGER_CLASS_FAILURE = "failure"
TRIGGER_CLASS_LOOP_WATCHDOG = "loop_watchdog"
TRIGGER_CLASS_EVENT = "event"

_TRIGGER_CLASS_BY_EVENT_TYPE = {
    "task_completed": TRIGGER_CLASS_COMPLETION,
    "work_stopped_incomplete": TRIGGER_CLASS_COMPLETION,
    "agent_waiting_input": TRIGGER_CLASS_BLOCKER,
    "agent_needs_response": TRIGGER_CLASS_BLOCKER,
    "agent_prompt_needs_response": TRIGGER_CLASS_BLOCKER,
    "agent_blocked_on_owner": TRIGGER_CLASS_BLOCKER,
    "owner_gate_opened": TRIGGER_CLASS_OWNER_DECISION,
    "agent_owner_decision": TRIGGER_CLASS_OWNER_DECISION,
    "agent_waiting_owner": TRIGGER_CLASS_OWNER_DECISION,
    "owner_decision_required": TRIGGER_CLASS_OWNER_DECISION,
    "needs_owner_payload": TRIGGER_CLASS_OWNER_DECISION,
    "agent_dead": TRIGGER_CLASS_FAILURE,
    "agent_process_failed": TRIGGER_CLASS_FAILURE,
    "agent_crash_loop": TRIGGER_CLASS_FAILURE,
    "session_quarantined": TRIGGER_CLASS_FAILURE,
    "task_failed": TRIGGER_CLASS_FAILURE,
    "wake_loop_no_progress": TRIGGER_CLASS_LOOP_WATCHDOG,
    "wake_loop_stalled": TRIGGER_CLASS_LOOP_WATCHDOG,
}


def trigger_class_for(event_type: str = "") -> str:
    """Which of the five task-211 trigger classes this event type means. A closed
    lookup, never inferred from anything pane-derived."""
    return _TRIGGER_CLASS_BY_EVENT_TYPE.get((event_type or "").strip(), TRIGGER_CLASS_EVENT)


_TOKEN_RE = re.compile(r"[^A-Za-z0-9_]")
_AGENT_REF_RE = re.compile(r"[^A-Za-z0-9_:.\-]")


def _sanitize_token(text: str, limit: int = 40) -> str:
    """A closed-vocabulary identifier, defensively re-stripped even though callers only
    ever pass event types the code itself defined — never text read from a pane."""
    return _TOKEN_RE.sub("", (text or "").strip())[:limit]


def _sanitize_agent_ref(agent_id: str, limit: int = 80) -> str:
    """The tmux target / agent ref: an operator-chosen identifier, not pane content, but
    still narrowed to a safe identifier charset before it is typed into ChatGPT."""
    return _AGENT_REF_RE.sub("", (agent_id or "").strip())[:limit] or "unknown"


def compose_phrase(*, event_id, event_type: str = "", project_id: str = "",
                   agent_id: str = "", trigger_class: str = "") -> str:
    """The ONLY text the companion may submit, extended with system-authored context.

    Every field is either a number, drawn from a closed lookup table, or run through a
    validator/sanitizer — never free text echoed from an agent pane or a ChatGPT reply.
    """
    tc = (trigger_class or trigger_class_for(event_type) or TRIGGER_CLASS_EVENT)
    et = _sanitize_token(event_type) or "unknown"
    proj = wake_routes.normalize_key(project_id) or wake_routes.FALLBACK_ROUTE
    agent = _sanitize_agent_ref(agent_id)
    try:
        eid = int(event_id)
    except (TypeError, ValueError):
        eid = 0
    return (f"[Owner OS wake] event={eid} trigger={tc} type={et} project={proj} "
            f"agent={agent}. {WAKE_PHRASE}")

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
    # The RESOLVED route this decision belongs to. Cooldowns are per chat, and a
    # cooldown cannot be scoped to something the row does not remember. Rows from
    # before this column are NULL and are counted as owner-os traffic, which is
    # what they overwhelmingly were.
    ("route_key", "TEXT"),
    # task 211: the agent ref, persisted alongside project_id for the same reason —
    # the wake TEXT is composed fresh at delivery time and needs it there.
    ("agent_id", "TEXT"),
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
    # Created after the columns exist — `actionable` is itself a migrated column,
    # so indexing it before the ALTER would fail on a pre-existing database.
    # See _migrate_send for the measurements motivating this.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_wake_audit_lookback "
                 "ON wake_audit (decision, actionable, id)")
    # coalesce_generic_backlog's `NOT EXISTS (... WHERE w.event_id=a.event_id ...)`
    # self-join has no usable index without this: `ix_wake_audit_lookback` leads on
    # `decision`, so that correlated subquery can only index-seek to decision='wake'
    # and then linearly scan every such row (thousands, growing forever) checking
    # event_id by hand, once per OUTER candidate row. Measured live 2026-08-30: a
    # read-only run of the exact query hung past 30s against a 104k-row table with
    # no event_id index at all.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_wake_audit_event_decision "
                 "ON wake_audit (event_id, decision)")


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


# A cooldown protects ONE chat from being spammed. Applying it across chats meant
# the busiest route silenced every other one: owner-os traffic alone produced most
# of the 17k `cooldown_active` skips, and a MESS or payments agent waiting on the
# owner simply never rang while that ran. The floor is therefore matched per route.
#
# NULL route_key rows predate the column and are counted as owner-os, which is what
# they were - so owner-os keeps exactly the protection it has today, and a project
# route is no longer held down by traffic that was never going to its chat.
_ROUTE_MATCH = ("(COALESCE(NULLIF(route_key,''), ?) = ?)")


def _route_params(route_key: str) -> tuple:
    key = (route_key or wake_routes.FALLBACK_ROUTE).strip() or wake_routes.FALLBACK_ROUTE
    return (wake_routes.FALLBACK_ROUTE, key)


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
                f"AND {_ROUTE_MATCH} ORDER BY id DESC LIMIT 1",
                _route_params(target["route_key"])).fetchone()
            if last_a and (now - float(last_a[0] or 0)) < ACTIONABLE_COOLDOWN_SECS:
                wait = int(ACTIONABLE_COOLDOWN_SECS - (now - float(last_a[0])))
                return {"wake": False, "reason": "actionable_cooldown_active",
                        "wait_secs": wait, "actionable": True}
            return {"wake": True, "reason": "actionable_waiting_transition",
                    "actionable": True,
                    "phrase": compose_phrase(event_id=event_id, event_type=event_type,
                                             project_id=project_id, agent_id=agent_id),
                    "conversation": target["conversation"],
                    "route_key": target["route_key"],
                    "route_reason": target["route_reason"]}
        # Scope to NON-actionable wakes, mirroring the actionable branch above. This
        # was unscoped, so every actionable wake decision reset the non-actionable
        # floor — the same asymmetry that was fixed in `claim_send`, but at the
        # DECISION gate rather than the send gate. Fixing only the send gate was not
        # enough: an event skipped here never becomes a `wake` row at all, and
        # `_redecide_cooldown_skips` re-runs this same query and gets the same skip,
        # so it can never reach the claim.
        #
        # Found during the P0 acceptance canaries: event 13946
        # (`work_stopped_incomplete`, cp-canary) sat in `skip/cooldown_active`
        # indefinitely while the owner-os route's last NON-actionable claim was
        # 2230s old — far outside its own 900s window.
        last = conn.execute(
            f"SELECT ts FROM wake_audit WHERE decision='wake' "
            f"AND COALESCE(actionable,0)=0 AND {_ROUTE_MATCH} "
            "ORDER BY id DESC LIMIT 1", _route_params(target["route_key"])).fetchone()
        if last and (now - float(last[0] or 0)) < COOLDOWN_SECS:
            wait = int(COOLDOWN_SECS - (now - float(last[0])))
            return {"wake": False, "reason": "cooldown_active", "wait_secs": wait,
                    "actionable": False}
        return {"wake": True, "reason": "urgent_event_not_yet_signalled",
                "actionable": False,
                "phrase": compose_phrase(event_id=event_id, event_type=event_type,
                                         project_id=project_id, agent_id=agent_id),
                "conversation": target["conversation"],
                "route_key": target["route_key"], "route_reason": target["route_reason"]}
    finally:
        if own:
            conn.close()


def record(decision: dict, *, event_id: int, severity: str = "", event_type: str = "",
           correlation_id: str = "", project_id: str = "", agent_id: str = "", conn=None,
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
            "event_type,actionable,project_id,agent_id,route_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, now_iso(), int(event_id), correlation_id, severity,
             "wake" if decision.get("wake") else "skip", str(decision.get("reason"))[:160],
             (event_type or "").strip(), 1 if actionable else 0,
             (project_id or "").strip(), (agent_id or "").strip()[:200],
             (decision.get("route_key") or "").strip()))
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


# ── pipeline observability ──────────────────────────────────────────────────
# `health()` answers "did we wake recently, and did it land?". That is not enough
# to notice the pipeline STOPPING, and the gap was demonstrated live: event 9870
# was decided for the gaika-drop chat and sat undelivered for fifteen minutes
# while health looked perfectly green, because a DIFFERENT chat had just been
# delivered to. Nothing on the surface said "something decided is not moving".
#
# These thresholds bound "not moving". A pending wake older than
# WAKE_STUCK_PENDING_SECS is stuck; and if the companion has not even ATTEMPTED a
# claim within WAKE_COMPANION_SILENT_SECS while work is pending, the deliverer
# itself is down — a distinct failure from "delivery was refused", and the one
# that used to be invisible because the last successful delivery kept looking
# recent enough.
STUCK_PENDING_SECS = int(os.getenv("WAKE_STUCK_PENDING_SECS", "600"))
COMPANION_SILENT_SECS = int(os.getenv("WAKE_COMPANION_SILENT_SECS", "300"))
CONSECUTIVE_FAILURE_LIMIT = int(os.getenv("WAKE_CONSECUTIVE_FAILURE_LIMIT", "3"))


def pipeline_health(conn=None, now: Optional[float] = None) -> dict:
    """Is the wake pipeline MOVING? Read-only; emits nothing.

    Deliberately emits no event: the wake path feeding itself is a failure this
    system has already had (the self-feeding rewake chain), so this reports and
    lets a caller decide. Every number comes from the audit tables that already
    exist.
    """
    now = now if now is not None else now_ts()
    enabled, kill = _enabled()
    conn, own = _conn(conn)
    try:
        conn.execute(_SUBMIT_SCHEMA)
        conn.execute(_DELIVERY_SCHEMA)
        conn.execute(_SEND_SCHEMA)
        _migrate_send(conn)
        # The SAME per-event eligibility `pending_wake` uses, including the backoff
        # that benches an event after a failed delivery (a short fast lane for a
        # transient composer-lookup failure, the original floor for everything else).
        # Counting a benched event as pending would let health call the pipeline
        # stuck while the selector is correctly waiting out that event's retry
        # window - an alarm describing a queue nobody is actually trying to drain.
        all_undelivered = conn.execute(
            "SELECT event_id, COALESCE(route_key,''), ts, COALESCE(actionable,0) "
            "FROM wake_audit "
            "WHERE decision='wake' AND acknowledged=0 AND superseded_by IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM wake_submitted s WHERE s.event_id=wake_audit.event_id) "
            "ORDER BY id ASC").fetchall()
        pending, benched = [], 0
        for row in all_undelivered:
            eid = int(row[0])
            last = conn.execute(
                "SELECT ts, delivered FROM wake_delivery WHERE event_id=? "
                "ORDER BY id DESC LIMIT 1", (eid,)).fetchone()
            if last and not last[1] and \
                    now < float(last[0]) + _event_retry_backoff_secs(conn, eid):
                benched += 1
                continue
            pending.append(row)
        oldest_age = int(now - float(pending[0][2])) if pending else 0
        oldest_route = (pending[0][1] or wake_routes.FALLBACK_ROUTE) if pending else None

        # The companion writes a wake_send row on EVERY claim attempt, allowed or
        # refused, so its silence is the cleanest liveness signal available.
        last_send = conn.execute("SELECT ts FROM wake_send ORDER BY id DESC LIMIT 1").fetchone()
        claim_age = int(now - float(last_send[0])) if last_send else None

        tail = conn.execute("SELECT delivered FROM wake_delivery ORDER BY id DESC "
                            "LIMIT ?", (CONSECUTIVE_FAILURE_LIMIT,)).fetchall()
        consecutive_failures = 0
        for (ok,) in tail:
            if ok:
                break
            consecutive_failures += 1

        # Per-route backlog: a single blocked chat must be nameable, not averaged
        # away behind a healthy total.
        by_route: dict = {}
        for _eid, rk, ts, _act in pending:
            key = rk or wake_routes.FALLBACK_ROUTE
            age = int(now - float(ts))
            by_route[key] = max(by_route.get(key, 0), age)

        # A wake waiting out its OWN chat's cooldown is not stuck - it is the
        # cooldown doing its job, and it will fire when the floor clears. Calling
        # that "stuck" would be an alarm that cries wolf, which is how a detector
        # trains people to ignore it. Only a wake whose floor has ALREADY cleared
        # and which still has not gone out is genuinely not moving.
        def _cooldown_left(route: str, actionable: bool) -> int:
            floor = ACTIONABLE_COOLDOWN_SECS if actionable else COOLDOWN_SECS
            r = conn.execute(
                f"SELECT ts FROM wake_send WHERE allowed=1 AND {_ROUTE_MATCH} "
                + ("AND COALESCE(actionable,0)=1 " if actionable else "")
                + "ORDER BY id DESC LIMIT 1", _route_params(route)).fetchone()
            if not r:
                return 0
            elapsed = now - float(r[0] or 0)
            return int(floor - elapsed) if elapsed < floor else 0

        # PER ROUTE, like every other floor in this module. Judging only the
        # single oldest pending wake would let a genuinely stuck wake on one chat
        # hide behind a legitimately-waiting wake on another - the same
        # cross-chat blindness that caused the original defects.
        oldest_per_route: dict = {}
        for eid, rk, ts, act in pending:
            key = rk or wake_routes.FALLBACK_ROUTE
            if key not in oldest_per_route or float(ts) < oldest_per_route[key][1]:
                oldest_per_route[key] = (eid, float(ts), bool(act))
        stuck_routes, waiting_routes, claimable_routes = [], [], []
        for key, (eid, ts, act) in oldest_per_route.items():
            age = int(now - ts)
            left = _cooldown_left(key, act)
            if left:
                waiting_routes.append((key, left))
                continue
            # Claimable NOW: its floor has cleared, whatever its age. This is the
            # set that says whether the deliverer has anything to do, which is a
            # different question from whether a wake has waited too long.
            claimable_routes.append(key)
            if age > STUCK_PENDING_SECS:
                stuck_routes.append((key, age))
        cooldown_remaining = max((l for _k, l in waiting_routes), default=0)

        reasons = []
        if kill:
            reasons.append("kill_switch_engaged")
        if not enabled:
            reasons.append("bridge_disabled")
        for key, age in sorted(stuck_routes, key=lambda x: -x[1]):
            reasons.append(f"pending_wake_stuck:{key}:{age}s")
        # A dead deliverer must be caught while the work is FRESH; tying this to
        # the stuck age would hide a crashed companion for the whole threshold.
        if (claimable_routes and claim_age is not None
                and claim_age > COMPANION_SILENT_SECS):
            reasons.append(f"companion_silent:{claim_age}s_with_{len(pending)}_pending")
        if claimable_routes and claim_age is None:
            reasons.append("companion_never_claimed")
        if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
            reasons.append(f"consecutive_delivery_failures:{consecutive_failures}")
        skew = worker_skew(conn=conn, now=now)
        for w in skew:
            reasons.append(f"worker_running_stale_code:{w['worker']}:"
                           f"started_{w['started_at_age_secs']}s_ago")

        status = "ok"
        if reasons:
            status = "stuck" if any(r.startswith(("pending_wake_stuck", "companion_silent",
                                                  "companion_never_claimed",
                                                  "consecutive_delivery_failures",
                                                  "worker_running_stale_code"))
                                    for r in reasons) else "disabled"
        if waiting_routes and status == "ok":
            # Reported, not alarmed: the owner can see WHY nothing is moving.
            for key, left in sorted(waiting_routes, key=lambda x: -x[1]):
                reasons.append(f"waiting_on_cooldown:{key}:{left}s")
            status = "waiting"
        return {"status": status, "reasons": reasons,
                "worker_skew": skew,
                "cooldown_remaining_secs": cooldown_remaining,
                "stuck_routes": dict(stuck_routes),
                "waiting_routes": dict(waiting_routes),
                "pending_count": len(pending),
                "benched_after_failure": int(benched),
                "pending_oldest_age_secs": oldest_age,
                "pending_oldest_route": oldest_route,
                "pending_by_route": by_route,
                "last_claim_attempt_age_secs": claim_age,
                "consecutive_delivery_failures": consecutive_failures,
                "thresholds": {"stuck_pending_secs": STUCK_PENDING_SECS,
                               "companion_silent_secs": COMPANION_SILENT_SECS,
                               "consecutive_failure_limit": CONSECUTIVE_FAILURE_LIMIT}}
    finally:
        if own:
            conn.close()


# ── deployer version skew ───────────────────────────────────────────────────
# The wake companion is a SEPARATE long-running process that imports this module
# at startup. Restarting the API alone therefore leaves the deliverer running the
# OLD code, and that is not theoretical: after the routing fix went live, the API
# decided a wake for the gaika-drop chat while the stale companion delivered it
# to owner-os and logged `[route owner-os]` for it. Same database, two versions
# of the truth, wrong chat.
#
# A worker records when it started; if this module's source is NEWER than that,
# the worker is running code that no longer exists on disk and must be restarted.
# No hashing and no version string to keep in sync - the file's own mtime is the
# fact that matters.
_WORKER_SCHEMA = """
CREATE TABLE IF NOT EXISTS wake_worker (
    worker TEXT PRIMARY KEY, pid INTEGER, started_ts REAL, started_at TEXT,
    last_seen_ts REAL, last_seen_at TEXT
)
"""


# Which source files matter varies by worker - the wake companion cares about
# its own delivery code, the agent orchestrator cares about the classifier it
# imports (this is how event 11073 happened: agent_control.py got four fixes
# across 2026-08-28 but ai-runtime.service, which owns the orchestrator loop,
# was only ever restarted for the first one).
# `..` entries are relative to this file's directory (core/), so a tools/ module
# is reachable — see _module_mtime.
_WORKER_WATCHED_FILES = {
    # The companion's delivery code is NOT only this module. It imports
    # tools/cdp_composer.py for submit_phrase — the composer selectors, the latch
    # boundary, page_responsive/recover_wedged_tab and the whole verification
    # loop live there — and tools/wake_companion.py is its own entrypoint. A fix
    # to either changed how wakes are delivered while raising no skew at all,
    # which is exactly the failure this mechanism exists to catch.
    # tmux_control.py joins the list for the same reason: the companion now runs the
    # control-plane guard first in every tick, so a fix to the probe or the repair is a
    # fix to how the companion sees the fleet at all.
    "wake_companion": ("wake_bridge.py", "wake_routes.py", "closed_loop_wake.py",
                       "tmux_control.py", "agent_watch.py",
                       os.path.join("..", "tools", "cdp_composer.py"),
                       os.path.join("..", "tools", "wake_companion.py")),
    "agent_orchestrator": ("agent_control.py", "agent_orchestrator.py",
                            os.path.join("control_plane", "waiting_transitions.py")),
}


def _module_mtime(worker: str = "wake_companion") -> float:
    """Newest mtime across the modules this worker actually runs."""
    newest = 0.0
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in _WORKER_WATCHED_FILES.get(worker, _WORKER_WATCHED_FILES["wake_companion"]):
        try:
            newest = max(newest, os.path.getmtime(os.path.join(here, rel)))
        except OSError:
            pass
    return newest


def register_worker(worker: str, conn=None, now: Optional[float] = None) -> dict:
    """A deliverer announces itself. Called on start and refreshed as it runs."""
    now = now if now is not None else now_ts()
    conn, own = _c(conn)
    try:
        conn.execute(_WORKER_SCHEMA)
        row = conn.execute("SELECT started_ts, pid FROM wake_worker WHERE worker=?",
                           (worker,)).fetchone()
        pid = os.getpid()
        if row and int(row[1] or 0) == pid:
            # Same process: a heartbeat. It must NOT move started_ts, or a busy
            # stale worker would keep clearing its own alarm.
            conn.execute("UPDATE wake_worker SET last_seen_ts=?, last_seen_at=? "
                         "WHERE worker=?", (now, now_iso(), worker))
        elif row:
            # A DIFFERENT pid is a genuine restart, which is exactly how stale
            # code gets fixed - so the clock restarts here. Without this the skew
            # alarm could never clear, and an alarm that cannot clear is worse
            # than no alarm: it trains everyone to ignore it.
            conn.execute("UPDATE wake_worker SET pid=?, started_ts=?, started_at=?, "
                         "last_seen_ts=?, last_seen_at=? WHERE worker=?",
                         (pid, now, now_iso(), now, now_iso(), worker))
            row = None
        else:
            conn.execute("INSERT INTO wake_worker (worker,pid,started_ts,started_at,"
                         "last_seen_ts,last_seen_at) VALUES (?,?,?,?,?,?)",
                         (worker, os.getpid(), now, now_iso(), now, now_iso()))
        conn.commit()
        return {"worker": worker, "started_ts": (row[0] if row else now)}
    finally:
        if own:
            conn.close()


def worker_skew(conn=None, now: Optional[float] = None) -> list:
    """Workers whose start predates the code they are supposed to be running."""
    now = now if now is not None else now_ts()
    conn, own = _c(conn)
    try:
        conn.execute(_WORKER_SCHEMA)
        out = []
        for worker, started, last_seen in conn.execute(
                "SELECT worker, started_ts, last_seen_ts FROM wake_worker"):
            code_mtime = _module_mtime(worker)
            if started and code_mtime > float(started):
                out.append({"worker": worker,
                            "started_at_age_secs": int(now - float(started)),
                            "code_newer_by_secs": int(code_mtime - float(started)),
                            "last_seen_age_secs": int(now - float(last_seen or 0))})
        return out
    finally:
        if own:
            conn.close()


PIPELINE_WATCH_INTERVAL_SECS = int(os.getenv("WAKE_PIPELINE_WATCH_SECS", "120"))


async def pipeline_watch_loop(log=None, sleep=None) -> None:
    """Say out loud when the pipeline stops moving.

    An endpoint nobody polls is not detection. This logs a transition into and
    out of a stuck state, so a wake that is decided and never delivered - or a
    companion process that has died - shows up in the service log the owner
    already reads, instead of being noticed as silence hours later.

    Log-only ON PURPOSE: it emits no event and actuates nothing, because the wake
    path feeding itself is a failure this system has already had. It also logs
    only on CHANGE, so a long outage is one line, not a stream.
    """
    import asyncio
    log = log or (lambda level, msg: None)
    sleep = sleep or asyncio.sleep
    previous = "ok"
    while True:
        try:
            h = pipeline_health()
            status = h.get("status", "ok")
            if status != previous:
                if status == "ok":
                    log("info", "wake pipeline recovered — deliveries moving again")
                else:
                    log("warning",
                        f"wake pipeline {status}: {'; '.join(h.get('reasons') or [])} "
                        f"(pending={h.get('pending_count')}, "
                        f"oldest={h.get('pending_oldest_age_secs')}s "
                        f"route={h.get('pending_oldest_route')}, "
                        f"last_claim={h.get('last_claim_attempt_age_secs')}s ago)")
                previous = status
        except Exception as e:  # noqa: BLE001 — a watcher must never kill the daemon
            log("warning", f"wake pipeline watch error: {type(e).__name__}: {e}")
        await sleep(PIPELINE_WATCH_INTERVAL_SECS)


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
        # Wakes whose phrase was latched (composer observed cleared) but whose
        # delivery was never confirmed, then aged out. Invisible everywhere else by
        # design — `expire_stale` excludes them, `should_wake` refuses them as
        # already-woken — so health is the only place the unresolved outcome shows.
        conn.execute(_ABANDON_SCHEMA)
        abandoned_total = conn.execute("SELECT COUNT(*) FROM wake_abandoned").fetchone()[0]
        ab = conn.execute("SELECT at,event_id,last_delivery_reason FROM wake_abandoned "
                          "ORDER BY ts DESC LIMIT 1").fetchone()
        return {"enabled": enabled, "kill_switch": kill,
                "abandoned_total": int(abandoned_total),
                "last_abandoned_at": (ab[0] if ab else None),
                "last_abandoned_event_id": (int(ab[1]) if ab else None),
                "last_abandoned_reason": (ab[2] if ab else None),
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
                "pipeline": pipeline_health(conn=conn, now=now),
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


def _redecide_cooldown_skips(conn, now: float) -> list:
    """A decision refused ONLY for a cooldown is not a verdict, it is bad timing.

    Event 4187 (a live agent prompt) landed 11 seconds after another actionable send,
    was skipped `actionable_cooldown_active`, and then had no path back: selection serves
    only decision='wake' rows, so the event stayed silent until the hourly reminder.
    Here, cooldown skips are re-decided once their event is still unserved; a re-decision
    is RECORDED only when it changes the answer (a wake, or a different refusal), so a
    floor still running does not mint an audit row per poll.

    Event 4619 (2026-08-15 incident): emitted 2026-08-14T21:46Z, skipped `cooldown_active`
    within the same second, sat unserved, and THIS function re-decided it to `wake`
    almost 24h later — the `ts > now-86400` window here is keyed on the SKIP DECISION's
    own timestamp, which for a skip minted at emission time is (by construction) always
    within 86400s of "now" for roughly the event's first 24 hours of existence. That
    window is wider than `MAX_WAKE_AGE_SECS` (the delivery-side staleness ceiling), so it
    could mint a FRESH wake decision for an event already past that ceiling — and
    `expire_stale` runs BEFORE this function on the same tick, so the newly-minted row
    would not be caught until the FOLLOWING tick, by which point selection may already
    have delivered it. Bounding the redecision window itself to `MAX_WAKE_AGE_SECS`
    closes the gap at its source: an event that could never survive `expire_stale` is
    never handed a fresh decision to survive with in the first place. Joined against the
    EVENT's own `ts_epoch` (not the skip's ts) for the same reason `expire_stale` now is —
    a replayed/re-decided row can never make the event itself younger.
    """
    rows = conn.execute(
        "SELECT a.event_id, a.severity, a.event_type, COALESCE(a.project_id,''), "
        "a.correlation_id, COALESCE(a.agent_id,'') FROM wake_audit a "
        "LEFT JOIN event e ON e.id = a.event_id WHERE a.id IN ("
        "  SELECT MAX(id) FROM wake_audit WHERE decision='skip' "
        "  AND reason IN ('cooldown_active','actionable_cooldown_active') "
        "  AND ts > ? GROUP BY event_id) "
        # No matching event row (a test/edge case that decided directly, bypassing the
        # durable log) means the event's age is UNKNOWN — that must never itself block
        # a redecision that would otherwise have fired; only a CONFIRMED old event does.
        "AND (e.ts_epoch IS NULL OR e.ts_epoch > ?) "
        "AND NOT EXISTS (SELECT 1 FROM wake_audit w WHERE w.event_id=a.event_id "
        "               AND w.decision='wake') "
        "AND NOT EXISTS (SELECT 1 FROM wake_submitted s WHERE s.event_id=a.event_id)",
        (now - 86400, now - MAX_WAKE_AGE_SECS)).fetchall()
    woken = []
    for event_id, severity, etype, project, corr, agent_id in rows:
        d = should_wake(event_id=int(event_id), severity=severity or "",
                        event_type=etype or "", correlation_id=corr or "",
                        project_id=project, agent_id=agent_id, conn=conn, now=now)
        if d.get("wake") or d.get("reason") not in (
                "cooldown_active", "actionable_cooldown_active"):
            record(d, event_id=int(event_id), severity=severity or "",
                   event_type=etype or "", correlation_id=corr or "",
                   project_id=project, agent_id=agent_id, conn=conn, now=now)
            if d.get("wake"):
                woken.append(int(event_id))
    return woken


def _agent_key_of(correlation_id: str) -> str:
    """The stable agent identity inside a correlation id. Both emitters tag their events
    with the pane target — `waiting:<target>` and `agentwatch:<target>` — so the target
    is the semantic identity; an unrecognized correlation is its own key, and an empty
    one is no key at all (never grouped)."""
    c = (correlation_id or "").strip()
    for prefix in ("waiting:", "agentwatch:"):
        if c.startswith(prefix):
            return c[len(prefix):]
    return c


def _supersede_stale_actionables(conn, now: float) -> list:
    """One agent, one pending doorbell ring. An actionable wake means "this agent is
    waiting NOW"; a newer wake for the same agent describes the same or a newer now, so
    every older unserved copy is obsolete the moment it exists — and during the
    2026-08-14 incident seventeen such copies queued ahead of fresh events, each
    consuming a claim slot in turn. Older copies are RETIRED with the same audited
    supersede mechanics as generic coalescing: pointer kept, provenance row written,
    nothing deleted."""
    rows = conn.execute(
        "SELECT a.id, a.event_id, a.correlation_id FROM wake_audit a "
        "WHERE a.decision='wake' AND a.acknowledged=0 AND a.superseded_by IS NULL "
        "AND COALESCE(a.actionable,0)=1 AND NOT EXISTS "
        "(SELECT 1 FROM wake_submitted s WHERE s.event_id=a.event_id) "
        "ORDER BY a.event_id ASC").fetchall()
    groups: dict = {}
    for aid, eid, corr in rows:
        key = _agent_key_of(corr)
        if key:
            groups.setdefault(key, []).append((aid, eid))
    superseded = []
    reason = "superseded_by_newer_actionable_same_agent"
    for key, members in groups.items():
        if len(members) < 2:
            continue
        keep_aid, keep_eid = members[-1]
        for aid, eid in members[:-1]:
            conn.execute("UPDATE wake_audit SET superseded_by=?, superseded_at=?, "
                         "superseded_reason=? WHERE id=?",
                         (keep_aid, now_iso(), reason, int(aid)))
            conn.execute(
                "INSERT INTO wake_coalesce_audit (ts,at,wake_audit_id,event_id,"
                "superseded_by_audit_id,superseded_by_event_id,reason) VALUES (?,?,?,?,?,?,?)",
                (now, now_iso(), int(aid), int(eid), keep_aid, keep_eid, reason))
            superseded.append(int(eid))
    if superseded:
        conn.commit()
    return superseded


_EXPIRE_SCHEMA = """
CREATE TABLE IF NOT EXISTS wake_expire_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, at TEXT, event_id INTEGER, reason TEXT, age_secs REAL
)
"""


def _invalid_event_ids(conn, event_ids: list) -> set:
    """Cross-reference `agent_watch`'s audited invalid-alert overlay — a proven-false
    alert (recovered crash, resolved stall episode, or an owner/coordinator manually
    retiring known-bad rows) must never be delivered late just because nothing had
    acknowledged it yet. That table is owned by agent_watch, not this module; reading it
    here is read-only and defensive — a DB that predates the overlay table (or one where
    it has not been created yet) reports no invalid ids rather than raising."""
    if not event_ids:
        return set()
    try:
        placeholders = ",".join("?" * len(event_ids))
        rows = conn.execute(
            f"SELECT event_id FROM agent_alert_invalid WHERE event_id IN ({placeholders})",
            tuple(int(e) for e in event_ids)).fetchall()
        return {int(r[0]) for r in rows}
    except Exception:  # noqa: BLE001 — table may not exist in this DB yet
        return set()


_ABANDON_SCHEMA = """
CREATE TABLE IF NOT EXISTS wake_abandoned (
    event_id INTEGER PRIMARY KEY,
    ts REAL, at TEXT, reason TEXT, last_delivery_reason TEXT, age_secs REAL
)
"""


def record_abandoned_wakes(conn=None, now: Optional[float] = None) -> list:
    """Record wakes that were SUBMITTED but never proven delivered, and are now too
    old to ever resolve.

    `expire_stale` deliberately excludes these — its query carries
    `AND NOT EXISTS (SELECT 1 FROM wake_submitted ...)` — because a phrase that may
    already sit in the owner's chat must never be re-offered. That rule is correct
    and is NOT changed here.

    The gap it left is that such an event simply stopped: never retried, never
    superseded, never expired, and absent from `wake_expire_audit`.

    What these events are, precisely: `cdp_composer` latches `wake_submitted` ONLY
    after it observes the composer cleared — "the page took the phrase". So a
    latched event's phrase almost certainly DID reach the chat; what was never
    confirmed is that the assistant then started. That is a weaker failure than a
    lost alert, and the record says so rather than overstating it.

    Observed 2026-08-29/30: events 12531, 11659, 11233 (`agent_waiting_input`,
    high, oar=1) and 12370 (`notifications_red`, critical) each latched, then hit
    `cdp_error:WebSocketTimeoutException` during post-send verification, and went
    silent for 12-24h with nothing recording the unresolved outcome.

    This turns that silent drop into a visible one. It records; it never re-offers,
    so the no-duplicate invariant holds by construction.

    Deliberately does NOT emit a control-plane event: an abandonment event would
    itself become a wake candidate, which could fail delivery and be abandoned in
    turn. A durable audit row is the record; a feedback loop is not.
    """
    now = now if now is not None else now_ts()
    conn, own = _c(conn)
    try:
        conn.execute(_SCHEMA)
        _migrate(conn)
        conn.execute(_SUBMIT_SCHEMA)
        conn.execute(_DELIVERY_SCHEMA)
        conn.execute(_ABANDON_SCHEMA)
        rows = conn.execute(
            "SELECT a.event_id, a.ts, e.ts_epoch FROM wake_audit a "
            "LEFT JOIN event e ON e.id = a.event_id "
            "WHERE a.decision='wake' AND a.acknowledged=0 "
            "AND a.superseded_by IS NULL "
            "AND EXISTS (SELECT 1 FROM wake_submitted s WHERE s.event_id=a.event_id) "
            "AND NOT EXISTS (SELECT 1 FROM wake_delivery d "
            "                WHERE d.event_id=a.event_id AND d.delivered=1) "
            "AND NOT EXISTS (SELECT 1 FROM wake_abandoned b WHERE b.event_id=a.event_id)"
        ).fetchall()
        out = []
        for event_id, decision_ts, event_ts_epoch in rows:
            decision_age = now - float(decision_ts or 0)
            event_age = (now - float(event_ts_epoch)) if event_ts_epoch is not None else None
            age = event_age if event_age is not None else decision_age
            if age <= MAX_WAKE_AGE_SECS:
                continue          # still inside its window; may yet be proven
            last = conn.execute(
                "SELECT reason FROM wake_delivery WHERE event_id=? ORDER BY id DESC LIMIT 1",
                (int(event_id),)).fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO wake_abandoned "
                "(event_id,ts,at,reason,last_delivery_reason,age_secs) VALUES (?,?,?,?,?,?)",
                (int(event_id), now, now_iso(), "submitted_delivery_unproven",
                 (last[0] if last else "") or "", age))
            # Same retirement mechanism expire_stale uses: the doorbell stops, the
            # event stays fully readable in the durable CTO inbox. It was already
            # unreachable via should_wake's already_woke_for_this_event rule; this
            # only makes the terminal state explicit instead of implicit.
            conn.execute("UPDATE wake_audit SET acknowledged=1, acknowledged_at=? "
                         "WHERE event_id=? AND decision='wake'", (now_iso(), int(event_id)))
            out.append({"event_id": int(event_id),
                        "reason": "submitted_delivery_unproven",
                        "last_delivery_reason": (last[0] if last else "") or "",
                        "age_secs": int(age)})
        if out:
            conn.commit()
        return out
    finally:
        if own:
            conn.close()


def abandoned_wakes(conn=None, limit: int = 50) -> list:
    """The abandonment log, newest first — for health surfaces and the owner."""
    conn, own = _c(conn)
    try:
        conn.execute(_ABANDON_SCHEMA)
        return [dict(zip(("event_id", "at", "reason", "last_delivery_reason", "age_secs"), r))
                for r in conn.execute(
                    "SELECT event_id,at,reason,last_delivery_reason,age_secs "
                    "FROM wake_abandoned ORDER BY ts DESC LIMIT ?", (int(limit),))]
    finally:
        if own:
            conn.close()


def expire_stale(conn=None, now: Optional[float] = None) -> list:
    """Retire (acknowledge) every decided-but-undelivered wake that is either past
    `MAX_WAKE_AGE_SECS` or has since been marked invalid by agent_watch/stall_doctor's
    audited overlay. Retirement uses the SAME mechanism as a normal acknowledgement —
    the event stays fully readable in the durable CTO inbox; only the doorbell stops.
    Every retirement is itself audited (`wake_expire_audit`), so "why did this old
    event never wake anyone" stays answerable from state alone.

    Staleness is checked against TWO independent clocks, either one sufficient:

      * the wake DECISION's own age (`stale_past_max_age`) — a decision minted long
        ago and never delivered (a chronically broken delivery path);
      * the underlying EVENT's own emission age (`event_older_than_max_age`) — event
        4619 (2026-08-15 incident): emitted 2026-08-14T21:46Z, skipped `cooldown_active`
        within the same second, then RE-DECIDED to `wake` almost 24h later by
        `_redecide_cooldown_skips` (a skip refused only for timing is re-considered
        while its event is still "recent" by ITS OWN 24h window) — the fresh decision
        timestamp made a day-old event look brand new to the decision-age check alone,
        and it was delivered ~24h late. The event's own `ts_epoch` (from the durable
        event log, joined by id) is the clock a replayed/re-decided row can never
        fake: whatever re-minted the decision, the event itself did not get younger.
    """
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        conn.execute(_EXPIRE_SCHEMA)
        conn.execute(_SUBMIT_SCHEMA)
        rows = conn.execute(
            "SELECT a.event_id, a.ts, e.ts_epoch FROM wake_audit a "
            "LEFT JOIN event e ON e.id = a.event_id "
            "WHERE a.decision='wake' AND a.acknowledged=0 "
            "AND a.superseded_by IS NULL AND NOT EXISTS "
            "(SELECT 1 FROM wake_submitted s WHERE s.event_id=a.event_id)"
        ).fetchall()
        if not rows:
            # The abandonment sweep must still run: its set is the one this query
            # EXCLUDES, so "nothing to expire" says nothing about it. Returning
            # early here skipped it in the normal case.
            record_abandoned_wakes(conn=conn, now=now)
            return []
        event_ids = [int(r[0]) for r in rows]
        invalid_ids = _invalid_event_ids(conn, event_ids)
        expired = []
        for event_id, decision_ts, event_ts_epoch in rows:
            decision_age = now - float(decision_ts or 0)
            # A missing event row (should not happen — the event log is append-only
            # and never deletes) means the event-age signal is UNKNOWN, not old: the
            # check is skipped entirely rather than silently falling back to the
            # decision's own ts, which would relabel an ordinary `stale_past_max_age`
            # retirement as `event_older_than_max_age` on no real evidence.
            event_age = (now - float(event_ts_epoch)) if event_ts_epoch is not None else None
            if event_id in invalid_ids:
                reason = "marked_invalid"
            elif event_age is not None and event_age > MAX_WAKE_AGE_SECS:
                reason = "event_older_than_max_age"
            elif decision_age > MAX_WAKE_AGE_SECS:
                reason = "stale_past_max_age"
            else:
                continue
            age_for_audit = event_age if event_age is not None else decision_age
            conn.execute("UPDATE wake_audit SET acknowledged=1, acknowledged_at=? "
                         "WHERE event_id=? AND decision='wake'", (now_iso(), event_id))
            conn.execute(
                "INSERT INTO wake_expire_audit (ts,at,event_id,reason,age_secs) "
                "VALUES (?,?,?,?,?)",
                (now, now_iso(), int(event_id), reason, age_for_audit))
            expired.append({"event_id": int(event_id), "reason": reason,
                            "age_secs": int(age_for_audit)})
        if expired:
            conn.commit()
        # Sibling sweep for the set this function deliberately excludes: submitted
        # but never proven delivered. Same tick, so no new scheduler is needed.
        record_abandoned_wakes(conn=conn, now=now)
        return expired
    finally:
        if own:
            conn.close()


def _is_transient_failure(reason: str) -> bool:
    r = reason or ""
    return any(r.startswith(p) for p in TRANSIENT_FAILURE_PREFIXES)


def _consecutive_transient_failures(conn, event_id: int) -> int:
    """How many of the most recent, UNBROKEN delivery attempts for this event were the
    transient composer-lookup failure. A success or a DIFFERENT failure reason ends the
    streak immediately — the fast lane is only for repeats of this one transient shape,
    never a general "this event has failed before" counter."""
    rows = conn.execute(
        "SELECT delivered, reason FROM wake_delivery WHERE event_id=? "
        "ORDER BY id DESC LIMIT ?",
        (int(event_id), TRANSIENT_RETRY_MAX_ATTEMPTS + 1)).fetchall()
    streak = 0
    for delivered, reason in rows:
        if delivered or not _is_transient_failure(reason):
            break
        streak += 1
    return streak


def _event_retry_backoff_secs(conn, event_id: int) -> int:
    """The bench window THIS event's most recent failure earns. A transient
    composer-lookup failure gets the short, bounded fast lane; a non-transient failure,
    or a transient one repeated past the attempt cap, keeps the original floor that
    event 4214 exists to enforce — the fast lane never becomes an unbounded hot-loop."""
    streak = _consecutive_transient_failures(conn, event_id)
    if 0 < streak <= TRANSIENT_RETRY_MAX_ATTEMPTS:
        return TRANSIENT_RETRY_BACKOFF_SECS
    return RETRY_BACKOFF_SECS


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
        # Stale or proven-invalid wakes are retired BEFORE anything else is considered —
        # a days-old actionable event must never be selected fresh just because delivery
        # only now started working again.
        expire_stale(conn=conn, now=now if now is not None else now_ts())
        # Cooldown refusals get a second hearing once the floor has cleared.
        _redecide_cooldown_skips(conn, now if now is not None else now_ts())
        # A second pass, deliberately redundant with the one above: a re-decision can
        # mint a FRESH wake row for an event that is not fresh at all (event 4619), and
        # that new row must never survive to selection below on the SAME tick just
        # because the first expire_stale ran before it existed. `_redecide_cooldown_skips`
        # is now itself bounded by event age, so this should be a no-op in the steady
        # state; it stays as the structural backstop for whatever re-decision path is
        # added next and forgets to check.
        expire_stale(conn=conn, now=now if now is not None else now_ts())
        # One agent, one pending ring: older unserved actionables for the same agent are
        # retired so a stale copy can never head-of-line block a fresh event.
        _supersede_stale_actionables(conn, now if now is not None else now_ts())
        # Fold the generic backlog down to its newest member PER ROUTE first: N generic
        # wakes for one chat are one instruction, but wakes for different chats are not
        # copies of each other and are never folded across routes.
        coalesced = coalesce_generic_backlog(conn=conn, now=now)
        # Actionable first, then oldest — the only ordering change. Within a class the old
        # oldest-first behaviour is untouched, so nothing is starved, it is merely outranked.
        conn.execute(_DELIVERY_SCHEMA)
        now_ = now if now is not None else now_ts()
        # Every eligible candidate, un-benched — the per-event backoff below (transient
        # composer failures get a short fast lane, everything else keeps the original
        # floor) cannot be expressed as one fixed cutoff, so it is applied in Python over
        # a bounded scan instead of as a single static SQL predicate.
        candidates = conn.execute(
            "SELECT a.event_id, COALESCE(a.actionable,0), "
            "COALESCE(a.project_id,''), COALESCE(a.event_type,''), "
            "COALESCE(a.agent_id,'') FROM wake_audit a "
            "WHERE a.decision='wake' AND a.acknowledged=0 "
            "AND a.superseded_by IS NULL AND NOT EXISTS "
            "(SELECT 1 FROM wake_submitted s WHERE s.event_id=a.event_id) "
            "ORDER BY COALESCE(a.actionable,0) DESC, a.id ASC "
            "LIMIT ?", (_CANDIDATE_SCAN_LIMIT,)).fetchall()
        r = None
        benched = None  # (event_id, retry_at, attempt, transient) for the head of line
        for cand in candidates:
            eid = int(cand[0])
            last = conn.execute(
                "SELECT ts, delivered FROM wake_delivery WHERE event_id=? "
                "ORDER BY id DESC LIMIT 1", (eid,)).fetchone()
            if last and not last[1]:
                streak = _consecutive_transient_failures(conn, eid)
                fast_lane = 0 < streak <= TRANSIENT_RETRY_MAX_ATTEMPTS
                backoff = TRANSIENT_RETRY_BACKOFF_SECS if fast_lane else RETRY_BACKOFF_SECS
                retry_at = float(last[0]) + backoff
                if now_ < retry_at:
                    if benched is None:
                        benched = (eid, retry_at, streak, fast_lane)
                    continue
            r = cand
            break
        if not r:
            result = {"pending": False, "reason": "nothing_to_wake_for",
                      "coalesced": coalesced["superseded_event_ids"]}
            if benched is not None:
                eid, retry_at, attempt, transient = benched
                result.update({"reason": "retry_backoff_pending", "event_id": eid,
                               "attempt": attempt, "transient_retry": transient,
                               "next_retry_in_secs": max(0, int(retry_at - now_))})
            return result
        # FAIL CLOSED per event: an event whose route cannot be resolved offers nothing,
        # rather than borrowing whichever chat another event would have used.
        # Re-resolved with the SAME inputs the decision used - project AND agent
        # ref. Passing only the project was a wrong-chat bug: event 9868 matched
        # the agent registry through its agent ref at decision time (gaika-drop)
        # and missed it here, so a wake decided for the project chat was
        # delivered to the owner-os control chat. Fresh resolution is still the
        # point (a rebind between decision and submission must take effect); it
        # just has to be the same QUESTION, not a narrower one.
        target = wake_routes.resolve(project_id=r[2], agent_id=r[4], conn=conn)
        if not target.get("bound"):
            return {"pending": False, "reason": target.get("reason", "no_route_bound"),
                    "event_id": int(r[0]), "route_key": target.get("route_key")}
        event_id, actionable, project_id, event_type, agent_id = r
        return {"pending": True, "event_id": int(event_id), "actionable": bool(actionable),
                "conversation": target["conversation"],
                "phrase": compose_phrase(event_id=event_id, event_type=event_type,
                                         project_id=project_id, agent_id=agent_id),
                "trigger_class": trigger_class_for(event_type),
                "agent_id": agent_id, "event_type": event_type,
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
        # Include rows still sitting at decision='skip' for a cooldown, not only
        # those that already reached 'wake'.
        #
        # The `wake`-only filter was the whole reason a backlog could exist at all.
        # A generic wake refused by the non-actionable floor never becomes a `wake`
        # row, so coalescing never saw it: 68 identical `notification_dead_letter`
        # skips (one persistent Telegram outage, no agent) queued as 68 separate
        # candidates, each re-decided by `_redecide_cooldown_skips` and each taking
        # its own 900s slot — ~21.5h of lane time carrying one instruction, while
        # fresh events (a canary's `work_stopped_incomplete`, an
        # `agent_process_failed`) waited behind them.
        #
        # The docstring's own argument applies unchanged to a skip: the phrase is
        # fixed and carries no event content, so N queued copies for one chat are N
        # copies of one identical instruction. Folding them raises no wake
        # frequency — it removes redundant candidates. The newest per route
        # survives and still says "go read Owner OS", and the CTO inbox keeps every
        # event regardless. Superseded rows are retired, never deleted.
        #
        # The skip branch is bounded to the SAME `MAX_WAKE_AGE_SECS` ceiling
        # `_redecide_cooldown_skips` already uses for its own event-age check: past
        # that ceiling a skip can never be redecided into `wake` again (that
        # function excludes it), so it can never become a live delivery candidate
        # either way — coalescing it buys nothing. Without this bound the query
        # re-resolves every historical `cooldown_active` skip ever written (3000+
        # weeks-old rows, most with no stored route_key, each needing a fresh
        # `wake_routes.resolve()` call) on EVERY tick forever, since most never
        # share a route with another row and so never collapse away. Found live:
        # a plain read of this exact predicate, and a direct call to this
        # function, both hung past 30s against the production db. Unknown age
        # (no matching event row) is never a reason to exclude — same convention
        # as `expire_stale`/`_redecide_cooldown_skips`.
        # The age bound applies to BOTH branches, not just skip. A `wake`-decision
        # row whose event is already past MAX_WAKE_AGE_SECS is not protected by
        # expire_stale here — that runs once per tick, but a row can sit as this
        # group's "kept" survivor across MULTIPLE ticks (e.g. its route is
        # contended) and cross the age threshold WHILE it holds that position,
        # absorbing fresher members via coalescing before expire_stale ever
        # catches it. Once it expires, every member folded into it is permanently
        # orphaned: their own rows are `superseded_by` a row that will never
        # deliver, and superseded rows are excluded from every future candidate
        # query, so they can never be reconsidered either. Reproduced live
        # 2026-08-30: a fresh canary work_stopped_incomplete event (14299) was
        # coalesced through a chain that ended up "kept" by event 14111 — an
        # unrelated, much older event whose OWN age had not yet crossed the
        # ceiling at coalescing time, but did shortly after, expiring it
        # (`event_older_than_max_age`) with 14299 never delivered and never
        # eligible to try again.
        rows = conn.execute(
            "SELECT a.id, a.event_id, COALESCE(a.project_id,''), "
            "COALESCE(a.route_key,''), COALESCE(a.agent_id,''), a.decision FROM wake_audit a "
            "LEFT JOIN event e ON e.id = a.event_id "
            "WHERE (a.decision='wake' OR (a.decision='skip' AND a.reason='cooldown_active')) "
            "AND (e.ts_epoch IS NULL OR e.ts_epoch > ?) "
            "AND a.acknowledged=0 AND a.superseded_by IS NULL AND COALESCE(a.actionable,0)=0 "
            "AND NOT EXISTS (SELECT 1 FROM wake_audit w WHERE w.event_id=a.event_id "
            "                AND w.decision='wake' AND w.id<>a.id) "
            "AND NOT EXISTS (SELECT 1 FROM wake_submitted s WHERE s.event_id=a.event_id) "
            "ORDER BY a.id ASC", (now - MAX_WAKE_AGE_SECS,)).fetchall()
        groups: dict = {}
        for aid, eid, project, stored_route, agent_ref, decision in rows:
            # Group by the RESOLVED route, not the raw project key. Two sessions of
            # one project (payorch-live-buttons, payorch-monitor-clean) resolve to a
            # single chat, so grouping on the raw key would leave them unfolded and
            # deliver the same "go read Owner OS" instruction to that chat twice,
            # drained one per cooldown window - the exact backlog this function
            # exists to collapse. The stored route_key is preferred (it is what the
            # decision actually used); older rows predate the column and are
            # resolved fresh.
            key = (stored_route or "").strip()
            if not key:
                try:
                    key = wake_routes.resolve(project_id=project, agent_id=agent_ref,
                                              conn=conn).get(
                        "route_key") or wake_routes.route_key_for_event(project)
                except Exception:  # noqa: BLE001 - grouping must never break the drain
                    key = wake_routes.route_key_for_event(project)
            groups.setdefault(key, []).append((aid, eid, decision))
        superseded, kept = [], []
        reason = "coalesced_into_newest_generic_wake"
        for key, members in groups.items():
            if len(members) < 2:
                kept.append(int(members[0][1]))
                continue
            # A `wake`-decision member must never be superseded by a `skip` one, even
            # a newer one — that would demote an already-decided, claim-ready wake
            # back to pending, discarding its wake status and forcing it through the
            # WHOLE decision-gate cooldown again. Reproduced live 2026-08-30: a wake
            # row sat unclaimed only briefly before a fresher `skip` row (a routine
            # duplicate on the same busy route) coalesced OVER it, repeatedly, in a
            # cycle that could run indefinitely on a route with continuous traffic.
            # Restrict "kept" to the wake-decision members when any exist; only pick
            # from skip-decision members when the whole group is still undecided.
            wake_members = [m for m in members if m[2] == "wake"]
            pool = wake_members or members
            keep_audit_id, keep_event_id = int(pool[-1][0]), int(pool[-1][1])
            kept.append(keep_event_id)
            for aid, eid, _decision in members:
                if aid == keep_audit_id:
                    continue
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
    ts REAL, at TEXT, source TEXT, event_id INTEGER, allowed INTEGER, reason TEXT,
    route_key TEXT
)
"""


def _migrate_send(conn) -> None:
    """As with wake_audit: a live DB predates the column, so add it rather than assume it."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(wake_send)")}
    if "actionable" not in have:
        conn.execute("ALTER TABLE wake_send ADD COLUMN actionable INTEGER DEFAULT 0")
    if "route_key" not in have:
        conn.execute("ALTER TABLE wake_send ADD COLUMN route_key TEXT")
    # Both cooldown lookbacks are `... WHERE allowed=1 AND actionable=? AND <route>
    # ORDER BY id DESC LIMIT 1`. Unindexed they SCAN: cheap while a recent row
    # matches and the scan stops early, but a route with no prior send of that
    # class walks the whole table. Measured on the live db 2026-08-30 —
    # wake_audit 104k rows: 0.021ms when a recent row matched, 23ms when none did.
    # Append-only tables, so this only gets worse. Indexes are additive and
    # destroy nothing; retention is a separate, owner-gated question.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_wake_send_lookback "
                 "ON wake_send (allowed, actionable, route_key, id)")


def claim_send(source: str, event_id: Optional[int] = None, conn=None,
               actionable: bool = False, now: Optional[float] = None,
               route_key: str = "") -> dict:
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

    The cooldown is measured PER ROUTE, for the same reason the decision-layer floors
    are: a claim is a slot in ONE chat. Measuring it globally re-imposed at the choke
    point exactly the cross-chat suppression removed from the decision — a gaika-drop
    wake sat 867 seconds behind an owner-os send that was never going to its chat. The
    CLAIM itself stays global and every attempt is still recorded, so the out-of-band
    duplicate this function exists to catch is caught exactly as before.
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
                             f"COALESCE(actionable,0)=1 AND {_ROUTE_MATCH} "
                             "ORDER BY id DESC LIMIT 1",
                             _route_params(route_key)).fetchone()
            if r and (now - float(r[0] or 0)) < ACTIONABLE_COOLDOWN_SECS:
                res = (False, f"actionable_cooldown_active:"
                              f"{int(ACTIONABLE_COOLDOWN_SECS - (now - float(r[0])))}s")
            else:
                res = (True, "claimed_actionable")
        else:
            # Look back at NON-actionable sends only, mirroring the actionable
            # branch above. This was unscoped, so every actionable claim reset the
            # 900s window for non-actionable events. With actionable wakes arriving
            # every ~60-90s and COOLDOWN_SECS=900, a non-actionable event could
            # never be claimed at all — not delayed, starved.
            #
            # Observed live 2026-08-29/30: event 13383 (notifications_red,
            # severity=critical, owner_action_required=1) went undelivered for ~4h
            # across 115 attempts. Its countdown decayed 865->822->784->752->713->679
            # and jumped straight back to 862 the moment an unrelated actionable
            # wake was claimed.
            r = conn.execute(f"SELECT ts FROM wake_send WHERE allowed=1 AND "
                             f"COALESCE(actionable,0)=0 AND {_ROUTE_MATCH} "
                             "ORDER BY id DESC LIMIT 1",
                             _route_params(route_key)).fetchone()
            if r and (now - float(r[0] or 0)) < COOLDOWN_SECS:
                res = (False, f"global_cooldown_active:"
                              f"{int(COOLDOWN_SECS - (now - float(r[0])))}s")
            else:
                res = (True, "claimed")
        conn.execute("INSERT INTO wake_send (ts,at,source,event_id,allowed,reason,"
                     "actionable,route_key) VALUES (?,?,?,?,?,?,?,?)",
                     (now, now_iso(), source, int(event_id or 0), int(res[0]), res[1],
                      1 if actionable else 0, (route_key or "").strip()))
        conn.commit()
        return {"allowed": res[0], "reason": res[1], "source": source,
                "actionable": bool(actionable),
                "route_key": (route_key or "").strip()}
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
                    # ONE refusal is not death — a busy page can eat a send transiently,
                    # and a hasty dead-mark on the owner-os chat silenced the whole
                    # notifier for two hours. Two CONSECUTIVE fired-and-refused sends to
                    # the same conversation are the threshold.
                    last2 = conn.execute(
                        "SELECT delivered, reason FROM wake_delivery WHERE conversation=? "
                        "ORDER BY id DESC LIMIT 2", ((conversation or "").strip(),)
                    ).fetchall()
                    if (len(last2) == 2 and all(
                            not d and r == "composer_did_not_clear_after_send"
                            for d, r in last2)):
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
