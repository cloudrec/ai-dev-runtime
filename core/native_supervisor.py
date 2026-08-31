"""Continue managed agents from NATIVE Claude Code lifecycle signals, without ChatGPT.

The wake loop's normal path was: scrape a tmux pane, classify the prose, wake ChatGPT,
and let ChatGPT decide to continue the agent. That is three inferences and a browser in
the way of "the agent finished a turn and could keep going".

Claude Code states the fact itself. `hooks/owneros_hook.py` turns it into a durable
event; this module reads those events and continues the SAME agent in-process. ChatGPT is
then reserved for what it is actually needed for: genuine owner gates and irreversible
decisions.

WHAT THIS MAY DO, AND NOTHING ELSE:

  * continue an agent that stopped with nothing pending and no monitor armed, using the
    EXISTING fail-closed safe-step allowlist (`is_safe_continuation`) — the same text the
    continuation watchdog has always sent. No new content is invented, ever.
  * refuse, loudly and durably, in every other case.

It never creates an agent (`agent_send` cannot), never answers a question, never crosses
an owner gate, and never guesses a route. A target it cannot resolve to exactly one live
pane is refused rather than assumed — the duplicate-agent incident earlier today came
from exactly that kind of assumption.

The tmux/quiescence watchdog is untouched and stays the fallback for crashes, silent
failures, older Claude builds, and any case where a hook never fires.
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

ENABLED = os.getenv("NATIVE_SUPERVISOR_ENABLED", "1") not in ("0", "false", "no")
# ROLL-OUT GATE. Empty means canary only. `*` means every managed target. Deliberately an
# ALLOWLIST: a new agent is supervised when it is added here or by Owner OS registration,
# never merely because it appeared.
_TARGETS_RAW = os.getenv("NATIVE_SUPERVISOR_TARGETS", "cp-canary:0.0")
# How far back a lifecycle event may be and still be worth acting on. An agent that
# stopped an hour ago has been handled by something else or is genuinely parked.
MAX_EVENT_AGE_SECS = int(os.getenv("NATIVE_SUPERVISOR_MAX_AGE_SECS", "900"))
# Per-target floor, so a chatty agent cannot be driven in a tight loop.
MIN_INTERVAL_SECS = int(os.getenv("NATIVE_SUPERVISOR_MIN_INTERVAL_SECS", "120"))
MAX_CONSECUTIVE = int(os.getenv("NATIVE_SUPERVISOR_MAX_CONSECUTIVE", "6"))

# ── transient vs terminal skips ──────────────────────────────────────────────────────
# A skip recorded in `native_supervision` CONSUMES its event: the candidate query joins on
# event_id, so the event is never looked at again. That is right for a terminal reason —
# the project is not in the rollout, the wait is deliberate, the event was not a turn
# boundary — and WRONG for a reason that describes a passing moment.
#
# The dead-end it caused, observed live on 2026-08-30: an agent still mid-turn when the
# scan ran was skipped `agent_already_working_again` and its event consumed. The agent then
# finished and went idle — but the turn boundary it would have reported was the very event
# just consumed, so no new one ever arrived. `/opt/mess` and `/opt/seo` both sat idle,
# supervised, ungated and untouched, because the loop was purely reactive over unconsumed
# events and there were none left.
#
# Transient skips are therefore NOT recorded, so the next tick re-evaluates them. This is
# self-limiting rather than a retry storm: MAX_EVENT_AGE_SECS already bounds how long an
# event stays a candidate, no send happens while skipping, and the send itself is still
# governed by MIN_INTERVAL_SECS, MAX_CONSECUTIVE and the terminal gate.
TRANSIENT_SKIP_REASONS = frozenset({
    "agent_already_working_again",
    "min_interval_not_elapsed",
    "pane_has_pending_input",
})

# ── quiescence sweep: the EMERGENCY fallback, never the primary path ──────────────────
# Events remain the first-class signal. But a purely reactive loop cannot rescue an agent
# whose last turn boundary was already consumed — the state /opt/mess and /opt/seo were
# found in. The sweep looks at supervised agents that are simply AT REST with nothing left
# to react to, and only after a long quiet period, so it can never outrun the event path.
#
# It adds no new authority: every gate the event path applies is applied here too —
# registration, external wait, terminal gate, pending input, MIN_INTERVAL_SECS,
# MAX_CONSECUTIVE and the safety classifier. Its only extra condition is that the target
# has been untouched by supervision for IDLE_SWEEP_QUIET_SECS.
IDLE_SWEEP_ENABLED = os.getenv("NATIVE_SUPERVISOR_IDLE_SWEEP", "1") not in ("0", "false", "no")
IDLE_SWEEP_QUIET_SECS = int(os.getenv("NATIVE_SUPERVISOR_IDLE_SWEEP_QUIET_SECS", "300"))
_AT_REST = frozenset({"idle", "completed", "unknown", ""})


def _quiet_secs(conn, target: str, now: float) -> Optional[float]:
    """How long `agent_watch` has observed this target unchanged, or None.

    The quiescence watcher is the authority here, which is what keeps this an EMERGENCY
    fallback: no row means no evidence of rest, and the sweep declines rather than
    guessing from a pane state sampled once.
    """
    try:
        row = conn.execute(
            "SELECT digest_since, cls FROM agent_watch_state WHERE target=?",
            (target,)).fetchone()
    except Exception:  # noqa: BLE001 — the watcher may not have run yet
        return None
    if not row or row[0] is None:
        return None
    return max(0.0, now - float(row[0]))

# ── terminal gate ────────────────────────────────────────────────────────────────────
# Requirement 4: when continuation is not converging, the supervisor must record ONE
# terminal state and go quiet — not keep skipping silently every 20 seconds forever.
#
# Hitting MAX_CONSECUTIVE in an hour IS the operational definition of "not converging":
# six automated continuations produced another turn boundary each time. Before this, the
# loop simply started refusing at the cap and said nothing, so an agent that had stopped
# making progress became invisible: no continuation, no event, no owner signal. The gate
# makes that state explicit, exactly once, and stops the sends until it clears.
GATE_TTL_SECS = int(os.getenv("NATIVE_SUPERVISOR_GATE_TTL_SECS", "21600"))   # 6h

_GATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS native_supervision_gate (
    target TEXT PRIMARY KEY, since TEXT, since_ts REAL, until_ts REAL,
    reason TEXT, event_id INTEGER, cleared_ts REAL);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS native_supervision (
    event_id INTEGER PRIMARY KEY, ts TEXT, ts_epoch REAL, target TEXT, action TEXT,
    reason TEXT, ok INTEGER, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_native_sup_target ON native_supervision (target, ts_epoch);
"""


