"""Continue managed agents from NATIVE lifecycle signals, without ChatGPT in the loop.

The old normal path was: scrape a pane, classify prose, wake ChatGPT, let ChatGPT continue
the agent — three inferences and a browser between "a turn ended" and "keep going". These
tests pin what the supervisor may do, and far more importantly what it may NOT.
"""
from __future__ import annotations

import os
import time

import pytest

from core import native_supervisor as ns


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    yield


def _agent(target="cp-canary:0.0", cwd="/root/cp-canary-v2", state="idle",
           pending=None, alive=True):
    return {"target": target, "claude_cwd": cwd, "cwd": cwd, "is_agent": True,
            "alive": alive, "state": state, "pending": pending}


# ── the policy, in isolation ─────────────────────────────────────────────────
def test_a_plain_turn_end_is_a_continuation_candidate():
    assert ns.decide("agent_turn_stopped", {})["action"] == "continue"


def test_an_armed_monitor_is_an_intentional_wait_not_a_stall():
    """The Auction case: a read-only monitor armed for a natural external close."""
    assert ns.decide("agent_turn_stopped",
                     {"background_tasks": [{"id": "w"}]})["reason"] == "intentional_external_wait"
    assert ns.decide("agent_turn_stopped",
                     {"session_crons": [{"schedule": "*/5 * * * *"}]})["action"] == "skip"


def test_questions_completions_and_failures_are_never_ours_to_answer():
    for et in ("agent_waiting_input", "task_completed", "agent_process_failed"):
        assert ns.decide(et, {})["action"] == "skip"


def test_a_stop_hook_already_continuing_does_not_re_enter():
    assert ns.decide("agent_turn_stopped", {"stop_hook_active": True})["action"] == "skip"


# ── identity: never guess which pane ─────────────────────────────────────────
def test_one_pane_for_the_cwd_resolves():
    assert ns.resolve_target("/root/cp-canary-v2", [_agent()]) == "cp-canary:0.0"


def test_two_panes_on_one_cwd_refuse_rather_than_guess():
    """Acting on an ambiguous identity is exactly how a duplicate live agent happened
    earlier today."""
    dup = [_agent(), _agent(target="cp-canary-2:0.0")]
    assert ns.resolve_target("/root/cp-canary-v2", dup) is None


def test_no_matching_pane_refuses():
    assert ns.resolve_target("/opt/nowhere", [_agent()]) is None
    assert ns.resolve_target("", [_agent()]) is None


# ── the roll-out gate ────────────────────────────────────────────────────────
def test_rollout_is_an_allowlist_not_everything_that_appears(monkeypatch):
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    assert ns.is_supervised("cp-canary:0.0") is True
    assert ns.is_supervised("mess-opus:0.0") is False
    monkeypatch.setattr(ns, "_TARGETS_RAW", "*")
    assert ns.is_supervised("anything:0.0") is True


# ── the declared external wait: bounded, audited, not a mute button ──────────
def test_a_declared_wait_is_live_then_expires():
    now = time.time()
    ns.mark_external_wait("x:0.0", reason="waiting on an external close", ttl_secs=60,
                          now=now)
    assert ns.in_external_wait("x:0.0", now=now + 10) is True
    assert ns.in_external_wait("x:0.0", now=now + 61) is False, "a declaration must expire"


def test_an_undeclared_target_is_not_waiting():
    assert ns.in_external_wait("never-declared:0.0") is False


def test_a_declaration_records_who_and_why():
    ns.mark_external_wait("y:0.0", reason="natural auction close", by="owner-os-session",
                          evidence="event 15519/15567")
    row = [w for w in ns.list_external_waits() if w["target"] == "y:0.0"][0]
    assert row["by"] == "owner-os-session" and "auction" in row["reason"]


def test_a_declaration_can_be_cleared():
    ns.mark_external_wait("z:0.0", reason="r", ttl_secs=600)
    ns.clear_external_wait("z:0.0")
    assert ns.in_external_wait("z:0.0") is False


# ── scan(): what it actually does to a live pane ─────────────────────────────
def _hook_event(conn, cwd="/root/cp-canary-v2", etype="agent_turn_stopped", payload=None,
                ts=None):
    import json as _j
    ts = time.time() if ts is None else ts
    p = {"source": "claude_hook", "cwd": cwd, "session_id": "s1"}
    p.update(payload or {})
    cur = conn.execute(
        "INSERT INTO event (ts,ts_epoch,source,type,agent_id,severity,payload) "
        "VALUES (?,?,?,?,?,?,?)",
        ("t", ts, "claude_hook", etype, "session:s1", "info", _j.dumps(p)))
    conn.commit()
    return cur.lastrowid


