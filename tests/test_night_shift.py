"""Night Shift executive — phase 2 (event bus + skeleton).

The failure mode an autonomous executive actually has is DOING TOO MUCH: inventing work,
repeating itself, and spending tokens to look alive. These tests pin the brakes first.
"""
from __future__ import annotations

import pytest

from core import night_shift as ns


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    yield


def test_a_signal_makes_the_executive_wake_now():
    sid = ns.signal("os_task_queue", "task_failed", target="cp-canary:0.0")
    pend = ns.pending_signals()
    assert [p["id"] for p in pend] == [sid]
    assert pend[0]["kind"] == "task_failed"


def test_a_consumed_signal_is_never_replayed():
    sid = ns.signal("health", "service_down")
    ns.consume_signals([sid])
    assert ns.pending_signals() == []


def test_a_missed_signal_costs_latency_not_correctness():
    """The tick is the floor: a pass with no signals still observes and records."""
    out = ns.executive_pass(trigger="tick")
    assert out["trigger"] == "tick" and out["observed"] == 0
    assert isinstance(out["findings"], list)


def test_an_identical_proposal_inside_the_cooldown_is_suppressed():
    a = ns.should_propose("cp-canary:0.0", "research", "screen promotion ideas", now=1000.0)
    b = ns.should_propose("cp-canary:0.0", "research", "screen promotion ideas", now=1100.0)
    assert a["propose"] is True
    assert b["propose"] is False and b["reason"] == "duplicate_proposal_in_cooldown"


def test_the_same_proposal_is_allowed_again_after_the_cooldown():
    ns.should_propose("x:0.0", "research", "same idea", now=1000.0)
    later = ns.should_propose("x:0.0", "research", "same idea",
                              now=1000.0 + ns.PROPOSAL_COOLDOWN_SECS + 1)
    assert later["propose"] is True


def test_a_target_with_an_active_task_gets_no_second_one():
    from core import os_task_queue as q
    t = q.enqueue("cp-canary:0.0", "already running")
    q.set_state(t["id"], q.WORKING)
    r = ns.should_propose("cp-canary:0.0", "research", "something else entirely")
    assert r["propose"] is False and r["reason"] == "target_already_has_active_task"


def test_inflight_counts_only_live_tasks():
    from core import os_task_queue as q
    a = q.enqueue("a:0.0", "one")
    b = q.enqueue("b:0.0", "two")
    assert ns.inflight() == 0, "queued work is not yet in flight"
    q.set_state(a["id"], q.SUBMITTED)
    q.set_state(b["id"], q.DONE)
    assert ns.inflight() == 1


def test_a_paused_project_is_a_note_not_a_blocker(monkeypatch):
    from core import continuation_governor as cg
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: {
        "arbitrage2-opus:0.0": {"enabled": False, "project": "arbitrage2"},
        "cp-canary:0.0": {"enabled": True, "project": "cp-canary"}})
    findings = ns.diagnose(ns.observe())
    paused = [f for f in findings if f["kind"] == "project_paused"]
    assert [p["target"] for p in paused] == ["arbitrage2-opus:0.0"]
    assert paused[0]["severity"] == "info", "paused is not a problem"


def test_critical_findings_are_handled_before_noise():
    ordered = ns.prioritize([{"severity": "info", "kind": "a"},
                             {"severity": "critical", "kind": "b"},
                             {"severity": "high", "kind": "c"}])
    assert [f["kind"] for f in ordered] == ["b", "c", "a"]


def test_a_pass_records_what_it_saw_for_audit():
    ns.signal("test", "something", target="t:0.0")
    ns.executive_pass(trigger="signal")
    import sqlite3, os
    c = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    row = c.execute("SELECT trigger,observed FROM ns_pass ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "signal" and row[1] == 1


def test_phase_2_creates_no_work_at_all():
    """Tier 0: an executive that is merely alive must not manufacture tasks, and must not
    spend a single token to prove it is running."""
    from core import os_task_queue as q
    ns.signal("test", "wake", target="cp-canary:0.0")
    out = ns.executive_pass(trigger="signal")
    assert out["acted"] == []
    assert q._list("1=1") == [], "no task rows may be created by an executive pass"


# ── the signal source: cto.emit feeds the executive ────────────────────────
def test_a_high_severity_event_wakes_the_executive():
    from core.control_plane import cto
    cto.emit("os_task_queue", "task_failed", agent_id="cp-canary:0.0", severity="high",
             payload={"task_id": "t1"})
    kinds = [s["kind"] for s in ns.pending_signals()]
    assert "task_failed" in kinds


def test_an_owner_decision_wakes_the_executive_even_at_low_severity():
    from core.control_plane import cto
    cto.emit("governor", "needs_owner_payload", agent_id="mess:0.0", severity="info",
             owner_action_required=True)
    assert [s["kind"] for s in ns.pending_signals()] == ["needs_owner_payload"]


def test_routine_info_events_do_not_wake_the_executive():
    """Noise suppression: waking on every info event would make the signal meaningless."""
    from core.control_plane import cto
    cto.emit("discovery", "agent_seen", agent_id="x:0.0", severity="info")
    assert ns.pending_signals() == []


def test_event_recording_survives_an_unavailable_executive(monkeypatch):
    """The signal is an accelerator. Emitting an event must never fail because the
    executive's table is unreachable."""
    from core.control_plane import cto
    from core import night_shift as _ns
    def _boom(*a, **k):
        raise RuntimeError("ns table gone")
    monkeypatch.setattr(_ns, "signal", _boom)
    out = cto.emit("os_task_queue", "task_failed", agent_id="c:0.0", severity="critical")
    assert out["event_id"] > 0, "the event is recorded regardless"