# An INTENTIONAL EXTERNAL WAIT, declared as durable state rather than inferred.
#
# The Stop hook's `background_tasks` / `session_crons` prove a wait structurally, but only
# once the agent NEXT ENDS A TURN. An agent that was already parked when hooks were
# installed emits nothing at all — measured on 2026-08-30: every live session had native
# records except `/opt/diamond/auction`, which was idle on a watch and therefore silent,
# and kept being escalated (15519, then 15567) as a stall it was not.
#
# So the state can also be DECLARED, with three properties that keep it honest:
#   * it names who declared it and why, and is auditable;
#   * it EXPIRES, so it can never silence an agent forever;
#   * it suppresses only no-progress escalation. A crash, a failure, or the agent asking
#     a question still wakes exactly as before — this is not a mute button.
EXTERNAL_WAIT_DEFAULT_TTL_SECS = int(
    os.getenv("NATIVE_SUPERVISOR_EXTERNAL_WAIT_TTL_SECS", "21600"))  # 6h

_WAIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS native_external_wait (
    target TEXT PRIMARY KEY, since TEXT, since_ts REAL, until_ts REAL,
    by TEXT, reason TEXT, evidence TEXT);
"""


def mark_external_wait(target: str, *, reason: str, by: str = "owner-os",
                       evidence: str = "", ttl_secs: Optional[int] = None,
                       conn=None, now: Optional[float] = None) -> dict:
    """Declare that `target` is waiting on something external, on purpose.

    Bounded by construction: without an explicit ttl it expires after
    EXTERNAL_WAIT_DEFAULT_TTL_SECS, and an expired declaration is simply absent.
    """
    now = now if now is not None else time.time()
    ttl = int(ttl_secs if ttl_secs is not None else EXTERNAL_WAIT_DEFAULT_TTL_SECS)
    if not target or ttl <= 0:
        return {"ok": False, "reason": "invalid_target_or_ttl"}
    conn, own = _conn(conn)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO native_external_wait "
            "(target,since,since_ts,until_ts,by,reason,evidence) VALUES (?,?,?,?,?,?,?)",
            (target, now_iso(), now, now + ttl, by, reason[:300], evidence[:600]))
        conn.commit()
        return {"ok": True, "target": target, "until_ts": now + ttl, "ttl_secs": ttl}
    finally:
        if own:
            conn.close()


def clear_external_wait(target: str, *, conn=None) -> dict:
    conn, own = _conn(conn)
    try:
        conn.execute("DELETE FROM native_external_wait WHERE target=?", (target,))
        conn.commit()
        return {"ok": True, "target": target}
    finally:
        if own:
            conn.close()


def in_external_wait(target: str, *, conn=None, now: Optional[float] = None) -> bool:
    """Is this target under a LIVE declaration? An expired one is not a declaration."""
    now = now if now is not None else time.time()
    conn, own = _conn(conn)
    try:
        row = conn.execute(
            "SELECT until_ts FROM native_external_wait WHERE target=?", (target,)).fetchone()
        return bool(row and row[0] and float(row[0]) > now)
    except Exception:  # noqa: BLE001 — unknown never means "suppress"
        return False
    finally:
        if own:
            conn.close()


def list_external_waits(conn=None, now: Optional[float] = None) -> list:
    now = now if now is not None else time.time()
    conn, own = _conn(conn)
    try:
        return [{"target": r[0], "since": r[1], "until_ts": r[3], "by": r[4],
                 "reason": r[5], "live": bool(r[3] and float(r[3]) > now)}
                for r in conn.execute(
                    "SELECT target,since,since_ts,until_ts,by,reason FROM "
                    "native_external_wait ORDER BY target")]
    finally:
        if own:
            conn.close()


def _conn(conn=None):
    conn, own = _c(conn)
    for stmt in (_SCHEMA + _WAIT_SCHEMA + _REG_SCHEMA).strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


# PROJECTS THAT NEVER AUTO-REGISTER. Auto-registration exists so a new agent is supervised
# without a manual edit; this list exists so that convenience can never reach a project
# whose gates are expensive to get wrong. A denylist beats discovery, always — an agent is
# excluded because of what its project DOES, not because nobody got round to listing it.
# The supervisor's own project, derived from where this file actually lives rather than
# named in a string. Supervising it would mean the supervisor answering its own turn
# boundaries and driving the very session that edits, tests and deploys the supervisor —
# so this entry must survive ANY configuration. Keeping it in the env-overridable list
# below made the one entry that can never be removed the easiest one to remove by
# accident: setting NATIVE_SUPERVISOR_DENY_PROJECTS for an unrelated reason silently
# dropped the self-reference guard along with everything else.
SELF_PROJECT = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Value-bearing and irreversible-risk projects. Env may WIDEN this; it can never narrow it
# past SELF_PROJECT. ACAP C1/C2 · Auction value gates · payment · outbound mail · miner
# triage.
AUTO_REGISTER_DENY_PROJECTS = {
    p.strip() for p in os.getenv(
        "NATIVE_SUPERVISOR_DENY_PROJECTS",
        "capacity,auction,payment-orchestrator,payorch,email,xmrig").split(",") if p.strip()
} | {SELF_PROJECT}
AUTO_REGISTER = os.getenv("NATIVE_SUPERVISOR_AUTO_REGISTER", "1") not in ("0", "false", "no")

_REG_SCHEMA = """
CREATE TABLE IF NOT EXISTS native_supervised_target (
    target TEXT PRIMARY KEY, project TEXT, cwd TEXT, since TEXT, since_ts REAL,
    by TEXT, reason TEXT);