def _sent(calls):
    def _send(target, text, idempotency_key=None, actor=None, source=None):
        calls.append({"target": target, "text": text, "idem": idempotency_key})
        return {"delivered": True, "submitted": True, "agent_created": False}
    return _send


def _scan(conn, agents, calls, **kw):
    return ns.scan(conn=conn, agents=agents, send_fn=_sent(calls),
                   safe_fn=lambda t: True, step_text="continue with the next safe step",
                   **kw)


def test_it_continues_the_same_agent_and_never_creates_one(monkeypatch):
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    conn, _ = ns._conn()
    _hook_event(conn)
    calls = []
    r = _scan(conn, [_agent()], calls)
    assert len(r["acted"]) == 1 and calls[0]["target"] == "cp-canary:0.0"
    assert r["acted"][0]["agent_created"] is False
    assert calls[0]["idem"].startswith("nativesup:"), "durable idempotency key"


def test_an_agent_that_went_back_to_work_is_left_alone(monkeypatch):
    """The event describes a moment already past; live state is re-read before acting."""
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    conn, _ = ns._conn()
    _hook_event(conn)
    calls = []
    r = _scan(conn, [_agent(state="working")], calls)
    assert calls == [] and r["skipped"][0]["why"] == "agent_already_working_again"


def test_staged_input_is_never_typed_over(monkeypatch):
    """Text in the composer means a human or another controller is mid-interaction."""
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    conn, _ = ns._conn()
    _hook_event(conn)
    calls = []
    r = _scan(conn, [_agent(pending="rm -rf something")], calls)
    assert calls == [] and r["skipped"][0]["why"] == "pane_has_pending_input"


def test_a_deny_listed_agent_is_never_touched(monkeypatch):
    """Auto-registration means "everything except", so the denylist is what actually
    protects the expensive projects — and it must beat discovery every time."""
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    monkeypatch.setattr(ns, "AUTO_REGISTER_DENY_PROJECTS", {"auction"})
    conn, _ = ns._conn()
    _hook_event(conn, cwd="/opt/diamond/auction")
    calls = []
    r = _scan(conn, [_agent(target="diamond-auction:0.0", cwd="/opt/diamond/auction")], calls)
    assert calls == [], "a deny-listed project must never be continued"
    assert r["skipped"][0]["why"] == "not_in_rollout_allowlist"


def test_the_supervisor_never_drives_its_own_session():
    """ai-dev-runtime is deny-listed: a supervisor that answers its own turn boundaries
    would loop on itself."""
    assert "ai-dev-runtime" in ns.AUTO_REGISTER_DENY_PROJECTS
    r = ns.auto_register([_agent(target="owner-os-wake-policy-opus:0.0",
                                 cwd="/root/ai-dev-runtime")])
    assert r["registered"] == []


def test_each_event_is_acted_on_at_most_once(monkeypatch):
    """Exactly-once: a second pass over the same event must do nothing."""
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    conn, _ = ns._conn()
    _hook_event(conn)
    calls = []
    _scan(conn, [_agent()], calls)
    _scan(conn, [_agent()], calls)
    assert len(calls) == 1, "the same lifecycle event must not continue an agent twice"


def test_a_tight_stream_of_stops_cannot_drive_a_loop(monkeypatch):
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    conn, _ = ns._conn()
    calls = []
    for _ in range(4):
        _hook_event(conn)
        _scan(conn, [_agent()], calls)
    assert len(calls) == 1, "the per-target floor must hold after the first continuation"


def test_an_unsafe_step_is_refused_by_the_allowlist(monkeypatch):
    """The safe-step classifier is the authority on what may ever be auto-submitted."""
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    conn, _ = ns._conn()
    _hook_event(conn)
    calls = []
    r = ns.scan(conn=conn, agents=[_agent()], send_fn=_sent(calls),
                safe_fn=lambda t: False, step_text="rm -rf /")
    assert calls == [] and r["skipped"][0]["why"] == "step_failed_safety_classifier"


