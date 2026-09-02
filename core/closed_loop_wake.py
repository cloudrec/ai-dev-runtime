"""Closed-loop wake watchdog (task 211) — the two pieces the wake bridge and the
stall doctor cannot cover on their own:

  * SLO watchdog: a wake was delivered (a REAL ChatGPT user turn landed) and no
    progress followed. Re-wake once; if that also gets no progress, escalate as
    actionable so the owner is never left believing the loop is still running.
  * owner_intervention metric: the closed loop's whole point is that the OWNER never
    has to type into an agent pane. When a pane that was notified owner_prompt/blocker
    resumes to working WITHOUT any proof the companion ever delivered a wake for it,
    that is conservative, positive evidence a human typed there directly — the loop
    failed the owner once. This is a METRIC, not itself a wake trigger (waking the
    owner to tell them they already acted would be noise).

Both pieces are additive: nothing here changes when or whether the bridge/doctor/watch
modules fire; this module only observes their durable state (wake_delivery, wake_audit,
the event log) and adds its own small, restart-safe table.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

# How long a delivered wake gets before "no progress" becomes worth acting on.
# Env-tunable per the task-211 spec (`WAKE_LOOP_SLO_SECS`).
WAKE_LOOP_SLO_SECS = int(os.getenv("WAKE_LOOP_SLO_SECS", "900"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wake_loop_watch (
    event_id INTEGER PRIMARY KEY,
    target TEXT, project_id TEXT, delivered_ts REAL, delivered_at TEXT,
    rewoken INTEGER DEFAULT 0, rewoken_ts REAL, rewoken_event_id INTEGER,
    escalated INTEGER DEFAULT 0, escalated_ts REAL, escalated_event_id INTEGER
);
CREATE TABLE IF NOT EXISTS owner_intervention_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, at TEXT, target TEXT, project_id TEXT, notified_event_id INTEGER,
    prev_notified_cls TEXT, event_id INTEGER
)
"""

# Columns added after the table shipped — a live DB predates them, so migrate rather
# than assume. `resolved` is the SILENT deregistration path: the watch stops being
# considered by slo_scan without ever emitting anything (unlike `escalated`, which is
# a terminal state reached BY emitting).
_WATCH_COLUMNS = (
    ("resolved", "INTEGER DEFAULT 0"),
    ("resolved_reason", "TEXT"),
    ("resolved_ts", "REAL"),
    # Where the wake was DELIVERED, kept apart from where it came from. The companion
    # used to pass the route key as `project_id` because `pending_wake` never returned
    # the originating project, so every SLO alarm about a /opt/seo agent was filed under
    # `owner-os` — the chat it was delivered to (events 16068, 16102).
    ("route_key", "TEXT"),
)


def _migrate_watch(conn) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(wake_loop_watch)")}
    for name, decl in _WATCH_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE wake_loop_watch ADD COLUMN {name} {decl}")


