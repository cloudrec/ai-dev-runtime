"""Continue managed agents from NATIVE lifecycle signals, without ChatGPT in the loop.

The old normal path was: scrape a pane, classify prose, wake ChatGPT, let ChatGPT continue
the agent — three inferences and a browser between "a turn ended" and "keep going". These
tests pin what the supervisor may do, and far more importantly what it may NOT.
"""
from __future__ import annotations

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