def test_a_declared_external_wait_stops_the_supervisor_poking_it(monkeypatch):
    """The Auction shape: parked on purpose, so continuation would interrupt a deliberate
    wait rather than help."""
    monkeypatch.setattr(ns, "_TARGETS_RAW", "*")
    conn, _ = ns._conn()
    _hook_event(conn, payload={"_declared_external_wait": True})
    calls = []
    r = _scan(conn, [_agent()], calls)
    assert calls == [] and r["skipped"][0]["why"] == "intentional_external_wait"


def test_stale_events_are_not_acted_on(monkeypatch):
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    conn, _ = ns._conn()
    _hook_event(conn, ts=time.time() - ns.MAX_EVENT_AGE_SECS - 60)
    calls = []
    _scan(conn, [_agent()], calls)
    assert calls == [], "an hour-old stop has been handled by something else"


# ── auto-registration (requirement 8: never per-agent manual setup) ──────────
def test_a_newly_discovered_agent_registers_itself():
    r = ns.auto_register([_agent(target="brand-new:0.0", cwd="/opt/newproj")])
    assert r["registered"] == [{"target": "brand-new:0.0", "project": "newproj"}]
    assert ns.is_supervised("brand-new:0.0") is True


def test_deny_listed_projects_never_auto_register(monkeypatch):
    """Convenience must not reach a project whose gates are expensive to get wrong."""
    monkeypatch.setattr(ns, "AUTO_REGISTER_DENY_PROJECTS", {"capacity", "auction",
                                                            "payment-orchestrator", "email"})
    agents = [_agent(target="capacity-blockchain:0.0", cwd="/opt/capacity"),
              _agent(target="diamond-auction:0.0", cwd="/opt/diamond/auction"),
              _agent(target="email:0.0", cwd="/opt/email"),
              _agent(target="ok-agent:0.0", cwd="/opt/harmless")]
    r = ns.auto_register(agents)
    assert [x["target"] for x in r["registered"]] == ["ok-agent:0.0"]
    for t in ("capacity-blockchain:0.0", "diamond-auction:0.0", "email:0.0"):
        assert ns.is_supervised(t) is False, t


def test_registration_is_idempotent():
    a = [_agent(target="dup:0.0", cwd="/opt/dup")]
    ns.auto_register(a)
    assert ns.auto_register(a)["registered"] == [], "already known, so not re-registered"


def test_dead_or_non_agent_panes_are_not_registered():
    assert ns.auto_register([_agent(target="dead:0.0", cwd="/opt/d", alive=False)])["registered"] == []
    a = _agent(target="shell:0.0", cwd="/opt/s"); a["is_agent"] = False
    assert ns.auto_register([a])["registered"] == []


def test_auto_registration_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(ns, "AUTO_REGISTER", False)
    r = ns.auto_register([_agent(target="nope:0.0", cwd="/opt/nope")])
    assert r["registered"] == [] and r["skipped"][0]["why"] == "auto_register_disabled"


def test_scan_registers_what_it_sees(monkeypatch):
    """The path that makes requirement 8 automatic rather than a chore."""
    monkeypatch.setattr(ns, "_TARGETS_RAW", "")
    conn, _ = ns._conn()
    calls = []
    _scan(conn, [_agent(target="seen-by-scan:0.0", cwd="/opt/seenproj")], calls)
    assert "seen-by-scan:0.0" in ns.registered_targets(conn=conn)


# ── /goal composition, and why it is not auto-submitted ──────────────────────
def test_a_goal_needs_a_verifiable_completion_condition():
    assert ns.compose_goal_step("finish the audit", "tests/audit.md exists and CI is green")
    assert ns.compose_goal_step("finish the audit", "") is None, "no end condition, no goal"
    assert ns.compose_goal_step("", "some condition") is None


def test_an_overlong_goal_is_refused():
    assert ns.compose_goal_step("x" * 400, "done") is None


def test_a_goal_line_is_not_auto_submitted_by_default():
    """It changes how the agent behaves for many turns, which is exactly what the
    fail-closed allowlist exists to keep out of an automated path."""
    line = ns.compose_goal_step("do the thing", "the thing is done")
    assert ns.may_autosubmit_goal(line, lambda t: True)["reason"] == "goal_autosubmit_disabled"


