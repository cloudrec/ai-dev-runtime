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


def test_an_agent_outside_the_rollout_is_not_touched(monkeypatch):
    monkeypatch.setattr(ns, "_TARGETS_RAW", "cp-canary:0.0")
    conn, _ = ns._conn()
    _hook_event(conn, cwd="/opt/mess")
    calls = []
    r = _scan(conn, [_agent(target="mess-opus:0.0", cwd="/opt/mess")], calls)
    assert calls == [] and r["skipped"][0]["why"] == "not_in_rollout_allowlist"


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
