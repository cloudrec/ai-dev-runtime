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