def _conn(conn=None):
    conn, own = _c(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    _migrate_watch(conn)
    return conn, own


# A runtime job has no pane and therefore no `agent_watch_state` row, ever — under
# `_progress_since`'s definition (a newer event) a stuck job that keeps re-emitting
# ITS OWN "still waiting" chatter would never look like progress either, but the real
# bug this guards is different: a job that went TERMINAL needs no further wake at all,
# and `_progress_since` has no way to know that. Statuses mirror job_store.STATUSES
# minus the in-flight ones (draft/waiting_approval/queued/planning/.../deploying).
_RUNTIME_JOB_TERMINAL_STATUSES = frozenset({
    "completed", "failed", "cancelled", "rolled_back", "blocked", "fallback_plan_only",
})

# A job parked on an OWNER GATE is waiting, not stalled — the job-shaped twin of the
# `intentional_external_wait` rule below. `runtime_watchdog` already states it outright:
# "`waiting_approval` is NEVER a stall: it is a true owner decision, announced once by
# the lifecycle bridge (runtime_events), not re-announced here." This module never
# learned it. So `runtime_events` announced the decision properly as
# `owner_decision_required` (high), the job then sat in `waiting_approval` exactly as it
# must until a human acts, `_progress_since` saw nothing move, and the watchdog
# escalated `wake_loop_stalled` — CRITICAL — telling the owner a second and louder time
# about a decision already sitting in their queue. Re-waking cannot help: the only thing
# that ends this state is the owner.
#
# Deliberately NOT folded into the terminal set. The job is not finished, and this
# resolution is self-limiting in the same way `agent_parked_completed` is: when the
# owner approves, the status leaves this set and the next event for the job opens its
# own fresh watch.
# Exactly the status that wakes AND parks: `runtime_events.EVENT_FOR_STATUS` maps
# `waiting_approval` to `owner_decision_required`, and nothing else in that table both
# raises a wake and then waits on a human. `draft`/`superseded` never wake at all, so
# they can never open a watch and are deliberately not listed on speculation.
_RUNTIME_JOB_OWNER_GATE_STATUSES = frozenset({"waiting_approval"})
_RUNTIME_JOB_TARGET_PREFIX = "runtimejob:"

# Hook-sourced wakes are addressed `session:<conversation id>`, a namespace that never
# appears in `agent_watch_state` (which is keyed by tmux target). So NONE of the
# pane-based resolutions below can ever fire for one, and if the session behind it is gone
# the watch chases progress that can never arrive — exactly the runtimejob argument, in a
# second namespace. Event 15923: cp-canary was killed at 20:20:35Z, its wake was delivered
# at 20:47:20Z to an agent that no longer existed, re-woken at 21:03 and escalated
# critical at 21:18:49Z, with no possible end.
_SESSION_TARGET_PREFIX = "session:"

# Wakes whose whole premise is "this agent is asking the owner something RIGHT NOW".
# agent_watch mints these from its `owner_prompt` and `blocker` classes.
_PROMPT_EVENT_TYPES = frozenset({
    "agent_prompt_needs_response", "agent_waiting_input", "agent_needs_response",
    "agent_prompt_needs_response",
})
# agent_watch classes that positively contradict "a prompt is on screen". `owner_prompt`
# and `blocker` are the prompting classes; `crashed` is a failure and must keep waking.
_NOT_PROMPTING_CLASSES = frozenset({"idle"})


def _session_target_gone(conn, event_id: int, target: str, agents=None) -> bool:
    """Is the session behind this watch provably gone?

    Fail-closed in the direction that keeps waking: unknown cwd, unreadable inventory or
    a still-present pane all return False, so a live agent's genuine stall escalates
    exactly as before. Resolution needs BOTH no live agent in the session's own working
    directory AND a terminal event already recorded for it, so the owner has still been
    told once — by `agent_dead` / `agent_process_failed` — before this goes quiet.
    """
    try:
        row = conn.execute("SELECT payload FROM event WHERE id=?", (event_id,)).fetchone()
        cwd = (json.loads(row[0]) or {}).get("cwd", "") if row and row[0] else ""
    except Exception:  # noqa: BLE001
        return False
    if not cwd:
        return False
    if agents is None:
        try:
            from core import agent_control as ac
            agents = ac.agent_list().get("agents", [])
        except Exception:  # noqa: BLE001 — no inventory, no claim
            return False
    live = [a for a in (agents or [])
            if (a.get("claude_cwd") or a.get("cwd") or "").rstrip("/") == cwd.rstrip("/")
            and a.get("alive")]
    if live:
        return False
    # The terminal event is recorded in the OTHER namespace — `agent_id='cp-canary:0.0'`,
    # `project_id='cp-canary-v2'` — which is the whole point of this function, so matching
    # it on the session-form target would never hit. The cwd is what the two namespaces
    # share. Bounded to a terminal event no older than the watched wake, so a crash from
    # last week cannot retire a watch opened today.
    project = cwd.rstrip("/").rsplit("/", 1)[-1]
    try:
        term = conn.execute(
            "SELECT 1 FROM event WHERE type IN ('agent_dead','agent_process_failed') "
            "AND (project_id=? OR agent_id LIKE ? OR agent_id=?) "
            "AND ts_epoch >= (SELECT ts_epoch FROM event WHERE id=?) LIMIT 1",
            (project, f"{project}%", target, event_id)).fetchone()
    except Exception:  # noqa: BLE001
        return False
    return bool(term)


def _runtime_job_terminal(target: str) -> bool:
    """Is the job behind this `runtimejob:<8-hex-prefix>` target ALREADY terminal?

    `agent_id`/target values for runtime jobs carry only the first 8 hex chars of the
    job id (see runtime_watchdog.py / runtime_events.py / runtime_supervisor.py — all
    three truncate the same way), never the full uuid, so this is a PREFIX match
    against the jobs store, not `job_store.get_job` (exact id). Read-only, against
    job_store's own configured DB path (so it follows RUNTIME_DB in tests exactly the
    way job_store itself does); any failure to read reads as "not confirmed terminal"
    — a job store this module cannot see must never be treated as resolved."""
    return _runtime_job_status(target) in _RUNTIME_JOB_TERMINAL_STATUSES


def _runtime_job_status(target: str) -> str:
    """This job's current status, or "" when it cannot be read.

    An unreadable job store yields "", which matches no status set, so every caller
    falls through to "not resolved" — a store this module cannot see must never be
    treated as resolved.
    """
    prefix = target[len(_RUNTIME_JOB_TARGET_PREFIX):]
    if not prefix:
        return ""
    try:
        import sqlite3
        from core import job_store
        jconn = sqlite3.connect(job_store._DB, timeout=5)
        try:
            row = jconn.execute(
                "SELECT status FROM jobs WHERE id LIKE ? LIMIT 1",
                (prefix + "%",)).fetchone()
        finally:
            jconn.close()
        return (row[0] if row else "") or ""
    except Exception:  # noqa: BLE001 — never let a job-store read block resolution
        return ""


# An INTENTIONAL external wait, proven structurally rather than read out of prose.
#
# 2026-08-30, diamond-auction:0.0: the agent finished its stage, armed a read-only monitor
# for a natural auction close, and said so — "if a natural auction close occurs, it
# auto-anchors and I'll be notified... Idle on the watch." Nothing was stuck. But
# `_progress_since` counts NEW EVENTS, and an agent waiting correctly emits none, so the
# watchdog re-woke it and then escalated `wake_loop_no_progress` (15519) at the owner.
#
# The distinction cannot be read from the sentence — that is the trap this session already
# proved twice (a stop whose wording matched no detector, and report prose classified as a
# live prompt). Claude Code states it structurally instead: a session that stopped with
# `background_tasks` still running or `session_crons` armed is waiting BY DESIGN, and the
# lifecycle hook records exactly those fields. An armed monitor is the difference between
# "waiting" and "stuck", and it is a fact, not an interpretation.
_INTENTIONAL_WAIT_LOOKBACK_SECS = int(
    os.getenv("WAKE_INTENTIONAL_WAIT_LOOKBACK_SECS", "7200"))


def _armed_external_wait(conn, target: str, now: float) -> bool:
    """Did this agent last stop with a monitor still armed?

    Reads the durable `agent_turn_stopped` record the native Stop hook writes. Absent that
    record — an older Claude, hooks disabled, a session started before install — this
    returns False and the watchdog behaves exactly as it did before, which is the required
    fallback: unproven means NOT resolved, never the reverse.
    """
    try:
        row = conn.execute(
            "SELECT payload FROM event WHERE agent_id=? AND type='agent_turn_stopped' "
            "AND ts_epoch > ? ORDER BY id DESC LIMIT 1",
            (target, now - _INTENTIONAL_WAIT_LOOKBACK_SECS)).fetchone()
    except Exception:  # noqa: BLE001 — a missing column or table never resolves a watch
        return False
    if not row or not row[0]:
        # No native record yet — an agent that was already parked when hooks were
        # installed emits nothing until it next moves. A DECLARED wait covers exactly that
        # gap, and is bounded and audited so it cannot silence anything indefinitely.
        try:
            from core import native_supervisor as _ns
            return _ns.in_external_wait(target, conn=conn, now=now)
        except Exception:  # noqa: BLE001 — unknown never means "resolved"
            return False
    try:
        p = json.loads(row[0])
    except Exception:  # noqa: BLE001
        return False
    return bool(p.get("background_tasks")) or bool(p.get("session_crons"))


def _identities(conn, target: str) -> tuple:
    """Every agent_id the SAME agent's events are recorded under.

    One agent speaks with two names here. `agent_watch` writes events under the tmux
    target (`gaika-opus:0.0`); the native hooks write theirs under
    `session:<conversation[:12]>`, because a hook knows its session and not the tmux
    world. Both are that agent, and a watch registered under one name was blind to
    everything it did under the other — including `agent_turn_stopped`, the most
    abundant proof of life in the system at 835 events a day.

    This is the same defect this module already names for `runtimejob:` targets: an
    identity whose activity `_progress_since` structurally cannot see is a guaranteed
    future false positive. It was simply not noticed that a plain agent has the problem
    too, in both directions — a session-form target additionally has no
    `agent_watch_state` row at all, so `pane_alive_and_working` could never resolve it.

    Returns the target plus any alias, target first. Best-effort: on any failure the
    caller gets exactly today's behaviour, never fewer identities than it had.
    """
    out = [target]
    try:
        if target.startswith(_SESSION_TARGET_PREFIX):
            prefix = target[len(_SESSION_TARGET_PREFIX):]
            if prefix:
                for (t,) in conn.execute(
                        "SELECT target FROM agent WHERE substr(conversation_id,1,?)=? "
                        "AND target NOT LIKE 'session:%' ORDER BY updated_at DESC LIMIT 2",
                        (len(prefix), prefix)).fetchall():
                    if t and t not in out:
                        out.append(t)
        elif not target.startswith(_RUNTIME_JOB_TARGET_PREFIX):
            row = conn.execute("SELECT conversation_id FROM agent WHERE target=?",
                               (target,)).fetchone()
            conv = (row[0] if row else "") or ""
            if conv:
                alias = _SESSION_TARGET_PREFIX + conv[:12]
                if alias not in out:
                    out.append(alias)
    except Exception:  # noqa: BLE001 — an unknown alias never means "resolved"
        pass
    return tuple(out)


def _natively_working(conn, target: str) -> bool:
    """Is the runtime itself reporting this agent as working right now?

    POSITIVE evidence, which is the thing `_progress_since` never had. It counts
    events, and an agent in the middle of a long turn emits none while it works —
    indistinguishable from a pane that died. Both `8aba07f` and `3d8d4bf` subtract
    exceptions from that proxy; this replaces it for the one case it was worst at.

    `claude agents --json` reports `busy` per session. The target may be either of
    the two names an agent goes by, so both are tried: a `session:<id>` target asks
    by (truncated) session id, a tmux target asks by the pid the registry holds.

    Fail-open in the only direction that matters: absence of an answer is NOT
    evidence of death, so this can only ever RESOLVE a watch, never escalate one.
    """
    try:
        from core import native_sessions
        if target.startswith(_SESSION_TARGET_PREFIX):
            sid = target[len(_SESSION_TARGET_PREFIX):]
            if sid and native_sessions.is_working(sid):
                return True
        for ident in _identities(conn, target):
            if ident.startswith(_SESSION_TARGET_PREFIX):
                sid = ident[len(_SESSION_TARGET_PREFIX):]
                if sid and native_sessions.is_working(sid):
                    return True
                continue
            row = conn.execute("SELECT pid FROM agent WHERE target=?",
                               (ident,)).fetchone()
            pid = row[0] if row else None
            if pid and native_sessions.is_working(pid=pid):
                return True
    except Exception:  # noqa: BLE001 — unknown never means "resolved", and never raises
        return False
    return False


def _watch_state_cls(conn, target: str) -> str:
    """agent_watch's current class for this agent, under whichever name it is filed."""
    for ident in _identities(conn, target):
        try:
            row = conn.execute("SELECT cls FROM agent_watch_state WHERE target=?",
                               (ident,)).fetchone()
        except Exception:  # noqa: BLE001
            return ""
        if row and row[0]:
            return row[0]
    return ""


def _resolution_reason(conn, *, event_id: int, target: str,
                       agents=None, delivered_ts: Optional[float] = None) -> Optional[str]:
    """Has the condition THIS watch exists for already resolved? Three independent
    signals, any one of which is sufficient:

      * the original event carries agent_watch's audited invalid-alert overlay — a
        recovered crash, a resolved stall episode, or an owner/coordinator manually
        retiring a known-bad row (2026-08-15: the 5576->5597 incident — a runtime job
        that went terminal, and whose original wake was already retired, still got
        re-woken because nothing ever checked);
      * the target is a runtime job (`runtimejob:<id>`) that has since reached a
        terminal status — a job has no pane, so `_progress_since` can NEVER see
        progress for one; every runtimejob watch is a guaranteed future false positive
        unless resolution is checked directly against the jobs store;
      * the target is a live tmux pane currently observed WORKING by agent_watch — the
        agent already moved on by itself.

    Returns the reason string when resolved, else None. Checked BEFORE any
    progress/SLO logic, on every scan, for every open watch — proactive, not just a
    gate in front of the rewake/escalate action.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM agent_alert_invalid WHERE event_id=?", (event_id,)).fetchone()
        if row:
            return "event_marked_invalid"
    except Exception:  # noqa: BLE001 — table may not exist yet in a fresh DB
        pass
    if not target:
        # Written before `register_delivery` refused to create these. It can never
        # resolve on its own terms and `slo_scan` skips it by name, so it would sit
        # open forever. Retire it rather than leave a permanent row that any future
        # backfill of `target` would silently re-animate into a weeks-late wake.
        return "watch_has_no_target"
    if target.startswith(_RUNTIME_JOB_TARGET_PREFIX):
        status = _runtime_job_status(target)
        if status in _RUNTIME_JOB_TERMINAL_STATUSES:
            return "runtime_job_terminal"
        if status in _RUNTIME_JOB_OWNER_GATE_STATUSES:
            return "runtime_job_awaiting_owner"
        return None
    if target.startswith(_SESSION_TARGET_PREFIX) and _session_target_gone(
            conn, event_id, target, agents=agents):
        return "target_session_no_longer_present"
    # A pane that parked with its own monitor armed is waiting, not stalled. Checked with
    # the other silent-resolution signals, so it deregisters the watch without emitting —
    # the owner is not told a second time about a state they already know is intentional.
    if _armed_external_wait(conn, target, now_ts()):
        return "intentional_external_wait"
    # Asked BEFORE the scraped class, because it is the same question answered by
    # the runtime instead of by inference. A pane mid-turn reports `busy` here while
    # emitting no events at all, which is exactly the state that produced the
    # escalations Part 53 could not account for.
    if _natively_working(conn, target):
        return "runtime_reports_agent_working"
    try:
        cls = _watch_state_cls(conn, target)
        if cls == "working":
            return "pane_alive_and_working"
        # task 221 (events 10268/10284, mess/chemmy-fast): an agent that finished its
        # authorized scope and is explicitly at rest (agent_watch's "completed" class —
        # `stated_finish_at_rest`) is not stuck; it is DONE. Without this, a wake
        # delivered right before the agent finished kept re-firing
        # wake_loop_no_progress/wake_loop_stalled for a state that never changes again,
        # because `_progress_since` correctly sees no further activity from a parked
        # agent. Deliberately NOT extended to "idle" (`no_signal` — no positive
        # completion evidence, genuinely ambiguous) or "crashed" (a real failure): both
        # must keep waking exactly as before. `owner_prompt`/`blocker` are untouched,
        # so a real waiting-owner state still wakes. A later state change moves `cls`
        # away from "completed", so this check stops applying on its own — no separate
        # reset logic is needed, and a NEW event for the same target starts its own
        # fresh watch regardless.
        if cls == "completed":
            return "agent_parked_completed"
        # The premise of a prompt wake is that a question is on screen. When agent_watch
        # has since reclassified the pane away from `owner_prompt`/`blocker`, that premise
        # is GONE, and re-waking and escalating to critical chases a question nobody is
        # asking. Event 16042→16068→16102: `agent_prompt_needs_response` for a pane that
        # was idle with no pending input and no assigned task, escalated to critical.
        #
        # This is deliberately NOT the general "idle means done" claim the comment above
        # refuses to make. It resolves only watches whose ORIGINAL event asserted a live
        # prompt, and only on the narrower fact that the asserted prompt is absent. A
        # genuinely waiting agent still classifies `owner_prompt`/`blocker` and keeps
        # escalating; crash, failure and stop watches are untouched; and with no
        # agent_watch row at all nothing is claimed.
        if cls and cls in _NOT_PROMPTING_CLASSES:
            try:
                ev = conn.execute("SELECT type FROM event WHERE id=?",
                                  (event_id,)).fetchone()
            except Exception:  # noqa: BLE001
                ev = None
            if ev and ev[0] in _PROMPT_EVENT_TYPES:
                return "prompt_no_longer_present"
    except Exception:  # noqa: BLE001 — table may not exist yet in a fresh DB
        pass
    # The watch's own question, answered: did this delivered wake produce movement?
    # `slo_scan` treated progress as a reason to SKIP the row for one pass and never
    # as the state it reaches by succeeding, so a watch whose wake plainly worked
    # stayed open forever — re-evaluated on every scan, for as long as the row lived.
    # Observed: 26 open watches for one session, every one of them with progress
    # recorded, the oldest 107 hours old. They can never fire (progress measured from
    # delivery only ever accumulates) and never close: inert, but immortal.
    #
    # Checked LAST, because it is the weakest claim — it says only that something
    # happened afterwards — so any structural reason above still wins the audit trail.
    # It can never suppress a due escalation: escalation requires the absence of
    # exactly what this asserts.
    if delivered_ts is not None and target and _progress_since(conn, target, delivered_ts):
        return "progress_observed"
    return None


def deregister_resolved(*, conn=None, now: Optional[float] = None, agents=None) -> list:
    """Proactively retire every OPEN watch whose underlying condition already
    resolved — SILENTLY: `resolved=1` is set, nothing is emitted. Distinct from
    `escalated`, which is a terminal state reached BY emitting; this is the state
    reached by NOT needing to. Called at the top of every `slo_scan`, and callable on
    its own for a one-time cleanup of rows a prior scan already got wrong."""
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        rows = conn.execute(
            "SELECT event_id, target, delivered_ts FROM wake_loop_watch "
            "WHERE escalated=0 AND COALESCE(resolved,0)=0").fetchall()
        deregistered = []
        for event_id, target, delivered_ts in rows:
            reason = _resolution_reason(conn, event_id=event_id, target=target or "",
                                        agents=agents, delivered_ts=delivered_ts)
            if not reason:
                continue
            conn.execute(
                "UPDATE wake_loop_watch SET resolved=1, resolved_reason=?, "
                "resolved_ts=? WHERE event_id=?", (reason, now, event_id))
            deregistered.append({"event_id": int(event_id), "target": target or "",
                                 "reason": reason})
        if deregistered:
            conn.commit()
        return deregistered
    finally:
        if own:
            conn.close()


# ── SLO watchdog ─────────────────────────────────────────────────────────────
def _target_from_event(conn, event_id: int) -> str:
    """The agent this event is about, straight off the event row.

    `agent_id` is the same string the watch calls `target` — the pane address, the
    `session:<id>` alias or the `runtimejob:<prefix>` handle, whichever the emitter
    used. Reading it here means a caller that forgets to pass the target no longer
    silently creates an untrackable watch.
    """
    try:
        row = conn.execute("SELECT agent_id FROM event WHERE id=?",
                           (int(event_id),)).fetchone()
    except Exception:  # noqa: BLE001 — a missing event is simply no target
        return ""
    return ((row[0] if row else "") or "").strip()


def register_delivery(*, event_id: int, target: str = "", project_id: str = "",
                      event_type: str = "", route_key: str = "", conn=None,
                      now: Optional[float] = None) -> None:
    """Start SLO tracking for a wake that a companion delivery just confirmed landed
    (a real ChatGPT user turn). Idempotent — a re-delivery of the same event id (which
    should never happen past the submission latch, but this must never crash if it
    does) leaves the original tracking row alone.

    NEVER registers a `loop_watchdog`-class delivery (`wake_loop_no_progress` /
    `wake_loop_stalled`) — those are the watchdog's OWN re-wake/escalation events.
    Before this check existed, every re-wake spawned a fresh watch that could itself
    re-wake, an unbounded self-feeding chain rate-limited only by the SLO window
    (2026-08-15: 5548 -> rewake 5563 -> rewake 5595 -> ...). `event_type` is the one
    piece of context that answers "is this delivery ITSELF a watchdog artifact" —
    trigger class is a closed lookup, so this can never be tricked by pane content.
    """
    from core import wake_bridge as wb
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        if wb.trigger_class_for(event_type) == wb.TRIGGER_CLASS_LOOP_WATCHDOG:
            return
        # A watch is ABOUT an agent. Without one it can never resolve, never
        # progress and never escalate — `slo_scan` skips it by name — so it is a row
        # that lives forever and means nothing. Fourteen such rows exist here, and
        # every one of them had the answer on its own event all along: `agent_id` IS
        # the target (`runtimejob:bea93aec`, `capacity-blockchain:0.0`, ...). The
        # caller simply did not pass it, and nothing looked.
        target = (target or "").strip()
        if not target:
            target = _target_from_event(conn, event_id)
        if not target:
            # Still nothing to watch. Refusing is the honest outcome: a tracking row
            # for an unnameable subject is not tracking.
            return
        conn.execute(
            "INSERT OR IGNORE INTO wake_loop_watch (event_id,target,project_id,route_key,"
            "delivered_ts,delivered_at) VALUES (?,?,?,?,?,?)",
            (int(event_id), target, project_id or "", route_key or "",
             now, now_iso()))
        conn.commit()
    finally:
        if own:
            conn.close()


def _transcript_advanced(conn, target: str, since_ts: float) -> bool:
    """Has this agent's session transcript been written since `since_ts`?

    The second progress oracle, and the one that does not depend on the agent being
    instrumented. Events are emitted by a hook; a transcript is written by the runtime
    itself as the agent works, so it keeps telling the truth when the hook goes quiet.

    Event 21139 -> 21178 -> 21272, 2026-09-02: this session emitted 136
    `agent_turn_stopped` events and then stopped at 09:52:57. The wake was delivered at
    09:59:19. Over the next 33 minutes the agent pushed commits, ran two regression
    suites and rebound two routes, and produced ZERO events under either of its
    identities — so `_progress_since` correctly reported what it could see, re-woke a
    working agent, and escalated it to a critical `wake_loop_stalled`. Its transcript
    mtime advanced throughout.

    Fails CLOSED: any doubt returns False and the caller keeps the behaviour it had
    before this function existed. A missing transcript, an unreadable directory, or a
    truncated session id matching more than one file are all "cannot tell", and cannot
    tell must never be the thing that silences a real stall.
    """
    import glob
    root = os.environ.get("OWNEROS_CLAUDE_PROJECTS")
    if root == "":            # explicitly emptied: the oracle is off, see tests/conftest
        return False
    root = root or os.path.expanduser(os.path.join("~", ".claude", "projects"))
    for ident in _identities(conn, target):
        if not ident.startswith(_SESSION_TARGET_PREFIX):
            continue
        sid = ident[len(_SESSION_TARGET_PREFIX):].strip()
        if len(sid) < 8:  # too short to identify a session; refuse to guess
            continue
        try:
            hits = glob.glob(os.path.join(root, "*", sid + "*.jsonl"))
            if len(hits) != 1:  # zero: unknown. more than one: ambiguous. both refuse.
                continue
            if os.stat(hits[0]).st_mtime > since_ts:
                return True
        except Exception:  # noqa: BLE001 — see "fails CLOSED" above
            continue
    return False


def _progress_since(conn, target: str, since_ts: float) -> bool:
    """Has ANYTHING happened for this agent since the wake was delivered? Any newer CTO
    event correlated to this target — a fresh agent_watch class, a stall_doctor action,
    a second wake decision — counts as progress, and so does a written transcript.
    Conservative on purpose: this only suppresses a re-wake/escalation, never suppresses
    one that is actually due.

    The watchdog's OWN bookkeeping events (source='closed_loop_wake' — the re-wake and
    the escalation it emits) are excluded: otherwise the re-wake's own event row would
    read as "progress" on the very next scan and the episode could never escalate.

    Events alone were not enough. They arrive through a hook, and an agent whose hook
    stops emitting looks identical to an agent that has stopped working — see
    `_transcript_advanced`, which reads what the runtime writes rather than what the
    instrumentation reports."""
    idents = _identities(conn, target)
    row = conn.execute(
        "SELECT COUNT(*) FROM event WHERE agent_id IN (%s) AND ts_epoch > ? "
        "AND source != 'closed_loop_wake'" % ",".join("?" * len(idents)),
        (*idents, since_ts)).fetchone()
    if row and row[0]:
        return True
    return _transcript_advanced(conn, target, since_ts)


def slo_scan(*, conn=None, now: Optional[float] = None,
            emit_fn: Optional[Callable] = None) -> dict:
    """One pass: retire every already-resolved watch silently first, then for every
    REMAINING tracked delivery with no progress past the SLO, re-wake once; for one
    still stalled a further SLO window after the re-wake, escalate."""
    now = now if now is not None else now_ts()
    if emit_fn is None:
        from core.control_plane.cto import emit as emit_fn  # noqa: F811
    conn, own = _conn(conn)
    try:
        deregistered = deregister_resolved(conn=conn, now=now)
        rewoken, escalated = [], []
        rows = conn.execute(
            "SELECT event_id, target, project_id, delivered_ts, rewoken, rewoken_ts, "
            "escalated, COALESCE(route_key,'') FROM wake_loop_watch WHERE escalated=0 "
            "AND COALESCE(resolved,0)=0").fetchall()
        for (eid, target, project_id, delivered_ts, is_rewoken, rewoken_ts, is_escalated,
             route_key) in rows:
            if not target:
                continue
            if _progress_since(conn, target, delivered_ts):
                continue
            if not is_rewoken:
                if now - delivered_ts < WAKE_LOOP_SLO_SECS:
                    continue
                ev = emit_fn(
                    "closed_loop_wake", "wake_loop_no_progress", project_id=project_id,
                    agent_id=target, severity="high", owner_action_required=True,
                    payload={"target": target, "original_event_id": eid,
                             "slo_secs": WAKE_LOOP_SLO_SECS,
                             "route_key": route_key},
                    action_taken=(f"{target}: wake {eid} delivered but no progress in "
                                  f"{WAKE_LOOP_SLO_SECS}s — re-waking once"),
                    correlation_id=f"agentwatch:{target}",
                    dedup_key=f"wakeloop:{eid}:no_progress",
                    dedup_window_secs=86400, conn=conn)
                new_eid = (ev or {}).get("event_id")
                conn.execute(
                    "UPDATE wake_loop_watch SET rewoken=1, rewoken_ts=?, "
                    "rewoken_event_id=? WHERE event_id=?", (now, new_eid, eid))
                conn.commit()
                rewoken.append({"event_id": eid, "rewoken_event_id": new_eid,
                                "target": target})
                continue
            if now - (rewoken_ts or now) < WAKE_LOOP_SLO_SECS:
                continue
            ev = emit_fn(
                "closed_loop_wake", "wake_loop_stalled", project_id=project_id,
                agent_id=target, severity="critical", owner_action_required=True,
                payload={"target": target, "original_event_id": eid,
                         "slo_secs": WAKE_LOOP_SLO_SECS, "route_key": route_key},
                action_taken=(f"{target}: wake {eid} re-woken with still no progress — "
                              "escalating"),
                correlation_id=f"agentwatch:{target}",
                dedup_key=f"wakeloop:{eid}:stalled",
                dedup_window_secs=86400, conn=conn)
            new_eid = (ev or {}).get("event_id")
            conn.execute(
                "UPDATE wake_loop_watch SET escalated=1, escalated_ts=?, "
                "escalated_event_id=? WHERE event_id=?", (now, new_eid, eid))
            conn.commit()
            escalated.append({"event_id": eid, "escalated_event_id": new_eid,
                              "target": target})
        return {"rewoken": rewoken, "escalated": escalated, "deregistered": deregistered}
    finally:
        if own:
            conn.close()


# ── owner_intervention metric ───────────────────────────────────────────────
def detect_owner_intervention(*, target: str, prev_notified_cls: str, project_id: str = "",
                              conn=None, now: Optional[float] = None,
                              emit_fn: Optional[Callable] = None) -> Optional[int]:
    """Conservative: fires ONLY when the pane was notified owner_prompt/blocker AND
    there is positive proof (a row in `event`) that a wake for it was NEVER delivered
    by the companion. A wake that DID deliver, or no notification at all, is never
    misclassified as an intervention — this must never count a companion-submitted
    turn as the owner's."""
    if prev_notified_cls not in ("owner_prompt", "blocker"):
        return None
    now = now if now is not None else now_ts()
    if emit_fn is None:
        from core.control_plane.cto import emit as emit_fn  # noqa: F811
    from core import wake_bridge as wb
    conn, own = _conn(conn)
    try:
        # wake_delivery is created lazily by wake_bridge; ensure it exists before this
        # module — which may run first in a fresh DB — queries it.
        conn.execute(wb._DELIVERY_SCHEMA)
        wb._migrate_delivery(conn)
        corr = f"agentwatch:{target}"
        row = conn.execute(
            "SELECT id FROM event WHERE correlation_id=? ORDER BY id DESC LIMIT 1",
            (corr,)).fetchone()
        if not row:
            return None
        notified_event_id = int(row[0])
        delivered = conn.execute(
            "SELECT 1 FROM wake_delivery WHERE event_id=? AND delivered=1",
            (notified_event_id,)).fetchone()
        if delivered:
            return None  # the companion handled it — not an intervention
        already = conn.execute(
            "SELECT 1 FROM owner_intervention_log WHERE notified_event_id=?",
            (notified_event_id,)).fetchone()
        if already:
            return None
        ev = emit_fn(
            "closed_loop_wake", "owner_intervention", project_id=project_id,
            agent_id=target, severity="info", owner_action_required=False, push=False,
            payload={"target": target, "prev_notified_cls": prev_notified_cls,
                     "notified_event_id": notified_event_id},
            action_taken=(f"{target}: resumed without a delivered wake for event "
                          f"{notified_event_id} — the owner acted directly"),
            correlation_id=corr,
            dedup_key=f"ownerintervention:{notified_event_id}",
            dedup_window_secs=86400, conn=conn)
        eid = (ev or {}).get("event_id")
        conn.execute(
            "INSERT INTO owner_intervention_log (ts,at,target,project_id,"
            "notified_event_id,prev_notified_cls,event_id) VALUES (?,?,?,?,?,?,?)",
            (now, now_iso(), target, project_id or "", notified_event_id,
             prev_notified_cls, eid))
        conn.commit()
        return eid
    finally:
        if own:
            conn.close()


