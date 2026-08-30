"""Direct-agent lifecycle watcher — reliable completion / interruption events for
tmux agents OUTSIDE the orchestrator plan.

Ownership: the inline `agent_watcher.transition_event` path already emits
completion / owner-decision / waiting for a direct agent on any observed ALIVE
transition. This module owns the DEAD/VANISHED pane the orchestrator sweep skips
before it can compute a transition — the confirmed ezetta-video miss (a session
that finished + exited with reports and produced NO event). For an ALIVE pane it
stays silent (records only). Covers: baseline silence, finish-then-exit
completion, SIGKILL/mid-run interruption, never-completion-on-death, resumed
conversations, dedup across polls / monitor restart, delivery failure, metrics.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core import direct_agent_lifecycle as dal


# ── harness ───────────────────────────────────────────────────────────────────
NOW = 1_700_000_000.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _report(ago_secs: float, path: str = "reports/DONE.md") -> dict:
    return {"path": path, "modified_at": _iso(NOW - ago_secs)}


def _agent(target="ezetta:0.0", state="idle", alive=True, cwd="/opt/ezetta-video", tail=""):
    return {"target": target, "session": target.split(":")[0], "is_agent": True,
            "alive": alive, "state": state, "claude_cwd": cwd, "_tail": tail}


def _obs(agent, *, reports=None, conv="conv-A", vanished=False, now=NOW):
    return dal.build_observation(agent, now_ts=now, reports=reports or [],
                                 conversation_id=conv, vanished=vanished)


def _prev(state="working", conv="conv-A", completion_emitted=False, cwd="/opt/ezetta-video"):
    return {"state": state, "conversation_id": conv, "completion_emitted": completion_emitted,
            "cwd": cwd, "first_idle_ts": None}


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(dal, "ENABLED", True)
    yield


# ── completion evidence (multi-signal, fail closed) ──────────────────────────
def test_completion_evidence_requires_fresh_report():
    ev = dal.completion_evidence(_obs(_agent(state="idle"), reports=[_report(30)]))
    assert ev and ev["report_path"] == "reports/DONE.md"


def test_completion_evidence_none_when_report_stale():
    cur = _obs(_agent(state="idle"), reports=[_report(dal.COMPLETION_WINDOW_SECS + 100)])
    assert dal.completion_evidence(cur) is None


def test_completion_evidence_none_when_child_running_or_active_markers():
    assert dal.completion_evidence(_obs(_agent(state="shell_running"), reports=[_report(5)])) is None
    busy = _obs(_agent(state="idle", tail="… esc to interrupt"), reports=[_report(5)])
    assert dal.completion_evidence(busy) is None


def test_completion_evidence_none_without_report():
    assert dal.completion_evidence(_obs(_agent(state="idle"), reports=[])) is None


# ── baseline: an existing idle session is NEVER notified on first observation ─
def test_baseline_existing_idle_is_silent_even_with_fresh_report():
    # inline transition_event owns live completion; the module must not retro-fire
    # for a session it is seeing for the first time.
    out = dal.decide(None, _obs(_agent(state="idle"), reports=[_report(20)]))
    assert out["metric"] in ("baseline_silenced", "completion_recognised")
    assert "event" not in out           # never a notification at baseline


def test_baseline_no_report_is_silent():
    out = dal.decide(None, _obs(_agent(state="idle"), reports=[]))
    assert out["metric"] == "baseline_silenced" and "event" not in out


# ── alive transitions: recorded, but the module NEVER notifies (inline owns) ──
def test_alive_completion_is_recorded_not_emitted():
    out = dal.decide(_prev(state="working"), _obs(_agent(state="idle"), reports=[_report(10)]))
    assert out["metric"] == "completion_recognised" and out.get("completed") is True
    assert "event" not in out           # inline emits the live completion, not us


def test_alive_completion_not_re_emitted_when_flag_already_set():
    out = dal.decide(_prev(state="idle", completion_emitted=True),
                     _obs(_agent(state="idle"), reports=[_report(10)]))
    assert "event" not in out


def test_alive_waiting_owner_is_silent_inline_owns_it():
    out = dal.decide(_prev(state="working"), _obs(_agent(state="waiting_owner")))
    assert out["metric"] == "noop" and "event" not in out


def test_alive_idle_no_evidence_after_work_fails_closed():
    out = dal.decide(_prev(state="working"), _obs(_agent(state="idle"), reports=[]))
    assert out["metric"] == "insufficient_evidence_suppressed" and "event" not in out


def test_alive_shell_running_is_not_a_finish():
    out = dal.decide(_prev(state="working"), _obs(_agent(state="shell_running"), reports=[_report(5)]))
    assert out["metric"] == "noop" and "event" not in out


# ── the ezetta fix: DEAD/exited pane the inline path structurally cannot see ──
def test_finish_then_clean_exit_emits_completion():
    # last live obs was idle (finished) with NO report yet; report landed, then the
    # pane exited → a completion the inline path never saw.
    prev = _prev(state="idle", completion_emitted=False)
    cur = _obs(_agent(alive=False, state="dead", tail=""), reports=[_report(15)], vanished=True)
    out = dal.decide(prev, cur)
    assert out["metric"] == "completion_candidate" and out["completed"] is True
    assert out["event"]["event_type"] == dal.EVENT_COMPLETED
    assert out["event"]["payload"]["to_state"] == "exited_after_completion"
    assert out["event"]["payload"]["owner_action_required"] is False


def test_working_to_dead_is_interruption_never_completion():
    out = dal.decide(_prev(state="working"), _obs(_agent(alive=False, state="dead"), reports=[]))
    assert out["metric"] == "dead_candidate"
    assert out["event"]["event_type"] == dal.EVENT_INTERRUPTED
    assert out["event"]["kind"] == "interrupted"
    assert out["event"]["payload"]["owner_action_required"] is True
    assert out["event"]["payload"]["classification"] == "interruption"


def test_sigkill_midrun_is_interruption_even_with_a_report():
    # a report on disk must NOT turn a mid-run SIGKILL into a completion.
    prev = _prev(state="working")
    cur = _obs(_agent(alive=False, state="dead", tail="… running… esc to interrupt"),
               reports=[_report(15)])
    out = dal.decide(prev, cur)
    assert out["event"]["event_type"] == dal.EVENT_INTERRUPTED


def test_vanished_while_active_is_interruption_with_reports_in_payload():
    prev = _prev(state="working")
    cur = _obs(_agent(alive=False, state="dead"), reports=[_report(15)], vanished=True)
    out = dal.decide(prev, cur)
    assert out["event"]["event_type"] == dal.EVENT_INTERRUPTED
    assert out["event"]["payload"]["newest_reports"] == ["reports/DONE.md"]


def test_dead_after_completion_is_not_re_alerted():
    out = dal.decide(_prev(state="idle", completion_emitted=True),
                     _obs(_agent(alive=False, state="dead")))
    assert out["metric"] == "dead_after_completion_ignored" and "event" not in out


def test_first_sight_already_dead_is_silent():
    out = dal.decide(None, _obs(_agent(alive=False, state="dead")))
    assert out["metric"] == "baseline_silenced" and "event" not in out


# ── resumed conversation re-baselines (prior state does not leak) ────────────
def test_resumed_conversation_sets_reset_flag():
    prev = _prev(state="idle", conv="conv-A", completion_emitted=True)
    cur = _obs(_agent(state="idle"), reports=[_report(10)], conv="conv-B")   # NEW conversation
    out = dal.decide(prev, cur)
    assert out.get("reset") is True


# ── missing cwd / conversation identity → no crash, silent ───────────────────
def test_missing_cwd_and_conversation_is_silent():
    a = _agent(cwd="")
    out = dal.decide(None, dal.build_observation(a, now_ts=NOW, reports=[], conversation_id=None))
    assert "event" not in out


# ── persistence round-trip ───────────────────────────────────────────────────
def test_store_roundtrip():
    dal.save_obs({"target": "ezetta:0.0", "conversation_id": "conv-A", "cwd": "/opt/x",
                  "state": "idle", "alive": True, "first_idle_ts": NOW, "last_seen_ts": NOW,
                  "completion_emitted": False, "last_report_path": None})
    got = dal.get_obs("ezetta:0.0")
    assert got and got["state"] == "idle" and got["completion_emitted"] is False


# ── sweep integration ────────────────────────────────────────────────────────
def _emit_capture(store):
    def emit(agent, project, event_type, payload, dedup_key="", dedup_window_secs=0):
        key = (agent, event_type, dedup_key)
        if key in store:
            return False                       # emulate record_commander_event dedup
        store[key] = payload
        return True
    return emit


def test_sweep_skips_configured_sessions():
    emitted = {}
    inv = {"agents": [_agent(target="seo-audit:0.0", state="idle")]}
    res = dal.sweep(inv, configured_sessions={"seo-audit"},
                    report_fn=lambda cwd: [_report(5)], conversation_fn=lambda cwd: "c",
                    tail_fn=lambda t: "", emit_fn=_emit_capture(emitted), now_ts=NOW)
    assert res["observed"] == 0 and res["events"] == [] and not emitted


def test_sweep_alive_direct_agent_is_silent():
    emitted = {}
    inv = {"agents": [_agent(target="ezetta:0.0", state="idle")]}
    res = dal.sweep(inv, configured_sessions=set(),
                    report_fn=lambda cwd: [_report(15)], conversation_fn=lambda cwd: "conv-A",
                    tail_fn=lambda t: "all six masters rendered", emit_fn=_emit_capture(emitted),
                    now_ts=NOW)
    assert res["events"] == [] and not emitted        # inline owns the live completion


def test_sweep_vanished_direct_agent_emits_interruption_once():
    emitted = {}
    inv1 = {"agents": [_agent(target="ezetta:0.0", state="working")]}
    dal.sweep(inv1, configured_sessions=set(), report_fn=lambda c: [], conversation_fn=lambda c: "conv-A",
              tail_fn=lambda t: "", emit_fn=_emit_capture(emitted), now_ts=NOW)
    # pane gone → interruption
    res = dal.sweep({"agents": []}, configured_sessions=set(), report_fn=lambda c: [],
                    conversation_fn=lambda c: None, tail_fn=lambda t: "",
                    emit_fn=_emit_capture(emitted), now_ts=NOW + 30)
    assert [e["kind"] for e in res["events"]] == ["interrupted"]
    # repeated poll (still gone) → dedup, no second event
    res2 = dal.sweep({"agents": []}, configured_sessions=set(), report_fn=lambda c: [],
                     conversation_fn=lambda c: None, tail_fn=lambda t: "",
                     emit_fn=_emit_capture(emitted), now_ts=NOW + 60)
    assert res2["events"] == []


def test_sweep_delivery_failure_is_counted_not_raised():
    def boom(*a, **k):
        raise RuntimeError("sink down")
    emitted = {}
    inv1 = {"agents": [_agent(target="ezetta:0.0", state="working")]}
    dal.sweep(inv1, configured_sessions=set(), report_fn=lambda c: [], conversation_fn=lambda c: "conv-A",
              tail_fn=lambda t: "", emit_fn=_emit_capture(emitted), now_ts=NOW)
    res = dal.sweep({"agents": []}, configured_sessions=set(), report_fn=lambda c: [],
                    conversation_fn=lambda c: None, tail_fn=lambda t: "", emit_fn=boom, now_ts=NOW + 30)
    assert res["events"] == []
    assert res["metrics_delta"].get("emit_error", 0) >= 1


def test_metrics_accumulate_and_read_back():
    dal.bump_metric("agents_observed", 3)
    dal.bump_metric("interruptions_emitted", 1)
    m = dal.metrics()
    assert m["agents_observed"] == 3 and m["interruptions_emitted"] == 1
