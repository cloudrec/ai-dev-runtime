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
AUTO_REGISTER_DENY_PROJECTS = {
    p.strip() for p in os.getenv(
        "NATIVE_SUPERVISOR_DENY_PROJECTS",
        # ACAP C1/C2 · Auction value-bearing gates · payment · outbound mail · miner
        # triage · and ai-dev-runtime, because the supervisor must never drive its own
        # session: it would answer its own turn boundaries and loop on itself.
        "capacity,auction,payment-orchestrator,payorch,email,xmrig,"
        "ai-dev-runtime").split(",") if p.strip()
}
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
                _record(conn, eid, target, "skip", "not_in_rollout_allowlist", True)
                skipped.append({"event_id": eid, "target": target,
                                "why": "not_in_rollout_allowlist"})
                continue
            # The pane must still be at rest RIGHT NOW — the event describes a moment that
            # has already passed, and re-reading live state is the point.
            live = next((a for a in agents if a.get("target") == target), None)
            if not live or live.get("state") in ("working", "shell_running"):
                _record(conn, eid, target, "skip", "agent_already_working_again", True)
                skipped.append({"event_id": eid, "target": target,
                                "why": "agent_already_working_again"})
                continue
            if live.get("pending"):
                # Text is staged in the composer: a human or another controller is mid-
                # interaction. Never type over that.
                _record(conn, eid, target, "skip", "pane_has_pending_input", True)
                skipped.append({"event_id": eid, "target": target,
                                "why": "pane_has_pending_input"})
                continue
            last, n_hour = _recent_for_target(conn, target, now)
            if (now - last) < MIN_INTERVAL_SECS:
                _record(conn, eid, target, "skip", "min_interval_not_elapsed", True)
                skipped.append({"event_id": eid, "target": target,
                                "why": "min_interval_not_elapsed"})
                continue
            if n_hour >= MAX_CONSECUTIVE:
                _record(conn, eid, target, "skip", "hourly_continuation_cap", True)
                skipped.append({"event_id": eid, "target": target,
                                "why": "hourly_continuation_cap"})
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
        return {"acted": acted, "skipped": skipped}
    finally:
        if own:
            conn.close()