def test_even_enabled_a_goal_must_still_pass_the_safety_classifier(monkeypatch):
    monkeypatch.setattr(ns, "GOAL_AUTOSUBMIT", True)
    line = ns.compose_goal_step("do the thing", "the thing is done")
    assert ns.may_autosubmit_goal(line, lambda t: False)["reason"] == "failed_safety_classifier"
    assert ns.may_autosubmit_goal(line, lambda t: True)["ok"] is True


def test_non_goal_text_is_rejected_by_the_goal_gate(monkeypatch):
    monkeypatch.setattr(ns, "GOAL_AUTOSUBMIT", True)
    assert ns.may_autosubmit_goal("rm -rf /", lambda t: True)["reason"] == "not_a_goal_line"


def test_the_denylist_revokes_a_target_registered_before_it_changed(monkeypatch):
    """Found live: owner-os-wake-policy-opus (project ai-dev-runtime, the supervisor's OWN
    session) was registered by an earlier build and still read as supervised after
    ai-dev-runtime was deny-listed. The denylist has to be evaluated on READ, not only at
    registration time."""
    monkeypatch.setattr(ns, "AUTO_REGISTER_DENY_PROJECTS", set())
    conn, _ = ns._conn()
    ns.auto_register([_agent(target="self:0.0", cwd="/root/ai-dev-runtime")], conn=conn)
    assert ns.is_supervised("self:0.0", conn=conn) is True
    monkeypatch.setattr(ns, "AUTO_REGISTER_DENY_PROJECTS", {"ai-dev-runtime"})
    assert ns.is_supervised("self:0.0", conn=conn) is False, "a denylist change must revoke"


def test_denied_registrations_are_purged_not_merely_ignored(monkeypatch):
    monkeypatch.setattr(ns, "AUTO_REGISTER_DENY_PROJECTS", set())
    conn, _ = ns._conn()
    ns.auto_register([_agent(target="gone:0.0", cwd="/opt/payorch")], conn=conn)
    monkeypatch.setattr(ns, "AUTO_REGISTER_DENY_PROJECTS", {"payorch"})
    assert ns.purge_denied(conn=conn) == ["gone:0.0"]
    assert ns.purge_denied(conn=conn) == [], "idempotent"


# ── requirement 4: exhausted continuation records ONE terminal state, then goes quiet ──
# Before this, hitting MAX_CONSECUTIVE simply skipped, silently, every tick, forever: an
# agent that had stopped converging produced no continuation, no event and no owner signal.

def _gate_conn(tmp_path):
    import sqlite3
    c = sqlite3.connect(str(tmp_path / "gate.db"))
    c.execute(ns._GATE_SCHEMA)
    return c


def test_gate_opens_once_and_emits_exactly_one_event(tmp_path):
    conn = _gate_conn(tmp_path)
    seen = []

    def emit(source, etype, **kw):
        seen.append((etype, kw.get("owner_action_required"), kw.get("severity")))
        return {"event_id": 4242}

    a = ns.open_gate("x:0.0", reason="cap", conn=conn, now=1000.0, emit_fn=emit)
    b = ns.open_gate("x:0.0", reason="cap", conn=conn, now=1001.0, emit_fn=emit)
    assert a["opened"] is True and a["event_id"] == 4242
    assert b["opened"] is False and b["reason"] == "gate_already_open"
    assert seen == [("agent_continuation_exhausted", True, "high")]


def test_gate_suppresses_further_sends_while_open(tmp_path):
    conn = _gate_conn(tmp_path)
    ns.open_gate("x:0.0", reason="cap", conn=conn, now=1000.0,
                 emit_fn=lambda *a, **k: {"event_id": 1})
    assert ns.in_gate("x:0.0", conn=conn, now=1000.0) is True
    assert ns.in_gate("x:0.0", conn=conn, now=1000.0 + ns.GATE_TTL_SECS + 1) is False
    assert ns.in_gate("other:0.0", conn=conn, now=1000.0) is False


def test_progress_clears_the_gate_without_notifying(tmp_path):
    conn = _gate_conn(tmp_path)
    seen = []
    ns.open_gate("x:0.0", reason="cap", conn=conn, now=1000.0,
                 emit_fn=lambda *a, **k: seen.append(a) or {"event_id": 1})
    r = ns.clear_gate("x:0.0", conn=conn, now=1100.0)
    assert r["cleared"] is True
    assert ns.in_gate("x:0.0", conn=conn, now=1101.0) is False
    assert len(seen) == 1                       # clearing is silent; only the open spoke
    # and a genuinely new episode may open again
    assert ns.open_gate("x:0.0", reason="cap", conn=conn, now=1200.0,
                        emit_fn=lambda *a, **k: {"event_id": 2})["opened"] is True


