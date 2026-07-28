"""Source-side retraction of stale commander events on recovery/completion."""
from __future__ import annotations

import pytest

from core import agent_control as ac


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "cmd.db"))


def _unacked():
    return ac.list_commander_events(unacked_only=True, limit=100)


def test_retract_acks_condition_events_when_active_and_records_lineage():
    ac.record_commander_event("a:0.0", "p", "agent_externally_blocked",
                              {"to_state": "externally_blocked"}, dedup_key="b1")
    ac.record_commander_event("a:0.0", "p", "agent_process_failed", {}, dedup_key="pf1")
    ac.record_commander_event("a:0.0", "p", "task_completed_no_remaining_work", {}, dedup_key="c1")
    rids = ac.retract_stale_condition_events("a:0.0", "working")
    assert len(rids) == 2
    types = {e["event_type"] for e in _unacked()}
    assert "agent_externally_blocked" not in types and "agent_process_failed" not in types
    assert "task_completed_no_remaining_work" in types      # non-condition untouched
    assert "commander_event_retracted" in types             # lineage marker recorded
    # lineage carries exact event id + stable subject_key
    marker = next(e for e in _unacked() if e["event_type"] == "commander_event_retracted")
    assert marker["payload"]["subject_key"] == "agent:a:0.0"
    assert marker["payload"]["retracted_event_id"] in rids


def test_retract_noop_when_not_active():
    ac.record_commander_event("a:0.0", "p", "agent_externally_blocked", {}, dedup_key="b1")
    assert ac.retract_stale_condition_events("a:0.0", "idle") == []
    assert ac.retract_stale_condition_events("a:0.0", "externally_blocked") == []   # still blocked → keep
    assert any(e["event_type"] == "agent_externally_blocked" for e in _unacked())


def test_retract_idempotent_and_restart_safe():
    ac.record_commander_event("a:0.0", "p", "agent_unexpected_idle", {}, dedup_key="s1")
    assert len(ac.retract_stale_condition_events("a:0.0", "working")) == 1
    # re-run (a later sweep / restart) → already acked → no double retraction
    assert ac.retract_stale_condition_events("a:0.0", "working") == []
    markers = [e for e in _unacked() if e["event_type"] == "commander_event_retracted"]
    assert len(markers) == 1                                # exactly one lineage marker


def test_retract_exact_agent_only():
    ac.record_commander_event("a:0.0", "p", "agent_externally_blocked", {}, dedup_key="a1")
    ac.record_commander_event("b:0.0", "p", "agent_externally_blocked", {}, dedup_key="b1")
    ac.retract_stale_condition_events("a:0.0", "completed")
    pairs = {(e["agent"], e["event_type"]) for e in _unacked()}
    assert ("b:0.0", "agent_externally_blocked") in pairs   # other agent untouched
    assert ("a:0.0", "agent_externally_blocked") not in pairs