# ── observability counters (additive, for diagnostics.observability_summary) ─
def counters(*, conn=None, now: Optional[float] = None) -> dict:
    """Small additive read: wakes delivered by trigger class, owner_intervention
    count, and loop-SLO breach counts (re-woken / escalated)."""
    from core import wake_bridge as wb
    conn, own = _conn(conn)
    try:
        conn.execute(wb._DELIVERY_SCHEMA)
        wb._migrate_delivery(conn)
        rows = conn.execute(
            "SELECT COALESCE(a.event_type,''), COUNT(*) FROM wake_delivery d "
            "JOIN wake_audit a ON a.event_id = d.event_id "
            "WHERE d.delivered=1 GROUP BY 1").fetchall()
        by_trigger: dict = {}
        for etype, n in rows:
            tc = wb.trigger_class_for(etype)
            by_trigger[tc] = by_trigger.get(tc, 0) + int(n)
        owner_intervention_count = conn.execute(
            "SELECT COUNT(*) FROM owner_intervention_log").fetchone()[0]
        rewoken = conn.execute(
            "SELECT COUNT(*) FROM wake_loop_watch WHERE rewoken=1").fetchone()[0]
        escalated = conn.execute(
            "SELECT COUNT(*) FROM wake_loop_watch WHERE escalated=1").fetchone()[0]
        resolved = conn.execute(
            "SELECT COUNT(*) FROM wake_loop_watch WHERE COALESCE(resolved,0)=1"
        ).fetchone()[0]
        return {
            "wakes_delivered_by_trigger_class": by_trigger,
            "wakes_delivered_total": sum(by_trigger.values()),
            "owner_intervention_count": int(owner_intervention_count),
            "loop_slo_rewoken": int(rewoken),
            "loop_slo_escalated": int(escalated),
            "loop_slo_resolved": int(resolved),
            "loop_slo_secs": WAKE_LOOP_SLO_SECS,
        }
    finally:
        if own:
            conn.close()