def test_exhausted_gate_is_a_wake_type():
    from core import wake_bridge as wb
    assert "agent_continuation_exhausted" in wb.WAKE_EVENT_TYPES
    # genuine owner attention, but NOT the fast lifecycle lane
    assert wb.is_actionable("agent_continuation_exhausted") is False


# ── a transient skip must not consume its event (the idle dead-end) ───────────────────
# Recording a skip consumes the event: the candidate query joins on event_id. For a
# reason that describes a PASSING MOMENT that is a dead-end. Observed live 2026-08-30:
# an agent still mid-turn was skipped `agent_already_working_again` and its event
# consumed; the agent then finished and went idle, but the turn boundary it would have
# reported WAS the consumed event, so none ever arrived again. /opt/mess and /opt/seo sat
# idle, supervised, ungated and untouched.

def test_working_agent_skip_does_not_consume_the_event(monkeypatch):
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    conn, _ = ns._conn()
    eid = _hook_event(conn)
    calls = []
    r = _scan(conn, [_agent(state="working")], calls)
    assert calls == []
    assert r["skipped"][0]["why"] == "agent_already_working_again"
    assert r["skipped"][0]["transient"] is True
    # the event is still a candidate, so the agent settling later is not a dead-end
    left = conn.execute(
        "SELECT COUNT(*) FROM event e LEFT JOIN native_supervision s ON s.event_id=e.id "
        "WHERE e.id=? AND s.event_id IS NULL", (eid,)).fetchone()[0]
    assert left == 1


def test_the_agent_is_continued_once_it_settles(monkeypatch):
    """The whole point: no new hook event is needed, and exactly one send happens."""
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    conn, _ = ns._conn()
    _hook_event(conn)
    calls = []
    _scan(conn, [_agent(state="working")], calls)      # mid-turn
    _scan(conn, [_agent()], calls)                     # settled — no new event
    assert len(calls) == 1
    _scan(conn, [_agent()], calls)                     # and never twice
    assert len(calls) == 1


def test_a_terminal_skip_still_consumes_the_event(monkeypatch):
    """Non-transient reasons must stay one-shot, or the loop re-litigates them forever."""
    monkeypatch.setattr(ns, "_TARGETS_RAW", "")        # nothing in the rollout
    conn, _ = ns._conn()
    # a denylisted project: never auto-registered, so the skip is genuinely terminal
    eid = _hook_event(conn, cwd="/opt/capacity")
    calls = []
    r = _scan(conn, [_agent(target="capacity-blockchain:0.0", cwd="/opt/capacity")], calls)
    assert calls == []
    assert r["skipped"][0].get("transient") in (False, None)
    left = conn.execute(
        "SELECT COUNT(*) FROM event e LEFT JOIN native_supervision s ON s.event_id=e.id "
        "WHERE e.id=? AND s.event_id IS NULL", (eid,)).fetchone()[0]
    assert left == 0


# ── the quiescence sweep: emergency fallback for a consumed-event dead-end ────────────

def _watch_state(conn, target, quiet_secs, now=None):
    now = now or time.time()
    conn.execute("CREATE TABLE IF NOT EXISTS agent_watch_state ("
                 "target TEXT PRIMARY KEY, cls TEXT, digest TEXT, at TEXT, ts REAL,"
                 "notified_cls TEXT, notified_digest TEXT, notified_at TEXT,"
                 "notified_ts REAL, emissions INTEGER DEFAULT 0, miss_count INTEGER,"
                 "digest_since REAL)")
    conn.execute("INSERT OR REPLACE INTO agent_watch_state(target,cls,digest_since) "
                 "VALUES(?,?,?)", (target, "idle", now - quiet_secs))
    conn.commit()


def test_sweep_rescues_an_agent_with_no_event_left_to_react_to(monkeypatch):
    """The /opt/mess and /opt/seo state: at rest, supervised, and nothing to react to."""
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    conn, _ = ns._conn()
    _watch_state(conn, "cp-canary:0.0", ns.IDLE_SWEEP_QUIET_SECS + 60)
    calls = []
    r = _scan(conn, [_agent()], calls)              # no hook event at all
    assert len(calls) == 1
    assert r["acted"][0]["via"] == "idle_sweep"


