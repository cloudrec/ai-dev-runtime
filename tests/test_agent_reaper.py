"""Reaper / reconciliation for vanished supervised sessions."""
from __future__ import annotations

import pytest

from core import agent_orchestrator as ao


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "reaper.db"))


def _seed(agent_key, session, state, goal=None, task=None):
    ao._upsert({"agent_key": agent_key, "session": session, "project": session,
                "approved_goal": goal, "current_task": task, "phase": None, "state": state,
                "last_fresh_activity_ts": 1.0, "prompt_hash": None, "blocker_category": None,
                "completion_evidence": None, "report_path": None, "approved_next_task": None,
                "notification_state": None, "retry_count": 0, "decision": None,
                "blocker_text": None, "decision_type": None, "context_pct": None,
                "context_tier": None, "exec_mode": None})


def _state(agent_key):
    return ao.get_record(agent_key)["state"]


def test_live_session_is_not_reaped():
    _seed("job:0.0", "job", "idle", goal="JobHunter V1")
    emits = []
    reaped = ao.reap_vanished({"job"}, emit=lambda *a: emits.append(a))
    assert reaped == [] and emits == []           # pane present → never reaped
    assert _state("job:0.0") == "idle"


def test_vanished_with_approved_work_reaps_and_emits_once():
    _seed("job:0.0", "job", "idle", goal="JobHunter V1")
    emits = []
    reaped = ao.reap_vanished(set(), emit=lambda *a: emits.append(a))     # no live sessions
    assert len(reaped) == 1 and reaped[0]["had_approved_unfinished_work"] is True
    assert len(emits) == 1                        # ONE owner event
    assert _state("job:0.0") == "vanished"        # atomically marked ended


def test_vanished_without_approved_work_reaps_but_no_event():
    _seed("scratch:0.0", "scratch", "idle")       # no goal/task
    emits = []
    reaped = ao.reap_vanished(set(), emit=lambda *a: emits.append(a))
    assert len(reaped) == 1 and reaped[0]["had_approved_unfinished_work"] is False
    assert emits == []                            # no work → no owner alert
    assert _state("scratch:0.0") == "vanished"


def test_completed_vanished_is_not_flagged_unfinished():
    _seed("done:0.0", "done", "completed", goal="finished thing")
    emits = []
    ao.reap_vanished(set(), emit=lambda *a: emits.append(a))
    assert emits == []                            # completed ≠ unfinished


def test_reap_is_idempotent_and_race_safe():
    _seed("job:0.0", "job", "idle", goal="JobHunter V1")
    emits = []
    ao.reap_vanished(set(), emit=lambda *a: emits.append(a))
    # second pass (a concurrent sweep / restart) must NOT re-reap or re-emit
    reaped2 = ao.reap_vanished(set(), emit=lambda *a: emits.append(a))
    assert reaped2 == [] and len(emits) == 1
    assert _state("job:0.0") == "vanished"


def test_restart_then_new_pane_recovers_record():
    _seed("job:0.0", "job", "idle", goal="JobHunter V1")
    ao.reap_vanished(set())
    assert _state("job:0.0") == "vanished"
    # owner restarts the agent → a live pane re-upserts a fresh state
    _seed("job:0.0", "job", "working", goal="JobHunter V1")
    assert _state("job:0.0") == "working"         # not stuck vanished


# ── a pane can die inside a session that keeps running ──────────────────────
# `hostsecure-clean:1.0` died 2026-09-13 09:37 and agent_watch raised a critical
# agent_process_failed for it. Window 0 of the same session was still alive, so the
# session appeared in `live_sessions`, this reaper skipped the record, and it sat at
# state `working` for hours. A dead agent reporting `working` is the one state nothing
# looks for: the reaper will not touch it and nobody is told the work is unfinished.

def test_a_dead_pane_in_a_live_session_is_reaped():
    _seed("hostsecure-clean:1.0", "hostsecure-clean", "working", goal="finish the audit")
    emits = []
    reaped = ao.reap_vanished({"hostsecure-clean"}, emit=lambda *a: emits.append(a),
                              live_targets={"hostsecure-clean:0.0"})
    assert [r["agent"] for r in reaped] == ["hostsecure-clean:1.0"]
    assert _state("hostsecure-clean:1.0") == "vanished"
    assert emits, "it carried approved unfinished work — that must be said once"


def test_the_surviving_pane_of_that_session_is_untouched():
    _seed("hostsecure-clean:0.0", "hostsecure-clean", "working", goal="still going")
    _seed("hostsecure-clean:1.0", "hostsecure-clean", "working", goal="dead")
    ao.reap_vanished({"hostsecure-clean"}, live_targets={"hostsecure-clean:0.0"})
    assert _state("hostsecure-clean:0.0") == "working"
    assert _state("hostsecure-clean:1.0") == "vanished"


def test_without_live_targets_the_old_session_only_behaviour_is_unchanged():
    """Callers that pass no pane inventory must behave exactly as before."""
    _seed("hostsecure-clean:1.0", "hostsecure-clean", "working", goal="g")
    assert ao.reap_vanished({"hostsecure-clean"}) == []
    assert _state("hostsecure-clean:1.0") == "working"


def test_an_empty_pane_inventory_reaps_nothing():
    """Absence of evidence is not evidence of death — the same rule the caller already
    enforces for sessions, restated here so the function is safe on its own."""
    _seed("job:0.0", "job", "working", goal="g")
    assert ao.reap_vanished({"job"}, live_targets=set()) == []
    assert _state("job:0.0") == "working"


def test_a_record_whose_key_is_not_pane_shaped_is_judged_by_session_only():
    """Some keys are not tmux targets. They must not be reaped merely for being
    absent from a pane list they could never appear in."""
    _seed("some-logical-key", "job", "working", goal="g")
    assert ao.reap_vanished({"job"}, live_targets={"job:0.0"}) == []
    assert _state("some-logical-key") == "working"


def test_the_pane_rule_is_idempotent():
    _seed("hostsecure-clean:1.0", "hostsecure-clean", "working", goal="g")
    first = ao.reap_vanished({"hostsecure-clean"}, live_targets={"hostsecure-clean:0.0"})
    second = ao.reap_vanished({"hostsecure-clean"}, live_targets={"hostsecure-clean:0.0"})
    assert len(first) == 1 and second == []