"""


def _project_of(agent: dict) -> str:
    cwd = (agent.get("claude_cwd") or agent.get("cwd") or "").rstrip("/")
    return cwd.rsplit("/", 1)[-1] if cwd else ""


def auto_register(agents: list, *, conn=None, now: Optional[float] = None,
                  by: str = "auto-discovery") -> dict:
    """Register newly-seen managed agents for supervision, minus the deny-listed projects.

    Requirement 8 was "this must never become per-agent manual setup". The honest way to
    satisfy that without also handing the supervisor every future agent on the box is to
    make registration automatic but SUBTRACTIVE: everything is registered except projects
    whose gates are expensive, and every registration is durable and attributed so it can
    be read back and revoked.
    """
    now = now if now is not None else time.time()
    conn, own = _conn(conn)
    try:
        if not AUTO_REGISTER:
            return {"registered": [], "skipped": [{"why": "auto_register_disabled"}]}
        purge_denied(conn=conn)
        known = {r[0] for r in conn.execute("SELECT target FROM native_supervised_target")}
        registered, skipped = [], []
        for a in agents or []:
            t = a.get("target") or ""
            if not t or not a.get("is_agent") or not a.get("alive") or t in known:
                continue
            proj = _project_of(a)
            if proj in AUTO_REGISTER_DENY_PROJECTS:
                skipped.append({"target": t, "project": proj, "why": "deny_listed_project"})
                continue
            conn.execute(
                "INSERT OR REPLACE INTO native_supervised_target "
                "(target,project,cwd,since,since_ts,by,reason) VALUES (?,?,?,?,?,?,?)",
                (t, proj, a.get("claude_cwd") or a.get("cwd") or "", now_iso(), now, by,
                 "auto-registered on discovery"))
            registered.append({"target": t, "project": proj})
        conn.commit()
        return {"registered": registered, "skipped": skipped}
    finally:
        if own:
            conn.close()


def registered_targets(conn=None) -> set:
    """Registered AND not deny-listed.

    The denylist has to be evaluated on READ, not only at registration time. A target
    registered before a denylist changed would otherwise stay supervised for ever —
    which is not hypothetical: `owner-os-wake-policy-opus` (project `ai-dev-runtime`,
    the supervisor's own session) was registered by an earlier build and still read as
    supervised after `ai-dev-runtime` was added to the denylist. A supervisor that
    answers its own turn boundaries loops on itself.
    """
    conn, own = _conn(conn)
    try:
        return {r[0] for r in conn.execute(
            "SELECT target, COALESCE(project,'') FROM native_supervised_target")
            if r[1] not in AUTO_REGISTER_DENY_PROJECTS}
    finally:
        if own:
            conn.close()


def purge_denied(conn=None) -> list:
    """Drop registrations whose project is now deny-listed. Idempotent, and safe to run
    on every pass — a denylist change takes effect without anyone remembering to clean up."""
    conn, own = _conn(conn)
    try:
        rows = [r[0] for r in conn.execute(
            "SELECT target, COALESCE(project,'') FROM native_supervised_target")
            if r[1] in AUTO_REGISTER_DENY_PROJECTS]
        for t in rows:
            conn.execute("DELETE FROM native_supervised_target WHERE target=?", (t,))
        if rows:
            conn.commit()
        return rows
    finally:
        if own:
            conn.close()


def allowed_targets() -> set:
    raw = (_TARGETS_RAW or "").strip()
    return {"*"} if raw == "*" else {t.strip() for t in raw.split(",") if t.strip()}


def validate_config() -> dict:
    """Is the supervisor's configuration coherent, and are the gates actually standing?

    The denylist is the only thing between an automated continuation and a pane whose agent
    holds mutation authority, and it is assembled from an environment variable. A typo there
    does not fail loudly — it silently produces a SHORTER denylist, which opens a gate nobody
    decided to open. Nothing checked that until now.

    Returns {"ok": bool, "problems": [...], "checked": {...}}. Problems are stated, never
    repaired: a config this important should be corrected deliberately, not patched at
    import time by the thing it governs.
    """
    problems, checked = [], {}

    checked["self_project"] = SELF_PROJECT
    if not SELF_PROJECT:
        problems.append("SELF_PROJECT is empty — the recursion guard cannot hold")
    elif SELF_PROJECT not in AUTO_REGISTER_DENY_PROJECTS:
        problems.append(
            f"SELF_PROJECT {SELF_PROJECT!r} is missing from the denylist — the supervisor "
            "could drive the session that deploys it")

    checked["deny_projects"] = sorted(AUTO_REGISTER_DENY_PROJECTS)
    # The value-bearing set is not merely a default: losing any of it silently widens who
    # may be typed into. Named explicitly so a shortened env is a PROBLEM, not a surprise.
    for required in ("capacity", "auction", "payment-orchestrator", "payorch", "xmrig"):
        if required not in AUTO_REGISTER_DENY_PROJECTS:
            problems.append(f"value-bearing project {required!r} is not denylisted")

    for name, value, low in (("MIN_INTERVAL_SECS", MIN_INTERVAL_SECS, 1),
                             ("MAX_CONSECUTIVE", MAX_CONSECUTIVE, 1),
                             ("MAX_EVENT_AGE_SECS", MAX_EVENT_AGE_SECS, 1),
                             ("GATE_TTL_SECS", GATE_TTL_SECS, 1),
                             ("IDLE_SWEEP_QUIET_SECS", IDLE_SWEEP_QUIET_SECS, 1)):
        checked[name] = value
        if value < low:
            problems.append(f"{name}={value} disables a rate guard (must be >= {low})")

    checked["goal_autosubmit"] = GOAL_AUTOSUBMIT
    if GOAL_AUTOSUBMIT:
        problems.append("GOAL_AUTOSUBMIT is on — a /goal line changes behaviour for many "
                        "turns and is not on the safe-continuation allowlist")

    checked["targets_raw"] = _TARGETS_RAW
    if _TARGETS_RAW.strip() == "*":
        # A wildcard rollout is legitimate; it must still not reach a denylisted project,
        # which auto_register enforces. Recorded so the combination is visible.
        checked["wildcard_rollout"] = True

    return {"ok": not problems, "problems": problems, "checked": checked}


def send_block_reason(target: str, project: str = "", *, conn=None) -> str:
    """WHY a target may not be sent to — the two reasons are not the same thing.

    Lifecycle OBSERVATION is already unconditional: hook events from every project are
    recorded durably (112 from ai-dev-runtime and 28 from capacity in a single day), and a
    genuine gate still routes through the owner-facing wake path, which does not consult
    this. What the denylist governs is narrower — whether the supervisor may TYPE INTO a
    pane. Conflating the two made the journal say `not_in_rollout_allowlist` for a
    value-bearing project that is deliberately excluded and for a project nobody has
    registered yet, which are different situations with different remedies.

      value_bearing_send_blocked — ACAP, Auction, payment, mail, miner triage. Deliberate
        and owner-only to change. Supervision authority is not mutation authority, but the
        supervisor types into a pane whose agent HOLDS mutation authority, so the block
        stays until an owner decides otherwise.
      supervisor_self_reference — the supervisor's own project. Structural, derived from
        the module's location, and not a value judgement: it would answer its own turn
        boundaries and drive the session that edits it.
      not_registered — simply never registered. Auto-registration fixes this by itself.
    """
    if project and project in AUTO_REGISTER_DENY_PROJECTS:
        return ("supervisor_self_reference" if project == SELF_PROJECT
                else "value_bearing_send_blocked")
    return "not_registered"


def is_supervised(target: str, *, conn=None) -> bool:
    """Supervised if explicitly allow-listed OR auto-registered. Either way it is a
    positive record — nothing is supervised merely by existing."""
    if not target:
        return False
    a = allowed_targets()
    if "*" in a or target in a:
        return True
    try:
        return target in registered_targets(conn=conn)
    except Exception:  # noqa: BLE001 — unknown is never "supervised"
        return False


def resolve_target(cwd: str, agents: list) -> Optional[str]:
    """The ONE live agent pane whose Claude runs in `cwd`.

    Zero matches or more than one is a refusal, never a guess. Two panes on one directory
    is precisely the state that produced a duplicate live agent earlier today; acting on
    an ambiguous identity is how a supervisor becomes the incident.
    """
    if not cwd:
        return None
    want = cwd.rstrip("/")
    hits = [a.get("target") for a in agents
            if a.get("is_agent") and a.get("alive")
            and (a.get("claude_cwd") or a.get("cwd") or "").rstrip("/") == want]
    return hits[0] if len(hits) == 1 else None


def open_gate(target: str, *, reason: str, conn=None, now: Optional[float] = None,
              emit_fn: Optional[Callable] = None, owner_facing: bool = True,
              project_id: str = "") -> dict:
    """Record the terminal state ONCE and emit one owner-facing event.

    Idempotent by construction: the row is the latch. A second call while the gate stands
    returns `opened=False` and emits nothing, which is what keeps a stuck agent from
    becoming an hourly notification.
    """
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        conn.execute(_GATE_SCHEMA)
        row = conn.execute(
            "SELECT until_ts, cleared_ts, event_id FROM native_supervision_gate "
            "WHERE target=?", (target,)).fetchone()
        if row and not row[1] and float(row[0] or 0) > now:
            return {"opened": False, "reason": "gate_already_open",
                    "event_id": int(row[2] or 0)}
        if emit_fn is None:
            from core.control_plane import cto
            emit_fn = cto.emit
        # Without a project the alarm cannot route to the project's own chat and lands
        # project-less in the inbox: all four gate events raised on 2026-08-30 carried an
        # EMPTY project_id. Falls back to the registration record, so a caller that does
        # not know the project still produces a routable event.
        if not project_id:
            try:
                r = conn.execute("SELECT project FROM native_supervised_target "
                                 "WHERE target=?", (target,)).fetchone()
                project_id = (r[0] or "") if r else ""
            except Exception:  # noqa: BLE001
                project_id = ""
        res = emit_fn("native_supervisor", "agent_continuation_exhausted",
                      agent_id=target, project_id=project_id,
                      severity="high" if owner_facing else "info",
                      owner_action_required=bool(owner_facing),
                      payload={"target": target, "reason": reason,
                               "gate_ttl_secs": GATE_TTL_SECS,
                               "automated_continuation": "stopped"},
                      action_taken=(f"{target}: automated continuation stopped — {reason}. "
                                    f"No further supervisor sends until the gate clears."),
                      dedup_key=f"nativesup:gate:{target}",
                      dedup_window_secs=GATE_TTL_SECS, conn=conn) or {}
        eid = int(res.get("event_id") or 0)
        conn.execute(
            "INSERT INTO native_supervision_gate"
            "(target,since,since_ts,until_ts,reason,event_id,cleared_ts) "
            "VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(target) DO UPDATE SET "
            "since=excluded.since, since_ts=excluded.since_ts, until_ts=excluded.until_ts, "
            "reason=excluded.reason, event_id=excluded.event_id, cleared_ts=NULL",
            (target, now_iso(), now, now + GATE_TTL_SECS, reason, eid))
        conn.commit()
        return {"opened": True, "reason": reason, "event_id": eid}
    finally:
        if own:
            conn.close()


# How far back a recorded intentional-wait skip still speaks for a target.
GATE_WAIT_LOOKBACK_SECS = int(os.getenv("NATIVE_SUPERVISOR_GATE_WAIT_LOOKBACK_SECS", "3600"))


def gate_exemption(conn, target: str, cwd: str = "", now: Optional[float] = None) -> str:
    """Is there an innocent explanation for reaching the continuation cap?

    The gate's claim is "six automated continuations each produced another turn boundary,
    so this is not converging". That claim is only sound if the agent had something to
    converge ON and was not deliberately waiting. Two ways it can be false:

    * The agent is in an intentional external wait. `native_supervision` records that skip
      under the payload CWD, because `decide()` runs before `resolve_target()`, while the
      cap and the gate key on the resolved tmux target. So a target could be recognised as
      waiting-by-design fourteen times and still be escalated as stalled — the same
      namespace split as the `session:`/tmux one in Part 23, in a third place. Both forms
      are therefore checked.

    * The agent has no assigned task. Continuation was never going to converge on work that
      was never given. Sending should still STOP — poking an agent with nothing to do is
      the spin the cap exists to end — but calling that an owner-attention failure is
      wrong, so the gate opens without waking anyone.

    Returns "" when the cap really does mean what the gate says.
    """
    now = now if now is not None else now_ts()
    names = [n for n in (target, cwd, (cwd or "").rstrip("/")) if n]
    for n in names:
        if in_external_wait(n, conn=conn, now=now):
            return "intentional_external_wait"
    try:
        marks = ",".join("?" * len(names))
        row = conn.execute(
            f"SELECT 1 FROM native_supervision WHERE target IN ({marks}) "
            "AND reason='intentional_external_wait' AND ts_epoch > ? LIMIT 1",
            (*names, now - GATE_WAIT_LOOKBACK_SECS)).fetchone()
        if row:
            return "recent_intentional_external_wait"
    except Exception:  # noqa: BLE001 — unknown never means "suppress the alarm"
        return ""
    try:
        from core import os_task_queue as q
        if q.active_task(target, conn=conn) is None:
            return "no_assigned_task"
    except Exception:  # noqa: BLE001
        pass
    return ""


def in_gate(target: str, *, conn=None, now: Optional[float] = None) -> bool:
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        conn.execute(_GATE_SCHEMA)
        row = conn.execute(
            "SELECT until_ts, cleared_ts FROM native_supervision_gate WHERE target=?",
            (target,)).fetchone()
        return bool(row and not row[1] and float(row[0] or 0) > now)
    finally:
        if own:
            conn.close()


def clear_gate(target: str, *, reason: str = "progress_observed", conn=None,
               now: Optional[float] = None) -> dict:
    """The agent resumed on its own, so the terminal state is no longer true.

    Deliberately NOT owner-facing: recovery is the good news, and the outage already spoke.
    """
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        conn.execute(_GATE_SCHEMA)
        cur = conn.execute(
            "UPDATE native_supervision_gate SET cleared_ts=? "
            "WHERE target=? AND cleared_ts IS NULL", (now, target))
        conn.commit()
        return {"cleared": bool(cur.rowcount), "target": target, "reason": reason}
    finally:
        if own:
            conn.close()


def decide(event_type: str, payload: dict) -> dict:
    """What this signal means for continuation. Pure, so the policy is testable alone."""
    if event_type != "agent_turn_stopped":
        # Everything else is either a question, a completion or a failure. None of those
        # is ours to answer — they travel the owner-facing wake path unchanged.
        return {"action": "skip", "reason": f"not_a_turn_boundary:{event_type}"}
    if payload.get("_declared_external_wait"):
        return {"action": "skip", "reason": "intentional_external_wait"}
    if payload.get("background_tasks") or payload.get("session_crons"):
        # Waiting BY DESIGN. The Auction case: an armed monitor is not a stall, and
        # poking it would interrupt a deliberate wait.
        return {"action": "skip", "reason": "intentional_external_wait"}
    if payload.get("stop_hook_active"):
        # We are inside a stop hook's own continuation; re-entering would loop.
        return {"action": "skip", "reason": "stop_hook_active"}
    return {"action": "continue", "reason": "turn_ended_nothing_armed"}


# /goal — a verified-completion loop, so a substantial task keeps going turn to turn
# without a ping. Composing one is safe; AUTO-SUBMITTING one is not the same thing, and the
# difference is deliberate.
#
# `is_safe_continuation` is an ALLOWLIST of recognised benign meta-steps, and a `/goal`
# line is not one: it changes how the agent behaves for many turns, which is exactly the
# class of instruction that allowlist exists to keep out of an automated path. So this
# composes the text and refuses to submit it unless GOAL_AUTOSUBMIT is explicitly turned
# on — and even then only for a caller that supplies its own verifiable condition.
#
# Left OFF by default on purpose: widening the classifier is a safety decision, not an
# implementation detail, and nothing here does it silently.
GOAL_AUTOSUBMIT = os.getenv("NATIVE_SUPERVISOR_GOAL_AUTOSUBMIT", "0") not in ("0", "false", "no")
_GOAL_MAX_LEN = 300


def compose_goal_step(objective: str, completion_condition: str) -> Optional[str]:
    """A `/goal` line with a VERIFIABLE stopping condition, or None if it cannot be made.

    Refuses an empty objective, an empty condition, or anything over the length cap. A
    goal without a checkable end is how an agent loops forever, so the condition is not
    optional — a caller that has not decided what "done" means has not got a goal.
    """
    o = " ".join((objective or "").split())
    c = " ".join((completion_condition or "").split())
    if not o or not c:
        return None
    line = f"/goal {o} — done when: {c}"
    return line if len(line) <= _GOAL_MAX_LEN else None


def may_autosubmit_goal(text: str, safe_fn: Callable) -> dict:
    """May this /goal line be submitted automatically? Two independent gates."""
    if not text or not text.startswith("/goal "):
        return {"ok": False, "reason": "not_a_goal_line"}
    if not GOAL_AUTOSUBMIT:
        return {"ok": False, "reason": "goal_autosubmit_disabled"}
    if not safe_fn(text):
        return {"ok": False, "reason": "failed_safety_classifier"}
    return {"ok": True, "reason": "allowed"}


def _recent_for_target(conn, target: str, now: float) -> tuple:
    row = conn.execute(
        "SELECT MAX(ts_epoch) FROM native_supervision WHERE target=? AND action='continue'",
        (target,)).fetchone()
    last = float(row[0]) if row and row[0] else 0.0
    n = conn.execute(
        "SELECT COUNT(*) FROM native_supervision WHERE target=? AND action='continue' "
        "AND ok=1 AND ts_epoch > ?", (target, now - 3600)).fetchone()[0]
    return last, int(n)


def _record(conn, event_id: int, target: str, action: str, reason: str, ok: bool,
            detail=None) -> None:
    try:
        conn.execute(
            "INSERT OR REPLACE INTO native_supervision "
            "(event_id,ts,ts_epoch,target,action,reason,ok,detail) VALUES (?,?,?,?,?,?,?,?)",
            (int(event_id), now_iso(), now_ts(), target, action, reason,
             1 if ok else 0, json.dumps(detail or {}, default=str)[:800]))
        conn.commit()
    except Exception:  # noqa: BLE001 — the audit must never break the loop
        pass


def scan(*, conn=None, now: Optional[float] = None, agents: Optional[list] = None,
         send_fn: Optional[Callable] = None, safe_fn: Optional[Callable] = None,
         step_text: Optional[str] = None) -> dict:
    """One supervision pass over native lifecycle events not yet acted on."""
    now = now if now is not None else time.time()
    conn, own = _conn(conn)
    try:
        if not ENABLED:
            return {"acted": [], "skipped": [{"why": "disabled"}]}
        if agents is None:
            from core import agent_control as ac
            agents = ac.agent_list().get("agents", [])
        if send_fn is None:
            from core import agent_control as ac
            send_fn = ac.agent_send
        if safe_fn is None:
            from core.agent_continuation_watchdog import is_safe_continuation as safe_fn
        if step_text is None:
            step_text = os.getenv("CONTINUATION_WATCHDOG_DEFAULT_STEP",
                                  "continue with the next safe step")

        # Requirement 8: a new agent becomes supervised on discovery, not by hand.
        try:
            auto_register(agents, conn=conn, now=now)
        except Exception:  # noqa: BLE001 — registration never breaks supervision
            pass

        rows = conn.execute(
            "SELECT e.id, e.type, e.payload FROM event e "
            "LEFT JOIN native_supervision s ON s.event_id = e.id "
            "WHERE e.source='claude_hook' AND s.event_id IS NULL "
            "AND e.ts_epoch > ? ORDER BY e.id", (now - MAX_EVENT_AGE_SECS,)).fetchall()

        acted, skipped = [], []
        for eid, etype, raw in rows:
            try:
                payload = json.loads(raw) if raw else {}
            except Exception:  # noqa: BLE001
                payload = {}
            d = decide(etype, payload)
            if d["action"] != "continue":
                _record(conn, eid, payload.get("cwd", ""), "skip", d["reason"], True)
                skipped.append({"event_id": eid, "why": d["reason"]})
                continue
            target = resolve_target(payload.get("cwd", ""), agents)
            if not target:
                _record(conn, eid, payload.get("cwd", ""), "skip",
                        "target_unresolved_or_ambiguous", True)
                skipped.append({"event_id": eid, "why": "target_unresolved_or_ambiguous"})
                continue
            if not is_supervised(target, conn=conn):
                why = send_block_reason(
                    target, _project_of({"claude_cwd": payload.get("cwd", "")}), conn=conn)
                _record(conn, eid, target, "skip", why, True)
                skipped.append({"event_id": eid, "target": target, "why": why,
                                "lifecycle_observed": True})
                continue
            # The pane must still be at rest RIGHT NOW — the event describes a moment that
            # has already passed, and re-reading live state is the point.
            def _skip(tgt: str, why: str) -> None:
                """Record a skip unless it describes a passing moment (see above)."""
                if why not in TRANSIENT_SKIP_REASONS:
                    _record(conn, eid, tgt, "skip", why, True)
                skipped.append({"event_id": eid, "target": tgt, "why": why,
                                "transient": why in TRANSIENT_SKIP_REASONS})

            live = next((a for a in agents if a.get("target") == target), None)
            if not live or live.get("state") in ("working", "shell_running"):
                # Working again under its own steam is the progress signal that retires a
                # terminal gate: the state the gate described is no longer true.
                if live:
                    try:
                        clear_gate(target, reason="agent_working_again", conn=conn, now=now)
                    except Exception:  # noqa: BLE001 — never breaks the loop
                        pass
                _skip(target, "agent_already_working_again")
                continue
            if in_gate(target, conn=conn, now=now):
                # Terminal state already recorded and already announced once. Stay quiet:
                # the whole point of the gate is that it does not re-notify.
                _record(conn, eid, target, "skip", "continuation_gate_open", True)
                skipped.append({"event_id": eid, "target": target,
                                "why": "continuation_gate_open"})
                continue
            if live.get("pending"):
                # Text is staged in the composer: a human or another controller is mid-
                # interaction. Never type over that.
                _skip(target, "pane_has_pending_input")
                continue
            last, n_hour = _recent_for_target(conn, target, now)
            if (now - last) < MIN_INTERVAL_SECS:
                _skip(target, "min_interval_not_elapsed")
                continue
            if n_hour >= MAX_CONSECUTIVE:
                # Not converging: MAX_CONSECUTIVE automated continuations in the hour each
                # produced another turn boundary. Record the terminal state ONCE, tell the
                # owner ONCE, and stop sending — rather than skipping silently forever.
                exempt = gate_exemption(conn, target, payload.get("cwd", ""), now)
                if exempt == "intentional_external_wait" or \
                        exempt == "recent_intentional_external_wait":
                    # Waiting by design is not a stall. No gate, no alarm.
                    _record(conn, eid, target, "skip", f"cap_reached_but_{exempt}", True)
                    skipped.append({"event_id": eid, "target": target,
                                    "why": f"cap_reached_but_{exempt}"})
                    continue
                g = open_gate(target, reason="continuation_cap_reached_without_progress",
                              conn=conn, now=now, owner_facing=not exempt)
                _record(conn, eid, target, "gate", "continuation_cap_reached_without_progress",
                        True, {"gate_opened": g.get("opened"),
                               "gate_event_id": g.get("event_id")})
                skipped.append({"event_id": eid, "target": target,
                                "why": "continuation_gate_opened" if g.get("opened")
                                else "continuation_gate_open"})
                continue
            if not safe_fn(step_text):
                # The allowlist is the authority on what may ever be auto-submitted.
                _record(conn, eid, target, "refuse", "step_failed_safety_classifier", False)
                skipped.append({"event_id": eid, "target": target,
                                "why": "step_failed_safety_classifier"})
                continue
            idem = f"nativesup:{eid}"
            try:
                res = send_fn(target, step_text, idempotency_key=idem,
                              actor="native_supervisor", source="claude_hook")
            except Exception as e:  # noqa: BLE001
                _record(conn, eid, target, "continue", f"send_failed:{type(e).__name__}",
                        False, {"error": str(e)[:200]})
                skipped.append({"event_id": eid, "target": target, "why": "send_failed"})
                continue
            ok = bool(res.get("delivered"))
            _record(conn, eid, target, "continue",
                    "continued_same_agent" if ok else f"not_delivered:{res.get('refused')}",
                    ok, {k: res.get(k) for k in
                         ("delivered", "submitted", "queued", "duplicate", "agent_created")})
            (acted if ok else skipped).append(
                {"event_id": eid, "target": target, "idempotency_key": idem,
                 "delivered": ok, "agent_created": res.get("agent_created")})
        if IDLE_SWEEP_ENABLED:
            acted_targets = {a.get("target") for a in acted}
            for a in agents:
                target = a.get("target") or ""
                if not target or target in acted_targets:
                    continue
                if (a.get("state") or "") not in _AT_REST or a.get("pending"):
                    continue
                if not is_supervised(target, conn=conn):
                    continue
                if in_external_wait(target, conn=conn, now=now) or in_gate(
                        target, conn=conn, now=now):
                    continue
                quiet = _quiet_secs(conn, target, now)
                if quiet is None or quiet < IDLE_SWEEP_QUIET_SECS:
                    # No quiescence evidence, or not quiet long enough. The event path
                    # owns everything before this point.
                    continue
                last, n_hour = _recent_for_target(conn, target, now)
                if (now - last) < max(MIN_INTERVAL_SECS, IDLE_SWEEP_QUIET_SECS):
                    continue
                if n_hour >= MAX_CONSECUTIVE:
                    # Same exemption as the event path. Without this the sweep was the
                    # louder of the two doors into the same room: it defaulted to
                    # owner_facing=True, so an agent with no assigned task would wake the
                    # owner here even though the event path had just been taught not to.
                    # Only the emit-level 6h dedup was hiding it (arbitrage2-fable,
                    # gate_opened=true at 00:39:19Z, silent solely because event 15986 was
                    # still inside the window).
                    exempt = gate_exemption(conn, target, a.get("claude_cwd")
                                            or a.get("cwd") or "", now)
                    if exempt in ("intentional_external_wait",
                                  "recent_intentional_external_wait"):
                        _record(conn, 0, target, "skip", f"cap_reached_but_{exempt}", True)
                        continue
                    g = open_gate(target, reason="idle_sweep_cap_reached_without_progress",
                                  conn=conn, now=now, owner_facing=not exempt)
                    _record(conn, 0, target, "gate",
                            "idle_sweep_cap_reached_without_progress", True,
                            {"gate_opened": g.get("opened"),
                             "gate_event_id": g.get("event_id"), "exempt": exempt})
                    continue
                if not safe_fn(step_text):
                    _record(conn, 0, target, "refuse", "step_failed_safety_classifier", False)
                    continue
                idem = f"nativesup:idle:{target}:{int(now // IDLE_SWEEP_QUIET_SECS)}"
                try:
                    res = send_fn(target, step_text, idempotency_key=idem,
                                  actor="native_supervisor", source="idle_sweep")
                except Exception as e:  # noqa: BLE001
                    _record(conn, 0, target, "continue",
                            f"send_failed:{type(e).__name__}", False,
                            {"error": str(e)[:200]})
                    continue
                ok = bool(res.get("delivered"))
                _record(conn, 0, target, "continue",
                        "continued_same_agent_idle_sweep" if ok
                        else f"not_delivered:{res.get('refused')}", ok,
                        {k: res.get(k) for k in
                         ("delivered", "submitted", "queued", "duplicate", "agent_created")})
                (acted if ok else skipped).append(
                    {"event_id": 0, "target": target, "idempotency_key": idem,
                     "delivered": ok, "via": "idle_sweep"})
        return {"acted": acted, "skipped": skipped}
    finally:
        if own:
            conn.close()