def test_sweep_declines_without_quiescence_evidence(monkeypatch):
    """No watcher row means no evidence of rest — decline rather than guess."""
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    conn, _ = ns._conn()
    calls = []
    _scan(conn, [_agent()], calls)
    assert calls == []


def test_sweep_declines_while_the_agent_is_only_briefly_quiet(monkeypatch):
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    conn, _ = ns._conn()
    _watch_state(conn, "cp-canary:0.0", 10)
    calls = []
    _scan(conn, [_agent()], calls)
    assert calls == []


def test_sweep_respects_external_wait_and_the_terminal_gate(monkeypatch):
    """It adds no authority: every gate the event path applies is applied here too."""
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    conn, _ = ns._conn()
    _watch_state(conn, "cp-canary:0.0", ns.IDLE_SWEEP_QUIET_SECS + 60)
    ns.mark_external_wait("cp-canary:0.0", reason="armed monitor", conn=conn)
    calls = []
    _scan(conn, [_agent()], calls)
    assert calls == []
    ns.clear_external_wait("cp-canary:0.0", conn=conn)
    ns.open_gate("cp-canary:0.0", reason="cap", conn=conn,
                 emit_fn=lambda *a, **k: {"event_id": 1})
    _scan(conn, [_agent()], calls)
    assert calls == []


def test_sweep_does_not_double_send_with_the_event_path(monkeypatch):
    """An agent continued from its own event is not swept again in the same pass."""
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    conn, _ = ns._conn()
    _watch_state(conn, "cp-canary:0.0", ns.IDLE_SWEEP_QUIET_SECS + 60)
    _hook_event(conn)
    calls = []
    _scan(conn, [_agent()], calls)
    assert len(calls) == 1


# ── the supervisor must never be able to drive its own session ────────────────────────
# The self-reference entry used to live inside the env-overridable denylist, which made
# the one entry that can never be removed the easiest to remove by accident: setting
# NATIVE_SUPERVISOR_DENY_PROJECTS for an unrelated reason dropped it silently.

def test_self_project_is_derived_from_where_the_code_lives():
    """Not a string someone can get wrong or forget to update."""
    import core.native_supervisor as mod
    expected = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(mod.__file__))))
    assert ns.SELF_PROJECT == expected
    assert ns.SELF_PROJECT in ns.AUTO_REGISTER_DENY_PROJECTS


def test_an_env_override_cannot_drop_the_self_reference_guard(monkeypatch):
    monkeypatch.setenv("NATIVE_SUPERVISOR_DENY_PROJECTS", "something-unrelated")
    import importlib
    import core.native_supervisor as mod
    reloaded = importlib.reload(mod)
    try:
        assert "something-unrelated" in reloaded.AUTO_REGISTER_DENY_PROJECTS   # widening works
        assert reloaded.SELF_PROJECT in reloaded.AUTO_REGISTER_DENY_PROJECTS   # and cannot narrow
    finally:
        monkeypatch.delenv("NATIVE_SUPERVISOR_DENY_PROJECTS", raising=False)
        importlib.reload(mod)


def test_an_agent_in_the_supervisors_own_cwd_is_never_registered(monkeypatch):
    monkeypatch.setattr(ns, "_TARGETS_RAW", "")
    conn, _ = ns._conn()
    own = os.path.dirname(os.path.dirname(os.path.abspath(ns.__file__)))
    ns.auto_register([_agent(target="owner-os-self:0.0", cwd=own)], conn=conn)
    assert "owner-os-self:0.0" not in ns.registered_targets(conn=conn)


# ── the continuation cap must mean what the gate says it means (event 16047) ──────────
# The gate claims "six continuations each produced another turn boundary, so this is not
# converging". Live, it fired on two agents where that claim was false: arbitrage2-fable
# had an armed monitor and was awaiting a FINAL, and the seo worker had no assigned task
# and was simply holding.

def _sup_row(conn, target, reason, ts):
    conn.execute("INSERT INTO native_supervision (event_id,ts,ts_epoch,target,action,"
                 "reason,ok,detail) VALUES (?,?,?,?,?,?,1,'{}')",
                 (0, "t", ts, target, "skip", reason))
    conn.commit()


def test_an_intentional_wait_recorded_under_the_cwd_still_exempts_the_target():
    """The namespace split: decide() records the skip under the payload cwd, while the
    cap and the gate key on the resolved tmux target."""
    conn, _ = ns._conn()
    now = time.time()
    _sup_row(conn, "/opt/arbitrage2-fable-audit", "intentional_external_wait", now - 60)
    assert ns.gate_exemption(conn, "arbitrage2-fable:0.0",
                             "/opt/arbitrage2-fable-audit", now) == \
        "recent_intentional_external_wait"


def test_a_live_external_wait_declaration_exempts_in_either_namespace():
    conn, _ = ns._conn()
    now = time.time()
    ns.mark_external_wait("/opt/proj", reason="armed monitor", conn=conn)
    assert ns.gate_exemption(conn, "proj:0.0", "/opt/proj", now) == "intentional_external_wait"


def test_a_stale_wait_record_does_not_exempt_forever():
    conn, _ = ns._conn()
    now = time.time()
    _sup_row(conn, "/opt/old", "intentional_external_wait",
             now - ns.GATE_WAIT_LOOKBACK_SECS - 60)
    assert ns.gate_exemption(conn, "old:0.0", "/opt/old", now) != \
        "recent_intentional_external_wait"


def test_no_assigned_task_opens_the_gate_but_wakes_nobody():
    """Sending must still stop — that is the spin the cap exists to end — but an agent
    with nothing assigned was never failing to converge."""
    conn, _ = ns._conn()
    seen = []

    def emit(source, etype, **kw):
        seen.append((kw.get("severity"), kw.get("owner_action_required")))
        return {"event_id": 7}

    g = ns.open_gate("x:0.0", reason="cap", conn=conn, now=1000.0, emit_fn=emit,
                     owner_facing=False)
    assert g["opened"] is True
    assert ns.in_gate("x:0.0", conn=conn, now=1000.0) is True     # sends still stop
    assert seen == [("info", False)]                              # nobody is woken


def test_a_genuine_stall_still_wakes_the_owner():
    conn, _ = ns._conn()
    seen = []
    ns.open_gate("y:0.0", reason="cap", conn=conn, now=1000.0,
                 emit_fn=lambda s, e, **kw: seen.append(
                     (kw.get("severity"), kw.get("owner_action_required"))) or {"event_id": 8})
    assert seen == [("high", True)]


# ── both doors into the gate must apply the same exemption ───────────────────────────
# The idle sweep defaulted to owner_facing=True, so an agent with no assigned task would
# wake the owner there even though the event path had just been taught not to. Only the
# emit-level 6h dedup hid it: arbitrage2-fable opened a sweep gate at 00:39:19Z with
# gate_opened=true and stayed silent solely because event 15986 was still in the window.

def test_the_idle_sweep_gate_applies_the_same_exemption(monkeypatch):
    """It must pass owner_facing EXPLICITLY. Relying on the default is what the bug was,
    and a test that only checks the resulting severity passes either way."""
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    monkeypatch.setattr(ns, "MAX_CONSECUTIVE", 0)          # cap already reached
    conn, _ = ns._conn()
    _watch_state(conn, "cp-canary:0.0", ns.IDLE_SWEEP_QUIET_SECS + 60)
    seen = []

    def fake_gate(t, **kw):
        seen.append(kw)
        return {"opened": True, "event_id": 55}

    monkeypatch.setattr(ns, "open_gate", fake_gate)
    calls = []
    _scan(conn, [_agent()], calls)
    assert calls == []                                     # no send at the cap
    assert len(seen) == 1
    assert "owner_facing" in seen[0], "the sweep must not fall back to the default"
    assert seen[0]["owner_facing"] is False                # no assigned task -> silent


def test_the_idle_sweep_skips_entirely_on_an_intentional_wait(monkeypatch):
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    monkeypatch.setattr(ns, "MAX_CONSECUTIVE", 0)
    conn, _ = ns._conn()
    _watch_state(conn, "cp-canary:0.0", ns.IDLE_SWEEP_QUIET_SECS + 60)
    ns.mark_external_wait("/root/cp-canary-v2", reason="armed monitor", conn=conn)
    opened = []
    monkeypatch.setattr(ns, "open_gate", lambda t, **kw: opened.append(t) or {"opened": True})
    _scan(conn, [_agent()], [])
    assert opened == []                                    # no gate at all


# ── deterministic proof of the suppression branch, without the dedup confound ─────────
# Live, this branch is masked: every candidate target already had a gate event inside the
# 6h `nativesup:gate:<target>` dedup window, so silence in production proves nothing. An
# isolated DB has no prior gate event, so the emit is unambiguous. `open_gate` is REAL
# here — only the emit sink is captured — so this exercises the whole wiring from the cap
# check through gate_exemption to the severity actually requested.

def test_end_to_end_no_assigned_task_gates_silently(monkeypatch):
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    monkeypatch.setattr(ns, "MAX_CONSECUTIVE", 0)          # cap reached on the first look
    monkeypatch.setattr(ns, "IDLE_SWEEP_ENABLED", False)   # isolate the EVENT path
    conn, _ = ns._conn()
    _hook_event(conn)
    emitted = []

    def emit(source, etype, **kw):
        emitted.append({"type": etype, "severity": kw.get("severity"),
                        "oar": kw.get("owner_action_required")})
        return {"event_id": 4242}

    monkeypatch.setattr(ns, "_conn", ns._conn)             # unchanged; explicit for clarity
    import core.control_plane.cto as _cto
    monkeypatch.setattr(_cto, "emit", emit)
    calls = []
    _scan(conn, [_agent()], calls)
    assert calls == [], "no send once the cap is reached"
    assert len(emitted) == 1, f"exactly one gate event, got {emitted}"
    assert emitted[0]["type"] == "agent_continuation_exhausted"
    assert emitted[0]["severity"] == "info"
    assert emitted[0]["oar"] is False                      # gate opens, nobody is woken
    assert ns.in_gate("cp-canary:0.0", conn=conn) is True   # and sends really do stop


def test_end_to_end_a_genuine_stall_still_wakes_the_owner(monkeypatch):
    """The control: same path, but with work to converge on, must stay owner-facing."""
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    monkeypatch.setattr(ns, "MAX_CONSECUTIVE", 0)
    monkeypatch.setattr(ns, "IDLE_SWEEP_ENABLED", False)
    monkeypatch.setattr(ns, "gate_exemption", lambda *a, **k: "")   # nothing innocent
    conn, _ = ns._conn()
    _hook_event(conn)
    emitted = []
    import core.control_plane.cto as _cto
    monkeypatch.setattr(_cto, "emit",
                        lambda s, e, **kw: emitted.append(
                            (kw.get("severity"), kw.get("owner_action_required")))
                        or {"event_id": 7})
    _scan(conn, [_agent()], [])
    assert emitted == [("high", True)]


# ── a gate alarm must name its project, or it cannot be routed ────────────────────────
# All four agent_continuation_exhausted events raised on 2026-08-30 carried an EMPTY
# project_id, so they could not route to the project's own chat and landed project-less.

def test_the_gate_alarm_carries_the_project(tmp_path):
    conn = _gate_conn(tmp_path)
    conn.execute(ns._REG_SCHEMA)
    conn.execute("INSERT INTO native_supervised_target(target,project,cwd) VALUES(?,?,?)",
                 ("seo-worker:0.0", "seo", "/opt/seo"))
    conn.commit()
    seen = []
    ns.open_gate("seo-worker:0.0", reason="cap", conn=conn, now=1000.0,
                 emit_fn=lambda s, e, **kw: seen.append(kw.get("project_id")) or {"event_id": 1})
    assert seen == ["seo"]


def test_an_explicit_project_wins_over_the_registry(tmp_path):
    conn = _gate_conn(tmp_path)
    conn.execute(ns._REG_SCHEMA)
    conn.execute("INSERT INTO native_supervised_target(target,project,cwd) VALUES(?,?,?)",
                 ("x:0.0", "from-registry", "/opt/x"))
    conn.commit()
    seen = []
    ns.open_gate("x:0.0", reason="cap", conn=conn, now=1000.0, project_id="explicit",
                 emit_fn=lambda s, e, **kw: seen.append(kw.get("project_id")) or {"event_id": 1})
    assert seen == ["explicit"]


def test_an_unregistered_target_still_emits(tmp_path):
    """No registry row must not break the alarm — it only loses the routing hint."""
    conn = _gate_conn(tmp_path)
    seen = []
    ns.open_gate("unknown:0.0", reason="cap", conn=conn, now=1000.0,
                 emit_fn=lambda s, e, **kw: seen.append(kw.get("project_id")) or {"event_id": 1})
    assert seen == [""]
